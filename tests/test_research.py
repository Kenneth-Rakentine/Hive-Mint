import copy
import json

import pytest
from sqlalchemy import select

from agents.research import ResearchFailed, research_gate, run_niche_research
from app.models import Event, Job, Opportunity, ResearchRecord
from app.states import JobStatus, Outcome, ProductStatus as S
from tests.conftest import advance_to


def factor(score, sources=(0,)):
    return {"score": score, "reason": f"evidence for {score}", "sources": list(sources)}


def opportunity(title="Motel Night Audit Workbook", **factors):
    scores = {"demand": 4, "competition": 2, "margin": 4, "ease": 5, "differentiation": 4, "support_burden": 2,
              "automation_suitability": 5} | factors
    return {"title": title, "product_type": "spreadsheet",
            "description": "Night audit, cash drawer and room revenue reconciliation for small motels.",
            "target_buyer": "independent motel owners", "price_low": 12, "price_high": 20,
            "contents": ["Night Audit", "Cash Drawer", "Dashboard"],
            "differentiation": "Reviews complain that templates don't reconcile cash to room revenue",
            "factors": {k: factor(v) for k, v in scores.items()}}


def research_answer(**overrides):
    answer = {
        "niche": "small motels", "summary": "Night audit and housekeeping logs sell steadily at $8-$25.",
        "selling": ["housekeeping checklists", "night audit logs"],
        "price_ranges": [{"item": "night audit template", "low": 8, "high": 25, "source": 0}],
        "common_contents": ["daily log", "room status"], "complaints": ["no totals", "hard to edit"],
        "gaps": ["cash reconciliation"],
        "sources": [{"url": f"https://example.com/page{i}", "title": f"p{i}", "supports": "listing prices"}
                    for i in range(6)],
        "confidence": "high", "opportunities": [opportunity()]}
    answer.update(overrides)
    return answer


def test_valid_research_is_stored_with_sources_and_opportunities(settings, session, fake_claude):
    fake_claude.replies = [{"structured_output": research_answer()}]
    record = run_niche_research(session, settings, "small motels")
    assert record.confidence == "high" and len(json.loads(record.sources_json)) == 6
    assert json.loads(record.findings_json)["complaints"] == ["no totals", "hard to edit"]
    opp = session.scalar(select(Opportunity))
    assert opp.research_id == record.id and opp.decision == Opportunity.PENDING
    assert json.loads(opp.factors_json)["demand"]["score"] == 4
    job = session.get(Job, record.job_id)
    assert job.status == JobStatus.SUCCEEDED and job.product_id is None and job.attempts == 1
    cmd, prompt = fake_claude.calls[0]
    assert "small motels" in prompt and "public pages only" in prompt
    assert cmd[cmd.index("--tools") + 1] == "WebSearch,WebFetch"


def test_invalid_research_is_retried_with_the_problems_listed(settings, session, fake_claude):
    thin = research_answer(sources=[{"url": "https://example.com/a", "supports": "x"}])
    branded = research_answer(opportunities=[opportunity(title="Airbnb Host Cleaning Tracker")])
    fake_claude.replies = [{"structured_output": thin}, {"structured_output": branded},
                           {"structured_output": research_answer()}]
    record = run_niche_research(session, settings, "short-term rentals")
    assert session.get(Job, record.job_id).attempts == 3
    assert "at least 6 are required" in fake_claude.calls[1][1]
    assert "Airbnb" in fake_claude.calls[2][1]
    assert len(session.scalars(select(Event).where(Event.event_type == "research.rejected")).all()) == 2


def test_research_gives_up_after_the_bounded_attempts(settings, session, fake_claude):
    bad = research_answer(sources=[])
    fake_claude.replies = [{"structured_output": bad}, {"is_error": True, "subtype": "error_during_execution"},
                           {"result": "no json"}]
    with pytest.raises(ResearchFailed, match="3 attempt"):
        run_niche_research(session, settings, "x")
    assert session.scalar(select(Job)).status == JobStatus.FAILED
    assert session.scalar(select(ResearchRecord)) is None


def test_factor_without_a_source_is_rejected(settings, session, fake_claude):
    opp = opportunity()
    opp["factors"]["demand"]["sources"] = []
    fake_claude.replies = [{"structured_output": research_answer(opportunities=[opp])}] * 3
    with pytest.raises(ResearchFailed, match="demand cites no source"):
        run_niche_research(session, settings, "x")


def _record(session, confidence="high", sources=3):
    record = ResearchRecord(niche="n", summary="s", confidence=confidence,
                            sources_json=json.dumps([{"url": f"https://e.com/{i}"} for i in range(sources)]))
    session.add(record)
    session.flush()
    return record


def _linked_product(session, settings, make_product, record):
    product = advance_to(session, settings, make_product(), S.RESEARCH)
    session.add(Opportunity(research_id=record.id, product_id=product.id, title="t", product_type="spreadsheet"))
    session.flush()
    return product


@pytest.mark.parametrize("confidence,sources,outcome", [
    ("high", 3, Outcome.PASS), ("medium", 5, Outcome.PASS), ("low", 9, Outcome.FAIL), ("high", 2, Outcome.FAIL)])
def test_research_gate_checks_the_evidence(session, settings, make_product, confidence, sources, outcome):
    product = _linked_product(session, settings, make_product, _record(session, confidence, sources))
    result = research_gate(product, None)
    assert result.outcome == outcome
    assert result.next_status == (S.FINANCE_REVIEW if outcome == Outcome.PASS else S.REJECTED)


def test_research_gate_rejects_a_product_without_evidence_and_passes_owner_specs(session, settings, make_product,
                                                                                 sample_product):
    unknown = advance_to(session, settings, make_product(), S.RESEARCH)
    assert research_gate(unknown, None).next_status == S.REJECTED
    owner = advance_to(session, settings, sample_product, S.RESEARCH)
    assert research_gate(owner, None).next_status == S.FINANCE_REVIEW


def test_research_answer_helper_is_not_shared_state():
    a = research_answer()
    b = copy.deepcopy(a)
    a["sources"].clear()
    assert b["sources"]
