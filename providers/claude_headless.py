"""Tier 2 provider: headless Claude Code (claude -p) on the owner's Max subscription.

Before every run:
- the daily run cap (MAX_CLAUDE_RUNS_PER_DAY) is checked and raises LimitReached when hit;
- the billing guard refuses (HeadlessRefused) if any pay-per-use variable such as
  ANTHROPIC_API_KEY is set, or if `claude auth status --json` does not show a claude.ai login
  with the Max subscription. Nothing can bill per token by accident.

Each run is one AgentRun (provider claude-headless, tier 2, tokens, duration, outcome) plus a
`claude.call` Event. The reported total_cost_usd is stored for the record only: the Max plan
covers it, so it is excluded from the $1/$20 cloud caps (app/budget.py). The raw JSON output is
kept under logs/headless/ for debugging. Tool permissions come from providers/headless_policy.py.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.clock import day_start
from app.db import utcnow
from app.lifecycle import LimitReached, log_event
from app.models import AgentRun, Job
from app.states import Department, JobStatus
from config.settings import ROOT, Settings
from providers.base import Completion, ProviderError, ProviderUnavailable
from providers.headless_policy import BILLING_ENV, DENY, REQUIRED_AUTH, Role

NAME = "claude-headless"
TIER = 2
AUTH_TIMEOUT_S = 60
LOG_DIR = ROOT / "logs" / "headless"


class HeadlessRefused(RuntimeError):
    """A safety guard refused to start a headless run (billing, login). Never retried automatically."""


def resolve_bin(name: str) -> str:
    """The claude executable; Task Scheduler's PATH may lack ~/.local/bin, so look there too."""
    found = shutil.which(name)
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / ("claude.exe" if os.name == "nt" else "claude")
    if fallback.is_file():
        return str(fallback)
    raise HeadlessRefused(f"HEADLESS_REFUSED: claude executable '{name}' not found (set CLAUDE_BIN)")


def child_env() -> dict[str, str]:
    """The parent environment minus anything that could switch claude to pay-per-use billing."""
    return {k: v for k, v in os.environ.items() if k not in BILLING_ENV}


def headless_runs_today(session: Session, settings: Settings) -> int:
    return session.scalar(select(func.count(AgentRun.id)).where(
        AgentRun.provider == NAME, AgentRun.status != JobStatus.CANCELLED,
        AgentRun.started_at >= day_start(settings.timezone)))


def check_subscription(settings: Settings, runner=None) -> None:
    """Raise HeadlessRefused unless claude is logged in with the Max subscription and no API key is set."""
    present = [name for name in BILLING_ENV if os.environ.get(name)]
    if present:
        raise HeadlessRefused(f"HEADLESS_REFUSED: {', '.join(present)} is set; headless runs must use the Max "
                              "subscription, never pay-per-use. Unset it and run the shift again.")
    runner = runner or subprocess.run
    try:
        result = runner([resolve_bin(settings.claude_bin), "auth", "status", "--json"], capture_output=True,
                        text=True, encoding="utf-8", timeout=AUTH_TIMEOUT_S, env=child_env(), cwd=ROOT)
        status = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as error:
        raise HeadlessRefused(f"HEADLESS_REFUSED: could not read `claude auth status` ({type(error).__name__})") \
            from error
    wrong = {k: status.get(k) for k, v in REQUIRED_AUTH.items() if status.get(k) != v}
    if wrong:
        raise HeadlessRefused(f"HEADLESS_REFUSED: claude is not logged in with the Max subscription "
                              f"(expected {REQUIRED_AUTH}, got {wrong}). Run `claude auth login`.")


