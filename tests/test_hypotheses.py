from __future__ import annotations

import sqlite3
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import _path  # noqa: F401

from pelositracker import db, hypotheses
from pelositracker.__main__ import main
from pelositracker.hypotheses import HypothesisResult


def _trade(
    pid: int,
    disclosed: str,
    *,
    transacted: str = "2025-01-01",
    ticker: str = "TST",
    kind: str = "buy",
    minimum: int = 100_100,
    chamber: str = "house",
) -> dict[str, object]:
    return {
        "id": pid,
        "politician_id": pid,
        "politician_name": f"Testa {pid} Fixture",
        "chamber": chamber,
        "ticker": ticker,
        "transaction_type": kind,
        "amount_min_cents": minimum,
        "amount_max_cents": 1_500_000,
        "transaction_date": transacted,
        "disclosure_date": disclosed,
    }


class FilterTests(unittest.TestCase):
    def test_purchases_only(self) -> None:
        rows = [_trade(1, "2025-01-02"), _trade(1, "2025-01-03", kind="sell")]
        self.assertEqual(len(hypotheses.purchases_only(rows)), 1)

    def test_fast_filers_boundaries_and_bad_dates(self) -> None:
        rows = [
            _trade(1, "2025-01-01", transacted="2025-01-01"),
            _trade(2, "2025-01-16", transacted="2025-01-01"),
            _trade(3, "2025-01-17", transacted="2025-01-01"),
            _trade(4, "2024-12-31", transacted="2025-01-01"),
            _trade(5, "bad", transacted="2025-01-01"),
        ]
        self.assertEqual(
            [row["politician_id"] for row in hypotheses.fast_filers(rows)], [1, 2]
        )

    def test_conviction_uses_minimum_floor_only(self) -> None:
        low = _trade(1, "2025-01-02", minimum=4_999_999)
        threshold = _trade(2, "2025-01-02", minimum=5_000_000)
        self.assertEqual(hypotheses.conviction_size([low, threshold]), [threshold])

    def test_consensus_enters_at_third_distinct_member_once(self) -> None:
        rows = [
            _trade(1, "2025-01-01"),
            _trade(1, "2025-01-02"),  # repeated member cannot advance threshold
            _trade(2, "2025-01-10"),
            _trade(3, "2025-01-30"),  # Jan 1 fell out; only members 1,2,3 remain
            _trade(4, "2025-01-30"),  # same active episode: no second signal
            _trade(5, "2025-03-10"),  # resets below 3
            _trade(6, "2025-03-11"),
            _trade(7, "2025-03-12"),  # second episode
        ]
        self.assertEqual(
            [row["politician_id"] for row in hypotheses.consensus_signals(rows)],
            [3, 7],
        )

    def test_consensus_context_crosses_phase_boundary_without_duplicate(self) -> None:
        rows = [
            _trade(1, "2025-06-10"),
            _trade(2, "2025-06-20"),
            _trade(3, "2025-07-01"),  # third disclosure: valid holdout signal
            _trade(4, "2025-07-02"),  # same active episode: no duplicate
        ]
        selected = hypotheses.consensus_signals(
            rows, signal_start=hypotheses.HOLDOUT_START, signal_end="2025-07-02"
        )
        self.assertEqual([row["politician_id"] for row in selected], [3])

    def test_consensus_context_suppresses_preexisting_episode(self) -> None:
        rows = [
            _trade(1, "2025-06-10"),
            _trade(2, "2025-06-11"),
            _trade(3, "2025-06-12"),  # episode active before holdout
            _trade(4, "2025-07-01"),
        ]
        self.assertEqual(
            hypotheses.consensus_signals(
                rows,
                signal_start=hypotheses.HOLDOUT_START,
                signal_end="2025-07-01",
            ),
            [],
        )

    def test_chamber_split_is_purchases_only(self) -> None:
        rows = [
            _trade(1, "2025-01-02", chamber="house"),
            _trade(2, "2025-01-02", chamber="senate"),
            _trade(3, "2025-01-02", chamber="house", kind="sell"),
        ]
        self.assertEqual(len(hypotheses.chamber_purchases(rows, "house")), 1)
        self.assertEqual(len(hypotheses.chamber_purchases(rows, "senate")), 1)


class BoundaryAndCohortTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = db.connect(":memory:")
        db.init_schema(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def _insert_trade(self, disclosed: str, ingest_hash: str) -> None:
        pid = db.get_or_create_politician(self.conn, "Testa Fixture", "house")
        self.conn.execute(
            """
            INSERT INTO trades (
                politician_id, ticker, asset_name, transaction_type,
                amount_min_cents, amount_max_cents, transaction_date,
                disclosure_date, source_url, ingest_hash, provenance
            ) VALUES (?, 'TST', 'TEST DATA', 'buy', 100100, 1500000,
                      '2024-01-01', ?, 'https://example.invalid/test', ?, 'fixtures')
            """,
            (pid, disclosed, ingest_hash),
        )
        self.conn.commit()

    def test_train_holdout_boundary_is_disjoint_and_inclusive(self) -> None:
        self._insert_trade("2023-12-31", "a")
        self._insert_trade(hypotheses.TRAIN_START, "b")
        self._insert_trade(hypotheses.TRAIN_END, "c")
        self._insert_trade(hypotheses.HOLDOUT_START, "d")
        train = hypotheses.load_window_trades(
            self.conn, hypotheses.TRAIN_START, hypotheses.TRAIN_END
        )
        holdout = hypotheses.load_window_trades(
            self.conn, hypotheses.HOLDOUT_START, "2025-07-01"
        )
        self.assertEqual([row["disclosure_date"] for row in train], ["2024-01-01", "2025-06-30"])
        self.assertEqual([row["disclosure_date"] for row in holdout], ["2025-07-01"])
        self.assertIn("source_url", train[0])

    def test_h6_selection_receives_train_rows_only(self) -> None:
        rows = [_trade(pid, "2025-01-02") for pid in range(1, 21)]

        def fake_score(
            conn: sqlite3.Connection,
            key: str,
            label: str,
            own: list[dict[str, object]],
            start: str,
            end: str,
        ) -> HypothesisResult:
            self.assertTrue(all(row["disclosure_date"] <= hypotheses.TRAIN_END for row in own))
            pid = int(own[0]["politician_id"])
            return HypothesisResult(key, label, 10, pid / 100, pid / 100, 0, 0, 0, 0, 0, 0, 0)

        with unittest.mock.patch("pelositracker.hypotheses._score", side_effect=fake_score):
            cohort = hypotheses.select_h6_cohort(self.conn, rows, "2025-10-08")
        self.assertEqual(cohort, (20, 19))  # ceil(20 / 10), pessimistic rank


class ArtifactAndPassTests(unittest.TestCase):
    def _result(self, key: str, low: float, trades: int) -> HypothesisResult:
        return HypothesisResult(key, key, trades, low, low + 0.01, 0, 0, 0, 0, 0, 0, 0)

    def test_pass_bar_all_conditions_and_h6_threshold(self) -> None:
        self.assertTrue(hypotheses.passes(self._result("H1", 0.01, 100), self._result("H1", 0.02, 100)))
        self.assertFalse(hypotheses.passes(self._result("H1", 0.01, 99), self._result("H1", 0.02, 100)))
        self.assertFalse(hypotheses.passes(self._result("H1", 0.01, 100), self._result("H1", 0.0199, 100)))
        self.assertTrue(hypotheses.passes(self._result("H6", 0.01, 100), self._result("H6", 0.02, 30)))

    def test_artifact_round_trip(self) -> None:
        artifact = hypotheses.BatteryArtifact(
            "train", hypotheses.TRAIN_START, hypotheses.TRAIN_END, "2025-10-08",
            (self._result("H1", 0.01, 100),), (7,), ("Testa Fixture",)
        )
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "train.json"
            hypotheses.write_artifact(path, artifact)
            self.assertEqual(hypotheses.read_artifact(path), artifact)

    def test_train_cli_refuses_to_overwrite_artifact(self) -> None:
        artifact = hypotheses.BatteryArtifact(
            "train", hypotheses.TRAIN_START, hypotheses.TRAIN_END, "2025-10-08",
            (self._result("H1", 0.01, 100),), (7,), ("Testa Fixture",)
        )
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "test.db"
            output = Path(temp) / "reports"
            with unittest.mock.patch(
                "pelositracker.hypotheses.run_train", return_value=artifact
            ) as run:
                self.assertEqual(
                    main(["--db", str(db_path), "hypothesis-battery", "train", "--output-dir", str(output)]),
                    0,
                )
                with self.assertRaisesRegex(ValueError, "already exists"):
                    main(["--db", str(db_path), "hypothesis-battery", "train", "--output-dir", str(output)])
                self.assertEqual(run.call_count, 1)

    def test_holdout_cli_refuses_to_overwrite_artifact(self) -> None:
        train = hypotheses.BatteryArtifact(
            "train", hypotheses.TRAIN_START, hypotheses.TRAIN_END, "2025-10-08",
            (self._result("H1", 0.01, 100),), (7,), ("Testa Fixture",)
        )
        holdout = hypotheses.BatteryArtifact(
            "holdout", hypotheses.HOLDOUT_START, "2026-01-02", "2026-01-02",
            (self._result("H1", 0.02, 100),), (7,), ("Testa Fixture",)
        )
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "test.db"
            output = Path(temp) / "reports"
            hypotheses.write_artifact(output / hypotheses.TRAIN_ARTIFACT, train)
            with (
                unittest.mock.patch(
                    "pelositracker.hypotheses.latest_price_date",
                    return_value="2026-01-02",
                ),
                unittest.mock.patch(
                    "pelositracker.hypotheses.run_holdout", return_value=holdout
                ) as run,
                unittest.mock.patch(
                    "pelositracker.hypotheses.artifact_is_committed",
                    return_value=True,
                ),
                unittest.mock.patch(
                    "pelositracker.hypotheses.format_full_report",
                    return_value="TEST REPORT",
                ),
            ):
                self.assertEqual(
                    main(["--db", str(db_path), "hypothesis-battery", "holdout", "--output-dir", str(output)]),
                    0,
                )
                with self.assertRaisesRegex(ValueError, "exactly-once guard"):
                    main(["--db", str(db_path), "hypothesis-battery", "holdout", "--output-dir", str(output)])
                self.assertEqual(run.call_count, 1)

    def test_committed_artifact_check_requires_head_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            artifact = root / "reports" / hypotheses.TRAIN_ARTIFACT
            artifact.parent.mkdir()
            artifact.write_text("local", encoding="utf-8")
            head = unittest.mock.Mock(returncode=0, stdout="abc123\n")
            mismatch = unittest.mock.Mock(returncode=0, stdout="def456\n")
            match = unittest.mock.Mock(returncode=0, stdout="abc123\n")
            with unittest.mock.patch(
                "pelositracker.hypotheses.subprocess.run",
                side_effect=[head, mismatch],
            ) as run:
                self.assertFalse(hypotheses.artifact_is_committed(artifact, root))
                self.assertEqual(run.call_count, 2)
            with unittest.mock.patch(
                "pelositracker.hypotheses.subprocess.run",
                side_effect=[head, match],
            ):
                self.assertTrue(hypotheses.artifact_is_committed(artifact, root))

    def test_holdout_reservation_is_fixed_and_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            marker = hypotheses.reserve_holdout(output, "2026-01-02")
            self.assertEqual(marker.name, hypotheses.HOLDOUT_RESERVATION)
            with self.assertRaisesRegex(ValueError, "exactly-once guard"):
                hypotheses.reserve_holdout(output, "2027-01-02")


if __name__ == "__main__":
    unittest.main()
