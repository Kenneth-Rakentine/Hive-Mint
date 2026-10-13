"""Enumerated statuses, departments and the product transition table.

The table is the first line of defence against skipped gates. Context-dependent rules
(e.g. "HUMAN_REVIEW may only release to READY_TO_PUBLISH after SEO") live as guards in
app.lifecycle.
"""

from __future__ import annotations

from enum import StrEnum


class ProductStatus(StrEnum):
    IDEA = "IDEA"
    RESEARCH = "RESEARCH"
    FINANCE_REVIEW = "FINANCE_REVIEW"
    SPECIFICATION = "SPECIFICATION"
    PRODUCTION = "PRODUCTION"
    TESTING = "TESTING"
    QA = "QA"
    RED_TEAM = "RED_TEAM"
    COMPLIANCE = "COMPLIANCE"
    SEO = "SEO"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    READY_TO_PUBLISH = "READY_TO_PUBLISH"
    PUBLISHED = "PUBLISHED"
    REWORK = "REWORK"
    REJECTED = "REJECTED"
    PAUSED = "PAUSED"
    RETIRED = "RETIRED"
    ERROR = "ERROR"


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    PAUSED = "PAUSED"  # held by a guard (e.g. DISK_LIMIT); requeued when the guard clears


OPEN_JOB_STATUSES = frozenset({JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.PAUSED})


class Department(StrEnum):
    OPERATIONS = "OPERATIONS"
    RESEARCH = "RESEARCH"
    FINANCE = "FINANCE"
    PRODUCT_DESIGN = "PRODUCT_DESIGN"
    PRODUCTION = "PRODUCTION"
    TESTING = "TESTING"
    QA = "QA"
    RED_TEAM = "RED_TEAM"
    COMPLIANCE = "COMPLIANCE"
    SEO = "SEO"
    ANALYTICS = "ANALYTICS"
    CUSTOMER_SERVICE = "CUSTOMER_SERVICE"


class Outcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    AMBIGUOUS = "AMBIGUOUS"


S = ProductStatus

PIPELINE: tuple[ProductStatus, ...] = (
    S.IDEA, S.RESEARCH, S.FINANCE_REVIEW, S.SPECIFICATION, S.PRODUCTION, S.TESTING, S.QA,
    S.RED_TEAM, S.COMPLIANCE, S.SEO, S.HUMAN_REVIEW, S.READY_TO_PUBLISH, S.PUBLISHED,
)

TERMINAL = frozenset({S.REJECTED, S.RETIRED})
# States a product can be paused or errored out of, and later resumed into.
ACTIVE = frozenset(set(PIPELINE) - {S.PUBLISHED} | {S.REWORK})
HOLD = frozenset({S.PAUSED, S.ERROR})

_BACKWARD: dict[ProductStatus, set[ProductStatus]] = {
    S.IDEA: {S.REJECTED},
    S.RESEARCH: {S.REJECTED},
    S.FINANCE_REVIEW: {S.RESEARCH, S.REJECTED},
    S.SPECIFICATION: {S.RESEARCH},
    S.PRODUCTION: {S.REWORK},
    S.TESTING: {S.REWORK},
    S.QA: {S.REWORK},
    S.RED_TEAM: {S.REWORK, S.REJECTED},
    S.COMPLIANCE: {S.REWORK, S.REJECTED, S.HUMAN_REVIEW},  # HUMAN_REVIEW = ambiguity
    S.SEO: {S.REWORK},
    S.HUMAN_REVIEW: {S.REWORK, S.REJECTED, S.COMPLIANCE},
    S.READY_TO_PUBLISH: {S.HUMAN_REVIEW},
    S.REWORK: {S.SPECIFICATION, S.PRODUCTION, S.REJECTED},
    S.PUBLISHED: {S.RETIRED},
}


def _build_table() -> dict[ProductStatus, frozenset[ProductStatus]]:
    table: dict[ProductStatus, set[ProductStatus]] = {s: set() for s in ProductStatus}
    for current, nxt in zip(PIPELINE, PIPELINE[1:]):
        table[current].add(nxt)
    for current, targets in _BACKWARD.items():
        table[current] |= targets
    for state in ACTIVE:
        table[state] |= HOLD
    # From a hold state the product may resume (to its recorded paused_from, enforced by
    # lifecycle guards) or be closed out.
    for hold in HOLD:
        table[hold] |= ACTIVE | TERMINAL
    table[S.PAUSED].add(S.ERROR)
    for state in TERMINAL:
        table[state] = set()
    return {k: frozenset(v) for k, v in table.items()}


TRANSITIONS: dict[ProductStatus, frozenset[ProductStatus]] = _build_table()


class IllegalTransition(ValueError):
    """Raised when a requested product status change is not allowed."""


def can_transition(current: ProductStatus, target: ProductStatus) -> bool:
    return target in TRANSITIONS[current]
