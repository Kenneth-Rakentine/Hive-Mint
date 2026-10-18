import json

import pytest
from sqlalchemy import select

from agents.departments import STUB_HANDLERS, StageResult
from agents.operations import ROUTING, OperationsManager, default_handlers
from app.lifecycle import transition
from app.models import AgentRun, Event, Job
from app.states import Department, JobStatus, Outcome, ProductStatus as S
from tests.conftest import advance_to


def test_routing_covers_every_status():
    assert set(ROUTING) == set(S)


@pytest.mark.parametrize("status,department", [
    (S.IDEA, Department.OPERATIONS),
    (S.RESEARCH, Department.RESEARCH),
    (S.FINANCE_REVIEW, Department.FINANCE),
    (S.SPECIFICATION, Department.PRODUCT_DESIGN),
    (S.PRODUCTION, Department.PRODUCTION),
    (S.TESTING, Department.TESTING),
    (S.QA, Department.QA),
    (S.RED_TEAM, Department.RED_TEAM),
    (S.COMPLIANCE, Department.COMPLIANCE),
    (S.SEO, Department.SEO),
    (S.REWORK, Department.PRODUCT_DESIGN),
])
def test_routing_table(status, department):
    assert ROUTING[status] == department


@pytest.mark.parametrize("status", [S.HUMAN_REVIEW, S.READY_TO_PUBLISH, S.PUBLISHED, S.PAUSED,
                                    S.ERROR, S.REJECTED, S.RETIRED])
def test_no_automated_owner_for_human_or_closed_states(status):
    assert ROUTING[status] is None


def test_every_routed_department_has_a_handler(settings):
    assert {d for d in ROUTING.values() if d} <= set(STUB_HANDLERS)
    assert set(default_handlers(settings)) == set(STUB_HANDLERS)


def test_testing_qa_and_red_team_are_separate_departments():
    assert len({ROUTING[S.TESTING], ROUTING[S.QA], ROUTING[S.RED_TEAM]}) == 3


def test_next_job_creates_job_for_routed_department_without_duplicates(session, settings, make_product):
    ops = OperationsManager(session, settings)
    product = advance_to(session, settings, make_product(), S.FINANCE_REVIEW)
    job = ops.next_job(product)
    assert job.department == Department.FINANCE
    assert job.status == JobStatus.QUEUED and job.for_status == S.FINANCE_REVIEW
    assert job.max_attempts == settings.max_job_attempts
    assert ops.next_job(product) is None
    assert len(session.scalars(select(Job)).all()) == 1


def test_human_review_gets_no_job(session, settings, make_product):
    product = advance_to(session, settings, make_product(), S.HUMAN_REVIEW)
    assert OperationsManager(session, settings).next_job(product) is None


def test_build_limit_defers_fourth_build(session, settings, make_product):
    from dataclasses import replace

    settings = replace(settings, max_products_built_per_day=10)  # only the total build limit applies here
    ops = OperationsManager(session, settings)
    products = [advance_to(session, settings, make_product(), S.PRODUCTION) for _ in range(4)]
    jobs = [ops.next_job(p) for p in products]
    assert [j is not None for j in jobs] == [True, True, True, False]
    assert ops.next_job(products[3]) is None  # still deferred, logged only once
    deferred = session.scalars(select(Event).where(Event.event_type == "job.deferred")).all()
    assert len(deferred) == 1 and deferred[0].product_id == products[3].id


def test_job_runs_and_advances_product(session, settings, make_product):
    ops = OperationsManager(session, settings)
    product = make_product()
    job = ops.next_job(product)
    ops.run_job(job)
    assert job.status == JobStatus.SUCCEEDED and job.attempts == 1
    assert product.status == S.RESEARCH
    assert json.loads(job.output_json)["outcome"] == "PASS"


def test_failed_handler_retries_then_errors(session, settings, make_product):
    calls = []

    def broken(product, job):
        calls.append(job.attempts)
        raise RuntimeError("generator crashed")

    ops = OperationsManager(session, settings, handlers={**STUB_HANDLERS, Department.OPERATIONS: broken})
    product = make_product()
    job = ops.next_job(product)
    ops.run_job(job)
    assert calls == [1, 2, 3]
    assert job.status == JobStatus.FAILED and "generator crashed" in job.error
    assert product.status == S.ERROR and product.paused_from == S.IDEA
    runs = session.scalars(select(AgentRun).where(AgentRun.job_id == job.id)).all()
    assert [r.status for r in runs] == [JobStatus.FAILED] * 3


def test_handler_returning_illegal_move_is_treated_as_failure(session, settings, make_product):
    def cheater(product, job):
        return StageResult(Outcome.PASS, S.PUBLISHED, "skip everything")

    ops = OperationsManager(session, settings, handlers={**STUB_HANDLERS, Department.OPERATIONS: cheater})
    product = make_product()
    ops.run_job(ops.next_job(product))
    assert product.status == S.ERROR


def test_stale_job_is_cancelled(session, settings, make_product):
    ops = OperationsManager(session, settings)
    product = make_product()
    job = ops.next_job(product)
    transition(session, settings, product, S.RESEARCH, actor="owner", reason="manual")
    ops.run_job(job)
    assert job.status == JobStatus.CANCELLED
    assert product.status == S.RESEARCH


