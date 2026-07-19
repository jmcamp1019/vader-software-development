"""Scheduled ingest runner: keeps the database fresh without manual commands.

Each cycle runs senate ingest -> house ingest -> shadow scan -> digest and emits one
structured log line. A failing source is logged and never kills the runner;
the house source keeps its fail-closed semantics (Clerk index unavailable ->
that source fails for the cycle, nothing inserted). A cycle result is never
fabricated: every source is reported as ok, FAILED, or skipped.

Single-instance guarantee: an O_CREAT|O_EXCL lockfile holding PID + timestamp.
Stale detection is heartbeat-based (the runner rewrites the lockfile at each
cycle, so its mtime is a liveness signal) rather than PID probing —
os.kill(pid, 0) is not a safe liveness check on Windows, where it can
terminate the probed process.
"""
from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import clerk, config, db, fetcher, shadow
from .digest import DigestResult, run_digest
from .pipeline import IngestStats, ingest_house_records, ingest_senate_filings

DEFAULT_INTERVAL_HOURS = 6.0
QUARANTINE_TRIPWIRE_RATIO = 0.02

SourceFn = Callable[[sqlite3.Connection], "IngestStats | None"]
DigestFn = Callable[[sqlite3.Connection], DigestResult]
ShadowFn = Callable[[sqlite3.Connection], shadow.ScanResult]


# --- single-instance lock -------------------------------------------------

def acquire_lock(lock_path: str | Path, stale_after_seconds: float) -> bool:
    """Try to take the runner lock; steal it once if the holder looks dead."""
    path = Path(lock_path)
    for attempt in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if attempt == 0 and _lock_is_stale(path, stale_after_seconds):
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass
                continue
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{os.getpid()}\n{_utc_now_iso()}\n")
        return True
    return False


def refresh_lock(lock_path: str | Path) -> None:
    """Heartbeat: rewrite the lockfile so its mtime proves the runner lives."""
    Path(lock_path).write_text(
        f"{os.getpid()}\n{_utc_now_iso()}\n", encoding="utf-8"
    )


def release_lock(lock_path: str | Path) -> None:
    try:
        os.unlink(lock_path)
    except FileNotFoundError:
        pass


def _lock_is_stale(path: Path, stale_after_seconds: float) -> bool:
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return True  # vanished between checks; the acquire retry settles it
    return (time.time() - mtime) > stale_after_seconds


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- sources ---------------------------------------------------------------

def _senate_source(conn: sqlite3.Connection) -> IngestStats:
    filings = fetcher.fetch_json(config.SENATE_DAILY_SUMMARIES_URL)
    return ingest_senate_filings(conn, filings, config.PROVENANCE_SENATE_GITHUB)


def _house_source(conn: sqlite3.Connection) -> IngestStats:
    records = fetcher.fetch_json(config.HOUSE_ALL_TRANSACTIONS_URL)
    # Fail closed: a Clerk index error propagates and fails this source's
    # cycle — nothing is inserted without the official integrity anchor.
    clerk_doc_ids = clerk.fetch_doc_ids_for_records(records)
    return ingest_house_records(conn, records, clerk_doc_ids)


def default_sources() -> list[tuple[str, SourceFn]]:
    return [("senate", _senate_source), ("house", _house_source)]


# --- cycle -----------------------------------------------------------------

@dataclass
class CycleResult:
    started: str
    source_segments: list[str] = field(default_factory=list)
    failures: int = 0
    house_records: int = 0
    house_quarantined: int = 0
    house_tripwire_records: int = 0
    house_tripwire_quarantined: int = 0
    house_legacy_unindexed: int = 0
    shadow_segment: str = "shadow not-started examined=0 appended=0 rejected_backfills=0"
    digest_new: int = 0
    tripwire: bool = False


def quarantine_tripwire(
    quarantined: int, total: int, threshold: float = QUARANTINE_TRIPWIRE_RATIO
) -> bool:
    """True when the quarantine share EXCEEDS the threshold (poisoned-mirror alarm)."""
    if total <= 0:
        return False
    return quarantined / total > threshold


