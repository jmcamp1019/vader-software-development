"""Tests for WO-3 alert digest. All politicians are FICTIONAL — TEST DATA only."""

from __future__ import annotations

import datetime
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import _path  # noqa: F401

from pelositracker import db, watchlists
from pelositracker.api import DISCLAIMER
from pelositracker.digest import (
    format_amount_range,
    get_watermark,
    run_digest,
    set_watermark,
)


class DigestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = db.connect(":memory:")
        db.init_schema(self.conn)
        self.testa_id = db.get_or_create_politician(self.conn, "Testa Fixture", "house")

        # Seed trades directly
        self.conn.execute(
            """
            INSERT INTO trades (
                id, politician_id, ticker, asset_name, transaction_type,
                amount_min_cents, amount_max_cents, transaction_date,
                disclosure_date, owner, source_url, ingest_hash, provenance
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                self.testa_id,
                "TST",
                "Test Asset",
                "buy",
                100100,
                1500000,
                "2026-07-01",
                "2026-07-15",
                "self",
                "http://example.com/tst",
                "hash1",
                "test-source",
            ),
        )
        self.conn.execute(
            """
            INSERT INTO trades (
                id, politician_id, ticker, asset_name, transaction_type,
                amount_min_cents, amount_max_cents, transaction_date,
                disclosure_date, owner, source_url, ingest_hash, provenance
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                2,
                self.testa_id,
                "OPEN",
                "Open Asset",
                "sell",
                5000000000,
                None,
                "2026-07-02",
                "2026-07-15",
                "self",
                "http://example.com/open",
                "hash2",
                "test-source",
            ),
        )
        self.conn.commit()

        self.temp_dir = tempfile.TemporaryDirectory()
        self.today = datetime.date(2026, 7, 15)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        self.conn.close()

    def test_format_amount_range(self) -> None:
        # 1. format_amount_range exact strings for the two examples.
        self.assertEqual(
            format_amount_range(100100, 1500000),
            "$1,001.00 – $15,000.00"
        )
        self.assertEqual(
            format_amount_range(5000000000, None),
            "$50,000,000.00 +"
        )

    def test_watermark_advancement(self) -> None:
        # 2. Watermark: get_watermark == 0 initially; after run_digest it equals
        # MAX(trades.id) even when nothing matched.
        self.assertEqual(get_watermark(self.conn), 0)

        res = run_digest(self.conn, output_dir=self.temp_dir.name, today=self.today)
        self.assertEqual(res.new_trades, 0)
        self.assertEqual(get_watermark(self.conn), 2)

    def test_match_flow_and_disclaimer(self) -> None:
        # 3. Match flow: watched ticker trade appears in digest_text with name, type,
        # the FULL range string, both dates, and source_url; the file
        # {tmp}/2026-07-15.txt exists and contains digest_text; DISCLAIMER is last.
        watchlists.add_ticker(self.conn, "TST")

        res = run_digest(self.conn, output_dir=self.temp_dir.name, today=self.today)
        self.assertEqual(res.new_trades, 1)

        text = res.digest_text
        self.assertIn("Testa Fixture", text)
        self.assertIn("house", text)
        self.assertIn("TST", text)
        self.assertIn("buy", text)
        self.assertIn("$1,001.00 – $15,000.00", text)
        self.assertIn("transacted 2026-07-01", text)
        self.assertIn("disclosed 2026-07-15", text)
        self.assertIn("source: http://example.com/tst", text)

        lines = text.strip().split("\n")
        self.assertEqual(lines[-1], DISCLAIMER)

        expected_file = os.path.join(self.temp_dir.name, "2026-07-15.txt")
        self.assertTrue(os.path.exists(expected_file))
        with open(expected_file, "r", encoding="utf-8") as f:
            file_content = f.read()
        self.assertEqual(file_content, text)
        self.assertEqual(res.output_path, Path(expected_file))

    def test_no_match(self) -> None:
        # 4. No-match: new_trades == 0, output_path is None, no file created in
        # output_dir, digest text still carries the DISCLAIMER.
        res = run_digest(self.conn, output_dir=self.temp_dir.name, today=self.today)
        self.assertEqual(res.new_trades, 0)
        self.assertIsNone(res.output_path)

        self.assertEqual(os.listdir(self.temp_dir.name), [])
        self.assertIn(DISCLAIMER, res.digest_text)
        self.assertIn("No new watched trades.", res.digest_text)

    def test_re_run(self) -> None:
        # 5. Re-run: second run_digest returns new_trades == 0 and writes nothing
        # new (file content unchanged).
        watchlists.add_ticker(self.conn, "TST")

        res1 = run_digest(self.conn, output_dir=self.temp_dir.name, today=self.today)
        self.assertEqual(res1.new_trades, 1)
        expected_file = os.path.join(self.temp_dir.name, "2026-07-15.txt")
        self.assertTrue(os.path.exists(expected_file))

        with open(expected_file, "r", encoding="utf-8") as f:
            content1 = f.read()

        res2 = run_digest(self.conn, output_dir=self.temp_dir.name, today=self.today)
        self.assertEqual(res2.new_trades, 0)
        self.assertIsNone(res2.output_path)

        with open(expected_file, "r", encoding="utf-8") as f:
            content2 = f.read()
        self.assertEqual(content1, content2)

    def test_open_ended_and_no_midpoint_or_average(self) -> None:
        # 6. Open-ended amount renders as "$50,000,000.00 +"; the digest text
        # contains no midpoint of the seeded ranges and no "midpoint"/"average".
        watchlists.add_ticker(self.conn, "OPEN")
        watchlists.add_ticker(self.conn, "TST")

        res = run_digest(self.conn, output_dir=self.temp_dir.name, today=self.today)
        self.assertEqual(res.new_trades, 2)
        text = res.digest_text

        self.assertIn("$50,000,000.00 +", text)
        self.assertNotIn("8,000", text)
        self.assertNotIn("8000", text)
        self.assertNotIn("25,000,000", text)
        self.assertNotIn("25000000", text)
        self.assertNotIn("midpoint", text.lower())
        self.assertNotIn("average", text.lower())

    @patch("pelositracker.digest.smtplib.SMTP")
    def test_email_behavior(self, mock_smtp_class: MagicMock) -> None:
        # 7. Email: no SMTP env vars → emailed False, smtplib untouched; all four
        # set (mocked SMTP) → emailed True, correct host/port/message; partial
        # config → emailed False.
        watchlists.add_ticker(self.conn, "TST")

        # 1. No SMTP env vars
        with patch.dict(os.environ, {}, clear=True):
            res = run_digest(self.conn, output_dir=self.temp_dir.name, today=self.today)
            self.assertEqual(res.new_trades, 1)
            self.assertFalse(res.emailed)
            mock_smtp_class.assert_not_called()

        # Reset watermark
        set_watermark(self.conn, 0)

        # 2. Only SMTP_HOST set
        with patch.dict(os.environ, {"SMTP_HOST": "localhost"}, clear=True):
            res = run_digest(self.conn, output_dir=self.temp_dir.name, today=self.today)
            self.assertEqual(res.new_trades, 1)
            self.assertFalse(res.emailed)
            mock_smtp_class.assert_not_called()

        # Reset watermark
        set_watermark(self.conn, 0)

        # 3. All four set
        env = {
            "SMTP_HOST": "localhost",
            "SMTP_PORT": "2525",
            "SMTP_FROM": "alerts@pelositracker.local",
            "SMTP_TO": "user@domain.local",
        }
        with patch.dict(os.environ, env, clear=True):
            mock_smtp_instance = MagicMock()
            mock_smtp_class.return_value.__enter__.return_value = mock_smtp_instance

            res = run_digest(self.conn, output_dir=self.temp_dir.name, today=self.today)
            self.assertEqual(res.new_trades, 1)
            self.assertTrue(res.emailed)

            mock_smtp_class.assert_called_once_with("localhost", 2525)
            mock_smtp_instance.send_message.assert_called_once()

            sent_msg = mock_smtp_instance.send_message.call_args[0][0]
            self.assertEqual(sent_msg["Subject"], "PelosiTracker digest 2026-07-15")
            self.assertEqual(sent_msg["From"], "alerts@pelositracker.local")
            self.assertEqual(sent_msg["To"], "user@domain.local")

            body_content = sent_msg.get_content().strip()
            self.assertIn("Testa Fixture", body_content)
            self.assertIn("TST", body_content)
            self.assertIn(DISCLAIMER, body_content)

    def test_missing_watchlists_table(self) -> None:
        # 8. Missing watchlists table → run_digest returns new_trades == 0 without
        # creating the watchlists table.
        fresh_conn = db.connect(":memory:")
        db.init_schema(fresh_conn)

        row = fresh_conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'watchlists'"
        ).fetchone()
        self.assertIsNone(row)

        res = run_digest(fresh_conn, output_dir=self.temp_dir.name, today=self.today)
        self.assertEqual(res.new_trades, 0)

        row = fresh_conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'watchlists'"
        ).fetchone()
        self.assertIsNone(row)
        fresh_conn.close()


if __name__ == "__main__":
    unittest.main()
