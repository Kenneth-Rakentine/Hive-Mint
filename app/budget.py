"""Hard spending caps for paid (Tier 2+) AI calls.

Every future paid provider must call assert_within_budget() before making a request and
record the real cost on its AgentRun afterwards. Headless Claude Code runs (provider
claude-headless) use the Max subscription: their reported cost is recorded but not spent, so it
is excluded here; they are capped by MAX_CLAUDE_RUNS_PER_DAY instead.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.clock import day_start, month_start
from app.models import AgentRun
from config.settings import Settings

PAID_TIER = 2
SUBSCRIPTION_PROVIDERS = ("claude-headless",)


class BudgetExceeded(RuntimeError):
    pass


def _spend_since(session: Session, since: datetime) -> int:
    return session.scalar(
        select(func.coalesce(func.sum(AgentRun.cost_usd_micros), 0)).where(
            AgentRun.tier >= PAID_TIER, AgentRun.started_at >= since,
            AgentRun.provider.not_in(SUBSCRIPTION_PROVIDERS),
        )
    )


def spend_today(session: Session, settings: Settings) -> int:
    return _spend_since(session, day_start(settings.timezone))


def spend_month(session: Session, settings: Settings) -> int:
    return _spend_since(session, month_start(settings.timezone))


def assert_within_budget(session: Session, settings: Settings, estimated_micros: int) -> None:
    if estimated_micros < 0:
        raise ValueError("Estimated cost cannot be negative")
    day, month = spend_today(session, settings), spend_month(session, settings)
    if day + estimated_micros > settings.cloud_daily_budget_micros:
        raise BudgetExceeded(f"Daily cloud budget would be exceeded ({day + estimated_micros} > "
                             f"{settings.cloud_daily_budget_micros} micro-USD)")
    if month + estimated_micros > settings.cloud_monthly_budget_micros:
        raise BudgetExceeded(f"Monthly cloud budget would be exceeded ({month + estimated_micros} > "
                             f"{settings.cloud_monthly_budget_micros} micro-USD)")
