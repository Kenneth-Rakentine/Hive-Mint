"""The shift orchestrator, with every Claude call and the clock and battery mocked."""

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from agents import red_team
from agents.departments import STUB_HANDLERS, StageResult
from agents.finance import finance_gate
from agents.operations import OperationsManager
from agents.research import research_gate
from app.lifecycle import LimitReached
from app.models import Build, Opportunity, Product, ResearchRecord, Shift, SpecVersion
from app.shift import STEPS, ShiftRunner, in_window
from app.states import Department, Outcome, ProductStatus as S
from providers.claude_headless import HeadlessRefused
from tests.conftest import advance_to

# 2026-10-18 is EDT (UTC-4): 17:00 UTC = 1 PM local, 02:00 UTC next day = 10 PM local.
IN_WINDOW = datetime(2026, 10, 18, 17, 30, tzinfo=UTC)  # 1:30 PM local
AFTER_WINDOW = datetime(2026, 10, 19, 2, 30, tzinfo=UTC)  # 10:30 PM local
MORNING = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)  # 8 AM local


class Clock:
    def __init__(self, at):
        self.at = at

    def __call__(self):
        return self.at


def strong_factors():
    scores = {"demand": 5, "competition": 1, "margin": 5, "ease": 5, "differentiation": 5, "support_burden": 1,
              "automation_suitability": 5}
    return json.dumps({k: {"score": v, "reason": "r", "sources": [0]} for k, v in scores.items()})


def fake_researcher(calls, *, titles=("Motel Night Audit Workbook",), then=None):
    def research(session, settings, niche):
        calls.append(("research", niche))
        record = ResearchRecord(niche=niche, summary="s", confidence="high",
                                sources_json=json.dumps([{"url": f"https://e.com/{i}"} for i in range(3)]))
        session.add(record)
        session.flush()
        for title in titles:
            session.add(Opportunity(research_id=record.id, title=title, product_type="spreadsheet", description="d",
                                    data_json=json.dumps({"price_low": 20, "price_high": 30}),
                                    factors_json=strong_factors()))
        session.flush()
        if then:
            then()
        return record

    return research


def stub_reviewer(calls, decision="PASS"):
    """Moves a product past RED_TEAM the way an imported verdict would, without a real build."""
    def review(session, settings, product):
        calls.append(("red_team", product.id))
        ops = OperationsManager(session, settings, handlers=STUB_HANDLERS)
        job = red_team.open_red_team_job(session, product) or ops.next_job(product)
        target = {"PASS": S.COMPLIANCE, "REWORK": S.REWORK}[decision]
        ops.complete_external_job(job, StageResult(Outcome.PASS, target, f"stub {decision}"), provider="test",
                                  model="test", tier=2)
        return type("Review", (), {"decision": decision})()

    return review


def recording_handlers(settings, calls):
    def wrap(department, handler):
        def inner(product, job):
            calls.append((department.value.lower(), product.id))
            return handler(product, job)

        inner.provider = "stub"
        return inner

    handlers = {**STUB_HANDLERS, Department.RESEARCH: research_gate, Department.FINANCE: finance_gate(settings)}
    return {d: wrap(d, h) for d, h in handlers.items()}


def runner(session, settings, calls, **kw):
    kw.setdefault("now", Clock(IN_WINDOW))
    kw.setdefault("battery", lambda: False)
    kw.setdefault("subscription_check", lambda s: None)
    kw.setdefault("researcher", fake_researcher(calls))
    kw.setdefault("reviewer", stub_reviewer(calls))
    ops = OperationsManager(session, settings, handlers=recording_handlers(settings, calls))
    return ShiftRunner(session, settings, ops=ops, **kw)


def test_window_is_local_time():
    from config.settings import Settings

    s = Settings()
    assert in_window(s, IN_WINDOW) and not in_window(s, AFTER_WINDOW) and not in_window(s, MORNING)
    overnight = replace(s, shift_start="22:00", shift_end="06:00")
    assert in_window(overnight, AFTER_WINDOW) and not in_window(overnight, IN_WINDOW)


