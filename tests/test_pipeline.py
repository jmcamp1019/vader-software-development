import sqlite3
import unittest
from pathlib import Path

import _path  # noqa: F401

from pelositracker import db, fetcher
from pelositracker.pipeline import ingest_records, ingest_senate_filings

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn: sqlite3.Connection = db.connect(":memory:")
        db.init_schema(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def _ingest_all(self) -> tuple[int, int, int]:
        house = fetcher.load_fixture(FIXTURES / "house_sample.json")
        senate = fetcher.load_fixture(FIXTURES / "senate_sample.json")
        s1 = ingest_records(self.conn, house, "house")
        s2 = ingest_records(self.conn, senate, "senate")
        return (
            s1.inserted + s2.inserted,
            s1.duplicates + s2.duplicates,
            s1.skipped + s2.skipped,
        )

    def test_first_ingest_inserts_and_skips_malformed(self) -> None:
        inserted, duplicates, skipped = self._ingest_all()
        self.assertEqual(inserted, 6)  # 7 fixture rows, 1 malformed
        self.assertEqual(duplicates, 0)
        self.assertEqual(skipped, 1)  # the "not-a-date" house row
        self.assertEqual(db.trade_count(self.conn), 6)
        self.assertEqual(db.politician_count(self.conn), 4)

    def test_reingest_is_idempotent(self) -> None:
        self._ingest_all()
        count_before = db.trade_count(self.conn)
        inserted, duplicates, _ = self._ingest_all()
        self.assertEqual(inserted, 0)
        self.assertEqual(duplicates, 6)
        self.assertEqual(db.trade_count(self.conn), count_before)

    def test_amount_ranges_survive_storage(self) -> None:
        self._ingest_all()
        row = self.conn.execute(
            "SELECT amount_min_cents, amount_max_cents FROM trades "
            "WHERE amount_max_cents IS NULL"
        ).fetchone()
        self.assertIsNotNone(row)  # open-ended "$50,000,000 +" kept its NULL max
        self.assertEqual(row["amount_min_cents"], 5_000_000_000)

    def test_politician_dedupe(self) -> None:
        pid1 = db.get_or_create_politician(self.conn, "Testa Fixture", "house")
        pid2 = db.get_or_create_politician(self.conn, "Testa Fixture", "house")
        self.assertEqual(pid1, pid2)

    def test_unknown_chamber_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ingest_records(self.conn, [], "parliament")

    def test_senate_filings_ingest_flattens_and_skips_malformed(self) -> None:
        filings = fetcher.load_fixture(FIXTURES / "senate_filings_sample.json")
        stats = ingest_senate_filings(self.conn, filings)
        # 3 filings / 5 total transactions: filing 1 has 2 valid rows, filing 2
        # is missing ptr_link/date_recieved (its 1 row is skipped), filing 3 has
        # 1 valid row and 1 unknown transaction type (skipped).
        self.assertEqual(stats.total_records, 5)
        self.assertEqual(stats.inserted, 3)
        self.assertEqual(stats.skipped, 2)
        self.assertEqual(stats.duplicates, 0)
        self.assertEqual(db.trade_count(self.conn), 3)

    def test_senate_filings_reingest_is_idempotent(self) -> None:
        filings = fetcher.load_fixture(FIXTURES / "senate_filings_sample.json")
        ingest_senate_filings(self.conn, filings)
        count_before = db.trade_count(self.conn)
        stats = ingest_senate_filings(self.conn, filings)
        self.assertEqual(stats.inserted, 0)
        self.assertEqual(stats.duplicates, 3)
        self.assertEqual(db.trade_count(self.conn), count_before)


if __name__ == "__main__":
    unittest.main()
