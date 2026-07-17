"""Politician canonical-identity tests. TEST DATA — fictional politicians only."""
from __future__ import annotations

import unittest

import _path  # noqa: F401

from pelositracker import db, watchlists
from pelositracker.normalizer import canonical_politician_name


class CanonicalNameTests(unittest.TestCase):
    def test_honorifics_dropped(self) -> None:
        self.assertEqual(
            canonical_politician_name("Testa Ann Mrs Fixture"),
            "testa ann fixture",
        )
        self.assertEqual(
            canonical_politician_name("Dr. Zed Placeholder"), "zed placeholder"
        )

    def test_adjacent_duplicate_tokens_collapse(self) -> None:
        self.assertEqual(
            canonical_politician_name("Zed Zed Placeholder"), "zed placeholder"
        )

    def test_punctuation_case_whitespace(self) -> None:
        self.assertEqual(
            canonical_politician_name("  Q.  ZED  Placeholder, "),
            "q zed placeholder",
        )

    def test_initials_are_kept(self) -> None:
        self.assertNotEqual(
            canonical_politician_name("Q. Zed Placeholder"),
            canonical_politician_name("Zed Placeholder"),
        )


def _add_trade(conn, politician_id: int, ingest_hash: str) -> None:
    conn.execute(
        """
        INSERT INTO trades (
            politician_id, ticker, asset_name, transaction_type,
            amount_min_cents, amount_max_cents, transaction_date,
            disclosure_date, owner, source_url, ingest_hash, provenance
        ) VALUES (?, 'TST', 'Test Asset', 'buy', 100100, 1500000,
                  '2026-01-02', '2026-01-05', NULL, 'https://example.test/1',
                  ?, 'fixtures')
        """,
        (politician_id, ingest_hash),
    )


class IdentityMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = db.connect(":memory:")
        db.init_schema(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def _raw_insert(self, full_name: str, chamber: str) -> int:
        cursor = self.conn.execute(
            "INSERT INTO politicians (full_name, chamber) VALUES (?, ?)",
            (full_name, chamber),
        )
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)

    def test_honorific_variant_merges_to_earliest_row(self) -> None:
        keep = self._raw_insert("Testa Ann Fixture", "house")
        dupe = self._raw_insert("Testa Ann Mrs Fixture", "house")
        _add_trade(self.conn, keep, "h1")
        _add_trade(self.conn, dupe, "h2")
        db.init_schema(self.conn)  # migration runs
        self.assertEqual(db.politician_count(self.conn), 1)
        rows = self.conn.execute(
            "SELECT COUNT(*) AS n FROM trades WHERE politician_id = ?", (keep,)
        ).fetchone()
        self.assertEqual(rows["n"], 2)
        self.assertIsNone(
            self.conn.execute(
                "SELECT 1 FROM politicians WHERE id = ?", (dupe,)
            ).fetchone()
        )

    def test_initials_variant_merges_when_unambiguous(self) -> None:
        a = self._raw_insert("Zed Zed Placeholder", "senate")  # -> zed placeholder
        b = self._raw_insert("Q. Zed Placeholder", "senate")  # -> q zed placeholder
        _add_trade(self.conn, a, "s1")
        _add_trade(self.conn, b, "s2")
        db.init_schema(self.conn)
        self.assertEqual(db.politician_count(self.conn), 1)
        rows = self.conn.execute(
            "SELECT COUNT(*) AS n FROM trades WHERE politician_id = ?", (a,)
        ).fetchone()
        self.assertEqual(rows["n"], 2)

    def test_conflicting_initials_never_merge(self) -> None:
        self._raw_insert("Alpha A. Beta", "house")
        self._raw_insert("Alpha B. Beta", "house")
        db.init_schema(self.conn)
        self.assertEqual(db.politician_count(self.conn), 2)

    def test_bare_surnames_never_merge(self) -> None:
        self._raw_insert("J. Fixture", "house")
        self._raw_insert("Fixture", "house")
        db.init_schema(self.conn)
        self.assertEqual(db.politician_count(self.conn), 2)

    def test_chambers_stay_separate(self) -> None:
        self._raw_insert("Testa Ann Fixture", "house")
        self._raw_insert("Testa Ann Fixture", "senate")
        db.init_schema(self.conn)
        self.assertEqual(db.politician_count(self.conn), 2)

    def test_migration_idempotent(self) -> None:
        keep = self._raw_insert("Testa Ann Fixture", "house")
        self._raw_insert("Testa Ann Mrs Fixture", "house")
        db.init_schema(self.conn)
        db.init_schema(self.conn)
        db.init_schema(self.conn)
        self.assertEqual(db.politician_count(self.conn), 1)
        alias_rows = self.conn.execute(
            "SELECT COUNT(*) AS n FROM politician_aliases WHERE politician_id = ?",
            (keep,),
        ).fetchone()
        self.assertEqual(alias_rows["n"], 1)

    def test_get_or_create_resolves_variants_to_survivor(self) -> None:
        first = db.get_or_create_politician(self.conn, "Testa Ann Fixture", "house")
        again = db.get_or_create_politician(
            self.conn, "Testa Ann Mrs Fixture", "house"
        )
        shouty = db.get_or_create_politician(
            self.conn, "TESTA ANN FIXTURE", "house"
        )
        self.assertEqual(first, again)
        self.assertEqual(first, shouty)
        self.assertEqual(db.politician_count(self.conn), 1)

    def test_watchlist_repointed_on_merge(self) -> None:
        keep = self._raw_insert("Testa Ann Fixture", "house")
        dupe = self._raw_insert("Testa Ann Mrs Fixture", "house")
        self.conn.commit()
        watchlists.init_watchlists_schema(self.conn)
        watchlists.add_politician(self.conn, dupe)
        db.init_schema(self.conn)
        row = self.conn.execute(
            "SELECT politician_id FROM watchlists WHERE kind = 'politician'"
        ).fetchone()
        self.assertEqual(int(row["politician_id"]), keep)

    def test_watchlist_collision_deduped_on_merge(self) -> None:
        keep = self._raw_insert("Testa Ann Fixture", "house")
        dupe = self._raw_insert("Testa Ann Mrs Fixture", "house")
        self.conn.commit()
        watchlists.init_watchlists_schema(self.conn)
        watchlists.add_politician(self.conn, keep)
        watchlists.add_politician(self.conn, dupe)
        db.init_schema(self.conn)
        rows = self.conn.execute(
            "SELECT COUNT(*) AS n FROM watchlists WHERE kind = 'politician'"
        ).fetchone()
        self.assertEqual(rows["n"], 1)


if __name__ == "__main__":
    unittest.main()
