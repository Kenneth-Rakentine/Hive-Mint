from pathlib import Path

import pytest

from app.db import init_db, make_engine, make_session_factory
from app.lifecycle import create_product, transition
from app.states import PIPELINE, ProductStatus
from config.settings import Settings


@pytest.fixture
def settings(tmp_path):
    return Settings(db_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
                    products_dir=tmp_path / "products", archive_dir=tmp_path / "archive",
                    storage_root=tmp_path, ollama_models_dir=tmp_path / "ollama-models",
                    min_free_disk_gb=0, max_hivemint_storage_gb=1000,
                    render_engine="none",  # tests never start Excel; test_render.py opts in explicitly
                    recalc_engine="formulas",  # same engine on every machine, with or without LibreOffice
                    ollama_url="http://127.0.0.1:9")  # nothing listens here: tests never reach a real Ollama


@pytest.fixture
def engine(settings):
    engine = make_engine(settings.db_url)
    init_db(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine):
    with make_session_factory(engine)() as session:
        yield session


@pytest.fixture
def make_product(session, settings):
    counter = iter(range(1, 10_000))

    def _make(name="Sample Tracker", product_type="spreadsheet"):
        n = next(counter)
        product = create_product(session, settings, slug=f"sample-{n}", name=f"{name} {n}", product_type=product_type)
        session.flush()
        return product

    return _make


def advance_to(session, settings, product, target: ProductStatus):
    """Walk a product forward along the pipeline, one legal gate at a time."""
    while product.status != target:
        nxt = PIPELINE[PIPELINE.index(product.status) + 1]
        transition(session, settings, product, nxt, actor="test", reason=f"test pass {product.status}")
    return product


SAMPLE_SLUG = "mobile-auto-detailer-income-expense-tracker"


@pytest.fixture(scope="session")
def sample_spec():
    from tools.spec import load_spec

    return load_spec(SAMPLE_SLUG)


@pytest.fixture(scope="session")
def good_build(tmp_path_factory, sample_spec):
    """One clean build of the sample product (files + ZIP), shared read-only by the session."""
    from tools.builders import BUILDERS
    from tools.packaging import make_zip

    out = tmp_path_factory.mktemp("good") / "v1"
    make_zip(out / sample_spec["zip_name"], BUILDERS[sample_spec["builder"]](sample_spec, out))
    return out


@pytest.fixture(scope="session")
def good_checks(sample_spec, good_build):
    """Every Testing check run once against the clean build (recalculation is slow)."""
    from agents.testing import run_checks

    return run_checks(sample_spec, good_build)


@pytest.fixture
def build_copy(tmp_path, good_build):
    """A private, writable copy of the clean build for tests that break something."""
    import shutil

    return Path(shutil.copytree(good_build, tmp_path / "copy" / "v1"))


@pytest.fixture
def sample_product(session, settings):
    product = create_product(session, settings, slug=SAMPLE_SLUG, name="Mobile Auto Detailer Income & Expense Tracker",
                             product_type="spreadsheet")
    session.flush()
    return product


# --------------------------------------------------------------------------- Ollama, always mocked

def record_build(session, product, build_dir, spec, version=1):
    """Record an existing build folder as the product's build, as Production would."""
    from app.models import Build, Job
    from app.states import Department, JobStatus
    from tools.packaging import sha256

    job = Job(product_id=product.id, department=Department.PRODUCTION, job_type="build_product",
              for_status=ProductStatus.PRODUCTION, status=JobStatus.SUCCEEDED)
    session.add(job)
    session.flush()
    build = Build(product_id=product.id, job_id=job.id, version=version, spec_slug=product.slug,
                  path=str(build_dir), zip_path=str(build_dir / spec["zip_name"]),
                  zip_sha256=sha256(build_dir / spec["zip_name"]))
    session.add(build)
    session.flush()
    return build


def ollama_reply(content, *, prompt_tokens=120, completion_tokens=40, status=200, done_reason="stop"):
    """A canned /api/chat response. content may be a dict (sent as JSON text) or a string."""
    import json

    import httpx

    text = content if isinstance(content, str) else json.dumps(content)
    return httpx.Response(status, json={"model": "qwen3.5:4b", "message": {"role": "assistant", "content": text},
                                        "done": True, "done_reason": done_reason, "prompt_eval_count": prompt_tokens,
                                        "eval_count": completion_tokens})


def ollama_client(handler):
    """An httpx client whose requests go to handler(request) instead of a real Ollama."""
    import httpx

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://ollama.test")


def ollama_down():
    import httpx

    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    return ollama_client(refuse)


# --------------------------------------------------------------------------- headless Claude, always mocked

MAX_AUTH = {"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max", "apiProvider": "firstParty"}


class FakeClaude:
    """Stands in for subprocess.run when ClaudeHeadless calls `claude`. Never starts a real process.

    replies: a list consumed one per `claude -p` call; each item is a dict merged into a successful
    result payload (e.g. {"structured_output": {...}}), an Exception to raise, or a callable
    (cmd, prompt) -> dict, which may also write files as a real run would.
    """

    def __init__(self, replies=(), auth=None):
        self.replies = list(replies)
        self.auth = MAX_AUTH if auth is None else auth
        self.calls = []  # (cmd, prompt) for each claude -p call

    def __call__(self, cmd, input=None, **kwargs):
        import json
        import subprocess

        if "auth" in cmd:
            return subprocess.CompletedProcess(cmd, 0, json.dumps(self.auth), "")
        self.calls.append((cmd, input))
        if not self.replies:
            raise AssertionError("FakeClaude: unexpected extra claude -p call")
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        if callable(reply):
            reply = reply(cmd, input)
        payload = {"type": "result", "subtype": "success", "is_error": False, "duration_ms": 1234,
                   "result": "done", "total_cost_usd": 0.42,
                   "usage": {"input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": 50},
                   "modelUsage": {"claude-opus-5-5": {}}} | reply
        return subprocess.CompletedProcess(cmd, payload.pop("_exit", 0), json.dumps(payload), "")


@pytest.fixture
def fake_claude(monkeypatch, tmp_path):
    """Patch every ClaudeHeadless to use one FakeClaude; set .replies before the code runs."""
    from providers import claude_headless

    fake = FakeClaude()
    monkeypatch.setattr(claude_headless.subprocess, "run", fake)
    monkeypatch.setattr(claude_headless, "resolve_bin", lambda name: "claude")
    monkeypatch.setattr(claude_headless, "LOG_DIR", tmp_path / "headless-logs")
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                 "CLAUDE_CODE_USE_FOUNDRY", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    return fake
