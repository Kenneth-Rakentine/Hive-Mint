from dataclasses import replace
import json
from datetime import UTC, datetime, timedelta

from app.lifecycle import transition
from app.models import Opportunity, ResearchRecord, Shift
from app.report import REPORT_NAME, build_report, desktop_dir, write_report
from app.states import ProductStatus as S
from tests.conftest import advance_to

NOW = datetime(2026, 10, 18, 23, 0, tzinfo=UTC)


def _shift(session, days_ago, stop="done: no work left", steps=None):
    start = NOW - timedelta(days=days_ago, hours=4)
    shift = Shift(started_at=start, ended_at=start + timedelta(hours=3), stop_reason=stop,
                  steps_json=json.dumps(steps or [{"step": "research", "ran": 1, "summary": "found 2 ideas"}]),
                  claude_runs=3, summary_json=json.dumps({"claude_runs_today": 3, "claude_runs_cap": 15,
                                                          "disk": {"free_gb": 120.5, "min_free_disk_gb": 50,
                                                                   "hivemint_folder_gb": 0.4,
                                                                   "max_hivemint_storage_gb": 20,
                                                                   "ollama_models_gb": 3.4}}))
    session.add(shift)
    session.flush()
    return shift


def test_newest_shift_first_and_only_the_last_14_days(session, settings):
    old = _shift(session, 20, stop="old shift")
    week = _shift(session, 7, stop="last week")
    today = _shift(session, 0, stop="stopped: the shift window ended (22:00) before qa")
    text = build_report(session, settings, NOW)
    assert text.index(f"SHIFT #{today.id}") < text.index(f"SHIFT #{week.id}")
    assert f"SHIFT #{old.id}" not in text and "old shift" not in text
    assert "found 2 ideas" in text and "Claude headless runs: 3 this shift; 3 of 15 used today" in text
    assert "Disk: 120.5 GB free" in text


def test_right_now_lists_approvals_paused_products_and_errors(session, settings, make_product):
    settings = replace(settings, auto_rework_grants=0, redesigns_per_product=0, auto_drop_failed=False)
    waiting = advance_to(session, settings, make_product("Ready"), S.HUMAN_REVIEW)
    paused = advance_to(session, settings, make_product("Stuck"), S.QA)
    for _ in range(settings.max_rework_cycles + 1):
        transition(session, settings, paused, S.REWORK, actor="qa", reason="fail")
        if paused.status == S.REWORK:
            transition(session, settings, paused, S.PRODUCTION, actor="design", reason="again")
            advance_to(session, settings, paused, S.QA)
    assert paused.status == S.PAUSED
    _shift(session, 0)
    text = build_report(session, settings, NOW)
    approvals = text[text.index("Needs you (HUMAN_REVIEW)"):text.index("Paused products")]
    assert f"#{waiting.id} {waiting.name}" in approvals
    assert f"python -m app.cli approve-rework {paused.id}" in text
    assert "Products in ERROR" in text


def test_ideas_and_scores_appear_under_the_shift_that_scored_them(session, settings):
    shift = _shift(session, 0)
    record = ResearchRecord(niche="n", summary="s", confidence="high", sources_json="[]")
    session.add(record)
    session.flush()
    session.add(Opportunity(research_id=record.id, title="Night Audit Workbook", product_type="spreadsheet",
                            score=78.5, rank=1, decision="SELECTED", scored_at=shift.started_at + timedelta(hours=1),
                            reasons_json=json.dumps(["selected: score 78.5 (rank 1)"])))
    session.flush()
    text = build_report(session, settings, NOW)
    assert " 78.5  SELECTED        Night Audit Workbook" in text and "selected: score 78.5" in text


def test_write_report_replaces_the_file(session, settings, tmp_path):
    _shift(session, 0)
    path = tmp_path / REPORT_NAME
    path.write_text("stale", encoding="utf-8")
    write_report(session, settings, path, NOW)
    content = path.read_bytes()
    assert content.startswith(b"HIVE MINT DAILY REPORT") and b"\r\n" in content and b"stale" not in content


def test_desktop_dir_is_a_real_folder():
    assert desktop_dir().is_dir()


def test_later_rounds_show_only_steps_that_did_work(session, settings):
    steps = [{"step": n, "ran": 0, "summary": f"{n} idle"} for n in
             ("research", "finance", "spec", "production", "testing", "qa", "red_team", "rework", "compliance")]
    steps += [{"step": "production", "ran": 1, "summary": "1 build(s)"}, {"step": "qa", "ran": 0, "summary": "qa again"}]
    _shift(session, 0, steps=steps)
    text = build_report(session, settings, NOW)
    assert "  QA           qa idle" in text and "1 build(s)" in text and "qa again" not in text
