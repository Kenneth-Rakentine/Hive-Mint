import json

import pytest
from sqlalchemy import select

from agents.departments import STUB_HANDLERS
from agents.operations import OperationsManager
from agents.rework import BriefFile, active_brief, create_brief
from app.lifecycle import transition
from app.models import ReworkBrief
from app.states import Department, ProductStatus as S
from tests.conftest import advance_to

CHANGES = [{"id": "merge_workbooks", "change": "Merge into one workbook", "reason": "Double entry"}]


def _rework(session, settings, product, changes=CHANGES):
    brief = create_brief(session, product, source="HUMAN", actor="owner", summary="Needs work",
                         required_changes=changes)
    transition(session, settings, product, S.REWORK, actor="owner", reason="Needs work")
    return brief


def test_brief_goes_with_a_new_production_job(session, settings, make_product):
    product = advance_to(session, settings, make_product(), S.HUMAN_REVIEW)  # no spec: stub world
    brief = _rework(session, settings, product)
    ops = OperationsManager(session, settings, handlers=STUB_HANDLERS)
    ops.run_job(ops.next_job(product))
    assert product.status == S.PRODUCTION

    job = ops.next_job(product)
    assert job.department == Department.PRODUCTION and job.job_type == "rework_build"
    attached = json.loads(job.input_json)["rework_brief"]
    assert attached["id"] == brief.id and attached["required_changes"][0]["id"] == "merge_workbooks"
    assert brief.status == ReworkBrief.IN_PRODUCTION and brief.production_job_id == job.id


def test_brief_the_spec_does_not_cover_pauses_instead_of_rebuilding(session, settings, sample_product, sample_spec):
    advance_to(session, settings, sample_product, S.HUMAN_REVIEW)
    _rework(session, settings, sample_product, [{"id": "not_built_yet", "change": "Something new",
                                                 "reason": "Buyers want it"}])
    ops = OperationsManager(session, settings, handlers=STUB_HANDLERS)
    ops.run_job(ops.next_job(sample_product))
    assert sample_product.status == S.PAUSED and sample_product.paused_from == S.REWORK
    assert "not_built_yet" in sample_product.status_reason and "resolves" in sample_product.status_reason
    assert ops.tick() == 0  # nothing churns while paused

    transition(session, settings, sample_product, S.REWORK, actor="owner", reason="resume")
    assert sample_product.rework_count == 1  # resuming is not a new rework cycle


def test_new_brief_supersedes_the_old_one(session, settings, make_product):
    product = make_product()
    first = create_brief(session, product, source="QA", actor="qa", summary="one", required_changes=CHANGES)
    second = create_brief(session, product, source="RED_TEAM", actor="rt", summary="two", required_changes=CHANGES)
    assert first.status == "SUPERSEDED" and active_brief(session, product) is second


@pytest.mark.parametrize("bad", [
    {"summary": "Needs work", "required_changes": []},
    {"summary": "Needs work", "required_changes": [{"id": "Bad Id", "change": "Merge files", "reason": "Because"}]},
])
def test_brief_file_validation(bad):
    with pytest.raises(ValueError):
        BriefFile.model_validate(bad)


def test_duplicate_change_ids_are_refused(session, make_product):
    with pytest.raises(ValueError, match="Duplicate"):
        create_brief(session, make_product(), source="HUMAN", actor="owner", summary="x",
                     required_changes=CHANGES * 2)
    assert session.scalar(select(ReworkBrief)) is None
