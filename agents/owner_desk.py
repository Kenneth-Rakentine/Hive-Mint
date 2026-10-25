"""The owner's desk: the last look before a product is cleared for upload, done for the owner.

Owner decision (2026-10-25): the desk may approve products outright, as long as the picture checks
are strict. The owner looks at approved products later, when he uploads them; publishing stays manual
(MAX_PUBLICATIONS_PER_DAY is 0) and nothing here touches a storefront.

A product reaches the desk in HUMAN_REVIEW straight from SEO. Compliance referrals (ambiguity) are
never the desk's: they wait for the owner. The desk runs once per listing:

1. Deterministic checks first, no AI: the listing package is complete; the download in it is the exact
   ZIP that passed Testing (same hash); listing.json still passes the listing rules; every picture is
   a listing-sized JPEG, and none is blank; SEO's picture review ran on this listing and found nothing.
   Any failure sends the listing back to SEO without spending a Claude run.
2. One headless Claude run (read-only) is a second, independent look. It must open every picture and
   fill in a fixed checklist for each one (cut-off text, error values or filler, missing example data,
   overlap or misalignment, unreadable at thumbnail size, contradicts the listing), then read the
   listing text against the files.
3. Code decides from the answer, not the model's verdict alone:
   - any ticked checklist box or any issue -> SEND_BACK, whatever the model's decision said;
     issues with the product -> REWORK with a brief; listing-only issues -> SEO for a new listing
     (after MAX_LISTING_REDOS redos of one build, the owner decides instead);
   - NEEDS_OWNER -> stays in HUMAN_REVIEW with the desk's question; the desk does not ask again;
   - APPROVE with every picture checked and clean -> approval recorded as the desk and the product
     moves to READY_TO_PUBLISH (or, with DESK_AUTO_APPROVE off, stays with a recommendation).
   A run that skips a picture or answers badly is retried once; then the owner decides.

Every desk verdict is a Review row (department OWNER_DESK) tied to the listing it judged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from PIL import Image, ImageStat
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agents.rework import create_brief
from app.db import utcnow
from app.lifecycle import log_event, record_approval, transition
from app.models import Build, Event, Job, Listing, Product, Review
from app.states import Department, JobStatus, ProductStatus
from config.settings import ROOT, Settings
from providers import headless_policy
from providers.base import ProviderError
from tools.listing import listing_problems
from tools.listing_images import image_problems
from tools.packaging import sha256
from tools.spec_store import product_spec

S = ProductStatus
ACTOR = "owner_desk"
APPROVER = "Claude (owner's desk)"
TRIES = 2
MAX_LISTING_REDOS = 2  # desk send-backs of one build's listing before the owner decides
BLANK_STDDEV = 4.0  # a picture whose pixels barely vary is blank or broken

APPROVE, SEND_BACK, NEEDS_OWNER = "APPROVE", "SEND_BACK", "NEEDS_OWNER"

CHECK_FIELDS = {
    "text_cut_off": "text cut off or clipped",
    "error_or_filler": "error values or filler text",
    "missing_example_data": "missing or empty example data",
    "overlap_or_misaligned": "overlapping or misaligned parts",
    "unreadable": "too small or faint to read",
    "contradicts_listing": "contradicts the listing",
}


class PictureCheck(BaseModel):
    image: str = Field(description="File name of the picture, exactly as listed")
    text_cut_off: bool = Field(description="Any word, number, name or header stops mid-word or runs past its "
                                           "cell, box, sheet edge or picture edge")
    error_or_filler: bool = Field(description="####, #REF!, #DIV/0!, #N/A, #VALUE!, NaN, lorem ipsum, TBD, XXX")
    missing_example_data: bool = Field(description="An example column, row, key, swatch or chart that should be "
                                                   "filled in is empty")
    overlap_or_misaligned: bool = Field(description="Text or shapes overlap, or labels don't line up with values")
    unreadable: bool = Field(description="Headline or key text too small or faint to read at thumbnail size")
    contradicts_listing: bool = Field(description="The picture shows something the listing doesn't say, or "
                                                  "the reverse, or looks unfinished")
    notes: str = Field(default="", description="What you saw, especially for any box you ticked")


class DeskIssue(BaseModel):
    area: Literal["product", "listing"] = Field(description="'product' if the files must change; 'listing' if "
                                                            "only the listing text or picture copy must change")
    where: str = Field(description="Picture, file, sheet, cell or listing field")
    problem: str = Field(min_length=5)
    fix: str = Field(min_length=5, description="What to change so it no longer happens")


class DeskVerdict(BaseModel):
    decision: Literal["APPROVE", "SEND_BACK", "NEEDS_OWNER"]
    summary: str = Field(min_length=10, description="Two or three sentences the owner reads later")
    pictures: list[PictureCheck] = Field(description="One entry for every picture listed, in order")
    issues: list[DeskIssue] = Field(default_factory=list)
    owner_question: str = Field(default="", description="For NEEDS_OWNER: what only the owner can decide")


PROMPT = """You are the owner's desk at Hive-Mint, a one-person shop selling digital spreadsheet products on Etsy.
The owner has trusted you to approve products for sale while he is away; he looks at them only later. Every gate
before you passed (tests, QA, an adversarial Red Team, compliance, SEO and a first picture review), but you are the
last check before real buyers pay for this, so look as hard as a picky buyer zooming in. Be strict: when in doubt,
send it back. A wrong approval costs refunds and reviews; a send-back only costs a rebuild.

