"""The pixel office's data: a read-only picture of the company, built only from the database.

CLAUDE.md: the GUI visualizes database state and never drives logic or shows simulated activity.
Every worker, crate and log line on the map comes from a row here: a product's status places its
crate at a building, a RUNNING job puts a worker to work, QUEUED jobs wait at the door, and the
message log is the Event table. GET /office/state serves this; app/static/office.html draws it.
"""

from __future__ import annotations

import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agents.owner_desk import APPROVER as DESK_APPROVER, needs_owner
from app.models import AgentRun, Event, Job, Listing, Product, Shift
from app.states import Department, JobStatus, ProductStatus
from config.settings import Settings
from providers.claude_headless import headless_runs_today
from tools.disk import disk_report

S = ProductStatus
D = Department

# Buildings in pipeline order: (id, name shown on the map, department, statuses whose crates sit there).
BUILDINGS: tuple[tuple[str, str, Department | None, tuple[ProductStatus, ...]], ...] = (
    ("research", "Research Hall", D.RESEARCH, (S.IDEA, S.RESEARCH)),
    ("finance", "Treasury", D.FINANCE, (S.FINANCE_REVIEW,)),
    ("design", "Drafting Room", D.PRODUCT_DESIGN, (S.SPECIFICATION, S.REWORK)),
    ("production", "Workshop", D.PRODUCTION, (S.PRODUCTION,)),
    ("testing", "Test Lab", D.TESTING, (S.TESTING,)),
    ("qa", "QA Gallery", D.QA, (S.QA,)),
    ("red_team", "Red Tower", D.RED_TEAM, (S.RED_TEAM,)),
    ("compliance", "Courthouse", D.COMPLIANCE, (S.COMPLIANCE,)),
    ("seo", "Market Stall", D.SEO, (S.SEO,)),
    ("keep", "The Lighthouse", D.OWNER_DESK, (S.HUMAN_REVIEW, S.READY_TO_PUBLISH)),
    ("store", "Storefront", None, (S.PUBLISHED,)),
    ("infirmary", "Infirmary", None, (S.PAUSED, S.ERROR)),
    ("graveyard", "Graveyard", None, (S.REJECTED, S.RETIRED)),
)
EVENT_LIMIT = 30
# Bookkeeping rows that repeat what a transition already says, or would drown the log.
QUIET_EVENTS = ("agent_run.finished", "job.started", "job.finished", "job.created", "claude.call")


def office_state(session: Session, settings: Settings, disk: dict | None = None) -> dict:
    products = session.scalars(select(Product).order_by(Product.id)).all()
    open_jobs = session.scalars(select(Job).where(Job.status.in_(
        (JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.PAUSED)))).all()
    listings = {row.product_id: row for row in session.scalars(select(Listing).order_by(Listing.id))}

    buildings = []
    for building_id, name, department, statuses in BUILDINGS:
        jobs = [j for j in open_jobs if department is not None and j.department == department]
        here = [p for p in products if p.status in statuses]
        buildings.append({
            "id": building_id, "name": name, "department": department.value if department else None,
            "statuses": [s.value for s in statuses],
            "products": [_product(p, listings.get(p.id)) for p in here],
            "running": [_job(j) for j in jobs if j.status == JobStatus.RUNNING],
            "queued": [_job(j) for j in jobs if j.status == JobStatus.QUEUED],
            "paused": [_job(j) for j in jobs if j.status == JobStatus.PAUSED],
        })

    shift = session.scalar(select(Shift).order_by(Shift.id.desc()).limit(1))
    events = session.scalars(select(Event).where(Event.event_type.not_in(QUIET_EVENTS))
                             .order_by(Event.id.desc()).limit(EVENT_LIMIT)).all()
    names = {p.id: p.name for p in products}
    reported = session.scalar(select(func.coalesce(func.sum(AgentRun.cost_usd_micros), 0))
                              .where(AgentRun.provider == "claude-headless")) or 0
    disk = disk if disk is not None else disk_report(settings).as_dict(settings)
    return {
        "resources": {
            "revenue_usd": 0.0,  # sales tracking arrives with the storefront adapters
            "claude_runs_today": headless_runs_today(session, settings),
            "claude_runs_cap": settings.max_claude_runs_per_day,
            "claude_reported_usd": round(reported / 1_000_000, 2),  # covered by the Max plan, never billed
            "products": len(products),
            "waiting_for_you": sum(1 for p in products if needs_owner(session, settings, p)),
            "approved_by_desk": sum(1 for p in products if p.status == S.READY_TO_PUBLISH
                                    and (p.approved_by or "") == DESK_APPROVER),
            "disk_free_gb": disk.get("free_gb"),
            "hivemint_gb": disk.get("hivemint_folder_gb"),
        },
        "shift": None if shift is None else {
            "id": shift.id, "running": shift.ended_at is None,
            "started_at": shift.started_at.isoformat(),
            "ended_at": shift.ended_at.isoformat() if shift.ended_at else None,
            "stop_reason": shift.stop_reason,
            "window": f"{settings.shift_start}-{settings.shift_end}",
        },
        "buildings": buildings,
        "events": [{"id": e.id, "at": e.created_at.isoformat(), "type": e.event_type, "actor": e.actor,
                    "product": names.get(e.product_id), "message": e.message[:240],
                    "to": e.to_status} for e in events],
    }


def _product(product: Product, listing: Listing | None) -> dict:
    return {"id": product.id, "name": product.name, "status": product.status.value,
            "reason": product.status_reason[:240], "reworks": product.rework_count,
            "listing": None if listing is None else {"title": listing.title, "price": listing.price_cents / 100,
                                                     "path": listing.path,
                                                     "images": len(json.loads(listing.data_json)
                                                                   .get("image_files", []))}}


def _job(job: Job) -> dict:
    return {"id": job.id, "type": job.job_type, "product_id": job.product_id, "attempts": job.attempts,
            "error": job.error}
