import pytest

from app.db import init_db, make_engine, make_session_factory
from app.lifecycle import create_product, transition
from app.states import PIPELINE, ProductStatus
from config.settings import Settings


@pytest.fixture
def settings(tmp_path):
    return Settings(db_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}")


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
