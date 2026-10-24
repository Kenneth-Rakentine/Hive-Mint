"""SEO department: turns a product that passed every gate into a ready-to-upload listing package.

One headless Claude Code run (read-only: it may open the build's files) drafts the listing: title,
13 tags, description, FAQ, price, the feature claims it makes (each tied to a spec feature id) and
the words for each preview image. Everything after that is deterministic:

- tools.listing checks the draft against the marketplace limits and the Compliance rules
  (forbidden claims, brand names, the regulated-topic disclaimer, the AI-use disclosure, every
  claim tied to a real feature, every included file named). An invalid draft is retried once with
  the problems listed; still invalid raises, so Operations retries the job and finally sets ERROR.
- tools.listing_images draws the preview images from the product's own Visual QA renders.
- The package is written to <build>/listing/: listing.json, LISTING.txt (copy-and-paste ready),
  images/, upload/ (the files the buyer downloads) and UPLOAD_STEPS.txt.

A clean package moves the product to HUMAN_REVIEW. Nothing is published: a person reviews the
package (python -m app.cli review <id>), approves it, and uploads it by hand.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from agents.departments import StageResult
from agents.research import opportunity_for
from agents.rework import create_brief
from app.lifecycle import log_event
from app.models import Build, Job, Listing, Product, ResearchRecord
from app.states import Department, Outcome, ProductStatus
from config.settings import ROOT, Settings
from providers import headless_policy
from providers.base import ProviderError
from providers.claude_headless import ClaudeHeadless
from tools.digest import pdf_text
from tools.listing import (AI_DISCLOSURE, AI_WORDS, HOOK_CHARS, TAG_COUNT, TAG_MAX, TITLE_MAX, listing_problems,
                           upload_problems)
from tools.listing_images import image_problems, make_images, plan_slots
from tools.spec_store import product_spec

S = ProductStatus
ACTOR = "seo"
LISTING_DIR = "listing"
DRAFT_TRIES = 2


class Claim(BaseModel):
    text: str = Field(description="A benefit or feature the listing states, in the listing's words")
    feature_id: str = Field(description="The spec feature id that proves it")


class FAQ(BaseModel):
    question: str
    answer: str


class ImageCopy(BaseModel):
    slot: str = Field(description="One of the available image slots, exactly as listed")
    headline: str = Field(max_length=60, description="Short, benefit-led headline; readable as a thumbnail")
    bullets: list[str] = Field(default_factory=list, max_length=3, description="Up to 3 bullets, 40 chars each")


class ListingDraft(BaseModel):
    title: str = Field(description=f"At most {TITLE_MAX} characters; the main search phrase first")
    tags: list[str] = Field(description=f"Exactly {TAG_COUNT} search phrases, at most {TAG_MAX} characters each")
    description: str = Field(description="Plain text. First 160 characters sell it. Then: what it does, who it is "
                                         "for, what is included (every file name), how to use it, compatibility, "
                                         "and the required disclaimer.")
    price: float = Field(description="Suggested price in USD")
    price_reason: str
    claims: list[Claim] = Field(min_length=1)
    faq: list[FAQ] = Field(min_length=3, max_length=6)
    images: list[ImageCopy] = Field(min_length=2)
    category: str = Field(description="Suggested marketplace category path, e.g. 'Paper & Party Supplies > Paper "
                                      "> Calendars & Planners'")


PROMPT = """You are Hive-Mint's SEO department. Write the marketplace listing for a finished digital product.
The product passed testing, QA, an adversarial review and compliance. Your job is to describe it truthfully so the
right buyers find it and know exactly what they get.

PRODUCT: {name}
FILES IN THE DOWNLOAD: {files}
The product files are in {build_dir} (read them if you need detail; do not change anything).

FEATURES (the only things you may claim; cite the id for each claim):
{features}

README:
{readme}

INSTRUCTIONS PDF (text):
{pdf}

MARKET RESEARCH:
{research}

PRICE: {price_rule}

RULES (code checks every one; a draft that breaks one is rejected):
- Title: at most {title_max} characters, main search phrase first, no brand or software names
  (do not write Excel, Google Sheets, Etsy and so on in the title or tags).
