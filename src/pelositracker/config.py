"""Configuration: data source endpoints and defaults."""
from __future__ import annotations

import os

# The original house-stock-watcher S3 bucket returns 403 and its domain no
# longer resolves. Per ADR-001, live house data comes from the community
# successor mirror on GitHub, cross-checked against the official House Clerk
# filing index (the "integrity anchor") on every ingest.
HOUSE_ALL_TRANSACTIONS_URL: str = (
    "https://raw.githubusercontent.com/TattooedHead/house-stock-watcher-data"
    "/main/data/all_transactions.json"
)

# Official House Clerk yearly filing index (TSV+XML inside a ZIP). Trades from
# the mirror are only inserted if their filing DocID appears in this index.
CLERK_HOUSE_INDEX_URL_TEMPLATE: str = (
    "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
)
# disclosures-clerk.house.gov returns 403 to non-browser user agents, so the
# Clerk index fetch (and only that fetch) identifies as a browser (ADR-001).
CLERK_USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# Provenance labels stored on every trade so one source's rows can be purged
# in a single statement if a mirror is ever compromised (ADR-001).
PROVENANCE_HOUSE_MIRROR: str = "house-stock-watcher-mirror"
PROVENANCE_SENATE_GITHUB: str = "senate-stock-watcher-github"
PROVENANCE_FIXTURES: str = "fixtures"
# The senate-stock-watcher S3 bucket now returns 403 Forbidden on direct/anonymous
# access, so live senate data is mirrored via the upstream project's GitHub repo
# instead. The flat "all_transactions.json" mirror lacks disclosure_date entirely,
# so we use the per-filing "all_daily_summaries.json" feed (nested transactions,
# disclosure_date = date_recieved at the filing level) via normalize_senate_filing.
SENATE_DAILY_SUMMARIES_URL: str = (
    "https://raw.githubusercontent.com/timothycarambat/senate-stock-watcher-data"
    "/master/aggregate/all_daily_summaries.json"
)

DEFAULT_DB_PATH: str = os.environ.get("PELOSITRACKER_DB", "pelositracker.db")
HTTP_TIMEOUT_SECONDS: int = int(os.environ.get("PELOSITRACKER_HTTP_TIMEOUT", "90"))
USER_AGENT: str = "PelosiTracker/0.1 (public-disclosure research tool)"
