"""Operations Manager: deterministic routing from product state to department.

No LLM is involved. The manager reads the database, queues the next job for the
department that owns the product's current gate, runs queued jobs with bounded retries,
and records every step.
"""

from __future__ import annotations

import json
import time

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agents.departments import HANDLERS, Handler
from app.db import utcnow
from app.lifecycle import log_event, start_of_today, transition
from app.models import AgentRun, Event, Job, Product
from app.states import OPEN_JOB_STATUSES, Department, JobStatus, ProductStatus
from config.settings import Settings

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


class OperationsManager:
    def __init__(self, session: Session, settings: Settings, handlers: dict[Department, Handler] | None = None):
        self.session = session
        self.settings = settings
        self.handlers = HANDLERS if handlers is None else handlers

    @staticmethod
    def route(product: Product) -> Department | None:
        return ROUTING[product.status]

    def open_job(self, product: Product) -> Job | None:
        return self.session.scalar(
            select(Job).where(Job.product_id == product.id, Job.status.in_(OPEN_JOB_STATUSES)).limit(1)
        )

    def builds_today(self) -> int:
        return self.session.scalar(
            select(func.count(Job.id)).where(Job.department == D.PRODUCTION, Job.created_at >= start_of_today())
        )

    def next_job(self, product: Product) -> Job | None:
        """Queue the job for the product's current gate. Returns None if nothing was queued."""
        department = self.route(product)
        if department is None or self.open_job(product) is not None:
            return None
        if department == D.PRODUCTION and self.builds_today() >= self.settings.max_builds_per_day:
            self._defer(product, f"Daily build limit reached ({self.settings.max_builds_per_day})")
            return None
        job = Job(
            product_id=product.id,
            department=department,
            job_type=JOB_TYPES[product.status],
            for_status=product.status,
            max_attempts=self.settings.max_job_attempts,
            input_json=json.dumps({"product_id": product.id, "status": product.status.value}),
        )
        self.session.add(job)
        log_event(self.session, "job.created", actor=ACTOR, product=product, job=job,
                  message=f"Queued {job.job_type} for {department.value}",
                  data={"department": department.value, "job_type": job.job_type})
        return job

    def _defer(self, product: Product, reason: str) -> None:
        # Log a deferral once per product per day, not on every tick.
        already = self.session.scalar(
            select(func.count(Event.id)).where(Event.product_id == product.id, Event.event_type == "job.deferred",
                                               Event.created_at >= start_of_today())
        )
        if not already:
            log_event(self.session, "job.deferred", actor=ACTOR, product=product, message=reason)

    def run_job(self, job: Job) -> Job:
        """Run a queued job with bounded retries; move the product according to the result."""
        if job.status != JobStatus.QUEUED:
            raise ValueError(f"Job {job.id} is {job.status}, not QUEUED")
        product = job.product
        if product.status != job.for_status:
            job.status, job.finished_at = JobStatus.CANCELLED, utcnow()
            job.error = f"Stale: product moved from {job.for_status} to {product.status}"
            log_event(self.session, "job.cancelled", actor=ACTOR, job=job, message=job.error)
            return job

        handler = self.handlers.get(job.department)
        job.status, job.started_at = JobStatus.RUNNING, utcnow()
        log_event(self.session, "job.started", actor=ACTOR, job=job, message=f"{job.department.value}: {job.job_type}")

        while job.attempts < job.max_attempts:
            job.attempts += 1
            run = AgentRun(job_id=job.id, department=job.department, provider="stub", model="none", tier=0,
                           cost_usd_micros=0, status=JobStatus.RUNNING)
            self.session.add(run)
            self.session.flush()
            clock = time.perf_counter()
            try:
                if handler is None:
                    raise LookupError(f"No handler registered for {job.department.value}")
                result = handler(product, job)
                transition(self.session, self.settings, product, result.next_status,
                           actor=job.department.value.lower(), reason=result.summary, job=job, run=run)
            except Exception as error:  # a failed attempt is recorded, then retried or escalated
                self._finish_run(run, clock, JobStatus.FAILED, error=f"{type(error).__name__}: {error}")
                continue
            self._finish_run(run, clock, JobStatus.SUCCEEDED, summary=result.summary)
            job.status, job.finished_at = JobStatus.SUCCEEDED, utcnow()
            job.output_json = json.dumps({"outcome": result.outcome.value, "next_status": result.next_status.value,
                                          "summary": result.summary, **result.data}, default=str)
            log_event(self.session, "job.finished", actor=ACTOR, job=job, message=result.summary,
                      data={"outcome": result.outcome.value, "attempts": job.attempts})
            return job

        job.status, job.finished_at = JobStatus.FAILED, utcnow()
        job.error = f"Failed after {job.attempts} attempt(s); last error: {run.error}"
        log_event(self.session, "job.failed", actor=ACTOR, job=job, message=job.error)
        transition(self.session, self.settings, product, S.ERROR, actor=ACTOR, reason=job.error, job=job)
        return job

    def _finish_run(self, run: AgentRun, clock: float, status: JobStatus, *, summary: str = "",
                    error: str | None = None) -> None:
        run.status, run.result_summary, run.error = status, summary, error
        run.finished_at = utcnow()
        run.duration_ms = int((time.perf_counter() - clock) * 1000)
        log_event(self.session, "agent_run.finished", actor=ACTOR, run=run, message=summary or error or "",
                  data={"provider": run.provider, "model": run.model, "tier": run.tier,
                        "cost_usd_micros": run.cost_usd_micros, "status": status.value,
                        "attempt": run.job.attempts, "max_attempts": run.job.max_attempts})

    def tick(self) -> int:
        """Queue the next job for every routable product, then run queued jobs. Returns jobs run."""
        routable = [status for status, dept in ROUTING.items() if dept is not None]
        for product in self.session.scalars(select(Product).where(Product.status.in_(routable)).order_by(Product.id)):
            self.next_job(product)
        self.session.commit()
        ran = 0
        for job in self.session.scalars(select(Job).where(Job.status == JobStatus.QUEUED).order_by(Job.id)).all():
            self.run_job(job)
            self.session.commit()
            ran += 1
        return ran
