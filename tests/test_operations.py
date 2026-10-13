import json

import pytest
from sqlalchemy import select

from agents.departments import STUB_HANDLERS, StageResult
from agents.operations import ROUTING, OperationsManager
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


def test_every_routed_department_has_a_handler():
    assert {d for d in ROUTING.values() if d} <= set(STUB_HANDLERS)


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
