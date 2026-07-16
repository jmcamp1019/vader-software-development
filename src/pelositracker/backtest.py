"""WO-7 backtest engine: copy-trading thesis, honest defaults, banded results.

Fixed semantics (docs/batch4_orders.md):
- Signal date is the DISCLOSURE date; entry is the next trading day's close
  strictly AFTER disclosure (no look-ahead — we cannot act before the filing
  was public).
- Every backtest runs twice, sized at amount_min and at amount_max, and every
  result is a BAND. Open-ended maxes cap at the floor of the next official
  STOCK Act band (conservative).
- A round-trip cost in basis points applies to every simulated position.
- The benchmark is an identical-cash-flow shadow portfolio in SPY; results
  are always reported against it, never bare.

Money in: integer cents. Derived analytics (returns, shares) are floats.
"""
from __future__ import annotations

import sqlite3
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import date as date_type, timedelta
from typing import Any, Mapping, Sequence

DEFAULT_COST_BPS = 20
BENCHMARK_TICKER = "SPY"
LEADERBOARD_MIN_PRICED_TRADES = 10

MANDATORY_REPORT_HEADER = (
    "HYPOTHETICAL BACKTEST. Results do not represent actual trading, ignore "
    "liquidity and slippage beyond the modeled cost, and past performance does "
    "not indicate future results. This is not investment advice."
)

# Official STOCK Act band floors, in cents. Used to cap open-ended maxes at
# the floor of the next band above the reported minimum.
STOCK_ACT_BAND_FLOORS_CENTS: tuple[int, ...] = (
    100_100,          # $1,001
    1_500_100,        # $15,001
    5_000_100,        # $50,001
    10_000_100,       # $100,001
    25_000_100,       # $250,001
    50_000_100,       # $500,001
    100_000_100,      # $1,000,001
    500_000_100,      # $5,000,001
    2_500_000_100,    # $25,000,001
    5_000_000_100,    # $50,000,001
)


def capped_max_cents(min_cents: int, max_cents: int | None) -> int:
    """Sizing max for a range; open-ended maxes cap at the next band floor.

    When the minimum already sits at/above the top official band, the cap is
    the minimum itself — never an invented larger number.
    """
    if max_cents is not None:
        return max_cents
    for floor in STOCK_ACT_BAND_FLOORS_CENTS:
        if floor > min_cents:
            return floor
    return min_cents


class Series:
    """A sorted daily close series with binary-search date lookups."""

    def __init__(self, rows: Sequence[tuple[str, int]]) -> None:
        ordered = sorted(rows)
        self.dates: list[str] = [row[0] for row in ordered]
        self.closes: list[int] = [row[1] for row in ordered]

    def first_close_after(self, day: str) -> tuple[str, int] | None:
        """Strictly after `day` — the no-look-ahead entry rule."""
        index = bisect_right(self.dates, day)
        if index >= len(self.dates):
            return None
        return self.dates[index], self.closes[index]

    def first_close_on_or_after(self, day: str) -> tuple[str, int] | None:
        index = bisect_left(self.dates, day)
        if index >= len(self.dates):
            return None
        return self.dates[index], self.closes[index]

    def close_on_or_before(self, day: str) -> tuple[str, int] | None:
        index = bisect_right(self.dates, day)
        if index == 0:
            return None
        return self.dates[index - 1], self.closes[index - 1]


@dataclass
class Position:
    ticker: str
    politician_id: int
    invested_cents: int
    entry_date: str
    entry_close_cents: int
    shares: float
    exit_date: str | None = None
    final_value_cents: float = 0.0
    closed: bool = False


@dataclass
class RunMetrics:
    sizing: str
    positions: int
    closed_positions: int
    invested_cents: int
    final_value_cents: int
    total_return: float
    cagr: float
    max_drawdown: float
    hit_rate: float
    benchmark_return: float
    excess_return: float


