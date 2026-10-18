"""The daily report: "Hive Mint Daily Report.txt" on the owner's Desktop, rebuilt from the database.

Written after every shift (and by python -m app.cli report). The whole file is regenerated each
time: a "right now" block (what waits for the owner's approval, paused products and the command to
continue them, products in ERROR), then every shift from the last REPORT_DAYS days, newest first,
with what each team did, ideas and scores, builds, Testing / Visual QA / QA / Red Team results,
reworks and spec changes, errors, disk usage and Claude runs used. Plain Python writes it; headless
Claude runs never touch the Desktop.
"""

from __future__ import annotations

import ctypes
import json
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.lifecycle import rework_limit
from app.models import AgentRun, Build, Event, Opportunity, Product, Review, ReworkBrief, Shift, SpecVersion, TestResult
from app.states import Department, JobStatus, ProductStatus
from config.settings import Settings
from providers.claude_headless import headless_runs_today
from tools.disk import disk_report

S = ProductStatus
REPORT_NAME = "Hive Mint Daily Report.txt"
REPORT_DAYS = 14
FOLDERID_DESKTOP = "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"
RULE = "=" * 78
STEP_LABELS = {"research": "Research", "finance": "Finance", "spec": "Spec", "production": "Production",
               "testing": "Testing", "qa": "QA", "red_team": "Red Team", "rework": "Rework",
               "compliance": "Compliance"}


def desktop_dir() -> Path:
    """The real Desktop folder, as [Environment]::GetFolderPath('Desktop') reports it (it may be in OneDrive)."""
    if os.name == "nt":
        class GUID(ctypes.Structure):
            _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort),
                        ("Data4", ctypes.c_ubyte * 8)]

        raw = uuid.UUID(FOLDERID_DESKTOP).bytes_le
        guid = GUID.from_buffer_copy(raw)
        out = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out)) == 0:
            try:
                return Path(out.value)
            finally:
                ctypes.windll.ole32.CoTaskMemFree(out)
    return Path.home() / "Desktop"


def report_path() -> Path:
    return desktop_dir() / REPORT_NAME


def write_report(session: Session, settings: Settings, path: Path | None = None, now: datetime | None = None) -> Path:
    path = path or report_path()
    text = build_report(session, settings, now or utcnow())
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\r\n")
    os.replace(tmp, path)
    return path


# --------------------------------------------------------------------------- content

def build_report(session: Session, settings: Settings, now: datetime) -> str:
    zone = ZoneInfo(settings.timezone)
    local = lambda dt: dt.astimezone(zone).strftime("%a %b %d %Y, %I:%M %p").replace(" 0", " ")  # noqa: E731
    lines = ["HIVE MINT DAILY REPORT", f"Updated {local(now)} ({settings.timezone}). Newest shift first; "
             f"the last {REPORT_DAYS} days are kept. Nothing is published without your approval.", ""]
    lines += _right_now(session, settings, now)
    shifts = session.scalars(select(Shift).where(Shift.started_at >= now - timedelta(days=REPORT_DAYS))
                             .order_by(Shift.started_at.desc(), Shift.id.desc())).all()
    if not shifts:
        lines += [RULE, "No shifts in the last 14 days.", ""]
    for shift in shifts:
        lines += _shift_section(session, settings, shift, local)
    return "\n".join(lines).rstrip() + "\n"


