"""SEO department, listing rules, listing images and the owner's review desk. Claude is always faked."""

import json
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select

from agents.seo import SEO, ListingDraft, ListingRejected, draft_problems
from app.lifecycle import TransitionBlocked
from app.models import Event, Job, Listing
from app.review import approve, reject, relist, review_card
from app.states import Department, ProductStatus as S
from tests.conftest import advance_to, record_build
from tools.listing import AI_DISCLOSURE, listing_problems, upload_problems
from tools.listing_images import HEIGHT, WIDTH, image_problems, plan_slots, save_jpeg

TAGS = ["mobile detailing", "detailer tracker", "car detailing log", "income tracker", "expense tracker",
        "mileage log", "small business", "profit tracker", "detailing business", "bookkeeping sheet",
        "job tracker", "self employed", "side hustle"]


def good_draft(spec, **changes) -> dict:
    files = ", ".join(f["name"] for f in spec["files"])
    description = (
        "Know what every detailing job really earns. This tracker logs each job once and turns it into income, "
        "costs, profit and a mileage deduction you can see at a glance on a Dashboard with charts.\n\n"
        f"WHAT YOU GET: {files}.\n\nWHO IT IS FOR: one-person and small mobile detailing businesses.\n\n"
        "HOW TO USE: open the workbook, read How to Use, then log jobs, expenses, supplies and trips. "
        "It opens in desktop spreadsheet software and Google Sheets.\n\n"
        "This is a record-keeping tool and not tax advice; check deductions with a tax professional.")
    draft = {"title": "Mobile Detailing Business Tracker: Jobs, Income, Expenses, Mileage and Profit Dashboard",
             "tags": TAGS, "description": description, "price": 12.0, "price_reason": "Mid-range for trackers.",
             "claims": [{"text": "Mileage deduction worked out for you", "feature_id": "mileage_deduction"},
                        {"text": "Dashboard with charts", "feature_id": "dashboard_charts"}],
             "faq": [{"question": f"Question {i}?", "answer": "Answer."} for i in range(3)],
             "images": [{"slot": "cover", "headline": "Know your real profit", "bullets": ["Log jobs once"]},
                        {"slot": "included", "headline": "Everything in the download", "bullets": []}],
             "category": "Paper & Party Supplies > Paper > Calendars & Planners"}
    return draft | changes


def clean_review(cmd, prompt):
    """A picture review that inspected every picture listed in its prompt and found nothing."""
    import re
    return {"structured_output": {"checked": re.findall(r"^- (\S+\.jpg):", prompt, re.M), "defects": []}}


def as_listing(draft: dict) -> dict:
    return draft | {"description": draft["description"] + "\n\n" + AI_DISCLOSURE}


# --------------------------------------------------------------------------- listing rules

def test_a_good_listing_passes(sample_spec):
    assert listing_problems(as_listing(good_draft(sample_spec)), sample_spec) == []


@pytest.mark.parametrize("change, expected", [
    ({"title": "x" * 141}, "title is 141 characters"),
    ({"tags": TAGS[:12]}, "12 tags; exactly 13"),
    ({"tags": TAGS[:12] + ["a tag that is far too long"]}, "the limit is 20"),
    ({"tags": TAGS[:12] + ["tag!"]}, "characters tags may not contain"),
    ({"tags": TAGS[:12] + ["mileage log"]}, "tags repeat"),
    ({"title": "Excel Mobile Detailing Tracker"}, "brand names: Excel"),
    ({"claims": [{"text": "Syncs with your bank", "feature_id": "bank_sync"}]}, "unknown feature 'bank_sync'"),
    ({"claims": []}, "no feature claims"),
    ({"price": 99}, "outside $2-$60"),
])
def test_listing_rules_catch_each_problem(sample_spec, change, expected):
    problems = listing_problems(as_listing(good_draft(sample_spec, **change)), sample_spec)
    assert any(expected in p for p in problems), problems


