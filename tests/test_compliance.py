import json
import shutil

from agents.compliance import compliance_findings, compliance_gate, product_texts
from app.models import ReworkBrief
from app.states import ProductStatus as S
from tests.conftest import advance_to, record_build


def test_clean_detailer_build_passes(sample_spec, good_build):
    claims, ambiguous = compliance_findings(sample_spec, product_texts(sample_spec, good_build))
    assert claims == [] and ambiguous == []


def test_forbidden_claim_and_missing_disclaimer_and_brand_are_found(sample_spec):
    texts = {"README.txt": "This IRS-approved tracker works great.", "Instructions.pdf": "Use it with Airbnb hosts.",
             "Mobile_Detailer_Business_Tracker.xlsx": "Open in Microsoft Excel or Google Sheets."}
    claims, ambiguous = compliance_findings(sample_spec, texts)
    assert claims == ['README.txt says "IRS-approved"']
    assert 'README.txt lacks the disclaimer "not tax advice"' in ambiguous
    assert "Instructions.pdf mentions brand names: Airbnb" in ambiguous
    assert not any("Excel" in a for a in ambiguous)  # naming the software it opens in is fine


def _at_compliance(session, settings, sample_product, sample_spec, build_dir):
    product = advance_to(session, settings, sample_product, S.COMPLIANCE)
    record_build(session, product, build_dir, sample_spec)
    return product


def test_gate_routes_clean_to_seo_claims_to_rework_ambiguity_to_a_person(session, settings, sample_product,
                                                                       sample_spec, good_build, tmp_path):
    gate = compliance_gate(settings)
    product = _at_compliance(session, settings, sample_product, sample_spec, good_build)
    assert gate(product, None).next_status == S.SEO

    claimed = tmp_path / "claimed"
    shutil.copytree(good_build, claimed)
    readme = claimed / "README.txt"
    readme.write_text(readme.read_text(encoding="utf-8") + "\nGuaranteed savings every year.\n", encoding="utf-8")
    record_build(session, product, claimed, sample_spec, version=2)
    result = gate(product, None)
    assert result.next_status == S.REWORK and "guaranteed savings" in result.summary
    brief = session.get(ReworkBrief, result.data["rework_brief_id"])
    assert brief.source == "COMPLIANCE" and brief.change_ids == ["remove_forbidden_claims_v2"]

    unclear = tmp_path / "unclear"
    shutil.copytree(good_build, unclear)
    (unclear / "README.txt").write_text("A tracker. Made for Airbnb hosts.\n", encoding="utf-8")
    record_build(session, product, unclear, sample_spec, version=3)
    result = gate(product, None)
    assert result.next_status == S.HUMAN_REVIEW
    assert "Airbnb" in json.dumps(result.data["ambiguous"]) and "not tax advice" in result.summary
