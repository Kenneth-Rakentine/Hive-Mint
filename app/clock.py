"""Local-calendar boundaries for daily and monthly limits.

Timestamps are stored in UTC; limits reset at local midnight in the configured timezone
(Settings.timezone, default America/New_York), including across DST changes.
"""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from app.db import utcnow


def day_start(tz: str, now: datetime | None = None) -> datetime:
    """Local midnight that began the current day in `tz`, as an aware datetime."""
    zone = ZoneInfo(tz)
    local = (now or utcnow()).astimezone(zone)
    return datetime.combine(local.date(), time.min, tzinfo=zone)


def month_start(tz: str, now: datetime | None = None) -> datetime:
    """Local midnight on the first day of the current month in `tz`."""
    return day_start(tz, now).replace(day=1)
