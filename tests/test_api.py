"""Tests for the WO-1 read-only query API.

All politicians and trades here are FICTIONAL — TEST DATA only.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from email.message import Message
from typing import Any

import _path  # noqa: F401

from pelositracker import db
from pelositracker.api import DISCLAIMER, build_server

_FORBIDDEN_KEY_FRAGMENTS = ("mid", "average", "avg", "estimate")


def _seed(db_path: str) -> tuple[int, int]:
    """Seed fictional data; returns (testa_id, zed_id)."""
    conn = db.connect(db_path)
    db.init_schema(conn)
    testa = db.get_or_create_politician(conn, "Testa Fixture", "house")
    zed = db.get_or_create_politician(conn, "Zed Placeholder", "senate")
    rows = [
        (testa, "TST", "Test Asset Co. - TEST DATA", "buy", 100_100, 1_500_000,
         "2026-06-01", "2026-06-10", "Self", "https://example.invalid/1", "hash-1"),
        (testa, "ZZZ", "Zed Corp - TEST DATA", "sell", 1_500_100, 5_000_000,
         "2026-06-05", "2026-06-20", None, "https://example.invalid/2", "hash-2"),
        # Open-ended range: max is NULL and must stay NULL end-to-end.
        (testa, "TST", "Test Asset Co. - TEST DATA", "buy", 5_000_000_000, None,
         "2026-05-01", "2026-05-30", "Spouse", "https://example.invalid/3", "hash-3"),
        (zed, "ZZZ", "Zed Corp - TEST DATA", "exchange", 100_100, 1_500_000,
         "2026-06-15", "2026-06-25", "Self", "https://example.invalid/4", "hash-4"),
    ]
    conn.executemany(
        """
        INSERT INTO trades (politician_id, ticker, asset_name, transaction_type,
            amount_min_cents, amount_max_cents, transaction_date, disclosure_date,
            owner, source_url, ingest_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    conn.close()
    return testa, zed


def _assert_no_amount_collapse(payload: Any) -> None:
    """Recursively assert no key anywhere suggests a midpoint/average amount."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            for fragment in _FORBIDDEN_KEY_FRAGMENTS:
                assert fragment not in key.lower(), f"forbidden key: {key}"
            _assert_no_amount_collapse(value)
    elif isinstance(payload, list):
        for item in payload:
            _assert_no_amount_collapse(item)


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        self.db_path = handle.name
        self.testa_id, self.zed_id = _seed(self.db_path)
        self.server = build_server(self.db_path, "127.0.0.1", 0)
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        os.unlink(self.db_path)

    def _request(
        self, path: str, method: str = "GET"
    ) -> tuple[int, dict[str, Any], Message]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            method=method,
            data=b"{}" if method != "GET" else None,
        )
        try:
            with urllib.request.urlopen(request) as response:
                return (
                    response.status,
                    json.loads(response.read().decode("utf-8")),
                    response.headers,
                )
        except urllib.error.HTTPError as exc:
            body = json.loads(exc.read().decode("utf-8"))
            return exc.code, body, exc.headers

    def _get(self, path: str) -> tuple[int, dict[str, Any], Message]:
        return self._request(path)

    def test_trades_sorted_desc_with_disclaimer_days_and_range(self) -> None:
        status, body, _ = self._get("/api/v1/trades")
        self.assertEqual(status, 200)
        self.assertEqual(body["disclaimer"], DISCLAIMER)
        self.assertEqual(body["count"], 4)
        dates = [trade["disclosure_date"] for trade in body["trades"]]
        self.assertEqual(dates, sorted(dates, reverse=True))
        newest = body["trades"][0]
        self.assertEqual(newest["disclosure_date"], "2026-06-25")
        self.assertEqual(newest["days_to_disclosure"], 10)
        self.assertEqual(newest["amount"], {"min_cents": 100_100, "max_cents": 1_500_000})
        self.assertEqual(newest["politician_name"], "Zed Placeholder")
        self.assertEqual(newest["owner"], "Self")
        self.assertEqual(newest["source_url"], "https://example.invalid/4")
        open_ended = [t for t in body["trades"] if t["amount"]["max_cents"] is None]
        self.assertEqual(len(open_ended), 1)
        self.assertEqual(open_ended[0]["amount"]["min_cents"], 5_000_000_000)
        _assert_no_amount_collapse(body)

    def test_filters(self) -> None:
        status, body, _ = self._get("/api/v1/trades?ticker=tst")
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 2)
        self.assertTrue(all(t["ticker"] == "TST" for t in body["trades"]))

        status, body, _ = self._get("/api/v1/trades?type=buy")
        self.assertEqual(body["count"], 2)

        status, body, _ = self._get(f"/api/v1/trades?politician_id={self.zed_id}")
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["trades"][0]["politician_id"], self.zed_id)

        status, body, _ = self._get(
            "/api/v1/trades?date_from=2026-06-10&date_to=2026-06-20"
        )
        self.assertEqual(
            [t["disclosure_date"] for t in body["trades"]],
            ["2026-06-20", "2026-06-10"],
        )

    def test_invalid_type_and_politician_id_rejected(self) -> None:
        status, body, _ = self._get("/api/v1/trades?type=hold")
        self.assertEqual(status, 400)
        self.assertEqual(body["disclaimer"], DISCLAIMER)
        status, _, _ = self._get("/api/v1/trades?politician_id=abc")
        self.assertEqual(status, 400)

    def test_limit_and_offset(self) -> None:
        status, body, _ = self._get("/api/v1/trades?limit=1&offset=1")
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["trades"][0]["disclosure_date"], "2026-06-20")

        status, body, _ = self._get("/api/v1/trades?limit=999")
        self.assertEqual(status, 200)  # clamped to 200, not an error
        self.assertLessEqual(body["count"], 200)

        status, body, _ = self._get("/api/v1/trades?limit=abc")
        self.assertEqual(status, 400)
        self.assertEqual(body["disclaimer"], DISCLAIMER)

        status, _, _ = self._get("/api/v1/trades?offset=-1")
        self.assertEqual(status, 400)

    def test_politicians_list_sorted_with_counts(self) -> None:
        status, body, _ = self._get("/api/v1/politicians")
        self.assertEqual(status, 200)
        self.assertEqual(body["disclaimer"], DISCLAIMER)
        names = [p["full_name"] for p in body["politicians"]]
        self.assertEqual(names, ["Testa Fixture", "Zed Placeholder"])
        counts = {p["full_name"]: p["trade_count"] for p in body["politicians"]}
        self.assertEqual(counts, {"Testa Fixture": 3, "Zed Placeholder": 1})

    def test_politician_aggregates_preserve_open_ended_range(self) -> None:
        status, body, _ = self._get(f"/api/v1/politicians/{self.testa_id}")
        self.assertEqual(status, 200)
        self.assertEqual(body["trade_count"], 3)
        self.assertEqual(
            body["top_tickers"],
            [{"ticker": "TST", "trades": 2}, {"ticker": "ZZZ", "trades": 1}],
        )
        # One open-ended trade → aggregate max is unknowable and must be null.
        self.assertEqual(
            body["total_volume"],
            {"min_cents": 100_100 + 1_500_100 + 5_000_000_000, "max_cents": None},
        )
        _assert_no_amount_collapse(body)

        status, body, _ = self._get(f"/api/v1/politicians/{self.zed_id}")
        self.assertEqual(
            body["total_volume"], {"min_cents": 100_100, "max_cents": 1_500_000}
        )

    def test_unknown_politician_404_with_disclaimer(self) -> None:
        status, body, _ = self._get("/api/v1/politicians/999999")
        self.assertEqual(status, 404)
        self.assertEqual(body["disclaimer"], DISCLAIMER)

    def test_ticker_trades_endpoint(self) -> None:
        status, body, _ = self._get("/api/v1/tickers/zzz/trades")
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 2)
        self.assertTrue(all(t["ticker"] == "ZZZ" for t in body["trades"]))

        status, body, _ = self._get("/api/v1/tickers/NOPE/trades")
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 0)
        self.assertEqual(body["trades"], [])

    def test_post_rejected_405_with_allow_header(self) -> None:
        status, body, headers = self._request("/api/v1/trades", method="POST")
        self.assertEqual(status, 405)
        self.assertEqual(headers["Allow"], "GET")
        self.assertEqual(body["disclaimer"], DISCLAIMER)

    def test_unknown_routes_404_json(self) -> None:
        for path in ("/api/v1/nope", "/trades", "/api/v2/trades", "/"):
            status, body, _ = self._get(path)
            self.assertEqual(status, 404, path)
            self.assertEqual(body["disclaimer"], DISCLAIMER)


if __name__ == "__main__":
    unittest.main()
