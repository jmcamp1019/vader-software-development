"""Tests for the WO-7 price cache. No network; fetch functions are injected."""
from __future__ import annotations

import unittest

import _path  # noqa: F401

from pelositracker import db
from pelositracker.prices import (
    PriceCoverage,
    ensure_prices,
    get_series,
    init_prices_schema,
    parse_stooq_csv,
    stooq_symbol,
)


class SymbolAndParseTests(unittest.TestCase):
    def test_symbol_mapping(self) -> None:
        self.assertEqual(stooq_symbol("NVDA"), "nvda.us")
        self.assertEqual(stooq_symbol("BRK.B"), "brk-b.us")
        self.assertEqual(stooq_symbol(" spy "), "spy.us")

    def test_parse_valid_csv_to_cents(self) -> None:
        payload = (
            "Date,Open,High,Low,Close,Volume\n"
            "2026-01-05,99.5,101.0,99.0,100.25,1000\n"
            "2026-01-06,100.3,102.0,100.0,101.5,1100\n"
        )
        self.assertEqual(
            parse_stooq_csv(payload, "TST"),
            [("2026-01-05", 10025), ("2026-01-06", 10150)],
        )

    def test_parse_rejects_no_data_and_empty(self) -> None:
        with self.assertRaises(ValueError):
            parse_stooq_csv("No data", "NOPE")
        with self.assertRaises(ValueError):
            parse_stooq_csv("Date,Open,High,Low,Close,Volume\n", "NOPE")


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

    def test_upsert_idempotent(self) -> None:
        for _ in range(2):
            self.conn.executemany(
                "INSERT OR REPLACE INTO prices (ticker, date, close_cents) VALUES (?, ?, ?)",
                [("TST", "2026-07-14", 10000)],
            )
        self.assertEqual(len(get_series(self.conn, "TST")), 1)


if __name__ == "__main__":
    unittest.main()