def test_description_must_name_files_keep_the_disclaimer_and_avoid_forbidden_claims(sample_spec):
    draft = good_draft(sample_spec)
    draft["description"] = (draft["description"].replace("Instructions.pdf", "a guide")
                            .replace("not tax advice", "general info") + " It is IRS-approved.")
    problems = listing_problems(as_listing(draft), sample_spec)
    assert any("Instructions.pdf" in p for p in problems)
    assert any("not tax advice" in p for p in problems)
    assert any("IRS-approved" in p for p in problems)
    assert any("QuickBooks" in p for p in listing_problems(
        as_listing(good_draft(sample_spec, description=good_draft(sample_spec)["description"] + " Like QuickBooks.")),
        sample_spec))


def test_the_ai_disclosure_is_required(sample_spec):
    assert any("disclose that AI" in p for p in listing_problems(good_draft(sample_spec), sample_spec))


def test_draft_rules_check_slots_ai_wording_and_researched_price(sample_spec):
    slots = ["cover", "dashboard", "included"]
    draft = ListingDraft.model_validate(good_draft(sample_spec, price=30.0))
    problems = draft_problems(draft, sample_spec, slots, (8.0, 18.0))
    assert any("no image copy for slots: dashboard" in p for p in problems)
    assert any("outside the researched $8.00-$18.00" in p for p in problems)
    ai = good_draft(sample_spec)
    ai["description"] += " Built with AI."
    assert any("mentions AI" in p for p in draft_problems(ListingDraft.model_validate(ai), sample_spec,
                                                          ["cover", "included"], None))


def test_upload_rules_follow_the_marketplace_file_limits(tmp_path):
    xlsx, big = tmp_path / "a.xlsx", tmp_path / "big.zip"
    xlsx.write_bytes(b"x")
    with big.open("wb") as handle:
        handle.truncate(21 * 1024 * 1024)
    problems = upload_problems([xlsx, big])
    assert any(".xlsx files cannot be uploaded" in p for p in problems)
    assert any("big.zip is 21.0 MB" in p for p in problems)
    small = []
    for i in range(6):
        (path := tmp_path / f"f{i}.pdf").write_bytes(b"%PDF")
        small.append(path)
    assert any("6 downloadable files" in p for p in upload_problems(small))
    (spaced := tmp_path / "My Tracker.zip").write_bytes(b"PK")
    assert any("file names must be" in p for p in upload_problems([spaced]))


# --------------------------------------------------------------------------- images

def test_images_are_listing_sized_jpegs_under_a_megabyte(tmp_path, sample_spec):
    from tools.listing_images import Slot, draw_slot

    slide = draw_slot(Slot("included", None), {"headline": "Everything included", "bullets": ["One", "Two"]},
                      sample_spec)
    path = save_jpeg(slide, tmp_path / "x.jpg")
    assert path.stat().st_size <= 1_000_000 and image_problems([path]) == []
    noisy = Image.effect_noise((WIDTH, HEIGHT), 120).convert("RGB")  # cannot compress under 1 MB
    assert any("under 1 MB" in p for p in image_problems([save_jpeg(noisy, tmp_path / "noise.jpg")]))
    Image.new("RGB", (800, 600)).save(small := tmp_path / "small.jpg")
    assert any("expected 2700x2025" in p for p in image_problems([small]))


def test_slots_without_renders_are_the_cover_and_the_file_list(sample_spec, good_build):
    assert [s.id for s in plan_slots(sample_spec, good_build)] == ["cover", "included"]


# --------------------------------------------------------------------------- the SEO department

def _seo_ready(session, settings, sample_product, sample_spec, build_copy):
    advance_to(session, settings, sample_product, S.SEO)
    build = record_build(session, sample_product, build_copy, sample_spec)
    job = Job(product_id=sample_product.id, department=Department.SEO, job_type="seo_metadata", for_status=S.SEO)
    session.add(job)
    session.flush()
    return build, job


