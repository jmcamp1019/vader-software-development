"""SQLite persistence layer for PelosiTracker.

Idempotency contract: trades carry a UNIQUE ingest_hash; re-ingesting the
same feed inserts nothing new (INSERT OR IGNORE).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

from .normalizer import (
    NormalizedTrade,
    canonical_politician_name,
    display_politician_name,
)

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
    ingest_hash TEXT NOT NULL UNIQUE,
    provenance TEXT
);

CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades (ticker);
CREATE INDEX IF NOT EXISTS idx_trades_politician ON trades (politician_id);
CREATE INDEX IF NOT EXISTS idx_trades_disclosure_date ON trades (disclosure_date);

CREATE TABLE IF NOT EXISTS politician_aliases (
    alias_key TEXT NOT NULL,
    chamber TEXT NOT NULL CHECK (chamber IN ('house', 'senate')),
    politician_id INTEGER NOT NULL REFERENCES politicians (id),
    PRIMARY KEY (alias_key, chamber)
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate_provenance(conn)
    _migrate_politician_identity(conn)
    # Imported here to keep the core schema and the additive WO-9 campaign
    # schema separately owned without a module import cycle.
    from . import shadow

    shadow.init_schema(conn)
    conn.commit()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _repoint_politician(
    conn: sqlite3.Connection, dupe_id: int, survivor_id: int
) -> None:
    """Move everything referencing a duplicate politician onto the survivor."""
    conn.execute(
        "UPDATE trades SET politician_id = ? WHERE politician_id = ?",
        (survivor_id, dupe_id),
    )
    if _table_exists(conn, "watchlists"):
        survivor_watched = conn.execute(
            "SELECT 1 FROM watchlists WHERE kind = 'politician' AND politician_id = ?",
            (survivor_id,),
        ).fetchone()
        if survivor_watched is None:
            conn.execute(
                "UPDATE watchlists SET politician_id = ?"
                " WHERE kind = 'politician' AND politician_id = ?",
                (survivor_id, dupe_id),
            )
        else:
            # Survivor already watched; a second row would break the partial
            # unique index and mean the same thing anyway.
            conn.execute(
                "DELETE FROM watchlists"
                " WHERE kind = 'politician' AND politician_id = ?",
                (dupe_id,),
            )
    conn.execute(
        "UPDATE politician_aliases SET politician_id = ? WHERE politician_id = ?",
        (survivor_id, dupe_id),
    )
    conn.execute("DELETE FROM politicians WHERE id = ?", (dupe_id,))


def _migrate_politician_identity(conn: sqlite3.Connection) -> None:
    """Canonical identity per member; merges duplicate spellings.

    Pass 1: every politician row gets a canonical alias key (honorifics
    dropped, adjacent duplicate tokens collapsed, casefolded). Rows whose key
    already belongs to another member of the same chamber are merged into the
    earliest-seen row.

    Pass 2 (initials): keys that differ only by the presence of single-letter
    initials ("c scott franklin" vs "scott franklin") merge ONLY when every
    initialed variant in the group agrees on the initials — conflicting
    initials ("john a smith" vs "john b smith") never merge. Requires at
    least two non-initial tokens so bare surnames cannot collapse.

    Incremental and idempotent: politicians without an alias row are the only
    pass-1 work; pass-2 groups converge to single members after merging.
    """
    unaliased = conn.execute(
        """
        SELECT p.id, p.full_name, p.chamber FROM politicians p
        LEFT JOIN politician_aliases a ON a.politician_id = p.id
        WHERE a.politician_id IS NULL ORDER BY p.id ASC
        """
    ).fetchall()
    for row in unaliased:
        key = canonical_politician_name(str(row["full_name"]))
        existing = conn.execute(
            "SELECT politician_id FROM politician_aliases"
            " WHERE alias_key = ? AND chamber = ?",
            (key, row["chamber"]),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO politician_aliases (alias_key, chamber, politician_id)"
                " VALUES (?, ?, ?)",
                (key, row["chamber"], row["id"]),
            )
        else:
            _repoint_politician(conn, int(row["id"]), int(existing["politician_id"]))

    groups: dict[tuple[str, str], list[tuple[str, int]]] = {}
    for row in conn.execute(
        "SELECT alias_key, chamber, politician_id FROM politician_aliases"
    ).fetchall():
        key = str(row["alias_key"])
        stripped = " ".join(t for t in key.split() if len(t) > 1)
        groups.setdefault((stripped, str(row["chamber"])), []).append(
            (key, int(row["politician_id"]))
        )
    for (stripped, _chamber), members in groups.items():
        ids = sorted({pid for _, pid in members})
        if len(ids) < 2 or len(stripped.split()) < 2:
            continue
        signatures = {
            tuple(t for t in key.split() if len(t) == 1) for key, _ in members
        }
        signatures.discard(())
        if len(signatures) != 1:
            continue  # conflicting initials: distinct people, never merge
        survivor = ids[0]
        for dupe in ids[1:]:
            _repoint_politician(conn, dupe, survivor)

    # Pass 3: repair survivor display names ("Scott Scott Franklin" ->
    # "Scott Franklin"). When the repaired spelling already names another row
    # in the same chamber, the two are a credentials-variant duplicate
    # ("Neal Patrick MD, FACS Dunn" vs "Neal Patrick Dunn") and merge.
    for row in conn.execute(
        "SELECT id, full_name, chamber FROM politicians"
    ).fetchall():
        pid = int(row["id"])
        if conn.execute(
            "SELECT 1 FROM politicians WHERE id = ?", (pid,)
        ).fetchone() is None:
            continue  # merged away earlier in this pass
        repaired = display_politician_name(str(row["full_name"]))
        if repaired == str(row["full_name"]):
            continue
        clash = conn.execute(
            "SELECT id FROM politicians WHERE full_name = ? AND chamber = ?"
            " AND id != ?",
            (repaired, row["chamber"], pid),
        ).fetchone()
        if clash is not None:
            survivor, dupe = sorted((pid, int(clash["id"])))
            _repoint_politician(conn, dupe, survivor)
            conn.execute(
                "UPDATE politicians SET full_name = ? WHERE id = ?",
                (repaired, survivor),
            )
        else:
            conn.execute(
                "UPDATE politicians SET full_name = ? WHERE id = ?",
                (repaired, pid),
            )


def _migrate_provenance(conn: sqlite3.Connection) -> None:
    """Additive migration: per-source provenance on trades (ADR-001).

    Pre-existing rows are backfilled by chamber — before this migration each
    chamber had exactly one live source, so the mapping is faithful.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(trades)")}
    if "provenance" in columns:
        return
    conn.execute("ALTER TABLE trades ADD COLUMN provenance TEXT")
    conn.execute(
        """
        UPDATE trades SET provenance = (
            SELECT CASE p.chamber
                WHEN 'senate' THEN 'senate-stock-watcher-github'
                ELSE 'house-stock-watcher-legacy'
            END
            FROM politicians p WHERE p.id = trades.politician_id
        )
        WHERE provenance IS NULL
        """
    )


