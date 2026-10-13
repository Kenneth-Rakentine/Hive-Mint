"""QA department: "is it good?" Deterministic checks first, then a local-model pass.

Deterministic (no AI): spelling against a dictionary plus a domain allowlist, consistent
sheet and column naming, every spec feature present in the build, and the README and PDF
naming every file in the download. Then the Tier 1 local model (Ollama) reads a compact
text digest of the product, never raw files, and reports clarity problems and confusing
instructions.

The model cannot fail a product on its own: its findings are capped at "major" and their
total deduction at QA_MODEL_MAX_DEDUCTION. A critical or major deterministic finding, or a
score under QA_PASS_SCORE, sends the product to REWORK with a rework brief.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

import httpx
from openpyxl import load_workbook
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import object_session

from agents.departments import StageResult
from agents.rework import create_brief
from app.lifecycle import log_event
from app.models import Build, Job, Product, Review
from app.states import Department, Outcome, ProductStatus
from config.settings import ROOT, Settings
from providers.ollama import OllamaProvider
from tools.digest import pdf_text, product_digest, workbook_text
from tools.spec import load_spec

S = ProductStatus
SEVERITY_POINTS = {"critical": 40, "major": 15, "minor": 5, "info": 0}
BLOCKING = frozenset({"critical", "major"})
ALLOWLIST = ROOT / "config" / "qa_words.txt"
SMALL_WORDS = frozenset({"a", "an", "and", "as", "at", "by", "for", "in", "of", "on", "or", "per", "the", "to",
                         "vs", "with"})
_WORD = re.compile(r"[A-Za-z][A-Za-z']*")
_SKIP_TOKEN = re.compile(r"\S*[_./@\\]\S*|\S*\d\S*")  # file names, URLs, emails, anything with digits

SYSTEM = ("You are the QA department of a company that sells spreadsheet templates to small-business owners. "
          "You check whether a product is clear and easy for a non-technical buyer to use. You only judge clarity "
          "and instructions: formulas, totals and file integrity were already tested by code. Be specific and "
          "fair; do not invent problems. If the instructions are clear, return few or no findings.")
PROMPT = """Review this product digest (sheet names, column headers, instruction text, chart titles, the PDF
guide and the README). Look for: confusing or contradictory instructions, steps a beginner could not follow,
undefined jargon, places where the buyer would not know where to type, and mismatches between the guide and
the workbook.

Text marked "[... trimmed]" was shortened to fit; do not report the cut itself as a problem.

Return JSON: clarity_score from 0 (unusable) to 100 (perfectly clear), and up to 8 findings, each with
severity "minor" or "major", where (sheet or document), issue, and suggestion.

PRODUCT DIGEST
{digest}
"""


@dataclass
class Finding:
    source: str  # "deterministic" or "model"
    check: str
    severity: str
    where: str
    issue: str
    suggestion: str = ""


class ModelFinding(BaseModel):
    severity: Literal["minor", "major"]
    where: str
    issue: str
    suggestion: str = ""


class ModelReview(BaseModel):
    clarity_score: int = Field(ge=0, le=100)
    findings: list[ModelFinding] = Field(default_factory=list, max_length=8)


# --------------------------------------------------------------------------- deterministic checks

@lru_cache(maxsize=1)
def _spellchecker():
    from spellchecker import SpellChecker

    return SpellChecker()


def allowlist(path: Path = ALLOWLIST) -> set[str]:
    if not path.is_file():
        return set()
    lines = path.read_text(encoding="utf-8").splitlines()
    return {line.strip().lower() for line in lines if line.strip() and not line.startswith("#")}


def words(text: str) -> list[str]:
    text = _SKIP_TOKEN.sub(" ", text)
    out = []
    for word in _WORD.findall(text.replace("-", " ")):
        word = word.strip("'")
        if word.endswith("'s"):
            word = word[:-2]
        if len(word) > 1 and not word.isupper():  # skip acronyms (IRS, PDF, PPE)
            out.append(word.lower())
    return out


def check_spelling(texts: dict[str, str], extra_words: set[str]) -> list[Finding]:
    """texts: where -> text. One finding listing every unknown word, with where it first appears."""
    first_seen: dict[str, str] = {}
    for where, text in texts.items():
        for word in words(text):
            first_seen.setdefault(word, where)
    unknown = sorted(w for w in _spellchecker().unknown(first_seen) if w not in extra_words)
    if not unknown:
        return []
    severity = "major" if len(unknown) >= 5 else "minor"
    listed = ", ".join(f"'{w}' ({first_seen[w]})" for w in unknown[:20])
    return [Finding("deterministic", "spelling", severity, "all text", f"{len(unknown)} unrecognised word(s): {listed}",
                    "Fix the spelling, or add genuine domain words to config/qa_words.txt")]


def _title_case_problem(name: str) -> bool:
    tokens = [t for t in re.split(r"[\s/]+", name) if t and t[0].isalpha()]
    for index, token in enumerate(tokens):
        if index > 0 and token.lower() in SMALL_WORDS:
            if token != token.lower():
                return True
        elif not token[0].isupper():
            return True
    return False


def _naming_key(name: str) -> str:
    """Names differing only in case, spacing or punctuation ("Paid?" vs "Paid", "Set Aside" vs "Set-Aside")
    are the same concept spelled two ways. Singular vs plural is allowed: "IRS Rates" (a tab) holds rates and
    "IRS Rate" (a column) is one rate."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def check_naming(sheet_names: list[str], headers: dict[str, list[str]]) -> list[Finding]:
    """Title Case for sheet names and headers, and one spelling per concept across sheets."""
    findings = []
    bad_case = [n for n in sheet_names if _title_case_problem(n)]
    bad_case += [f"{sheet}: {h}" for sheet, hs in headers.items() for h in hs if _title_case_problem(h)]
    if bad_case:
        findings.append(Finding("deterministic", "naming", "minor", "sheet and column names",
                                f"Not in Title Case: {', '.join(bad_case[:12])}",
                                "Use Title Case consistently (small words like 'per' and 'to' stay lowercase)"))
    forms: dict[str, set[str]] = {}
    for name in sheet_names + [h for hs in headers.values() for h in hs]:
        forms.setdefault(_naming_key(name), set()).add(name)
    clashes = [sorted(f) for f in forms.values() if len(f) > 1]
    if clashes:
        findings.append(Finding("deterministic", "naming", "major", "sheet and column names",
                                "Same concept, different names: " + "; ".join(" vs ".join(c) for c in clashes),
                                "Pick one name per concept and use it everywhere"))
    return findings


