"""Seed niches for the Research department.

Each shift researches one niche in depth: first the niche scout's best NEW candidate
(agents/scout.py; the scout surveys for new niches about once a week), and only when there is
none, the first seed niche below with no research record from the last RESEARCH_REFRESH_DAYS days. Generic products only: no brand materials, logos, trademarks
or vendor-specific templates.
"""

RESEARCH_REFRESH_DAYS = 7

SEED_NICHES = [
    "small motel / independent hotel / short-term rental operations: front desk, night audit, housekeeping and "
    "maintenance logs (generic, no brand materials)",
    "mobile service businesses (mobile detailing, cleaning, pet grooming, handyman and similar)",
    "IT incident and problem management templates (generic only, no vendor or framework branding)",
    "music producer workflow tools (session planning, release tracking, sample and royalty logs)",
]
