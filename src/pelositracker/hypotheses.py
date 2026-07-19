"""WO-8 pre-registered hypothesis battery.

This module is intentionally a thin filter/orchestration layer over the WO-7
engine.  It does not alter entry, sizing, cost, exit, or benchmark semantics.
The real holdout is guarded by a write-once report path and may only be run
after the committed train artifact exists.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import subprocess
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import backtest

TRAIN_START = "2024-01-01"
TRAIN_END = "2025-06-30"
HOLDOUT_START = "2025-07-01"
HOLD_DAYS = 90
COST_BPS = 20
FAST_FILE_DAYS = 15
CONVICTION_MIN_CENTS = 5_000_000  # $50,000; compare to disclosed floor only.
CONSENSUS_MEMBERS = 3
CONSENSUS_WINDOW_DAYS = 30
H6_MIN_TRAIN_TRADES = 10
H6_HOLDOUT_MIN_TRADES = 30
MATERIALITY_PP = 2.0
TRAIN_ARTIFACT = "hypothesis-battery-train-2025-06-30.json"
HOLDOUT_RESERVATION = "hypothesis-battery-holdout-once.lock"


@dataclass(frozen=True)
class HypothesisResult:
    key: str
    label: str
    priced_trades: int
    excess_low: float
    excess_high: float
    total_low: float
    total_high: float
    benchmark_low: float
    benchmark_high: float
    hit_low: float
    hit_high: float
    unpriced_trades: int


@dataclass(frozen=True)
class BatteryArtifact:
    phase: str
    disclosure_start: str
    disclosure_end: str
    scoring_end: str
    results: tuple[HypothesisResult, ...]
    h6_cohort_ids: tuple[int, ...]
    h6_cohort_names: tuple[str, ...]


def load_window_trades(
    conn: sqlite3.Connection, start: str, end: str
) -> list[dict[str, Any]]:
    """Load only disclosures inside the inclusive window (boundary guard)."""
    rows = conn.execute(
        """
        SELECT t.id, t.politician_id, p.full_name AS politician_name, p.chamber,
               t.ticker, t.transaction_type, t.amount_min_cents,
               t.amount_max_cents, t.transaction_date, t.disclosure_date,
               t.source_url
        FROM trades t JOIN politicians p ON p.id = t.politician_id
        WHERE t.disclosure_date >= ? AND t.disclosure_date <= ?
          AND t.transaction_type IN ('buy', 'sell')
        ORDER BY t.disclosure_date ASC, t.id ASC
        """,
        (start, end),
    ).fetchall()
    return [dict(row) for row in rows]


def purchases_only(trades: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [trade for trade in trades if trade["transaction_type"] == "buy"]


def fast_filers(trades: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Buys disclosed 0..15 calendar days after transaction."""
    selected: list[Mapping[str, Any]] = []
    for trade in purchases_only(trades):
        try:
            lag = (
                date.fromisoformat(str(trade["disclosure_date"]))
                - date.fromisoformat(str(trade["transaction_date"]))
            ).days
        except (TypeError, ValueError):
            continue
        if 0 <= lag <= FAST_FILE_DAYS:
            selected.append(trade)
    return selected