PRODUCT: {name}
The product files are in {build_dir} (open the workbooks' renders, the PDF guide and the README if useful; change
nothing).

LISTING
Title: {title}
Price: ${price:.2f}   Quantity: 999   SKU: {sku}
Tags: {tags}
Description:
{description}

PICTURES (open EVERY one with the Read tool and look closely at all of it):
{pictures}

For EACH picture, fill in the checklist honestly; tick a box (true) for anything you can actually see:
- text_cut_off: a word, number, name or header stops mid-word or runs past its cell, box, the sheet or the picture
  (e.g. "Jordan & Sam (exampl", "Mileage Deduct", "builds o");
- error_or_filler: ####, #REF!, #DIV/0!, #N/A, #VALUE!, NaN, lorem ipsum, TBD, XXX;
- missing_example_data: an example column, row, key, colour swatch or chart that should be filled in is empty;
- overlap_or_misaligned: text or shapes overlap, labels don't line up with their values, boxes are uneven;
- unreadable: headline or key text too small or faint to read as an Etsy thumbnail;
- contradicts_listing: the picture and the listing disagree, or the product looks unfinished.
Then read the listing text: anything untrue, unclear, misspelled or promising more than the files deliver?

DECIDE
- APPROVE only if every picture is clean, the listing is accurate, and you would be glad to buy it.
- SEND_BACK with one issue per problem (area "product" if the files must change, "listing" if only the listing
  text or picture copy must change).
- NEEDS_OWNER only for what you can't judge: legal, tax or trademark questions, or a business choice such as
  whether the shop should sell this at all. Say what he must decide in owner_question.
{problems}
Answer with JSON matching the provided schema; "pictures" must have one entry for every picture listed above."""


@dataclass
class DeskResult:
    decision: str
    summary: str


def latest_listing(session: Session, product: Product) -> Listing | None:
    return session.scalar(select(Listing).where(Listing.product_id == product.id).order_by(Listing.id.desc()).limit(1))


def desk_review_for(session: Session, listing: Listing) -> Review | None:
    """The desk's verdict on this exact listing, if it gave one."""
    for review in session.scalars(select(Review).where(Review.product_id == listing.product_id,
                                                       Review.department == Department.OWNER_DESK)
                                  .order_by(Review.id.desc())):
        if json.loads(review.report_json or "{}").get("listing_id") == listing.id:
            return review
    return None