def test_steps_run_in_order_and_an_idea_reaches_human_review(session, settings):
    calls = []
    shift = runner(session, settings, calls).run()
    steps = [s["step"] for s in json.loads(shift.steps_json)]
    assert steps[:len(STEPS)] == list(STEPS)  # first pass in the fixed order, then production..compliance repeats
    assert set(steps[len(STEPS):]) <= set(STEPS[3:])
    product = session.scalar(select(Product))
    assert product.status == S.HUMAN_REVIEW and product.name == "Motel Night Audit Workbook"
    order = [c[0] for c in calls]
    first = {name: order.index(name) for name in ("research", "operations", "finance", "product_design",
                                                  "production", "testing", "qa", "red_team", "compliance", "seo")}
    assert list(first) == sorted(first, key=first.get)  # each department first acted in pipeline order
    assert shift.stop_reason.startswith("done") and shift.ended_at is not None
    assert json.loads(shift.summary_json)["claude_runs_cap"] == settings.max_claude_runs_per_day


def test_battery_skips_the_shift(session, settings):
    calls = []
    shift = runner(session, settings, calls, battery=lambda: True).run()
    assert shift.stop_reason == "skipped: on battery" and not calls and json.loads(shift.steps_json) == []
    assert runner(session, replace(settings, skip_on_battery=False), [], battery=lambda: True).run() \
        .stop_reason.startswith("done")


def test_outside_the_window_nothing_runs_unless_ignored(session, settings):
    calls = []
    shift = runner(session, settings, calls, now=Clock(MORNING)).run()
    assert shift.stop_reason.startswith("skipped: outside the shift window") and not calls
    shift = runner(session, settings, calls, now=Clock(MORNING), ignore_window=True).run()
    assert shift.stop_reason.startswith("done") and calls


def test_window_ending_mid_run_finishes_the_step_then_stops_and_the_next_shift_resumes(session, settings):
    calls, clock = [], Clock(IN_WINDOW)

    def late():
        clock.at = AFTER_WINDOW  # research ran past 10 PM

    shift = runner(session, settings, calls, now=clock, researcher=fake_researcher(calls, then=late)).run()
    assert [s["step"] for s in json.loads(shift.steps_json)] == ["research"]
    assert "window ended" in shift.stop_reason and "before finance" in shift.stop_reason
    assert session.scalar(select(ResearchRecord)) is not None  # the running step completed and was kept
    assert session.scalar(select(Product)) is None  # finance never started

    clock.at = IN_WINDOW.replace(day=19)  # next afternoon
    shift = runner(session, settings, calls, now=clock).run()
    assert session.scalar(select(Product)).status == S.HUMAN_REVIEW
    assert ("research", calls[0][1]) not in calls[1:]  # the researched niche was not researched again


def test_claude_run_limit_stops_the_shift_at_once(session, settings):
    calls = []

    def capped(session, settings, niche):
        raise LimitReached("Daily Claude headless run limit reached (15/15)")

    shift = runner(session, settings, calls, researcher=capped).run()
    assert shift.stop_reason == "stopped at research: Daily Claude headless run limit reached (15/15)"
    assert [s["step"] for s in json.loads(shift.steps_json)] == ["research"]
    assert not calls


def test_refused_billing_guard_stops_before_any_step(session, settings):
    def refuse(s):
        raise HeadlessRefused("HEADLESS_REFUSED: ANTHROPIC_API_KEY is set")

    shift = runner(session, settings, [], subscription_check=refuse).run()
    assert "ANTHROPIC_API_KEY" in shift.stop_reason and json.loads(shift.steps_json) == []
    assert "ANTHROPIC_API_KEY" in json.loads(shift.errors_json)[0]


def test_new_product_limit_specs_only_what_can_be_built_today(session, settings):
    calls = []
    limited = replace(settings, max_products_built_per_day=1)
    two = fake_researcher(calls, titles=("Alpha Tracker", "Beta Tracker"))
    shift = runner(session, limited, calls, researcher=two).run()
    statuses = sorted(p.status for p in session.scalars(select(Product)))
    assert statuses == sorted([S.HUMAN_REVIEW, S.SPECIFICATION])
    spec_step = next(s for s in json.loads(shift.steps_json) if s["step"] == "spec")
    assert "1 wait for build room" in spec_step["summary"]


def test_idea_limit_is_a_hard_stop(session, settings):
    calls = []
    shift = runner(session, replace(settings, max_ideas_per_day=0), calls).run()
    assert shift.stop_reason.startswith("stopped at finance: Daily idea limit reached")