def _right_now(session: Session, settings: Settings, now: datetime) -> list[str]:
    def products(status):
        return session.scalars(select(Product).where(Product.status == status).order_by(Product.id)).all()

    lines = [RULE, "RIGHT NOW", RULE, "", "Waiting for your approval (HUMAN_REVIEW):"]
    waiting = products(S.HUMAN_REVIEW)
    from app.review import latest_listing

    for p in waiting:
        lines.append(f"  #{p.id} {p.name}: {p.status_reason}")
        if (listing := latest_listing(session, p)) is not None:
            lines.append(f"      listing ${listing.price_cents / 100:.2f}: {listing.path}")
        lines.append(f"      to decide: python -m app.cli review {p.id}")
    if not waiting:
        lines.append("  none")
    ready = products(S.READY_TO_PUBLISH)
    lines += ["", "Approved, waiting for you to upload (READY_TO_PUBLISH):"]
    for p in ready:
        listing = latest_listing(session, p)
        lines.append(f"  #{p.id} {p.name}" + (f": follow UPLOAD_STEPS.txt in {listing.path}" if listing else ""))
    if not ready:
        lines.append("  none")
    lines += ["", "Paused products:"]
    for p in products(S.PAUSED):
        command = (f"python -m app.cli approve-rework {p.id}" if p.paused_from == S.REWORK
                   else f"python -m app.cli resume {p.id}")
        lines.append(f"  #{p.id} {p.name} (reworks used {p.rework_count}/{rework_limit(settings, p)}): "
                     f"{p.status_reason}")
        lines.append(f"      to continue: {command}")
    if not products(S.PAUSED):
        lines.append("  none")
    lines += ["", "Products in ERROR (fix the cause, then python -m app.cli resume <id>):"]
    lines += [f"  #{p.id} {p.name}: {p.status_reason}" for p in products(S.ERROR)] or ["  none"]
    counts = dict(session.execute(select(Product.status, func.count()).group_by(Product.status)).all())
    lines += ["", "All products: " + (", ".join(f"{k.value if hasattr(k, 'value') else k} {v}"
                                              for k, v in sorted(counts.items(), key=lambda kv: str(kv[0])))
                                     or "none"),
              f"Claude headless runs today: {headless_runs_today(session, settings)} of "
              f"{settings.max_claude_runs_per_day}", ""]
    return lines


def _between(column, shift: Shift):
    end = shift.ended_at or utcnow()
    return (column >= shift.started_at) & (column <= end)


