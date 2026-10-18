import subprocess

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from app.budget import spend_today
from app.lifecycle import LimitReached
from app.models import AgentRun, Event, Job
from app.states import Department, JobStatus, ProductStatus
from providers import headless_policy as policy
from providers.base import ProviderError, ProviderUnavailable
from providers.claude_headless import ClaudeHeadless, HeadlessRefused, headless_runs_today


class Answer(BaseModel):
    verdict: str
    count: int


@pytest.fixture
def job(session, make_product):
    product = make_product()
    job = Job(product_id=product.id, department=Department.RESEARCH, job_type="t", for_status=ProductStatus.RESEARCH)
    session.add(job)
    session.flush()
    return job


def _run(settings, session, job, **kwargs):
    return ClaudeHeadless(settings, session).run(job, Department.RESEARCH, "prompt text", policy.RESEARCH,
                                                 response_model=Answer, **kwargs)


def test_successful_run_is_logged_with_tokens_duration_and_reported_cost(settings, session, job, fake_claude):
    fake_claude.replies = [{"structured_output": {"verdict": "ok", "count": 2}}]
    completion = _run(settings, session, job)
    assert completion.data == Answer(verdict="ok", count=2)
    run = session.scalar(select(AgentRun))
    assert (run.provider, run.tier, run.status, run.model) == ("claude-headless", 2, JobStatus.SUCCEEDED,
                                                                "claude-opus-5-5")
    assert (run.prompt_tokens, run.completion_tokens, run.cost_usd_micros) == (1050, 200, 420_000)
    assert run.duration_ms is not None and run.finished_at is not None
    event = session.scalar(select(Event).where(Event.event_type == "claude.call"))
    assert '"role": "research"' in event.data_json
    # Subscription runs are recorded but never count against the $1/$20 cloud caps.
    assert spend_today(session, settings) == 0
    assert fake_claude.calls[0][1] == "prompt text"  # prompt goes in on stdin


def test_command_line_is_restricted_and_has_no_mcp(settings, session, job, fake_claude):
    fake_claude.replies = [{"structured_output": {"verdict": "ok", "count": 1}}]
    _run(settings, session, job)
    cmd = fake_claude.calls[0][0]
    for flag in ("-p", "--restricted", "--strict-mcp-config", "--no-session-persistence"):
        assert flag in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    assert cmd[cmd.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert cmd[cmd.index("--tools") + 1] == "WebSearch,WebFetch"
    allowed = cmd[cmd.index("--allowedTools") + 1:cmd.index("--disallowedTools")]
    denied = cmd[cmd.index("--disallowedTools") + 1:]
    assert allowed == ["WebSearch", "WebFetch"]
    assert "Bash(git *)" in denied and "PowerShell" in denied and "Bash(rm *)" in denied
    assert "--json-schema" in cmd


def test_roles_stay_inside_the_ceiling_and_never_reach_outside_the_repo():
    for role in policy.ROLES.values():
        assert set(role.tools) <= policy.CEILING_TOOLS and set(role.allow) <= policy.CEILING_ALLOW
        for rule in role.allow:
            assert "//" not in rule and "~" not in rule and "Desktop" not in rule and ".." not in rule
    assert "Bash" not in policy.RED_TEAM.tools and "Bash" not in policy.RESEARCH.tools
    with pytest.raises(ValueError):
        policy.Role("bad", tools=("Bash",), allow=("Bash(git push *)",))


def test_api_key_in_environment_refuses_and_records_it(settings, session, job, fake_claude, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    fake_claude.replies = [{"structured_output": {"verdict": "ok", "count": 1}}]
    with pytest.raises(HeadlessRefused, match="ANTHROPIC_API_KEY"):
        _run(settings, session, job)
    assert not fake_claude.calls  # claude -p never started
    run = session.scalar(select(AgentRun))
    assert run.status == JobStatus.CANCELLED and "ANTHROPIC_API_KEY" in run.error
    assert headless_runs_today(session, settings) == 0  # a refusal does not use up the daily cap


@pytest.mark.parametrize("auth", [
    {"loggedIn": False},
    {"loggedIn": True, "authMethod": "api_key", "subscriptionType": None, "apiProvider": "firstParty"},
    {"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "pro", "apiProvider": "firstParty"},
])
def test_not_logged_in_with_max_refuses(settings, session, job, fake_claude, auth):
    fake_claude.auth = auth
    fake_claude.replies = [{"structured_output": {"verdict": "ok", "count": 1}}]
    with pytest.raises(HeadlessRefused, match="Max subscription"):
        _run(settings, session, job)
    assert not fake_claude.calls


def test_child_environment_never_carries_billing_variables(settings, session, job, monkeypatch):
    seen = {}

    def runner(cmd, **kwargs):
        seen.setdefault("envs", []).append(kwargs["env"])
        import json
        if "auth" in cmd:
            return subprocess.CompletedProcess(cmd, 0, json.dumps(policy.REQUIRED_AUTH), "")
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"subtype": "success", "result": "x",
                                                               "structured_output": {"verdict": "a", "count": 1}}), "")

    monkeypatch.setattr("providers.claude_headless.resolve_bin", lambda name: "claude")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "")  # empty is not "set", but must still be stripped
    ClaudeHeadless(settings, session, runner=runner).run(job, Department.RESEARCH, "p", policy.RESEARCH,
                                                         response_model=Answer)
    for env in seen["envs"]:
        assert not set(policy.BILLING_ENV) & set(env)


def test_daily_run_cap_raises_before_starting(settings, session, job, fake_claude):
    from dataclasses import replace

    capped = replace(settings, max_claude_runs_per_day=2)
    fake_claude.replies = [{"structured_output": {"verdict": "ok", "count": 1}}] * 2
    _run(capped, session, job)
    _run(capped, session, job)
    with pytest.raises(LimitReached, match="2/2"):
        _run(capped, session, job)
    assert len(fake_claude.calls) == 2


def test_timeout_is_provider_unavailable_and_logged(settings, session, job, fake_claude):
    fake_claude.replies = [subprocess.TimeoutExpired("claude", 1200)]
    with pytest.raises(ProviderUnavailable, match="timed out"):
        _run(settings, session, job)
    assert session.scalar(select(AgentRun)).status == JobStatus.FAILED


def test_error_result_and_malformed_output_raise_provider_error(settings, session, job, fake_claude):
    fake_claude.replies = [{"subtype": "error_max_turns", "is_error": True, "_exit": 1},
                           {"structured_output": {"verdict": "ok"}},  # missing count
                           {"result": "not json at all"}]
    for match in ("error_max_turns", "did not match Answer", "did not match Answer"):
        with pytest.raises(ProviderError, match=match):
            _run(settings, session, job)
    runs = session.scalars(select(AgentRun)).all()
    assert [r.status for r in runs] == [JobStatus.FAILED] * 3
    assert runs[0].prompt_tokens == 1050  # tokens kept on failure


def test_fenced_json_result_is_accepted_without_structured_output(settings, session, job, fake_claude):
    fake_claude.replies = [{"result": '```json\n{"verdict": "fine", "count": 3}\n```'}]
    assert _run(settings, session, job).data.count == 3
