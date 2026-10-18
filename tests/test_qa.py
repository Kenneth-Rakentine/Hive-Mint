"""QA department: deterministic checks first, then a (mocked) local-model clarity pass."""

import json
from dataclasses import replace

import httpx
import pytest
from sqlalchemy import select

from agents.departments import STUB_HANDLERS
from agents.operations import OperationsManager
from agents.qa import (
    QA, Finding, check_docs_mention_files, check_features, check_naming, check_spelling, deterministic_checks,
    score_report,
)
from app.lifecycle import resume
from app.models import AgentRun, Review, ReworkBrief
from app.states import Department, JobStatus, ProductStatus as S
from config.settings import Settings
from tests.conftest import advance_to, ollama_client, ollama_down, ollama_reply, record_build
from tools.render import RenderedChart, RenderResult

CLEAR = {"clarity_score": 90, "findings": [
    {"severity": "minor", "where": "Dashboard", "issue": "Say which cells are yellow", "suggestion": "Add a key"}]}


def _qa_ops(session, settings, client):
    return OperationsManager(session, settings, handlers={**STUB_HANDLERS, Department.QA: QA(settings, client)})


@pytest.fixture
def product_at_qa(session, settings, sample_product):
    return advance_to(session, settings, sample_product, S.QA)


# --------------------------------------------------------------------------- deterministic checks

def test_clean_build_has_no_deterministic_findings(sample_spec, good_build):
    assert deterministic_checks(sample_spec, good_build) == []


def test_misspellings_are_found_and_allowlisted_words_are_not():
    findings = check_spelling({"How to Use": "Recieve your totls by Venmo. See Mobile_Tracker.xlsx and IRS rules."},
                              {"venmo"})
    (finding,) = findings
    assert finding.check == "spelling" and finding.severity == "minor"
    assert "'recieve' (How to Use)" in finding.issue and "'totls'" in finding.issue
    assert "venmo" not in finding.issue and "'irs'" not in finding.issue and "tracker" not in finding.issue
    many = check_spelling({"x": "aaab bbbc cccd ddde eeef"}, set())
    assert many[0].severity == "major"


def test_inconsistent_naming_is_found():
    findings = check_naming(["How to Use", "dashboard"],
                            {"Jobs": ["Customer Name", "Paid?"], "Customers": ["Customer name", "Paid"]})
    by_severity = {f.severity: f.issue for f in findings}
    assert "dashboard" in by_severity["minor"] and "Customers: Customer name" in by_severity["minor"]
    assert "Customer Name vs Customer name" in by_severity["major"] and "Paid vs Paid?" in by_severity["major"]
    assert check_naming(["IRS Rates", "Mileage Log"], {"Mileage Log": ["IRS Rate", "Cost per Job"]}) == []


def test_missing_spec_feature_is_critical(sample_spec, good_build):
    spec = json.loads(json.dumps(sample_spec))
    spec["features"].append({"id": "invoices", "description": "Printable invoices",
                             "evidence": {"file": "Mobile_Detailer_Business_Tracker.xlsx", "sheet": "Invoice"}})
    spec["features"].append({"id": "more_charts", "description": "Lots of charts",
                             "evidence": {"file": "Mobile_Detailer_Business_Tracker.xlsx", "sheet": "Dashboard",
                                          "charts_min": 5}})
    findings = check_features(spec, good_build)
    assert [f.severity for f in findings] == ["critical", "critical"]
    assert "sheet 'Invoice' is missing" in findings[0].issue and "3 chart(s), needs 5" in findings[1].issue


def test_docs_must_name_every_file(sample_spec):
    findings = check_docs_mention_files(sample_spec, {
        "README.txt": "Open Mobile_Detailer_Business_Tracker.xlsx first.",
        "Instructions.pdf": "Files: Mobile_Detailer_Business_\nTracker.xlsx and README.txt",  # broken by a wrap
    })
    (finding,) = findings
    assert finding.where == "README.txt" and "Instructions.pdf" in finding.issue and finding.severity == "major"


def test_model_findings_alone_cannot_fail_a_product():
    model = [Finding("model", "clarity", "major", "x", "unclear") for _ in range(8)]
    report = score_report(model, pass_score=70, model_cap=20)
    assert report["model_deduction"] == 20 and report["score"] == 80 and report["decision"] == "PASS"
    vision = [Finding("vision", "chart_visual", "major", "chart", "looks off") for _ in range(15)]
    both = score_report(model + vision, pass_score=70, model_cap=20, vision_cap=10)
    assert (both["model_deduction"], both["vision_deduction"], both["score"], both["decision"]) == (20, 10, 70, "PASS")


