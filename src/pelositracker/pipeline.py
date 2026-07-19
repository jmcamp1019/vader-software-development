"""Ingestion pipeline: raw feed records -> normalized trades -> SQLite."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

from . import clerk, config, db
from .normalizer import (
    NormalizedTrade,
    extract_filing_doc_id,
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
    quarantined: int = 0
    legacy_unindexed: int = 0

    def summary(self) -> str:
        return (
            f"[{self.chamber}] records={self.total_records} inserted={self.inserted} "
            f"duplicates={self.duplicates} skipped={self.skipped} "
            f"quarantined={self.quarantined} "
            f"legacy_unindexed={self.legacy_unindexed}"
        )


def ingest_records(
    conn: sqlite3.Connection,
    records: list[dict[str, Any]],
    chamber: str,
    provenance: str | None = None,
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
    stats.inserted, stats.duplicates = db.upsert_trades(conn, normalized, provenance)
    return stats


def ingest_house_records(
    conn: sqlite3.Connection,
    records: list[dict[str, Any]],
    clerk_doc_ids: set[str],
) -> IngestStats:
    """Ingest house mirror records anchored to the official Clerk index (ADR-001).

    A record whose filing DocID is missing or absent from the official index is
    quarantined: counted, reported, never inserted. Pre-2015 rows predate usable
    PTR coverage in the official bulk index and remain quarantined under the
    narrower legacy-unindexed count. Records that pass the anchor but fail
    normalization (artifacts, bad dates/amounts) are skipped.
    """
    stats = IngestStats(chamber="house", total_records=len(records))

    normalized: list[NormalizedTrade] = []
    for record in records:
        year = clerk.filing_year(record)
        if year is None:
            # A DocID from another fetched year must never anchor a record
            # whose own filing year cannot be established.
            stats.quarantined += 1
            continue
        if year < clerk.CLERK_PTR_INDEX_START_YEAR:
            stats.quarantined += 1
            stats.legacy_unindexed += 1
            continue
        doc_id = extract_filing_doc_id(record)
        if doc_id is None or doc_id not in clerk_doc_ids:
            stats.quarantined += 1
            continue
        try:
            normalized.append(normalize_house_record(record))
        except ValueError:
            stats.skipped += 1

    db.init_schema(conn)
    stats.inserted, stats.duplicates = db.upsert_trades(
        conn, normalized, config.PROVENANCE_HOUSE_MIRROR
    )
    return stats


def ingest_senate_filings(
    conn: sqlite3.Connection,
    filings: list[dict[str, Any]],
    provenance: str | None = None,
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
    stats.inserted, stats.duplicates = db.upsert_trades(conn, normalized, provenance)
    return stats
