"""Red Team: a Claude Code session's verdict is validated, recorded and routed."""

import json

import pytest
from sqlalchemy import select

from agents import red_team
from agents.departments import STUB_HANDLERS
from agents.operations import OperationsManager
from agents.production import Production
from agents.red_team import RedTeamVerdict, VerdictError, export_packet, import_verdict
from agents.rework import BriefFile
from app.models import AgentRun, Build, Job, Review, ReworkBrief
from app.states import OPEN_JOB_STATUSES, Department, JobStatus, ProductStatus as S
from config.settings import ROOT
from tests.conftest import advance_to, record_build
from tools.spec import load_spec

SEED = ROOT / "config" / "rework" / "mobile-auto-detailer-income-expense-tracker-001.json"


def _verdict(product, decision="PASS", version=1, **extra):
    data = {"product_id": product.id, "build_version": version, "decision": decision,
            "summary": "Fair case against publishing, weighed.", "model": "claude-opus-5-5",
            "findings": [{"severity": "minor", "category": "free_alternatives", "issue": "Free templates exist"}]}
    if decision == "REWORK":
        data["required_changes"] = [{"id": "merge_workbooks", "change": "One workbook with tabs",
                                     "reason": "Double entry across files", "acceptance": "One .xlsx"}]
    if decision == "REJECT":
        data["reject_reason"] = "No viable market"
    return RedTeamVerdict.model_validate(data | extra)


@pytest.fixture
def at_red_team(session, settings, sample_product, sample_spec, good_build):
    """The sample product waiting for its Red Team review, with its job queued by Operations."""
    advance_to(session, settings, sample_product, S.RED_TEAM)
    record_build(session, sample_product, good_build, sample_spec)
    ops = OperationsManager(session, settings)
    assert ops.tick() == 0  # the Red Team job is queued but never run automatically
    job = ops.open_job(sample_product)
    assert job.department == Department.RED_TEAM and job.status == JobStatus.QUEUED
    return sample_product


@pytest.mark.parametrize("change, message", [
    ({"decision": "REWORK"}, "required change"),
    ({"decision": "REJECT"}, "reject_reason"),
    ({"decision": "PASS", "required_changes": [{"id": "x_y", "change": "Do it", "reason": "Because"}]},
     "must not list"),
    ({"findings": [{"severity": "major", "category": "vibes", "issue": "Feels off"}]}, "category"),
    ({"summary": "short"}, "summary"),
])
def test_invalid_verdicts_are_refused(change, message):
    data = {"product_id": 1, "build_version": 1, "decision": "PASS", "summary": "A long enough summary.",
            "findings": []} | change
    with pytest.raises(ValueError, match=message):
        RedTeamVerdict.model_validate(data)


def test_pass_moves_on_to_compliance_and_logs_a_tier2_run(session, settings, at_red_team, tmp_path):
    source = tmp_path / "verdict.json"
    verdict = _verdict(at_red_team)
    source.write_text(verdict.model_dump_json())
    review = import_verdict(session, settings, at_red_team, verdict, source=source, base=tmp_path / "reviews")

    assert at_red_team.status == S.COMPLIANCE
    assert (review.department, review.decision, review.model) == (Department.RED_TEAM, "PASS", "claude-opus-5-5")
    job = session.get(Job, review.job_id)
    assert job.status == JobStatus.SUCCEEDED and json.loads(job.output_json)["review_id"] == review.id
    (run,) = session.scalars(select(AgentRun).where(AgentRun.job_id == job.id)).all()
    assert (run.provider, run.model, run.tier, run.cost_usd_micros, run.status) == \
        ("claude-code", "claude-opus-5-5", 2, 0, JobStatus.SUCCEEDED)
    assert (tmp_path / "reviews" / str(at_red_team.id) / "verdict-v1-1.json").is_file()


