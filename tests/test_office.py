"""The pixel office shows database state and nothing else."""

from fastapi.testclient import TestClient

from app.api import create_app
from app.lifecycle import transition
from app.models import Job
from app.office import BUILDINGS, office_state
from app.states import Department, JobStatus, ProductStatus as S
from tests.conftest import advance_to


def _building(state, building_id):
    return next(b for b in state["buildings"] if b["id"] == building_id)


def test_every_status_has_exactly_one_building():
    placed = [status for _, _, _, statuses in BUILDINGS for status in statuses]
    assert sorted(placed) == sorted(S) and len(placed) == len(set(placed))


def test_an_empty_company_shows_nobody_working(session, settings):
    state = office_state(session, settings)
    assert state["shift"] is None and state["events"] == []
    assert all(not b["products"] and not b["running"] and not b["queued"] for b in state["buildings"])
    assert state["resources"]["revenue_usd"] == 0 and state["resources"]["products"] == 0


def test_products_and_jobs_appear_where_the_database_says(session, settings, make_product):
    building = make_product()
    advance_to(session, settings, building, S.PRODUCTION)
    session.add(Job(product_id=building.id, department=Department.PRODUCTION, job_type="build_product",
                    for_status=S.PRODUCTION, status=JobStatus.RUNNING))
    waiting = make_product()
    advance_to(session, settings, waiting, S.HUMAN_REVIEW)
    broken = make_product()
    advance_to(session, settings, broken, S.QA)
    transition(session, settings, broken, S.ERROR, actor="test", reason="Ollama is off")
    session.flush()

    state = office_state(session, settings)
    workshop = _building(state, "production")
    assert [p["id"] for p in workshop["products"]] == [building.id] and len(workshop["running"]) == 1
    assert [p["id"] for p in _building(state, "keep")["products"]] == [waiting.id]
    assert _building(state, "infirmary")["products"][0]["reason"] == "Ollama is off"
    assert state["resources"]["waiting_for_you"] == 1 and state["resources"]["products"] == 3
    assert state["events"] and all(e["type"] not in ("job.started", "agent_run.finished") for e in state["events"])


def test_the_office_page_and_its_data_are_served(settings):
    with TestClient(create_app(settings)) as client:
        page = client.get("/")
        assert page.status_code == 200 and "Hive-Mint Office" in page.text
        data = client.get("/office/state").json()
        assert {b["id"] for b in data["buildings"]} == {b[0] for b in BUILDINGS}
        assert data["resources"]["claude_runs_cap"] == settings.max_claude_runs_per_day


def test_the_office_has_its_own_icon(settings):
    with TestClient(create_app(settings)) as client:
        icon = client.get("/favicon.ico")
        assert icon.status_code == 200 and icon.content[:4] == b"\x00\x00\x01\x00"
