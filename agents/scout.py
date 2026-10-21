"""Niche scout: the Research department's wide search for niches worth researching in depth.

One headless Claude Code run (web search and public pages only, like niche research) surveys
many niches for small digital utility products and returns 8 to 15 candidates. Each candidate
carries cited evidence for five factors:

- demand:        signs people buy this kind of file (bestseller badges, review counts, "N sold",
                 search interest, forum threads asking for it)
- competition:   how crowded and strong the existing listings are (lower is better)
- price:         what buyers pay for comparable files
- buildability:  how fully a spreadsheet workbook + PDF guide (what Production can build and
                 Testing can verify today) delivers the product
- safety:        low risk: no regulated advice, trademarks, licensed content or personal data

Code validates the answer (enough distinct sources, every factor cites one, no brand names, no
niche Hive-Mint already researched or holds) and scores each candidate with fixed weights. The
shift then researches the best NEW candidate in depth (agents.research), one per shift.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from urllib.parse import urlparse

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from agents.research import Factor, Source
from app.db import utcnow
from app.lifecycle import log_event
from app.models import Job, NicheCandidate, ResearchRecord
from app.states import Department, JobStatus, ProductStatus
from config.niches import SEED_NICHES
from config.settings import Settings
from providers import headless_policy
from providers.base import ProviderError
from providers.claude_headless import ClaudeHeadless
from tools.brands import brand_hits

ACTOR = "research"
MIN_CANDIDATES, MAX_CANDIDATES = 8, 15
SCOUT_MIN_SOURCES = 10
SCOUT_REFRESH_DAYS = 7       # scout again when the last scout is older than this
CANDIDATE_MIN_SCORE = 55.0   # weaker candidates are kept for the record but never researched
# Weights sum to 1. Competition is "lower is better" and is inverted before weighting.
# Owner decision 2026-10-21: proven demand matters most; a crowded market is not a reason to skip.
WEIGHTS = {"demand": 0.45, "competition": 0.05, "price": 0.15, "buildability": 0.25, "safety": 0.10}
INVERTED = ("competition",)


class CandidateFactors(BaseModel):
    demand: Factor = Field(description="5 = strong, repeated evidence of buyers (bestsellers, many reviews/sales); "
                                       "1 = no evidence anyone buys this")
    competition: Factor = Field(description="5 = saturated with strong, well-reviewed paid listings; 1 = few or weak "
                                            "ones. Free templates elsewhere barely count")
    price: Factor = Field(description="5 = comparable files sell for $15+; 3 = about $8; 1 = under $3 or free")
    buildability: Factor = Field(description="5 = a formula-driven spreadsheet + PDF guide fully delivers it; "
                                             "3 = partly (needs design polish or printables); "
                                             "1 = needs an app, service, art, audio or per-buyer custom work")
    safety: Factor = Field(description="5 = generic, no advice or personal data; 3 = needs a disclaimer "
                                       "(money, health, legal topics); 1 = regulated, trademarked or licensed content")


class Candidate(BaseModel):
    niche: str = Field(min_length=8, max_length=200, description="Generic niche: audience + job to be done")
    audience: str = Field(min_length=5)
    example_products: list[str] = Field(min_length=2, max_length=6,
                                        description="Spreadsheet/PDF products Hive-Mint could make for it")
    price_low: float = Field(ge=0)
    price_high: float = Field(ge=0)
    why_now: str = Field(min_length=10, description="The evidence-backed reason this niche is worth a deep look")
    risks: list[str] = Field(default_factory=list)
    factors: CandidateFactors


class ScoutResult(BaseModel):
    summary: str = Field(min_length=20)
    sources: list[Source] = Field(min_length=1)
    candidates: list[Candidate] = Field(min_length=1, max_length=MAX_CANDIDATES)


PROMPT = """You are Hive-Mint's Research department, scouting for NEW niches. Hive-Mint makes small digital utility
products: spreadsheet workbooks with formulas, dashboards and charts, sold with a PDF guide as a ZIP download
on marketplaces such as Etsy and Gumroad. Code tests every formula, so rules-based, calculation-heavy tools
fit best (trackers, planners with totals, calculators, logs that summarise, budgets, inventories, schedules).

