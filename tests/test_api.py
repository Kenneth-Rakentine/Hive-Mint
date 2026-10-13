from fastapi.testclient import TestClient

from agents.operations import OperationsManager
from app.api import create_app
from app.db import init_db, make_engine, make_session_factory
from app.lifecycle import create_product


def test_health(settings):
    with TestClient(create_app(settings)) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "ok"}


def test_status_reflects_database(settings):
    engine = make_engine(settings.db_url)
    init_db(engine)
    with make_session_factory(engine)() as session:
        product = create_product(session, settings, slug="tracker", name="Tracker", product_type="spreadsheet")
        session.flush()
        OperationsManager(session, settings).next_job(product)
        session.commit()
    engine.dispose()

    with TestClient(create_app(settings)) as client:
        data = client.get("/status").json()
    assert data["products_by_state"]["IDEA"] == 1
    assert data["products_by_state"]["PUBLISHED"] == 0
    assert len(data["products_by_state"]) == 18
    assert data["jobs_by_status"]["QUEUED"] == 1
    assert data["queued_jobs"][0]["department"] == "OPERATIONS"
    assert data["queued_jobs"][0]["product"] == "Tracker"
    assert data["limits"]["max_publications_per_day"] == 0
    assert data["cloud_spend_usd"] == {"today": "0.00", "month": "0.00"}