def desk_pending(session: Session, settings: Settings, product: Product) -> bool:
    """True while the desk still owes this product a look (it then isn't waiting on the owner yet)."""
    if not settings.desk_enabled or product.status != S.HUMAN_REVIEW or product.previous_status != S.SEO:
        return False
    listing = latest_listing(session, product)
    return listing is not None and desk_review_for(session, listing) is None


def needs_owner(session: Session, settings: Settings, product: Product) -> bool:
    """A product in HUMAN_REVIEW that only the owner can move on."""
    return product.status == S.HUMAN_REVIEW and not desk_pending(session, settings, product)


def pending_products(session: Session, settings: Settings) -> list[Product]:
    products = session.scalars(select(Product).where(Product.status == S.HUMAN_REVIEW).order_by(Product.id)).all()
    return [p for p in products if desk_pending(session, settings, p)]


class OwnerDesk:
    def __init__(self, settings: Settings, provider_factory=None):
        self.settings = settings
        if provider_factory is None:
            from providers.claude_headless import ClaudeHeadless
            provider_factory = lambda session: ClaudeHeadless(settings, session)  # noqa: E731
        self.provider_factory = provider_factory

    def review(self, session: Session, product: Product) -> DeskResult:
        """Give one product in HUMAN_REVIEW the desk's look and act on it. Hard stops (run cap, billing
        guard) propagate; the product then simply waits for the next shift."""
        from agents.operations import HARD_STOPS

        listing = latest_listing(session, product)
        if not desk_pending(session, self.settings, product) or listing is None:
            raise ValueError(f"Product {product.id} is not waiting for the owner's desk")
        build = session.get(Build, listing.build_id)
        job = Job(product_id=product.id, department=Department.OWNER_DESK, job_type="owner_desk_review",
                  for_status=S.HUMAN_REVIEW, max_attempts=TRIES, status=JobStatus.RUNNING, started_at=utcnow(),
                  input_json=json.dumps({"product_id": product.id, "listing_id": listing.id}))
        session.add(job)
        log_event(session, "job.started", actor=ACTOR, product=product, job=job,
                  message=f"Owner's desk review of listing #{listing.id}")
        session.commit()  # the office shows the desk working while the run goes on
        try:
            problems = self.precheck(session, product, listing, build)
            if problems:
                verdict = DeskVerdict(decision=SEND_BACK, summary="The listing package failed the desk's "
                                      "deterministic checks: " + "; ".join(problems)[:600], pictures=[],
                                      issues=[DeskIssue(area="listing", where="listing package", problem=p,
                                                        fix="Write the listing package again") for p in problems])
                model = "none"
            else:
                verdict, model = self._ask(session, job, product, listing, build)
        except HARD_STOPS as error:
            job.status, job.finished_at, job.error = JobStatus.CANCELLED, utcnow(), f"stopped: {error}"
            log_event(session, "job.cancelled", actor=ACTOR, job=job, message=f"Desk review stopped: {error}")
            session.commit()
            raise
        result = self._act(session, product, listing, build, job, verdict, model)
        job.status, job.finished_at = JobStatus.SUCCEEDED, utcnow()
        job.output_json = json.dumps({"decision": result.decision, "summary": result.summary})
        log_event(session, "job.finished", actor=ACTOR, job=job, message=result.summary[:500],
                  data={"decision": result.decision})
        return result

    # ------------------------------------------------------------------ deterministic checks

    def precheck(self, session: Session, product: Product, listing: Listing, build: Build) -> list[str]:
        out = Path(listing.path)
        data = json.loads(listing.data_json)
        problems = []
        if build is None or not Path(build.path).is_dir():
            return ["the build folder is missing"]
        zip_path = out / "upload" / Path(build.zip_path).name
        if not zip_path.is_file():
            problems.append("the download is missing from the listing's upload folder")
        elif build.zip_sha256 and sha256(zip_path) != build.zip_sha256:
            problems.append("the download in the listing is not the exact ZIP that passed testing")
        for name in ("listing.json", "LISTING.txt", "UPLOAD_STEPS.txt"):
            if not (out / name).is_file():
                problems.append(f"{name} is missing")
        spec = product_spec(session, product, self.settings.products_dir)
        if spec is not None:
            problems += [f"listing rule: {p}" for p in listing_problems(data, spec)]
        images = [out / "images" / name for name in data.get("image_files", [])]
        problems += image_problems(images)
        problems += [f"{p.name} looks blank" for p in images if p.is_file() and _blank(p)]
        reviewed = session.scalar(select(Event).where(Event.event_type == "listing.pictures_reviewed",
                                                      Event.job_id == listing.job_id)
                                  .order_by(Event.id.desc()).limit(1))
        if listing.job_id is not None and reviewed is None:
            problems.append("SEO's picture review never ran on this listing")
        elif reviewed is not None and json.loads(reviewed.data_json or "{}").get("defects", 0):
            problems.append("SEO's picture review found defects in this listing")
        return problems

    # ------------------------------------------------------------------ the Claude look

    def _ask(self, session: Session, job: Job, product: Product, listing: Listing,
             build: Build) -> tuple[DeskVerdict, str]:
        data = json.loads(listing.data_json)
        images = [Path(listing.path) / "images" / name for name in data["image_files"]]
        names = [p.name for p in images]
        provider = self.provider_factory(session)
        problems, last = "", ""
        for _ in range(TRIES):
            job.attempts += 1
            prompt = PROMPT.format(
                name=product.name, build_dir=_rel(Path(build.path)), title=listing.title,
                price=listing.price_cents / 100, sku=data.get("sku", ""), tags=", ".join(json.loads(listing.tags_json)),
                description=data.get("description", "")[:6000],
                pictures="\n".join(f"- {p.name}: {p}" for p in images), problems=problems)
            try:
                completion = provider.run(job, Department.OWNER_DESK, prompt, headless_policy.OWNER_DESK,
                                          response_model=DeskVerdict)
            except ProviderError as error:
                last = str(error)
                problems = f"\nYour previous answer failed: {last}\n"
                continue
            verdict: DeskVerdict = completion.data
            checked = [c.image for c in verdict.pictures]
            if missing := [n for n in names if n not in checked]:
                last = f"the review skipped {', '.join(missing)}"
                problems = f"\nYour previous answer skipped these pictures: {', '.join(missing)}. Check every one.\n"
                continue
            return verdict, completion.model
        log_event(session, "owner_desk.incomplete", actor=ACTOR, product=product, job=job, message=last[:500])
        return DeskVerdict(decision=NEEDS_OWNER, summary=f"The desk could not finish its review ({last[:300]}); "
                           "please look at this one yourself.", pictures=[],
                           owner_question="Review this product yourself."), "none"

    # ------------------------------------------------------------------ acting on the verdict

    def _act(self, session: Session, product: Product, listing: Listing, build: Build, job: Job,
             verdict: DeskVerdict, model: str) -> DeskResult:
        flagged = [(c.image, label, c.notes) for c in verdict.pictures
                   for field, label in CHECK_FIELDS.items() if getattr(c, field)]
        decision = verdict.decision
        issues = list(verdict.issues)
        if flagged or issues:
            decision = SEND_BACK if decision != NEEDS_OWNER or flagged else NEEDS_OWNER
        if decision == APPROVE and not verdict.pictures:
            decision = NEEDS_OWNER  # never approve without having looked
        # A ticked box with no matching issue still needs a fix in the brief.
        described = {i.where for i in issues}
        for image, label, notes in flagged:
            if not any(image in w for w in described):
                issues.append(DeskIssue(area="product", where=image, problem=f"{label}: {notes or 'see picture'}",
                                        fix=f"Fix what makes {image} show {label}, then rebuild the pictures"))
        review = Review(product_id=product.id, job_id=job.id, build_id=build.id if build else None,
                        department=Department.OWNER_DESK, reviewer="claude-headless" if model != "none" else ACTOR,
                        model=model, decision=decision, summary=verdict.summary,
                        findings_json=json.dumps([i.model_dump() for i in issues]),
                        report_json=json.dumps({"listing_id": listing.id, "model_decision": verdict.decision,
                                                "pictures": [c.model_dump() for c in verdict.pictures],
                                                "owner_question": verdict.owner_question}))
        session.add(review)
        session.flush()
        log_event(session, "owner_desk.reviewed", actor=ACTOR, product=product, job=job,
                  message=f"{decision}: {verdict.summary}"[:900],
                  data={"decision": decision, "pictures": len(verdict.pictures), "issues": len(issues),
                        "listing_id": listing.id})

        if decision == APPROVE:
            if not self.settings.desk_auto_approve:
                product.status_reason = f"Recommended by the owner's desk: {verdict.summary}"[:1000]
                return DeskResult(APPROVE, f"Recommended (auto-approve is off): {verdict.summary}")
            record_approval(session, product, approver=APPROVER, note=verdict.summary)
            transition(session, self.settings, product, S.READY_TO_PUBLISH, actor=ACTOR, job=job,
                       reason=f"Approved by the owner's desk (every picture checked): {verdict.summary}"[:1000])
            return DeskResult(APPROVE, verdict.summary)

        if decision == NEEDS_OWNER:
            question = verdict.owner_question or verdict.summary
            product.status_reason = f"The owner's desk needs you: {question}"[:1000]
            return DeskResult(NEEDS_OWNER, question)

        if any(i.area == "product" for i in issues):
            brief = create_brief(
                session, product, source="OWNER_DESK", actor=ACTOR, review=review, job=job, build=build,
                summary=f"The owner's desk found {len(issues)} problem(s): {verdict.summary}"[:1000],
                required_changes=[{"id": f"desk_fix_v{build.version}_{n}", "change": i.fix,
                                   "reason": f"{i.where}: {i.problem}",
                                   "acceptance": "The owner's desk finds nothing on the rebuilt product and pictures"}
                                  for n, i in enumerate(issues, start=1)],
                findings=[i.model_dump() for i in issues])
            transition(session, self.settings, product, S.REWORK, actor=ACTOR, job=job,
                       reason=f"Owner's desk sent it back (brief #{brief.id}): "
                              + "; ".join(f"{i.where}: {i.problem}" for i in issues)[:600])
            return DeskResult(SEND_BACK, f"Rework brief #{brief.id}: {verdict.summary}")

        redos = session.scalar(select(func.count(Event.id)).where(Event.product_id == product.id,
                                                                   Event.event_type == "owner_desk.relisted",
                                                                   Event.data_json.contains(f'"build_id": {build.id},')))
        if redos >= MAX_LISTING_REDOS:
            product.status_reason = (f"The owner's desk sent this listing back {redos} times; please decide: "
                                     + "; ".join(i.problem for i in issues))[:1000]
            return DeskResult(NEEDS_OWNER, product.status_reason)
        log_event(session, "owner_desk.relisted", actor=ACTOR, product=product, job=job,
                  message="; ".join(f"{i.where}: {i.problem} -> {i.fix}" for i in issues)[:900],
                  data={"build_id": build.id, "issues": [i.model_dump() for i in issues]})
        transition(session, self.settings, product, S.SEO, actor=ACTOR, job=job,
                   reason="Owner's desk asked for a new listing: " + "; ".join(i.problem for i in issues)[:600])
        return DeskResult(SEND_BACK, f"Listing redo: {verdict.summary}")


def desk_feedback(session: Session, product: Product) -> str:
    """What the desk asked SEO to change last time, for the next listing draft (empty if nothing)."""
    event = session.scalar(select(Event).where(Event.product_id == product.id,
                                               Event.event_type == "owner_desk.relisted")
                           .order_by(Event.id.desc()).limit(1))
    return event.message if event is not None else ""


def _blank(path: Path) -> bool:
    with Image.open(path) as image:
        return max(ImageStat.Stat(image.convert("L")).stddev) < BLANK_STDDEV


def _rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()
