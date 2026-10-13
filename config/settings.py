"""Runtime settings loaded from environment variables and an optional .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TIMEZONE = "America/New_York"
DEFAULT_DB_URL = f"sqlite:///{(ROOT / 'data' / 'hivemint.db').as_posix()}"


def usd_to_micros(amount: str | Decimal) -> int:
    """Convert a USD amount to integer micro-dollars (1 USD = 1_000_000)."""
    return int(Decimal(str(amount)) * 1_000_000)


def _int(name: str, default: int) -> int:
    value = int(os.getenv(name, default))
    if value < 0:
        raise ValueError(f"{name} must not be negative")
    return value


def _micros(name: str, default: str) -> int:
    value = usd_to_micros(os.getenv(name, default))
    if value < 0:
        raise ValueError(f"{name} must not be negative")
    return value


@dataclass(frozen=True)
class Settings:
    db_url: str = DEFAULT_DB_URL
    cloud_daily_budget_micros: int = usd_to_micros("1.00")
    cloud_monthly_budget_micros: int = usd_to_micros("20.00")
    max_ideas_per_day: int = 20
    max_builds_per_day: int = 3
    max_publications_per_day: int = 0
    max_job_attempts: int = 3
    max_rework_cycles: int = 3
    ollama_url: str = "http://127.0.0.1:11434"
    # Daily and monthly limits reset at local midnight in this IANA timezone.
    timezone: str = DEFAULT_TIMEZONE

    def __post_init__(self) -> None:
        ZoneInfo(self.timezone)  # fail fast on an unknown timezone

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(ROOT / ".env", override=False)
        return cls(
            db_url=os.getenv("HIVEMINT_DB_URL", DEFAULT_DB_URL),
            cloud_daily_budget_micros=_micros("CLOUD_DAILY_BUDGET_USD", "1.00"),
            cloud_monthly_budget_micros=_micros("CLOUD_MONTHLY_BUDGET_USD", "20.00"),
            max_ideas_per_day=_int("MAX_IDEAS_PER_DAY", 20),
            max_builds_per_day=_int("MAX_BUILDS_PER_DAY", 3),
            max_publications_per_day=_int("MAX_PUBLICATIONS_PER_DAY", 0),
            max_job_attempts=max(1, _int("MAX_JOB_ATTEMPTS", 3)),
            max_rework_cycles=_int("MAX_REWORK_CYCLES", 3),
            ollama_url=os.getenv("OLLAMA_URL", "http://127.0.0.1:11434"),
            timezone=os.getenv("HIVEMINT_TIMEZONE", DEFAULT_TIMEZONE),
        )

    def limits(self) -> dict[str, object]:
        """Human-readable limits for status output."""
        return {
            "cloud_daily_budget_usd": f"{self.cloud_daily_budget_micros / 1_000_000:.2f}",
            "cloud_monthly_budget_usd": f"{self.cloud_monthly_budget_micros / 1_000_000:.2f}",
            "max_ideas_per_day": self.max_ideas_per_day,
            "max_builds_per_day": self.max_builds_per_day,
            "max_publications_per_day": self.max_publications_per_day,
            "max_job_attempts": self.max_job_attempts,
            "max_rework_cycles": self.max_rework_cycles,
            "limits_reset": f"local midnight ({self.timezone})",
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
