"""Tests for WO-7 backtest engine. Fixture data is synthetic — fictional
politicians, hand-computed expected returns, no network anywhere."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

import _path  # noqa: F401

from pelositracker.backtest import (
    MANDATORY_REPORT_HEADER,
    Series,
    capped_max_cents,
    format_report,
    simulate,
)

_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "backtest_fixture.json").read_text(
        encoding="utf-8"
    )
)

FROM_DATE = "2026-01-05"
END_DATE = "2026-01-16"


def _series() -> dict[str, Series]:
    return {
        ticker: Series([(d, c) for d, c in rows])
        for ticker, rows in _FIXTURE["prices"].items()
        if ticker != "SPY"
    }


def _spy() -> Series:
    return Series([(d, c) for d, c in _FIXTURE["prices"]["SPY"]])


def _trades(indices: list[int] | None = None) -> list[dict[str, Any]]:
    trades = _FIXTURE["trades"]
    if indices is None:
        return list(trades)
    return [trades[i] for i in indices]


def _run(trades: list[dict[str, Any]], sizing: str = "min", cost_bps: int = 20,
         hold_days: int | None = None,
         realized_only: bool = False) -> tuple[Any, dict[str, int]]:
    return simulate(
        trades,
        _series(),
        _spy(),
        sizing=sizing,
        cost_bps=cost_bps,
        hold_days=hold_days,
        from_date=FROM_DATE,
        end_date=END_DATE,
        realized_only=realized_only,
    )


class BandCapTests(unittest.TestCase):
    def test_explicit_max_passes_through(self) -> None:
        self.assertEqual(capped_max_cents(100_100, 1_500_000), 1_500_000)

    def test_open_max_caps_at_next_band_floor(self) -> None:
        # $10,001 open-ended -> next official floor is $15,001
        self.assertEqual(capped_max_cents(1_000_100, None), 1_500_100)
        # $50M+ open-ended: no higher band exists; cap at the minimum itself
        self.assertEqual(capped_max_cents(5_000_000_000, None), 5_000_000_100)
        self.assertEqual(capped_max_cents(6_000_000_000, None), 6_000_000_000)


class LookAheadTests(unittest.TestCase):
    def test_entry_strictly_after_disclosure(self) -> None:
        # Disclosure 2026-01-05 IS a trading day; entry must be the 6th.
        metrics, _ = _run(_trades([0]))
        self.assertEqual(metrics.positions, 1)
        series = Series([(d, c) for d, c in _FIXTURE["prices"]["TST"]])
        entry = series.first_close_after("2026-01-05")
        self.assertEqual(entry, ("2026-01-06", 10_000))

    def test_disclosure_on_last_price_day_gets_no_entry(self) -> None:
        late = dict(_trades([0])[0])
        late["disclosure_date"] = "2026-01-16"
        metrics, exclusions = _run([late])
        self.assertEqual(metrics.positions, 0)
        self.assertEqual(exclusions["no_entry_price"], 1)


class SingleTradeMathTests(unittest.TestCase):
    """TST buy (disclosed 01-05) + politician sell (disclosed 01-09).

    Hand-computed: entry 01-06 @ $100.00, exit 01-12 @ $125.00.
    20 bps round trip: 1.25 * 0.998 - 1 = +24.75%.
    SPY shadow: entry 01-06 @ $500.00, exit 01-12 @ $510.00:
    1.02 * 0.998 - 1 = +1.796%. Excess = +22.954 pp.
    """

    def test_mirror_sell_return_after_costs(self) -> None:
        metrics, _ = _run(_trades([0, 1]))
        self.assertEqual(metrics.positions, 1)
        self.assertEqual(metrics.closed_positions, 1)
        self.assertAlmostEqual(metrics.total_return, 0.2475, places=10)
        self.assertAlmostEqual(metrics.benchmark_return, 0.01796, places=10)
        self.assertAlmostEqual(metrics.excess_return, 0.22954, places=10)
        self.assertEqual(metrics.hit_rate, 1.0)

    def test_zero_cost_changes_only_the_cost_term(self) -> None:
        metrics, _ = _run(_trades([0, 1]), cost_bps=0)
        self.assertAlmostEqual(metrics.total_return, 0.25, places=10)

    def test_fixed_hold_exit(self) -> None:
        # hold 3 days from entry 01-06 -> target 01-09, exit @ $120.00
        metrics, _ = _run(_trades([0]), hold_days=3)
        self.assertEqual(metrics.closed_positions, 1)
        self.assertAlmostEqual(metrics.total_return, 1.2 * 0.998 - 1.0, places=10)

    def test_drawdown_reflects_only_price_path(self) -> None:
        # Rising TST path: the only dip in the TWR index is the modeled exit
        # cost baked into daily marks (0.998 on day one).
        metrics, _ = _run(_trades([0, 1]))
        self.assertAlmostEqual(metrics.max_drawdown, -0.002, places=9)


class FullFixtureTests(unittest.TestCase):
    def test_exclusions_counted_never_silently_dropped(self) -> None:
        metrics, exclusions = _run(_trades())
        self.assertEqual(metrics.positions, 2)  # TST + ZZZ
        self.assertEqual(exclusions["null_ticker"], 1)
        self.assertEqual(exclusions["unpriced_ticker"], 1)  # NOPE
        self.assertEqual(exclusions["unmatched_sells"], 1)  # Testa's ZZZ sell

    def test_band_sizing_and_open_range_cap(self) -> None:
        run_min, _ = _run(_trades(), sizing="min")
        run_max, _ = _run(_trades(), sizing="max")
        # min: 100,100 + 1,000,100; max: 1,500,000 + capped 1,500,100 cents
        self.assertEqual(run_min.invested_cents, 1_100_200)
        self.assertEqual(run_max.invested_cents, 3_000_100)
        # Hand-computed money-weighted returns (TST closed, ZZZ marked @ 110.00)
        self.assertAlmostEqual(run_min.total_return, -0.332099, places=5)
        self.assertAlmostEqual(run_max.total_return, -0.071316, places=5)
        # Band ordering: lower bound first, never a single number
        band = sorted((run_min.total_return, run_max.total_return))
        self.assertLess(band[0], band[1])

    def test_losing_open_position_counts_against_hit_rate(self) -> None:
        metrics, _ = _run(_trades())
        self.assertAlmostEqual(metrics.hit_rate, 0.5, places=10)  # TST won, ZZZ losing
        self.assertLess(metrics.max_drawdown, -0.3)  # ZZZ collapsed 18->11


class RealizedOnlyTests(unittest.TestCase):
    """Full fixture: TST closes (mirror sell); ZZZ stays an open mark.

    Realized-only must therefore equal the hand-computed single-TST numbers
    and exclude ZZZ from capital, hit rate, and the benchmark shadow.
    """

    def test_realized_only_restricts_to_closed_positions(self) -> None:
        metrics, _ = _run(_trades(), realized_only=True)
        self.assertEqual(metrics.positions, 1)
        self.assertEqual(metrics.closed_positions, 1)
        self.assertEqual(metrics.invested_cents, 100_100)
        self.assertAlmostEqual(metrics.total_return, 0.2475, places=10)
        self.assertAlmostEqual(metrics.benchmark_return, 0.01796, places=10)
        self.assertAlmostEqual(metrics.excess_return, 0.22954, places=10)
        self.assertEqual(metrics.hit_rate, 1.0)

    def test_realized_only_differs_from_marked_run(self) -> None:
        marked, _ = _run(_trades())
        realized, _ = _run(_trades(), realized_only=True)
        self.assertNotAlmostEqual(
            marked.total_return, realized.total_return, places=3
        )

    def test_no_closed_positions_yields_zero_not_crash(self) -> None:
        metrics, _ = _run(_trades([2]), realized_only=True)  # ZZZ buy, never sold
        self.assertEqual(metrics.positions, 0)
        self.assertEqual(metrics.total_return, 0.0)
        self.assertEqual(metrics.hit_rate, 0.0)


class ReportTests(unittest.TestCase):
    def test_report_has_header_bands_and_exclusions(self) -> None:
        from pelositracker.backtest import BacktestOutcome

        run_min, exclusions = _run(_trades(), sizing="min")
        run_max, _ = _run(_trades(), sizing="max")
        outcome = BacktestOutcome(
            from_date=FROM_DATE,
            end_date=END_DATE,
            exit_mode="mirror-sells",
            cost_bps=20,
            runs={"min": run_min, "max": run_max},
            exclusions=exclusions,
            unpriced_tickers=["NOPE"],
        )
        report = format_report(outcome)
        self.assertIn(MANDATORY_REPORT_HEADER, report)
        self.assertIn("| Excess vs benchmark |", report)
        self.assertIn("Band |", report)
        self.assertIn("[", report)  # banded values present
        self.assertIn("Trades on unpriced tickers: 1", report)
        self.assertIn("Trades without a ticker: 1", report)
        self.assertNotIn("midpoint", report.lower())
        self.assertNotIn("average", report.lower())


if __name__ == "__main__":
    unittest.main()