def test_ai_caps_that_could_fail_a_product_alone_are_refused():
    with pytest.raises(ValueError, match="local model alone"):
        Settings(qa_pass_score=70, qa_model_max_deduction=25, qa_vision_max_deduction=10)
    blocking = score_report([Finding("deterministic", "docs_mention_files", "major", "README.txt", "x")],
                            pass_score=70, model_cap=20)
    assert blocking["score"] == 85 and blocking["decision"] == "REWORK"


# --------------------------------------------------------------------------- the department

def test_qa_pass_records_report_and_moves_to_red_team(session, settings, product_at_qa, sample_spec, good_build):
    build = record_build(session, product_at_qa, good_build, sample_spec)
    prompts = []

    def handler(request):
        prompts.append(json.loads(request.content)["messages"][-1]["content"])
        return ollama_reply(CLEAR, prompt_tokens=2900, completion_tokens=300)

    ops = _qa_ops(session, settings, ollama_client(handler))
    job = ops.next_job(product_at_qa)
    ops.run_job(job)

    assert product_at_qa.status == S.RED_TEAM, product_at_qa.status_reason
    review = session.scalar(select(Review))
    report = json.loads(review.report_json)
    assert (review.department, review.decision, review.build_id, review.job_id) == \
        (Department.QA, "PASS", build.id, job.id)
    assert review.score == report["score"] == 95 and report["clarity_score"] == 90
    assert report["findings"][0]["source"] == "model"
    assert "Sheet: Dashboard" in prompts[0] and "Columns: Date | Customer Name" in prompts[0]
    assert "PK" not in prompts[0][:200]  # a text digest, not the binary file
    runs = session.scalars(select(AgentRun).where(AgentRun.job_id == job.id)).all()
    assert {(r.provider, r.tier) for r in runs} == {("qa", 0), ("ollama", 1)}
    ollama_run = next(r for r in runs if r.provider == "ollama")
    assert (ollama_run.model, ollama_run.prompt_tokens, ollama_run.cost_usd_micros) == ("qwen3.5:4b", 2900, 0)


def test_qa_rework_creates_a_brief(session, settings, product_at_qa, sample_spec, build_copy):
    (build_copy / "README.txt").write_text("Thanks! Open the workbook.", encoding="utf-8")
    record_build(session, product_at_qa, build_copy, sample_spec)

    ops = _qa_ops(session, settings, ollama_client(lambda request: ollama_reply(CLEAR)))
    ops.run_job(ops.next_job(product_at_qa))

    assert product_at_qa.status == S.REWORK and "QA REWORK" in product_at_qa.status_reason
    brief = session.scalar(select(ReworkBrief))
    assert brief.source == "QA" and brief.status == ReworkBrief.OPEN
    assert any(i.startswith("qa-docs-mention-files") for i in brief.change_ids)

    ops.run_job(ops.next_job(product_at_qa))  # Product Design: the spec does not resolve QA's ids yet
    assert product_at_qa.status == S.PAUSED and "qa-docs-mention-files" in product_at_qa.status_reason


def test_ollama_down_gives_bounded_retries_then_error(session, settings, product_at_qa, sample_spec, good_build):
    record_build(session, product_at_qa, good_build, sample_spec)
    session.commit()
    ops = _qa_ops(session, settings, ollama_down())

    ticks = 0
    while ops.tick():
        ticks += 1
        assert ticks < 5, "tick loop must terminate"

    (job,) = [j for j in product_at_qa.jobs if j.department == Department.QA]
    assert job.status == JobStatus.FAILED and job.attempts == settings.max_job_attempts == 3
    assert "OLLAMA_UNAVAILABLE" in job.error
    assert product_at_qa.status == S.ERROR and product_at_qa.paused_from == S.QA
    assert "OLLAMA_UNAVAILABLE" in product_at_qa.status_reason
    ollama_runs = session.scalars(select(AgentRun).where(AgentRun.provider == "ollama")).all()
    assert len(ollama_runs) == 3 and all(r.status == JobStatus.FAILED for r in ollama_runs)
    assert session.scalar(select(Review)) is None

    resume(session, settings, product_at_qa, actor="owner")  # Ollama is back
    assert product_at_qa.status == S.QA
    ops.handlers[Department.QA] = QA(settings, ollama_client(lambda request: ollama_reply(CLEAR)))
    ops.tick()
    assert product_at_qa.status == S.RED_TEAM


# --------------------------------------------------------------------------- visual QA

VISION_BAD = {"looks_ok": False, "findings": [
    {"severity": "major", "problem": "overlapping_text", "issue": "Title sits on the gridlines",
     "suggestion": "Move it"},
    {"severity": "major", "problem": "duplicate_axis_labels", "issue": "$0 $0 $1 $1 on the axis"}]}


