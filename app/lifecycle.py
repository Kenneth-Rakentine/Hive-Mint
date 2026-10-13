"""Product lifecycle: the only code path allowed to change Product.status.

Every change is checked against the transition table and context guards and is written
to the append-only Event log.
"""

from __future__ import annotations

import json
from datetime import datetime, time

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import AgentRun, Event, Job, Product
from app.states import ACTIVE, HOLD, IllegalTransition, ProductStatus, can_transition
from config.settings import Settings

S = ProductStatus


class TransitionBlocked(IllegalTransition):
    """The move exists in the table but a guard (gate history, approval) forbids it now."""


class LimitReached(RuntimeError):
    """A configured daily limit would be exceeded."""


def start_of_today() -> datetime:
    """Daily limits reset at midnight UTC."""
    now = utcnow()
    return datetime.combine(now.date(), time.min, tzinfo=now.tzinfo)


def log_event(
    session: Session,
    event_type: str,
    *,
    actor: str,
    message: str = "",
    product: Product | None = None,
    job: Job | None = None,
    run: AgentRun | None = None,
    from_status: str | None = None,
    to_status: str | None = None,
    data: dict | None = None,
) -> Event:
    session.flush()  # make sure referenced rows have ids
    event = Event(
        event_type=event_type,
        actor=actor,
        message=message,
        product_id=product.id if product else (job.product_id if job else None),
        job_id=job.id if job else (run.job_id if run else None),
        agent_run_id=run.id if run else None,
        from_status=from_status,
        to_status=to_status,
        data_json=json.dumps(data or {}, sort_keys=True, default=str),
    )
    session.add(event)
    return event


def create_product(
    session: Session,
    settings: Settings,
    *,
    slug: str,
    name: str,
    product_type: str,
    description: str = "",
    actor: str = "operations",
) -> Product:
    created_today = session.scalar(select(func.count(Product.id)).where(Product.created_at >= start_of_today()))
    if created_today >= settings.max_ideas_per_day:
        raise LimitReached(f"Daily idea limit reached ({settings.max_ideas_per_day})")
    product = Product(slug=slug, name=name, product_type=product_type, description=description,
                      status=S.IDEA, status_reason="New idea")
    session.add(product)
    log_event(session, "product.created", actor=actor, product=product, to_status=S.IDEA,
              message=f"Created idea: {name}", data={"slug": slug, "product_type": product_type})
    return product


def publications_today(session: Session) -> int:
    return session.scalar(
        select(func.count(Event.id)).where(
            Event.event_type == "product.transition",
            Event.to_status == S.PUBLISHED.value,
            Event.created_at >= start_of_today(),
        )
    )


def _check_guards(session: Session, settings: Settings, product: Product, target: ProductStatus) -> None:
    current = product.status
    if current in HOLD and target in ACTIVE and target != product.paused_from:
        raise TransitionBlocked(f"{current} may only resume to {product.paused_from}, not {target}")
    if current == S.HUMAN_REVIEW and target == S.READY_TO_PUBLISH:
        if product.previous_status != S.SEO:
            raise TransitionBlocked("Release requires a product that passed SEO; finish the remaining gates first")
        if not product.approved_by:
            raise TransitionBlocked("Release requires a recorded human approval")
    if current == S.HUMAN_REVIEW and target == S.COMPLIANCE and product.previous_status != S.COMPLIANCE:
        raise TransitionBlocked("Only a compliance referral can be returned to COMPLIANCE")
    if target == S.PUBLISHED and publications_today(session) >= settings.max_publications_per_day:
        raise LimitReached(f"Daily publication limit reached ({settings.max_publications_per_day})")


def transition(
    session: Session,
    settings: Settings,
    product: Product,
    target: ProductStatus | str,
    *,
    actor: str,
    reason: str,
) -> Product:
    """Move a product to `target`, enforcing the table, guards and limits, and log it."""
    target = ProductStatus(target)
    current = product.status
    if not can_transition(current, target):
        raise IllegalTransition(f"{current} -> {target} is not an allowed transition")
    _check_guards(session, settings, product, target)

    requested = target
    if target == S.REWORK:
        product.rework_count += 1
        product.approved_by = product.approved_at = None
        if product.rework_count > settings.max_rework_cycles:
            target = S.PAUSED
            reason = f"Rework limit reached ({settings.max_rework_cycles}); human decision needed. Last failure: {reason}"

    if target in HOLD:
        if current in ACTIVE:
            product.paused_from = current
    elif current in HOLD:
        product.paused_from = None

    product.previous_status = current
    product.status = target
    product.status_reason = reason
    product.updated_at = utcnow()
    log_event(session, "product.transition", actor=actor, product=product, from_status=current,
              to_status=target, message=reason,
              data={"requested": requested} if requested != target else None)
    return product


def record_approval(session: Session, product: Product, *, approver: str, note: str = "") -> Product:
    """A human approves the exact artifact that passed every gate. Required before release."""
    if product.status != S.HUMAN_REVIEW or product.previous_status != S.SEO:
        raise TransitionBlocked("Only a product that passed SEO and is in HUMAN_REVIEW can be approved")
    if not approver.strip():
        raise ValueError("Approver name is required")
    product.approved_by = approver.strip()
    product.approved_at = utcnow()
    log_event(session, "product.approved", actor=product.approved_by, product=product, message=note or "Approved")
    return product