def purge_provenance(conn: sqlite3.Connection, provenance: str) -> int:
    """Delete every trade a single source contributed. Returns rows deleted."""
    cursor = conn.execute("DELETE FROM trades WHERE provenance = ?", (provenance,))
    conn.commit()
    return cursor.rowcount


def get_or_create_politician(conn: sqlite3.Connection, full_name: str, chamber: str) -> int:
    """Resolve by canonical identity so variant spellings share one row."""
    key = canonical_politician_name(full_name)
    alias = conn.execute(
        "SELECT politician_id FROM politician_aliases"
        " WHERE alias_key = ? AND chamber = ?",
        (key, chamber),
    ).fetchone()
    if alias is not None:
        return int(alias["politician_id"])
    conn.execute(
        "INSERT OR IGNORE INTO politicians (full_name, chamber) VALUES (?, ?)",
        (full_name, chamber),
    )
    row = conn.execute(
        "SELECT id FROM politicians WHERE full_name = ? AND chamber = ?",
        (full_name, chamber),
    ).fetchone()
    assert row is not None  # INSERT OR IGNORE guarantees existence
    conn.execute(
        "INSERT OR IGNORE INTO politician_aliases (alias_key, chamber, politician_id)"
        " VALUES (?, ?, ?)",
        (key, chamber, int(row["id"])),
    )
    return int(row["id"])


def upsert_trades(
    conn: sqlite3.Connection,
    trades: Iterable[NormalizedTrade],
    provenance: str | None = None,
) -> tuple[int, int]:
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
                transaction_date, disclosure_date, owner, source_url, ingest_hash,
                provenance
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                provenance,
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