def test_seo_writes_a_complete_listing_package(session, settings, sample_product, sample_spec, build_copy, fake_claude):
    build, job = _seo_ready(session, settings, sample_product, sample_spec, build_copy)
    fake_claude.replies = [{"structured_output": good_draft(sample_spec)}, clean_review]
    result = SEO(settings)(sample_product, job)
    assert result.next_status == S.HUMAN_REVIEW, result.summary
    out = Path(build.path) / "listing"
    listing = json.loads((out / "listing.json").read_text(encoding="utf-8"))
    assert listing["description"].endswith(AI_DISCLOSURE)
    assert listing["image_files"] == ["01-cover.jpg", "02-included.jpg"]
    assert image_problems(sorted((out / "images").glob("*.jpg"))) == []
    assert (out / "upload" / sample_spec["zip_name"]).is_file()
    text = (out / "LISTING.txt").read_text(encoding="utf-8")
    assert "TAGS (13" in text and "QUANTITY: 999" in text and f"SKU: HM-{sample_product.id:03d}-V" in text
    assert listing["sku"] in (out / "UPLOAD_STEPS.txt").read_text(encoding="utf-8")
    assert "UPLOAD" in (out / "UPLOAD_STEPS.txt").read_text(encoding="utf-8").upper()
    row = session.scalar(select(Listing))
    assert (row.build_id, row.price_cents) == (build.id, 1200)
    cmd = fake_claude.calls[0][0]
    assert cmd[cmd.index("--tools") + 1] == "Read,Glob,Grep"


def test_a_rejected_draft_is_retried_with_the_problems(session, settings, sample_product, sample_spec, build_copy,
                                                       fake_claude):
    _, job = _seo_ready(session, settings, sample_product, sample_spec, build_copy)
    fake_claude.replies = [{"structured_output": good_draft(sample_spec, title="x" * 200)},
                           {"structured_output": good_draft(sample_spec)}, clean_review]
    assert SEO(settings)(sample_product, job).next_status == S.HUMAN_REVIEW
    assert "title is 200 characters" in fake_claude.calls[1][1]
    assert session.scalar(select(Event).where(Event.event_type == "listing.rejected")) is not None


def test_two_bad_drafts_raise_so_operations_retries_or_errors(session, settings, sample_product, sample_spec,
                                                             build_copy, fake_claude):
    _, job = _seo_ready(session, settings, sample_product, sample_spec, build_copy)
    fake_claude.replies = [{"structured_output": good_draft(sample_spec, price=500)}] * 2
    with pytest.raises(ListingRejected, match="outside"):
        SEO(settings)(sample_product, job)


# --------------------------------------------------------------------------- the review desk

def _in_review(session, settings, sample_product, sample_spec, build_copy, fake_claude, with_listing=True):
    _, job = _seo_ready(session, settings, sample_product, sample_spec, build_copy)
    if with_listing:
        fake_claude.replies = [{"structured_output": good_draft(sample_spec)}, clean_review]
        SEO(settings)(sample_product, job)
    from app.lifecycle import transition
    transition(session, settings, sample_product, S.HUMAN_REVIEW, actor="seo", reason="listing ready")
    return sample_product


def test_approval_needs_a_listing_and_ends_ready_to_publish(session, settings, sample_product, sample_spec,
                                                            build_copy, fake_claude):
    product = _in_review(session, settings, sample_product, sample_spec, build_copy, fake_claude, with_listing=False)
    with pytest.raises(TransitionBlocked, match="No listing package"):
        approve(session, settings, product, approver="Kenny")
    relist(session, settings, product, actor="Kenny", reason="make one")
    assert product.status == S.SEO


def test_approve_records_who_and_moves_to_ready_to_publish(session, settings, sample_product, sample_spec,
                                                          build_copy, fake_claude):
    product = _in_review(session, settings, sample_product, sample_spec, build_copy, fake_claude)
    card = review_card(session, product)
    assert card["listing"]["price"] == 12.0 and card["listing"]["images"] == 2
    approve(session, settings, product, approver="Kenny", note="looks good")
    assert product.status == S.READY_TO_PUBLISH and product.approved_by == "Kenny"


