"""The owner's desk: strict picture checks, then approve, send back or leave for the owner. Claude is faked."""

import json
import re
from dataclasses import replace
from pathlib import Path

from PIL import Image
from sqlalchemy import select

from agents.owner_desk import APPROVER, OwnerDesk, desk_pending, needs_owner, pending_products
from agents.seo import SEO
from app.lifecycle import transition
from app.models import Event, Job, ReworkBrief
from app.review import reject
from app.states import Department, ProductStatus as S
from tests.conftest import advance_to
from tests.test_seo import _in_review, clean_review, good_draft

FLAGS = ("text_cut_off", "error_or_filler", "missing_example_data", "overlap_or_misaligned", "unreadable",
         "contradicts_listing")


def verdict(decision="APPROVE", flag=None, issues=(), skip=0, question=""):
    """A desk answer that checked every picture in its prompt (minus `skip`), ticking `flag` on the first."""
    def reply(cmd, prompt):
        names = re.findall(r"^- (\S+\.jpg):", prompt, re.M)
        names = names[:len(names) - skip]
        pictures = [{"image": n, **{f: (f == flag and i == 0) for f in FLAGS},
                     "notes": "header 'Mileage Deduct' is cut off" if flag and i == 0 else ""}
                    for i, n in enumerate(names)]
        return {"structured_output": {"decision": decision, "summary": "Checked every picture and the listing.",
                                      "pictures": pictures, "issues": list(issues), "owner_question": question}}
    return reply


def _ready(session, settings, sample_product, sample_spec, build_copy, fake_claude):
    product = _in_review(session, settings, sample_product, sample_spec, build_copy, fake_claude)
    session.commit()
    return product


def test_a_clean_review_approves_and_moves_to_ready_to_publish(session, settings, sample_product, sample_spec,
                                                               build_copy, fake_claude):
    product = _ready(session, settings, sample_product, sample_spec, build_copy, fake_claude)
    assert desk_pending(session, settings, product) and not needs_owner(session, settings, product)
    fake_claude.replies = [verdict()]
    result = OwnerDesk(settings).review(session, product)
    assert result.decision == "APPROVE"
    assert product.status == S.READY_TO_PUBLISH and product.approved_by == APPROVER
    cmd, prompt = fake_claude.calls[-1]
    assert cmd[cmd.index("--tools") + 1] == "Read,Glob,Grep"
    assert "01-cover.jpg" in prompt and "02-included.jpg" in prompt and "text_cut_off" in prompt
    job = session.scalar(select(Job).where(Job.department == Department.OWNER_DESK))
    assert job.status == "SUCCEEDED"
    assert not desk_pending(session, settings, product)


def test_a_ticked_box_sends_it_back_even_if_the_model_says_approve(session, settings, sample_product, sample_spec,
                                                                   build_copy, fake_claude):
    product = _ready(session, settings, sample_product, sample_spec, build_copy, fake_claude)
    fake_claude.replies = [verdict("APPROVE", flag="text_cut_off")]
    assert OwnerDesk(settings).review(session, product).decision == "SEND_BACK"
    assert product.status == S.REWORK and product.approved_by is None
    brief = session.scalar(select(ReworkBrief))
    assert brief.source == "OWNER_DESK" and "cut off" in brief.required_changes_json


def test_a_listing_only_problem_goes_back_to_seo_with_the_desks_notes(session, settings, sample_product, sample_spec,
                                                                      build_copy, fake_claude):
    product = _ready(session, settings, sample_product, sample_spec, build_copy, fake_claude)
    issue = {"area": "listing", "where": "description", "problem": "Says 'Google Sheets' twice in a row",
             "fix": "Mention Google Sheets once"}
    fake_claude.replies = [verdict("SEND_BACK", issues=[issue])]
    OwnerDesk(settings).review(session, product)
    assert product.status == S.SEO
    job = Job(product_id=product.id, department=Department.SEO, job_type="seo_metadata", for_status=S.SEO)
    session.add(job)
    session.flush()
    fake_claude.replies = [{"structured_output": good_draft(sample_spec)}, clean_review]
    SEO(settings)(product, job)
    assert "Mention Google Sheets once" in fake_claude.calls[-2][1]


