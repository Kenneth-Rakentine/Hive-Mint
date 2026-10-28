"""Runtime settings loaded from environment variables and an optional .env file."""

from __future__ import annotations

import os
import re
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
RENDER_ENGINES = ("auto", "excel", "libreoffice", "none")
DEFAULT_OLLAMA_MODELS_DIR = Path.home() / ".ollama" / "models"
# Finance: the weighted opportunity formula. Each factor is scored 1-5 (competition and support burden
# are "lower is better" and inverted); the weighted total is scaled to 0-100.
OPPORTUNITY_FACTORS = ("demand", "competition", "margin", "ease", "differentiation", "support_burden",
                       "automation_suitability")
INVERTED_FACTORS = frozenset({"competition", "support_burden"})
# Owner decision 2026-10-21: demand counts most and competition little. Marketplace buyers pay for a
# ready-made file even when free or similar ones exist, so a crowded market is not a reason to skip.
DEFAULT_OPPORTUNITY_WEIGHTS = (("demand", 0.35), ("competition", 0.05), ("margin", 0.15), ("ease", 0.10),
                               ("differentiation", 0.15), ("support_burden", 0.10), ("automation_suitability", 0.10))
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


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


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    if value.strip().lower() in ("1", "true", "yes", "on"):
        return True
    if value.strip().lower() in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name} must be true or false")


