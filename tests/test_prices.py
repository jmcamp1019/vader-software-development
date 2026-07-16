"""Tests for the WO-7 price cache. No network; fetch functions are injected."""
from __future__ import annotations

import io
import json
import unittest
import unittest.mock
import urllib.error
from datetime import datetime, timezone

import _path  # noqa: F401

from pelositracker import db
from pelositracker.prices import (
    PriceCoverage,
    TransientFetchError,
    ensure_prices,
    fetch_daily_closes,
    get_series,
    init_prices_schema,
    parse_yahoo_chart,
    yahoo_symbol,
)


def _epoch(day: str) -> int:
    # 14:30 UTC = 9:30 ET market open; mid-session so the UTC date is the
    # trading date, matching real Yahoo bar timestamps.
    return int(
        datetime.fromisoformat(f"{day}T14:30:00+00:00")
        .astimezone(timezone.utc)
        .timestamp()
    )


def _chart_payload(rows: list[tuple[str, float | None]]) -> str:
    return json.dumps(
        {
            "chart": {
                "result": [
                    {
                        "timestamp": [_epoch(day) for day, _ in rows],
                        "indicators": {
                            "quote": [{"close": [close for _, close in rows]}]
                        },
                    }
                ],
                "error": None,
            }
        }
    )


class SymbolAndParseTests(unittest.TestCase):
    def test_symbol_mapping(self) -> None:
        self.assertEqual(yahoo_symbol("NVDA"), "NVDA")
        self.assertEqual(yahoo_symbol("BRK.B"), "BRK-B")
        self.assertEqual(yahoo_symbol(" spy "), "SPY")

    def test_parse_valid_chart_to_cents(self) -> None:
        payload = _chart_payload([("2026-01-05", 100.25), ("2026-01-06", 101.5)])
        self.assertEqual(
            parse_yahoo_chart(payload, "TST"),
            [("2026-01-05", 10025), ("2026-01-06", 10150)],
        )

    def test_parse_skips_null_closes(self) -> None:
        payload = _chart_payload([("2026-01-05", 100.25), ("2026-01-06", None)])
        self.assertEqual(parse_yahoo_chart(payload, "TST"), [("2026-01-05", 10025)])

    def test_parse_rejects_no_data_and_empty(self) -> None:
        with self.assertRaises(ValueError):
            parse_yahoo_chart("not json", "NOPE")
        with self.assertRaises(ValueError):
            parse_yahoo_chart(
                json.dumps({"chart": {"result": None, "error": {"code": "Not Found"}}}),
                "NOPE",
            )
        with self.assertRaises(ValueError):
            parse_yahoo_chart(_chart_payload([("2026-01-05", None)]), "NOPE")
        with self.assertRaises(ValueError):  # null arrays, not omitted keys
            parse_yahoo_chart(
                json.dumps(
                    {
                        "chart": {
                            "result": [
                                {
                                    "timestamp": None,
                                    "indicators": {"quote": [{"close": None}]},
                                }
                            ]
                        }
                    }
                ),
                "NOPE",
            )


class HttpFailureMappingTests(unittest.TestCase):
    """Only a definitive 404 may mark a ticker unpriced (ADR-002)."""

    @staticmethod
    def _http_error(code: int) -> urllib.error.HTTPError:
        return urllib.error.HTTPError(
            "https://example.invalid", code, "err", None, io.BytesIO(b"")
        )

    def test_404_is_definitive(self) -> None:
        with unittest.mock.patch(
            "pelositracker.prices.urllib.request.urlopen",
            side_effect=self._http_error(404),
        ):
            with self.assertRaises(ValueError):
                fetch_daily_closes("NOPE")

    def test_throttle_and_block_are_transient(self) -> None:
        for code in (401, 403, 429, 500, 503):
            with unittest.mock.patch(
                "pelositracker.prices.urllib.request.urlopen",
                side_effect=self._http_error(code),
            ):
                with self.assertRaises(TransientFetchError):
                    fetch_daily_closes("TST")

    def test_network_error_is_transient(self) -> None:
        with unittest.mock.patch(
            "pelositracker.prices.urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection reset"),
        ):
            with self.assertRaises(TransientFetchError):
                fetch_daily_closes("TST")


class EnsurePricesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = db.connect(":memory:")
        init_prices_schema(self.conn)
        self.fetch_calls: list[str] = []

    def tearDown(self) -> None:
        self.conn.close()

    def _fake_fetch(self, ticker: str) -> list[tuple[str, int]]:
        self.fetch_calls.append(ticker)
        if ticker == "NOPE":
            raise ValueError("no data")
        return [("2026-07-14", 10000), ("2026-07-15", 10100)]

    def test_fetch_store_and_cache(self) -> None:
        coverage = ensure_prices(
            self.conn, ["tst", "NOPE"], "2026-07-16",
            fetch=self._fake_fetch, sleep_seconds=0, log=lambda line: None,
        )
        self.assertIsInstance(coverage, PriceCoverage)
        self.assertEqual(coverage.fetched, 1)
        self.assertEqual(coverage.unpriced, 1)
        self.assertEqual(coverage.unpriced_tickers, ["NOPE"])
        self.assertEqual(
            get_series(self.conn, "TST"),
            [("2026-07-14", 10000), ("2026-07-15", 10100)],
        )
        # Second call: covered ticker and known-unpriced ticker skip the fetch.
        self.fetch_calls.clear()
        coverage = ensure_prices(
            self.conn, ["TST", "NOPE"], "2026-07-16",
            fetch=self._fake_fetch, sleep_seconds=0, log=lambda line: None,
        )
        self.assertEqual(self.fetch_calls, [])
        self.assertEqual(coverage.cached, 1)
        self.assertEqual(coverage.unpriced, 1)

    def test_stale_series_refetched(self) -> None:
        self.conn.execute(
            "INSERT INTO prices (ticker, date, close_cents) VALUES ('TST', '2025-01-02', 9000)"
        )
        self.conn.commit()
        ensure_prices(
            self.conn, ["TST"], "2026-07-16",
            fetch=self._fake_fetch, sleep_seconds=0, log=lambda line: None,
        )
        self.assertEqual(self.fetch_calls, ["TST"])
        series = get_series(self.conn, "TST")
        self.assertEqual(len(series), 3)  # old row kept, new rows upserted

    def test_transient_failure_retried_once_never_marked_unpriced(self) -> None:
        calls: list[str] = []

        def throttled(ticker: str) -> list[tuple[str, int]]:
            calls.append(ticker)
            raise TransientFetchError("HTTP 429")

        coverage = ensure_prices(
            self.conn, ["TST"], "2026-07-16",
            fetch=throttled, sleep_seconds=0, log=lambda line: None,
        )
        self.assertEqual(calls, ["TST", "TST"])  # exactly one retry
        self.assertEqual(coverage.skipped, 1)
        self.assertEqual(coverage.unpriced, 0)
        unpriced_rows = self.conn.execute(
            "SELECT COUNT(*) FROM unpriced_tickers"
        ).fetchone()[0]
        self.assertEqual(unpriced_rows, 0)

    def test_upsert_idempotent(self) -> None:
        for _ in range(2):
            self.conn.executemany(
                "INSERT OR REPLACE INTO prices (ticker, date, close_cents) VALUES (?, ?, ?)",
                [("TST", "2026-07-14", 10000)],
            )
        self.assertEqual(len(get_series(self.conn, "TST")), 1)


if __name__ == "__main__":
    unittest.main()
