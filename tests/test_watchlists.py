"""Tests for WO-2 watchlists. All politicians are FICTIONAL — TEST DATA only."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.request

import _path  # noqa: F401

from pelositracker import db, watchlists
from pelositracker.__main__ import main
from pelositracker.api import build_server


class WatchlistModuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = db.connect(":memory:")
        db.init_schema(self.conn)
        self.testa_id = db.get_or_create_politician(self.conn, "Testa Fixture", "house")

    def tearDown(self) -> None:
        self.conn.close()

    def test_xor_constraint_rejects_both_and_neither(self) -> None:
        watchlists.init_watchlists_schema(self.conn)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO watchlists (kind, politician_id, ticker, created_at)"
                " VALUES ('ticker', ?, 'TST', 'now')",
                (self.testa_id,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO watchlists (kind, created_at) VALUES ('ticker', 'now')"
            )

    def test_round_trip_add_list_remove(self) -> None:
        ticker_id = watchlists.add_ticker(self.conn, "nvda")
        politician_entry_id = watchlists.add_politician(self.conn, self.testa_id)
        entries = watchlists.list_watchlists(self.conn)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["kind"], "ticker")
        self.assertEqual(entries[0]["ticker"], "NVDA")  # upper-cased on insert
        self.assertIsNone(entries[0]["politician_name"])
        self.assertEqual(entries[1]["kind"], "politician")
        self.assertEqual(entries[1]["politician_name"], "Testa Fixture")
        self.assertIsNone(entries[1]["ticker"])
        self.assertTrue(watchlists.remove_watchlist(self.conn, ticker_id))
        self.assertFalse(watchlists.remove_watchlist(self.conn, ticker_id))
        remaining = watchlists.list_watchlists(self.conn)
        self.assertEqual([e["id"] for e in remaining], [politician_entry_id])

    def test_dedupe_case_insensitive(self) -> None:
        watchlists.add_ticker(self.conn, "nvda")
        with self.assertRaises(ValueError):
            watchlists.add_ticker(self.conn, "NVDA")
        watchlists.add_politician(self.conn, self.testa_id)
        with self.assertRaises(ValueError):
            watchlists.add_politician(self.conn, self.testa_id)
        self.assertEqual(len(watchlists.list_watchlists(self.conn)), 2)

    def test_unknown_politician_rejected(self) -> None:
        with self.assertRaises(ValueError):
            watchlists.add_politician(self.conn, 999_999)
        self.assertEqual(watchlists.list_watchlists(self.conn), [])

    def test_blank_ticker_rejected(self) -> None:
        with self.assertRaises(ValueError):
            watchlists.add_ticker(self.conn, "   ")

    def test_list_without_table_reads_nothing_and_creates_nothing(self) -> None:
        bare = sqlite3.connect(":memory:")
        bare.row_factory = sqlite3.Row
        self.assertEqual(watchlists.list_watchlists(bare), [])
        self.assertFalse(watchlists.remove_watchlist(bare, 1))
        row = bare.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'watchlists'"
        ).fetchone()
        self.assertIsNone(row)
        bare.close()

    def test_schema_init_idempotent(self) -> None:
        watchlists.init_watchlists_schema(self.conn)
        watchlists.init_watchlists_schema(self.conn)


class WatchlistCliAndApiTests(unittest.TestCase):
    def setUp(self) -> None:
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        self.db_path = handle.name
        conn = db.connect(self.db_path)
        db.init_schema(conn)
        self.testa_id = db.get_or_create_politician(conn, "Testa Fixture", "house")
        conn.commit()  # get_or_create_politician leaves the commit to its caller
        conn.close()

    def tearDown(self) -> None:
        os.unlink(self.db_path)

    def _run(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = main(["--db", self.db_path, *argv])
        return code, out.getvalue()

    def test_cli_round_trip(self) -> None:
        code, output = self._run("watch", "add", "--ticker", "nvda")
        self.assertEqual(code, 0)
        self.assertIn("added watchlist entry", output)

        code, output = self._run("watch", "add", "--ticker", "NVDA")
        self.assertEqual(code, 1)  # dedupe
        self.assertIn("already watched", output)

        code, _ = self._run("watch", "add", "--politician-id", str(self.testa_id))
        self.assertEqual(code, 0)

        code, output = self._run("watch", "list")
        self.assertEqual(code, 0)
        self.assertIn("NVDA", output)
        self.assertIn("Testa Fixture", output)

        code, _ = self._run("watch", "remove", "1")
        self.assertEqual(code, 0)
        code, output = self._run("watch", "remove", "1")
        self.assertEqual(code, 1)
        self.assertIn("no watchlist entry", output)

    def test_api_watchlists_endpoint_read_only(self) -> None:
        conn = db.connect(self.db_path)
        watchlists.add_ticker(conn, "TST")
        watchlists.add_politician(conn, self.testa_id)
        conn.close()

        server = build_server(self.db_path, "127.0.0.1", 0)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/v1/watchlists"
            ) as response:
                body = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertIn("disclaimer", body)
        self.assertEqual(len(body["watchlists"]), 2)
        self.assertEqual(body["watchlists"][0]["ticker"], "TST")
        self.assertEqual(body["watchlists"][1]["politician_name"], "Testa Fixture")


if __name__ == "__main__":
    unittest.main()
