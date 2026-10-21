"""Research department: gather evidence about a niche, then gate products on that evidence.

Niche research (python -m app.cli research --niche "...", or the shift) is one headless Claude
Code run with web search and public-page fetching only. It returns a structured record: what
sells, price ranges, common product contents, buyer complaints, gaps, source URLs, a confidence
level and up to five candidate opportunities with per-factor estimates. Code validates it
(enough distinct sources, every factor cites one, no brand names in titles) before anything is
stored. An invalid answer is retried with the problems listed, up to MAX_JOB_ATTEMPTS.

The RESEARCH gate for a product is deterministic: it passes only if the product's opportunity
comes from a record with enough sources and better than low confidence.
"""

from __future__ import annotations

import json
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from agents.departments import StageResult
from app.db import utcnow
from app.lifecycle import log_event
from app.models import Job, Opportunity, Product, ResearchRecord
from app.states import Department, JobStatus, Outcome, ProductStatus
from config.settings import Settings
from providers.base import ProviderError
from providers.claude_headless import ClaudeHeadless
from providers import headless_policy
from tools.brands import brand_hits
from tools.spec import SPEC_DIR

S = ProductStatus
MIN_SOURCES = 3          # the RESEARCH gate's minimum for a stored record
RESEARCH_MIN_SOURCES = 6  # a new niche research answer must cite at least this many
ACTOR = "research"

# What Production can build today. Finance scores automation suitability 1 for anything else.
BUILDABLE_TYPES = ("spreadsheet",)


class Source(BaseModel):
    url: str = Field(description="Public http(s) URL actually read or returned by web search")
    title: str = ""
    supports: str = Field(description="What this source shows, in one sentence")


class PriceRange(BaseModel):
    item: str
    low: float = Field(ge=0)
    high: float = Field(ge=0)
    source: int = Field(ge=0, description="Index into sources")


class Factor(BaseModel):
    score: int = Field(ge=1, le=5)
    reason: str = Field(min_length=5)
    sources: list[int] = Field(default_factory=list, description="Indexes into sources backing this score")


class Factors(BaseModel):
    demand: Factor = Field(description="5 = many sales/reviews seen; 1 = little evidence of buyers")
    competition: Factor = Field(description="5 = crowded with strong, well-reviewed PAID listings on the same "
                                            "marketplace; 1 = few or weak ones. Free templates elsewhere barely "
                                            "count: marketplace buyers pay for a ready-made file instead of hunting "
                                            "for a free one")
    margin: Factor = Field(description="5 = buyers pay $15+ for this kind of file; 1 = under $3")
    ease: Factor = Field(description="5 = a spreadsheet + PDF can deliver it fully; 1 = needs software or services")
    differentiation: Factor = Field(description="5 = clear gap vs existing listings; 3 = a solid, well-made "
                                                "version of a proven seller; 1 = a weaker copy of what exists")
    support_burden: Factor = Field(description="5 = buyers will need lots of help; 1 = self-explanatory")
    automation_suitability: Factor = Field(description="5 = generic, rules-based, testable by formulas; "
                                                       "1 = needs custom work per buyer")


class OpportunityDraft(BaseModel):
    title: str = Field(min_length=5, max_length=120, description="Generic product name, no brand names")
    product_type: Literal["spreadsheet", "pdf", "other"] = Field(
        description="spreadsheet = Excel workbook (+ PDF instructions and README in a ZIP)")
    description: str = Field(min_length=20)
    target_buyer: str
    price_low: float = Field(ge=0)
    price_high: float = Field(ge=0)
    contents: list[str] = Field(min_length=1, description="Sheets/sections the product would contain")
    differentiation: str = Field(description="The gap it fills, tied to complaints or missing features")
    factors: Factors


class ResearchResult(BaseModel):
    niche: str
    summary: str = Field(min_length=20)
    selling: list[str] = Field(description="What is selling now, with evidence")
    price_ranges: list[PriceRange]
    common_contents: list[str]
    complaints: list[str] = Field(description="Buyer complaints from public reviews")
    gaps: list[str]
    sources: list[Source] = Field(min_length=1)
    confidence: Literal["low", "medium", "high"]
    opportunities: list[OpportunityDraft] = Field(min_length=1, max_length=5)


