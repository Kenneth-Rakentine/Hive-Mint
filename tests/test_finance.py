import json
from dataclasses import replace

import pytest
from sqlalchemy import select

from agents.finance import evaluate, finance_gate, margin_score, net_per_sale, score, select_opportunities
from app.lifecycle import LimitReached
from app.models import Opportunity, Product, ResearchRecord
from app.states import ProductStatus as S
from config.settings import OPPORTUNITY_FACTORS
from tests.conftest import advance_to

WEIGHTS = {"demand": 0.25, "competition": 0.15, "margin": 0.15, "ease": 0.10, "differentiation": 0.15,
           "support_burden": 0.10, "automation_suitability": 0.10}


def test_formula_extremes_and_inversion():
    best = {f: 5 for f in OPPORTUNITY_FACTORS} | {"competition": 1, "support_burden": 1}
    worst = {f: 1 for f in OPPORTUNITY_FACTORS} | {"competition": 5, "support_burden": 5}
    assert score(best, WEIGHTS)[0] == 100.0
    assert score(worst, WEIGHTS)[0] == 0.0
    middle = {f: 3 for f in OPPORTUNITY_FACTORS}
    assert score(middle, WEIGHTS)[0] == 50.0
    value, reasons = score(best, WEIGHTS)
    assert len(reasons) == 7 and "competition: 5/5 (inverted from 1)" in reasons[1]


def test_formula_weights_each_factor():
    base = {f: 3 for f in OPPORTUNITY_FACTORS}
    assert score(base | {"demand": 5}, WEIGHTS)[0] == 62.5  # 50 + 0.25 * 0.5 * 100
    assert score(base | {"competition": 5}, WEIGHTS)[0] == 42.5  # more competition lowers the score


def test_formula_rejects_out_of_range_factors():
    with pytest.raises(ValueError):
        score({f: 3 for f in OPPORTUNITY_FACTORS} | {"ease": 6}, WEIGHTS)


@pytest.mark.parametrize("price,expected", [(2.99, 1), (6, 2), (10, 3), (19.99, 4), (29, 5)])
def test_margin_from_price_after_fees(price, expected):
    assert margin_score(net_per_sale(price)) == expected


def _opportunity(session, record, title, product_type="spreadsheet", price=(12, 20), **factors):
    scores = {f: 3 for f in OPPORTUNITY_FACTORS} | factors
    opp = Opportunity(research_id=record.id, title=title, product_type=product_type, description=f"{title} desc",
                      data_json=json.dumps({"price_low": price[0], "price_high": price[1]}),
                      factors_json=json.dumps({k: {"score": v, "reason": "r", "sources": [0]}
                                               for k, v in scores.items()}))
    session.add(opp)
    session.flush()
    return opp


@pytest.fixture
def record(session):
    record = ResearchRecord(niche="n", summary="s", confidence="high", sources_json="[]")
    session.add(record)
    session.flush()
    return record


def test_evaluate_records_deterministic_adjustments(settings, session, record):
    opp = _opportunity(session, record, "PDF planner", product_type="pdf", price=(2, 3), margin=5)
    value, reasons, factors = evaluate(opp, record, settings)
    assert factors["margin"] == 1 and factors["automation_suitability"] == 1
    assert any("margin set to 1" in r for r in reasons)
    assert any("cannot build 'pdf'" in r for r in reasons)
    record.confidence = "medium"
    assert evaluate(opp, record, settings)[0] == value - 10


def test_select_moves_only_the_top_opportunities_above_the_threshold(settings, session, record):
    settings = replace(settings, max_opportunities_per_shift=2)
    strong = {"demand": 5, "competition": 1, "ease": 5, "differentiation": 5, "support_burden": 1,
              "automation_suitability": 5}
    a = _opportunity(session, record, "Alpha Tracker", price=(20, 30), **strong)
    b = _opportunity(session, record, "Beta Tracker", price=(20, 30), **strong | {"demand": 4})
    c = _opportunity(session, record, "Gamma Tracker", price=(20, 30), **strong | {"demand": 3})
    weak = _opportunity(session, record, "Weak Tracker", price=(2, 3))
    created = select_opportunities(session, settings)
    assert [p.name for p in created] == ["Alpha Tracker", "Beta Tracker"]
    assert (a.decision, b.decision, c.decision, weak.decision) == (
        Opportunity.SELECTED, Opportunity.SELECTED, Opportunity.NOT_TOP, Opportunity.BELOW_THRESHOLD)
    assert (a.rank, b.rank, c.rank, weak.rank) == (1, 2, 3, 4)
    assert c.score >= settings.opportunity_threshold > weak.score
    assert "only the top 2" in c.reasons_json and "below the threshold" in weak.reasons_json
    product = session.get(Product, a.product_id)
    assert product.status == S.IDEA and product.slug == f"alpha-tracker-o{a.id}"
    assert select_opportunities(session, settings) == []  # already decided; nothing re-scored


def test_low_confidence_research_is_never_selected(settings, session, record):
    record.confidence = "low"
    opp = _opportunity(session, record, "Any", price=(30, 40), demand=5, ease=5, differentiation=5)
    assert select_opportunities(session, settings) == []
    assert opp.decision == Opportunity.BELOW_THRESHOLD and "low research confidence" in opp.reasons_json


def test_idea_limit_stops_selection_and_leaves_the_rest_pending(settings, session, record):
    settings = replace(settings, max_ideas_per_day=1)
    strong = {"demand": 5, "competition": 1, "ease": 5, "differentiation": 5, "support_burden": 1,
              "automation_suitability": 5}
    first = _opportunity(session, record, "One", price=(20, 30), **strong)
    second = _opportunity(session, record, "Two", price=(20, 30), **strong)
    with pytest.raises(LimitReached):
        select_opportunities(session, settings)
    assert first.decision == Opportunity.SELECTED and second.decision == Opportunity.PENDING


def test_finance_gate_rechecks_the_stored_score(settings, session, record, make_product, sample_product):
    gate = finance_gate(settings)
    product = advance_to(session, settings, make_product(), S.FINANCE_REVIEW)
    opp = _opportunity(session, record, "Gate")
    opp.product_id, opp.score = product.id, 80.0
    assert gate(product, None).next_status == S.SPECIFICATION
    opp.score = 40.0
    assert gate(product, None).next_status == S.REJECTED
    orphan = advance_to(session, settings, make_product(), S.FINANCE_REVIEW)
    assert gate(orphan, None).next_status == S.REJECTED
    owner = advance_to(session, settings, sample_product, S.FINANCE_REVIEW)
    assert gate(owner, None).next_status == S.SPECIFICATION
    assert session.scalars(select(Opportunity)).all()
