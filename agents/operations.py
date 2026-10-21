"""Operations Manager: deterministic routing from product state to department.

No LLM is involved. The manager reads the database, queues the next job for the
department that owns the product's current gate, runs queued jobs with bounded retries,
and records every step. Jobs owned by an external handler (the Red Team, done in a
human-triggered Claude Code session) stay queued until their result is imported.
"""

from __future__ import annotations

import json
import time

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agents.compliance import compliance_gate
from agents.departments import STUB_HANDLERS, Handler, StageResult
from agents.finance import finance_gate
from agents.production import Production
from agents.qa import QA
from agents.red_team import RedTeam
from agents.research import research_gate
from agents.seo import SEO
from agents.spec_writer import ProductDesign
from agents.rework import active_brief, brief_payload
from agents.testing import Testing
from app.db import utcnow
from app.clock import day_start
from app.lifecycle import LimitReached, log_event, transition
from app.models import AgentRun, Build, Event, Job, Product
from app.states import OPEN_JOB_STATUSES, Department, JobStatus, ProductStatus
from config.settings import Settings
from providers.claude_headless import HeadlessRefused
from tools.disk import check_disk

# Raised by a handler when a hard limit or safety guard stops work (daily Claude run cap, billing
# guard). Not a failure of the job: the attempt is given back, the job goes back to the queue and
# the error propagates so the shift stops cleanly. The job runs again next shift.
HARD_STOPS = (LimitReached, HeadlessRefused)

S = ProductStatus
D = Department

# None = no automated owner: a human decides, or the stage is not built yet.
# Analytics and Customer Service get routes once products can be published.
ROUTING: dict[ProductStatus, Department | None] = {
    S.IDEA: D.OPERATIONS,
    S.RESEARCH: D.RESEARCH,
    S.FINANCE_REVIEW: D.FINANCE,
    S.SPECIFICATION: D.PRODUCT_DESIGN,
    S.PRODUCTION: D.PRODUCTION,
    S.TESTING: D.TESTING,
    S.QA: D.QA,
    S.RED_TEAM: D.RED_TEAM,
    S.COMPLIANCE: D.COMPLIANCE,
    S.SEO: D.SEO,
    S.REWORK: D.PRODUCT_DESIGN,
    S.HUMAN_REVIEW: None,
    S.READY_TO_PUBLISH: None,
    S.PUBLISHED: None,
    S.PAUSED: None,
    S.ERROR: None,
    S.REJECTED: None,
    S.RETIRED: None,
}

JOB_TYPES: dict[ProductStatus, str] = {
    S.IDEA: "intake",
    S.RESEARCH: "market_research",
    S.FINANCE_REVIEW: "finance_review",
    S.SPECIFICATION: "write_specification",
    S.PRODUCTION: "build_product",
    S.TESTING: "functional_tests",
    S.QA: "quality_review",
    S.RED_TEAM: "red_team_review",
    S.COMPLIANCE: "compliance_review",
    S.SEO: "seo_metadata",
    S.REWORK: "rework_plan",
}

ACTOR = "operations"


def default_handlers(settings: Settings) -> dict[Department, Handler]:
    """Every pipeline department is real. Red Team is external to tick(): an owner's session or the shift
    (agents.red_team.run_headless) does it."""
    return {**STUB_HANDLERS, D.RESEARCH: research_gate, D.FINANCE: finance_gate(settings),
            D.PRODUCT_DESIGN: ProductDesign(settings), D.PRODUCTION: Production(settings),
            D.TESTING: Testing(settings), D.QA: QA(settings), D.RED_TEAM: RedTeam(settings),
            D.COMPLIANCE: compliance_gate(settings), D.SEO: SEO(settings)}


