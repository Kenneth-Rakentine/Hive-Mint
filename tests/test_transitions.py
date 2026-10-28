from dataclasses import replace
import pytest

from app.lifecycle import LimitReached, TransitionBlocked, record_approval, transition
from app.states import PIPELINE, TRANSITIONS, IllegalTransition, ProductStatus as S, can_transition
from tests.conftest import advance_to


def test_every_forward_step_is_legal():
    for current, nxt in zip(PIPELINE, PIPELINE[1:]):
        assert can_transition(current, nxt), f"{current} -> {nxt}"


def test_table_covers_every_status():
    assert set(TRANSITIONS) == set(S)


@pytest.mark.parametrize("current,target", [
    (S.IDEA, S.PRODUCTION),
    (S.IDEA, S.PUBLISHED),
    (S.RESEARCH, S.SPECIFICATION),
    (S.TESTING, S.RED_TEAM),
    (S.QA, S.COMPLIANCE),
    (S.SEO, S.PUBLISHED),
    (S.SEO, S.READY_TO_PUBLISH),
    (S.COMPLIANCE, S.READY_TO_PUBLISH),
    (S.REWORK, S.TESTING),
    (S.PUBLISHED, S.IDEA),
])
def test_skipping_gates_is_illegal(current, target):
    assert not can_transition(current, target)


@pytest.mark.parametrize("terminal", [S.REJECTED, S.RETIRED])
def test_terminal_states_have_no_exits(terminal):
    assert TRANSITIONS[terminal] == frozenset()


def test_illegal_transition_raises_and_leaves_product_unchanged(session, settings, make_product):
    product = make_product()
    with pytest.raises(IllegalTransition):
        transition(session, settings, product, S.PRODUCTION, actor="test", reason="skip")
    assert product.status == S.IDEA


def test_full_legal_path_to_human_review(session, settings, make_product):
    product = advance_to(session, settings, make_product(), S.HUMAN_REVIEW)
    assert product.status == S.HUMAN_REVIEW
    assert product.previous_status == S.SEO


def test_failures_route_backward(session, settings, make_product):
    product = advance_to(session, settings, make_product(), S.TESTING)
    transition(session, settings, product, S.REWORK, actor="testing", reason="formula error")
    assert product.status == S.REWORK and product.rework_count == 1
    with pytest.raises(IllegalTransition):
        transition(session, settings, product, S.QA, actor="test", reason="skip back to QA")
    transition(session, settings, product, S.PRODUCTION, actor="design", reason="rebuild")
    assert product.status == S.PRODUCTION


def test_release_requires_seo_path_and_human_approval(session, settings, make_product):
    product = advance_to(session, settings, make_product(), S.HUMAN_REVIEW)
    with pytest.raises(TransitionBlocked):
        transition(session, settings, product, S.READY_TO_PUBLISH, actor="ops", reason="no approval")
    record_approval(session, product, approver="owner")
    transition(session, settings, product, S.READY_TO_PUBLISH, actor="owner", reason="approved")
    assert product.status == S.READY_TO_PUBLISH


def test_compliance_referral_cannot_be_released_without_seo(session, settings, make_product):
    product = advance_to(session, settings, make_product(), S.COMPLIANCE)
    transition(session, settings, product, S.HUMAN_REVIEW, actor="compliance", reason="ambiguous claim")
    with pytest.raises(TransitionBlocked):
        record_approval(session, product, approver="owner")
    with pytest.raises(TransitionBlocked):
        transition(session, settings, product, S.READY_TO_PUBLISH, actor="owner", reason="skip SEO")
    transition(session, settings, product, S.COMPLIANCE, actor="owner", reason="claim is fine")
    assert product.status == S.COMPLIANCE


def test_only_compliance_referral_returns_to_compliance(session, settings, make_product):
    product = advance_to(session, settings, make_product(), S.HUMAN_REVIEW)
    with pytest.raises(TransitionBlocked):
        transition(session, settings, product, S.COMPLIANCE, actor="owner", reason="re-check")