def test_every_step_is_recorded_with_its_summary(session, settings):
    shift = runner(session, settings, []).run()
    research = json.loads(shift.steps_json)[0]
    assert research["step"] == "research" and "1 opportunities" in research["summary"]
    assert research["started"] <= research["finished"]
    assert session.scalars(select(Shift)).all() == [shift]


# --------------------------------------------------------------------------- automated rework, end to end

def test_red_team_rework_is_implemented_rebuilt_and_reviewed_again(session, settings, sample_product, fake_claude):
    """Red Team asks for two changes; the shift revises the spec (headless, mocked), Production rebuilds,
    Testing and QA run again, and the second Red Team review passes. Testing/QA are stubs here; the real
    ones have their own tests."""
    from agents.compliance import compliance_gate
    from agents.production import Production
    from agents.spec_writer import ProductDesign

    product = advance_to(session, settings, sample_product, S.PRODUCTION)
    handlers = {**STUB_HANDLERS, Department.PRODUCT_DESIGN: ProductDesign(settings),
                Department.PRODUCTION: Production(settings), Department.COMPLIANCE: compliance_gate(settings)}
    ops = OperationsManager(session, settings, handlers=handlers)
    verdicts = ["REWORK", "PASS"]

    def review(session, settings, product):
        build = session.scalar(select(Build).where(Build.product_id == product.id).order_by(Build.version.desc()))
        job = red_team.open_red_team_job(session, product) or ops.next_job(product)
        decision = verdicts.pop(0)
        changes = [{"id": "mileage_double_count_guard", "change": "Warn on fuel + mileage", "reason": "double count"},
                   {"id": "add_example_workbook", "change": "Ship an example", "reason": "buyers want one"}]
        verdict = red_team.RedTeamVerdict(product_id=product.id, build_version=build.version, decision=decision,
                                          summary="Strongest fair case here.",
                                          required_changes=changes if decision == "REWORK" else [])
        assert job.status.value == "QUEUED"
        return red_team.import_verdict(session, settings, product, verdict)

    def revise(cmd, prompt):
        draft = settings.products_dir / "drafts" / f"product-{product.id}" / "spec.json"
        spec = json.loads(draft.read_text(encoding="utf-8"))
        spec["resolves"] += ["mileage_double_count_guard", "add_example_workbook"]
        spec["changelog"].append({"spec_version": 5, "summary": "Guard and example workbook"})
        draft.write_text(json.dumps(spec), encoding="utf-8")
        return {"structured_output": {"spec_path": "x", "summary": "Implemented both changes.",
                                      "spec_check_passed": True}}

    fake_claude.replies = [revise]
    result = ShiftRunner(session, settings, ops=ops, now=Clock(IN_WINDOW), battery=lambda: False,
                         subscription_check=lambda s: None, reviewer=review,
                         researcher=fake_researcher([], titles=())).run()  # no new ideas: this shift is the rework

    assert product.status == S.HUMAN_REVIEW, result.stop_reason
    versions = session.scalars(select(SpecVersion).where(SpecVersion.product_id == product.id)
                               .order_by(SpecVersion.version)).all()
    assert [v.version for v in versions] == [4, 5] and "mileage_double_count_guard" in versions[1].diff_text
    builds = session.scalars(select(Build).where(Build.product_id == product.id).order_by(Build.version)).all()
    assert [b.version for b in builds] == [1, 2]  # rebuilt from spec v5
    steps = [s["step"] for s in json.loads(result.steps_json)]
    assert steps.count("red_team") >= 2 and steps.count("production") >= 2
    assert product.rework_count == 1 and len(fake_claude.calls) == 1


def test_jobs_interrupted_by_a_killed_shift_are_requeued(session, settings, make_product):
    from app.models import Job
    from app.states import JobStatus

    product = advance_to(session, settings, make_product(), S.QA)
    job = Job(product_id=product.id, department=Department.QA, job_type="quality_review", for_status=S.QA,
              status=JobStatus.RUNNING, attempts=1)
    orphan = Job(product_id=None, department=Department.RESEARCH, job_type="niche_research", for_status=S.RESEARCH,
                 status=JobStatus.RUNNING, attempts=1)
    session.add_all([job, orphan])
    session.commit()
    calls = []
    runner(session, settings, calls, researcher=fake_researcher(calls, titles=())).run()
    assert orphan.status == JobStatus.CANCELLED
    assert product.status == S.HUMAN_REVIEW  # the requeued QA job ran, and the product went on