class OperationsManager:
    def __init__(self, session: Session, settings: Settings, handlers: dict[Department, Handler] | None = None):
        self.session = session
        self.settings = settings
        self.handlers = default_handlers(settings) if handlers is None else handlers

    @staticmethod
    def route(product: Product) -> Department | None:
        return ROUTING[product.status]

    def open_job(self, product: Product) -> Job | None:
        return self.session.scalar(
            select(Job).where(Job.product_id == product.id, Job.status.in_(OPEN_JOB_STATUSES)).limit(1)
        )

    def builds_today(self) -> int:
        return self.session.scalar(
            select(func.count(Job.id)).where(Job.department == D.PRODUCTION, Job.created_at >= day_start(self.settings.timezone))
        )

    def has_build(self, product: Product) -> bool:
        return self.session.scalar(select(func.count(Build.id)).where(Build.product_id == product.id)) > 0

    def new_products_built_today(self) -> int:
        """Production jobs queued today for a product's first build (rebuilds don't count)."""
        jobs = self.session.scalars(select(Job.input_json).where(
            Job.department == D.PRODUCTION, Job.created_at >= day_start(self.settings.timezone))).all()
        return sum(1 for raw in jobs if json.loads(raw or "{}").get("first_build"))

    def next_job(self, product: Product) -> Job | None:
        """Queue the job for the product's current gate. Returns None if nothing was queued."""
        department = self.route(product)
        open_job = self.open_job(product)
        if open_job is not None and open_job.for_status != product.status:
            # e.g. a queued Red Team job when a person sent the product to REWORK; tick() never
            # runs external jobs, so without this the product would wait on it forever.
            self._cancel_stale(open_job)
            open_job = None
        if department is None or open_job is not None:
            return None
        first_build = department == D.PRODUCTION and not self.has_build(product)
        if department == D.PRODUCTION and self.builds_today() >= self.settings.max_builds_per_day:
            self._defer(product, f"Daily build limit reached ({self.settings.max_builds_per_day})")
            return None
        if first_build and self.new_products_built_today() >= self.settings.max_products_built_per_day:
            self._defer(product, f"Daily new-product build limit reached ({self.settings.max_products_built_per_day})")
            return None
        job_input = {"product_id": product.id, "status": product.status.value}
        if first_build:
            job_input["first_build"] = True
        brief = active_brief(self.session, product) if department == D.PRODUCTION else None
        if brief is not None:
            job_input["rework_brief"] = brief_payload(brief)
        job = Job(
            product_id=product.id,
            department=department,
            job_type=JOB_TYPES[product.status] if brief is None else "rework_build",
            for_status=product.status,
            max_attempts=self.settings.max_job_attempts,
            input_json=json.dumps(job_input),
        )
        self.session.add(job)
        log_event(self.session, "job.created", actor=ACTOR, product=product, job=job,
                  message=f"Queued {job.job_type} for {department.value}"
                          + (f" with rework brief #{brief.id}" if brief else ""),
                  data={"department": department.value, "job_type": job.job_type,
                        "rework_brief_id": brief.id if brief else None})
        if brief is not None:
            brief.status, brief.production_job_id = brief.IN_PRODUCTION, job.id
        return job

    def _defer(self, product: Product, reason: str) -> None:
        # Log a deferral once per product per day, not on every tick.
        already = self.session.scalar(
            select(func.count(Event.id)).where(Event.product_id == product.id, Event.event_type == "job.deferred",
                                               Event.created_at >= day_start(self.settings.timezone))
        )
        if not already:
            log_event(self.session, "job.deferred", actor=ACTOR, product=product, message=reason)

    def run_job(self, job: Job) -> Job:
        """Run a queued job with bounded retries; move the product according to the result."""
        if job.status != JobStatus.QUEUED:
            raise ValueError(f"Job {job.id} is {job.status}, not QUEUED")
        product = job.product
        if product.status != job.for_status:
            return self._cancel_stale(job)

        if job.department == D.PRODUCTION and (limit := check_disk(self.settings)):
            job.status, job.error = JobStatus.PAUSED, limit
            log_event(self.session, "job.paused", actor=ACTOR, job=job, message=limit)
            return job

        handler = self.handlers.get(job.department)
        job.status, job.started_at = JobStatus.RUNNING, utcnow()
        log_event(self.session, "job.started", actor=ACTOR, job=job, message=f"{job.department.value}: {job.job_type}")

        while job.attempts < job.max_attempts:
            job.attempts += 1
            run = AgentRun(job_id=job.id, department=job.department, provider=getattr(handler, "provider", "stub"),
                           model="none", tier=0,
                           cost_usd_micros=0, status=JobStatus.RUNNING)
            self.session.add(run)
            # Commit before the (possibly long) handler: the office sees the job RUNNING, and the database
            # isn't held locked for the minutes a headless Claude run can take.
            self.session.commit()
            clock = time.perf_counter()
            try:
                if handler is None:
                    raise LookupError(f"No handler registered for {job.department.value}")
                result = handler(product, job)
                transition(self.session, self.settings, product, result.next_status,
                           actor=job.department.value.lower(), reason=result.summary, job=job, run=run)
            except HARD_STOPS as error:
                job.attempts -= 1
                job.status, job.started_at = JobStatus.QUEUED, None
                self._finish_run(run, clock, JobStatus.CANCELLED, error=f"{type(error).__name__}: {error}")
                log_event(self.session, "job.requeued", actor=ACTOR, job=job, message=str(error))
                raise
            except Exception as error:  # a failed attempt is recorded, then retried or escalated
                self._finish_run(run, clock, JobStatus.FAILED, error=f"{type(error).__name__}: {error}")
                continue
            return self._succeed(job, run, clock, result)

        job.status, job.finished_at = JobStatus.FAILED, utcnow()
        job.error = f"Failed after {job.attempts} attempt(s); last error: {run.error}"
        log_event(self.session, "job.failed", actor=ACTOR, job=job, message=job.error)
        transition(self.session, self.settings, product, S.ERROR, actor=ACTOR, reason=job.error, job=job)
        return job

    def _cancel_stale(self, job: Job) -> Job:
        job.status, job.finished_at = JobStatus.CANCELLED, utcnow()
        job.error = f"Stale: product moved from {job.for_status} to {job.product.status}"
        log_event(self.session, "job.cancelled", actor=ACTOR, job=job, message=job.error)
        return job

    def complete_external_job(self, job: Job, result: StageResult, *, provider: str, model: str, tier: int) -> Job:
        """Finish a job whose work happened outside tick(), e.g. a Claude Code Red Team session.

        Records one AgentRun for that work and moves the product exactly as run_job would. Any
        error propagates so the caller can roll back; nothing is retried automatically.
        """
        if job.status != JobStatus.QUEUED:
            raise ValueError(f"Job {job.id} is {job.status}, not QUEUED")
        if job.product.status != job.for_status:
            raise ValueError(f"Job {job.id} is stale: product is {job.product.status}, job was for {job.for_status}")
        job.status, job.started_at = JobStatus.RUNNING, utcnow()
        job.attempts += 1
        log_event(self.session, "job.started", actor=ACTOR, job=job,
                  message=f"{job.department.value}: {job.job_type} (external: {provider})")
        run = AgentRun(job_id=job.id, department=job.department, provider=provider, model=model, tier=tier,
                       cost_usd_micros=0, status=JobStatus.RUNNING)
        self.session.add(run)
        self.session.flush()
        clock = time.perf_counter()
        transition(self.session, self.settings, job.product, result.next_status,
                   actor=job.department.value.lower(), reason=result.summary, job=job, run=run)
        return self._succeed(job, run, clock, result)

    def _succeed(self, job: Job, run: AgentRun, clock: float, result: StageResult) -> Job:
        self._finish_run(run, clock, JobStatus.SUCCEEDED, summary=result.summary)
        job.status, job.finished_at = JobStatus.SUCCEEDED, utcnow()
        job.output_json = json.dumps({"outcome": result.outcome.value, "next_status": result.next_status.value,
                                      "summary": result.summary, **result.data}, default=str)
        log_event(self.session, "job.finished", actor=ACTOR, job=job, message=result.summary,
                  data={"outcome": result.outcome.value, "attempts": job.attempts})
        return job

    def is_external(self, job: Job) -> bool:
        """Jobs done by a human-triggered session (Red Team via Claude Code) wait for an import."""
        return getattr(self.handlers.get(job.department), "external", False)

    def _finish_run(self, run: AgentRun, clock: float, status: JobStatus, *, summary: str = "",
                    error: str | None = None) -> None:
        run.status, run.result_summary, run.error = status, summary, error
        run.finished_at = utcnow()
        run.duration_ms = int((time.perf_counter() - clock) * 1000)
        log_event(self.session, "agent_run.finished", actor=ACTOR, run=run, message=summary or error or "",
                  data={"provider": run.provider, "model": run.model, "tier": run.tier,
                        "cost_usd_micros": run.cost_usd_micros, "status": status.value,
                        "attempt": run.job.attempts, "max_attempts": run.job.max_attempts})

    def resume_paused_jobs(self) -> None:
        """Requeue jobs paused by the disk guard once it clears. Checked once per tick."""
        paused = self.session.scalars(select(Job).where(Job.status == JobStatus.PAUSED)).all()
        disk_paused = [j for j in paused if (j.error or "").startswith("DISK_LIMIT")]
        if disk_paused and check_disk(self.settings) is None:
            for job in disk_paused:
                job.status, job.error = JobStatus.QUEUED, None
                log_event(self.session, "job.resumed", actor=ACTOR, job=job, message="Disk limits clear; requeued")

    def tick(self, statuses: set[ProductStatus] | None = None) -> int:
        """Queue the next job for every routable product, then run queued jobs. Returns jobs run.

        statuses limits the tick to products at those gates (the shift runs one gate at a time). A
        job paused by a guard is not counted as run, so a tick loop stops while it waits. A hard stop
        (HARD_STOPS) is committed, then re-raised to the caller.
        """
        self.resume_paused_jobs()
        routable = [status for status, dept in ROUTING.items()
                    if dept is not None and (statuses is None or status in statuses)]
        for product in self.session.scalars(select(Product).where(Product.status.in_(routable)).order_by(Product.id)):
            self.next_job(product)
        self.session.commit()
        ran = 0
        queued = select(Job).where(Job.status == JobStatus.QUEUED, Job.product_id.is_not(None)).order_by(Job.id)
        for job in self.session.scalars(queued).all():
            if self.is_external(job) or (statuses is not None and job.for_status not in statuses):
                continue  # external jobs wait for their session, e.g. python -m app.cli review-import
            try:
                self.run_job(job)
            finally:
                self.session.commit()
            ran += job.status not in (JobStatus.PAUSED, JobStatus.QUEUED)
        return ran
