"""Command-line interface.

Usage:
    python -m pelositracker ingest --source fixtures [--db pelositracker.db]
    python -m pelositracker ingest --source house
    python -m pelositracker ingest --source senate
    python -m pelositracker stats [--db pelositracker.db]
"""
from __future__ import annotations

import argparse
import sys
import urllib.error
from pathlib import Path

from . import clerk, config, db, fetcher
from .pipeline import ingest_house_records, ingest_records, ingest_senate_filings

DISCLAIMER = (
    "PelosiTracker displays public STOCK Act disclosure data. Filings may lag "
    "trades by up to 45 days and report amount ranges, not exact values. "
    "Informational only — not investment advice."
)


def _cmd_ingest(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    try:
        if args.source == "fixtures":
            fixture_dir = Path(args.fixture_dir)
            batches = [
                ("house", fetcher.load_fixture(fixture_dir / "house_sample.json")),
                ("senate", fetcher.load_fixture(fixture_dir / "senate_sample.json")),
            ]
            for chamber, records in batches:
                print(
                    ingest_records(
                        conn, records, chamber, config.PROVENANCE_FIXTURES
                    ).summary()
                )
        elif args.source == "house":
            records = fetcher.fetch_json(config.HOUSE_ALL_TRANSACTIONS_URL)
            # ADR-001 integrity anchor: fetch the official Clerk filing index;
            # fail closed (insert nothing) if it cannot be retrieved.
            try:
                clerk_doc_ids = clerk.fetch_doc_ids_for_records(records)
            except (ValueError, OSError) as exc:  # URLError is an OSError
                print(
                    "house ingest aborted: official Clerk index unavailable "
                    f"({exc}); failing closed, nothing inserted",
                    file=sys.stderr,
                )
                return 1
            stats = ingest_house_records(conn, records, clerk_doc_ids)
            print(stats.summary())
            if stats.quarantined:
                print(
                    f"quarantined {stats.quarantined} mirror trade(s) with no "
                    "matching filing in the official House Clerk index (not inserted)",
                    file=sys.stderr,
                )
        else:  # senate
            filings = fetcher.fetch_json(config.SENATE_DAILY_SUMMARIES_URL)
            print(
                ingest_senate_filings(
                    conn, filings, config.PROVENANCE_SENATE_GITHUB
                ).summary()
            )

        print(f"db={args.db} trades={db.trade_count(conn)} politicians={db.politician_count(conn)}")
        print(DISCLAIMER)
        return 0
    finally:
        conn.close()


def _cmd_stats(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    try:
        db.init_schema(conn)
        print(f"trades={db.trade_count(conn)} politicians={db.politician_count(conn)}")
        for ticker, count in db.top_tickers(conn):
            print(f"  {ticker:<8} {count}")
        print(DISCLAIMER)
        return 0
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pelositracker")
    parser.add_argument("--db", default=config.DEFAULT_DB_PATH, help="SQLite database path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest = subparsers.add_parser("ingest", help="Fetch and store disclosure data")
    ingest.add_argument("--source", choices=("fixtures", "house", "senate"), required=True)
    ingest.add_argument("--fixture-dir", default="tests/fixtures")
    ingest.set_defaults(func=_cmd_ingest)

    stats = subparsers.add_parser("stats", help="Show database summary")
    stats.set_defaults(func=_cmd_stats)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