def check_features(spec: dict, build_dir: Path) -> list[Finding]:
    """Every feature the spec promises has visible evidence in the build."""
    findings = []
    workbooks: dict[str, object] = {}
    for feature in spec.get("features", []):
        evidence = feature["evidence"]
        path = build_dir / evidence["file"]
        problem = None
        if not path.is_file():
            problem = f"{evidence['file']} is missing"
        elif "sheet" in evidence:
            if evidence["file"] not in workbooks:
                workbooks[evidence["file"]] = load_workbook(path)
            wb = workbooks[evidence["file"]]
            ws = wb[evidence["sheet"]] if evidence["sheet"] in wb.sheetnames else None
            if ws is None:
                problem = f"sheet '{evidence['sheet']}' is missing"
            elif "column" in evidence and evidence["column"] not in {str(c.value).strip() for c in ws[1] if c.value}:
                problem = f"column '{evidence['column']}' is missing on {ws.title}"
            elif "label" in evidence and not any(isinstance(c.value, str) and c.value.strip() == evidence["label"]
                                                 for row in ws.iter_rows() for c in row):
                problem = f"'{evidence['label']}' is not shown on {ws.title}"
            elif len(ws._charts) < evidence.get("charts_min", 0):
                problem = f"{ws.title} has {len(ws._charts)} chart(s), needs {evidence['charts_min']}"
        if problem:
            findings.append(Finding("deterministic", "spec_features", "critical", evidence["file"],
                                    f"Promised feature '{feature['id']}' ({feature['description']}): {problem}",
                                    "Build the feature or stop promising it"))
    return findings


def check_docs_mention_files(spec: dict, doc_texts: dict[str, str]) -> list[Finding]:
    """The README and the PDF each name every other file in the download."""
    findings = []
    names = [f["name"] for f in spec["files"]]
    for doc, text in doc_texts.items():
        flat = re.sub(r"\s+", "", text)  # PDF extraction can break long names across lines
        missing = [n for n in names if n != doc and n not in text and n not in flat]
        if missing:
            findings.append(Finding("deterministic", "docs_mention_files", "major", doc,
                                    f"{doc} does not mention {', '.join(missing)}",
                                    "List every file in the download, by its exact name"))
    return findings


def deterministic_checks(spec: dict, build_dir: Path, extra_words: set[str] | None = None) -> list[Finding]:
    extra_words = allowlist() if extra_words is None else extra_words
    texts: dict[str, str] = {}
    sheet_names: list[str] = []
    headers: dict[str, list[str]] = {}
    doc_texts: dict[str, str] = {}
    findings: list[Finding] = []
    for file_spec in spec["files"]:
        path = build_dir / file_spec["name"]
        if not path.is_file():
            continue  # Testing already failed missing files; features below will say so too
        try:
            if file_spec["kind"] == "xlsx":
                for sheet, entry in workbook_text(file_spec, path).items():
                    sheet_names.append(sheet)
                    if entry["headers"]:
                        headers[sheet] = entry["headers"]
                    texts[f"{file_spec['name']}:{sheet}"] = " ".join(
                        [sheet] + entry["headers"] + entry["text"] + entry["charts"])
            elif file_spec["kind"] == "pdf":
                doc_texts[file_spec["name"]] = texts[file_spec["name"]] = pdf_text(path)
            else:
                doc_texts[file_spec["name"]] = texts[file_spec["name"]] = path.read_text(encoding="utf-8")
        except Exception as error:  # unreadable for a buyer is a QA problem too
            findings.append(Finding("deterministic", "readable", "critical", file_spec["name"],
                                    f"Could not read {file_spec['name']}: {type(error).__name__}: {error}"))
    findings += check_spelling(texts, extra_words)
    findings += check_naming(sheet_names, headers)
    findings += check_features(spec, build_dir)
    findings += check_docs_mention_files(spec, doc_texts)
    return findings