def conviction_size(trades: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Buys whose disclosed minimum is at least $50,000; no midpoint used."""
    return [
        trade
        for trade in purchases_only(trades)
        if int(trade["amount_min_cents"]) >= CONVICTION_MIN_CENTS
    ]


def consensus_signals(
    trades: Sequence[Mapping[str, Any]],
    signal_start: str | None = None,
    signal_end: str | None = None,
) -> list[Mapping[str, Any]]:
    """Enter exactly when a ticker crosses 3 distinct buyers in trailing 30d.

    The inclusive 30-calendar-day window is [disclosure-29d, disclosure].  A
    ticker cannot signal again until its rolling distinct-member count first
    falls below three, preventing every later disclosure in one consensus
    episode from becoming another entry.
    """
    windows: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    active: dict[str, bool] = defaultdict(bool)
    selected: list[Mapping[str, Any]] = []
    for trade in purchases_only(trades):
        ticker_raw = trade.get("ticker")
        if ticker_raw is None or not str(ticker_raw).strip():
            continue
        ticker = str(ticker_raw).upper()
        current = date.fromisoformat(str(trade["disclosure_date"]))
        cutoff = current - timedelta(days=CONSENSUS_WINDOW_DAYS - 1)
        window = [
            prior
            for prior in windows[ticker]
            if date.fromisoformat(str(prior["disclosure_date"])) >= cutoff
        ]
        before = {int(prior["politician_id"]) for prior in window}
        if len(before) < CONSENSUS_MEMBERS:
            active[ticker] = False
        window.append(trade)
        after = {int(prior["politician_id"]) for prior in window}
        if not active[ticker] and len(after) >= CONSENSUS_MEMBERS:
            disclosed = str(trade["disclosure_date"])
            if (signal_start is None or disclosed >= signal_start) and (
                signal_end is None or disclosed <= signal_end
            ):
                selected.append(trade)
            active[ticker] = True
        windows[ticker] = window
    return selected


def chamber_purchases(
    trades: Sequence[Mapping[str, Any]], chamber: str
) -> list[Mapping[str, Any]]:
    return [trade for trade in purchases_only(trades) if trade["chamber"] == chamber]


def _score(
    conn: sqlite3.Connection,
    key: str,
    label: str,
    trades: Sequence[Mapping[str, Any]],
    disclosure_start: str,
    scoring_end: str,
) -> HypothesisResult:
    outcome = backtest.run_backtest(
        conn,
        trades,
        disclosure_start,
        scoring_end,
        cost_bps=COST_BPS,
        hold_days=HOLD_DAYS,
        realized_only=True,
    )
    excess = outcome.band("excess_return")
    total = outcome.band("total_return")
    benchmark = outcome.band("benchmark_return")
    hit = outcome.band("hit_rate")
    return HypothesisResult(
        key=key,
        label=label,
        priced_trades=outcome.runs["min"].positions,
        excess_low=excess[0],
        excess_high=excess[1],
        total_low=total[0],
        total_high=total[1],
        benchmark_low=benchmark[0],
        benchmark_high=benchmark[1],
        hit_low=hit[0],
        hit_high=hit[1],
        unpriced_trades=outcome.exclusions.get("unpriced_ticker", 0),
    )


def select_h6_cohort(
    conn: sqlite3.Connection,
    train_trades: Sequence[Mapping[str, Any]],
    scoring_end: str,
) -> tuple[int, ...]:
    """Top decile by TRAIN-only realized hold-90 lower excess bound."""
    grouped: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for trade in purchases_only(train_trades):
        grouped[int(trade["politician_id"])].append(trade)
    eligible: list[tuple[float, int]] = []
    for pid, own in grouped.items():
        result = _score(conn, "H6-rank", "train rank", own, TRAIN_START, scoring_end)
        if result.priced_trades >= H6_MIN_TRAIN_TRADES:
            eligible.append((result.excess_low, pid))
    eligible.sort(key=lambda item: (-item[0], item[1]))
    count = math.ceil(len(eligible) / 10) if eligible else 0
    return tuple(pid for _, pid in eligible[:count])


def _battery_results(
    conn: sqlite3.Connection,
    trades: Sequence[Mapping[str, Any]],
    disclosure_start: str,
    scoring_end: str,
    h6_cohort: Sequence[int],
    consensus_context: Sequence[Mapping[str, Any]],
    disclosure_end: str,
) -> tuple[HypothesisResult, ...]:
    cohort = set(h6_cohort)
    specs: tuple[tuple[str, str, list[Mapping[str, Any]]], ...] = (
        ("H1", "purchases-only", purchases_only(trades)),
        ("H2", "fast-filers (<=15d)", fast_filers(trades)),
        ("H3", "conviction-size (min >= $50,000)", conviction_size(trades)),
        (
            "H4",
            "consensus (3 members / 30d)",
            consensus_signals(
                consensus_context,
                signal_start=disclosure_start,
                signal_end=disclosure_end,
            ),
        ),
        ("H5-house", "chamber-split: house", chamber_purchases(trades, "house")),
        ("H5-senate", "chamber-split: senate", chamber_purchases(trades, "senate")),
        (
            "H6",
            "train top-decile skill cohort",
            [trade for trade in purchases_only(trades) if int(trade["politician_id"]) in cohort],
        ),
    )
    return tuple(
        _score(conn, key, label, selected, disclosure_start, scoring_end)
        for key, label, selected in specs
    )


def train_scoring_end() -> str:
    """Allow the last train disclosure to complete its fixed 90d holding."""
    return (date.fromisoformat(TRAIN_END) + timedelta(days=HOLD_DAYS + 10)).isoformat()


def latest_price_date(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT MAX(date) AS latest FROM prices WHERE ticker = ?",
        (backtest.BENCHMARK_TICKER,),
    ).fetchone()
    if row is None or row["latest"] is None:
        raise ValueError("SPY benchmark prices missing")
    return str(row["latest"])


def run_train(conn: sqlite3.Connection) -> BatteryArtifact:
    trades = load_window_trades(conn, TRAIN_START, TRAIN_END)
    context_start = (
        date.fromisoformat(TRAIN_START) - timedelta(days=CONSENSUS_WINDOW_DAYS - 1)
    ).isoformat()
    consensus_context = load_window_trades(conn, context_start, TRAIN_END)
    scoring_end = train_scoring_end()
    cohort = select_h6_cohort(conn, trades, scoring_end)
    names = {
        int(trade["politician_id"]): str(trade["politician_name"])
        for trade in trades
    }
    return BatteryArtifact(
        phase="train",
        disclosure_start=TRAIN_START,
        disclosure_end=TRAIN_END,
        scoring_end=scoring_end,
        results=_battery_results(
            conn,
            trades,
            TRAIN_START,
            scoring_end,
            cohort,
            consensus_context,
            TRAIN_END,
        ),
        h6_cohort_ids=cohort,
        h6_cohort_names=tuple(names[pid] for pid in cohort),
    )


def run_holdout(
    conn: sqlite3.Connection, train: BatteryArtifact, latest: str
) -> BatteryArtifact:
    if train.phase != "train" or train.disclosure_end != TRAIN_END:
        raise ValueError("invalid committed train artifact")
    trades = load_window_trades(conn, HOLDOUT_START, latest)
    context_start = (
        date.fromisoformat(HOLDOUT_START)
        - timedelta(days=CONSENSUS_WINDOW_DAYS - 1)
    ).isoformat()
    consensus_context = load_window_trades(conn, context_start, latest)
    return BatteryArtifact(
        phase="holdout",
        disclosure_start=HOLDOUT_START,
        disclosure_end=latest,
        scoring_end=latest,
        results=_battery_results(
            conn,
            trades,
            HOLDOUT_START,
            latest,
            train.h6_cohort_ids,
            consensus_context,
            latest,
        ),
        h6_cohort_ids=train.h6_cohort_ids,
        h6_cohort_names=train.h6_cohort_names,
    )


def write_artifact(path: Path, artifact: BatteryArtifact) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(artifact)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def read_artifact(path: Path) -> BatteryArtifact:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return BatteryArtifact(
        phase=str(payload["phase"]),
        disclosure_start=str(payload["disclosure_start"]),
        disclosure_end=str(payload["disclosure_end"]),
        scoring_end=str(payload["scoring_end"]),
        results=tuple(HypothesisResult(**row) for row in payload["results"]),
        h6_cohort_ids=tuple(int(pid) for pid in payload["h6_cohort_ids"]),
        h6_cohort_names=tuple(str(name) for name in payload["h6_cohort_names"]),
    )


def artifact_is_committed(path: Path, repo_root: Path | None = None) -> bool:
    """True only when HEAD contains byte-for-byte the artifact being read."""
    root = (repo_root or Path.cwd()).resolve()
    try:
        relative = path.resolve().relative_to(root).as_posix()
    except ValueError:
        return False
    result = subprocess.run(
        ["git", "show", f"HEAD:{relative}"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout == path.read_bytes()


def reserve_holdout(output_dir: Path, latest: str) -> Path:
    """Atomically and permanently reserve the single real holdout evaluation."""
    output_dir.mkdir(parents=True, exist_ok=True)
    marker = output_dir / HOLDOUT_RESERVATION
    try:
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise ValueError("holdout already reserved; exactly-once guard fired") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(f"holdout evaluation reserved through {latest}\n")
    return marker


def passes(train: HypothesisResult, holdout: HypothesisResult) -> bool:
    minimum = H6_HOLDOUT_MIN_TRADES if train.key == "H6" else 100
    return (
        train.excess_low > 0.0
        and holdout.excess_low > 0.0
        and holdout.excess_low * 100 >= MATERIALITY_PP
        and train.priced_trades >= 100
        and holdout.priced_trades >= minimum
    )


def format_train_report(artifact: BatteryArtifact) -> str:
    lines = [
        f"> {backtest.MANDATORY_REPORT_HEADER}",
        "",
        "# WO-8 hypothesis battery — TRAIN results only",
        "",
        f"Disclosure window: {artifact.disclosure_start} to {artifact.disclosure_end}",
        f"Scoring: forced realized hold-{HOLD_DAYS}, {COST_BPS} bps, vs SPY",
        "HOLDOUT HAS NOT BEEN EVALUATED.",
        "",
        "| Hypothesis | Priced trades | Excess-vs-SPY band | Hit-rate band | Unpriced |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in artifact.results:
        lines.append(
            f"| {row.key} {row.label} | {row.priced_trades} |"
            f" [{row.excess_low*100:+.2f}%, {row.excess_high*100:+.2f}%] |"
            f" [{row.hit_low*100:.2f}%, {row.hit_high*100:.2f}%] | {row.unpriced_trades} |"
        )
    lines += ["", "H6 frozen train cohort: " + ", ".join(artifact.h6_cohort_names), ""]
    return "\n".join(lines)


def format_full_report(train: BatteryArtifact, holdout: BatteryArtifact) -> str:
    train_rows = {row.key: row for row in train.results}
    holdout_rows = {row.key: row for row in holdout.results}
    lines = [
        f"> {backtest.MANDATORY_REPORT_HEADER}",
        "",
        "# WO-8 pre-registered hypothesis battery — FINAL",
        "",
        f"Train disclosures: {train.disclosure_start} to {train.disclosure_end}",
        f"Holdout disclosures (evaluated once): {holdout.disclosure_start} to {holdout.disclosure_end}",
        f"Scoring: forced realized hold-{HOLD_DAYS}, {COST_BPS} bps, next-close entry, SPY shadow",
        "",
        "| Hypothesis | Train trades | Train excess band | Holdout trades | Holdout excess band | Verdict |",
        "|---|---:|---:|---:|---:|---|",
    ]
    row_passes: dict[str, bool] = {}
    for key in train_rows:
        before = train_rows[key]
        after = holdout_rows[key]
        passed = passes(before, after)
        row_passes[key] = passed
        lines.append(
            f"| {key} {before.label} | {before.priced_trades} |"
            f" [{before.excess_low*100:+.2f}%, {before.excess_high*100:+.2f}%] |"
            f" {after.priced_trades} |"
            f" [{after.excess_low*100:+.2f}%, {after.excess_high*100:+.2f}%] |"
            f" {'PASS' if passed else 'FAIL'} |"
        )
    pass_count = sum(row_passes.values())
    lines += [
        "",
        f"Passing scored rows: {pass_count} of 7"
        " (six hypotheses; H5 has separately judged House and Senate rows).",
        "",
        "H6 frozen train cohort: " + ", ".join(train.h6_cohort_names),
        "",
    ]
    return "\n".join(lines)