class ClaudeHeadless:
    name = NAME
    tier = TIER

    def __init__(self, settings: Settings, session: Session, runner=None):
        self.settings = settings
        self.session = session
        self._runner = runner  # injectable for tests; the test suite never starts a real claude

    @property
    def runner(self):
        return self._runner or subprocess.run

    def command(self, role: Role, json_schema: dict | None) -> list[str]:
        cmd = [resolve_bin(self.settings.claude_bin), "-p", "--output-format", "json", "--restricted",
               "--permission-mode", "dontAsk", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
               "--no-session-persistence", "--tools", ",".join(role.tools)]
        if json_schema is not None:
            cmd += ["--json-schema", json.dumps(json_schema)]
        cmd += ["--allowedTools", *role.allow, "--disallowedTools", *DENY]
        return cmd

    def run(self, job: Job, department: Department, prompt: str, role: Role, *,
            response_model: type[BaseModel] | None = None, timeout_s: float | None = None) -> Completion:
        """One headless run. The prompt goes in on stdin; the answer is validated against response_model."""
        if (used := headless_runs_today(self.session, self.settings)) >= self.settings.max_claude_runs_per_day:
            raise LimitReached(f"Daily Claude headless run limit reached ({used}/"
                               f"{self.settings.max_claude_runs_per_day})")
        run = AgentRun(job_id=job.id, department=department, provider=self.name, model="", tier=self.tier,
                       cost_usd_micros=0, status=JobStatus.RUNNING)
        self.session.add(run)
        self.session.flush()
        clock = time.perf_counter()
        try:
            check_subscription(self.settings, self.runner)
        except HeadlessRefused as error:
            self._finish(run, clock, JobStatus.CANCELLED, error=str(error), role=role)
            raise
        schema = response_model.model_json_schema() if response_model is not None else None
        try:
            completion = self._call(run, role, prompt, schema, response_model, clock, timeout_s)
        except ProviderError as error:
            run.prompt_tokens, run.completion_tokens = error.prompt_tokens, error.completion_tokens
            self._finish(run, clock, JobStatus.FAILED, error=str(error), role=role)
            raise
        run.prompt_tokens, run.completion_tokens = completion.prompt_tokens, completion.completion_tokens
        self._finish(run, clock, JobStatus.SUCCEEDED, summary=completion.text[:300], role=role)
        return completion

    def _call(self, run: AgentRun, role: Role, prompt: str, schema: dict | None,
              response_model: type[BaseModel] | None, clock: float, timeout_s: float | None) -> Completion:
        try:
            result = self.runner(self.command(role, schema), input=prompt, capture_output=True, text=True,
                                 encoding="utf-8", timeout=timeout_s or self.settings.claude_timeout_s,
                                 env=child_env(), cwd=ROOT)
        except subprocess.TimeoutExpired as error:
            raise ProviderUnavailable(f"claude -p timed out after {error.timeout:.0f}s") from error
        except OSError as error:
            raise ProviderUnavailable(f"claude -p could not start: {error}") from error
        self._keep_log(run, result.stdout, result.stderr)
        try:
            payload = json.loads(result.stdout)
        except ValueError as error:
            raise ProviderError(f"claude -p exited {result.returncode} without JSON output: "
                                f"{(result.stderr or result.stdout)[:300]}") from error
        usage = payload.get("usage") or {}
        tokens = {"prompt_tokens": int(usage.get("input_tokens") or 0)
                  + int(usage.get("cache_read_input_tokens") or 0)
                  + int(usage.get("cache_creation_input_tokens") or 0),
                  "completion_tokens": int(usage.get("output_tokens") or 0)}
        run.model = ",".join(sorted(payload.get("modelUsage") or {})) or payload.get("model", "")
        run.cost_usd_micros = int(round(float(payload.get("total_cost_usd") or 0) * 1_000_000))
        if result.returncode != 0 or payload.get("is_error") or payload.get("subtype") != "success":
            raise ProviderError(f"claude -p failed ({payload.get('subtype')}, exit {result.returncode}): "
                                f"{str(payload.get('result') or result.stderr)[:300]}", **tokens)
        text = payload.get("result") or ""
        data = None
        if response_model is not None:
            raw = payload.get("structured_output")
            try:
                data = response_model.model_validate(raw if raw is not None else json.loads(_strip_fence(text)))
            except (ValueError, ValidationError) as error:
                raise ProviderError(f"claude -p output did not match {response_model.__name__}: {error}",
                                    **tokens) from error
        return Completion(text=text, model=run.model, **tokens,
                          duration_ms=int((time.perf_counter() - clock) * 1000), data=data)

    def _keep_log(self, run: AgentRun, stdout: str, stderr: str) -> None:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            (LOG_DIR / f"run-{run.id}.json").write_text(
                json.dumps({"stdout": stdout, "stderr": stderr}, indent=1), encoding="utf-8")
        except OSError:
            pass  # a debug copy is optional; the AgentRun row is the record

    def _finish(self, run: AgentRun, clock: float, status: JobStatus, *, role: Role, summary: str = "",
                error: str | None = None) -> None:
        run.status, run.result_summary, run.error = status, summary, error
        run.finished_at = utcnow()
        run.duration_ms = int((time.perf_counter() - clock) * 1000)
        log_event(self.session, "claude.call", actor=self.name, run=run, message=error or "ok",
                  data={"role": role.name, "model": run.model, "tier": run.tier, "prompt_tokens": run.prompt_tokens,
                        "completion_tokens": run.completion_tokens, "duration_ms": run.duration_ms,
                        "reported_cost_usd_micros": run.cost_usd_micros, "status": status.value})


def _strip_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return text
