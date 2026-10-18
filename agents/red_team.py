"""Red Team department: argues against publishing. Tier 2/3, done in a human-triggered
Claude Code session ($0), never an automatic cloud call.

Operations queues a RED_TEAM job and leaves it waiting. The session (/hive-review <id>):
1. `python -m app.cli review-export <id>` writes research/reviews/<id>/packet.md: spec,
   product digest, test results, QA report, rework history, and the rendered views (chart
   PNGs and sheet PDFs from Visual QA) for the reviewer to look at.
2. Claude reviews it as an independent Red Team and writes a structured verdict JSON.
3. `python -m app.cli review-import <id> <file.json>` validates the verdict and records it:
   PASS moves the product to COMPLIANCE, REWORK to REWORK with a rework brief, REJECT ends it.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agents.departments import StageResult
from agents.rework import RequiredChange, create_brief
from app.db import utcnow
from app.lifecycle import log_event
from app.models import Build, Job, Product, Review, ReworkBrief, TestResult
from app.states import OPEN_JOB_STATUSES, Department, Outcome, ProductStatus
from config.settings import ROOT, Settings
from tools.digest import product_digest
from tools.render import RENDERS_DIR
from tools.spec import load_spec

S = ProductStatus
REVIEWS_DIR = ROOT / "research" / "reviews"
PROVIDER = "claude-code"
TIER = 2
NEXT = {"PASS": S.COMPLIANCE, "REWORK": S.REWORK, "REJECT": S.REJECTED}
CATEGORIES = ("free_alternatives", "differentiation", "double_entry", "missing_feature", "refund_risk", "misleading",
              "presentation", "other")


class VerdictError(ValueError):
    """The verdict cannot be accepted (invalid, stale or for a product not awaiting Red Team)."""


class RedTeamFinding(BaseModel):
    severity: Literal["critical", "major", "minor"]
    category: Literal[CATEGORIES]  # type: ignore[valid-type]
    issue: str = Field(min_length=5)
    evidence: str = ""


class RedTeamVerdict(BaseModel):
    product_id: int
    build_version: int = Field(ge=1)
    decision: Literal["PASS", "REWORK", "REJECT"]
    summary: str = Field(min_length=10, description="The strongest fair case, in a few sentences")
    reviewer: str = PROVIDER
    model: str = PROVIDER
    findings: list[RedTeamFinding] = Field(default_factory=list)
    required_changes: list[RequiredChange] = Field(default_factory=list)
    reject_reason: str | None = None

    @model_validator(mode="after")
    def _decision_has_its_parts(self) -> "RedTeamVerdict":
        if self.decision == "REWORK" and not self.required_changes:
            raise ValueError("REWORK needs at least one specific required change")
        if self.decision != "REWORK" and self.required_changes:
            raise ValueError(f"{self.decision} must not list required changes")
        if self.decision == "REJECT" and not (self.reject_reason or "").strip():
            raise ValueError("REJECT needs a reject_reason")
        ids = [c.id for c in self.required_changes]
        if len(ids) != len(set(ids)):
            raise ValueError("required change ids must be unique")
        return self


class RedTeam:
    """Registered so Operations routes RED_TEAM here, but never run by tick(): see review-import."""

    provider = PROVIDER
    external = True

    def __init__(self, settings: Settings):
        self.settings = settings

    def __call__(self, product: Product, job: Job) -> StageResult:
        raise RuntimeError(f"Red Team runs in a Claude Code session: /hive-review {product.id}")


# --------------------------------------------------------------------------- helpers

def latest_build(session: Session, product: Product) -> Build | None:
    return session.scalar(select(Build).where(Build.product_id == product.id).order_by(Build.version.desc()).limit(1))


def review_dir(product: Product, base: Path = REVIEWS_DIR) -> Path:
    return base / str(product.id)


def open_red_team_job(session: Session, product: Product) -> Job | None:
    return session.scalar(select(Job).where(Job.product_id == product.id, Job.department == Department.RED_TEAM,
                                            Job.status.in_(OPEN_JOB_STATUSES)).order_by(Job.id.desc()).limit(1))


def _verdict_example(product: Product, build: Build | None) -> str:
    example = {
        "product_id": product.id,
        "build_version": build.version if build else 1,
        "decision": "REWORK",
        "summary": "Free templates cover the basics; this needs X to justify the price.",
        "reviewer": "claude-code",
        "model": "<your model id>",
        "findings": [{"severity": "major", "category": "missing_feature",
                      "issue": "No printable invoice, which most detailers expect.",
                      "evidence": "Digest: no Invoice sheet; competitors' listings advertise invoices."}],
        "required_changes": [{"id": "add_invoice_sheet", "change": "Add an Invoice tab that fills from a job row.",
                              "reason": "Buyers expect to bill customers from the same file.",
                              "acceptance": "Selecting a job fills a printable invoice."}],
        "reject_reason": None,
    }
    return json.dumps(example, indent=2)


def rendered_views(build_dir: Path) -> list[str]:
    """Packet lines listing the Visual QA renders of this build, or saying there are none."""
    manifest = build_dir / RENDERS_DIR / "manifest.json"
    if not manifest.is_file():
        return ["No renders for this build (QA ran without Excel or LibreOffice). Open the workbook yourself."]
    render = json.loads(manifest.read_text(encoding="utf-8"))
    lines = [f"Rendered by **{render['engine'] or 'nothing'}** into `{render['out_dir']}`. Open every image below "
             "with the Read tool and judge it as a buyer would, at 100% zoom.", ""]
    lines += [f"- Warning: {w}" for w in render["warnings"]]
    lines += [f"- Chart {c['index']} on {c['sheet']}, '{c['title']}': `{c['png']}`" for c in render["charts"]]
    if not render["charts"]:
        lines.append("- No chart images (see the warnings). Look at the sheet PDFs and the workbook instead.")
    lines += ["", "Sheet PDFs (the Dashboard one shows the whole layout on one page):", ""]
    lines += [f"- `{p}`" for p in render["sheet_pdfs"]] or ["- None."]
    return lines


def _spec_for_packet(spec: dict) -> dict:
    """The spec, with scenario detail reduced to names (the test results below cover the answers)."""
    trimmed = json.loads(json.dumps(spec))
    for file_spec in trimmed["files"]:
        if "scenarios" in file_spec:
            file_spec["scenarios"] = [s["name"] for s in file_spec["scenarios"]]
    return trimmed


# --------------------------------------------------------------------------- export

def export_packet(session: Session, product: Product, base: Path = REVIEWS_DIR) -> Path:
    spec = load_spec(product.slug)
    if spec is None:
        raise VerdictError(f"No specification for '{product.slug}'")
    build = latest_build(session, product)
    if build is None:
        raise VerdictError(f"Product {product.id} has no build to review")
    out_dir = review_dir(product, base)
    out_dir.mkdir(parents=True, exist_ok=True)
    verdict_path = out_dir / "verdict.json"

    results = session.scalars(select(TestResult).where(TestResult.build_id == build.id)).all()
    failed = [r for r in results if not r.passed]
    qa = session.scalar(select(Review).where(Review.product_id == product.id, Review.department == Department.QA)
                        .order_by(Review.id.desc()).limit(1))
    verdicts = session.scalars(select(Review).where(Review.product_id == product.id,
                                                    Review.department == Department.RED_TEAM)
                               .order_by(Review.id)).all()
    briefs = session.scalars(select(ReworkBrief).where(ReworkBrief.product_id == product.id)
                             .order_by(ReworkBrief.id)).all()

    lines = [
        f"# Red Team review packet: {product.name} (product {product.id})",
        "",
        f"- Generated: {utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
        f"- Product status: **{product.status}** (rework cycles used: {product.rework_count})",
        f"- Build under review: **v{build.version}** at `{build.path}`",
        f"- Download: `{build.zip_path}` (sha256 {build.zip_sha256[:16]}...)",
        f"- Spec version: {spec.get('spec_version', 1)}",
        "",
        "## Your task",
        "",
        "You are an independent Red Team. Make the strongest *fair* case against publishing this product, then "
        "decide. Consider at least:",
        "",
        "- **Free alternatives**: what a buyer can get free (Google Sheets templates, Wave, a notebook) and "
        "whether this is clearly better.",
        "- **Weak differentiation**: why this one instead of the many similar listings.",
        "- **Double data entry**: anything the buyer must type twice or copy between places.",
        "- **Missing features buyers expect** for this niche.",
        "- **Refund risk**: what makes a buyer feel misled, confused or disappointed (claims the product does "
        "not meet, broken charts, US-only tax assumptions, setup friction).",
        "- **Presentation**: open every rendered chart image and the Dashboard PDF listed under *Rendered views*. "
        "Overlapping or clipped text, duplicate axis labels, crowded or unreadable labels and charts that look "
        "broken in a listing photo are reasons against publishing (category `presentation`).",
        "",
        "Decide **PASS** (publishable as is), **REWORK** (fixable: list specific required changes, each with a "
        "stable lowercase id, what to change, why, and how to tell it is done) or **REJECT** (not worth "
        "fixing: give reject_reason). Open the files in the build folder if the digest is not enough.",
        "",
        "## Verdict format",
        "",
        f"Write JSON to `{verdict_path.relative_to(ROOT) if verdict_path.is_relative_to(ROOT) else verdict_path}`, "
        f"then run:",
        "",
        "```",
        f".venv\\Scripts\\python -m app.cli review-import {product.id} "
        f"{verdict_path.relative_to(ROOT) if verdict_path.is_relative_to(ROOT) else verdict_path}",
        "```",
        "",
        f"`build_version` must be {build.version}. Finding categories: {', '.join(CATEGORIES)}. "
        "Severities: critical, major, minor. Example:",
        "",
        "```json",
        _verdict_example(product, build),
        "```",
        "",
        "## Test results (Testing: does it function?)",
        "",
        f"{len(results) - len(failed)} passed, {len(failed)} failed, on build v{build.version}.",
    ]
    lines += [f"- FAILED {r.check} [{r.target}]: {r.detail}" for r in failed[:30]]
    lines += ["", "## QA report (QA: is it good?)", ""]
    if qa is None:
        lines.append("No QA report recorded.")
    else:
        report = json.loads(qa.report_json)
        lines.append(f"{qa.decision}, score {qa.score}/100 (reviewer {qa.reviewer}, model {qa.model or 'n/a'}, "
                     f"build id {qa.build_id}).")
        lines += [f"- [{f['severity']}] ({f['source']}/{f['check']}) {f['where']}: {f['issue']}"
                  for f in report.get("findings", [])]
    lines += ["", "## Rendered views (Visual QA)", ""] + rendered_views(Path(build.path))
    lines += ["", "## Rework history", ""]
    lines += [f"- Brief #{b.id} from {b.source} ({b.status}): " +
              "; ".join(f"`{c['id']}` {c['change']}" for c in json.loads(b.required_changes_json))
              for b in briefs] or ["None."]
    lines += ["", "## Earlier Red Team verdicts", ""]
    lines += [f"- {v.created_at:%Y-%m-%d}: {v.decision} on build id {v.build_id}: {v.summary}" for v in verdicts] \
        or ["None."]
    lines += ["", "## Product digest (text extracted from the build)", "", "```text",
              product_digest(spec, Path(build.path)), "```", "",
              "## Specification", "", "```json", json.dumps(_spec_for_packet(spec), indent=2), "```", ""]

    packet = out_dir / "packet.md"
    packet.write_text("\n".join(lines), encoding="utf-8")
    log_event(session, "review.exported", actor="cli", product=product, message=f"Review packet written to {packet}",
              data={"build_id": build.id, "version": build.version, "packet": str(packet)})
    return packet


# --------------------------------------------------------------------------- import

def load_verdict(path: Path) -> RedTeamVerdict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise VerdictError(f"Cannot read verdict {path}: {error}") from error
    try:
        return RedTeamVerdict.model_validate(raw)
    except ValueError as error:
        raise VerdictError(f"Invalid verdict: {error}") from error


def import_verdict(session: Session, settings: Settings, product: Product, verdict: RedTeamVerdict,
                   source: Path | None = None, base: Path = REVIEWS_DIR) -> Review:
    """Record a Red Team verdict and move the product. The caller commits (or rolls back on error)."""
    from agents.operations import OperationsManager

    if verdict.product_id != product.id:
        raise VerdictError(f"Verdict is for product {verdict.product_id}, not {product.id}")
    if product.status != S.RED_TEAM:
        raise VerdictError(f"Product {product.id} is {product.status}, not awaiting Red Team")
    job = open_red_team_job(session, product)
    if job is None:
        raise VerdictError(f"No open Red Team job for product {product.id}; run `python -m app.cli run` first")
    build = latest_build(session, product)
    if build is None or build.version != verdict.build_version:
        raise VerdictError(f"Verdict reviewed build v{verdict.build_version}, but the latest build is "
                           f"v{build.version if build else 'none'}; export a fresh packet and review again")

    findings = [f.model_dump() for f in verdict.findings]
    changes = [c.model_dump() for c in verdict.required_changes]
    review = Review(product_id=product.id, job_id=job.id, build_id=build.id, department=Department.RED_TEAM,
                    reviewer=verdict.reviewer, model=verdict.model, decision=verdict.decision, summary=verdict.summary,
                    findings_json=json.dumps(findings), report_json=verdict.model_dump_json())
    session.add(review)
    session.flush()
    summary = f"Red Team {verdict.decision} v{build.version}: {verdict.summary}"
    data = {"review_id": review.id, "build_id": build.id, "version": build.version}
    if verdict.decision == "REWORK":
        brief = create_brief(session, product, source="RED_TEAM", actor=verdict.reviewer, summary=verdict.summary,
                             required_changes=changes, findings=findings, review=review, job=job, build=build)
        data["rework_brief_id"] = brief.id
        summary += " Required: " + ", ".join(c["id"] for c in changes)
    elif verdict.decision == "REJECT":
        summary += f" Rejected: {verdict.reject_reason}"
    outcome = Outcome.PASS if verdict.decision == "PASS" else Outcome.FAIL
    OperationsManager(session, settings).complete_external_job(
        job, StageResult(outcome, NEXT[verdict.decision], summary, data),
        provider=PROVIDER, model=verdict.model, tier=TIER)

    if source is not None:
        out_dir = review_dir(product, base)
        out_dir.mkdir(parents=True, exist_ok=True)
        count = session.scalar(select(func.count(Review.id)).where(Review.product_id == product.id,
                                                                    Review.department == Department.RED_TEAM))
        kept = out_dir / f"verdict-v{build.version}-{count}.json"
        if source.resolve() != kept.resolve():
            shutil.copyfile(source, kept)
    return review
