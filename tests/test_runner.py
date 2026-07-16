"""Tests for WO-4 scheduled runner. No network; sources are injected fakes."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

import _path  # noqa: F401

from pelositracker import db
from pelositracker.digest import DigestResult
from pelositracker.pipeline import IngestStats
from pelositracker.runner import (
    CycleResult,
    acquire_lock,
    format_cycle_line,
    format_tripwire_line,
    quarantine_tripwire,
    refresh_lock,
    release_lock,
    resolve_interval_hours,
    run_cycle,
)


def _stats(chamber: str, **overrides: int) -> IngestStats:
    stats = IngestStats(chamber=chamber, total_records=overrides.pop("total_records", 0))
    for name, value in overrides.items():
        setattr(stats, name, value)
    return stats


def _no_digest(conn: sqlite3.Connection) -> DigestResult:
    return DigestResult(new_trades=0, digest_text="", output_path=None, emailed=False)


class LockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.lock_path = Path(self.tmp.name) / "runner.lock"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_lock_is_exclusive(self) -> None:
        self.assertTrue(acquire_lock(self.lock_path, stale_after_seconds=3600))
        self.assertFalse(acquire_lock(self.lock_path, stale_after_seconds=3600))

    def test_release_allows_reacquire(self) -> None:
        self.assertTrue(acquire_lock(self.lock_path, stale_after_seconds=3600))
        release_lock(self.lock_path)
        self.assertTrue(acquire_lock(self.lock_path, stale_after_seconds=3600))

    def test_stale_lock_is_stolen(self) -> None:
        self.assertTrue(acquire_lock(self.lock_path, stale_after_seconds=3600))
        old = time.time() - 7200
        os.utime(self.lock_path, (old, old))
        self.assertTrue(acquire_lock(self.lock_path, stale_after_seconds=3600))

    def test_fresh_lock_is_not_stolen(self) -> None:
        self.assertTrue(acquire_lock(self.lock_path, stale_after_seconds=3600))
        refresh_lock(self.lock_path)
        self.assertFalse(acquire_lock(self.lock_path, stale_after_seconds=3600))

    def test_lockfile_records_pid(self) -> None:
        self.assertTrue(acquire_lock(self.lock_path, stale_after_seconds=3600))
        content = self.lock_path.read_text(encoding="utf-8")
        self.assertEqual(int(content.splitlines()[0]), os.getpid())


class CycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = db.connect(":memory:")
        db.init_schema(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_failing_source_isolated_from_next_source(self) -> None:
        ran: list[str] = []

        def senate_fails(conn: sqlite3.Connection) -> IngestStats:
            raise OSError("feed unreachable")

        def house_ok(conn: sqlite3.Connection) -> IngestStats:
            ran.append("house")
            return _stats("house", total_records=100, inserted=5, duplicates=95)

        result = run_cycle(
            self.conn,
            sources=[("senate", senate_fails), ("house", house_ok)],
            digest_fn=_no_digest,
        )
        self.assertEqual(ran, ["house"])  # house still ran after senate failed
        self.assertEqual(result.failures, 1)
        self.assertIn("senate FAILED (feed unreachable)", result.source_segments[0])
        self.assertIn("house ok inserted=5", result.source_segments[1])

    def test_house_fail_closed_counts_as_failure(self) -> None:
        def house_clerk_down(conn: sqlite3.Connection) -> IngestStats:
            raise ValueError("Clerk index unavailable")

        result = run_cycle(
            self.conn, sources=[("house", house_clerk_down)], digest_fn=_no_digest
        )
        self.assertEqual(result.failures, 1)
        self.assertEqual(result.house_records, 0)
        self.assertFalse(result.tripwire)

    def test_skipped_source_reported_as_skipped_never_fabricated(self) -> None:
        result = run_cycle(
            self.conn,
            sources=[("senate", lambda conn: None)],
            digest_fn=_no_digest,
        )
        self.assertEqual(result.source_segments, ["senate skipped"])
        self.assertEqual(result.failures, 0)

    def test_cycle_line_structure(self) -> None:
        result = run_cycle(
            self.conn,
            sources=[
                ("senate", lambda conn: _stats("senate", total_records=10, inserted=1, duplicates=9)),
                ("house", lambda conn: _stats("house", total_records=200, inserted=2, duplicates=196, quarantined=2)),
            ],
            digest_fn=_no_digest,
        )
        line = format_cycle_line(result, consecutive_failures=0)
        self.assertIn(result.started, line)
        self.assertIn("senate ok inserted=1 duplicates=9 skipped=0 quarantined=0", line)
        self.assertIn("house ok inserted=2 duplicates=196 skipped=0 quarantined=2", line)
        self.assertIn("digest new=0", line)
        self.assertIn("consecutive_failures=0", line)

    def test_digest_runs_after_sources(self) -> None:
        calls: list[str] = []

        def digest_spy(conn: sqlite3.Connection) -> DigestResult:
            calls.append("digest")
            return DigestResult(new_trades=3, digest_text="", output_path=None, emailed=False)

        result = run_cycle(
            self.conn,
            sources=[("senate", lambda conn: calls.append("senate") or _stats("senate"))],
            digest_fn=digest_spy,
        )
        self.assertEqual(calls, ["senate", "digest"])
        self.assertEqual(result.digest_new, 3)


class TripwireTests(unittest.TestCase):
    def test_threshold_math(self) -> None:
        self.assertTrue(quarantine_tripwire(3, 100))    # 3% > 2%
        self.assertFalse(quarantine_tripwire(2, 100))   # exactly 2% does not trip
        self.assertFalse(quarantine_tripwire(0, 100))
        self.assertFalse(quarantine_tripwire(0, 0))     # empty cycle never trips
        self.assertFalse(quarantine_tripwire(5, 0))     # degenerate input guarded
        self.assertTrue(quarantine_tripwire(21, 1000))  # 2.1% > 2%

    def test_cycle_sets_tripwire_and_warning_line(self) -> None:
        conn = db.connect(":memory:")
        db.init_schema(conn)
        result = run_cycle(
            conn,
            sources=[
                ("house", lambda c: _stats("house", total_records=100, inserted=50, quarantined=50)),
            ],
            digest_fn=_no_digest,
        )
        conn.close()
        self.assertTrue(result.tripwire)
        warning = format_tripwire_line(result)
        self.assertIn("WARNING", warning)
        self.assertIn("50/100", warning)
        self.assertIn("50.0%", warning)
        self.assertIn("mirror", warning)


class IntervalTests(unittest.TestCase):
    def test_cli_value_wins(self) -> None:
        with unittest.mock.patch.dict(os.environ, {"PT_RUN_INTERVAL_HOURS": "12"}):
            self.assertEqual(resolve_interval_hours(3.0), 3.0)

    def test_env_fallback_then_default(self) -> None:
        with unittest.mock.patch.dict(os.environ, {"PT_RUN_INTERVAL_HOURS": "12"}):
            self.assertEqual(resolve_interval_hours(None), 12.0)
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(resolve_interval_hours(None), 6.0)


if __name__ == "__main__":
    unittest.main()