PROMPT = """You are Hive-Mint's Research department. Research this niche for small digital utility products
(spreadsheet workbooks with formulas and charts, sold with a PDF guide and README as a ZIP download):

NICHE: {niche}
{scout_notes}
Research it in depth. Gather evidence with web search and by reading public pages only:
- what is selling now: open at least 6 comparable listings or product pages across at least 2 marketplaces or
  stores, and note their prices, visible review or sales counts and bestseller badges;
- price ranges buyers pay for comparable files;
- what those products commonly contain (sheets, sections, features);
- buyer complaints from public reviews and forum threads (what disappoints, what is missing, what is confusing);
- gaps: needs that existing products do not meet well;
- whether demand is steady or seasonal, and whether buyers are hobbyists or businesses (who pays more).

Rules you must follow:
- Use web search and public pages only. Do not log in, do not bypass paywalls, robots.txt or anti-bot
  measures, and do not bulk-scrape marketplace listings or reviews. Reading a few individual public pages is fine.
- Cite every claim: each price range and each factor score points at entries in "sources" (0-based indexes).
  Only list URLs you actually saw. At least {min_sources} distinct sources.
- Generic products only: no brand names, logos, trademarks or vendor-specific templates in opportunity titles
  (no "Airbnb", "Marriott", "ServiceNow", "ITIL", "Ableton", "Excel" and so on). A generic description of the
  audience is fine ("short-term rental hosts").
- Be honest about confidence: "high" only with several independent sources agreeing; "low" if evidence is thin.
- Propose 1 to 5 opportunities that Hive-Mint could build as a spreadsheet product, scoring each factor 1-5
  using the rubric in the schema descriptions. Do not inflate scores.
{problems}
Answer with JSON matching the provided schema."""


def research_problems(result: ResearchResult, min_sources: int = RESEARCH_MIN_SOURCES) -> list[str]:
    """Deterministic checks on a research answer. Empty means it may be stored."""
    problems = []
    urls = {s.url.strip().rstrip("/").lower() for s in result.sources
            if urlparse(s.url).scheme in ("http", "https") and urlparse(s.url).netloc}
    if len(urls) < min_sources:
        problems.append(f"only {len(urls)} distinct http(s) source URLs; at least {min_sources} are required")
    count = len(result.sources)
    for price in result.price_ranges:
        if price.source >= count:
            problems.append(f"price range '{price.item}' cites source {price.source}, which does not exist")
        if price.high < price.low:
            problems.append(f"price range '{price.item}' has high < low")
    for opp in result.opportunities:
        if hits := brand_hits(opp.title):
            problems.append(f"opportunity '{opp.title}' uses brand names in its title: {', '.join(hits)}")
        for name, factor in opp.factors:
            if not factor.sources:
                problems.append(f"opportunity '{opp.title}': factor {name} cites no source")
            elif any(i >= count or i < 0 for i in factor.sources):
                problems.append(f"opportunity '{opp.title}': factor {name} cites a source that does not exist")
        if opp.price_high < opp.price_low:
            problems.append(f"opportunity '{opp.title}' has price_high < price_low")
    return problems


def run_niche_research(session: Session, settings: Settings, niche: str, *,
                       provider: ClaudeHeadless | None = None) -> ResearchRecord:
    """Research one niche with bounded retries. Hard stops (run cap, billing guard) propagate."""
    provider = provider or ClaudeHeadless(settings, session)
    job = Job(product_id=None, department=Department.RESEARCH, job_type="niche_research",
              for_status=S.RESEARCH, max_attempts=settings.max_job_attempts, status=JobStatus.RUNNING,
              started_at=utcnow(), input_json=json.dumps({"niche": niche}))
    session.add(job)
    log_event(session, "job.started", actor=ACTOR, job=job, message=f"Niche research: {niche}")
    log_event(session, "research.started", actor=ACTOR, job=job,
              message=f"Researching in depth: {niche} (web search; this takes several minutes)")
    session.commit()  # the office shows the Research Hall at work while the run is going
    problems: list[str] = []
    last_error = ""
    try:
        while job.attempts < job.max_attempts:
            job.attempts += 1
            note = ("\nYour previous answer was rejected for these reasons; fix them:\n- " + "\n- ".join(problems)
                    + "\n") if problems else ""
            try:
                completion = provider.run(job, Department.RESEARCH,
                                          PROMPT.format(niche=niche, min_sources=RESEARCH_MIN_SOURCES,
                                                        scout_notes=scout_notes(session, niche), problems=note),
                                          headless_policy.RESEARCH, response_model=ResearchResult)
            except ProviderError as error:
                last_error, problems = str(error), []
                continue
            result: ResearchResult = completion.data
            problems = research_problems(result)
            if problems:
                last_error = "; ".join(problems)
                log_event(session, "research.rejected", actor=ACTOR, job=job, message=last_error)
                continue
            record = _store(session, niche, job, result)
            job.status, job.finished_at = JobStatus.SUCCEEDED, utcnow()
            job.output_json = json.dumps({"research_id": record.id, "opportunities": len(result.opportunities),
                                          "confidence": result.confidence})
            log_event(session, "job.finished", actor=ACTOR, job=job,
                      message=f"Research record #{record.id}: {len(result.opportunities)} opportunities, "
                              f"{result.confidence} confidence")
            return record
    except BaseException as error:
        job.status, job.finished_at, job.error = JobStatus.CANCELLED, utcnow(), f"{type(error).__name__}: {error}"
        log_event(session, "job.cancelled", actor=ACTOR, job=job, message=job.error)
        raise
    job.status, job.finished_at = JobStatus.FAILED, utcnow()
    job.error = f"Failed after {job.attempts} attempt(s); last error: {last_error}"
    log_event(session, "job.failed", actor=ACTOR, job=job, message=job.error)
    raise ResearchFailed(job.error)


