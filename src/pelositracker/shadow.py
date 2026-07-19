"""Prospective WO-9 shadow tracking for the frozen WO-8 H2 strategy.

The campaign records qualifying public disclosures only.  It is write-once,
append-only, and deliberately contains no execution or position model.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import hypotheses

STRATEGY_KEY = "H2_FAST_FILERS"
STRATEGY_VERSION = 1
WINDOW_DAYS = 90

STATUS_NOT_STARTED = "not-started"
STATUS_ACTIVE = "active"
STATUS_COMPLETED = "completed"

SCHEMA = """
CREATE TABLE IF NOT EXISTS shadow_tracking_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    strategy_key TEXT NOT NULL CHECK (strategy_key = 'H2_FAST_FILERS'),
    strategy_version INTEGER NOT NULL CHECK (strategy_version = 1),
    activation_utc TEXT NOT NULL,
    scheduled_end_utc TEXT NOT NULL,
    activation_trade_id_boundary INTEGER NOT NULL
        CHECK (activation_trade_id_boundary >= 0),
    last_scanned_trade_id INTEGER NOT NULL
        CHECK (last_scanned_trade_id >= activation_trade_id_boundary),
    completed_utc TEXT,
    CHECK (completed_utc IS NULL OR completed_utc >= scheduled_end_utc)
);

CREATE TABLE IF NOT EXISTS shadow_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_trade_id INTEGER NOT NULL UNIQUE,
    observation_utc TEXT NOT NULL,
    strategy_key TEXT NOT NULL CHECK (strategy_key = 'H2_FAST_FILERS'),
    strategy_version INTEGER NOT NULL CHECK (strategy_version = 1),
    politician_id INTEGER NOT NULL,
    politician_name TEXT NOT NULL,
    chamber TEXT NOT NULL CHECK (chamber IN ('house', 'senate')),
    ticker TEXT,
    asset_name TEXT NOT NULL,
    transaction_date TEXT NOT NULL,
    disclosure_date TEXT NOT NULL,
    disclosure_lag_days INTEGER NOT NULL
        CHECK (disclosure_lag_days BETWEEN 0 AND 15),
    transaction_type TEXT NOT NULL CHECK (transaction_type = 'buy'),
    amount_min_cents INTEGER NOT NULL,
    amount_max_cents INTEGER,
    source_url TEXT NOT NULL,
    provenance TEXT
);

CREATE TABLE IF NOT EXISTS shadow_scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_utc TEXT NOT NULL,
    before_trade_id INTEGER NOT NULL CHECK (before_trade_id >= 0),
    after_trade_id INTEGER NOT NULL CHECK (after_trade_id >= before_trade_id),
    rows_examined INTEGER NOT NULL CHECK (rows_examined >= 0),
    signals_appended INTEGER NOT NULL CHECK (signals_appended >= 0),
    rejected_backfills INTEGER NOT NULL CHECK (rejected_backfills >= 0),
    campaign_status TEXT NOT NULL CHECK (campaign_status IN ('active', 'completed'))
);

CREATE TRIGGER IF NOT EXISTS shadow_state_identity_immutable
BEFORE UPDATE ON shadow_tracking_state
WHEN NEW.id IS NOT OLD.id
  OR NEW.strategy_key IS NOT OLD.strategy_key
  OR NEW.strategy_version IS NOT OLD.strategy_version
  OR NEW.activation_utc IS NOT OLD.activation_utc
  OR NEW.scheduled_end_utc IS NOT OLD.scheduled_end_utc
  OR NEW.activation_trade_id_boundary IS NOT OLD.activation_trade_id_boundary
BEGIN
    SELECT RAISE(ABORT, 'shadow campaign identity is immutable');
END;

CREATE TRIGGER IF NOT EXISTS shadow_state_progress_monotonic
BEFORE UPDATE ON shadow_tracking_state
WHEN NEW.last_scanned_trade_id < OLD.last_scanned_trade_id
  OR (OLD.completed_utc IS NOT NULL AND NEW.completed_utc IS NOT OLD.completed_utc)
BEGIN
    SELECT RAISE(ABORT, 'shadow campaign progress cannot move backward');
END;