def test_reject_needs_a_reason(session, settings, sample_product, sample_spec, build_copy, fake_claude):
    product = _in_review(session, settings, sample_product, sample_spec, build_copy, fake_claude)
    with pytest.raises(ValueError):
        reject(session, settings, product, actor="Kenny", reason=" ")
    reject(session, settings, product, actor="Kenny", reason="too similar to free templates")
    assert product.status == S.REJECTED


def test_only_a_product_that_came_from_seo_can_be_relisted(session, settings, make_product):
    from app.lifecycle import transition
    product = make_product()
    advance_to(session, settings, product, S.COMPLIANCE)
    transition(session, settings, product, S.HUMAN_REVIEW, actor="compliance", reason="ambiguous brand mention")
    with pytest.raises(TransitionBlocked, match="came from SEO"):
        relist(session, settings, product, actor="Kenny")


def test_cover_text_always_fits_inside_the_picture():
    from PIL import ImageDraw

    from tools.listing_images import COVER_TEXT_WIDTH, cover_text

    draw = ImageDraw.Draw(Image.new("RGB", (WIDTH, HEIGHT)))
    bullets = ["Month-by-month cash-flow calendar", "Who pledged, gave and still owes",
               "Costs follow your RSVP headcount"]
    (head, head_lines), (sub, sub_lines) = cover_text(draw, "Wedding Budget, Payments & Family Money", bullets)
    assert len(head_lines) <= 2
    for f, lines in ((head, head_lines), (sub, sub_lines)):
        assert all(draw.textlength(line, font=f) <= COVER_TEXT_WIDTH for line in lines)
    very_long = "A headline far too long to ever fit on two lines of a listing cover picture at full size"
    (head, head_lines), _ = cover_text(draw, very_long, [])
    assert all(draw.textlength(line, font=head) <= COVER_TEXT_WIDTH for line in head_lines)


def test_visible_picture_defects_send_the_product_to_rework(session, settings, sample_product, sample_spec,
                                                           build_copy, fake_claude):
    from app.models import ReworkBrief

    _, job = _seo_ready(session, settings, sample_product, sample_spec, build_copy)

    def flawed(cmd, prompt):
        names = clean_review(cmd, prompt)["structured_output"]["checked"]
        return {"structured_output": {"checked": names, "defects": [
            {"image": names[0], "where": "Start Here, Couple Names", "problem": "Name cut off: 'Jordan & Sam (exampl'",
             "fix": "Widen column B on Start Here so the couple's names fit"}]}}

    fake_claude.replies = [{"structured_output": good_draft(sample_spec)}, flawed]
    result = SEO(settings)(sample_product, job)
    assert result.next_status == S.REWORK and "cut off" in result.summary
    brief = session.scalar(select(ReworkBrief))
    assert brief.source == "SEO" and "Widen column B" in json.loads(brief.required_changes_json)[0]["change"]
    prompt = fake_claude.calls[-1][1]
    assert "cut off" in prompt and "01-cover.jpg" in prompt
    assert fake_claude.calls[-1][0][fake_claude.calls[-1][0].index("--tools") + 1] == "Read,Glob,Grep"


def test_a_review_that_skips_pictures_is_not_accepted(session, settings, sample_product, sample_spec, build_copy,
                                                      fake_claude):
    _, job = _seo_ready(session, settings, sample_product, sample_spec, build_copy)
    lazy = {"structured_output": {"checked": ["01-cover.jpg"], "defects": []}}
    fake_claude.replies = [{"structured_output": good_draft(sample_spec)}, lazy, lazy]
    with pytest.raises(ListingRejected, match="skipped"):
        SEO(settings)(sample_product, job)


def test_a_held_product_can_be_rejected(session, settings, make_product):
    from app.lifecycle import transition
    product = make_product()
    advance_to(session, settings, product, S.QA)
    transition(session, settings, product, S.PAUSED, actor="test", reason="rework limit reached")
    reject(session, settings, product, actor="Kenny", reason="QA failed four times")
    assert product.status == S.REJECTED
