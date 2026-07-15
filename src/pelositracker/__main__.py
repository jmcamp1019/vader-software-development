"""Command-line interface.

Usage:
    python -m pelositracker ingest --source fixtures [--db pelositracker.db]
    python -m pelositracker ingest --source house
    python -m pelositracker ingest --source senate
    python -m pelositracker stats [--db pelositracker.db]
    python -m pelositracker serve
"""
from __future__ import annotations

import argparse
import sys
import urllib.error
from pathlib import Path

from . import api, clerk, config, db, digest, fetcher, watchlists
from .api import DISCLAIMER
from .pipeline import ingest_house_records, ingest_records, ingest_senate_filings


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


def _cmd_watch(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    try:
        db.init_schema(conn)
        if args.watch_command == "add":
            try:
                if args.ticker is not None:
                    entry_id = watchlists.add_ticker(conn, args.ticker)
                else:
                    entry_id = watchlists.add_politician(conn, args.politician_id)
            except ValueError as exc:
                print(exc, file=sys.stderr)
                return 1
            print(f"added watchlist entry {entry_id}")
        elif args.watch_command == "list":
            entries = watchlists.list_watchlists(conn)
            if not entries:
                print("watchlist is empty")
            for entry in entries:
                if entry["kind"] == "ticker":
                    target = str(entry["ticker"])
                else:
                    target = f"{entry['politician_name']} (id {entry['politician_id']})"
                print(
                    f"{entry['id']:>4}  {entry['kind']:<10} {target}"
                    f"  added {entry['created_at']}"
                )
        else:  # remove
            if not watchlists.remove_watchlist(conn, args.watch_id):
                print(f"no watchlist entry {args.watch_id}", file=sys.stderr)
                return 1
            print(f"removed watchlist entry {args.watch_id}")
        return 0
    finally:
        conn.close()


def _cmd_digest(args: argparse.Namespace) -> int:
    conn = db.connect(args.db)
    try:
        db.init_schema(conn)
        result = digest.run_digest(conn, output_dir=args.output_dir)
        print(result.digest_text)
        if result.output_path is not None:
            print(f"digest written to {result.output_path}")
        if result.emailed:
            print("digest emailed")
        return 0
    finally:
        conn.close()


def _cmd_serve(args: argparse.Namespace) -> int:
    api.serve(args.db)
    return 0


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

    serve = subparsers.add_parser("serve", help="Run the local read-only query API")
    serve.set_defaults(func=_cmd_serve)

    watch = subparsers.add_parser("watch", help="Manage watchlist entries")
    watch_sub = watch.add_subparsers(dest="watch_command", required=True)
    watch_add = watch_sub.add_parser("add", help="Watch a ticker or politician")
    add_target = watch_add.add_mutually_exclusive_group(required=True)
    add_target.add_argument("--ticker")
    add_target.add_argument("--politician-id", type=int, dest="politician_id")
    watch_sub.add_parser("list", help="List watchlist entries")
    watch_remove = watch_sub.add_parser("remove", help="Remove a watchlist entry")
    watch_remove.add_argument("watch_id", type=int)
    watch.set_defaults(func=_cmd_watch)

    digest_parser = subparsers.add_parser(
        "digest", help="Print and record new watched trades since the last run"
    )
    digest_parser.add_argument("--output-dir", default="digests")
    digest_parser.set_defaults(func=_cmd_digest)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