def run_cycle(
    conn: sqlite3.Connection,
    sources: list[tuple[str, SourceFn]] | None = None,
    digest_fn: DigestFn | None = None,
    shadow_fn: ShadowFn | None = None,
) -> CycleResult:
    """One ingest -> shadow -> digest pass with isolated source/shadow failures."""
    if sources is None:
        sources = default_sources()
    if digest_fn is None:
        digest_fn = run_digest
    if shadow_fn is None:
        shadow_fn = shadow.scan

    result = CycleResult(started=_utc_now_iso())
    db.init_schema(conn)
    for name, source in sources:
        try:
            stats = source(conn)
        except (ValueError, OSError) as exc:  # URLError is an OSError
            result.failures += 1
            result.source_segments.append(f"{name} FAILED ({exc})")
            continue
        if stats is None:
            result.source_segments.append(f"{name} skipped")
            continue
        result.source_segments.append(
            f"{name} ok inserted={stats.inserted} duplicates={stats.duplicates}"
            f" skipped={stats.skipped} quarantined={stats.quarantined}"
            f" legacy_unindexed={stats.legacy_unindexed}"
        )
        if name == "house":
            result.house_records = stats.total_records
            result.house_quarantined = stats.quarantined
            result.house_legacy_unindexed = stats.legacy_unindexed
            result.house_tripwire_records = (
                stats.total_records - stats.legacy_unindexed
            )
            result.house_tripwire_quarantined = (
                stats.quarantined - stats.legacy_unindexed
            )
    result.tripwire = quarantine_tripwire(
        result.house_tripwire_quarantined, result.house_tripwire_records
    )
    try:
        shadow_result = shadow_fn(conn)
    except (sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        result.failures += 1
        result.shadow_segment = f"shadow FAILED ({exc})"
    else:
        result.shadow_segment = shadow.format_scan_segment(shadow_result)
    digest_result = digest_fn(conn)
    result.digest_new = digest_result.new_trades
    return result


def format_cycle_line(result: CycleResult, consecutive_failures: int) -> str:
    segments = " | ".join([*result.source_segments, result.shadow_segment])
    return (
        f"{result.started} {segments} | digest new={result.digest_new}"
        f" | consecutive_failures={consecutive_failures}"
    )


def format_tripwire_line(result: CycleResult) -> str:
    share = (
        100.0 * result.house_tripwire_quarantined / result.house_tripwire_records
    )
    legacy_note = (
        f"; legacy_unindexed={result.house_legacy_unindexed} excluded"
        if result.house_legacy_unindexed
        else ""
    )
    return (
        f"WARNING: house integrity quarantine {result.house_tripwire_quarantined}/"
        f"{result.house_tripwire_records} supported records ({share:.1f}%) exceeds the "
        f"{QUARANTINE_TRIPWIRE_RATIO:.0%} tripwire — possible mirror compromise;"
        f" inspect before trusting new data{legacy_note}"
    )


# --- loop ------------------------------------------------------------------

def resolve_interval_hours(cli_value: float | None) -> float:
    if cli_value is not None:
        return cli_value
    env_value = os.environ.get("PT_RUN_INTERVAL_HOURS")
    if env_value:
        return float(env_value)
    return DEFAULT_INTERVAL_HOURS


def run_loop(
    db_path: str,
    interval_hours: float,
    once: bool = False,
    lock_path: str | Path | None = None,
) -> int:
    """Run cycles until interrupted (or one cycle with once=True). Returns exit code."""
    if lock_path is None:
        lock_path = f"{db_path}.runner.lock"
    interval_seconds = interval_hours * 3600.0
    # A live runner refreshes the lock every cycle; well past two intervals
    # with no heartbeat means the holder is dead.
    stale_after = max(2.0 * interval_seconds, 600.0)
    if not acquire_lock(lock_path, stale_after):
        print(f"another runner holds the lock ({lock_path}); exiting")
        return 1
    consecutive_failures = 0
    try:
        while True:
            refresh_lock(lock_path)
            conn = db.connect(db_path)
            try:
                result = run_cycle(conn)
            finally:
                conn.close()
            consecutive_failures = (
                consecutive_failures + 1 if result.failures else 0
            )
            print(format_cycle_line(result, consecutive_failures))
            if result.tripwire:
                print(format_tripwire_line(result))
            if once:
                return 0
            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        print("runner stopped")
        return 0
    finally:
        release_lock(lock_path)
