import json
from pathlib import Path

import pytest
from sqlalchemy import select

from agents.departments import STUB_HANDLERS
from agents.operations import OperationsManager
from agents.rework import create_brief
from agents.spec_writer import ProductDesign, spec_problems
from app.models import Event, Job, Opportunity, ResearchRecord, SpecVersion
from app.states import Department, JobStatus, ProductStatus as S
from tests.conftest import SAMPLE_SLUG, advance_to
from tools.spec import SPEC_DIR, SpecError
from tools.spec_store import product_spec, save_version


@pytest.fixture(scope="module")
def raw_spec():
    return json.loads((SPEC_DIR / f"{SAMPLE_SLUG}.json").read_text(encoding="utf-8"))


def _draft(settings, product) -> Path:
    return settings.products_dir / "drafts" / f"product-{product.id}" / "spec.json"


def _job(session, product):
    job = Job(product_id=product.id, department=Department.PRODUCT_DESIGN, job_type="t", for_status=product.status)
    session.add(job)
    session.flush()
    return job


def _answer(**kw):
    return {"structured_output": {"spec_path": "products/drafts/x/spec.json", "summary": "Done.",
                                  "spec_check_passed": True} | kw}


# --------------------------------------------------------------------------- spec store

def test_owner_spec_is_imported_once_and_newer_file_versions_are_imported_with_a_diff(
        session, settings, sample_product, raw_spec, tmp_path):
    spec = product_spec(session, sample_product, settings.products_dir)
    assert spec["spec_version"] == 4 and spec["files"][0]["sheets"][2]["columns"][0] == "Date"  # normalized
    row = session.scalar(select(SpecVersion))
    assert (row.version, row.author) == (4, "file-import")
    assert (settings.products_dir / str(sample_product.id) / "spec-v4.json").is_file()
    product_spec(session, sample_product, settings.products_dir)
    assert len(session.scalars(select(SpecVersion)).all()) == 1  # not re-imported

    edited = dict(raw_spec, spec_version=5, name="Renamed")
    (tmp_path / f"{SAMPLE_SLUG}.json").write_text(json.dumps(edited), encoding="utf-8")
    assert product_spec(session, sample_product, spec_dir=tmp_path)["name"] == "Renamed"
    newest = session.scalars(select(SpecVersion).order_by(SpecVersion.id)).all()[-1]
    assert (newest.version, newest.author) == (5, "human")
    assert '- "name": "Mobile Auto Detailer Income & Expense Tracker"' in newest.diff_text
    assert '+ "name": "Renamed"' in newest.diff_text


def test_save_version_rejects_invalid_or_non_increasing_specs(session, sample_product, raw_spec):
    save_version(session, sample_product, raw_spec, author="human", reason="r")
    with pytest.raises(SpecError, match="higher"):
        save_version(session, sample_product, raw_spec, author="human", reason="r")
    with pytest.raises(SpecError, match="slug"):
        save_version(session, sample_product, dict(raw_spec, spec_version=9, slug="other"), author="human", reason="r")
    with pytest.raises(SpecError):
        save_version(session, sample_product, {"slug": SAMPLE_SLUG, "spec_version": 10}, author="human", reason="r")


def test_spec_problems_enforce_product_rules(raw_spec):
    assert spec_problems(raw_spec) == []
    assert "claims_not_to_make is empty" in spec_problems(dict(raw_spec, claims_not_to_make=[]))
    broken = json.loads(json.dumps(raw_spec))
    for scenario in broken["files"][0]["scenarios"]:
        scenario["inputs"] = [r for r in scenario["inputs"] if r["sheet"] != "Supplies"]
        scenario["expect"] = [r for r in scenario["expect"] if r["sheet"] != "Supplies"]
    assert any("'Supplies' has formulas but no scenario" in p for p in spec_problems(broken))


# --------------------------------------------------------------------------- SPECIFICATION

def test_owner_product_uses_its_spec_without_claude(session, settings, sample_product, fake_claude):
    product = advance_to(session, settings, sample_product, S.SPECIFICATION)
    result = ProductDesign(settings)(product, _job(session, product))
    assert result.next_status == S.PRODUCTION and not fake_claude.calls


def _researched_product(session, settings, make_product):
    record = ResearchRecord(niche="mobile services", summary="s", confidence="high", sources_json="[]",
                            findings_json=json.dumps({"complaints": ["no mileage"]}))
    session.add(record)
    session.flush()
    product = advance_to(session, settings, make_product(), S.SPECIFICATION)
    session.add(Opportunity(research_id=record.id, product_id=product.id, title="Mobile Groomer Tracker",
                            product_type="spreadsheet", description="d", data_json="{}", score=80.0))
    session.flush()
    return product


def test_spec_writer_drafts_checks_and_stores_v1(session, settings, make_product, fake_claude, raw_spec):
    product = _researched_product(session, settings, make_product)

    def write(cmd, prompt):
        assert "spec-check products/" in prompt or "spec-check " in prompt
        assert "Mobile Groomer Tracker" in prompt and "no mileage" in prompt
        _draft(settings, product).parent.mkdir(parents=True, exist_ok=True)
        _draft(settings, product).write_text(json.dumps(dict(raw_spec, slug="whatever", spec_version=7)))
        return _answer(summary="A groomer tracker.")

    fake_claude.replies = [write]
    result = ProductDesign(settings)(product, _job(session, product))
    assert result.next_status == S.PRODUCTION
    row = session.scalar(select(SpecVersion))
    assert (row.version, row.author, row.spec["slug"]) == (1, "claude-headless", product.slug)
    cmd = fake_claude.calls[0][0]
    assert cmd[cmd.index("--tools") + 1] == "Read,Glob,Grep,Write,Edit,Bash"