@dataclass
class BacktestOutcome:
    from_date: str
    end_date: str
    exit_mode: str
    cost_bps: int
    runs: dict[str, RunMetrics] = field(default_factory=dict)
    exclusions: dict[str, int] = field(default_factory=dict)
    unpriced_tickers: list[str] = field(default_factory=list)

    def band(self, metric: str) -> tuple[float, float]:
        values = sorted(getattr(run, metric) for run in self.runs.values())
        return values[0], values[-1]


def _sized_cents(trade: Mapping[str, Any], sizing: str) -> int:
    min_cents = int(trade["amount_min_cents"])
    if sizing == "min":
        return min_cents
    return capped_max_cents(min_cents, trade["amount_max_cents"])


def simulate(
    trades: Sequence[Mapping[str, Any]],
    price_series: Mapping[str, Series],
    spy: Series,
    *,
    sizing: str,
    cost_bps: int,
    hold_days: int | None,
    from_date: str,
    end_date: str,
) -> tuple[RunMetrics, dict[str, int]]:
    """One sized run. Returns metrics plus exclusion counts for this run.

    trades must be pre-filtered to the window and sorted by disclosure_date;
    exit_mode is fixed-hold when hold_days is not None, else mirror-sells.
    """
    cost_factor = 1.0 - cost_bps / 10_000.0
    exclusions = {"null_ticker": 0, "unpriced_ticker": 0, "no_entry_price": 0,
                  "unmatched_sells": 0}
    positions: list[Position] = []
    bench_shadows: list[Position | None] = []  # 1:1 with positions
    open_by_key: dict[tuple[int, str], list[Position]] = {}

    def _open(trade: Mapping[str, Any], series: Series) -> None:
        entry = series.first_close_after(str(trade["disclosure_date"]))
        if entry is None or entry[0] > end_date:
            exclusions["no_entry_price"] += 1
            return
        entry_date, entry_close = entry
        invested = _sized_cents(trade, sizing)
        position = Position(
            ticker=str(trade["ticker"]).upper(),
            politician_id=int(trade["politician_id"]),
            invested_cents=invested,
            entry_date=entry_date,
            entry_close_cents=entry_close,
            shares=invested / entry_close,
        )
        positions.append(position)
        open_by_key.setdefault((position.politician_id, position.ticker), []).append(position)
        # Benchmark shadow: identical dollars into SPY on the same date.
        bench_entry = spy.first_close_on_or_after(entry_date)
        if bench_entry is None:
            bench_shadows.append(None)
        else:
            bench_shadows.append(
                Position(
                    ticker=BENCHMARK_TICKER,
                    politician_id=position.politician_id,
                    invested_cents=invested,
                    entry_date=bench_entry[0],
                    entry_close_cents=bench_entry[1],
                    shares=invested / bench_entry[1],
                )
            )

    def _close(position: Position, series: Series, exit_day: str) -> None:
        exit_row = series.first_close_on_or_after(exit_day)
        if exit_row is None or exit_row[0] > end_date:
            return  # stays open; marked at the end
        position.exit_date = exit_row[0]
        position.final_value_cents = position.shares * exit_row[1] * cost_factor
        position.closed = True

    for trade in trades:
        ticker = trade["ticker"]
        if ticker is None or str(ticker).strip() == "":
            exclusions["null_ticker"] += 1
            continue
        ticker = str(ticker).upper()
        series = price_series.get(ticker)
        if series is None or not series.dates:
            exclusions["unpriced_ticker"] += 1
            continue
        kind = str(trade["transaction_type"])
        if kind == "buy":
            _open(trade, series)
        elif kind == "sell" and hold_days is None:
            key = (int(trade["politician_id"]), ticker)
            held = [p for p in open_by_key.get(key, []) if not p.closed]
            if not held:
                exclusions["unmatched_sells"] += 1
                continue
            # FIFO: the politician's sell closes their oldest open position;
            # exit is the next trading day after the SELL disclosure.
            exit_after = series.first_close_after(str(trade["disclosure_date"]))
            if exit_after is None:
                continue
            _close(held[0], series, exit_after[0])
        # exchanges and (in fixed-hold mode) sells generate no action

    if hold_days is not None:
        for position in positions:
            target = (
                date_type.fromisoformat(position.entry_date) + timedelta(days=hold_days)
            ).isoformat()
            series = price_series[position.ticker]
            _close(position, series, target)

    # Mark whatever is still open at the last close on/before end_date, with
    # the eventual exit cost applied so open and closed positions compare
    # honestly.
    for position in positions:
        if position.closed:
            continue
        mark = price_series[position.ticker].close_on_or_before(end_date)
        if mark is None or mark[0] < position.entry_date:
            mark = (position.entry_date, position.entry_close_cents)
        position.exit_date = None
        position.final_value_cents = position.shares * mark[1] * cost_factor

    # Benchmark exits mirror the portfolio's exit dates position-for-position.
    for portfolio_pos, bench_pos in zip(positions, bench_shadows):
        if bench_pos is None:
            continue
        if portfolio_pos.closed and portfolio_pos.exit_date is not None:
            exit_row = spy.first_close_on_or_after(portfolio_pos.exit_date)
            if exit_row is not None and exit_row[0] <= end_date:
                bench_pos.exit_date = exit_row[0]
                bench_pos.final_value_cents = bench_pos.shares * exit_row[1] * cost_factor
                bench_pos.closed = True
        if not bench_pos.closed:
            mark = spy.close_on_or_before(end_date)
            if mark is None or mark[0] < bench_pos.entry_date:
                mark = (bench_pos.entry_date, bench_pos.entry_close_cents)
            bench_pos.final_value_cents = bench_pos.shares * mark[1] * cost_factor

    invested = sum(p.invested_cents for p in positions)
    final_value = sum(p.final_value_cents for p in positions)
    total_return = (final_value / invested - 1.0) if invested else 0.0
    bench_real = [p for p in bench_shadows if p is not None]
    bench_invested = sum(p.invested_cents for p in bench_real)
    bench_value = sum(p.final_value_cents for p in bench_real)
    benchmark_return = (bench_value / bench_invested - 1.0) if bench_invested else 0.0

    window_days = max(
        (date_type.fromisoformat(end_date) - date_type.fromisoformat(from_date)).days,
        1,
    )
    cagr = (1.0 + total_return) ** (365.25 / window_days) - 1.0 if invested else 0.0
    winners = sum(1 for p in positions if p.final_value_cents > p.invested_cents)
    hit_rate = winners / len(positions) if positions else 0.0
    max_drawdown = _max_drawdown(positions, price_series, spy, end_date, cost_factor)

    metrics = RunMetrics(
        sizing=sizing,
        positions=len(positions),
        closed_positions=sum(1 for p in positions if p.closed),
        invested_cents=invested,
        final_value_cents=round(final_value),
        total_return=total_return,
        cagr=cagr,
        max_drawdown=max_drawdown,
        hit_rate=hit_rate,
        benchmark_return=benchmark_return,
        excess_return=total_return - benchmark_return,
    )
    return metrics, exclusions