CREATE TRIGGER IF NOT EXISTS shadow_state_no_delete
BEFORE DELETE ON shadow_tracking_state
BEGIN
    SELECT RAISE(ABORT, 'shadow campaign cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS shadow_signals_no_update
BEFORE UPDATE ON shadow_signals
BEGIN
    SELECT RAISE(ABORT, 'shadow signals are append-only');
END;

CREATE TRIGGER IF NOT EXISTS shadow_signals_no_delete
BEFORE DELETE ON shadow_signals
BEGIN
    SELECT RAISE(ABORT, 'shadow signals are append-only');
END;

CREATE TRIGGER IF NOT EXISTS shadow_scans_no_update
BEFORE UPDATE ON shadow_scans
BEGIN
    SELECT RAISE(ABORT, 'shadow scans are append-only');
END;

CREATE TRIGGER IF NOT EXISTS shadow_scans_no_delete
BEFORE DELETE ON shadow_scans
BEGIN
    SELECT RAISE(ABORT, 'shadow scans are append-only');
END;
"""


@dataclass(frozen=True)
class ScanResult:
    status: str
    scan_utc: str
    before_trade_id: int
    after_trade_id: int
    rows_examined: int
    signals_appended: int
    rejected_backfills: int


@dataclass(frozen=True)
class ShadowStatus:
    status: str
    strategy_key: str | None
    strategy_version: int | None
    activation_utc: str | None
    scheduled_end_utc: str | None
    activation_trade_id_boundary: int | None
    last_scanned_trade_id: int | None
    completed_utc: str | None
    scan_count: int
    signal_count: int


def init_schema(conn: sqlite3.Connection) -> None:
    """Create the additive WO-9 tables and immutability triggers."""
    conn.executescript(SCHEMA)


def _utc_timestamp(now: datetime | None) -> tuple[datetime, str]:
    value = datetime.now(timezone.utc) if now is None else now
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("shadow timestamps must be timezone-aware")
    value = value.astimezone(timezone.utc).replace(microsecond=0)
    return value, value.isoformat(timespec="seconds")


def _begin_write(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        raise sqlite3.OperationalError(
            "shadow operation requires a connection with no pending transaction"
        )
    conn.execute("BEGIN IMMEDIATE")


def start(conn: sqlite3.Connection, now: datetime | None = None) -> ShadowStatus:
    """Activate the sole campaign and baseline every currently stored trade."""
    activated, activation_utc = _utc_timestamp(now)
    scheduled_end_utc = (activated + timedelta(days=WINDOW_DAYS)).isoformat(
        timespec="seconds"
    )
    _begin_write(conn)
    try:
        existing = conn.execute(
            "SELECT 1 FROM shadow_tracking_state WHERE id = 1"
        ).fetchone()
        if existing is not None:
            raise ValueError("shadow tracking already started")
        row = conn.execute("SELECT COALESCE(MAX(id), 0) AS max_id FROM trades").fetchone()
        boundary = int(row["max_id"])
        conn.execute(
            """
            INSERT INTO shadow_tracking_state (
                id, strategy_key, strategy_version, activation_utc,
                scheduled_end_utc, activation_trade_id_boundary,
                last_scanned_trade_id, completed_utc
            ) VALUES (1, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                STRATEGY_KEY,
                STRATEGY_VERSION,
                activation_utc,
                scheduled_end_utc,
                boundary,
                boundary,
            ),
        )
        conn.commit()
    except (sqlite3.Error, ValueError):
        conn.rollback()
        raise
    return get_status(conn)


def _is_backfill(disclosure_date: object, activation_date: date) -> bool:
    try:
        disclosed = date.fromisoformat(str(disclosure_date))
    except (TypeError, ValueError):
        return False
    return disclosed < activation_date


def _insert_signal(
    conn: sqlite3.Connection, trade: dict[str, Any], observation_utc: str
) -> None:
    lag = (
        date.fromisoformat(str(trade["disclosure_date"]))
        - date.fromisoformat(str(trade["transaction_date"]))
    ).days
    conn.execute(
        """
        INSERT INTO shadow_signals (
            source_trade_id, observation_utc, strategy_key, strategy_version,
            politician_id, politician_name, chamber, ticker, asset_name,
            transaction_date, disclosure_date, disclosure_lag_days,
            transaction_type, amount_min_cents, amount_max_cents,
            source_url, provenance
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(trade["id"]),
            observation_utc,
            STRATEGY_KEY,
            STRATEGY_VERSION,
            int(trade["politician_id"]),
            str(trade["politician_name"]),
            str(trade["chamber"]),
            trade["ticker"],
            str(trade["asset_name"]),
            str(trade["transaction_date"]),
            str(trade["disclosure_date"]),
            lag,
            str(trade["transaction_type"]),
            int(trade["amount_min_cents"]),
            trade["amount_max_cents"],
            str(trade["source_url"]),
            trade["provenance"],
        ),
    )


def _insert_scan_audit(conn: sqlite3.Connection, result: ScanResult) -> None:
    conn.execute(
        """
        INSERT INTO shadow_scans (
            scan_utc, before_trade_id, after_trade_id, rows_examined,
            signals_appended, rejected_backfills, campaign_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            result.scan_utc,
            result.before_trade_id,
            result.after_trade_id,
            result.rows_examined,
            result.signals_appended,
            result.rejected_backfills,
            result.status,
        ),
    )