def test_publication_blocked_by_zero_daily_limit(session, settings, make_product):
    product = advance_to(session, settings, make_product(), S.HUMAN_REVIEW)
    record_approval(session, product, approver="owner")
    transition(session, settings, product, S.READY_TO_PUBLISH, actor="owner", reason="approved")
    assert settings.max_publications_per_day == 0
    with pytest.raises(LimitReached):
        transition(session, settings, product, S.PUBLISHED, actor="ops", reason="publish")
    assert product.status == S.READY_TO_PUBLISH


def test_rework_clears_approval(session, settings, make_product):
    product = advance_to(session, settings, make_product(), S.HUMAN_REVIEW)
    record_approval(session, product, approver="owner")
    transition(session, settings, product, S.REWORK, actor="owner", reason="change header")
    assert product.approved_by is None


def test_pause_resumes_only_to_paused_from(session, settings, make_product):
    product = advance_to(session, settings, make_product(), S.QA)
    transition(session, settings, product, S.PAUSED, actor="owner", reason="hold")
    assert product.paused_from == S.QA
    with pytest.raises(TransitionBlocked):
        transition(session, settings, product, S.SEO, actor="owner", reason="jump ahead")
    transition(session, settings, product, S.QA, actor="owner", reason="resume")
    assert product.status == S.QA and product.paused_from is None


def test_rework_cap_pauses_for_human(session, settings, make_product):
    settings = replace(settings, auto_rework_grants=0, redesigns_per_product=0, auto_drop_failed=False)  # the automatic extra cycle has its own test
    product = advance_to(session, settings, make_product(), S.PRODUCTION)
    for _ in range(settings.max_rework_cycles):
        transition(session, settings, product, S.REWORK, actor="testing", reason="fail")
        transition(session, settings, product, S.PRODUCTION, actor="design", reason="retry")
    transition(session, settings, product, S.REWORK, actor="testing", reason="fail again")
    assert product.status == S.PAUSED
    assert product.paused_from == S.REWORK  # held at the refused rework; resuming enters it
    assert "Rework limit" in product.status_reason and "approve-rework" in product.status_reason


def test_approve_rework_grants_cycles_and_resumes_a_paused_product(session, settings, make_product):
    from app.lifecycle import approve_rework

    settings = replace(settings, auto_rework_grants=0, redesigns_per_product=0, auto_drop_failed=False)
    product = advance_to(session, settings, make_product(), S.PRODUCTION)
    for _ in range(settings.max_rework_cycles):
        transition(session, settings, product, S.REWORK, actor="testing", reason="fail")
        transition(session, settings, product, S.PRODUCTION, actor="design", reason="retry")
    transition(session, settings, product, S.REWORK, actor="testing", reason="fail again")
    assert product.status == S.PAUSED
    approve_rework(session, settings, product, cycles=1, actor="owner")
    assert product.status == S.REWORK and product.extra_rework_cycles == 1
    count = product.rework_count
    transition(session, settings, product, S.PRODUCTION, actor="design", reason="revised")
    transition(session, settings, product, S.REWORK, actor="red_team", reason="still not right")
    assert product.status == S.PAUSED and product.rework_count == count + 1  # exactly one extra cycle granted


def test_approve_rework_on_an_active_rework_only_raises_the_limit(session, settings, make_product):
    from app.lifecycle import approve_rework, rework_limit

    product = advance_to(session, settings, make_product(), S.QA)
    transition(session, settings, product, S.REWORK, actor="qa", reason="fail")
    approve_rework(session, settings, product, cycles=2, actor="owner")
    assert product.status == S.REWORK and rework_limit(settings, product) == settings.max_rework_cycles + 2
    with pytest.raises(ValueError):
        approve_rework(session, settings, product, cycles=0, actor="owner")
