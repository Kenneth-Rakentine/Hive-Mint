import json

import pytest
from sqlalchemy import select

from agents.operations import OperationsManager
from app.lifecycle import LimitReached, create_product, transition
from app.models import AgentRun, Event
from app.states import ProductStatus as S


def _events(session, **filters):
    query = select(Event).order_by(Event.id)
    for key, value in filters.items():
        query = query.where(getattr(Event, key) == value)
    return session.scalars(query).all()


def test_product_creation_is_logged(session, make_product):
    product = make_product()
    (event,) = _events(session, event_type="product.created")
    assert event.product_id == product.id and event.to_status == "IDEA"


def test_each_transition_logs_from_and_to(session, settings, make_product):
    product = make_product()
    transition(session, settings, product, S.RESEARCH, actor="operations", reason="intake ok")
    transition(session, settings, product, S.FINANCE_REVIEW, actor="research", reason="evidence ok")
    events = _events(session, event_type="product.transition")
    assert [(e.from_status, e.to_status, e.actor) for e in events] == [
        ("IDEA", "RESEARCH", "operations"),
        ("RESEARCH", "FINANCE_REVIEW", "research"),
    ]
    assert events[1].message == "evidence ok"


def test_redirected_rework_records_requested_status(session, settings, make_product):
    settings_no_rework = type(settings)(db_url=settings.db_url, max_rework_cycles=0)
    product = make_product()
    for status in (S.RESEARCH, S.FINANCE_REVIEW, S.SPECIFICATION, S.PRODUCTION):
        transition(session, settings, product, status, actor="t", reason="ok")
    transition(session, settings_no_rework, product, S.REWORK, actor="testing", reason="fail")
    event = _events(session, event_type="product.transition")[-1]
    assert event.to_status == "PAUSED"
    assert json.loads(event.data_json) == {"requested": "REWORK"}


def test_job_lifecycle_and_agent_run_are_logged(session, settings, make_product):
    ops = OperationsManager(session, settings)
    product = make_product()
    job = ops.next_job(product)
    ops.run_job(job)
    types = [e.event_type for e in _events(session, job_id=job.id)]
    assert types == ["job.created", "job.started", "product.transition", "agent_run.finished", "job.finished"]

    (run,) = session.scalars(select(AgentRun).where(AgentRun.job_id == job.id)).all()
    assert (run.provider, run.model, run.tier, run.cost_usd_micros) == ("stub", "none", 0, 0)
    assert run.status == "SUCCEEDED" and run.finished_at and run.duration_ms is not None
    assert _events(session, event_type="product.transition")[0].agent_run_id == run.id
    run_event = _events(session, event_type="agent_run.finished")[0]
    assert run_event.agent_run_id == run.id
    assert json.loads(run_event.data_json)["provider"] == "stub"


def test_events_survive_a_new_session(engine, session, settings, make_product):
    from app.db import make_session_factory

    make_product()
    session.commit()
    with make_session_factory(engine)() as fresh:
        assert len(_events(fresh)) == 1


def test_daily_idea_limit(session, settings):
    limited = type(settings)(db_url=settings.db_url, max_ideas_per_day=2)
    for n in range(2):
        create_product(session, limited, slug=f"idea-{n}", name=f"Idea {n}", product_type="pdf")
    with pytest.raises(LimitReached):
        create_product(session, limited, slug="idea-3", name="Idea 3", product_type="pdf")
