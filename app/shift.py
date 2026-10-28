"""The shift orchestrator: one unattended run of the whole company (python -m app.cli shift).

Pre-flight, in order: skip on battery (SKIP_ON_BATTERY), stay inside the shift window
(SHIFT_START-SHIFT_END local time), the headless billing guard, the disk guard.

Then the steps run in a fixed order:
  research -> finance -> spec -> production -> testing -> qa (incl. Visual QA) -> red_team
  -> rework -> compliance (Compliance and SEO, ending in HUMAN_REVIEW) -> desk (the owner's desk:
  approve to READY_TO_PUBLISH, send back, or leave for the owner)
and production..desk repeat while products keep moving, so a reworked product is rebuilt
and reviewed again in the same shift.

Before each step the window is checked: when it has ended, the shift stops cleanly after the step
that was running. A hard limit (daily Claude runs, ideas, the billing guard) stops the shift the
moment it is hit; the interrupted job goes back to the queue. All state lives in the database, so
the next shift simply continues. Publications stay at 0: nothing here publishes; the owner uploads.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agents import red_team
from agents.owner_desk import OwnerDesk, pending_products
from agents.finance import select_opportunities
from agents.operations import HARD_STOPS, OperationsManager
from agents.research import ResearchFailed, opportunity_for, run_niche_research
from agents.scout import ScoutFailed, next_candidate, run_scout, scout_due
from app.db import utcnow
from app.lifecycle import auto_resume_rework, log_event
from app.models import AgentRun, Job, Opportunity, Product, ResearchRecord, Shift
from app.states import JobStatus, ProductStatus
from config.niches import RESEARCH_REFRESH_DAYS, SEED_NICHES
from config.settings import Settings
from providers.claude_headless import NAME as HEADLESS, check_subscription, headless_runs_today
from tools.disk import check_disk, disk_report
from tools.power import on_battery

S = ProductStatus
ACTOR = "shift"
STEPS = ("research", "finance", "spec", "production", "testing", "qa", "red_team", "rework", "compliance", "desk")
REPEATING = STEPS[3:]
MAX_ROUNDS = 6  # production..compliance passes per shift; each rework cycle needs one
MAX_TICKS = 20  # per gate, a safety bound
# Started but not finished: counts toward MAX_PRODUCTS_IN_PROGRESS.
IN_PROGRESS = (S.SPECIFICATION, S.PRODUCTION, S.TESTING, S.QA, S.RED_TEAM, S.COMPLIANCE, S.SEO, S.REWORK)


def in_window(settings: Settings, now: datetime) -> bool:
    local = now.astimezone(ZoneInfo(settings.timezone)).time()
    start, end = (time.fromisoformat(t) for t in (settings.shift_start, settings.shift_end))
    return start <= local < end if start <= end else (local >= start or local < end)


@dataclass
class ShiftRunner:
    session: Session
    settings: Settings
    now: Callable[[], datetime] = utcnow
    battery: Callable[[], bool | None] | None = None  # default: tools.power.on_battery
    ignore_window: bool = False
    ops: OperationsManager | None = None
    researcher: Callable = run_niche_research
    scout: Callable = run_scout
    reviewer: Callable = red_team.run_headless
    desk: Callable | None = None  # (session, product) -> DeskResult; default agents.owner_desk.OwnerDesk
    subscription_check: Callable = check_subscription
    log: list[dict] = field(default_factory=list)
    _created_last: int = 0
    errors: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.ops = self.ops or OperationsManager(self.session, self.settings)
        self.battery = self.battery or on_battery
        self.desk = self.desk or OwnerDesk(self.settings).review

    # ------------------------------------------------------------------ the run

    def run(self) -> Shift:
        shift = Shift(started_at=self.now())
        self.session.add(shift)
        log_event(self.session, "shift.started", actor=ACTOR, message="Shift started",
                  data={"ignore_window": self.ignore_window})
        self.session.commit()
        self._runs_before = self._headless_total()
        self._recover_interrupted()
        self._auto_resume()
        try:
            stop = self._preflight() or self._steps()
        except Exception as error:  # a bug, not a limit: record it and stop cleanly
            self.session.rollback()
            self.errors.append(f"{type(error).__name__}: {error}")
            stop = f"error: {type(error).__name__}: {error}"
        return self._finish(shift, stop or "done: no work left")

    def _preflight(self) -> str | None:
        if self.settings.skip_on_battery and self.battery() is True:
            return "skipped: on battery"
        if not self.ignore_window and not in_window(self.settings, self.now()):
            return f"skipped: outside the shift window ({self.settings.shift_start}-{self.settings.shift_end})"
        try:
            self.subscription_check(self.settings)
        except HARD_STOPS as error:
            self.errors.append(str(error))
            return f"stopped: {error}"
        if problem := check_disk(self.settings):
            return f"stopped: {problem}"
        return None

    def _steps(self) -> str | None:
        # Research and Finance repeat until an idea passes Finance, the per-shift research limit is
        # reached, or too few Claude runs would be left to build what research finds.
        for round_ in range(max(1, self.settings.max_research_per_shift)):
            if round_ and not self._worth_another_niche():
                break
            for name in ("research", "finance"):
                if stop := self._step(name):
                    return stop
            if self._created_last or not self.log[-2]["ran"]:
                break
        if stop := self._step("spec"):
            return stop
        for _ in range(MAX_ROUNDS):
            moved = 0
            for name in REPEATING:
                if stop := self._step(name):
                    return stop
                moved += self.log[-1]["ran"]
            if not moved:
                return None
        return None

    def _step(self, name: str) -> str | None:
        """Run one step unless the window has closed. Returns a stop reason, or None to go on."""
        if not self.ignore_window and not in_window(self.settings, self.now()):
            return f"stopped: the shift window ended ({self.settings.shift_end}) before {name}"
        entry = {"step": name, "ran": 0, "summary": "", "started": self.now().isoformat()}
        self.log.append(entry)
        try:
            entry["ran"], entry["summary"] = getattr(self, f"_do_{name}")()
        except HARD_STOPS as error:
            self.session.commit()
            entry["summary"] = f"stopped: {error}"
            return f"stopped at {name}: {error}"
        finally:
            entry["finished"] = self.now().isoformat()
            self.session.commit()
        return None

    # ------------------------------------------------------------------ steps

    def _in_progress(self) -> int:
        return self.session.scalar(select(func.count(Product.id)).where(Product.status.in_(IN_PROGRESS)))

    def _busy(self) -> str | None:
        count, limit = self._in_progress(), self.settings.max_products_in_progress
        if limit and count >= limit:
            return f"{count} products in progress (limit {limit}); finishing those before starting new ones"
        return None

    def _do_research(self) -> tuple[int, str]:
        """Pick one niche and research it in depth. The best NEW scout candidate comes first; when none is
        left and the last scout is a week old, scout for new ones; the seed niches are the fallback."""
        if busy := self._busy():
            return 0, f"skipped: {busy}"
        note = ""
        candidate = next_candidate(self.session)
        if candidate is None and scout_due(self.session, self.now()):
            try:
                found = self.scout(self.session, self.settings)
                note = f"scouted {len(found)} new niche(s); "
            except ScoutFailed as error:
                self.errors.append(f"scout: {error}")
                note = "scout failed; "
            self.session.commit()
            candidate = next_candidate(self.session)
        since = self.now() - timedelta(days=RESEARCH_REFRESH_DAYS)
        recent = set(self.session.scalars(select(ResearchRecord.niche).where(ResearchRecord.created_at >= since)))
        niche = candidate.niche if candidate is not None else next((n for n in SEED_NICHES if n not in recent), None)
        if niche is None:
            return 0, note + f"no strong scout candidate, and every seed niche was researched in the last " \
                             f"{RESEARCH_REFRESH_DAYS} days"
        try:
            record = self.researcher(self.session, self.settings, niche)
        except ResearchFailed as error:
            self.errors.append(f"research: {error}")
            if candidate is not None:  # don't retry the same candidate every shift
                candidate.status = candidate.SKIPPED
            return 0, note + f"research failed for '{niche}': {error}"
        count = self.session.scalar(select(func.count(Opportunity.id)).where(Opportunity.research_id == record.id))
        return 1, note + f"'{niche}': {count} opportunities, {record.confidence} confidence (record #{record.id})"

    def _worth_another_niche(self) -> bool:
        left = self.settings.max_claude_runs_per_day - headless_runs_today(self.session, self.settings)
        return left >= self.settings.research_reserve_runs

    def _do_finance(self) -> tuple[int, str]:
        if busy := self._busy():
            self._created_last = 0
            moved = self._drain({S.IDEA, S.RESEARCH, S.FINANCE_REVIEW})
            return moved, f"no new products: {busy}; {moved} gate job(s)"
        created = select_opportunities(self.session, self.settings)
        self._created_last = len(created)
        self.session.commit()
        moved = self._drain({S.IDEA, S.RESEARCH, S.FINANCE_REVIEW})
        names = ", ".join(p.name for p in created) or "none"
        return len(created) + moved, f"{len(created)} new product(s) from opportunities ({names}); {moved} gate job(s)"

    def _do_spec(self) -> tuple[int, str]:
        waiting = self.session.scalars(select(Product).where(Product.status == S.SPECIFICATION)).all()
        waiting.sort(key=lambda p: -((opportunity_for(self.session, p) or Opportunity(score=100)).score or 0))
        room = max(0, self.settings.max_products_built_per_day - self.ops.new_products_built_today()
                   - self._unbuilt_in_production())
        ran = 0
        for product in waiting[:room]:
            job = self.ops.next_job(product)
            if job is not None:
                self.session.commit()
                try:
                    self.ops.run_job(job)
                finally:
                    self.session.commit()
                ran += 1
        held = len(waiting) - ran
        return ran, f"{ran} spec(s) written" + (f"; {held} wait for build room" if held > 0 else "")

    def _do_production(self) -> tuple[int, str]:
        return self._gate({S.PRODUCTION}, "build(s)")

    def _do_testing(self) -> tuple[int, str]:
        return self._gate({S.TESTING}, "test run(s)")

    def _do_qa(self) -> tuple[int, str]:
        return self._gate({S.QA}, "QA review(s), including Visual QA")

    def _do_red_team(self) -> tuple[int, str]:
        products = self.session.scalars(select(Product).where(Product.status == S.RED_TEAM)
                                        .order_by(Product.id)).all()
        decisions = []
        for product in products:
            review = self.reviewer(self.session, self.settings, product)
            self.session.commit()
            decisions.append(f"#{product.id} {review.decision if review else 'ERROR'}")
        return len(products), ", ".join(decisions) or "nothing to review"

    def _do_rework(self) -> tuple[int, str]:
        return self._gate({S.REWORK}, "rework plan(s)")

    def _do_compliance(self) -> tuple[int, str]:
        return self._gate({S.COMPLIANCE, S.SEO}, "compliance/SEO job(s)")

    def _do_desk(self) -> tuple[int, str]:
        decisions = []
        for product in pending_products(self.session, self.settings):
            result = self.desk(self.session, product)
            self.session.commit()
            decisions.append(f"#{product.id} {result.decision}")
        return len(decisions), ", ".join(decisions) or "nothing for the owner's desk"

    # ------------------------------------------------------------------ helpers

    def _recover_interrupted(self) -> None:
        """A shift killed mid-job (Task Scheduler's time limit, a crash, a shutdown) leaves RUNNING rows behind.
        Only one shift runs at a time, so they are stale: requeue product jobs (the attempt is given back) and
        close everything else, so the next tick can pick the work up again."""
        for job in self.session.scalars(select(Job).where(Job.status == JobStatus.RUNNING)).all():
            for run in job.runs:
                if run.status == JobStatus.RUNNING:
                    run.status, run.error, run.finished_at = JobStatus.FAILED, "interrupted", self.now()
            if job.product_id is None:
                job.status, job.error, job.finished_at = JobStatus.CANCELLED, "interrupted", self.now()
            else:
                job.status, job.started_at = JobStatus.QUEUED, None
                job.attempts = max(0, job.attempts - 1)
            log_event(self.session, "job.recovered", actor=ACTOR, job=job,
                      message=f"Interrupted job found at shift start; now {job.status.value}")
        self.session.commit()

    def _auto_resume(self) -> None:
        for product in self.session.scalars(select(Product).where(Product.status == S.PAUSED)).all():
            auto_resume_rework(self.session, self.settings, product)
        self.session.commit()

    def _gate(self, statuses: set[ProductStatus], label: str) -> tuple[int, str]:
        ran = self._drain(statuses)
        return ran, f"{ran} {label}"

    def _drain(self, statuses: set[ProductStatus]) -> int:
        total = 0
        for _ in range(MAX_TICKS):
            ran = self.ops.tick(statuses)
            if not ran:
                break
            total += ran
        return total

    def _unbuilt_in_production(self) -> int:
        products = self.session.scalars(select(Product).where(Product.status == S.PRODUCTION)).all()
        return sum(1 for p in products if not self.ops.has_build(p))

    def _headless_total(self) -> int:
        return self.session.scalar(select(func.count(AgentRun.id)).where(AgentRun.provider == HEADLESS))

    def _finish(self, shift: Shift, stop: str) -> Shift:
        shift.ended_at = self.now()
        shift.stop_reason = stop
        shift.steps_json = json.dumps(self.log)
        shift.claude_runs = self._headless_total() - self._runs_before
        shift.errors_json = json.dumps(self.errors)
        shift.summary_json = json.dumps({
            "claude_runs_today": headless_runs_today(self.session, self.settings),
            "claude_runs_cap": self.settings.max_claude_runs_per_day,
            "disk": disk_report(self.settings).as_dict(self.settings)}, default=str)
        log_event(self.session, "shift.finished", actor=ACTOR, message=stop,
                  data={"shift_id": shift.id, "steps": len(self.log), "claude_runs": shift.claude_runs})
        self.session.commit()
        return shift
