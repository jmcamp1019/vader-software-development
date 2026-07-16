"""Daily close-price history cached from Yahoo's free chart API (WO-7).

Closes are stored as integer cents. Tickers the source cannot resolve land in
unpriced_tickers and are excluded from backtests — the exclusion count is
part of every backtest output (survivorship honesty). Transient failures
(throttling, outages) are never recorded as unpriced; they are retried once
and otherwise skipped for this run only.
"""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.error
import urllib.parse
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

# The only HTTP status that definitively means "this symbol does not exist".
# Every other failure (throttle, outage, block) is transient/systemic and must
# never mark a ticker unpriced (ADR-002).
_SYMBOL_NOT_FOUND_HTTP = 404

FetchFn = Callable[[str], list[tuple[str, int]]]


class TransientFetchError(Exception):
    """Endpoint hiccup (throttle/outage); must not mark the ticker unpriced."""


def init_prices_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def yahoo_symbol(ticker: str) -> str:
    """Map a US ticker to Yahoo's symbol form (BRK.B -> BRK-B)."""
    return ticker.strip().upper().replace(".", "-")


def parse_yahoo_chart(payload: str, ticker: str) -> list[tuple[str, int]]:
    """Parse Yahoo chart JSON into (date, close_cents); ValueError when unusable."""
    try:
        result = json.loads(payload)["chart"]["result"][0]
        timestamps = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"yahoo returned no data for {ticker}") from exc
    if not isinstance(timestamps, list) or not isinstance(closes, list):
        raise ValueError(f"yahoo returned no data for {ticker}")
    rows: list[tuple[str, int]] = []
    for ts, close in zip(timestamps, closes):
        if close is None:
            continue
        # US-market bar timestamps fall mid-session UTC, so the UTC date is
        # the trading date.
        day = datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat()
        close_cents = round(float(close) * 100)
        if close_cents > 0:
            rows.append((day, close_cents))
    if not rows:
        raise ValueError(f"yahoo returned an empty series for {ticker}")
    return rows


def fetch_daily_closes(ticker: str) -> list[tuple[str, int]]:
    """Fetch (date, close_cents) rows for a ticker.

    Raises ValueError when the ticker is unresolvable (record as unpriced) and
    TransientFetchError on throttling/outage (skip this run, never record).
    """
    url = config.YAHOO_CHART_URL_TEMPLATE.format(
        symbol=urllib.parse.quote(yahoo_symbol(ticker)),
        range=config.PRICE_FETCH_RANGE,
    )
    request = urllib.request.Request(url, headers={"User-Agent": config.USER_AGENT})
    try:
        with urllib.request.urlopen(
            request, timeout=config.HTTP_TIMEOUT_SECONDS
        ) as resp:
            payload = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code == _SYMBOL_NOT_FOUND_HTTP:
            raise ValueError(f"HTTP 404 (symbol not found) for {ticker}") from exc
        raise TransientFetchError(f"HTTP {exc.code} for {ticker}") from exc
    except OSError as exc:
        raise TransientFetchError(f"{exc} for {ticker}") from exc
    return parse_yahoo_chart(payload, ticker)


@dataclass
class PriceCoverage:
    fetched: int = 0
    cached: int = 0
    unpriced: int = 0
    skipped: int = 0  # transient failures; retried next run, never unpriced
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
            rows = _fetch_with_retry(fetch, ticker, sleep_seconds)
        except (TransientFetchError, OSError) as exc:
            coverage.skipped += 1
            log(f"[prices] {ticker}: SKIPPED transient ({exc})")
            time.sleep(sleep_seconds)
            continue
        except ValueError as exc:
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


def _fetch_with_retry(
    fetch: FetchFn, ticker: str, sleep_seconds: float
) -> list[tuple[str, int]]:
    """One retry after a longer pause on transient failure; ValueError passes through."""
    try:
        return fetch(ticker)
    except (TransientFetchError, OSError):
        time.sleep(sleep_seconds * 10)
        return fetch(ticker)


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
