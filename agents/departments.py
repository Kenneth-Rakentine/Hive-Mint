"""Department handlers.

Each handler receives a product and its job and returns a StageResult naming the next
product status. The handlers here are STUBS: they apply trivial deterministic checks and
pass the product forward so the pipeline can be exercised end to end. Production
(agents/production.py), Testing (agents/testing.py), QA (agents/qa.py) and Red Team
(agents/red_team.py, via a Claude Code session) are real; see
agents.operations.default_handlers. Product Design's rework routing is real and
deterministic. Keep Testing deterministic.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy.orm import object_session

from agents.rework import active_brief, uncovered
from app.models import Job, Product
from app.states import PIPELINE, Department, Outcome, ProductStatus
from tools.spec import load_spec

S = ProductStatus


@dataclass(frozen=True)
class StageResult:
    outcome: Outcome
    next_status: ProductStatus
    summary: str
    data: dict = field(default_factory=dict)


Handler = Callable[[Product, Job], StageResult]


def _forward(status: ProductStatus) -> ProductStatus:
    return PIPELINE[PIPELINE.index(status) + 1]


def stub_pass(department: Department) -> Handler:
    def handler(product: Product, job: Job) -> StageResult:
        return StageResult(Outcome.PASS, _forward(product.status),
                           f"[stub] {department.value} passed {product.status.value}")

    handler.__name__ = f"stub_{department.value.lower()}"
    return handler


def operations_intake(product: Product, job: Job) -> StageResult:
    """Deterministic intake: an idea needs a name and a product type to enter research."""
    if not product.name.strip() or not product.product_type.strip():
        return StageResult(Outcome.FAIL, S.REJECTED, "Idea is missing a name or product type")
    return StageResult(Outcome.PASS, S.RESEARCH, "Intake complete; idea queued for research")


# Failures found at or after production only need a rebuild; earlier ones need a new spec.
_REBUILD_FROM = frozenset({S.PRODUCTION, S.TESTING, S.QA, S.SEO})


def product_design(product: Product, job: Job) -> StageResult:
    if product.status == S.REWORK:
        brief = active_brief(object_session(product), product)
        if brief is not None:
            spec = load_spec(product.slug)
            missing = uncovered(brief, spec) if spec is not None else []
            if missing:
                return StageResult(
                    Outcome.FAIL, S.PAUSED,
                    f"Rework brief #{brief.id} needs spec/builder changes before a rebuild: {', '.join(missing)}. "
                    f"Implement them, list them under 'resolves' in config/specs/{product.slug}.json, then run "
                    f"python -m app.cli resume {product.id}", {"brief_id": brief.id, "uncovered": missing})
            return StageResult(Outcome.PASS, S.PRODUCTION,
                               f"Rework brief #{brief.id} ({len(brief.change_ids)} changes) routed to Production",
                               {"brief_id": brief.id})
        target = S.PRODUCTION if product.previous_status in _REBUILD_FROM else S.SPECIFICATION
        return StageResult(Outcome.PASS, target, f"[stub] rework routed to {target.value}")
    return stub_pass(Department.PRODUCT_DESIGN)(product, job)


STUB_HANDLERS: dict[Department, Handler] = {
    Department.OPERATIONS: operations_intake,
    Department.RESEARCH: stub_pass(Department.RESEARCH),
    Department.FINANCE: stub_pass(Department.FINANCE),
    Department.PRODUCT_DESIGN: product_design,
    Department.PRODUCTION: stub_pass(Department.PRODUCTION),
    Department.TESTING: stub_pass(Department.TESTING),
    Department.QA: stub_pass(Department.QA),
    Department.RED_TEAM: stub_pass(Department.RED_TEAM),
    Department.COMPLIANCE: stub_pass(Department.COMPLIANCE),
    Department.SEO: stub_pass(Department.SEO),
}
