"""Product Design: write a product's spec, and revise it when a gate sends the product back.

SPECIFICATION: an owner-specified product (config/specs/<slug>.json) uses that spec. Otherwise one
headless Claude Code run turns the approved opportunity and its research into a v4 spec. Claude
writes the draft to products/drafts/, checks it with `python -m app.cli spec-check <file>` (which
builds it and runs Testing) until it passes, then answers. Code validates the draft again and stores
it as spec v1.

REWORK: if the active rework brief asks for changes the spec does not cover yet (or a failed build or
test left no brief, in which case one is written from the failure), one headless run edits a copy
of the current spec to implement them, lists their ids under "resolves" and bumps spec_version.
Code checks all of that and stores the new version with a diff; Production rebuilds from it and
every gate runs again. A run that fails or returns an unusable spec raises, so the job retries
within MAX_JOB_ATTEMPTS and then goes to ERROR. The rework limit (lifecycle) still pauses the
product for the owner (python -m app.cli approve-rework <id>).
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from agents.departments import StageResult, product_design as deterministic_design
from agents.research import opportunity_for, owner_sourced
from agents.rework import active_brief, create_brief, uncovered
from app.lifecycle import log_event
from app.models import Job, Product, ResearchRecord, Review, ReworkBrief
from app.states import Department, JobStatus, Outcome, ProductStatus
from config.settings import ROOT, Settings
from providers import headless_policy
from providers.base import ProviderError
from providers.claude_headless import ClaudeHeadless
from tools.spec import SPEC_DIR, SpecError, prepare
from tools.spec_store import dump, latest_version, product_spec, save_version

S = ProductStatus
ACTOR = "product_design"
SPEC_TIMEOUT_S = 2400  # drafting plus several spec-check rounds
FORMAT_EXAMPLE = SPEC_DIR / "mobile-auto-detailer-income-expense-tracker.json"
BUILDER_DOC = ROOT / "tools" / "builders" / "spreadsheet.py"


class SpecAnswer(BaseModel):
    spec_path: str = Field(description="Repo-relative path of the spec file you wrote")
    summary: str = Field(description="What the spec contains or what you changed, in two or three sentences")
    spec_check_passed: bool = Field(description="True only if the last spec-check run printed SPEC OK")


def _rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


RULES = """Spec rules (code rejects a spec that breaks them):
- Format: read {builder_doc} (the module docstring documents every sheet kind, item type, token, chart and PDF/text
  field) and use {example} as the worked example. "builder" must be "spreadsheet".
- Formulas may only use the functions in spreadsheet_rules.allowed_functions (Excel 2010+ and Google Sheets both
  have them). Keep the example's allowed_functions and forbidden_values lists unless you need fewer functions.
- Every table or layout sheet with formulas must be exercised by at least one scenario (inputs and/or expect),
  with exact expected numbers you have worked out by hand. Use "op": "nonzero" / "blank" / "nonblank" for
  example workbooks and warning cells.
- Include "features" (each with evidence that QA can find in the files), "claims_not_to_make" (phrases the
  product must never say, e.g. "guaranteed", "certified", "approved by"), and set "regulated_topic" to "tax",
  "legal", "financial" or "" so Compliance knows which disclaimer the README and PDF need.
- Generic only: no brand names, logos, trademarks or vendor-specific wording anywhere in the files.
- The README and the PDF must name every file in the download; required_headings / required_phrases must match
  the text you write.
- Check your file with:  .venv/Scripts/python -m app.cli spec-check {draft}
  Fix every problem it reports and run it again until it prints SPEC OK. Do not edit any other file."""

WRITE_PROMPT = """You are Hive-Mint's Product Design department. Write the product spec for this approved opportunity.

PRODUCT: {name}  (slug: {slug})
OPPORTUNITY:
{opportunity}

RESEARCH (niche: {niche}, confidence {confidence}):
{research}

Design a spreadsheet product a small-business buyer can use without help: a template workbook (role "template")
with clear input tables, a Dashboard of key figures and 2-3 charts, monthly or category summaries where useful,
a How to Use sheet and a Lists sheet; an example workbook ("example_of" the template) with realistic sample data;
an Instructions.pdf; and a README.txt. Address the buyer complaints and gaps above. Set "slug" to "{slug}",
"name" to the product name, "spec_version" to 1, and "zip_name" to a short file name ending in .zip.

Write the complete spec as JSON to {draft}.

{rules}

When spec-check prints SPEC OK, answer with JSON matching the provided schema."""

REVISE_PROMPT = """You are Hive-Mint's Product Design department. A gate sent product "{name}" back for rework. Revise its
spec so the next build passes.

The current spec (version {version}) has been copied to {draft}. Edit that file in place.

