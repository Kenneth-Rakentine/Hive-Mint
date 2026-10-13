"""Read-only status snapshot shared by the API and the CLI."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.budget import spend_month, spend_today
from app.models import Job, Product
from app.states import JobStatus, ProductStatus
from config.settings import Settings
from tools.disk import disk_report


def snapshot(session: Session, settings: Settings) -> dict:
    products = dict.fromkeys((s.value for s in ProductStatus), 0)
    for status, count in session.execute(select(Product.status, func.count()).group_by(Product.status)):
        products[status.value] = count
    jobs = dict.fromkeys((s.value for s in JobStatus), 0)
    for status, count in session.execute(select(Job.status, func.count()).group_by(Job.status)):
        jobs[status.value] = count
    def listed(status: JobStatus):
        return session.execute(select(Job, Product.name).join(Product).where(Job.status == status).order_by(Job.id)).all()

    queued, paused = listed(JobStatus.QUEUED), listed(JobStatus.PAUSED)
    return {
        "products_by_state": products,
        "jobs_by_status": jobs,
        "queued_jobs": [
            {"id": job.id, "product": name, "department": job.department.value, "job_type": job.job_type,
             "created_at": job.created_at.isoformat()}
            for job, name in queued
        ],
        "paused_jobs": [
            {"id": job.id, "product": name, "department": job.department.value, "job_type": job.job_type,
             "error": job.error}
            for job, name in paused
        ],
        "disk": disk_report(settings).as_dict(settings),
        "cloud_spend_usd": {"today": f"{spend_today(session, settings) / 1_000_000:.2f}",
                            "month": f"{spend_month(session, settings) / 1_000_000:.2f}"},
        "limits": settings.limits(),
    }