def _max_drawdown(
    positions: list[Position],
    price_series: Mapping[str, Series],
    spy: Series,
    end_date: str,
    cost_factor: float,
) -> float:
    """Max drawdown of the daily time-weighted return index (flows removed)."""
    if not positions:
        return 0.0
    calendar = [d for d in spy.dates if d <= end_date]
    if not calendar:
        return 0.0
    start = min(p.entry_date for p in positions)
    calendar = [d for d in calendar if d >= start]

    index = 1.0
    peak = 1.0
    max_dd = 0.0
    prev_value = 0.0
    for day in calendar:
        flows = sum(p.invested_cents for p in positions if p.entry_date == day)
        value = 0.0
        for p in positions:
            if p.entry_date > day:
                continue
            if p.closed and p.exit_date is not None and p.exit_date <= day:
                value += p.final_value_cents  # realized, held as cash
                continue
            mark = price_series[p.ticker].close_on_or_before(day)
            if mark is None:
                mark = (p.entry_date, p.entry_close_cents)
            value += p.shares * mark[1] * cost_factor
        base = prev_value + flows
        if base > 0:
            index *= value / base
            peak = max(peak, index)
            max_dd = min(max_dd, index / peak - 1.0)
        prev_value = value
    return max_dd


def load_trades(
    conn: sqlite3.Connection,
    from_date: str,
    politician_id: int | None = None,
    chamber: str | None = None,
) -> list[dict[str, Any]]:
    """Buy and sell trades in the window, oldest disclosure first."""
    where = ["t.disclosure_date >= ?", "t.transaction_type IN ('buy', 'sell')"]
    args: list[Any] = [from_date]
    if politician_id is not None:
        where.append("t.politician_id = ?")
        args.append(politician_id)
    if chamber is not None:
        where.append("p.chamber = ?")
        args.append(chamber)
    rows = conn.execute(
        f"""
        SELECT t.politician_id, p.full_name AS politician_name, t.ticker,
               t.transaction_type, t.amount_min_cents, t.amount_max_cents,
               t.disclosure_date
        FROM trades t JOIN politicians p ON p.id = t.politician_id
        WHERE {' AND '.join(where)}
        ORDER BY t.disclosure_date ASC, t.id ASC
        """,
        args,
    ).fetchall()
    return [dict(row) for row in rows]


