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
                    min_free_disk_gb=0, max_hivemint_storage_gb=1000)


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

def ollama_reply(content, *, prompt_tokens=120, completion_tokens=40, status=200):
    """A canned /api/chat response. content may be a dict (sent as JSON text) or a string."""
    import json

    import httpx

    text = content if isinstance(content, str) else json.dumps(content)
    return httpx.Response(status, json={"model": "qwen3.5:4b", "message": {"role": "assistant", "content": text},
                                        "done": True, "prompt_eval_count": prompt_tokens,
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
