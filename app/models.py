"""SQLAlchemy models: the database is the source of truth for every decision and cost."""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import Boolean, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, UTCDateTime, utcnow
from app.states import Department, JobStatus, ProductStatus


def _enum(cls):
    # Store enum values as plain strings so the database stays readable.
    return Enum(cls, native_enum=False, length=32, validate_strings=True)


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    product_type: Mapped[str] = mapped_column(String(50))
    description: Mapped[str] = mapped_column(Text, default="")
    # Change status only through app.lifecycle.transition().
    status: Mapped[ProductStatus] = mapped_column(_enum(ProductStatus), default=ProductStatus.IDEA, index=True)
    previous_status: Mapped[ProductStatus | None] = mapped_column(_enum(ProductStatus))
    paused_from: Mapped[ProductStatus | None] = mapped_column(_enum(ProductStatus))
    status_reason: Mapped[str] = mapped_column(Text, default="")
    rework_count: Mapped[int] = mapped_column(Integer, default=0)
    # Extra rework cycles the owner granted (python -m app.cli approve-rework) on top of MAX_REWORK_CYCLES.
    extra_rework_cycles: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    approved_by: Mapped[str | None] = mapped_column(String(100))
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    # No delete cascades: products with an audit trail are retired, never deleted.
    jobs: Mapped[list[Job]] = relationship(back_populates="product")
    events: Mapped[list[Event]] = relationship(back_populates="product")

    def __repr__(self) -> str:
        return f"<Product {self.id} {self.slug} {self.status}>"


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Null only for work that comes before any product exists, e.g. niche research.
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"), index=True)
    department: Mapped[Department] = mapped_column(_enum(Department), index=True)
    job_type: Mapped[str] = mapped_column(String(80))
    # Product state the job was created for; a job is stale if the product has moved on.
    for_status: Mapped[ProductStatus] = mapped_column(_enum(ProductStatus))
    status: Mapped[JobStatus] = mapped_column(_enum(JobStatus), default=JobStatus.QUEUED, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    input_json: Mapped[str] = mapped_column(Text, default="{}")
    output_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    product: Mapped[Product | None] = relationship(back_populates="jobs")
    runs: Mapped[list[AgentRun]] = relationship(back_populates="job")

    def __repr__(self) -> str:
        return f"<Job {self.id} {self.department} {self.status}>"


class AgentRun(Base):
    """One attempt by a provider (stub, local model, Claude Code session, API) at a job."""

    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    department: Mapped[Department] = mapped_column(_enum(Department))
    provider: Mapped[str] = mapped_column(String(50))
    model: Mapped[str] = mapped_column(String(100))
    tier: Mapped[int] = mapped_column(Integer, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd_micros: Mapped[int] = mapped_column(Integer, default=0)  # 1 USD = 1_000_000
    status: Mapped[JobStatus] = mapped_column(_enum(JobStatus), default=JobStatus.RUNNING)
    result_summary: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    job: Mapped[Job] = relationship(back_populates="runs")


class Event(Base):
    """Append-only audit log. Never update or delete rows."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"), index=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), index=True)
    agent_run_id: Mapped[int | None] = mapped_column(ForeignKey("agent_runs.id"))
    event_type: Mapped[str] = mapped_column(String(60), index=True)
    actor: Mapped[str] = mapped_column(String(100))
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str | None] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text, default="")
    data_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)

    product: Mapped[Product | None] = relationship(back_populates="events")


class Build(Base):
    """One Production output: a versioned folder of deliverables plus the ZIP a buyer downloads."""

    __tablename__ = "builds"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    spec_slug: Mapped[str] = mapped_column(String(120))
    path: Mapped[str] = mapped_column(Text)
    zip_path: Mapped[str] = mapped_column(Text)
    zip_sha256: Mapped[str] = mapped_column(String(64))
    manifest_json: Mapped[str] = mapped_column(Text, default="[]")  # [{name, bytes, sha256}]
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)

    def __repr__(self) -> str:
        return f"<Build {self.id} product={self.product_id} v{self.version}>"


class TestResult(Base):
    """One deterministic Testing check against one build. Never updated; a re-test adds rows."""

    __test__ = False  # not a pytest test class
    __tablename__ = "test_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    agent_run_id: Mapped[int | None] = mapped_column(ForeignKey("agent_runs.id"))
    build_id: Mapped[int | None] = mapped_column(ForeignKey("builds.id"), index=True)
    check: Mapped[str] = mapped_column(String(80))
    target: Mapped[str] = mapped_column(String(200), default="")
    passed: Mapped[bool] = mapped_column(Boolean)
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Review(Base):
    """A QA report or Red Team verdict on one build. Never updated; a re-review adds a row."""

    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), index=True)
    build_id: Mapped[int | None] = mapped_column(ForeignKey("builds.id"), index=True)
    department: Mapped[Department] = mapped_column(_enum(Department))
    reviewer: Mapped[str] = mapped_column(String(100))  # provider or person, e.g. "qa+ollama", "claude-code"
    model: Mapped[str] = mapped_column(String(100), default="")
    decision: Mapped[str] = mapped_column(String(16))  # PASS, REWORK or REJECT
    score: Mapped[int | None] = mapped_column(Integer)
    summary: Mapped[str] = mapped_column(Text, default="")
    findings_json: Mapped[str] = mapped_column(Text, default="[]")
    report_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


class ReworkBrief(Base):
    """Structured changes a product needs before it can pass the gate that sent it back.

    Each required change has a stable id. A spec lists the ids its builder implements under
    "resolves"; Product Design only routes a product to Production once every id is covered.
    """

    __tablename__ = "rework_briefs"

    OPEN, IN_PRODUCTION, ADDRESSED = "OPEN", "IN_PRODUCTION", "ADDRESSED"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    source: Mapped[str] = mapped_column(String(32))  # QA, RED_TEAM or HUMAN
    actor: Mapped[str] = mapped_column(String(100))
    review_id: Mapped[int | None] = mapped_column(ForeignKey("reviews.id"))
    source_job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"))
    build_id: Mapped[int | None] = mapped_column(ForeignKey("builds.id"))  # the build that was reviewed
    summary: Mapped[str] = mapped_column(Text, default="")
    required_changes_json: Mapped[str] = mapped_column(Text, default="[]")  # [{id, change, reason, acceptance}]
    findings_json: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String(16), default="OPEN", index=True)
    production_job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"))
    addressed_build_id: Mapped[int | None] = mapped_column(ForeignKey("builds.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    addressed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    @property
    def change_ids(self) -> list[str]:
        return [c["id"] for c in json.loads(self.required_changes_json)]


class ResearchRecord(Base):
    """Evidence gathered about one niche: what sells, prices, contents, complaints, gaps, with sources."""

    __tablename__ = "research_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    niche: Mapped[str] = mapped_column(String(300), index=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    findings_json: Mapped[str] = mapped_column(Text, default="{}")
    sources_json: Mapped[str] = mapped_column(Text, default="[]")  # [{url, title, supports}]
    confidence: Mapped[str] = mapped_column(String(16))  # low, medium or high
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


class Opportunity(Base):
    """One candidate product found by Research and scored by Finance."""

    __tablename__ = "opportunities"

    PENDING, SELECTED, BELOW_THRESHOLD, NOT_TOP, LIMIT = "PENDING", "SELECTED", "BELOW_THRESHOLD", "NOT_TOP", "LIMIT"

    id: Mapped[int] = mapped_column(primary_key=True)
    research_id: Mapped[int] = mapped_column(ForeignKey("research_records.id"), index=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    product_type: Mapped[str] = mapped_column(String(50))
    description: Mapped[str] = mapped_column(Text, default="")
    data_json: Mapped[str] = mapped_column(Text, default="{}")  # price range, contents, differentiation...
    factors_json: Mapped[str] = mapped_column(Text, default="{}")  # factor -> {score, reason, sources}
    score: Mapped[float | None] = mapped_column()
    rank: Mapped[int | None] = mapped_column(Integer)
    decision: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    reasons_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    scored_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class SpecVersion(Base):
    """One version of a product's spec. Never updated; a revision adds a row with a diff."""

    __tablename__ = "spec_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    spec_json: Mapped[str] = mapped_column(Text)
    diff_text: Mapped[str] = mapped_column(Text, default="")  # unified diff against the previous version
    author: Mapped[str] = mapped_column(String(50))  # file-import, claude-headless or human
    reason: Mapped[str] = mapped_column(Text, default="")
    brief_id: Mapped[int | None] = mapped_column(ForeignKey("rework_briefs.id"))
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)

    @property
    def spec(self) -> dict:
        return json.loads(self.spec_json)


class Shift(Base):
    """One unattended run of the shift orchestrator (python -m app.cli shift)."""

    __tablename__ = "shifts"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    stop_reason: Mapped[str] = mapped_column(Text, default="")
    steps_json: Mapped[str] = mapped_column(Text, default="[]")  # [{step, ran, summary}]
    claude_runs: Mapped[int] = mapped_column(Integer, default=0)
    errors_json: Mapped[str] = mapped_column(Text, default="[]")
