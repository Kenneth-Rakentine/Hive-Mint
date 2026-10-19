"""Listing rules: deterministic checks on a marketplace listing before a person sees it. No AI.

The SEO department drafts a listing (agents/seo.py); everything here decides whether it may be
shown to the owner. Limits follow Etsy's published rules for digital downloads (researched
2026-10-18; re-verify before enabling any automated publishing):

- title at most 140 characters; 13 tags of at most 20 characters each;
- the description must say when an item was made with AI (Etsy Creativity Standards);
- at most 5 downloadable files of at most 20 MB each, in an accepted file type. .xlsx is not an
  accepted type, so spreadsheet products are delivered inside the ZIP.

Every claim in the listing must point at a feature id from the product spec, and the listing text
is held to the same Compliance rules as the product files (forbidden claims, brand names,
regulated-topic disclaimers).
"""

from __future__ import annotations

import re
from pathlib import Path

from tools.brands import brand_hits

TITLE_MAX = 140
TAG_COUNT = 13
TAG_MAX = 20
DESCRIPTION_MAX = 10_000
HOOK_CHARS = 160  # what search previews show of the description
MAX_FILES = 5
MAX_FILE_BYTES = 20 * 1024 * 1024
ACCEPTED_TYPES = frozenset({".bmp", ".doc", ".gif", ".jpeg", ".jpg", ".mobi", ".mov", ".mp3", ".mpeg", ".pdf",
                            ".png", ".psp", ".rtf", ".stl", ".txt", ".zip", ".epub", ".ibook"})
PRICE_MIN, PRICE_MAX = 2.0, 60.0
# Etsy tags allow letters, numbers, spaces, hyphens and apostrophes.
TAG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9 '\-]*$")

AI_DISCLOSURE = ("How this was made: I designed this product with the help of AI tools, then checked it with "
                 "automated formula tests and a human review before listing.")


AI_WORDS = re.compile(r"\b(ai|artificial intelligence)\b", re.IGNORECASE)


def ai_disclosure_present(description: str) -> bool:
    return AI_WORDS.search(description) is not None


def listing_text(listing: dict) -> str:
    """Every buyer-visible string in a listing, for Compliance-style checks."""
    parts = [listing.get("title", ""), listing.get("description", ""), " ".join(listing.get("tags", []))]
    parts += [f"{q.get('question', '')} {q.get('answer', '')}" for q in listing.get("faq", [])]
    for image in listing.get("images", []):
        parts += [image.get("headline", ""), *image.get("bullets", [])]
    return "\n".join(parts)


def listing_problems(listing: dict, spec: dict) -> list[str]:
    """Problems that block a listing. Empty means it may go to the owner."""
    problems = []
    title = listing.get("title", "").strip()
    if not title:
        problems.append("title is empty")
    elif len(title) > TITLE_MAX:
        problems.append(f"title is {len(title)} characters; the limit is {TITLE_MAX}")

    tags = [t.strip() for t in listing.get("tags", [])]
    if len(tags) != TAG_COUNT:
        problems.append(f"{len(tags)} tags; exactly {TAG_COUNT} are required")
    if len({t.lower() for t in tags}) != len(tags):
        problems.append("tags repeat")
    for tag in tags:
        if len(tag) > TAG_MAX:
            problems.append(f"tag '{tag}' is {len(tag)} characters; the limit is {TAG_MAX}")
        if not TAG_PATTERN.match(tag.lower()):
            problems.append(f"tag '{tag}' uses characters tags may not contain")

    description = listing.get("description", "")
    if len(description) > DESCRIPTION_MAX:
        problems.append(f"description is {len(description)} characters; the limit is {DESCRIPTION_MAX}")
    if len(description.strip()) < 300:
        problems.append("description is too short to explain the product (under 300 characters)")
    if not ai_disclosure_present(description):
        problems.append("description does not disclose that AI was used (required by the marketplace)")
    for file_spec in spec["files"]:
        if file_spec["name"] not in description:
            problems.append(f"description does not list the included file {file_spec['name']}")

    feature_ids = {f["id"] for f in spec.get("features", [])}
    claims = listing.get("claims", [])
    if not claims:
        problems.append("listing makes no feature claims tied to the spec")
    for claim in claims:
        if claim.get("feature_id") not in feature_ids:
            problems.append(f"claim \"{claim.get('text', '')}\" cites unknown feature '{claim.get('feature_id')}'")

    text = listing_text(listing)
    low = text.lower()
    problems += [f"listing says \"{c}\", which the spec forbids" for c in spec.get("claims_not_to_make", [])
                 if c.lower() in low]
    if hits := brand_hits(title) + brand_hits(" ".join(tags)):
        problems.append(f"title or tags use brand names: {', '.join(sorted(set(hits)))}")
    if hits := brand_hits(text, body_text=True):
        problems.append(f"listing text names brands (only the software a file opens in is fine): "
                        f"{', '.join(hits)}")
    if topic := spec.get("regulated_topic"):
        if f"not {topic} advice" not in " ".join(description.lower().split()):
            problems.append(f"description lacks the disclaimer \"not {topic} advice\"")

    price = listing.get("price")
    if not isinstance(price, (int, float)) or not PRICE_MIN <= price <= PRICE_MAX:
        problems.append(f"price {price!r} is outside ${PRICE_MIN:.0f}-${PRICE_MAX:.0f}")
    return problems


def upload_problems(paths: list[Path]) -> list[str]:
    """The downloadable files a buyer receives must fit the marketplace's file rules."""
    problems = []
    if not paths:
        problems.append("no downloadable files")
    if len(paths) > MAX_FILES:
        problems.append(f"{len(paths)} downloadable files; the limit is {MAX_FILES}")
    for path in paths:
        if path.suffix.lower() not in ACCEPTED_TYPES:
            problems.append(f"{path.name}: {path.suffix} files cannot be uploaded; put it in the ZIP")
        # Etsy: up to 70 characters of letters, numbers, periods, underscores and hyphens, and a file
        # can't be renamed after upload (help.etsy.com, "How to Add Files to a Digital Listing", 2026-10-19).
        if len(path.name) > 70 or not re.fullmatch(r"[A-Za-z0-9._-]+", path.name):
            problems.append(f"{path.name}: file names must be at most 70 letters, numbers, periods, underscores "
                            "or hyphens")
        if not path.is_file():
            problems.append(f"{path.name} is missing")
        elif path.stat().st_size > MAX_FILE_BYTES:
            problems.append(f"{path.name} is {path.stat().st_size / 1_048_576:.1f} MB; the limit is 20 MB")
    return problems
