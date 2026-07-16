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
from typing import Any

from . import api, backtest, clerk, config, db, digest, fetcher, prices, runner, watchlists
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


def _today() -> str:
    from datetime import date

    return date.today().isoformat()


def _prepare_backtest(
    args: argparse.Namespace,
) -> tuple[Any, list[dict[str, Any]], str, prices.PriceCoverage]:
    conn = db.connect(args.db)
    db.init_schema(conn)
    end_date = _today()
    trades = backtest.load_trades(
        conn,
        args.from_date,
        politician_id=getattr(args, "politician_id", None),
        chamber=getattr(args, "chamber", None),
    )
    tickers = sorted(
        {str(t["ticker"]).upper() for t in trades if t["ticker"]}
        | {backtest.BENCHMARK_TICKER}
    )
    print(f"[prices] ensuring coverage for {len(tickers)} tickers to {end_date}")
    coverage = prices.ensure_prices(conn, tickers, end_date)
    print(
        f"[prices] fetched={coverage.fetched} cached={coverage.cached}"
        f" unpriced={coverage.unpriced}"
    )
    return conn, trades, end_date, coverage


def _cmd_backtest(args: argparse.Namespace) -> int:
    conn, trades, end_date, coverage = _prepare_backtest(args)
    try:
        outcome = backtest.run_backtest(
            conn,
            trades,
            args.from_date,
            end_date,
            cost_bps=args.cost_bps,
            hold_days=args.hold,
        )
        report = backtest.format_report(outcome)
        print(report)
        reports_dir = Path("reports")
        reports_dir.mkdir(parents=True, exist_ok=True)
        report_path = reports_dir / f"backtest-{end_date}.md"
        suffix = f"\n_Run: exit={outcome.exit_mode} cost={outcome.cost_bps}bps_\n\n"
        with report_path.open("a", encoding="utf-8") as handle:
            handle.write(report + suffix)
        print(f"report appended to {report_path}")
        return 0
    finally:
        conn.close()


def _cmd_leaderboard(args: argparse.Namespace) -> int:
    conn, trades, end_date, coverage = _prepare_backtest(args)
    try:
        rows = backtest.build_leaderboard(
            conn, trades, args.from_date, end_date, cost_bps=args.cost_bps
        )
        print(f"> {backtest.MANDATORY_REPORT_HEADER}\n")
        print(
            f"Leaderboard {args.from_date} → {end_date} (mirror-sells,"
            f" {args.cost_bps} bps, min {backtest.LEADERBOARD_MIN_PRICED_TRADES}"
            " priced trades, sorted by PESSIMISTIC excess bound)"
        )
        print(f"unpriced tickers excluded: {coverage.unpriced}")
        header = f"{'#':>3}  {'politician':<32} {'trades':>6}  {'excess band vs SPY':<24} {'hit rate band':<20}"
        print(header)
        print("-" * len(header))
        for rank, row in enumerate(rows, start=1):
            excess = f"[{row.excess_low * 100:+.1f}%, {row.excess_high * 100:+.1f}%]"
            hit = f"[{row.hit_rate_low * 100:.0f}%, {row.hit_rate_high * 100:.0f}%]"
            print(
                f"{rank:>3}  {row.politician_name[:32]:<32} {row.priced_trades:>6}"
                f"  {excess:<24} {hit:<20}"
            )
        if not rows:
            print("(no politician meets the minimum priced-trade threshold)")
        return 0
    finally:
        conn.close()


def _cmd_run(args: argparse.Namespace) -> int:
    interval = runner.resolve_interval_hours(args.interval_hours)
    return runner.run_loop(args.db, interval, once=args.once)


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

    run_parser = subparsers.add_parser(
        "run", help="Scheduled runner: ingest senate + house then digest, on a loop"
    )
    run_parser.add_argument(
        "--interval-hours",
        type=float,
        default=None,
        help="Hours between cycles (default env PT_RUN_INTERVAL_HOURS or 6)",
    )
    run_parser.add_argument(
        "--once", action="store_true", help="Run a single cycle and exit"
    )
    run_parser.set_defaults(func=_cmd_run)

    backtest_parser = subparsers.add_parser(
        "backtest", help="Hypothetical copy-trading backtest (banded, vs SPY)"
    )
    backtest_parser.add_argument("--from", dest="from_date", required=True)
    exit_group = backtest_parser.add_mutually_exclusive_group()
    exit_group.add_argument("--hold", type=int, default=None, metavar="DAYS")
    exit_group.add_argument(
        "--mirror-sells", action="store_true", help="Exit on politician sells (default)"
    )
    backtest_parser.add_argument("--politician-id", type=int, default=None)
    backtest_parser.add_argument("--chamber", choices=("house", "senate"), default=None)
    backtest_parser.add_argument(
        "--cost-bps", type=int, default=backtest.DEFAULT_COST_BPS
    )
    backtest_parser.set_defaults(func=_cmd_backtest)

    leaderboard_parser = subparsers.add_parser(
        "leaderboard", help="Per-politician excess-vs-SPY bands (pessimistic order)"
    )
    leaderboard_parser.add_argument("--from", dest="from_date", required=True)
    leaderboard_parser.add_argument(
        "--cost-bps", type=int, default=backtest.DEFAULT_COST_BPS
    )
    leaderboard_parser.set_defaults(func=_cmd_leaderboard)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