- Tags: exactly {tag_count}, each at most {tag_max} characters, lowercase letters, numbers, spaces, hyphens or
  apostrophes only, no repeats. Use the phrases buyers actually search for.
- Description: plain text, under 10,000 characters. The first {hook} characters must make a buyer want it.
  Name every file in the download exactly as written above. Saying which software opens the files is fine in the
  description. {disclaimer}
- Never say any of: {forbidden}. Never promise results, savings, compliance or approval. Never claim a feature
  that is not in the FEATURES list.
- Do not mention AI in the description: a fixed AI-use disclosure is added automatically.
- Images: write copy for each of these slots, in this order: {slots}. "cover" is the thumbnail: a headline of
  at most about 45 characters that says the main benefit, plus up to 3 very short bullets. Other slots describe
  the page shown.
{problems}
Answer with JSON matching the provided schema."""


class PictureDefect(BaseModel):
    image: str = Field(description="File name of the picture, e.g. 03-start-here.jpg")
    where: str = Field(description="Where in the picture: the sheet, cell, label or area")
    problem: str = Field(min_length=5, description="What a buyer would see that looks wrong")
    fix: str = Field(min_length=5, description="What to change in the product or listing so it no longer happens")


class PictureReview(BaseModel):
    checked: list[str] = Field(description="File names of every picture you opened and inspected")
    defects: list[PictureDefect] = Field(default_factory=list)


PICTURE_PROMPT = """You are Hive-Mint's last visual check before the owner sees a listing. Open every listing picture
below with the Read tool and inspect it closely, the way a picky buyer zooming in on Etsy would. These pictures show
the real product's sheets, so a defect in a picture is usually a defect in the product.

Report every one of these you can see (and anything else that looks broken or careless):
- text cut off or clipped: a name, label, header, sentence or number that stops mid-word or runs past its box or
  past the edge of the sheet or picture (e.g. "Jordan & Sam (exampl", "Mileage Deduct", "builds o");
- text spilling outside its cell, box or button, overlapping other text or lines;
- error values or filler: ####, #REF!, #DIV/0!, #N/A, #VALUE!, NaN, "lorem ipsum", "TBD", "XXX";
- a row, label or key with a missing value or swatch next to it, empty columns or sections that should be filled
  in a filled-in example, charts with no bars or lines;
- misaligned or uneven boxes, labels not lined up with their values;
- headline or bullet text on the picture that is cut off, overlaps the sheet, or is too small to read;
- anything on the picture that contradicts the listing title or makes the product look unfinished.

Do not report matters of taste. Report only what you can actually see. If a picture is clean, report nothing for it.

PICTURES:
{pictures}

