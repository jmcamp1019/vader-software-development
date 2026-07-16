# PelosiTracker — Batch 4 Work Order (Vader Workflow)
One work order. CLAUDE-DIRECT throughout (strategy simulation is judgment code);
agy may be dispatched ONLY for test fixtures. All standing CLAUDE.md rules apply.
Purpose: convert the copy-trading thesis from opinion to evidence before any
real-money feature is even considered. The output of WO-7 is the input to the
CEO's Phase B/C decision.

---

## WO-7 — Backtest engine + politician leaderboard (CLAUDE-DIRECT)

### Price data
- New module prices.py: fetch daily OHLC history per ticker from Stooq's free
  CSV endpoint (https://stooq.com/q/d/l/?s={symbol}.us&i=d) — no API key,
  stdlib urllib + csv. Cache into a new `prices` table (ticker, date, close)
  with idempotent upserts; refresh only missing ranges. Respectful fetching:
  sequential, small sleep between tickers, clear log line per ticker.
- Tickers that Stooq can't resolve are recorded in an `unpriced_tickers` table
  and excluded, with the exclusion count reported in every backtest output.

### Simulation semantics (fixed — these are the honest-defaults)
- Signal date = DISCLOSURE date (never transaction date — we cannot act on
  information before it was public). Entry = next trading day's close after
  disclosure. This models OUR latency truthfully.
- Buy signals open positions; politician sell disclosures close matching
  positions if held, else are ignored. Alternative fixed-hold exits (30/90/180
  days) run as separate scenarios.
- Position sizing: ranges are never collapsed. Every backtest runs TWICE —
  once sizing at amount_min, once at amount_max — and all results are reported
  as a BAND [min-sized, max-sized]. Open-ended maxes cap at the range floor of
  the next official STOCK Act band to stay conservative.
- Costs: configurable round-trip cost in basis points (default 20 bps) applied
  to every simulated trade. Zero-cost runs are not reported alone, ever.
- Benchmark: identical cash flows into SPY on the same dates (dollar-for-dollar
  shadow portfolio). Every result is reported AGAINST benchmark, never bare.

### Outputs
- `python -m pelositracker backtest --from 2024-01-01 [--hold N|--mirror-sells]
  [--politician-id X] [--chamber house|senate] [--cost-bps N]`
- Metrics per run: total return band, CAGR band, max drawdown, hit rate,
  benchmark return on identical flows, and excess-vs-benchmark band.
- Leaderboard: `python -m pelositracker leaderboard --from DATE` — per
  politician (minimum 10 priced trades): excess-return-vs-SPY band at our
  latency, trade count, hit rate. Sorted by lower bound of excess return
  (pessimistic ordering — a politician is only "top" if even the conservative
  sizing beats the benchmark).
- Report: backtest writes reports/backtest-<date>.md with parameters, results
  table, exclusion counts, and this mandatory header verbatim:
  "HYPOTHETICAL BACKTEST. Results do not represent actual trading, ignore
  liquidity and slippage beyond the modeled cost, and past performance does
  not indicate future results. This is not investment advice."

### Gate additions
- No look-ahead: entry prices strictly after disclosure date (test enforced
  with synthetic price fixtures).
- Range discipline: any code path producing a single-number result instead of
  a band = automatic reject.
- Survivorship honesty: delisted/unpriced tickers reported, never silently
  dropped from denominator narratives.
- Tests: synthetic price + trade fixtures with hand-computed expected returns
  (fictional politicians); benchmark shadow math; band ordering; look-ahead
  guard; cost application. No live network in tests.

### Explicit non-goals for WO-7
No brokerage integration, no order placement, no live trading of any kind, no
paper-trading wiring. Those are Phase B/C questions that exist only if this
work order's numbers earn them, and they involve controls (credential handling,
kill switches, human confirmation) that are CEO-level decisions, not work-order
line items.
