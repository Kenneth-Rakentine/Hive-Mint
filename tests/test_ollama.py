import json

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from agents.operations import OperationsManager
from app.models import AgentRun, Event
from app.states import Department, JobStatus
from providers.base import ProviderError, ProviderUnavailable
from providers.ollama import OllamaProvider
from tests.conftest import ollama_client, ollama_down, ollama_reply


class Answer(BaseModel):
    verdict: str
    score: int


@pytest.fixture
def job(session, settings, make_product):
    return OperationsManager(session, settings).next_job(make_product())


def test_successful_call_is_logged_as_a_free_tier1_agent_run(session, settings, job):
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return ollama_reply({"verdict": "clear", "score": 88}, prompt_tokens=321, completion_tokens=54)

    provider = OllamaProvider(settings, session, client=ollama_client(handler))
    result = provider.generate(job, Department.QA, "Review this", system="Be brief", response_model=Answer)

    assert result.data == Answer(verdict="clear", score=88)
    assert seen["model"] == settings.ollama_model == "qwen3.5:4b"
    assert seen["stream"] is False and seen["think"] is False
    assert seen["format"]["properties"].keys() == {"verdict", "score"}
    assert [m["role"] for m in seen["messages"]] == ["system", "user"]

    run = session.scalar(select(AgentRun).where(AgentRun.job_id == job.id))
    assert (run.provider, run.model, run.tier, run.cost_usd_micros) == ("ollama", "qwen3.5:4b", 1, 0)
    assert (run.prompt_tokens, run.completion_tokens) == (321, 54)
    assert run.status == JobStatus.SUCCEEDED and run.duration_ms is not None and run.finished_at
    assert session.scalar(select(Event).where(Event.event_type == "ollama.call")).agent_run_id == run.id


def test_ollama_off_raises_unavailable_and_logs_a_failed_run(session, settings, job):
    provider = OllamaProvider(settings, session, client=ollama_down())
    with pytest.raises(ProviderUnavailable, match="OLLAMA_UNAVAILABLE"):
        provider.generate(job, Department.QA, "hello")
    run = session.scalar(select(AgentRun).where(AgentRun.job_id == job.id))
    assert run.status == JobStatus.FAILED and "OLLAMA_UNAVAILABLE" in run.error and run.cost_usd_micros == 0


@pytest.mark.parametrize("reply", [
    lambda: ollama_reply("not json at all"),
    lambda: ollama_reply({"verdict": "clear"}),  # missing score
    lambda: ollama_reply({}, status=500),
])
def test_bad_output_is_a_provider_error(session, settings, job, reply):
    provider = OllamaProvider(settings, session, client=ollama_client(lambda request: reply()))
    with pytest.raises(ProviderError) as caught:
        provider.generate(job, Department.QA, "hello", response_model=Answer)
    assert not isinstance(caught.value, ProviderUnavailable)
    assert session.scalar(select(AgentRun).where(AgentRun.job_id == job.id)).status == JobStatus.FAILED
