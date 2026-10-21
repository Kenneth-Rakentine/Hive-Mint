"""Finance department: score opportunities with the weighted opportunity formula.

score = 100 * sum(weight * (s - 1) / 4) over the seven factors, where s is the 1-5 factor
score and "lower is better" factors (competition, support burden) are inverted first (s -> 6 - s).
Research's estimates are adjusted by deterministic rules, each recorded as a reason:
- margin is recomputed from the researched price range after marketplace fees;
- automation suitability is 1 for product types Production cannot build yet;
- low research confidence is never selected (medium is scored like high; owner decision 2026-10-21).

Only the top MAX_OPPORTUNITIES_PER_SHIFT opportunities at or above OPPORTUNITY_THRESHOLD become
products (IDEA), within MAX_IDEAS_PER_DAY. Every decision and its reasons are stored.
"""

from __future__ import annotations

import json
import re

from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from agents.departments import StageResult
from agents.research import BUILDABLE_TYPES, opportunity_for, owner_sourced
from app.db import utcnow
from app.lifecycle import LimitReached, create_product, log_event
from app.models import Opportunity, Product, ResearchRecord
from app.states import Outcome, ProductStatus
from config.settings import INVERTED_FACTORS, OPPORTUNITY_FACTORS, Settings

S = ProductStatus
ACTOR = "finance"
# Marketplace fee assumption for a digital download: ~6.5% transaction + ~3% + $0.25 processing + $0.20 listing.
FEE_RATE, FEE_FIXED = 0.095, 0.45
# Net revenue per sale (USD) -> margin score.
MARGIN_STEPS = ((3, 1), (6, 2), (10, 3), (18, 4))


def net_per_sale(price: float) -> float:
    return round(price * (1 - FEE_RATE) - FEE_FIXED, 2)


def margin_score(net: float) -> int:
    for limit, score in MARGIN_STEPS:
        if net < limit:
            return score
    return 5


def score(factors: dict[str, int], weights: dict[str, float]) -> tuple[float, list[str]]:
    """The weighted opportunity formula. Returns (0-100 score, one reason per factor)."""
    total, reasons = 0.0, []
    for name in OPPORTUNITY_FACTORS:
        raw = factors[name]
        if not 1 <= raw <= 5:
            raise ValueError(f"factor {name} must be 1-5, got {raw}")
        value = 6 - raw if name in INVERTED_FACTORS else raw
        points = weights[name] * (value - 1) / 4 * 100
        total += points
        inverted = f" (inverted from {raw})" if name in INVERTED_FACTORS else ""
        reasons.append(f"{name}: {value}/5{inverted} x {weights[name]:.2f} = {points:.1f}")
    return round(total, 1), reasons


def evaluate(opp: Opportunity, record: ResearchRecord, settings: Settings) -> tuple[float, list[str], dict]:
    """Apply the deterministic adjustments, then the formula. Returns (score, reasons, factor scores used)."""
    estimates = json.loads(opp.factors_json)
    data = json.loads(opp.data_json)
    factors = {name: int(estimates[name]["score"]) for name in OPPORTUNITY_FACTORS}
    reasons = [f"research estimate {name}={factors[name]}: {estimates[name]['reason']}" for name in OPPORTUNITY_FACTORS]
    price = (float(data.get("price_low", 0)) + float(data.get("price_high", 0))) / 2
    if price > 0:
        net = net_per_sale(price)
        computed = margin_score(net)
        if computed != factors["margin"]:
            reasons.append(f"margin set to {computed} from the price range: mid ${price:.2f} nets ${net:.2f} after fees")
        factors["margin"] = computed
    if opp.product_type not in BUILDABLE_TYPES:
        reasons.append(f"automation_suitability set to 1: Production cannot build '{opp.product_type}' products yet")
        factors["automation_suitability"] = 1
    value, formula = score(factors, dict(settings.opportunity_weights))
    reasons += formula
    return value, reasons, factors


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:80]


