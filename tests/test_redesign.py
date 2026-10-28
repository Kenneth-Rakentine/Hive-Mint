"""Back to the drawing board: out of rework cycles -> one redesign from scratch -> dropped if that fails too."""

import json
from dataclasses import replace

from sqlalchemy import select

from agents.rework import create_brief
from agents.spec_writer import ProductDesign, redesign_pending
from app.lifecycle import auto_resume_rework, transition
from app.models import Event, Job, ReworkBrief, SpecVersion
from app.states import Department, ProductStatus as S
from tests.conftest import SAMPLE_SLUG, advance_to
from tools.spec import SPEC_DIR
from tools.spec_store import product_spec


def _strict(settings, **kw):
    return replace(settings, **({"max_rework_cycles": 1, "auto_rework_grants": 0, "redesigns_per_product": 1,
                                 "auto_drop_failed": True} | kw))


def _fail_again(session, settings, product):
    transition(session, settings, product, S.PRODUCTION, actor="design", reason="rebuild")
    advance_to(session, settings, product, S.QA)
    transition(session, settings, product, S.REWORK, actor="qa", reason="QA REWORK again")


def test_out_of_cycles_goes_back_to_the_drawing_board_then_is_dropped(session, settings, make_product):
    settings = _strict(settings)
    product = make_product()
    advance_to(session, settings, product, S.QA)
    transition(session, settings, product, S.REWORK, actor="qa", reason="QA REWORK 1")
    _fail_again(session, settings, product)  # over the limit: redesign
    assert product.status == S.REWORK and product.rework_count == 1
    assert "drawing board" in product.status_reason
    assert len(session.scalars(select(Event).where(Event.event_type == "product.redesign")).all()) == 1
    _fail_again(session, settings, product)  # the redesign fails too: dropped, slot freed
    assert product.status == S.REJECTED and "Dropped" in product.status_reason


def test_an_owner_specified_product_pauses_instead_of_being_dropped(session, settings, sample_product):
    settings = _strict(settings, redesigns_per_product=0)
    advance_to(session, settings, sample_product, S.QA)
    transition(session, settings, sample_product, S.REWORK, actor="qa", reason="QA REWORK 1")
    _fail_again(session, settings, sample_product)
    assert sample_product.status == S.PAUSED


def test_a_product_paused_before_redesigns_existed_gets_one(session, settings, make_product):
    old = replace(settings, max_rework_cycles=0, auto_rework_grants=0, redesigns_per_product=0,
                  auto_drop_failed=False)
    product = make_product()
    advance_to(session, old, product, S.QA)
    transition(session, old, product, S.REWORK, actor="qa", reason="fail")
    assert product.status == S.PAUSED
    assert auto_resume_rework(session, _strict(settings), product)
    assert product.status == S.REWORK and "redesign #1" in product.status_reason


def test_product_design_writes_a_fresh_spec_from_the_failure_history(session, settings, sample_product, fake_claude):
    settings = _strict(settings, max_rework_cycles=0)
    raw = json.loads((SPEC_DIR / f"{SAMPLE_SLUG}.json").read_text(encoding="utf-8"))
    product_spec(session, sample_product, settings.products_dir)  # v4 on record before the failures
    advance_to(session, settings, sample_product, S.RED_TEAM)
    create_brief(session, sample_product, source="RED_TEAM", actor="claude", summary="Too much on one sheet",
                 required_changes=[{"id": "split_summary", "change": "Split it", "reason": "Headers cut off"}])
    transition(session, settings, sample_product, S.REWORK, actor="red_team", reason="Red Team REWORK")
    assert redesign_pending(session, sample_product)
    draft = settings.products_dir / "drafts" / f"product-{sample_product.id}" / "spec.json"

    def write(cmd, prompt):
        assert "drawing board" in prompt and "Too much on one sheet" in prompt and "Headers cut off" in prompt
        assert "previous-spec.json" in prompt and '"spec_version" to 5' in prompt
        draft.write_text(json.dumps(dict(raw, spec_version=5, name="Simpler Detailer Tracker")), encoding="utf-8")
        return {"structured_output": {"spec_path": "x", "summary": "Fewer sheets.", "spec_check_passed": True}}

    fake_claude.replies = [write]
    job = Job(product_id=sample_product.id, department=Department.PRODUCT_DESIGN, job_type="rework_plan",
              for_status=S.REWORK)
    session.add(job)
    session.flush()
    result = ProductDesign(settings)(sample_product, job)
    assert result.next_status == S.PRODUCTION and "Redesigned" in result.summary
    newest = session.scalars(select(SpecVersion).order_by(SpecVersion.version)).all()[-1]
    assert newest.version == 5 and newest.reason.startswith("Redesign")
    assert json.loads(newest.spec_json)["resolves"] == []
    assert session.scalar(select(ReworkBrief)).status == "SUPERSEDED"
    assert not redesign_pending(session, sample_product)