# --------------------------------------------------------------------------- scoring

def score_report(findings: list[Finding], *, pass_score: int, model_cap: int) -> dict:
    deterministic = sum(SEVERITY_POINTS[f.severity] for f in findings if f.source != "model")
    model = min(model_cap, sum(SEVERITY_POINTS[f.severity] for f in findings if f.source == "model"))
    score = max(0, 100 - deterministic - model)
    blocking = [f for f in findings if f.source != "model" and f.severity in BLOCKING]
    decision = "PASS" if score >= pass_score and not blocking else "REWORK"
    return {"score": score, "decision": decision, "pass_score": pass_score, "deterministic_deduction": deterministic,
            "model_deduction": model, "model_deduction_cap": model_cap}


def required_changes(findings: list[Finding]) -> list[dict]:
    """REWORK brief items: every critical or major finding, deterministic first."""
    changes = []
    ordered = sorted(findings, key=lambda f: (f.source == "model", f.check))
    for index, finding in enumerate((f for f in ordered if f.severity in BLOCKING), start=1):
        changes.append({"id": f"qa-{finding.check.replace('_', '-')}-{index}",
                        "change": finding.suggestion or f"Fix: {finding.issue}",
                        "reason": f"[{finding.severity}] {finding.where}: {finding.issue}",
                        "acceptance": f"QA no longer reports this {finding.check} finding"})
    return changes


# --------------------------------------------------------------------------- department

class QA:
    provider = "qa"

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client

    def __call__(self, product: Product, job: Job) -> StageResult:
        session = object_session(product)
        spec = load_spec(product.slug)
        if spec is None:
            return StageResult(Outcome.FAIL, S.REWORK, f"QA: no specification for '{product.slug}'")
        build = session.scalar(select(Build).where(Build.product_id == product.id)
                               .order_by(Build.version.desc()).limit(1))
        if build is None:
            return StageResult(Outcome.FAIL, S.REWORK, "QA: no build recorded for this product")
        build_dir = Path(build.path)

        findings = deterministic_checks(spec, build_dir)
        provider = OllamaProvider(self.settings, session, client=self.client)
        completion = provider.generate(job, Department.QA, PROMPT.format(digest=product_digest(spec, build_dir)),
                                       system=SYSTEM, response_model=ModelReview)  # Ollama down: raises, job retries
        model_review: ModelReview = completion.data
        findings += [Finding("model", "clarity", f.severity, f.where, f.issue, f.suggestion)
                     for f in model_review.findings]

        report = score_report(findings, pass_score=self.settings.qa_pass_score,
                              model_cap=self.settings.qa_model_max_deduction)
        report |= {"build_version": build.version, "clarity_score": model_review.clarity_score,
                   "model": completion.model, "findings": [asdict(f) for f in findings]}
        counts = {s: sum(f.severity == s for f in findings) for s in ("critical", "major", "minor")}
        summary = (f"QA {report['decision']} v{build.version}: score {report['score']}/100 "
                   f"(pass {report['pass_score']}); {counts['critical']} critical, {counts['major']} major, "
                   f"{counts['minor']} minor; model clarity {model_review.clarity_score}/100")
        review = Review(product_id=product.id, job_id=job.id, build_id=build.id, department=Department.QA,
                        reviewer=f"qa+{provider.name}", model=completion.model, decision=report["decision"],
                        score=report["score"], summary=summary, findings_json=json.dumps(report["findings"]),
                        report_json=json.dumps(report))
        session.add(review)
        session.flush()
        log_event(session, "qa.report", actor="qa", product=product, job=job, message=summary,
                  data={"review_id": review.id, "score": report["score"], "decision": report["decision"]})
        data = {"review_id": review.id, "score": report["score"], "build_id": build.id, "version": build.version}
        if report["decision"] == "PASS":
            return StageResult(Outcome.PASS, S.RED_TEAM, summary, data)

        blocking = [f for f in findings if f.severity in BLOCKING]
        changes = required_changes(findings) or [{
            "id": "qa-score", "change": "Raise the QA score: address the minor findings in the QA report",
            "reason": f"Score {report['score']} is below the pass mark {report['pass_score']}", "acceptance": ""}]
        brief = create_brief(session, product, source="QA", actor="qa", summary=summary, required_changes=changes,
                             findings=[asdict(f) for f in blocking] or report["findings"], review=review, job=job,
                             build=build)
        top = "; ".join(f"[{f.severity}] {f.issue}" for f in blocking[:3])
        return StageResult(Outcome.FAIL, S.REWORK, f"{summary}. {top}".strip(), data | {"rework_brief_id": brief.id})
