"""Rework briefs: the structured hand-off from a failed gate back to Production.

QA, Red Team and a human reviewer all create briefs here. A brief lists required changes,
each with a stable id. Product Design sends the product to Production only when the spec
declares (under "resolves") that its builder implements every id; otherwise the product
pauses for a human or Claude Code session to update the spec and builder.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.lifecycle import log_event
from app.models import Build, Job, Product, Review, ReworkBrief

CHANGE_ID = r"^[a-z0-9][a-z0-9_.-]{1,62}$"
ACTIVE_BRIEF = (ReworkBrief.OPEN, ReworkBrief.IN_PRODUCTION)
SUPERSEDED = "SUPERSEDED"


class RequiredChange(BaseModel):
    id: str = Field(pattern=CHANGE_ID, description="Stable lowercase id, e.g. merge_workbooks")
    change: str = Field(min_length=5, description="What must change, specifically")
    reason: str = Field(min_length=5, description="Why: the buyer problem it fixes")
    acceptance: str = Field(default="", description="How to tell it is done")


class BriefFile(BaseModel):
    """A rework brief written by a person (python -m app.cli rework <id> <file.json>)."""

    summary: str = Field(min_length=5)
    required_changes: list[RequiredChange] = Field(min_length=1)
    findings: list[dict] = Field(default_factory=list)


def create_brief(session: Session, product: Product, *, source: str, actor: str, summary: str,
                 required_changes: list[dict], findings: list[dict] | None = None, review: Review | None = None,
                 job: Job | None = None, build: Build | None = None) -> ReworkBrief:
    if not required_changes:
        raise ValueError("A rework brief needs at least one required change")
    ids = [c["id"] for c in required_changes]
    if len(ids) != len(set(ids)):
        raise ValueError(f"Duplicate change ids in rework brief: {ids}")
    for older in session.scalars(select(ReworkBrief).where(ReworkBrief.product_id == product.id,
                                                           ReworkBrief.status.in_(ACTIVE_BRIEF))):
        older.status = SUPERSEDED
    session.flush()
    brief = ReworkBrief(product_id=product.id, source=source, actor=actor, summary=summary,
                        review_id=review.id if review else None, source_job_id=job.id if job else None,
                        build_id=build.id if build else None, status=ReworkBrief.OPEN,
                        required_changes_json=json.dumps(required_changes),
                        findings_json=json.dumps(findings or [], default=str))
    session.add(brief)
    session.flush()
    log_event(session, "rework.brief_created", actor=actor, product=product, job=job, message=summary,
              data={"brief_id": brief.id, "source": source, "change_ids": ids})
    return brief


def active_brief(session: Session, product: Product) -> ReworkBrief | None:
    return session.scalar(select(ReworkBrief).where(ReworkBrief.product_id == product.id,
                                                    ReworkBrief.status.in_(ACTIVE_BRIEF))
                          .order_by(ReworkBrief.id.desc()).limit(1))


def uncovered(brief: ReworkBrief, spec: dict) -> list[str]:
    """Change ids the spec's builder does not implement yet."""
    resolved = set(spec.get("resolves", []))
    return [i for i in brief.change_ids if i not in resolved]


def brief_payload(brief: ReworkBrief) -> dict:
    return {"id": brief.id, "source": brief.source, "summary": brief.summary,
            "required_changes": json.loads(brief.required_changes_json)}