def scout_notes(session: Session, niche: str) -> str:
    """What the niche scout already found about this niche, as a head start for the deep research."""
    from app.models import NicheCandidate

    cand = session.scalar(select(NicheCandidate).where(NicheCandidate.niche == niche)
                          .order_by(NicheCandidate.id.desc()).limit(1))
    if cand is None:
        return ""
    data = json.loads(cand.data_json)
    lines = [f"Scout notes (a quick survey; verify and go deeper): audience: {cand.audience}",
             f"why it looked promising: {data.get('why_now', '')}",
             f"example products: {', '.join(data.get('example_products', []))}",
             f"prices seen: ${data.get('price_low', 0):.0f}-${data.get('price_high', 0):.0f}"]
    if data.get("risks"):
        lines.append(f"risks noted: {'; '.join(data['risks'])}")
    return "\n".join(lines) + "\n"


class ResearchFailed(RuntimeError):
    """Niche research ran out of attempts without a valid answer."""


def _store(session: Session, niche: str, job: Job, result: ResearchResult) -> ResearchRecord:
    findings = result.model_dump(include={"selling", "price_ranges", "common_contents", "complaints", "gaps"})
    record = ResearchRecord(niche=niche, job_id=job.id, summary=result.summary, confidence=result.confidence,
                            findings_json=json.dumps(findings), sources_json=json.dumps(
                                [s.model_dump() for s in result.sources]))
    session.add(record)
    session.flush()
    for opp in result.opportunities:
        session.add(Opportunity(
            research_id=record.id, title=opp.title.strip(), product_type=opp.product_type,
            description=opp.description,
            data_json=json.dumps(opp.model_dump(exclude={"title", "product_type", "description", "factors"})),
            factors_json=json.dumps(opp.factors.model_dump())))
    from app.models import NicheCandidate

    for cand in session.scalars(select(NicheCandidate).where(NicheCandidate.niche == niche,
                                                             NicheCandidate.status == NicheCandidate.NEW)):
        cand.status, cand.research_id = NicheCandidate.RESEARCHED, record.id
    log_event(session, "research.recorded", actor=ACTOR, job=job,
              message=f"{niche}: {len(result.opportunities)} opportunities, {len(result.sources)} sources, "
                      f"{result.confidence} confidence", data={"research_id": record.id})
    return record


def owner_sourced(product: Product) -> bool:
    """A product the owner specified by hand (config/specs/<slug>.json) instead of one Research found."""
    return (SPEC_DIR / f"{product.slug}.json").is_file()


def opportunity_for(session: Session, product: Product) -> Opportunity | None:
    return session.scalar(select(Opportunity).where(Opportunity.product_id == product.id).limit(1))


def research_gate(product: Product, job: Job) -> StageResult:
    """RESEARCH gate: the product must rest on a research record with enough evidence."""
    session = object_session(product)
    opp = opportunity_for(session, product)
    if opp is None:
        if owner_sourced(product):
            return StageResult(Outcome.PASS, S.FINANCE_REVIEW,
                               "Owner-sourced product with a hand-written spec; research done by the owner")
        return StageResult(Outcome.FAIL, S.REJECTED, "No research evidence: the product has no opportunity record")
    record = session.get(ResearchRecord, opp.research_id)
    sources = len(json.loads(record.sources_json))
    if record.confidence == "low":
        return StageResult(Outcome.FAIL, S.REJECTED, f"Research record #{record.id} has low confidence")
    if sources < MIN_SOURCES:
        return StageResult(Outcome.FAIL, S.REJECTED,
                           f"Research record #{record.id} has {sources} sources; {MIN_SOURCES} are required")
    return StageResult(Outcome.PASS, S.FINANCE_REVIEW,
                       f"Research record #{record.id}: {sources} sources, {record.confidence} confidence",
                       {"research_id": record.id, "opportunity_id": opp.id})


research_gate.provider = "deterministic"
