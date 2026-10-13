from sqlalchemy import select

from app import cli
from app.db import make_engine, make_session_factory
from app.models import Product
from app.states import ProductStatus


def test_demo_reaches_human_review(settings, monkeypatch):
    engine = make_engine(settings.db_url)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "get_engine", lambda: engine)

    assert cli.main(["demo"]) == 0
    assert cli.main(["demo"]) == 1  # refuses to duplicate the sample product
    with make_session_factory(engine)() as session:
        product = session.scalar(select(Product).where(Product.slug == cli.DEMO["slug"]))
        assert product.status == ProductStatus.HUMAN_REVIEW
        assert product.name == "Mobile Auto Detailer Income & Expense Tracker"
    assert cli.main(["status"]) == 0
    engine.dispose()
