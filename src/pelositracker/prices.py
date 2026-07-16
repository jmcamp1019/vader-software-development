"""Daily close-price history cached from Stooq's free CSV endpoint (WO-7).

Closes are stored as integer cents. Tickers Stooq cannot resolve land in
unpriced_tickers and are excluded from backtests — the exclusion count is
part of every backtest output (survivorship honesty).
"""
from __future__ import annotations

import csv
import io
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date as date_type, datetime, timedelta, timezone
from typing import Callable

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    ticker TEXT NOT NULL,
    date TEXT NOT NULL,
    close_cents INTEGER NOT NULL,
    PRIMARY KEY (ticker, date)
);

CREATE TABLE IF NOT EXISTS unpriced_tickers (
    ticker TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    checked_at TEXT NOT NULL
);
"""

# A cached series is considered current when its newest row is within this
# many days of the requested end date (weekends/holidays grace).
_COVERAGE_GRACE_DAYS = 7

FetchFn = Callable[[str], list[tuple[str, int]]]


def init_prices_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def stooq_symbol(ticker: str) -> str:
    """Map a US ticker to Stooq's symbol form (BRK.B -> brk-b.us)."""
    return ticker.strip().lower().replace(".", "-") + ".us"


def parse_stooq_csv(payload: str, ticker: str) -> list[tuple[str, int]]:
    """Parse Stooq daily CSV into (date, close_cents); ValueError when unusable."""
    if not payload.startswith("Date,"):
        raise ValueError(f"stooq returned no data for {ticker}")
    rows: list[tuple[str, int]] = []
    for record in csv.DictReader(io.StringIO(payload)):
        date = (record.get("Date") or "").strip()
        raw_close = (record.get("Close") or "").strip()
        if not date or not raw_close:
            continue
        try:
            close_cents = round(float(raw_close) * 100)
        except ValueError:
            continue
        if close_cents > 0:
            rows.append((date, close_cents))
    if not rows:
        raise ValueError(f"stooq returned an empty series for {ticker}")
    return rows


def fetch_daily_closes(ticker: str) -> list[tuple[str, int]]:
    """Fetch (date, close_cents) rows for a ticker; ValueError when unresolvable."""
    url = config.STOOQ_DAILY_CSV_URL_TEMPLATE.format(symbol=stooq_symbol(ticker))
    request = urllib.request.Request(url, headers={"User-Agent": config.USER_AGENT})
    with urllib.request.urlopen(request, timeout=config.HTTP_TIMEOUT_SECONDS) as resp:
        payload = resp.read().decode("utf-8", errors="replace")
    return parse_stooq_csv(payload, ticker)


@dataclass
class PriceCoverage:
    fetched: int = 0
    cached: int = 0
    unpriced: int = 0
    unpriced_tickers: list[str] = field(default_factory=list)


def _covered(conn: sqlite3.Connection, ticker: str, end_date: str) -> bool:
    row = conn.execute(
        "SELECT MAX(date) AS newest FROM prices WHERE ticker = ?", (ticker,)
    ).fetchone()
    if row is None or row["newest"] is None:
        return False
    needed = date_type.fromisoformat(end_date) - timedelta(days=_COVERAGE_GRACE_DAYS)
    return date_type.fromisoformat(str(row["newest"])) >= needed


def ensure_prices(
    conn: sqlite3.Connection,
    tickers: list[str],
    end_date: str,
    fetch: FetchFn = fetch_daily_closes,
    sleep_seconds: float | None = None,
    log: Callable[[str], None] = print,
) -> PriceCoverage:
    """Make sure each ticker's series reaches end_date; fetch only what's missing."""
    if sleep_seconds is None:
        sleep_seconds = config.PRICE_FETCH_SLEEP_SECONDS
    init_prices_schema(conn)
    coverage = PriceCoverage()
    known_unpriced = {
        str(row["ticker"])
        for row in conn.execute("SELECT ticker FROM unpriced_tickers")
    }
    for ticker in sorted({t.upper() for t in tickers}):
        if ticker in known_unpriced:
            coverage.unpriced += 1
            coverage.unpriced_tickers.append(ticker)
            continue
        if _covered(conn, ticker, end_date):
            coverage.cached += 1
            continue
        try:
            rows = fetch(ticker)
        except (ValueError, OSError) as exc:  # URLError is an OSError
            conn.execute(
                "INSERT OR REPLACE INTO unpriced_tickers (ticker, reason, checked_at)"
                " VALUES (?, ?, ?)",
                (ticker, str(exc), _now_iso()),
            )
            conn.commit()
            coverage.unpriced += 1
            coverage.unpriced_tickers.append(ticker)
            log(f"[prices] {ticker}: UNPRICED ({exc})")
            time.sleep(sleep_seconds)
            continue
        conn.executemany(
            "INSERT OR REPLACE INTO prices (ticker, date, close_cents) VALUES (?, ?, ?)",
            [(ticker, date, close) for date, close in rows],
        )
        conn.commit()
        coverage.fetched += 1
        log(f"[prices] {ticker}: fetched {len(rows)} rows")
        time.sleep(sleep_seconds)
    return coverage


def get_series(
    conn: sqlite3.Connection, ticker: str, start_date: str | None = None
) -> list[tuple[str, int]]:
    """Sorted (date, close_cents) series; optionally from start_date onward."""
    sql = "SELECT date, close_cents FROM prices WHERE ticker = ?"
    args: list[str] = [ticker.upper()]
    if start_date is not None:
        sql += " AND date >= ?"
        args.append(start_date)
    sql += " ORDER BY date ASC"
    return [
        (str(row["date"]), int(row["close_cents"]))
        for row in conn.execute(sql, args)
    ]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