def run_backtest(
    conn: sqlite3.Connection,
    trades: Sequence[Mapping[str, Any]],
    from_date: str,
    end_date: str,
    cost_bps: int = DEFAULT_COST_BPS,
    hold_days: int | None = None,
) -> BacktestOutcome:
    """Band run (min- and max-sized) over cached prices. No network here."""
    from . import prices as prices_module

    tickers = sorted(
        {
            str(t["ticker"]).upper()
            for t in trades
            if t["ticker"] is not None and str(t["ticker"]).strip()
        }
    )
    series: dict[str, Series] = {}
    unpriced: list[str] = []
    for ticker in tickers:
        rows = prices_module.get_series(conn, ticker)
        if rows:
            series[ticker] = Series(rows)
        else:
            unpriced.append(ticker)
    spy_rows = prices_module.get_series(conn, BENCHMARK_TICKER)
    if not spy_rows:
        raise ValueError("SPY benchmark prices missing; run the price fetch first")
    spy = Series(spy_rows)

    outcome = BacktestOutcome(
        from_date=from_date,
        end_date=end_date,
        exit_mode=f"hold-{hold_days}d" if hold_days is not None else "mirror-sells",
        cost_bps=cost_bps,
        unpriced_tickers=unpriced,
    )
    for sizing in ("min", "max"):
        metrics, exclusions = simulate(
            trades,
            series,
            spy,
            sizing=sizing,
            cost_bps=cost_bps,
            hold_days=hold_days,
            from_date=from_date,
            end_date=end_date,
        )
        outcome.runs[sizing] = metrics
        outcome.exclusions = exclusions  # identical across sizings by design
    return outcome


def _pct(value: float) -> str:
    return f"{value * 100:+.2f}%"


def _band_str(low: float, high: float) -> str:
    return f"[{_pct(low)}, {_pct(high)}]"


def _money(cents: int) -> str:
    return f"${cents // 100:,}.{cents % 100:02d}"