Answer with JSON matching the provided schema; "checked" must list every picture file name above."""


class ListingRejected(RuntimeError):
    """The listing draft still broke the rules after the allowed tries."""


class SEO:
    """SEO handler: SEO -> HUMAN_REVIEW with a validated listing package."""

    provider = "seo"

    def __init__(self, settings: Settings, provider_factory=None, image_maker=make_images):
        self.settings = settings
        self.provider_factory = provider_factory or (lambda session: ClaudeHeadless(settings, session))
        self.image_maker = image_maker

    def __call__(self, product: Product, job: Job) -> StageResult:
        session = object_session(product)
        spec = product_spec(session, product, self.settings.products_dir)
        build = session.scalar(select(Build).where(Build.product_id == product.id)
                               .order_by(Build.version.desc()).limit(1))
        if spec is None or build is None:
            return StageResult(Outcome.FAIL, S.REWORK, "SEO: no spec or build to list")
        build_dir = Path(build.path)
        zip_path = Path(build.zip_path)

        if upload := upload_problems([zip_path]):
            brief = create_brief(session, product, source="SEO", actor=ACTOR,
                                 summary="The download does not fit the marketplace's file rules",
                                 required_changes=[{"id": f"fit_upload_rules_v{build.version}",
                                                    "change": "; ".join(upload),
                                                    "reason": "Buyers must be able to download the product",
                                                    "acceptance": "One ZIP under 20 MB holds every file"}],
                                 findings=[{"upload": p} for p in upload], job=job, build=build)
            return StageResult(Outcome.FAIL, S.REWORK, "SEO: " + "; ".join(upload), {"rework_brief_id": brief.id})

        slots = [s.id for s in plan_slots(spec, build_dir)]
        price_range = self._price_range(session, product)
        draft = self._draft(session, job, product, spec, build_dir, slots, price_range)
        listing = self._finish(draft, spec, price_range)
        listing["sku"] = sku(product, build.version)

        out = build_dir / LISTING_DIR
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True)
        copy = {image["slot"]: image for image in listing["images"]}
        images = self.image_maker(spec, build_dir, copy, out / "images")
        if bad := image_problems(images):
            raise ListingRejected("Listing images failed checks: " + "; ".join(bad))
        review = self._picture_review(session, job, product, images)
        if review.defects:
            brief = create_brief(
                session, product, source="SEO", actor=ACTOR,
                summary=f"The listing pictures show {len(review.defects)} visible defect(s)",
                required_changes=[{"id": f"picture_fix_v{build.version}_{i}", "change": d.fix,
                                   "reason": f"{d.image} ({d.where}): {d.problem}",
                                   "acceptance": "The listing picture review finds nothing on the rebuilt pictures"}
                                  for i, d in enumerate(review.defects, start=1)],
                findings=[d.model_dump() for d in review.defects], job=job, build=build)
            log_event(session, "listing.pictures_rejected", actor=ACTOR, product=product, job=job,
                      message="; ".join(f"{d.image}: {d.problem}" for d in review.defects)[:900])
            return StageResult(Outcome.FAIL, S.REWORK,
                               f"Picture review found {len(review.defects)} defect(s) in v{build.version}: "
                               + "; ".join(f"{d.image}: {d.problem}" for d in review.defects)[:400],
                               {"rework_brief_id": brief.id})
        upload_dir = out / "upload"
        upload_dir.mkdir()
        shutil.copy2(zip_path, upload_dir / zip_path.name)
        listing["image_files"] = [p.name for p in images]
        listing["upload_files"] = [zip_path.name]
        (out / "listing.json").write_text(json.dumps(listing, indent=2), encoding="utf-8")
        (out / "LISTING.txt").write_text(listing_txt(listing), encoding="utf-8")
        (out / "UPLOAD_STEPS.txt").write_text(upload_steps(listing, out), encoding="utf-8")

        row = Listing(product_id=product.id, build_id=build.id, job_id=job.id, path=str(out),
                      title=listing["title"], price_cents=int(round(listing["price"] * 100)),
                      tags_json=json.dumps(listing["tags"]), data_json=json.dumps(listing))
        session.add(row)
        log_event(session, "listing.created", actor=ACTOR, product=product, job=job,
                  message=f"Listing for v{build.version}: {len(images)} images, ${listing['price']:.2f}",
                  data={"listing_path": str(out), "images": len(images), "price": listing["price"]})
        return StageResult(Outcome.PASS, S.HUMAN_REVIEW,
                           f"Listing package ready for v{build.version} (${listing['price']:.2f}, {len(images)} "
                           f"images): {out}", {"listing_path": str(out)})

    # ------------------------------------------------------------------ helpers

    def _picture_review(self, session: Session, job: Job, product: Product, images: list[Path]) -> PictureReview:
        """One headless run that looks at every listing picture (Claude reads images) for visible defects."""
        provider = self.provider_factory(session)
        names = [p.name for p in images]
        pictures = "\n".join(f"- {p.name}: {p}" for p in images)
        last = ""
        for _ in range(DRAFT_TRIES):
            try:
                completion = provider.run(job, Department.SEO, PICTURE_PROMPT.format(pictures=pictures),
                                          headless_policy.SEO, response_model=PictureReview)
            except ProviderError as error:
                last = str(error)
                continue
            review: PictureReview = completion.data
            missing = [n for n in names if n not in review.checked]
            if not missing:
                log_event(session, "listing.pictures_reviewed", actor=ACTOR, product=product, job=job,
                          message=f"{len(names)} picture(s) inspected, {len(review.defects)} defect(s)")
                return review
            last = f"the review skipped {', '.join(missing)}"
        raise ListingRejected(f"Picture review incomplete: {last}")

    def _price_range(self, session: Session, product: Product) -> tuple[float, float] | None:
        opp = opportunity_for(session, product)
        if opp is None:
            return None
        data = json.loads(opp.data_json)
        low, high = data.get("price_low"), data.get("price_high")
        return (float(low), float(high)) if low is not None and high is not None and high >= low > 0 else None

    def _draft(self, session: Session, job: Job, product: Product, spec: dict, build_dir: Path,
               slots: list[str], price_range) -> ListingDraft:
        provider = self.provider_factory(session)
        problems: list[str] = []
        last = ""
        for _ in range(DRAFT_TRIES):
            note = ("\nYour previous draft was rejected for these reasons; fix every one:\n- "
                    + "\n- ".join(problems) + "\n") if problems else ""
            prompt = self._prompt(session, product, spec, build_dir, slots, price_range, note)
            try:
                completion = provider.run(job, Department.SEO, prompt, headless_policy.SEO,
                                          response_model=ListingDraft)
            except ProviderError as error:
                last, problems = str(error), []
                continue
            draft: ListingDraft = completion.data
            problems = draft_problems(draft, spec, slots, price_range)
            if not problems:
                return draft
            last = "; ".join(problems)
            log_event(session, "listing.rejected", actor=ACTOR, product=product, job=job, message=last)
        raise ListingRejected(f"Listing draft rejected after {DRAFT_TRIES} tries: {last}")

    def _finish(self, draft: ListingDraft, spec: dict, price_range) -> dict:
        listing = draft.model_dump()
        listing["tags"] = [t.strip().lower() for t in listing["tags"]]
        listing["description"] = draft.description.rstrip() + "\n\n" + AI_DISCLOSURE
        listing["price_range"] = list(price_range) if price_range else None
        listing["product"] = spec.get("name", "")
        listing["spec_version"] = spec.get("spec_version")
        return listing

    def _prompt(self, session, product, spec, build_dir, slots, price_range, note) -> str:
        readme = next((f for f in spec["files"] if f["kind"] == "text"), None)
        pdf = next((f for f in spec["files"] if f["kind"] == "pdf"), None)
        readme_text = (build_dir / readme["name"]).read_text(encoding="utf-8", errors="replace") \
            if readme and (build_dir / readme["name"]).is_file() else ""
        pdf_body = pdf_text(build_dir / pdf["name"]) if pdf and (build_dir / pdf["name"]).is_file() else ""
        research = "Owner-sourced product: no research record."
        if (opp := opportunity_for(session, product)) is not None:
            record = session.get(ResearchRecord, opp.research_id)
            findings = json.loads(record.findings_json)
            research = json.dumps({"summary": record.summary, "complaints": findings.get("complaints"),
                                   "gaps": findings.get("gaps"), "selling": findings.get("selling"),
                                   "differentiation": json.loads(opp.data_json).get("differentiation")}, indent=1)
        if price_range:
            price_rule = (f"Research saw buyers pay ${price_range[0]:.2f}-${price_range[1]:.2f}. Pick a price inside that "
                          "range and explain why.")
        else:
            price_rule = ("No researched price range. Suggest a fair price between $5 and $25 for what this "
                          "product offers and explain why; the owner will confirm it.")
        topic = spec.get("regulated_topic")
        disclaimer = (f'Include the exact words "not {topic} advice" in a short disclaimer.' if topic else "")
        features = "\n".join(f"- {f['id']}: {f['description']}" for f in spec.get("features", []))
        try:
            build_rel = build_dir.relative_to(ROOT)
        except ValueError:
            build_rel = build_dir
        return PROMPT.format(name=spec.get("name", product.name), files=", ".join(f["name"] for f in spec["files"]),
                             build_dir=build_rel, features=features, readme=readme_text[:4000],
                             pdf=pdf_body[:6000], research=research, price_rule=price_rule, title_max=TITLE_MAX,
                             tag_count=TAG_COUNT, tag_max=TAG_MAX, hook=HOOK_CHARS, disclaimer=disclaimer,
                             forbidden=", ".join(f'"{c}"' for c in spec.get("claims_not_to_make", [])) or "(none)",
                             slots=", ".join(slots), problems=note)


def draft_problems(draft: ListingDraft, spec: dict, slots: list[str], price_range) -> list[str]:
    """Listing rules plus checks that only apply to a draft (image slots, researched price)."""
    listing = draft.model_dump()
    listing["tags"] = [t.strip().lower() for t in listing["tags"]]
    listing["description"] = draft.description.rstrip() + "\n\n" + AI_DISCLOSURE
    problems = listing_problems(listing, spec)
    if AI_WORDS.search(draft.description):
        problems.append("the description mentions AI; leave that to the automatic disclosure")
    given = [image.slot for image in draft.images]
    if missing := [s for s in slots if s not in given]:
        problems.append(f"no image copy for slots: {', '.join(missing)}")
    if unknown := [s for s in given if s not in slots]:
        problems.append(f"image copy for unknown slots: {', '.join(unknown)}")
    for image in draft.images:
        if len(image.headline) > 60:
            problems.append(f"image '{image.slot}' headline is over 60 characters")
        problems += [f"image '{image.slot}' bullet over 40 characters: {b}" for b in image.bullets if len(b) > 40]
    if price_range and not price_range[0] <= draft.price <= price_range[1]:
        problems.append(f"price ${draft.price:.2f} is outside the researched ${price_range[0]:.2f}-"
                        f"${price_range[1]:.2f}")
    return problems


def sku(product: Product, version: int) -> str:
    """Stock-keeping code for the listing form, e.g. HM-005-V3: shop prefix, product number, build version.
    Etsy shows it only to the seller; it ties a listing (and its sales) back to the product here."""
    return f"HM-{product.id:03d}-V{version}"


def listing_txt(listing: dict) -> str:
    faq = "\n\n".join(f"Q: {q['question']}\nA: {q['answer']}" for q in listing["faq"])
    return "\n".join([
        "LISTING — copy each part into the marketplace form", "=" * 60, "",
        f"TITLE ({len(listing['title'])}/{TITLE_MAX} characters):", listing["title"], "",
        f"PRICE: ${listing['price']:.2f}   ({listing['price_reason']})", "",
        "QUANTITY: 999   (digital files never run out)",
        f"SKU: {listing.get('sku', '')}   (only you see it; it links the listing to Hive-Mint's product number)", "",
        f"CATEGORY (suggested): {listing['category']}", "",
        f"TAGS ({len(listing['tags'])}; paste one at a time):", ", ".join(listing["tags"]), "",
        "DESCRIPTION:", "-" * 60, listing["description"], "-" * 60, "",
        "FAQ (for your shop's FAQ section or buyer messages):", "", faq, "",
    ])


def upload_steps(listing: dict, out: Path) -> str:
    images = "\n".join(f"     {name}" for name in listing["image_files"])
    return f"""HOW TO LIST THIS PRODUCT BY HAND (about 10 minutes)
====================================================

Before the first listing: make sure your shop is set up (payments, billing, shop policies) and that you
have checked your employer's outside-business policy.

1. Shop Manager > Listings > Add a listing.
2. Photos: upload these in order (the first one is the thumbnail):
{images}
   They are in: {out / 'images'}
3. Listing details:
   - Title, description and tags: copy them from LISTING.txt in this folder.
   - About this listing: Who made it = I did; What is it = A finished product; When was it made = the
     range that includes this year.
   - Category: {listing['category']} (or the closest match the form offers).
   - Type: Digital.
4. Digital files: upload {', '.join(listing['upload_files'])} from: {out / 'upload'}
5. Price & inventory:
   - Price: ${listing['price']:.2f}
   - Quantity: 999 (digital items do not run out)
   - SKU: {listing.get('sku', '')} (only you see it; it ties this listing to the product in Hive-Mint)
   - Renewal: Automatic
   - Returns & exchanges: none needed (digital downloads use Etsy's fixed "Instant downloads" policy)
6. Check the preview, then Publish.
7. Back in Hive-Mint, nothing else is needed yet: sales tracking arrives in a later version.

The description already includes the AI-use disclosure the marketplace requires. Do not remove it.
"""
