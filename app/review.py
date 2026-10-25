"""The owner's review desk: everything a person needs to decide on a product in HUMAN_REVIEW.

review_card() gathers the evidence trail from the database (tests, QA, Red Team, Compliance, the
listing, AI usage). The decisions are explicit and logged:

- approve: records the approval and moves the product to READY_TO_PUBLISH. Publishing itself stays
  manual (MAX_PUBLICATIONS_PER_DAY is 0): the owner uploads the listing package by hand. The owner's
  desk (agents/owner_desk.py) approves on the owner's behalf when its strict picture checks pass.
- reject: also takes back a product the desk approved but the owner has not uploaded.
- reject: the product ends in REJECTED.
- relist: the listing alone goes back to SEO for a new draft (the product is unchanged).
Changes to the product itself go through the existing rework command with a brief.
"""

from __future__ import annotations

import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.lifecycle import TransitionBlocked, log_event, record_approval, transition
from app.models import AgentRun, Build, Event, Job, Listing, Product, Review, TestResult
from app.states import Department, ProductStatus
from config.settings import Settings

S = ProductStatus


def latest_build(session: Session, product: Product) -> Build | None:
    return session.scalar(select(Build).where(Build.product_id == product.id).order_by(Build.version.desc()).limit(1))


def latest_listing(session: Session, product: Product, build: Build | None = None) -> Listing | None:
    query = select(Listing).where(Listing.product_id == product.id)
    if build is not None:
        query = query.where(Listing.build_id == build.id)
    return session.scalar(query.order_by(Listing.id.desc()).limit(1))


def _latest_review(session: Session, product: Product, department: Department, build: Build | None) -> Review | None:
    query = select(Review).where(Review.product_id == product.id, Review.department == department)
    if build is not None:
        query = query.where(Review.build_id == build.id)
    return session.scalar(query.order_by(Review.id.desc()).limit(1))


def review_card(session: Session, product: Product) -> dict:
    build = latest_build(session, product)
    card: dict = {"id": product.id, "name": product.name, "status": product.status.value,
                  "status_reason": product.status_reason, "rework_cycles": product.rework_count,
                  "approved_by": product.approved_by}
    if desk := session.scalar(select(Review).where(Review.product_id == product.id,
                                                   Review.department == Department.OWNER_DESK)
                              .order_by(Review.id.desc()).limit(1)):
        report = json.loads(desk.report_json or "{}")
        card["owner_desk"] = {"decision": desk.decision, "summary": desk.summary,
                              "pictures": len(report.get("pictures", [])),
                              "question": report.get("owner_question", ""),
                              "issues": [f"{i['where']}: {i['problem']}" for i in json.loads(desk.findings_json)][:8]}
    if build is None:
        return card
    tests = session.scalars(select(TestResult).where(TestResult.build_id == build.id)).all()
    card["build"] = {"version": build.version, "path": build.path, "zip": build.zip_path}
    card["testing"] = {"passed": sum(t.passed for t in tests), "total": len(tests),
                       "failed": [f"{t.check} {t.target}: {t.detail}" for t in tests if not t.passed][:5]}
    if qa := _latest_review(session, product, Department.QA, build):
        card["qa"] = {"decision": qa.decision, "score": qa.score, "summary": qa.summary}
    if red := _latest_review(session, product, Department.RED_TEAM, build):
        card["red_team"] = {"decision": red.decision, "summary": red.summary}
    compliance = session.scalar(select(Event).where(Event.product_id == product.id,
                                                    Event.event_type == "compliance.checked")
                                .order_by(Event.id.desc()).limit(1))
    if compliance is not None:
        data = json.loads(compliance.data_json)
        card["compliance"] = {"claims": data.get("claims", []), "ambiguous": data.get("ambiguous", []),
                              "message": compliance.message}
    if listing := latest_listing(session, product, build):
        data = json.loads(listing.data_json)
        card["listing"] = {"title": listing.title, "price": listing.price_cents / 100, "tags": json.loads(listing.tags_json),
                           "images": len(data.get("image_files", [])), "path": listing.path,
                           "price_reason": data.get("price_reason", "")}
    runs = session.execute(select(AgentRun.provider, func.count(AgentRun.id), func.sum(AgentRun.cost_usd_micros))
                           .join(Job, AgentRun.job_id == Job.id).where(Job.product_id == product.id)
                           .group_by(AgentRun.provider)).all()
    card["ai_runs"] = {provider: {"runs": count, "reported_cost_usd": round((cost or 0) / 1_000_000, 2)}
                       for provider, count, cost in runs}
    return card


def _require_review(product: Product) -> None:
    if product.status != S.HUMAN_REVIEW:
        raise TransitionBlocked(f"Product {product.id} is {product.status}, not HUMAN_REVIEW")


def approve(session: Session, settings: Settings, product: Product, *, approver: str, note: str = "") -> Product:
    """Approve the exact build and listing that passed every gate, then mark it ready to publish."""
    _require_review(product)
    build = latest_build(session, product)
    if build is None or latest_listing(session, product, build) is None:
        raise TransitionBlocked("No listing package for the latest build; run the SEO step first "
                                f"(python -m app.cli relist {product.id})")
    record_approval(session, product, approver=approver, note=note)
    return transition(session, settings, product, S.READY_TO_PUBLISH, actor=approver,
                      reason=f"Approved by {approver}" + (f": {note}" if note else ""))


def reject(session: Session, settings: Settings, product: Product, *, actor: str, reason: str) -> Product:
    """Turn a product down for good: one waiting for review, one approved but not uploaded yet (the approval
    is withdrawn first), or one held (PAUSED or ERROR)."""
    if not reason.strip():
        raise ValueError("Say why the product is rejected")
    if product.status == S.READY_TO_PUBLISH:
        transition(session, settings, product, S.HUMAN_REVIEW, actor=actor,
                   reason=f"Approval ({product.approved_by}) withdrawn by {actor}: {reason}")
    if product.status not in (S.PAUSED, S.ERROR):
        _require_review(product)
    return transition(session, settings, product, S.REJECTED, actor=actor, reason=f"Rejected by {actor}: {reason}")


def relist(session: Session, settings: Settings, product: Product, *, actor: str, reason: str = "") -> Product:
    """Send only the listing back to SEO for a fresh draft."""
    _require_review(product)
    log_event(session, "listing.redo_requested", actor=actor, product=product, message=reason or "Redo the listing")
    return transition(session, settings, product, S.SEO, actor=actor,
                      reason=f"Listing redo requested by {actor}" + (f": {reason}" if reason else ""))
