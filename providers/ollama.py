"""Tier 1 provider: a local Ollama server (default model qwen3.5:4b), free to run.

Every call, successful or not, is logged as its own AgentRun (provider, model, tier, token
counts, duration, $0) plus an `ollama.call` Event. If Ollama is off the call raises
ProviderUnavailable; the Operations Manager's bounded job retries take it from there.
"""

from __future__ import annotations

import base64
import json
import time

import httpx
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from app.db import utcnow
from app.lifecycle import log_event
from app.models import AgentRun, Job
from app.states import Department, JobStatus
from config.settings import Settings
from providers.base import Completion, ProviderError, ProviderUnavailable

TIER = 1


class OllamaProvider:
    name = "ollama"
    tier = TIER

    def __init__(self, settings: Settings, session: Session, client: httpx.Client | None = None):
        self.settings = settings
        self.session = session
        self.model = settings.ollama_model
        self.client = client or httpx.Client(base_url=settings.ollama_url, timeout=settings.ollama_timeout_s)

    def generate(self, job: Job, department: Department, prompt: str, *, system: str = "",
                 response_model: type[BaseModel] | None = None, images: list[bytes] | None = None) -> Completion:
        """One chat call. With response_model, the output is constrained to its JSON schema and validated.
        images (PNG bytes) go to a vision-capable model with the prompt."""
        user = {"role": "user", "content": prompt}
        if images:
            user["images"] = [base64.b64encode(image).decode("ascii") for image in images]
        messages = ([{"role": "system", "content": system}] if system else []) + [user]
        body = {"model": self.model, "messages": messages, "stream": False, "think": False,
                "options": {"temperature": 0, "num_ctx": self.settings.ollama_num_ctx}}
        if response_model is not None:
            body["format"] = response_model.model_json_schema()

        run = AgentRun(job_id=job.id, department=department, provider=self.name, model=self.model, tier=self.tier,
                       cost_usd_micros=0, status=JobStatus.RUNNING)
        self.session.add(run)
        self.session.flush()
        clock = time.perf_counter()
        try:
            completion = self._call(body, response_model, clock)
        except ProviderError as error:
            run.prompt_tokens, run.completion_tokens = error.prompt_tokens, error.completion_tokens
            self._finish(run, clock, JobStatus.FAILED, error=str(error))
            raise
        run.prompt_tokens, run.completion_tokens = completion.prompt_tokens, completion.completion_tokens
        self._finish(run, clock, JobStatus.SUCCEEDED, summary=completion.text[:200])
        return completion

    def _call(self, body: dict, response_model: type[BaseModel] | None, clock: float) -> Completion:
        try:
            response = self.client.post("/api/chat", json=body)
        except (httpx.ConnectError, httpx.TimeoutException) as error:
            raise ProviderUnavailable(f"OLLAMA_UNAVAILABLE: cannot reach Ollama at {self.settings.ollama_url} "
                                      f"({type(error).__name__}). Start Ollama, then resume the product.") from error
        except httpx.HTTPError as error:
            raise ProviderError(f"Ollama request failed: {type(error).__name__}: {error}") from error
        if response.status_code != 200:
            raise ProviderError(f"Ollama returned HTTP {response.status_code}: {response.text[:300]}")
        try:
            payload = response.json()
            text = payload["message"]["content"]
        except (ValueError, KeyError, TypeError) as error:
            raise ProviderError(f"Ollama response was not a chat message: {response.text[:300]}") from error

        tokens = {"prompt_tokens": int(payload.get("prompt_eval_count") or 0),
                  "completion_tokens": int(payload.get("eval_count") or 0)}
        data = None
        if response_model is not None:
            try:
                data = response_model.model_validate(json.loads(text))
            except (ValueError, ValidationError) as error:
                cut = " (output cut off at the context limit: raise OLLAMA_NUM_CTX)" \
                    if payload.get("done_reason") == "length" else ""
                raise ProviderError(f"Model output did not match {response_model.__name__}{cut}: {error}",
                                    **tokens) from error
        return Completion(text=text, model=payload.get("model", self.model), **tokens,
                          duration_ms=int((time.perf_counter() - clock) * 1000), data=data)

    def _finish(self, run: AgentRun, clock: float, status: JobStatus, *, summary: str = "",
                error: str | None = None) -> None:
        run.status, run.result_summary, run.error = status, summary, error
        run.finished_at = utcnow()
        run.duration_ms = int((time.perf_counter() - clock) * 1000)
        log_event(self.session, "ollama.call", actor=self.name, run=run, message=error or "ok",
                  data={"model": run.model, "tier": run.tier, "prompt_tokens": run.prompt_tokens,
                        "completion_tokens": run.completion_tokens, "duration_ms": run.duration_ms,
                        "cost_usd_micros": 0, "status": status.value})