def _weights(name: str) -> tuple[tuple[str, float], ...]:
    """OPPORTUNITY_WEIGHTS=demand=0.25,competition=0.15,... (every factor once, summing to 1)."""
    value = os.getenv(name)
    if not value:
        return DEFAULT_OPPORTUNITY_WEIGHTS
    pairs = [part.split("=", 1) for part in value.split(",") if part.strip()]
    return tuple((k.strip(), float(v)) for k, v in pairs)


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
    # Extra rework cycles the system grants on its own, per product, before pausing for the owner. Every
    # rework comes with a concrete brief, so one more try is usually cheaper than waiting a day.
    auto_rework_grants: int = 1
    # The owner's desk (agents/owner_desk.py) reviews products that reach HUMAN_REVIEW from SEO. With
    # auto-approve on (owner decision 2026-10-25) a clean verdict moves the product to READY_TO_PUBLISH;
    # off, the verdict is only a recommendation. Upload stays manual either way.
    # Back to the drawing board: a product that runs out of rework cycles gets this many fresh redesigns (a new
    # spec from scratch that learns from every failure, with a fresh rework budget). When those fail too, it is
    # dropped (REJECTED) so its slot goes to a new idea; owner-specified products pause for the owner instead.
    redesigns_per_product: int = 1
    auto_drop_failed: bool = True
    desk_enabled: bool = True
    desk_auto_approve: bool = True
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3.5:4b"
    ollama_timeout_s: float = 120.0
    # Context window (prompt + answer) requested per call; Ollama's own default (4096) truncates QA's answers.
    ollama_num_ctx: int = 8192
    ollama_models_dir: Path = DEFAULT_OLLAMA_MODELS_DIR
    # QA: pass score, and the most a local model's findings may deduct (deterministic checks come first):
    # the text clarity pass and the chart vision pass are capped separately, and together they may not
    # deduct more than 100 - qa_pass_score, so the local model can never fail a product on its own.
    qa_pass_score: int = 70
    # The local model reports a dozen or more nitpicks on nearly every product, so it always reaches its cap.
    # The caps together use at most half the margin (100 - pass score), leaving room for a few small
    # deterministic findings; at 20 + 10 any one of them failed the product (owner-visible bottleneck, 2026-10-28).
    qa_model_max_deduction: int = 10
    qa_vision_max_deduction: int = 5
    # Visual QA renders workbooks with Excel (COM) if available ("auto"), else LibreOffice; "none" skips it.
    render_engine: str = "auto"
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
    # Autopilot (python -m app.cli shift). Headless Claude Code runs use the Max subscription; their reported
    # cost is logged only, so the hard cap is the number of runs per local day.
    max_claude_runs_per_day: int = 15
    # Distinct products that may get their first build per day; rework rebuilds count toward max_builds_per_day.
    max_products_built_per_day: int = 2
    # Niches a shift may research while none of its ideas has passed Finance yet. Another niche is only
    # started while at least research_reserve_runs headless runs remain for building what it finds.
    max_research_per_shift: int = 3
    research_reserve_runs: int = 6
    # Work in progress: while this many products are between SPECIFICATION and the owner's desk (REWORK
    # included), shifts research no new niches and Finance starts no new products; the runs and the daily
    # builds go to finishing what is already started.
    max_products_in_progress: int = 6
    shift_start: str = "13:00"  # local time in `timezone`
    shift_end: str = "22:00"
    skip_on_battery: bool = True
    claude_bin: str = "claude"
    claude_timeout_s: float = 1200.0
    opportunity_weights: tuple[tuple[str, float], ...] = DEFAULT_OPPORTUNITY_WEIGHTS
    opportunity_threshold: float = 65.0
    max_opportunities_per_shift: int = 3

    def __post_init__(self) -> None:
        ZoneInfo(self.timezone)  # fail fast on an unknown timezone
        if self.recalc_engine not in RECALC_ENGINES:
            raise ValueError(f"HIVEMINT_RECALC_ENGINE must be one of {', '.join(RECALC_ENGINES)}")
        if self.render_engine not in RENDER_ENGINES:
            raise ValueError(f"HIVEMINT_RENDER_ENGINE must be one of {', '.join(RENDER_ENGINES)}")
        if 2 * (self.qa_model_max_deduction + self.qa_vision_max_deduction) > 100 - self.qa_pass_score:
            raise ValueError("QA_MODEL_MAX_DEDUCTION + QA_VISION_MAX_DEDUCTION must not exceed half of 100 - QA_PASS_SCORE, "
                             "or the local model alone could fail a product")
        for name, value in (("SHIFT_START", self.shift_start), ("SHIFT_END", self.shift_end)):
            if not _HHMM.match(value):
                raise ValueError(f"{name} must be HH:MM (24-hour), got {value!r}")
        weights = dict(self.opportunity_weights)
        if set(weights) != set(OPPORTUNITY_FACTORS) or len(weights) != len(self.opportunity_weights):
            raise ValueError(f"OPPORTUNITY_WEIGHTS must name each factor once: {', '.join(OPPORTUNITY_FACTORS)}")
        if any(w < 0 for w in weights.values()) or abs(sum(weights.values()) - 1) > 1e-6:
            raise ValueError("OPPORTUNITY_WEIGHTS must be non-negative and sum to 1")
        if not 0 <= self.opportunity_threshold <= 100:
            raise ValueError("OPPORTUNITY_THRESHOLD must be between 0 and 100")

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
            auto_rework_grants=max(0, _int("AUTO_REWORK_GRANTS", 1)),
            redesigns_per_product=max(0, _int("REDESIGNS_PER_PRODUCT", 1)),
            auto_drop_failed=_bool("AUTO_DROP_FAILED", True),
            desk_enabled=_bool("DESK_ENABLED", True),
            desk_auto_approve=_bool("DESK_AUTO_APPROVE", True),
            ollama_url=os.getenv("OLLAMA_URL", "http://127.0.0.1:11434"),
            ollama_model=os.getenv("OLLAMA_MODEL", "qwen3.5:4b"),
            ollama_timeout_s=_float("OLLAMA_TIMEOUT_S", 120.0),
            ollama_num_ctx=_int("OLLAMA_NUM_CTX", 8192),
            ollama_models_dir=Path(os.getenv("OLLAMA_MODELS") or DEFAULT_OLLAMA_MODELS_DIR),
            qa_pass_score=_int("QA_PASS_SCORE", 70),
            qa_model_max_deduction=_int("QA_MODEL_MAX_DEDUCTION", 10),
            qa_vision_max_deduction=_int("QA_VISION_MAX_DEDUCTION", 5),
            render_engine=os.getenv("HIVEMINT_RENDER_ENGINE", "auto"),
            min_free_disk_gb=_float("MIN_FREE_DISK_GB", 50.0),
            max_hivemint_storage_gb=_float("MAX_HIVEMINT_STORAGE_GB", 20.0),
            storage_root=Path(os.getenv("HIVEMINT_STORAGE_ROOT", ROOT)),
            timezone=os.getenv("HIVEMINT_TIMEZONE", DEFAULT_TIMEZONE),
            products_dir=Path(os.getenv("HIVEMINT_PRODUCTS_DIR", ROOT / "products")),
            archive_dir=Path(os.getenv("HIVEMINT_ARCHIVE_DIR", ROOT / "archive")),
            recalc_engine=os.getenv("HIVEMINT_RECALC_ENGINE", "auto"),
            soffice_path=os.getenv("HIVEMINT_SOFFICE") or None,
            max_claude_runs_per_day=_int("MAX_CLAUDE_RUNS_PER_DAY", 15),
            max_products_built_per_day=_int("MAX_PRODUCTS_BUILT_PER_DAY", 2),
            max_research_per_shift=_int("MAX_RESEARCH_PER_SHIFT", 3),
            research_reserve_runs=_int("RESEARCH_RESERVE_RUNS", 6),
            max_products_in_progress=_int("MAX_PRODUCTS_IN_PROGRESS", 6),
            shift_start=os.getenv("SHIFT_START", "13:00"),
            shift_end=os.getenv("SHIFT_END", "22:00"),
            skip_on_battery=_bool("SKIP_ON_BATTERY", True),
            claude_bin=os.getenv("CLAUDE_BIN", "claude"),
            claude_timeout_s=_float("CLAUDE_TIMEOUT_S", 1200.0),
            opportunity_weights=_weights("OPPORTUNITY_WEIGHTS"),
            opportunity_threshold=_float("OPPORTUNITY_THRESHOLD", 65.0),
            max_opportunities_per_shift=_int("MAX_OPPORTUNITIES_PER_SHIFT", 3),
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
            "auto_rework_grants": self.auto_rework_grants,
            "redesigns_per_product": self.redesigns_per_product,
            "auto_drop_failed": self.auto_drop_failed,
            "owners_desk": ("approves" if self.desk_auto_approve else "recommends") if self.desk_enabled else "off",
            "max_claude_runs_per_day": self.max_claude_runs_per_day,
            "max_products_built_per_day": self.max_products_built_per_day,
            "max_research_per_shift": self.max_research_per_shift,
            "max_products_in_progress": self.max_products_in_progress,
            "shift_window": f"{self.shift_start}-{self.shift_end}",
            "skip_on_battery": self.skip_on_battery,
            "min_free_disk_gb": self.min_free_disk_gb,
            "max_hivemint_storage_gb": self.max_hivemint_storage_gb,
            "limits_reset": f"local midnight ({self.timezone})",
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
