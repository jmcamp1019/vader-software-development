"""Ingestion pipeline: raw feed records -> normalized trades -> SQLite."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

from . import db
from .normalizer import (
    NormalizedTrade,
    normalize_house_record,
    normalize_senate_filing,
    normalize_senate_record,
)

Normalizer = Callable[[dict[str, Any]], NormalizedTrade]

_NORMALIZERS: dict[str, Normalizer] = {
    "house": normalize_house_record,
    "senate": normalize_senate_record,
}


@dataclass(slots=True)
class IngestStats:
    chamber: str
    total_records: int = 0
    inserted: int = 0
    duplicates: int = 0
    skipped: int = 0

    def summary(self) -> str:
        return (
            f"[{self.chamber}] records={self.total_records} inserted={self.inserted} "
            f"duplicates={self.duplicates} skipped={self.skipped}"
        )


def ingest_records(
    conn: sqlite3.Connection,
    records: list[dict[str, Any]],
    chamber: str,
) -> IngestStats:
    """Normalize and idempotently store a batch of raw feed records.

    Malformed records are skipped and counted, never silently dropped.
    """
    if chamber not in _NORMALIZERS:
        raise ValueError(f"unknown chamber: {chamber!r}")
    normalize = _NORMALIZERS[chamber]
    stats = IngestStats(chamber=chamber, total_records=len(records))

    normalized: list[NormalizedTrade] = []
    for record in records:
        try:
            normalized.append(normalize(record))
        except ValueError:
            stats.skipped += 1

    db.init_schema(conn)
    stats.inserted, stats.duplicates = db.upsert_trades(conn, normalized)
    return stats


def ingest_senate_filings(
    conn: sqlite3.Connection,
    filings: list[dict[str, Any]],
) -> IngestStats:
    """Normalize and idempotently store senate daily-summary filings.

    Each filing may expand into zero or more trades (one per nested
    transaction). A malformed filing skips all of its transactions; a
    malformed individual transaction within a valid filing skips just that one.
    """
    stats = IngestStats(chamber="senate")
    normalized: list[NormalizedTrade] = []
    for filing in filings:
        txn_count = len(filing.get("transactions") or [])
        stats.total_records += txn_count
        try:
            trades = normalize_senate_filing(filing)
        except ValueError:
            stats.skipped += txn_count
            continue
        normalized.extend(trades)
        stats.skipped += txn_count - len(trades)

    db.init_schema(conn)
    stats.inserted, stats.duplicates = db.upsert_trades(conn, normalized)
    return stats
