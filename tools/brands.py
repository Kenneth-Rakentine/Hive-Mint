"""Brand and trademark names a generic product must not use as its own name or imply an
affiliation with. Research rejects opportunity titles that contain one; Compliance sends a
product whose files mention one to HUMAN_REVIEW, because only a person can judge the context.
"""

from __future__ import annotations

import re

BRANDS = (
    # hotels, rentals and travel
    "Marriott", "Hilton", "Hyatt", "IHG", "Holiday Inn", "Wyndham", "Choice Hotels", "Best Western", "Motel 6",
    "Super 8", "Airbnb", "Vrbo", "Booking.com", "Expedia", "Cloudbeds", "Opera PMS",
    # IT service management
    "ServiceNow", "ITIL", "Jira", "Atlassian", "Zendesk", "PagerDuty", "Freshservice", "BMC Remedy",
    # music production
    "Ableton", "FL Studio", "Logic Pro", "Pro Tools", "Splice", "Spotify", "DistroKid", "Native Instruments",
    # marketplaces and office software
    "Etsy", "Gumroad", "Amazon", "Microsoft", "Excel", "Google Sheets", "QuickBooks", "Intuit",
)

_PATTERN = re.compile(r"(?<![A-Za-z0-9])(" + "|".join(re.escape(b) for b in BRANDS) + r")(?![A-Za-z0-9])",
                      re.IGNORECASE)

# Naming the software a file opens in is normal and fine in body text; only product names are strict.
BODY_TEXT_OK = frozenset({"microsoft", "excel", "google sheets", "amazon"})


def brand_hits(text: str, *, body_text: bool = False) -> list[str]:
    """Brand names found in text, as written in BRANDS. body_text allows naming the software it opens in."""
    found = {m.group(1).lower() for m in _PATTERN.finditer(text or "")}
    if body_text:
        found -= BODY_TEXT_OK
    by_lower = {b.lower(): b for b in BRANDS}
    return sorted(by_lower[f] for f in found)