def test_unusable_spec_is_retried_within_the_bound_then_errors(session, settings, make_product, fake_claude, raw_spec):
    product = _researched_product(session, settings, make_product)

    def bad(cmd, prompt):
        _draft(settings, product).parent.mkdir(parents=True, exist_ok=True)
        _draft(settings, product).write_text(json.dumps(dict(raw_spec, claims_not_to_make=[])))
        return _answer()

    fake_claude.replies = [bad, {"is_error": True, "subtype": "error_during_execution", "_exit": 1}, bad]
    ops = OperationsManager(session, settings, handlers={**STUB_HANDLERS,
                                                         Department.PRODUCT_DESIGN: ProductDesign(settings)})
    job = ops.run_job(ops.next_job(product))
    assert job.status == JobStatus.FAILED and job.attempts == 3 and product.status == S.ERROR
    assert "claims_not_to_make is empty" in job.error
    assert len(fake_claude.calls) == 3


# --------------------------------------------------------------------------- REWORK

CHANGES = [{"id": "mileage_double_count_guard", "change": "Warn when fuel and mileage are both entered",
            "reason": "buyers double count", "acceptance": "warning cell non-empty"},
           {"id": "add_example_workbook", "change": "Ship an example workbook", "reason": "buyers want to see it",
            "acceptance": "example passes Testing"}]


def _rework_product(session, settings, sample_product, changes=CHANGES):
    product = advance_to(session, settings, sample_product, S.RED_TEAM)
    create_brief(session, product, source="RED_TEAM", actor="claude-code", summary="Two changes",
                 required_changes=changes)
    from app.lifecycle import transition
    transition(session, settings, product, S.REWORK, actor="red_team", reason="Red Team REWORK")
    return product


def test_rework_revises_the_spec_to_cover_the_brief_with_a_diff(session, settings, sample_product, fake_claude):
    product = _rework_product(session, settings, sample_product)

    def revise(cmd, prompt):
        assert "mileage_double_count_guard" in prompt and "set \"spec_version\" to 5" in prompt
        draft = _draft(settings, product)
        spec = json.loads(draft.read_text(encoding="utf-8"))  # the current spec was copied there
        assert spec["spec_version"] == 4
        spec["resolves"] += ["mileage_double_count_guard", "add_example_workbook"]
        spec["parameters"]["expense_categories"][1] = "Fuel (actual expense method only)"
        draft.write_text(json.dumps(spec), encoding="utf-8")
        return _answer(summary="Added the guard and an example workbook.")

    fake_claude.replies = [revise]
    result = ProductDesign(settings)(product, _job(session, product))
    assert result.next_status == S.PRODUCTION
    rows = session.scalars(select(SpecVersion).order_by(SpecVersion.version)).all()
    assert [r.version for r in rows] == [4, 5] and rows[1].brief_id is not None
    assert "Fuel (actual expense method only)" in rows[1].diff_text and '"add_example_workbook"' in rows[1].diff_text
    assert session.scalar(select(Event).where(Event.event_type == "spec.revised")) is not None
    assert (settings.products_dir / str(product.id) / "spec-v5.json").is_file()


def test_revision_that_skips_a_brief_change_is_rejected(session, settings, sample_product, fake_claude):
    product = _rework_product(session, settings, sample_product)

    def partial(cmd, prompt):
        draft = _draft(settings, product)
        spec = json.loads(draft.read_text(encoding="utf-8"))
        spec["resolves"].append("mileage_double_count_guard")
        draft.write_text(json.dumps(spec), encoding="utf-8")
        return _answer()

    fake_claude.replies = [partial]
    with pytest.raises(SpecError, match="add_example_workbook"):
        ProductDesign(settings)(product, _job(session, product))


def test_covered_brief_goes_straight_to_production(session, settings, sample_product, fake_claude):
    product = _rework_product(session, settings, sample_product,
                              changes=[{"id": "dashboard_charts", "change": "Add charts", "reason": "buyers"}])
    assert ProductDesign(settings)(product, _job(session, product)).next_status == S.PRODUCTION
    assert not fake_claude.calls


def test_failed_tests_without_a_brief_get_one_and_a_revision(session, settings, sample_product, fake_claude):
    from app.lifecycle import transition

    product = advance_to(session, settings, sample_product, S.TESTING)
    testing = Job(product_id=product.id, department=Department.TESTING, job_type="functional_tests",
                  for_status=S.TESTING, status=JobStatus.SUCCEEDED,
                  output_json=json.dumps({"failures": ["scenario [x]: expected 700, got 1300"]}))
    session.add(testing)
    session.flush()
    transition(session, settings, product, S.REWORK, actor="testing", reason="Testing failed 1 of 50 checks")

    def revise(cmd, prompt):
        assert "expected 700, got 1300" in prompt
        draft = _draft(settings, product)
        spec = json.loads(draft.read_text(encoding="utf-8"))
        spec["resolves"].append(f"fix_testing_failures_{testing.id}")
        draft.write_text(json.dumps(spec), encoding="utf-8")
        return _answer()

    fake_claude.replies = [revise]
    assert ProductDesign(settings)(product, _job(session, product)).next_status == S.PRODUCTION
