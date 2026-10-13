"""Department handlers.

Each handler receives a product and its job and returns a StageResult naming the next
product status. v0.1 handlers are STUBS: they apply trivial deterministic checks and pass
the product forward so the pipeline can be exercised end to end. Replace each one with a
real implementation in later milestones; keep Testing deterministic.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from app.models import Job, Product
from app.states import PIPELINE, Department, Outcome, ProductStatus

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
        target = S.PRODUCTION if product.previous_status in _REBUILD_FROM else S.SPECIFICATION
        return StageResult(Outcome.PASS, target, f"[stub] rework routed to {target.value}")
    return stub_pass(Department.PRODUCT_DESIGN)(product, job)


HANDLERS: dict[Department, Handler] = {
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
