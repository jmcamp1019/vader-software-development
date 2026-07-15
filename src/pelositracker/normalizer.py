"""Normalize raw House / Senate Stock Watcher records into typed trades.

Domain invariants enforced here:
- Amounts stay as ranges (min/max integer cents; open max = None).
- Every trade carries transaction_date, disclosure_date, and source_url.
- A deterministic ingest_hash makes ingestion idempotent.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from .amounts import parse_amount_range

_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y")

_TYPE_MAP = {
    "purchase": "buy",
    "sale": "sell",
    "sale_full": "sell",
    "sale_partial": "sell",
    "sale (full)": "sell",
    "sale (partial)": "sell",
    "exchange": "exchange",
}


@dataclass(frozen=True, slots=True)
class NormalizedTrade:
    politician_name: str
    chamber: str  # "house" | "senate"
    ticker: str | None
    asset_name: str
    transaction_type: str  # "buy" | "sell" | "exchange"
    amount_min_cents: int
    amount_max_cents: int | None
    transaction_date: str  # ISO YYYY-MM-DD
    disclosure_date: str  # ISO YYYY-MM-DD
    owner: str | None
    source_url: str
    ingest_hash: str


def parse_date(raw: str | None) -> str:
    """Parse feed dates (ISO or MM/DD/YYYY) into ISO format."""
    if raw is None or not raw.strip():
        raise ValueError("empty date")
    cleaned = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            parsed: date = datetime.strptime(cleaned, fmt).date()
            return parsed.isoformat()
        except ValueError:
            continue
    raise ValueError(f"unparseable date: {raw!r}")


def normalize_type(raw: str | None) -> str:
    if raw is None or not raw.strip():
        raise ValueError("empty transaction type")
    mapped = _TYPE_MAP.get(raw.strip().lower())
    if mapped is None:
        raise ValueError(f"unknown transaction type: {raw!r}")
    return mapped


def clean_ticker(raw: str | None) -> str | None:
    """The feeds use '--' (or blank) for unknown tickers."""
    if raw is None:
        return None
    stripped = raw.strip()
    if stripped in ("", "--", "N/A"):
        return None
    return stripped.upper()


def _clean_name(raw: str | None) -> str:
    if raw is None or not raw.strip():
        raise ValueError("missing politician name")
    name = raw.strip()
    if name.lower().startswith("hon. "):
        name = name[5:].strip()
    return name


def compute_ingest_hash(
    politician_name: str,
    chamber: str,
    ticker: str | None,
    transaction_type: str,
    transaction_date: str,
    disclosure_date: str,
    amount_min_cents: int,
    amount_max_cents: int | None,
    asset_name: str,
    owner: str | None,
) -> str:
    key = "|".join(
        [
            chamber,
            politician_name,
            ticker or "",
            transaction_type,
            transaction_date,
            disclosure_date,
            str(amount_min_cents),
            "" if amount_max_cents is None else str(amount_max_cents),
            asset_name,
            owner or "",
        ]
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _build(
    *,
    politician_name: str,
    chamber: str,
    ticker: str | None,
    asset_name: str,
    transaction_type: str,
    amount_raw: str | None,
    transaction_date_raw: str | None,
    disclosure_date_raw: str | None,
    owner: str | None,
    source_url: str | None,
) -> NormalizedTrade:
    amount_min, amount_max = parse_amount_range(amount_raw)
    tx_date = parse_date(transaction_date_raw)
    disc_date = parse_date(disclosure_date_raw)
    url = (source_url or "").strip()
    if not url:
        raise ValueError("missing source_url (ptr_link)")
    digest = compute_ingest_hash(
        politician_name,
        chamber,
        ticker,
        transaction_type,
        tx_date,
        disc_date,
        amount_min,
        amount_max,
        asset_name,
        owner,
    )
    return NormalizedTrade(
        politician_name=politician_name,
        chamber=chamber,
        ticker=ticker,
        asset_name=asset_name,
        transaction_type=transaction_type,
        amount_min_cents=amount_min,
        amount_max_cents=amount_max,
        transaction_date=tx_date,
        disclosure_date=disc_date,
        owner=owner,
        source_url=url,
        ingest_hash=digest,
    )


def normalize_house_record(record: dict[str, Any]) -> NormalizedTrade:
    """Normalize one record from the House Stock Watcher all_transactions feed."""
    return _build(
        politician_name=_clean_name(record.get("representative")),
        chamber="house",
        ticker=clean_ticker(record.get("ticker")),
        asset_name=(record.get("asset_description") or "").strip() or "(undisclosed asset)",
        transaction_type=normalize_type(record.get("type")),
        amount_raw=record.get("amount"),
        transaction_date_raw=record.get("transaction_date"),
        disclosure_date_raw=record.get("disclosure_date"),
        owner=(record.get("owner") or None),
        source_url=record.get("ptr_link"),
    )


def normalize_senate_record(record: dict[str, Any]) -> NormalizedTrade:
    """Normalize one record from the Senate Stock Watcher aggregate feed."""
    return _build(
        politician_name=_clean_name(record.get("senator")),
        chamber="senate",
        ticker=clean_ticker(record.get("ticker")),
        asset_name=(record.get("asset_description") or "").strip() or "(undisclosed asset)",
        transaction_type=normalize_type(record.get("type")),
        amount_raw=record.get("amount"),
        transaction_date_raw=record.get("transaction_date"),
        disclosure_date_raw=record.get("disclosure_date") or record.get("date_recieved"),
        owner=(record.get("owner") or None),
        source_url=record.get("ptr_link"),
    )


def normalize_senate_filing(filing: dict[str, Any]) -> list[NormalizedTrade]:
    """Normalize one filing from the Senate Stock Watcher daily-summaries feed.

    Each filing carries filer-level fields (name, ptr_link, date_recieved) and a
    nested "transactions" array. disclosure_date comes from the filing's
    date_recieved, not from the individual transactions (which don't have one).

    Raises ValueError if the filing itself is malformed (missing name, ptr_link,
    or date_recieved) so the whole filing is skipped. Malformed individual
    transactions within an otherwise-valid filing are skipped, not raised.
    """
    full_name = f"{(filing.get('first_name') or '').strip()} {(filing.get('last_name') or '').strip()}".strip()
    politician_name = _clean_name(full_name or None)
    disclosure_date_raw = filing.get("date_recieved")
    parse_date(disclosure_date_raw)
    source_url = filing.get("ptr_link")
    if not (source_url or "").strip():
        raise ValueError("missing source_url (ptr_link) for filing")

    trades: list[NormalizedTrade] = []
    for txn in filing.get("transactions") or []:
        try:
            trades.append(
                _build(
                    politician_name=politician_name,
                    chamber="senate",
                    ticker=clean_ticker(txn.get("ticker")),
                    asset_name=(txn.get("asset_description") or "").strip()
                    or "(undisclosed asset)",
                    transaction_type=normalize_type(txn.get("type")),
                    amount_raw=txn.get("amount"),
                    transaction_date_raw=txn.get("transaction_date"),
                    disclosure_date_raw=disclosure_date_raw,
                    owner=(txn.get("owner") or None),
                    source_url=source_url,
                )
            )
        except ValueError:
            continue
    return trades
