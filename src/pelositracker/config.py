"""Configuration: data source endpoints and defaults."""
from __future__ import annotations

import os

HOUSE_ALL_TRANSACTIONS_URL: str = (
    "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com"
    "/data/all_transactions.json"
)
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
