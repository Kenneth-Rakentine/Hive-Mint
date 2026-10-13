from datetime import UTC, datetime, timedelta

import pytest

from app.clock import day_start, month_start
from app.lifecycle import LimitReached, create_product
from app.models import Product
from config.settings import Settings

NY = "America/New_York"


def test_default_timezone_is_new_york():
    assert Settings().timezone == NY


def test_unknown_timezone_is_rejected():
    with pytest.raises(Exception):
        Settings(timezone="Mars/Olympus_Mons")


def test_evening_in_new_york_is_still_the_same_local_day():
    # 01:30 UTC on Oct 14 is 21:30 EDT on Oct 13: the day started at 04:00 UTC on Oct 13.
    now = datetime(2026, 10, 14, 1, 30, tzinfo=UTC)
    assert day_start(NY, now).astimezone(UTC) == datetime(2026, 10, 13, 4, 0, tzinfo=UTC)


def test_day_start_follows_daylight_saving():
    winter = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    assert day_start(NY, winter).astimezone(UTC) == datetime(2026, 1, 15, 5, 0, tzinfo=UTC)  # EST, UTC-5
    # 2026-11-01 is the fall-back day: midnight is still EDT (UTC-4).
    fall_back = datetime(2026, 11, 1, 18, 0, tzinfo=UTC)
    assert day_start(NY, fall_back).astimezone(UTC) == datetime(2026, 11, 1, 4, 0, tzinfo=UTC)


def test_month_start_is_local_midnight_on_the_first():
    now = datetime(2026, 11, 1, 2, 0, tzinfo=UTC)  # 22:00 EDT on Oct 31: still October locally
    assert month_start(NY, now).astimezone(UTC) == datetime(2026, 10, 1, 4, 0, tzinfo=UTC)
    now = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)  # November, after the switch to EST
    assert month_start(NY, now).astimezone(UTC) == datetime(2026, 11, 1, 4, 0, tzinfo=UTC)


def test_idea_limit_counts_only_since_local_midnight(session, settings):
    limited = Settings(db_url=settings.db_url, max_ideas_per_day=1)
    yesterday_late = day_start(NY) - timedelta(minutes=1)
    session.add(Product(slug="old", name="Old", product_type="pdf", created_at=yesterday_late))
    session.flush()
    create_product(session, limited, slug="new", name="New", product_type="pdf")
    with pytest.raises(LimitReached):
        create_product(session, limited, slug="newer", name="Newer", product_type="pdf")
