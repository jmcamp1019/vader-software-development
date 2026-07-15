"""Tests for ADR-001: house mirror ingest anchored to the official Clerk index."""
from __future__ import annotations

import io
import sqlite3
import unittest
import zipfile
from typing import Any

import _path  # noqa: F401

from pelositracker import config, db
from pelositracker.clerk import filing_year, parse_index_doc_ids
from pelositracker.normalizer import (
    extract_filing_doc_id,
    normalize_house_record,
)
from pelositracker.pipeline import ingest_house_records


def _index_zip(rows: list[tuple[str, str, str]], header_cols: str = "Last\tFilingType\tDocID") -> bytes:
    """Build a synthetic Clerk {YEAR}FD.zip payload (BOM-prefixed TSV, like the real one)."""
    lines = [header_cols] + ["\t".join(row) for row in rows]
    text = "﻿" + "\r\n".join(lines) + "\r\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("2026FD.txt", text.encode("utf-8"))
        archive.writestr("2026FD.xml", b"<FinancialDisclosures/>")
    return buffer.getvalue()


def _mirror_record(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "transaction_date": "07/01/2026",
        "disclosure_date": "07/09/2026",
        "ticker": "TST",
        "asset_description": "Test Asset Co. Common Stock - TEST DATA",
        "asset_type": "Stock",
        "type": "purchase",
        "amount": "$1,001 - $15,000",
        "amount_mid": 8000,
        "representative": "Hon. Testa Fixture",
        "district": "ZZ00",
        "owner": "Self",
        "filing_id": "20034977",
        "source_url": "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20034977.pdf",
    }
    record.update(overrides)
    return record


