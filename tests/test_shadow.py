"""WO-9 prospective shadow tracking tests. All people and assets are TEST DATA."""
from __future__ import annotations

import ast
import contextlib
import io
import sqlite3
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

import _path  # noqa: F401

from pelositracker import db, hypotheses, shadow
from pelositracker.__main__ import main

ACTIVATION = datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)


class ShadowTrackingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = db.connect(":memory:")
        db.init_schema(self.conn)
        self.sequence = 0

    def tearDown(self) -> None:
        self.conn.close()

    def _insert_trade(
        self,
        *,
        ticker: str | None = "TST",
        transaction_type: str = "buy",
        transaction_date: str = "2026-07-19",
        disclosure_date: str = "2026-07-19",
        amount_min_cents: int = 100_100,
        amount_max_cents: int | None = 1_500_000,
        provenance: str = "fictional-test-feed",
    ) -> int:
        self.sequence += 1
        politician_id = db.get_or_create_politician(
            self.conn, "Testa Fixture", "house"
        )
        cursor = self.conn.execute(
            """
            INSERT INTO trades (
                politician_id, ticker, asset_name, transaction_type,
                amount_min_cents, amount_max_cents, transaction_date,
                disclosure_date, source_url, ingest_hash, provenance
            ) VALUES (?, ?, 'TEST DATA ASSET', ?, ?, ?, ?, ?,
                      'https://example.invalid/fictional-disclosure', ?, ?)
            """,
            (
                politician_id,
                ticker,
                transaction_type,
                amount_min_cents,
                amount_max_cents,
                transaction_date,
                disclosure_date,
                f"shadow-test-{self.sequence}",
                provenance,
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def test_activation_baselines_existing_trades_permanently(self) -> None:
        existing_id = self._insert_trade()
        snapshot = shadow.start(self.conn, ACTIVATION)
        self.assertEqual(snapshot.activation_trade_id_boundary, existing_id)
        new_id = self._insert_trade()

        result = shadow.scan(self.conn, ACTIVATION + timedelta(hours=1))

        self.assertEqual(result.rows_examined, 1)
        self.assertEqual(result.signals_appended, 1)
        source_ids = [
            int(row["source_trade_id"])
            for row in self.conn.execute("SELECT source_trade_id FROM shadow_signals")
        ]
        self.assertEqual(source_ids, [new_id])

    def test_second_activation_rejected_without_state_change(self) -> None:
        self._insert_trade()
        original = shadow.start(self.conn, ACTIVATION)

        with self.assertRaisesRegex(ValueError, "already started"):
            shadow.start(self.conn, ACTIVATION + timedelta(days=1))

        self.assertEqual(shadow.get_status(self.conn), original)

    def test_h2_signal_snapshots_exact_range_provenance_lag_and_utc(self) -> None:
        shadow.start(self.conn, ACTIVATION)
        source_id = self._insert_trade(
            transaction_date="2026-07-04",
            disclosure_date="2026-07-19",
            amount_min_cents=5_000_000,
            amount_max_cents=None,
            provenance="fictional-test-feed-v2",
        )
        observed = ACTIVATION + timedelta(hours=2)

        result = shadow.scan(self.conn, observed)

        self.assertEqual(result.signals_appended, 1)
        row = self.conn.execute("SELECT * FROM shadow_signals").fetchone()
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(int(row["source_trade_id"]), source_id)
        self.assertEqual(row["observation_utc"], "2026-07-19T14:00:00+00:00")
        self.assertEqual(row["strategy_key"], shadow.STRATEGY_KEY)
        self.assertEqual(int(row["strategy_version"]), shadow.STRATEGY_VERSION)
        self.assertEqual(row["politician_name"], "Testa Fixture")
        self.assertEqual(row["transaction_date"], "2026-07-04")
        self.assertEqual(row["disclosure_date"], "2026-07-19")
        self.assertEqual(int(row["disclosure_lag_days"]), 15)
        self.assertEqual(int(row["amount_min_cents"]), 5_000_000)
        self.assertIsNone(row["amount_max_cents"])
        self.assertEqual(row["source_url"], "https://example.invalid/fictional-disclosure")
        self.assertEqual(row["provenance"], "fictional-test-feed-v2")

    def test_h2_boundaries_reuse_wo8_selector(self) -> None:
        shadow.start(self.conn, ACTIVATION)
        lag_zero = self._insert_trade(transaction_date="2026-07-19")
        lag_fifteen = self._insert_trade(transaction_date="2026-07-04")
        self._insert_trade(transaction_date="2026-07-03")  # lag 16
        self._insert_trade(transaction_date="2026-07-20")  # negative lag
        self._insert_trade(transaction_date="2026-07-19", transaction_type="sell")

        with unittest.mock.patch.object(
            hypotheses, "fast_filers", wraps=hypotheses.fast_filers
        ) as selector:
            result = shadow.scan(self.conn, ACTIVATION + timedelta(hours=1))

        selector.assert_called_once()
        self.assertEqual(result.rows_examined, 5)
        self.assertEqual(result.signals_appended, 2)
        source_ids = {
            int(row["source_trade_id"])
            for row in self.conn.execute("SELECT source_trade_id FROM shadow_signals")
        }
        self.assertEqual(source_ids, {lag_zero, lag_fifteen})

    def test_pre_activation_disclosure_is_audited_backfill_not_signal(self) -> None:
        shadow.start(self.conn, ACTIVATION)
        self._insert_trade(
            transaction_date="2026-07-17", disclosure_date="2026-07-18"
        )

        result = shadow.scan(self.conn, ACTIVATION + timedelta(hours=1))

        self.assertEqual(result.rows_examined, 1)
        self.assertEqual(result.rejected_backfills, 1)
        self.assertEqual(result.signals_appended, 0)
        audit = self.conn.execute("SELECT * FROM shadow_scans").fetchone()
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(int(audit["rejected_backfills"]), 1)
        self.assertEqual(
            int(self.conn.execute("SELECT COUNT(*) FROM shadow_signals").fetchone()[0]),
            0,
        )

    def test_missing_ticker_remains_in_signal_cohort(self) -> None:
        shadow.start(self.conn, ACTIVATION)
        self._insert_trade(ticker=None)

        result = shadow.scan(self.conn, ACTIVATION + timedelta(hours=1))

        self.assertEqual(result.signals_appended, 1)
        row = self.conn.execute("SELECT ticker, asset_name FROM shadow_signals").fetchone()
        self.assertIsNotNone(row)
        assert row is not None
        self.assertIsNone(row["ticker"])
        self.assertEqual(row["asset_name"], "TEST DATA ASSET")

    def test_scans_use_durable_watermark_and_zero_scan_is_audited(self) -> None:
        shadow.start(self.conn, ACTIVATION)
        source_id = self._insert_trade()
        first = shadow.scan(self.conn, ACTIVATION + timedelta(hours=1))
        second = shadow.scan(self.conn, ACTIVATION + timedelta(hours=2))

        self.assertEqual(first.after_trade_id, source_id)
        self.assertEqual(second.before_trade_id, source_id)
        self.assertEqual(second.after_trade_id, source_id)
        self.assertEqual(second.rows_examined, 0)
        self.assertEqual(second.signals_appended, 0)
        snapshot = shadow.get_status(self.conn)
        self.assertEqual(snapshot.scan_count, 2)
        self.assertEqual(snapshot.signal_count, 1)

    def test_signal_scan_and_campaign_rows_reject_mutation(self) -> None:
        shadow.start(self.conn, ACTIVATION)
        self._insert_trade()
        shadow.scan(self.conn, ACTIVATION + timedelta(hours=1))

        statements = (
            "UPDATE shadow_signals SET ticker = 'ZZZ'",
            "DELETE FROM shadow_signals",
            "UPDATE shadow_scans SET rows_examined = 99",
            "DELETE FROM shadow_scans",
            "UPDATE shadow_tracking_state SET strategy_version = 2 WHERE id = 1",
            "DELETE FROM shadow_tracking_state",
        )
        for statement in statements:
            with self.subTest(statement=statement):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.conn.execute(statement)
                self.conn.rollback()

    def test_scheduled_end_records_completion_once_and_blocks_late_signal(self) -> None:
        snapshot = shadow.start(self.conn, ACTIVATION)
        boundary = snapshot.activation_trade_id_boundary
        self._insert_trade()
        scheduled_end = ACTIVATION + timedelta(days=shadow.WINDOW_DAYS)

        completed = shadow.scan(self.conn, scheduled_end)
        repeated = shadow.scan(self.conn, scheduled_end + timedelta(days=1))

        self.assertEqual(completed.status, shadow.STATUS_COMPLETED)
        self.assertEqual(completed.signals_appended, 0)
        self.assertEqual(repeated.status, shadow.STATUS_COMPLETED)
        final = shadow.get_status(self.conn)
        self.assertEqual(final.completed_utc, scheduled_end.isoformat(timespec="seconds"))
        self.assertEqual(final.last_scanned_trade_id, boundary)
        self.assertEqual(final.scan_count, 1)
        self.assertEqual(final.signal_count, 0)

    def test_signal_snapshot_survives_source_provenance_purge(self) -> None:
        shadow.start(self.conn, ACTIVATION)
        source_id = self._insert_trade(provenance="fictional-purge-source")
        first = shadow.scan(self.conn, ACTIVATION + timedelta(hours=1))

        deleted = db.purge_provenance(self.conn, "fictional-purge-source")
        second = shadow.scan(self.conn, ACTIVATION + timedelta(hours=2))

        self.assertEqual(deleted, 1)
        self.assertEqual(first.after_trade_id, source_id)
        self.assertEqual(second.before_trade_id, source_id)
        self.assertEqual(second.after_trade_id, source_id)
        self.assertEqual(shadow.get_status(self.conn).signal_count, 1)

    def test_scan_transaction_rolls_back_signal_audit_and_watermark_together(self) -> None:
        snapshot = shadow.start(self.conn, ACTIVATION)
        self._insert_trade()
        self.conn.execute(
            """
            CREATE TRIGGER fictional_shadow_insert_failure
            BEFORE INSERT ON shadow_signals
            BEGIN
                SELECT RAISE(ABORT, 'fictional insert failure');
            END;
            """
        )
        self.conn.commit()

        with self.assertRaisesRegex(sqlite3.IntegrityError, "fictional insert failure"):
            shadow.scan(self.conn, ACTIVATION + timedelta(hours=1))

        final = shadow.get_status(self.conn)
        self.assertEqual(
            final.last_scanned_trade_id, snapshot.activation_trade_id_boundary
        )
        self.assertEqual(final.scan_count, 0)
        self.assertEqual(final.signal_count, 0)

    def test_scan_before_activation_logs_nothing(self) -> None:
        result = shadow.scan(self.conn, ACTIVATION)
        self.assertEqual(result.status, shadow.STATUS_NOT_STARTED)
        self.assertEqual(shadow.get_status(self.conn).scan_count, 0)


class ShadowCliAndAuditTests(unittest.TestCase):
    def test_shadow_cli_start_scan_status_and_second_start_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db_path = str(Path(temp) / "shadow.db")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(main(["--db", db_path, "shadow", "status"]), 0)
                self.assertEqual(main(["--db", db_path, "shadow", "start"]), 0)
                self.assertEqual(main(["--db", db_path, "shadow", "scan"]), 0)
                self.assertEqual(main(["--db", db_path, "shadow", "start"]), 1)

            output = stdout.getvalue()
            self.assertIn("status=not-started", output)
            self.assertIn("status=active", output)
            self.assertIn("shadow active", output)
            self.assertIn("not investment advice", output)
            self.assertIn("already started", stderr.getvalue())

    def test_shadow_source_has_no_transactional_service_dependencies(self) -> None:
        source_path = Path(shadow.__file__)
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        imports: set[str] = set()
        identifiers: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imports.add(node.module.split(".")[0])
            elif isinstance(node, ast.Name):
                identifiers.add(node.id.lower())
            elif isinstance(node, ast.Attribute):
                identifiers.add(node.attr.lower())

        self.assertLessEqual(
            imports,
            {"__future__", "sqlite3", "dataclasses", "datetime", "typing"},
        )
        self.assertTrue(
            identifiers.isdisjoint(
                {
                    "account",
                    "credentials",
                    "deposit",
                    "orders",
                    "portfolio",
                    "submit_order",
                    "withdrawal",
                }
            )
        )


if __name__ == "__main__":
    unittest.main()
