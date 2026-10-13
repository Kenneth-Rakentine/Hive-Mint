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
RECALC_ENGINES = ("auto", "libreoffice", "formulas")
DEFAULT_OLLAMA_MODELS_DIR = Path.home() / ".ollama" / "models"


def usd_to_micros(amount: str | Decimal) -> int:
    """Convert a USD amount to integer micro-dollars (1 USD = 1_000_000)."""
    return int(Decimal(str(amount)) * 1_000_000)


def _int(name: str, default: int) -> int:
    value = int(os.getenv(name, default))
    if value < 0:
        raise ValueError(f"{name} must not be negative")
    return value


def _float(name: str, default: float) -> float:
    value = float(os.getenv(name, default))
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
    ollama_model: str = "qwen3.5:4b"
    ollama_timeout_s: float = 120.0
    ollama_models_dir: Path = DEFAULT_OLLAMA_MODELS_DIR
    # QA: pass score, and the most a local model's findings may deduct (deterministic checks come first).
    qa_pass_score: int = 70
    qa_model_max_deduction: int = 20
    # Disk guard: Production jobs pause (DISK_LIMIT) below this free space or above this folder size.
    min_free_disk_gb: float = 50.0
    max_hivemint_storage_gb: float = 20.0
    storage_root: Path = ROOT
    # Daily and monthly limits reset at local midnight in this IANA timezone.
    timezone: str = DEFAULT_TIMEZONE
    # Where Production writes builds, and where stale build folders are moved aside.
    products_dir: Path = ROOT / "products"
    archive_dir: Path = ROOT / "archive"
    # Testing recalculates workbooks with LibreOffice if found ("auto"), else the formulas package.
    recalc_engine: str = "auto"
    soffice_path: str | None = None

    def __post_init__(self) -> None:
        ZoneInfo(self.timezone)  # fail fast on an unknown timezone
        if self.recalc_engine not in RECALC_ENGINES:
            raise ValueError(f"HIVEMINT_RECALC_ENGINE must be one of {', '.join(RECALC_ENGINES)}")

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
            ollama_model=os.getenv("OLLAMA_MODEL", "qwen3.5:4b"),
            ollama_timeout_s=_float("OLLAMA_TIMEOUT_S", 120.0),
            ollama_models_dir=Path(os.getenv("OLLAMA_MODELS") or DEFAULT_OLLAMA_MODELS_DIR),
            qa_pass_score=_int("QA_PASS_SCORE", 70),
            qa_model_max_deduction=_int("QA_MODEL_MAX_DEDUCTION", 20),
            min_free_disk_gb=_float("MIN_FREE_DISK_GB", 50.0),
            max_hivemint_storage_gb=_float("MAX_HIVEMINT_STORAGE_GB", 20.0),
            storage_root=Path(os.getenv("HIVEMINT_STORAGE_ROOT", ROOT)),
            timezone=os.getenv("HIVEMINT_TIMEZONE", DEFAULT_TIMEZONE),
            products_dir=Path(os.getenv("HIVEMINT_PRODUCTS_DIR", ROOT / "products")),
            archive_dir=Path(os.getenv("HIVEMINT_ARCHIVE_DIR", ROOT / "archive")),
            recalc_engine=os.getenv("HIVEMINT_RECALC_ENGINE", "auto"),
            soffice_path=os.getenv("HIVEMINT_SOFFICE") or None,
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
            "min_free_disk_gb": self.min_free_disk_gb,
            "max_hivemint_storage_gb": self.max_hivemint_storage_gb,
            "limits_reset": f"local midnight ({self.timezone})",
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