def test_rework_routes_back_to_production_after_testing_failure(session, settings, make_product):
    ops = OperationsManager(session, settings)
    product = advance_to(session, settings, make_product(), S.TESTING)
    transition(session, settings, product, S.REWORK, actor="testing", reason="totals do not reconcile")
    job = ops.next_job(product)
    assert job.department == Department.PRODUCT_DESIGN
    ops.run_job(job)
    assert product.status == S.PRODUCTION


def test_tick_drives_idea_to_human_review_and_stops(session, settings, make_product):
    ops = OperationsManager(session, settings, handlers=STUB_HANDLERS)
    product = make_product()
    session.commit()
    total = 0
    while ran := ops.tick():
        total += ran
    assert product.status == S.HUMAN_REVIEW
    assert total == 10  # IDEA .. SEO: one job per gate
    jobs = session.scalars(select(Job).order_by(Job.id)).all()
    assert [j.for_status for j in jobs] == [S.IDEA, S.RESEARCH, S.FINANCE_REVIEW, S.SPECIFICATION, S.PRODUCTION,
                                            S.TESTING, S.QA, S.RED_TEAM, S.COMPLIANCE, S.SEO]
    assert all(j.status == JobStatus.SUCCEEDED for j in jobs)


# --------------------------------------------------------------------------- v0.4: hard stops and limits

def _flaky(failures, exc=RuntimeError("headless call failed")):
    calls = {"n": 0}

    def handler(product, job):
        calls["n"] += 1
        if calls["n"] <= failures:
            raise exc
        return StageResult(Outcome.PASS, S.SPECIFICATION, "ok")

    handler.calls = calls
    return handler


def _ops_with(session, settings, department, handler):
    return OperationsManager(session, settings, handlers={**STUB_HANDLERS, department: handler})


def test_headless_failure_is_retried_within_the_bound_then_succeeds(session, settings, make_product):
    handler = _flaky(2)
    ops = _ops_with(session, settings, Department.FINANCE, handler)
    product = advance_to(session, settings, make_product(), S.FINANCE_REVIEW)
    job = ops.run_job(ops.next_job(product))
    assert job.status == JobStatus.SUCCEEDED and job.attempts == 3 and product.status == S.SPECIFICATION
    runs = session.scalars(select(AgentRun).where(AgentRun.job_id == job.id)).all()
    assert [r.status for r in runs] == [JobStatus.FAILED, JobStatus.FAILED, JobStatus.SUCCEEDED]


def test_headless_failure_beyond_the_bound_sends_the_product_to_error(session, settings, make_product):
    handler = _flaky(99)
    ops = _ops_with(session, settings, Department.FINANCE, handler)
    product = advance_to(session, settings, make_product(), S.FINANCE_REVIEW)
    job = ops.run_job(ops.next_job(product))
    assert handler.calls["n"] == settings.max_job_attempts  # bounded: never retried forever
    assert job.status == JobStatus.FAILED and product.status == S.ERROR


@pytest.mark.parametrize("stop", ["limit", "refused"])
def test_hard_stop_requeues_without_using_an_attempt(session, settings, make_product, stop):
    from app.lifecycle import LimitReached
    from providers.claude_headless import HeadlessRefused

    exc = LimitReached("Daily Claude headless run limit reached") if stop == "limit" else HeadlessRefused("no")
    ops = _ops_with(session, settings, Department.FINANCE, _flaky(1, exc))
    product = advance_to(session, settings, make_product(), S.FINANCE_REVIEW)
    job = ops.next_job(product)
    with pytest.raises(type(exc)):
        ops.run_job(job)
    assert job.status == JobStatus.QUEUED and job.attempts == 0 and product.status == S.FINANCE_REVIEW
    assert session.scalar(select(AgentRun).where(AgentRun.job_id == job.id)).status == JobStatus.CANCELLED
    ops.run_job(job)  # next shift: the same job runs and succeeds
    assert job.status == JobStatus.SUCCEEDED and job.attempts == 1


def test_tick_can_be_limited_to_one_department(session, settings, make_product):
    ops = OperationsManager(session, settings, handlers=STUB_HANDLERS)
    idea = make_product()
    finance = advance_to(session, settings, make_product(), S.FINANCE_REVIEW)
    assert ops.tick(departments={Department.FINANCE}) == 1
    assert finance.status == S.SPECIFICATION and idea.status == S.IDEA


def test_new_product_build_limit_defers_first_builds_but_not_rebuilds(session, settings, make_product):
    from dataclasses import replace

    from app.models import Build

    limited = replace(settings, max_products_built_per_day=1, max_builds_per_day=10)
    ops = OperationsManager(session, limited, handlers=STUB_HANDLERS)
    first, second, rebuilt = (advance_to(session, limited, make_product(), S.PRODUCTION) for _ in range(3))
    old = Job(product_id=rebuilt.id, department=Department.PRODUCTION, job_type="build_product",
              for_status=S.PRODUCTION, status=JobStatus.SUCCEEDED)
    session.add(old)
    session.flush()
    session.add(Build(product_id=rebuilt.id, job_id=old.id, version=1, spec_slug="x", path="p", zip_path="z",
                      zip_sha256="0" * 64))
    session.flush()
    assert ops.next_job(first) is not None
    assert ops.next_job(second) is None  # deferred: one new product per day
    assert ops.next_job(rebuilt) is not None  # a rebuild is not a new product
    assert session.scalar(select(Event).where(Event.event_type == "job.deferred")).product_id == second.id