def test_rework_routes_back_to_production_with_the_brief(session, settings, at_red_team):
    import_verdict(session, settings, at_red_team, _verdict(at_red_team, "REWORK"))
    assert at_red_team.status == S.REWORK and at_red_team.rework_count == 1
    brief = session.scalar(select(ReworkBrief))
    assert (brief.source, brief.change_ids, brief.status) == ("RED_TEAM", ["merge_workbooks"], ReworkBrief.OPEN)
    assert brief.review_id == session.scalar(select(Review)).id

    ops = OperationsManager(session, settings, handlers={**STUB_HANDLERS, Department.PRODUCTION: Production(settings)})
    ops.run_job(ops.next_job(at_red_team))  # Product Design: spec v2 resolves merge_workbooks
    assert at_red_team.status == S.PRODUCTION

    job = ops.next_job(at_red_team)
    assert job.job_type == "rework_build"
    assert json.loads(job.input_json)["rework_brief"]["required_changes"][0]["id"] == "merge_workbooks"
    ops.run_job(job)
    assert at_red_team.status == S.TESTING
    build = session.scalar(select(Build).order_by(Build.version.desc()).limit(1))
    assert build.version == 2 and build.job_id == job.id
    assert brief.status == ReworkBrief.ADDRESSED and brief.addressed_build_id == build.id
    assert "rework brief" in at_red_team.status_reason


def test_reject_ends_the_product(session, settings, at_red_team):
    import_verdict(session, settings, at_red_team, _verdict(at_red_team, "REJECT"))
    assert at_red_team.status == S.REJECTED and "No viable market" in at_red_team.status_reason
    ops = OperationsManager(session, settings)
    assert ops.next_job(at_red_team) is None and ops.tick() == 0
    open_jobs = session.scalars(select(Job).where(Job.status.in_(OPEN_JOB_STATUSES))).all()
    assert open_jobs == []


def test_stale_wrong_product_or_wrong_state_verdicts_are_refused(session, settings, at_red_team, make_product):
    with pytest.raises(VerdictError, match="latest build is v1"):
        import_verdict(session, settings, at_red_team, _verdict(at_red_team, version=2))
    with pytest.raises(VerdictError, match="not 1|is for product"):
        import_verdict(session, settings, at_red_team, _verdict(at_red_team, product_id=999))
    other = make_product()
    with pytest.raises(VerdictError, match="not awaiting Red Team"):
        import_verdict(session, settings, other, _verdict(other))
    assert at_red_team.status == S.RED_TEAM and session.scalar(select(Review)) is None


def test_red_team_job_is_never_run_by_tick(session, settings, at_red_team):
    ops = OperationsManager(session, settings)
    for _ in range(3):
        assert ops.tick() == 0
    assert ops.open_job(at_red_team).attempts == 0


def test_export_packet_has_everything_the_reviewer_needs(session, settings, at_red_team, tmp_path):
    packet = export_packet(session, at_red_team, tmp_path)
    text = packet.read_text(encoding="utf-8")
    assert packet == tmp_path / str(at_red_team.id) / "packet.md"
    for section in ("## Your task", "Free alternatives", "Double data entry", "Refund risk", "## Verdict format",
                    f"review-import {at_red_team.id}", "## Test results", "## QA report", "## Rework history",
                    "## Product digest", "### Sheet: Dashboard", "## Specification", '"resolves"'):
        assert section in text, section
    assert '"build_version": 1' in text


def test_seed_brief_is_valid_and_resolved_by_spec_v2():
    brief = BriefFile.model_validate(json.loads(SEED.read_text(encoding="utf-8")))
    ids = [c.id for c in brief.required_changes]
    assert ids == ["merge_workbooks", "dashboard_charts", "profit_after_mileage", "tax_set_aside"]
    assert set(ids) <= set(load_spec("mobile-auto-detailer-income-expense-tracker")["resolves"])


def test_reviews_folder_is_gitignored():
    assert "research/reviews/" in (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert red_team.REVIEWS_DIR == ROOT / "research" / "reviews"
