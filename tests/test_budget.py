from datetime import timedelta

import pytest

from app.budget import BudgetExceeded, assert_within_budget, spend_month, spend_today
from app.clock import day_start, month_start
from app.db import utcnow
from app.models import AgentRun, Job
from app.states import Department, JobStatus, ProductStatus


def _run(session, product, *, tier, micros, started_at=None):
    job = Job(product_id=product.id, department=Department.RESEARCH, job_type="t", for_status=ProductStatus.RESEARCH)
    session.add(job)
    session.flush()
    run = AgentRun(job_id=job.id, department=Department.RESEARCH, provider="x", model="m", tier=tier,
                   cost_usd_micros=micros, status=JobStatus.SUCCEEDED, started_at=started_at or utcnow())
    session.add(run)
    session.flush()
    return run


def test_local_tiers_do_not_count(session, settings, make_product):
    p = make_product()
    _run(session, p, tier=0, micros=5_000_000)
    _run(session, p, tier=1, micros=5_000_000)
    assert spend_today(session, settings) == 0
    assert_within_budget(session, settings, 900_000)


def test_daily_cap(session, settings, make_product):
    _run(session, make_product(), tier=2, micros=800_000)
    assert_within_budget(session, settings, 200_000)  # exactly $1.00 is allowed
    with pytest.raises(BudgetExceeded, match="Daily"):
        assert_within_budget(session, settings, 200_001)


def test_monthly_cap(session, settings, make_product):
    first = month_start(settings.timezone)
    if first >= day_start(settings.timezone):
        pytest.skip("first day of the month: no earlier day to seed")
    _run(session, make_product(), tier=3, micros=19_900_000, started_at=first + timedelta(hours=1))
    assert spend_today(session, settings) == 0
    assert spend_month(session, settings) == 19_900_000
    with pytest.raises(BudgetExceeded, match="Monthly"):
        assert_within_budget(session, settings, 200_000)