def format_report(outcome: BacktestOutcome, extra_exclusions: Mapping[str, int] | None = None) -> str:
    """Markdown report; every number banded, exclusions always shown."""
    bands = {
        "Total return": outcome.band("total_return"),
        "CAGR (annualized, window-based)": outcome.band("cagr"),
        "Max drawdown (TWR index)": outcome.band("max_drawdown"),
        "Hit rate": outcome.band("hit_rate"),
        "Benchmark (SPY, identical flows)": outcome.band("benchmark_return"),
        "Excess vs benchmark": outcome.band("excess_return"),
    }
    run_min = outcome.runs["min"]
    run_max = outcome.runs["max"]
    lines = [
        f"> {MANDATORY_REPORT_HEADER}",
        "",
        f"# Backtest report — {outcome.from_date} to {outcome.end_date}",
        "",
        f"- Exit mode: {outcome.exit_mode}",
        f"- Round-trip cost: {outcome.cost_bps} bps",
        f"- Entry rule: next trading-day close strictly after DISCLOSURE date",
        f"- Positions (priced buys): {run_min.positions}"
        f" ({run_min.closed_positions} closed, rest marked at window end)",
        f"- Simulated capital: {_money(run_min.invested_cents)} (min-sized) /"
        f" {_money(run_max.invested_cents)} (max-sized, open ranges capped at"
        " the next official band floor)",
        "",
        "| Metric | Min-sized | Max-sized | Band |",
        "|---|---|---|---|",
    ]
    for label, (low, high) in bands.items():
        attr = {
            "Total return": "total_return",
            "CAGR (annualized, window-based)": "cagr",
            "Max drawdown (TWR index)": "max_drawdown",
            "Hit rate": "hit_rate",
            "Benchmark (SPY, identical flows)": "benchmark_return",
            "Excess vs benchmark": "excess_return",
        }[label]
        lines.append(
            f"| {label} | {_pct(getattr(run_min, attr))} |"
            f" {_pct(getattr(run_max, attr))} | {_band_str(low, high)} |"
        )
    lines += [
        "",
        "## Exclusions (survivorship honesty)",
        "",
        f"- Trades without a ticker: {outcome.exclusions.get('null_ticker', 0)}",
        f"- Trades on unpriced tickers: {outcome.exclusions.get('unpriced_ticker', 0)}"
        f" (distinct unpriced tickers: {len(outcome.unpriced_tickers)})",
        f"- Buys with no tradable entry before window end: {outcome.exclusions.get('no_entry_price', 0)}",
        f"- Sell disclosures with no matching open position: {outcome.exclusions.get('unmatched_sells', 0)}",
    ]
    if extra_exclusions:
        for name, count in extra_exclusions.items():
            lines.append(f"- {name}: {count}")
    if outcome.unpriced_tickers:
        sample = ", ".join(outcome.unpriced_tickers[:20])
        suffix = " …" if len(outcome.unpriced_tickers) > 20 else ""
        lines.append(f"- Unpriced tickers: {sample}{suffix}")
    lines.append("")
    return "\n".join(lines)


@dataclass
class LeaderboardRow:
    politician_id: int
    politician_name: str
    priced_trades: int
    excess_low: float
    excess_high: float
    hit_rate_low: float
    hit_rate_high: float


def build_leaderboard(
    conn: sqlite3.Connection,
    trades: Sequence[Mapping[str, Any]],
    from_date: str,
    end_date: str,
    cost_bps: int = DEFAULT_COST_BPS,
    min_priced_trades: int = LEADERBOARD_MIN_PRICED_TRADES,
) -> list[LeaderboardRow]:
    """Per-politician mirror-sells bands, sorted by the PESSIMISTIC bound."""
    by_politician: dict[int, list[Mapping[str, Any]]] = {}
    names: dict[int, str] = {}
    for trade in trades:
        pid = int(trade["politician_id"])
        by_politician.setdefault(pid, []).append(trade)
        names[pid] = str(trade["politician_name"])

    rows: list[LeaderboardRow] = []
    for pid, own_trades in by_politician.items():
        outcome = run_backtest(
            conn, own_trades, from_date, end_date, cost_bps=cost_bps, hold_days=None
        )
        priced = outcome.runs["min"].positions
        if priced < min_priced_trades:
            continue
        excess = outcome.band("excess_return")
        hit = outcome.band("hit_rate")
        rows.append(
            LeaderboardRow(
                politician_id=pid,
                politician_name=names[pid],
                priced_trades=priced,
                excess_low=excess[0],
                excess_high=excess[1],
                hit_rate_low=hit[0],
                hit_rate_high=hit[1],
            )
        )
    rows.sort(key=lambda r: r.excess_low, reverse=True)
    return rows