def test_a_review_that_skips_a_picture_leaves_the_product_for_the_owner(session, settings, sample_product,
                                                                        sample_spec, build_copy, fake_claude):
    product = _ready(session, settings, sample_product, sample_spec, build_copy, fake_claude)
    fake_claude.replies = [verdict(skip=1), verdict(skip=1)]
    assert OwnerDesk(settings).review(session, product).decision == "NEEDS_OWNER"
    assert product.status == S.HUMAN_REVIEW and "desk needs you" in product.status_reason
    assert "skipped" in fake_claude.calls[-1][1]  # the retry was told what it missed
    assert needs_owner(session, settings, product) and not desk_pending(session, settings, product)


def test_needs_owner_stays_in_review_with_the_question(session, settings, sample_product, sample_spec, build_copy,
                                                       fake_claude):
    product = _ready(session, settings, sample_product, sample_spec, build_copy, fake_claude)
    fake_claude.replies = [verdict("NEEDS_OWNER", question="Is a mileage tool tax advice in your state?")]
    OwnerDesk(settings).review(session, product)
    assert product.status == S.HUMAN_REVIEW and "mileage tool" in product.status_reason
    assert pending_products(session, settings) == []


def test_a_swapped_download_is_caught_without_asking_claude(session, settings, sample_product, sample_spec,
                                                           build_copy, fake_claude):
    product = _ready(session, settings, sample_product, sample_spec, build_copy, fake_claude)
    upload = next((Path(build_copy) / "listing" / "upload").glob("*.zip"))
    upload.write_bytes(b"not the tested zip")
    calls = len(fake_claude.calls)
    assert OwnerDesk(settings).review(session, product).decision == "SEND_BACK"
    assert len(fake_claude.calls) == calls and product.status == S.SEO


def test_a_blank_picture_is_caught_without_asking_claude(session, settings, sample_product, sample_spec, build_copy,
                                                        fake_claude):
    product = _ready(session, settings, sample_product, sample_spec, build_copy, fake_claude)
    cover = Path(build_copy) / "listing" / "images" / "01-cover.jpg"
    with Image.open(cover) as image:
        Image.new("RGB", image.size, "white").save(cover, "JPEG")
    calls = len(fake_claude.calls)
    OwnerDesk(settings).review(session, product)
    assert len(fake_claude.calls) == calls and product.status == S.SEO
    assert "looks blank" in session.scalar(select(Event).where(Event.event_type == "owner_desk.reviewed")).message


def test_listing_redos_are_bounded(session, settings, sample_product, sample_spec, build_copy, fake_claude):
    product = _ready(session, settings, sample_product, sample_spec, build_copy, fake_claude)
    issue = {"area": "listing", "where": "title", "problem": "Title repeats a word", "fix": "Drop the repeat"}
    desk = OwnerDesk(settings)
    for _ in range(2):
        fake_claude.replies = [verdict("SEND_BACK", issues=[issue])]
        desk.review(session, product)
        assert product.status == S.SEO
        job = Job(product_id=product.id, department=Department.SEO, job_type="seo_metadata", for_status=S.SEO)
        session.add(job)
        session.flush()
        fake_claude.replies = [{"structured_output": good_draft(sample_spec)}, clean_review]
        transition(session, settings, product, SEO(settings)(product, job).next_status, actor="seo", reason="ok")
    fake_claude.replies = [verdict("SEND_BACK", issues=[issue])]
    assert desk.review(session, product).decision == "NEEDS_OWNER"
    assert product.status == S.HUMAN_REVIEW


def test_with_auto_approve_off_a_clean_review_is_only_a_recommendation(session, settings, sample_product, sample_spec,
                                                                       build_copy, fake_claude):
    settings = replace(settings, desk_auto_approve=False)
    product = _ready(session, settings, sample_product, sample_spec, build_copy, fake_claude)
    fake_claude.replies = [verdict()]
    OwnerDesk(settings).review(session, product)
    assert product.status == S.HUMAN_REVIEW and product.status_reason.startswith("Recommended")


def test_compliance_referrals_never_go_to_the_desk(session, settings, make_product):
    product = make_product()
    advance_to(session, settings, product, S.COMPLIANCE)
    transition(session, settings, product, S.HUMAN_REVIEW, actor="compliance", reason="brand name?")
    assert not desk_pending(session, settings, product) and needs_owner(session, settings, product)


def test_the_owner_can_take_back_a_desk_approval(session, settings, sample_product, sample_spec, build_copy,
                                                 fake_claude):
    product = _ready(session, settings, sample_product, sample_spec, build_copy, fake_claude)
    fake_claude.replies = [verdict()]
    OwnerDesk(settings).review(session, product)
    reject(session, settings, product, actor="Kenny", reason="Not the look I want for the shop")
    assert product.status == S.REJECTED