def _fake_renderer(engine="excel"):
    """Stands in for Excel: writes one PNG per spec'd chart and reports them, as tools.render does."""
    calls = []

    def render(workbooks, out_dir):
        calls.append((workbooks, out_dir))
        out_dir.mkdir(parents=True, exist_ok=True)
        result = RenderResult(engine=engine, out_dir=str(out_dir))
        for index, title in enumerate(["Income vs Costs by Month", "Profit After Mileage by Month",
                                       "Costs by Category"], start=1):
            png = out_dir / f"chart{index}.png"
            png.write_bytes(b"\x89PNG fake " + title.encode())
            result.charts.append(RenderedChart(workbooks[0].name, "Dashboard", index, title, str(png)))
        return result

    render.calls = calls
    return render


def test_vision_pass_sends_every_chart_image_and_is_capped(session, settings, product_at_qa, sample_spec,
                                                           good_build):
    build = record_build(session, product_at_qa, good_build, sample_spec)
    seen = []

    def handler(request):
        message = json.loads(request.content)["messages"][-1]
        seen.append(message)
        return ollama_reply(VISION_BAD if message.get("images") else CLEAR, prompt_tokens=700)

    renderer = _fake_renderer()
    qa = QA(settings, ollama_client(handler), renderer=renderer)
    ops = OperationsManager(session, settings, handlers={**STUB_HANDLERS, Department.QA: qa})
    job = ops.next_job(product_at_qa)
    ops.run_job(job)

    (workbooks, out_dir), = renderer.calls
    assert [p.name for p in workbooks] == ["Mobile_Detailer_Business_Tracker.xlsx"]
    assert out_dir == good_build / "renders"
    images = [m for m in seen if m.get("images")]
    assert len(images) == 3 and all(len(m["images"]) == 1 for m in images)
    assert '"Costs by Category" on the Dashboard tab of a blank template' in images[2]["content"]
    assert "NOT a problem" in images[0]["content"]  # empty charts are expected in a template

    review = session.scalar(select(Review))
    report = json.loads(review.report_json)
    vision = [f for f in report["findings"] if f["source"] == "vision"]
    assert len(vision) == 6 and vision[0]["issue"] == "[overlapping_text] Title sits on the gridlines"
    assert "chart1.png" in vision[0]["where"]
    # 6 major vision findings = 90 points, capped at 10; text 1 minor = 5: still a PASS, the model can't fail it.
    assert (report["vision_deduction"], report["model_deduction"], report["score"]) == (10, 5, 85)
    assert product_at_qa.status == S.RED_TEAM and "3 chart(s) rendered by excel" in review.summary
    assert report["render"]["engine"] == "excel" and len(report["render"]["charts"]) == 3
    runs = session.scalars(select(AgentRun).where(AgentRun.job_id == job.id, AgentRun.provider == "ollama")).all()
    assert len(runs) == 4 and all(r.cost_usd_micros == 0 and r.tier == 1 for r in runs)  # 1 text + 3 charts
    assert build.version == report["build_version"]


def test_skipped_render_is_reported_and_skips_the_vision_pass(session, settings, product_at_qa, sample_spec,
                                                               good_build):
    record_build(session, product_at_qa, good_build, sample_spec)
    calls = []

    def handler(request):
        calls.append(json.loads(request.content)["messages"][-1])
        return ollama_reply(CLEAR)

    ops = _qa_ops(session, settings, ollama_client(handler))  # settings.render_engine == "none"
    ops.run_job(ops.next_job(product_at_qa))

    assert len(calls) == 1 and "images" not in calls[0]
    review = session.scalar(select(Review))
    report = json.loads(review.report_json)
    (skip,) = [f for f in report["findings"] if f["source"] == "render"]
    assert skip["severity"] == "info" and "Visual render skipped" in skip["issue"]
    assert "visual render skipped" in review.summary and product_at_qa.status == S.RED_TEAM


def test_ollama_down_during_the_vision_pass_retries_then_errors(session, settings, product_at_qa, sample_spec,
                                                               good_build):
    record_build(session, product_at_qa, good_build, sample_spec)

    def handler(request):
        if json.loads(request.content)["messages"][-1].get("images"):
            raise httpx.ConnectError("connection refused", request=request)
        return ollama_reply(CLEAR)

    qa = QA(settings, ollama_client(handler), renderer=_fake_renderer())
    ops = OperationsManager(session, settings, handlers={**STUB_HANDLERS, Department.QA: qa})
    job = ops.next_job(product_at_qa)
    ops.run_job(job)
    assert job.status == JobStatus.FAILED and job.attempts == 3 and "OLLAMA_UNAVAILABLE" in job.error
    assert product_at_qa.status == S.ERROR and session.scalar(select(Review)) is None


def test_qa_without_a_build_goes_to_rework(session, settings, product_at_qa):
    result = QA(replace(settings), ollama_down())(product_at_qa, None)
    assert result.next_status == S.REWORK and "no build" in result.summary
