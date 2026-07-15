"""Watchlists: politicians or tickers to match in alert digests.

Each entry targets exactly one of (politician_id, ticker), enforced by a
CHECK constraint. Dedupe uses partial unique indexes rather than a UNIQUE
table constraint — SQLite treats NULLs as distinct inside UNIQUE
constraints, so a plain UNIQUE (kind, politician_id, ticker) would not
prevent duplicate entries.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('politician', 'ticker')),
    politician_id INTEGER REFERENCES politicians (id),
    ticker TEXT,
    created_at TEXT NOT NULL,
    CHECK (
        (kind = 'politician' AND politician_id IS NOT NULL AND ticker IS NULL)
        OR
        (kind = 'ticker' AND ticker IS NOT NULL AND politician_id IS NULL)
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_watchlists_unique_politician
    ON watchlists (politician_id) WHERE kind = 'politician';
CREATE UNIQUE INDEX IF NOT EXISTS idx_watchlists_unique_ticker
    ON watchlists (ticker) WHERE kind = 'ticker';
"""


def init_watchlists_schema(conn: sqlite3.Connection) -> None:
    """Create the watchlists table and dedupe indexes; safe to repeat."""
    conn.executescript(_SCHEMA)
    conn.commit()


def _table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'watchlists'"
    ).fetchone()
    return row is not None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def add_ticker(conn: sqlite3.Connection, ticker: str) -> int:
    symbol = ticker.strip().upper()
    if not symbol:
        raise ValueError("ticker required")
    init_watchlists_schema(conn)
    try:
        cursor = conn.execute(
            "INSERT INTO watchlists (kind, ticker, created_at) VALUES ('ticker', ?, ?)",
            (symbol, _utc_now()),
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError(f"ticker {symbol} is already watched") from exc
    conn.commit()
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def add_politician(conn: sqlite3.Connection, politician_id: int) -> int:
    init_watchlists_schema(conn)
    known = conn.execute(
        "SELECT 1 FROM politicians WHERE id = ?", (politician_id,)
    ).fetchone()
    if known is None:
        raise ValueError(f"unknown politician id {politician_id}")
    try:
        cursor = conn.execute(
            "INSERT INTO watchlists (kind, politician_id, created_at)"
            " VALUES ('politician', ?, ?)",
            (politician_id, _utc_now()),
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError(f"politician {politician_id} is already watched") from exc
    conn.commit()
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def list_watchlists(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """List entries oldest-first.

    Never writes or runs DDL — the API layer calls this through a read-only
    connection — so a database without the table yields [] untouched.
    """
    if not _table_exists(conn):
        return []
    rows = conn.execute(
        """
        SELECT w.id, w.kind, w.politician_id, p.full_name AS politician_name,
               w.ticker, w.created_at
        FROM watchlists w
        LEFT JOIN politicians p ON p.id = w.politician_id
        ORDER BY w.id ASC
        """
    ).fetchall()
    return [
        {
            "id": row["id"],
            "kind": row["kind"],
            "politician_id": row["politician_id"],
            "politician_name": row["politician_name"],
            "ticker": row["ticker"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def remove_watchlist(conn: sqlite3.Connection, watchlist_id: int) -> bool:
    if not _table_exists(conn):
        return False
    cursor = conn.execute("DELETE FROM watchlists WHERE id = ?", (watchlist_id,))
    conn.commit()
    return cursor.rowcount == 1