def test_one_extra_rework_cycle_is_granted_automatically_then_it_pauses(session, settings, make_product):
    settings = replace(settings, max_rework_cycles=1, auto_rework_grants=1, redesigns_per_product=0,
                       auto_drop_failed=False)
    product = make_product()
    advance_to(session, settings, product, S.QA)
    transition(session, settings, product, S.REWORK, actor="qa", reason="fail 1")
    for reason in ("fail 2", "fail 3"):
        transition(session, settings, product, S.PRODUCTION, actor="design", reason="rebuild")
        advance_to(session, settings, product, S.QA)
        transition(session, settings, product, S.REWORK, actor="qa", reason=reason)
        if reason == "fail 2":
            assert product.status == S.REWORK
    assert product.status == S.PAUSED
    grants = session.scalars(select(Event).where(Event.event_type == "rework.auto_granted")).all()
    assert len(grants) == 1


def test_the_shift_runs_the_desk_after_seo(session, settings, sample_product, sample_spec, build_copy, fake_claude):
    from app.shift import ShiftRunner

    product = _ready(session, settings, sample_product, sample_spec, build_copy, fake_claude)
    seen = []

    def desk(s, p):
        seen.append(p.id)
        from agents.owner_desk import DeskResult
        return DeskResult("NEEDS_OWNER", "test")

    runner = ShiftRunner(session, settings, ignore_window=True, battery=lambda: False, desk=desk,
                         subscription_check=lambda st: None, scout=lambda s, st: [],
                         researcher=lambda *a: (_ for _ in ()).throw(AssertionError("no research")))
    count, summary = runner._do_desk()
    assert seen == [product.id] and count == 1 and "NEEDS_OWNER" in summary



def test_report_and_office_separate_what_needs_the_owner(session, settings, sample_product, sample_spec, build_copy,
                                                        fake_claude):
    from datetime import datetime, timezone

    from app.office import office_state
    from app.report import build_report

    product = _ready(session, settings, sample_product, sample_spec, build_copy, fake_claude)
    state = office_state(session, settings, disk={})
    assert state["resources"]["waiting_for_you"] == 0  # the desk hasn't looked yet: nothing needs Kenny
    assert "Waiting for the owner's desk" in build_report(session, settings, datetime.now(timezone.utc))
    fake_claude.replies = [verdict()]
    OwnerDesk(settings).review(session, product)
    session.add(Event(event_type="listing.drafted", actor="claude-session", product_id=product.id,
                      message="Etsy draft saved; the owner checks it and publishes"))
    session.flush()
    report = build_report(session, settings, datetime.now(timezone.utc))
    assert "approved by the owner's desk" in report and "Etsy draft saved" in report
    assert office_state(session, settings, disk={})["resources"]["approved_by_desk"] == 1


def test_the_desk_command_records_a_verdict(session, engine, settings, sample_product, sample_spec, build_copy,
                                            fake_claude, monkeypatch, capsys):
    from app import cli

    product = _ready(session, settings, sample_product, sample_spec, build_copy, fake_claude)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "get_engine", lambda: engine)
    fake_claude.replies = [verdict()]
    assert cli.main(["desk", str(product.id)]) == 0
    assert "APPROVE" in capsys.readouterr().out
    assert cli.main(["drafted", str(product.id), "--note", "photos to add"]) == 0
    assert cli.main(["review"]) == 0
    assert "approved by the owner's desk" in " ".join(capsys.readouterr().out.split())


def test_a_product_paused_before_auto_grants_existed_gets_its_extra_cycle_once(session, settings, make_product):
    from app.lifecycle import auto_resume_rework

    strict = replace(settings, max_rework_cycles=0, auto_rework_grants=0, redesigns_per_product=0,
                     auto_drop_failed=False)
    product = make_product()
    advance_to(session, strict, product, S.QA)
    transition(session, strict, product, S.REWORK, actor="qa", reason="fail")
    assert product.status == S.PAUSED
    once = replace(settings, max_rework_cycles=0, auto_rework_grants=1, redesigns_per_product=0,
                   auto_drop_failed=False)
    assert auto_resume_rework(session, once, product) and product.status == S.REWORK
    transition(session, once, product, S.PRODUCTION, actor="design", reason="rebuild")
    advance_to(session, once, product, S.QA)
    transition(session, once, product, S.REWORK, actor="qa", reason="fail again")
    assert product.status == S.PAUSED
    assert not auto_resume_rework(session, once, product)