Find {min_candidates} to {max_candidates} niches where buyers demonstrably pay for this kind of file and a
spreadsheet + PDF can fully deliver the product. Look widely: small businesses and trades, side hustles,
households and personal finance, hobbies and collectors, teachers and classrooms, landlords and property,
events, health-and-fitness logging, farms and gardens, creators, nonprofits, freelancers and more.

How to look (web search and public pages only):
- marketplace search results and category pages, bestseller or "popular now" signals, review and sales counts
  visible on individual public listings, round-up articles of best-selling digital templates, and forum or
  community threads where people ask for such a tool;
- compare several listings per niche: typical prices, how many strong competitors, what reviewers praise
  or complain about.

Rules you must follow:
- Use web search and public pages only. Do not log in, do not bypass paywalls, robots.txt or anti-bot
  measures, and do not bulk-scrape marketplace listings or reviews. Reading individual public pages is fine.
- Cite evidence: every factor lists the "sources" entries (0-based indexes) that back it. Only list URLs you
  actually saw. At least {min_sources} distinct sources overall.
- Generic niches only: no brand names, trademarks or vendor-specific products in niche names.
- Skip these niches; Hive-Mint already covers them:
{exclude}
- Score honestly with the rubric in the schema; do not inflate. A niche with weak buying evidence is still
  worth listing with a low demand score, so the comparison stays fair.
{problems}
Answer with JSON matching the provided schema."""


def candidate_score(factors: dict[str, int]) -> float:
    total = 0.0
    for name, weight in WEIGHTS.items():
        value = 6 - factors[name] if name in INVERTED else factors[name]
        total += weight * (value - 1) / 4 * 100
    return round(total, 1)


def _key(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def known_niches(session: Session) -> list[str]:
    """Niches Hive-Mint has already researched, scouted or seeded; the scout must look elsewhere."""
    researched = session.scalars(select(ResearchRecord.niche)).all()
    scouted = session.scalars(select(NicheCandidate.niche)).all()
    seen, out = set(), []
    for niche in [*SEED_NICHES, *researched, *scouted]:
        if _key(niche) not in seen:
            seen.add(_key(niche))
            out.append(niche)
    return out


def scout_problems(result: ScoutResult, known: list[str]) -> list[str]:
    problems = []
    urls = {s.url.strip().rstrip("/").lower() for s in result.sources
            if urlparse(s.url).scheme in ("http", "https") and urlparse(s.url).netloc}
    if len(urls) < SCOUT_MIN_SOURCES:
        problems.append(f"only {len(urls)} distinct http(s) source URLs; at least {SCOUT_MIN_SOURCES} are required")
    if len(result.candidates) < MIN_CANDIDATES:
        problems.append(f"only {len(result.candidates)} candidates; give {MIN_CANDIDATES} to {MAX_CANDIDATES}")
    known_keys = {_key(k) for k in known}
    seen: set[str] = set()
    count = len(result.sources)
    for cand in result.candidates:
        if hits := brand_hits(cand.niche):
            problems.append(f"niche '{cand.niche}' uses brand names: {', '.join(hits)}")
        key = _key(cand.niche)
        if key in known_keys:
            problems.append(f"niche '{cand.niche}' is already covered; pick a different one")
        if key in seen:
            problems.append(f"niche '{cand.niche}' is listed twice")
        seen.add(key)
        if cand.price_high < cand.price_low:
            problems.append(f"niche '{cand.niche}' has price_high < price_low")
        for name, factor in cand.factors:
            if not factor.sources:
                problems.append(f"niche '{cand.niche}': factor {name} cites no source")
            elif any(i >= count or i < 0 for i in factor.sources):
                problems.append(f"niche '{cand.niche}': factor {name} cites a source that does not exist")
    return problems


class ScoutFailed(RuntimeError):
    """The scout ran out of attempts without a valid answer."""


def run_scout(session: Session, settings: Settings, *, provider: ClaudeHeadless | None = None) -> list[NicheCandidate]:
    """Scout for new niches with bounded retries. Hard stops (run cap, billing guard) propagate."""
    provider = provider or ClaudeHeadless(settings, session)
    job = Job(product_id=None, department=Department.RESEARCH, job_type="niche_scout",
              for_status=ProductStatus.RESEARCH, max_attempts=settings.max_job_attempts, status=JobStatus.RUNNING,
              started_at=utcnow(), input_json="{}")
    session.add(job)
    log_event(session, "job.started", actor=ACTOR, job=job, message="Niche scout")
    log_event(session, "research.started", actor=ACTOR, job=job,
              message="Scouting for new niches (web search; this takes several minutes)")
    session.commit()  # the office shows the Research Hall at work while the run is going
    known = known_niches(session)
    exclude = "\n".join(f"  - {n}" for n in known) or "  (none yet)"
    problems: list[str] = []
    last_error = ""
    try:
        while job.attempts < job.max_attempts:
            job.attempts += 1
            note = ("\nYour previous answer was rejected for these reasons; fix them:\n- " + "\n- ".join(problems)
                    + "\n") if problems else ""
            prompt = PROMPT.format(min_candidates=MIN_CANDIDATES, max_candidates=MAX_CANDIDATES,
                                   min_sources=SCOUT_MIN_SOURCES, exclude=exclude, problems=note)
            try:
                completion = provider.run(job, Department.RESEARCH, prompt, headless_policy.RESEARCH,
                                          response_model=ScoutResult)
            except ProviderError as error:
                last_error, problems = str(error), []
                continue
            result: ScoutResult = completion.data
            problems = scout_problems(result, known)
            if problems:
                last_error = "; ".join(problems)
                log_event(session, "scout.rejected", actor=ACTOR, job=job, message=last_error)
                continue
            rows = _store(session, job, result)
            job.status, job.finished_at = JobStatus.SUCCEEDED, utcnow()
            job.output_json = json.dumps({"candidates": [r.id for r in rows]})
            log_event(session, "job.finished", actor=ACTOR, job=job, message=f"Niche scout: {len(rows)} candidates")
            return rows
    except BaseException as error:
        job.status, job.finished_at, job.error = JobStatus.CANCELLED, utcnow(), f"{type(error).__name__}: {error}"
        log_event(session, "job.cancelled", actor=ACTOR, job=job, message=job.error)
        raise
    job.status, job.finished_at = JobStatus.FAILED, utcnow()
    job.error = f"Failed after {job.attempts} attempt(s); last error: {last_error}"
    log_event(session, "job.failed", actor=ACTOR, job=job, message=job.error)
    raise ScoutFailed(job.error)


def _store(session: Session, job: Job, result: ScoutResult) -> list[NicheCandidate]:
    sources = [s.model_dump() for s in result.sources]
    rows = []
    for cand in result.candidates:
        factors = {name: f.score for name, f in cand.factors}
        cited = sorted({i for _, f in cand.factors for i in f.sources})
        row = NicheCandidate(
            job_id=job.id, niche=cand.niche.strip(), audience=cand.audience,
            data_json=json.dumps(cand.model_dump(include={"example_products", "price_low", "price_high",
                                                          "why_now", "risks"})),
            factors_json=json.dumps(cand.factors.model_dump()),
            sources_json=json.dumps([sources[i] for i in cited]),
            score=candidate_score(factors),
            status=NicheCandidate.NEW)
        session.add(row)
        rows.append(row)
    session.flush()
    best = sorted(rows, key=lambda r: -r.score)[:3]
    log_event(session, "scout.recorded", actor=ACTOR, job=job,
              message=f"{len(rows)} niche candidates from {len(sources)} sources; best: "
                      + "; ".join(f"{r.niche} ({r.score:.0f})" for r in best))
    return rows


def next_candidate(session: Session) -> NicheCandidate | None:
    """The best NEW candidate worth a deep look, if any."""
    return session.scalar(select(NicheCandidate)
                          .where(NicheCandidate.status == NicheCandidate.NEW,
                                 NicheCandidate.score >= CANDIDATE_MIN_SCORE)
                          .order_by(NicheCandidate.score.desc(), NicheCandidate.id).limit(1))


def scout_due(session: Session, now) -> bool:
    last = session.scalar(select(Job.created_at).where(Job.job_type == "niche_scout",
                                                       Job.status == JobStatus.SUCCEEDED)
                          .order_by(Job.created_at.desc()).limit(1))
    return last is None or last < now - timedelta(days=SCOUT_REFRESH_DAYS)