def _shift_section(session: Session, settings: Settings, shift: Shift, local) -> list[str]:
    end = shift.ended_at or utcnow()
    lines = [RULE, f"SHIFT #{shift.id}: {local(shift.started_at)} to {local(end).split(', ')[-1]}",
             f"Result: {shift.stop_reason}", RULE, ""]
    steps = json.loads(shift.steps_json or "[]")
    lines.append("What each team did:")
    first_pass = len(STEP_LABELS)
    shown = [s for i, s in enumerate(steps) if i < first_pass or s["ran"]]  # later rounds: only steps that did work
    lines += [f"  {STEP_LABELS.get(s['step'], s['step']):<12} {s['summary']}" for s in shown] or [
        "  nothing ran (see Result)"]

    opps = session.scalars(select(Opportunity).where(_between(Opportunity.scored_at, shift))
                           .order_by(Opportunity.rank)).all()
    lines += ["", "Ideas found and scores:"]
    for opp in opps:
        reasons = json.loads(opp.reasons_json or "[]")
        lines.append(f"  {opp.score:5.1f}  {opp.decision:<15} {opp.title}")
        if reasons:
            lines.append(f"         {reasons[-1]}")
    if not opps:
        lines.append("  none scored this shift")

    builds = session.scalars(select(Build).where(_between(Build.created_at, shift)).order_by(Build.id)).all()
    lines += ["", "Products built:"]
    for build in builds:
        product = session.get(Product, build.product_id)
        files = ", ".join(m["name"] for m in json.loads(build.manifest_json or "[]"))
        lines.append(f"  #{product.id} {product.name} v{build.version}: {files}")
        lines.append(f"      {build.path}")
        tests = dict(session.execute(select(TestResult.passed, func.count()).where(TestResult.build_id == build.id)
                                     .group_by(TestResult.passed)).all())
        if tests:
            lines.append(f"      Testing: {tests.get(True, 0)} passed, {tests.get(False, 0)} failed")
    if not builds:
        lines.append("  none")

    reviews = session.scalars(select(Review).where(_between(Review.created_at, shift)).order_by(Review.id)).all()
    lines += ["", "Testing, Visual QA, QA and Red Team results:"]
    failed_tests = session.scalars(select(TestResult).where(_between(TestResult.created_at, shift),
                                                            TestResult.passed.is_(False))).all()
    for test in failed_tests[:8]:
        lines.append(f"  Testing failed: {test.check} [{test.target}] {test.detail}"[:300])
    for review in reviews:
        product = session.get(Product, review.product_id)
        build = session.get(Build, review.build_id) if review.build_id else None
        tag = f"#{product.id} v{build.version if build else '?'}"
        if review.department == Department.QA:
            report = json.loads(review.report_json or "{}")
            render = report.get("render", {})
            vision = sum(1 for f in report.get("findings", []) if f.get("source") == "vision")
            visual = (f"Visual QA: {len(render.get('charts', []))} chart(s) by {render.get('engine')}, {vision} "
                      f"finding(s)" if render.get("engine") else "Visual QA skipped (no Excel/LibreOffice)")
            lines.append(f"  QA {tag}: {review.decision}, score {review.score}. {visual}")
        else:
            changes = json.loads(review.report_json or "{}").get("required_changes", [])
            lines.append(f"  Red Team {tag}: {review.decision}. {review.summary}"[:400])
            lines += [f"      required: {c['id']}: {c['change']}"[:300] for c in changes]
    if not reviews and not failed_tests:
        lines.append("  none")

    revisions = session.scalars(select(SpecVersion).where(_between(SpecVersion.created_at, shift))
                                .order_by(SpecVersion.id)).all()
    lines += ["", "Reworks done (spec versions):"]
    for row in revisions:
        product = session.get(Product, row.product_id)
        brief = session.get(ReworkBrief, row.brief_id) if row.brief_id else None
        if row.author == "file-import":
            lines.append(f"  #{product.id} spec v{row.version}: first version, imported from config/specs")
            continue
        changed = sum(1 for ln in row.diff_text.splitlines() if ln[:1] in "+-" and ln[:3] not in ("+++", "---"))
        what = f"brief #{brief.id} ({', '.join(brief.change_ids)})" if brief else row.reason[:120]
        lines.append(f"  #{product.id} spec v{row.version} by {row.author}: {what}; {changed} diff lines")
    if not revisions:
        lines.append("  none")

    errors = json.loads(shift.errors_json or "[]")
    failed_runs = session.scalars(select(AgentRun).where(_between(AgentRun.started_at, shift),
                                                         AgentRun.status == JobStatus.FAILED)).all()
    job_failures = session.scalars(select(Event).where(_between(Event.created_at, shift),
                                                       Event.event_type == "job.failed")).all()
    lines += ["", "Errors:"]
    lines += [f"  {e}"[:300] for e in errors]
    lines += [f"  job #{e.job_id} failed: {e.message}"[:300] for e in job_failures]
    lines += [f"  {r.provider} run #{r.id} ({r.department.value}) failed: {r.error}"[:300] for r in failed_runs[:10]]
    if not (errors or job_failures or failed_runs):
        lines.append("  none")

    summary = json.loads(shift.summary_json or "{}")
    disk = summary.get("disk") or disk_report(settings).as_dict(settings)
    ollama = disk.get("ollama_models_gb")
    lines += ["", f"Disk: {disk['free_gb']} GB free (minimum {disk['min_free_disk_gb']}); Hive-Mint folder "
                  f"{disk['hivemint_folder_gb']} GB (max {disk['max_hivemint_storage_gb']}); Ollama models "
                  f"{'not found' if ollama is None else f'{ollama} GB'}",
              f"Claude headless runs: {shift.claude_runs} this shift; {summary.get('claude_runs_today', '?')} of "
              f"{summary.get('claude_runs_cap', settings.max_claude_runs_per_day)} used today", ""]
    return lines