REQUIRED CHANGES (rework brief #{brief_id} from {source}); implement every one, exactly:
{changes}

FINDINGS FROM THE LAST REVIEWS AND TESTS:
{findings}

Also:
- add every change id above to "resolves" (keep the ids already there);
- set "spec_version" to {next_version} and append a changelog entry for it saying what changed;
- keep "slug" as "{slug}"; change nothing the required changes and findings do not call for.

{rules}

When spec-check prints SPEC OK, answer with JSON matching the provided schema."""


def spec_problems(spec: dict) -> list[str]:
    """Product-level rules on top of tools.spec.validate_spec. Empty means acceptable."""
    try:
        normalized = prepare(spec)
    except (SpecError, KeyError, TypeError, StopIteration) as error:
        return [f"invalid spec: {error}"]
    problems = []
    if normalized.get("builder") != "spreadsheet":
        problems.append('builder must be "spreadsheet"')
    if not normalized.get("claims_not_to_make"):
        problems.append("claims_not_to_make is empty")
    if not normalized.get("features"):
        problems.append("features is empty")
    if "regulated_topic" not in normalized:
        problems.append('regulated_topic is missing (use "" if none)')
    for file in normalized["files"]:
        if file["kind"] != "xlsx" or file.get("example_of"):
            continue
        covered = {ref["sheet"] for s in file.get("scenarios", []) for ref in s.get("inputs", []) + s.get("expect", [])}
        for sheet in file.get("sheets", []):
            has_formulas = any(c.get("formula") for c in sheet.get("column_defs", [])) or any(
                "formula" in i or i.get("type") == "grid" for i in sheet.get("items", []))
            if has_formulas and sheet["name"] not in covered:
                problems.append(f"{file['name']}: sheet '{sheet['name']}' has formulas but no scenario touches it")
    return problems


class ProductDesign:
    """SPECIFICATION and REWORK handler (replaces the stub and the pause-for-a-human rework routing)."""

    # The Operations attempt wrapper's name; each headless call logs its own claude-headless AgentRun.
    provider = "product_design"

    def __init__(self, settings: Settings, provider_factory=None):
        self.settings = settings
        self.provider_factory = provider_factory or (lambda session: ClaudeHeadless(settings, session))

    def __call__(self, product: Product, job: Job) -> StageResult:
        if product.status == S.SPECIFICATION:
            return self.specify(product, job)
        if product.status == S.REWORK:
            return self.rework(product, job)
        return deterministic_design(product, job)

    # ------------------------------------------------------------------ SPECIFICATION

    def specify(self, product: Product, job: Job) -> StageResult:
        session = object_session(product)
        if owner_sourced(product) or latest_version(session, product) is not None:
            spec = product_spec(session, product, self.settings.products_dir)
            return StageResult(Outcome.PASS, S.PRODUCTION, f"Spec v{spec['spec_version']} on record; routed to "
                                                           "Production", {"spec_version": spec["spec_version"]})
        opp = opportunity_for(session, product)
        if opp is None:
            return StageResult(Outcome.FAIL, S.RESEARCH, "No opportunity or spec to design from; back to Research")
        record = session.get(ResearchRecord, opp.research_id)
        draft = self._draft_path(product)
        draft.unlink(missing_ok=True)
        opportunity = {"title": opp.title, "description": opp.description, **json.loads(opp.data_json),
                       "score": opp.score}
        research = {"summary": record.summary, **json.loads(record.findings_json)}
        prompt = WRITE_PROMPT.format(name=product.name, slug=product.slug, niche=record.niche,
                                     confidence=record.confidence, opportunity=json.dumps(opportunity, indent=1),
                                     research=json.dumps(research, indent=1), draft=_rel(draft), rules=self._rules(draft))
        spec, answer = self._run(session, job, prompt, draft)
        spec["slug"], spec["spec_version"] = product.slug, 1
        self._accept(spec, draft)
        row = save_version(session, product, spec, author="claude-headless", reason=answer.summary,
                           products_dir=self.settings.products_dir, job=job)
        return StageResult(Outcome.PASS, S.PRODUCTION, f"Spec v1 written: {answer.summary}"[:500],
                           {"spec_version_id": row.id})

    # ------------------------------------------------------------------ REWORK

    def rework(self, product: Product, job: Job) -> StageResult:
        session = object_session(product)
        spec = product_spec(session, product, self.settings.products_dir)
        brief = active_brief(session, product)
        if brief is None and product.previous_status in (S.PRODUCTION, S.TESTING) and spec is not None:
            brief = self._brief_from_failure(session, product, job)
        if brief is None or spec is None:
            return deterministic_design(product, job)
        missing = uncovered(brief, spec)
        if not missing:
            return StageResult(Outcome.PASS, S.PRODUCTION,
                               f"Rework brief #{brief.id} ({len(brief.change_ids)} changes) routed to Production",
                               {"brief_id": brief.id})
        current = latest_version(session, product)
        draft = self._draft_path(product)
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.write_text(current.spec_json + "\n", encoding="utf-8")
        changes = json.loads(brief.required_changes_json)
        prompt = REVISE_PROMPT.format(
            name=product.name, slug=product.slug, version=current.version, next_version=current.version + 1,
            draft=_rel(draft), brief_id=brief.id, source=brief.source, rules=self._rules(draft),
            changes="\n".join(f"- {c['id']}: {c['change']}\n  reason: {c['reason']}\n  acceptance: "
                              f"{c.get('acceptance', '')}" for c in changes),
            findings=self._findings(session, product, brief))
        revised, answer = self._run(session, job, prompt, draft)
        revised["slug"] = product.slug
        revised["spec_version"] = current.version + 1
        if still := [i for i in brief.change_ids if i not in revised.get("resolves", [])]:
            raise SpecError(f"revised spec does not list these brief changes under resolves: {', '.join(still)}")
        self._accept(revised, draft)
        row = save_version(session, product, revised, author="claude-headless", reason=answer.summary,
                           products_dir=self.settings.products_dir, brief_id=brief.id, job=job)
        log_event(session, "spec.revised", actor=ACTOR, product=product, job=job,
                  message=f"Spec v{row.version} implements rework brief #{brief.id}: {', '.join(missing)}",
                  data={"spec_version_id": row.id, "brief_id": brief.id, "change_ids": missing})
        return StageResult(Outcome.PASS, S.PRODUCTION,
                           f"Spec v{row.version} implements rework brief #{brief.id} ({', '.join(missing)})",
                           {"brief_id": brief.id, "spec_version_id": row.id})

    def _brief_from_failure(self, session: Session, product: Product, job: Job) -> ReworkBrief:
        """A failed build or test leaves no brief; write one so the spec gets fixed instead of rebuilt as is."""
        last = session.scalar(select(Job).where(Job.product_id == product.id, Job.status == JobStatus.SUCCEEDED,
                                                Job.department.in_([Department.PRODUCTION, Department.TESTING]))
                              .order_by(Job.id.desc()).limit(1))
        output = json.loads(last.output_json) if last else {}
        failures = output.get("failures") or [product.status_reason]
        source = "TESTING" if last is not None and last.department == Department.TESTING else "PRODUCTION"
        change_id = f"fix_{source.lower()}_failures_{last.id if last else job.id}"
        return create_brief(session, product, source=source, actor=ACTOR, summary=product.status_reason[:500],
                            required_changes=[{"id": change_id, "change": "Fix the spec so these checks pass: "
                                               + "; ".join(failures[:20]),
                                               "reason": "The build must function before anyone reviews it",
                                               "acceptance": "spec-check prints SPEC OK"}],
                            findings=[{"failure": f} for f in failures], job=job)

    # ------------------------------------------------------------------ shared

    def _draft_path(self, product: Product) -> Path:
        return self.settings.products_dir / "drafts" / f"product-{product.id}" / "spec.json"

    @staticmethod
    def _rules(draft: Path) -> str:
        return RULES.format(builder_doc=_rel(BUILDER_DOC), example=_rel(FORMAT_EXAMPLE), draft=_rel(draft))

    def _run(self, session: Session, job: Job, prompt: str, draft: Path) -> tuple[dict, SpecAnswer]:
        completion = self.provider_factory(session).run(job, Department.PRODUCT_DESIGN, prompt, headless_policy.SPEC,
                                                        response_model=SpecAnswer, timeout_s=SPEC_TIMEOUT_S)
        answer: SpecAnswer = completion.data
        if not draft.is_file():
            raise ProviderError(f"Claude answered but wrote no spec at {_rel(draft)}")
        try:
            return json.loads(draft.read_text(encoding="utf-8")), answer
        except ValueError as error:
            raise SpecError(f"{_rel(draft)} is not valid JSON: {error}") from error

    @staticmethod
    def _accept(spec: dict, draft: Path) -> None:
        if problems := spec_problems(spec):
            raise SpecError(f"spec in {_rel(draft)} rejected: " + "; ".join(problems))

    @staticmethod
    def _findings(session: Session, product: Product, brief: ReworkBrief) -> str:
        lines = [f"- [{brief.source}] {json.dumps(f)}" for f in json.loads(brief.findings_json)[:15]]
        if brief.review_id:
            review = session.get(Review, brief.review_id)
            lines.append(f"- review #{review.id} ({review.department.value} {review.decision}): {review.summary}")
        last_qa = session.scalar(select(Review).where(Review.product_id == product.id,
                                                      Review.department == Department.QA)
                                 .order_by(Review.id.desc()).limit(1))
        if last_qa is not None and last_qa.id != brief.review_id:
            lines.append(f"- last QA review #{last_qa.id} ({last_qa.decision}, score {last_qa.score}): "
                         f"{last_qa.summary}")
        return "\n".join(lines) or "- none recorded"
