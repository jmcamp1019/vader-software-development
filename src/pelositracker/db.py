"""SQLite persistence layer for PelosiTracker.

Idempotency contract: trades carry a UNIQUE ingest_hash; re-ingesting the
same feed inserts nothing new (INSERT OR IGNORE).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

from .normalizer import NormalizedTrade

SCHEMA = """
CREATE TABLE IF NOT EXISTS politicians (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name TEXT NOT NULL,
    chamber TEXT NOT NULL CHECK (chamber IN ('house', 'senate')),
    UNIQUE (full_name, chamber)
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    politician_id INTEGER NOT NULL REFERENCES politicians (id),
    ticker TEXT,
    asset_name TEXT NOT NULL,
    transaction_type TEXT NOT NULL CHECK (transaction_type IN ('buy', 'sell', 'exchange')),
    amount_min_cents INTEGER NOT NULL,
    amount_max_cents INTEGER,
    transaction_date TEXT NOT NULL,
    disclosure_date TEXT NOT NULL,
    owner TEXT,
    source_url TEXT NOT NULL,
    ingest_hash TEXT NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades (ticker);
CREATE INDEX IF NOT EXISTS idx_trades_politician ON trades (politician_id);
CREATE INDEX IF NOT EXISTS idx_trades_disclosure_date ON trades (disclosure_date);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def get_or_create_politician(conn: sqlite3.Connection, full_name: str, chamber: str) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO politicians (full_name, chamber) VALUES (?, ?)",
        (full_name, chamber),
    )
    row = conn.execute(
        "SELECT id FROM politicians WHERE full_name = ? AND chamber = ?",
        (full_name, chamber),
    ).fetchone()
    assert row is not None  # INSERT OR IGNORE guarantees existence
    return int(row["id"])


def upsert_trades(conn: sqlite3.Connection, trades: Iterable[NormalizedTrade]) -> tuple[int, int]:
    """Insert trades idempotently. Returns (inserted, duplicates)."""
    inserted = 0
    duplicates = 0
    for trade in trades:
        politician_id = get_or_create_politician(conn, trade.politician_name, trade.chamber)
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO trades (
                politician_id, ticker, asset_name, transaction_type,
                amount_min_cents, amount_max_cents,
                transaction_date, disclosure_date, owner, source_url, ingest_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                politician_id,
                trade.ticker,
                trade.asset_name,
                trade.transaction_type,
                trade.amount_min_cents,
                trade.amount_max_cents,
                trade.transaction_date,
                trade.disclosure_date,
                trade.owner,
                trade.source_url,
                trade.ingest_hash,
            ),
        )
        if cursor.rowcount == 1:
            inserted += 1
        else:
            duplicates += 1
    conn.commit()
    return inserted, duplicates


def trade_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM trades").fetchone()
    return int(row["n"])


def politician_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM politicians").fetchone()
    return int(row["n"])


def top_tickers(conn: sqlite3.Connection, limit: int = 10) -> list[tuple[str, int]]:
    rows = conn.execute(
        """
        SELECT ticker, COUNT(*) AS n FROM trades
        WHERE ticker IS NOT NULL
        GROUP BY ticker ORDER BY n DESC, ticker ASC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [(str(r["ticker"]), int(r["n"])) for r in rows]