class ClerkIndexParsingTests(unittest.TestCase):
    def test_parses_doc_ids_from_bom_tsv(self) -> None:
        payload = _index_zip(
            [("Fixture", "P", "20034977"), ("Placeholder", "C", "10078673"), ("Blank", "P", "")]
        )
        self.assertEqual(parse_index_doc_ids(payload), {"20034977", "10078673"})

    def test_missing_doc_id_column_rejected(self) -> None:
        payload = _index_zip([("Fixture", "P", "20034977")], header_cols="Last\tFilingType\tWrongCol")
        with self.assertRaises(ValueError):
            parse_index_doc_ids(payload)

    def test_zip_without_txt_index_rejected(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("2026FD.xml", b"<FinancialDisclosures/>")
        with self.assertRaises(ValueError):
            parse_index_doc_ids(buffer.getvalue())

    def test_non_zip_payload_rejected_as_value_error(self) -> None:
        with self.assertRaises(ValueError):
            parse_index_doc_ids(b"<html>error page</html>")

    def test_filing_year_from_pdf_url(self) -> None:
        self.assertEqual(filing_year(_mirror_record()), 2026)

    def test_filing_year_falls_back_to_disclosure_date(self) -> None:
        record = _mirror_record(source_url="https://example.invalid/no-year.pdf")
        self.assertEqual(filing_year(record), 2026)

    def test_filing_year_none_when_underivable(self) -> None:
        record = _mirror_record(source_url="", disclosure_date="")
        self.assertIsNone(filing_year(record))


class HouseNormalizerGuardTests(unittest.TestCase):
    def test_mirror_source_url_field_supported(self) -> None:
        trade = normalize_house_record(_mirror_record())
        self.assertTrue(trade.source_url.endswith("20034977.pdf"))

    def test_amount_mid_ignored_entirely(self) -> None:
        honest = normalize_house_record(_mirror_record(amount_mid=8000))
        poisoned = normalize_house_record(_mirror_record(amount_mid=999_999_999))
        self.assertEqual(honest.amount_min_cents, 100_100)
        self.assertEqual(honest.amount_max_cents, 1_500_000)
        self.assertEqual(honest.ingest_hash, poisoned.ingest_hash)

    def test_control_character_artifact_rejected(self) -> None:
        record = _mirror_record(asset_description="McKesson F\x00\x00 S\x00: New")
        with self.assertRaises(ValueError):
            normalize_house_record(record)

    def test_extract_filing_doc_id_prefers_filing_id(self) -> None:
        self.assertEqual(extract_filing_doc_id(_mirror_record()), "20034977")

    def test_extract_filing_doc_id_falls_back_to_url(self) -> None:
        record = _mirror_record(filing_id=None)
        self.assertEqual(extract_filing_doc_id(record), "20034977")

    def test_extract_filing_doc_id_none_when_unanchorable(self) -> None:
        record = _mirror_record(filing_id=None, source_url="https://example.invalid/x")
        self.assertIsNone(extract_filing_doc_id(record))


class HouseIngestAnchorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn: sqlite3.Connection = db.connect(":memory:")
        db.init_schema(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_anchored_trade_inserted_with_provenance(self) -> None:
        stats = ingest_house_records(self.conn, [_mirror_record()], {"20034977"})
        self.assertEqual(stats.inserted, 1)
        self.assertEqual(stats.quarantined, 0)
        row = self.conn.execute("SELECT provenance FROM trades").fetchone()
        self.assertEqual(row["provenance"], config.PROVENANCE_HOUSE_MIRROR)

    def test_unanchored_trade_quarantined_never_inserted(self) -> None:
        records = [
            _mirror_record(),  # anchored
            _mirror_record(  # filing unknown to the official index
                filing_id="99999999",
                source_url="https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/99999999.pdf",
                ticker="EVIL",
            ),
            _mirror_record(filing_id=None, source_url="https://example.invalid/x"),  # unanchorable
        ]
        stats = ingest_house_records(self.conn, records, {"20034977"})
        self.assertEqual(stats.inserted, 1)
        self.assertEqual(stats.quarantined, 2)
        self.assertEqual(db.trade_count(self.conn), 1)
        evil = self.conn.execute("SELECT 1 FROM trades WHERE ticker = 'EVIL'").fetchone()
        self.assertIsNone(evil)

    def test_artifact_row_skipped_not_quarantined(self) -> None:
        records = [_mirror_record(asset_description="bad\x00row")]
        stats = ingest_house_records(self.conn, records, {"20034977"})
        self.assertEqual(stats.inserted, 0)
        self.assertEqual(stats.skipped, 1)
        self.assertEqual(stats.quarantined, 0)

    def test_reingest_idempotent(self) -> None:
        ingest_house_records(self.conn, [_mirror_record()], {"20034977"})
        stats = ingest_house_records(self.conn, [_mirror_record()], {"20034977"})
        self.assertEqual(stats.inserted, 0)
        self.assertEqual(stats.duplicates, 1)
        self.assertEqual(db.trade_count(self.conn), 1)


class ProvenanceTests(unittest.TestCase):
    def test_purge_provenance_deletes_only_that_source(self) -> None:
        conn = db.connect(":memory:")
        db.init_schema(conn)
        ingest_house_records(conn, [_mirror_record()], {"20034977"})
        conn.execute(
            """
            INSERT INTO trades (politician_id, ticker, asset_name, transaction_type,
                amount_min_cents, amount_max_cents, transaction_date, disclosure_date,
                owner, source_url, ingest_hash, provenance)
            VALUES (?, 'KEEP', 'Kept Asset', 'buy', 100100, 1500000,
                '2026-07-01', '2026-07-09', NULL, 'https://example.invalid/keep', 'hash-keep', ?)
            """,
            (
                db.get_or_create_politician(conn, "Testa Fixture", "senate"),
                config.PROVENANCE_SENATE_GITHUB,
            ),
        )
        deleted = db.purge_provenance(conn, config.PROVENANCE_HOUSE_MIRROR)
        self.assertEqual(deleted, 1)
        self.assertEqual(db.trade_count(conn), 1)
        conn.close()

    def test_migration_adds_and_backfills_provenance(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        # Legacy schema: trades table without the provenance column.
        conn.executescript(
            """
            CREATE TABLE politicians (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL,
                chamber TEXT NOT NULL CHECK (chamber IN ('house', 'senate')),
                UNIQUE (full_name, chamber)
            );
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                politician_id INTEGER NOT NULL REFERENCES politicians (id),
                ticker TEXT,
                asset_name TEXT NOT NULL,
                transaction_type TEXT NOT NULL,
                amount_min_cents INTEGER NOT NULL,
                amount_max_cents INTEGER,
                transaction_date TEXT NOT NULL,
                disclosure_date TEXT NOT NULL,
                owner TEXT,
                source_url TEXT NOT NULL,
                ingest_hash TEXT NOT NULL UNIQUE
            );
            INSERT INTO politicians (full_name, chamber) VALUES ('Old Senator', 'senate');
            INSERT INTO trades (politician_id, ticker, asset_name, transaction_type,
                amount_min_cents, amount_max_cents, transaction_date, disclosure_date,
                owner, source_url, ingest_hash)
            VALUES (1, 'OLD', 'Legacy Asset', 'buy', 100100, 1500000,
                '2026-01-01', '2026-01-05', NULL, 'https://example.invalid/old', 'hash-old');
            """
        )
        db.init_schema(conn)
        row = conn.execute("SELECT provenance FROM trades WHERE ticker = 'OLD'").fetchone()
        self.assertEqual(row["provenance"], "senate-stock-watcher-github")
        conn.close()


if __name__ == "__main__":
    unittest.main()
