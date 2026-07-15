"""Alert digest module for PelosiTracker.

Selects new trades since the last run that match watchlists, writes them
to text files, and sends them via email.
"""

from __future__ import annotations

import os
import sqlite3
import smtplib
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .api import DISCLAIMER


@dataclass
class DigestResult:
    new_trades: int
    digest_text: str
    output_path: Path | None
    emailed: bool


def init_digest_schema(conn: sqlite3.Connection) -> None:
    """Create the digest_watermark table; safe to repeat."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS digest_watermark (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            last_seen_trade_id INTEGER NOT NULL
        );
        """
    )
    conn.commit()


def get_watermark(conn: sqlite3.Connection) -> int:
    """Get the last seen trade ID watermark; defaults to 0."""
    init_digest_schema(conn)
    row = conn.execute(
        "SELECT last_seen_trade_id FROM digest_watermark WHERE id = 1"
    ).fetchone()
    if row is None:
        return 0
    return int(row["last_seen_trade_id"])


def set_watermark(conn: sqlite3.Connection, trade_id: int) -> None:
    """Set the last seen trade ID watermark."""
    init_digest_schema(conn)
    conn.execute(
        "INSERT OR REPLACE INTO digest_watermark (id, last_seen_trade_id) VALUES (1, ?)",
        (trade_id,),
    )
    conn.commit()


def _format_cents(cents: int) -> str:
    """Format integer cents into a dollar string with thousands separators."""
    dollars = cents // 100
    remainder = cents % 100
    return f"${dollars:,}.{remainder:02d}"


def format_amount_range(min_cents: int, max_cents: int | None) -> str:
    """Format min/max cents into a range string with an en dash or '+' for open-ended."""
    min_str = _format_cents(min_cents)
    if max_cents is None:
        return f"{min_str} +"
    max_str = _format_cents(max_cents)
    return f"{min_str} – {max_str}"


def run_digest(
    conn: sqlite3.Connection,
    output_dir: str | Path = "digests",
    today: date | None = None,
) -> DigestResult:
    """Run the alert digest check and generate/send notifications."""
    if today is None:
        today = datetime.now(timezone.utc).date()

    # Check if watchlists table exists
    has_watchlists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'watchlists'"
    ).fetchone() is not None

    trades: list[dict[str, Any]] = []
    if has_watchlists:
        watermark = get_watermark(conn)
        rows = conn.execute(
            """
            SELECT t.id, p.full_name, p.chamber, t.ticker, t.asset_name,
                   t.transaction_type, t.amount_min_cents, t.amount_max_cents,
                   t.transaction_date, t.disclosure_date, t.source_url
            FROM trades t
            JOIN politicians p ON p.id = t.politician_id
            WHERE t.id > ? AND (
                t.ticker IN (SELECT ticker FROM watchlists WHERE kind = 'ticker')
                OR
                t.politician_id IN (SELECT politician_id FROM watchlists WHERE kind = 'politician')
            )
            ORDER BY t.id ASC
            """,
            (watermark,),
        ).fetchall()
        trades = [dict(row) for row in rows]

    # Generate digest_text
    if trades:
        blocks: list[str] = []
        for t in trades:
            ticker_or_asset = t["ticker"] if t["ticker"] is not None else t["asset_name"]
            amt_range = format_amount_range(t["amount_min_cents"], t["amount_max_cents"])
            block = (
                f"{t['full_name']} ({t['chamber']}) - {ticker_or_asset} - {t['transaction_type']}\n"
                f"amount: {amt_range}\n"
                f"transacted {t['transaction_date']}, disclosed {t['disclosure_date']}\n"
                f"source: {t['source_url']}"
            )
            blocks.append(block)

        digest_text = f"PelosiTracker digest — {today.isoformat()} — {len(trades)} new watched trade(s)\n\n"
        digest_text += "\n\n".join(blocks)
        digest_text += f"\n\n{DISCLAIMER}\n"
    else:
        digest_text = f"No new watched trades.\n\n{DISCLAIMER}\n"

    # Write file if there are matches
    file_path: Path | None = None
    if trades:
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        file_path = out_dir / f"{today.isoformat()}.txt"
        with file_path.open("a", encoding="utf-8") as f:
            f.write(digest_text)

    # SMTP configuration and email sending
    emailed = False
    if trades:
        smtp_host = os.environ.get("SMTP_HOST")
        smtp_port = os.environ.get("SMTP_PORT")
        smtp_from = os.environ.get("SMTP_FROM")
        smtp_to = os.environ.get("SMTP_TO")

        if smtp_host and smtp_port and smtp_from and smtp_to:
            from email.message import EmailMessage
            msg = EmailMessage()
            msg["Subject"] = f"PelosiTracker digest {today.isoformat()}"
            msg["From"] = smtp_from
            msg["To"] = smtp_to
            msg.set_content(digest_text)

            with smtplib.SMTP(smtp_host, int(smtp_port)) as server:
                server.send_message(msg)
            emailed = True

    # Advance watermark to MAX(trades.id)
    max_row = conn.execute("SELECT MAX(id) AS max_id FROM trades").fetchone()
    max_trade_id = max_row["max_id"] if max_row and max_row["max_id"] is not None else 0
    set_watermark(conn, max_trade_id)

    return DigestResult(
        new_trades=len(trades),
        digest_text=digest_text,
        output_path=file_path,
        emailed=emailed,
    )
