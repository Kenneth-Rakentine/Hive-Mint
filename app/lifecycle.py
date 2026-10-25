"""Product lifecycle: the only code path allowed to change Product.status.

Every change is checked against the transition table and context guards and is written
to the append-only Event log.
"""

from __future__ import annotations

import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.clock import day_start
from app.db import utcnow
from app.models import AgentRun, Event, Job, Product
from app.states import ACTIVE, HOLD, IllegalTransition, ProductStatus, can_transition
from config.settings import Settings

S = ProductStatus


class TransitionBlocked(IllegalTransition):
    """The move exists in the table but a guard (gate history, approval) forbids it now."""


class LimitReached(RuntimeError):
    """A configured daily limit would be exceeded."""


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
    created_today = session.scalar(select(func.count(Product.id)).where(Product.created_at >= day_start(settings.timezone)))
    if created_today >= settings.max_ideas_per_day:
        raise LimitReached(f"Daily idea limit reached ({settings.max_ideas_per_day})")
    product = Product(slug=slug, name=name, product_type=product_type, description=description,
                      status=S.IDEA, status_reason="New idea")
    session.add(product)
    log_event(session, "product.created", actor=actor, product=product, to_status=S.IDEA,
              message=f"Created idea: {name}", data={"slug": slug, "product_type": product_type})
    return product


def publications_today(session: Session, settings: Settings) -> int:
    return session.scalar(
        select(func.count(Event.id)).where(
            Event.event_type == "product.transition",
            Event.to_status == S.PUBLISHED.value,
            Event.created_at >= day_start(settings.timezone),
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
            raise TransitionBlocked("Release requires a recorded approval (the owner's, or the owner's desk)")
    if current == S.HUMAN_REVIEW and target == S.COMPLIANCE and product.previous_status != S.COMPLIANCE:
        raise TransitionBlocked("Only a compliance referral can be returned to COMPLIANCE")
    if current == S.HUMAN_REVIEW and target == S.SEO and product.previous_status != S.SEO:
        raise TransitionBlocked("Only a product that came from SEO can have its listing redone")
    if target == S.PUBLISHED and publications_today(session, settings) >= settings.max_publications_per_day:
        raise LimitReached(f"Daily publication limit reached ({settings.max_publications_per_day})")


def _auto_grants_used(session: Session, product: Product) -> int:
    return session.scalar(select(func.count(Event.id)).where(Event.product_id == product.id,
                                                             Event.event_type == "rework.auto_granted")) or 0


def rework_limit(settings: Settings, product: Product) -> int:
    """MAX_REWORK_CYCLES plus any extra cycles the owner granted (python -m app.cli approve-rework)."""
    return settings.max_rework_cycles + (product.extra_rework_cycles or 0)


def transition(
    session: Session,
    settings: Settings,
    product: Product,
    target: ProductStatus | str,
    *,
    actor: str,
    reason: str,
    job: Job | None = None,
    run: AgentRun | None = None,
) -> Product:
    """Move a product to `target`, enforcing the table, guards and limits, and log it."""
    target = ProductStatus(target)
    current = product.status
    if not can_transition(current, target):
        raise IllegalTransition(f"{current} -> {target} is not an allowed transition")
    _check_guards(session, settings, product, target)

    requested = target
    held_at = current
    if target == S.REWORK and current not in HOLD:  # resuming a held rework is not a new cycle
        product.rework_count += 1
        product.approved_by = product.approved_at = None
        limit = rework_limit(settings, product)
        if product.rework_count > limit and _auto_grants_used(session, product) < settings.auto_rework_grants:
            product.extra_rework_cycles = (product.extra_rework_cycles or 0) + 1
            limit = rework_limit(settings, product)
            log_event(session, "rework.auto_granted", actor="operations", product=product, job=job,
                      message=f"Rework limit reached; one extra cycle granted automatically (limit now {limit}). "
                              f"The next time it pauses for the owner.",
                      data={"rework_count": product.rework_count, "limit": limit})
        if product.rework_count > limit:
            target, held_at = S.PAUSED, S.REWORK  # held at the rework it was refused; resume enters it
            reason = (f"Rework limit reached ({limit}); human decision needed: python -m app.cli approve-rework "
                      f"{product.id}. Last failure: {reason}")

    if target in HOLD:
        if current in ACTIVE:
            product.paused_from = held_at
    elif current in HOLD:
        product.paused_from = None

    product.previous_status = current
    product.status = target
    product.status_reason = reason
    product.updated_at = utcnow()
    log_event(session, "product.transition", actor=actor, product=product, job=job, run=run, from_status=current,
              to_status=target, message=reason,
              data={"requested": requested} if requested != target else None)
    return product


def resume(session: Session, settings: Settings, product: Product, *, actor: str, reason: str = "") -> Product:
    """Return a PAUSED or ERROR product to the gate it was held at."""
    if product.status not in HOLD or product.paused_from is None:
        raise TransitionBlocked(f"Only a PAUSED or ERROR product can be resumed (status is {product.status})")
    return transition(session, settings, product, product.paused_from, actor=actor,
                      reason=reason or f"Resumed from {product.status} by {actor}")


def approve_rework(session: Session, settings: Settings, product: Product, *, cycles: int, actor: str) -> Product:
    """The owner grants extra rework cycles. A product paused by the rework limit resumes into REWORK."""
    if cycles < 1:
        raise ValueError("Grant at least one cycle")
    if product.status in (S.REJECTED, S.RETIRED, S.PUBLISHED):
        raise TransitionBlocked(f"A {product.status} product cannot be reworked")
    product.extra_rework_cycles = (product.extra_rework_cycles or 0) + cycles
    log_event(session, "rework.approved", actor=actor, product=product,
              message=f"{actor} granted {cycles} extra rework cycle(s); limit now {rework_limit(settings, product)}",
              data={"cycles": cycles, "extra_rework_cycles": product.extra_rework_cycles,
                    "rework_count": product.rework_count})
    if product.status == S.PAUSED and product.paused_from == S.REWORK:
        resume(session, settings, product, actor=actor, reason=f"Extra rework cycle(s) approved by {actor}")
    return product


def auto_resume_rework(session: Session, settings: Settings, product: Product) -> bool:
    """A product paused at the rework limit gets the automatic extra cycle if it hasn't had it yet (it was
    paused before AUTO_REWORK_GRANTS existed, or the limit was lowered). Returns True if it resumed."""
    if product.status != S.PAUSED or product.paused_from != S.REWORK:
        return False
    if _auto_grants_used(session, product) >= settings.auto_rework_grants:
        return False
    product.extra_rework_cycles = (product.extra_rework_cycles or 0) + 1
    log_event(session, "rework.auto_granted", actor="operations", product=product,
              message=f"One extra rework cycle granted automatically (limit now {rework_limit(settings, product)}). "
                      f"The next time it pauses for the owner.",
              data={"rework_count": product.rework_count, "limit": rework_limit(settings, product)})
    resume(session, settings, product, actor="operations", reason="Automatic extra rework cycle")
    return True


def record_approval(session: Session, product: Product, *, approver: str, note: str = "") -> Product:
    """The owner, or the owner's desk on his behalf, approves the exact artifact that passed every gate.
    Required before release."""
    if product.status != S.HUMAN_REVIEW or product.previous_status != S.SEO:
        raise TransitionBlocked("Only a product that passed SEO and is in HUMAN_REVIEW can be approved")
    if not approver.strip():
        raise ValueError("Approver name is required")
    product.approved_by = approver.strip()
    product.approved_at = utcnow()
    log_event(session, "product.approved", actor=product.approved_by, product=product, message=note or "Approved")
    return product