def scan(conn: sqlite3.Connection, now: datetime | None = None) -> ScanResult:
    """Append newly observed H2 disclosures and advance the durable watermark."""
    observed, observation_utc = _utc_timestamp(now)
    _begin_write(conn)
    try:
        state = conn.execute(
            "SELECT * FROM shadow_tracking_state WHERE id = 1"
        ).fetchone()
        if state is None:
            conn.rollback()
            return ScanResult(
                STATUS_NOT_STARTED, observation_utc, 0, 0, 0, 0, 0
            )

        before = int(state["last_scanned_trade_id"])
        if state["completed_utc"] is not None:
            conn.commit()
            return ScanResult(
                STATUS_COMPLETED, observation_utc, before, before, 0, 0, 0
            )

        activation = datetime.fromisoformat(str(state["activation_utc"]))
        scheduled_end = datetime.fromisoformat(str(state["scheduled_end_utc"]))
        if observed < activation:
            raise ValueError("scan timestamp precedes campaign activation")

        max_row = conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS max_id FROM trades"
        ).fetchone()
        # A provenance purge can remove the current highest trade rows.  The
        # durable watermark must still never move backward; AUTOINCREMENT
        # ensures later rows will advance beyond it.
        captured_max = max(before, int(max_row["max_id"]))
        if observed >= scheduled_end:
            conn.execute(
                "UPDATE shadow_tracking_state SET completed_utc = ? WHERE id = 1",
                (observation_utc,),
            )
            result = ScanResult(
                STATUS_COMPLETED, observation_utc, before, before, 0, 0, 0
            )
            _insert_scan_audit(conn, result)
            conn.commit()
            return result

        rows = conn.execute(
            """
            SELECT t.id, t.politician_id,
                   p.full_name AS politician_name, p.chamber,
                   t.ticker, t.asset_name, t.transaction_type,
                   t.amount_min_cents, t.amount_max_cents,
                   t.transaction_date, t.disclosure_date,
                   t.source_url, t.provenance
            FROM trades t
            JOIN politicians p ON p.id = t.politician_id
            WHERE t.id > ? AND t.id <= ?
            ORDER BY t.id ASC
            """,
            (before, captured_max),
        ).fetchall()
        activation_date = activation.date()
        candidates: list[dict[str, Any]] = []
        rejected_backfills = 0
        for row in rows:
            trade = dict(row)
            if _is_backfill(trade["disclosure_date"], activation_date):
                rejected_backfills += 1
            else:
                candidates.append(trade)

        selected = hypotheses.fast_filers(candidates)
        for selected_trade in selected:
            _insert_signal(conn, dict(selected_trade), observation_utc)

        result = ScanResult(
            STATUS_ACTIVE,
            observation_utc,
            before,
            captured_max,
            len(rows),
            len(selected),
            rejected_backfills,
        )
        conn.execute(
            "UPDATE shadow_tracking_state SET last_scanned_trade_id = ? WHERE id = 1",
            (captured_max,),
        )
        _insert_scan_audit(conn, result)
        conn.commit()
        return result
    except (sqlite3.Error, ValueError, KeyError, TypeError):
        conn.rollback()
        raise


def get_status(conn: sqlite3.Connection) -> ShadowStatus:
    """Return campaign identity, progress, and append-only row counts."""
    state = conn.execute(
        "SELECT * FROM shadow_tracking_state WHERE id = 1"
    ).fetchone()
    scan_count = int(conn.execute("SELECT COUNT(*) FROM shadow_scans").fetchone()[0])
    signal_count = int(
        conn.execute("SELECT COUNT(*) FROM shadow_signals").fetchone()[0]
    )
    if state is None:
        return ShadowStatus(
            STATUS_NOT_STARTED, None, None, None, None, None, None, None,
            scan_count, signal_count,
        )
    campaign_status = (
        STATUS_COMPLETED if state["completed_utc"] is not None else STATUS_ACTIVE
    )
    return ShadowStatus(
        campaign_status,
        str(state["strategy_key"]),
        int(state["strategy_version"]),
        str(state["activation_utc"]),
        str(state["scheduled_end_utc"]),
        int(state["activation_trade_id_boundary"]),
        int(state["last_scanned_trade_id"]),
        None if state["completed_utc"] is None else str(state["completed_utc"]),
        scan_count,
        signal_count,
    )


def format_scan_segment(result: ScanResult) -> str:
    """Format the structured runner/CLI audit segment."""
    return (
        f"shadow {result.status} examined={result.rows_examined}"
        f" appended={result.signals_appended}"
        f" rejected_backfills={result.rejected_backfills}"
    )