def select_opportunities(session: Session, settings: Settings) -> list[Product]:
    """Score every pending opportunity; turn the best into IDEA products. Raises LimitReached at the idea cap."""
    pending = session.scalars(select(Opportunity).where(Opportunity.decision == Opportunity.PENDING)
                              .order_by(Opportunity.id)).all()
    scored = []
    for opp in pending:
        record = session.get(ResearchRecord, opp.research_id)
        value, reasons, _ = evaluate(opp, record, settings)
        opp.score, opp.scored_at = value, utcnow()
        scored.append((opp, record, reasons))
    scored.sort(key=lambda item: (-item[0].score, item[0].id))
    created: list[Product] = []
    for rank, (opp, record, reasons) in enumerate(scored, start=1):
        opp.rank = rank
        if record.confidence == "low":
            decision, reasons = Opportunity.BELOW_THRESHOLD, reasons + ["not selected: low research confidence"]
        elif opp.score < settings.opportunity_threshold:
            decision = Opportunity.BELOW_THRESHOLD
            reasons = reasons + [f"not selected: {opp.score} is below the threshold {settings.opportunity_threshold:g}"]
        elif len(created) >= settings.max_opportunities_per_shift:
            decision = Opportunity.NOT_TOP
            reasons = reasons + [f"not selected: only the top {settings.max_opportunities_per_shift} move on"]
        else:
            try:
                product = create_product(session, settings, slug=f"{slugify(opp.title)}-o{opp.id}", name=opp.title,
                                         product_type=opp.product_type, description=opp.description, actor=ACTOR)
            except LimitReached:
                opp.reasons_json = json.dumps(reasons + ["waiting: daily idea limit reached"])
                session.flush()
                raise  # the rest stay PENDING for the next shift
            session.flush()
            opp.product_id, decision = product.id, Opportunity.SELECTED
            reasons = reasons + [f"selected: score {opp.score} (rank {rank})"]
            created.append(product)
        opp.decision, opp.reasons_json = decision, json.dumps(reasons)
        log_event(session, "finance.scored", actor=ACTOR,
                  product=session.get(Product, opp.product_id) if opp.product_id else None,
                  message=f"{opp.title}: {opp.score} -> {decision}",
                  data={"opportunity_id": opp.id, "score": opp.score, "rank": rank, "decision": decision})
    return created


def finance_gate(settings: Settings):
    """FINANCE_REVIEW gate: re-check the stored score against the threshold."""

    def handler(product, job) -> StageResult:
        session = object_session(product)
        opp = opportunity_for(session, product)
        if opp is None:
            if owner_sourced(product):
                return StageResult(Outcome.PASS, S.SPECIFICATION, "Owner-sourced product; approved by the owner, not scored")
            return StageResult(Outcome.FAIL, S.REJECTED, "No scored opportunity for this product")
        if opp.score is None or opp.score < settings.opportunity_threshold:
            return StageResult(Outcome.FAIL, S.REJECTED,
                               f"Opportunity score {opp.score} is below the threshold {settings.opportunity_threshold:g}")
        return StageResult(Outcome.PASS, S.SPECIFICATION,
                           f"Opportunity #{opp.id} scored {opp.score} (threshold {settings.opportunity_threshold:g})",
                           {"opportunity_id": opp.id, "score": opp.score})

    handler.provider = "deterministic"
    return handler


def reopen_recent(session: Session, days: int = 14) -> list[Opportunity]:
    """The owner asks Finance to score recent turned-down ideas again (e.g. after the weights change).

    Only ideas turned down for their score, from research that was not low confidence, go back to PENDING;
    the next shift's Finance step scores them with the current settings. Every reopen is logged.
    """
    from datetime import timedelta

    since = utcnow() - timedelta(days=days)
    rows = session.scalars(select(Opportunity).join(ResearchRecord, ResearchRecord.id == Opportunity.research_id)
                           .where(Opportunity.decision == Opportunity.BELOW_THRESHOLD,
                                  Opportunity.product_id.is_(None),
                                  ResearchRecord.confidence != "low",
                                  Opportunity.created_at >= since)).all()
    for opp in rows:
        log_event(session, "finance.reopened", actor="owner",
                  message=f"{opp.title}: was {opp.score}; Finance scores it again with the current settings",
                  data={"opportunity_id": opp.id, "previous_score": opp.score})
        opp.decision, opp.score, opp.rank = Opportunity.PENDING, None, None
    session.flush()
    return rows

