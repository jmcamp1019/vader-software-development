# PelosiTracker — Batch 5 Work Order: WO-8 Hypothesis Battery
CLAUDE-DIRECT throughout. Purpose: give the copy-trading thesis its one honest
shot at redemption. This is a PRE-REGISTERED battery: the hypothesis list below
is final before any result is computed. No hypothesis may be added, removed,
tuned, or re-parameterized after results exist. The holdout window is evaluated
EXACTLY ONCE. If the battery fails, the program does not iterate on the holdout
— that is the entire point.

## Data protocol
- Train window: disclosures 2024-01-01 → 2025-06-30.
- Holdout window: disclosures 2025-07-01 → latest. Holdout is computed once,
  after train results are committed, and never re-run with modified parameters.
- Scoring: forced realization at fixed hold-90 (chosen from the WO-7 robustness
  pass BEFORE this battery; not a free parameter), 20 bps round-trip, entry next
  trading close after disclosure, SPY shadow on identical flows, min/max sizing
  bands. Pass metric = LOWER band of excess-vs-SPY.
- All results reported for all hypotheses in reports/hypothesis-battery-<date>.md,
  passes and failures alike, with the mandatory hypothetical-backtest header.

## Pre-registered hypotheses (final — six, no additions)
- H1 purchases-only: buy disclosures only; sells carry no signal.
- H2 fast-filers: buys disclosed within 15 days of transaction (tests whether
  latency is the edge-killer).
- H3 conviction-size: buys with amount_min >= $50,000 (band floor as conviction
  proxy; ranges never collapsed).
- H4 consensus: tickers bought by >= 3 distinct members within any 30-day
  window; enter at the 3rd disclosure.
- H5 chamber-split: H1 computed separately for house vs senate (structural
  difference in filing behavior).
- H6 skill-persistence: members in the top decile of the TRAIN-window
  realized leaderboard (min 10 priced trades) — do their HOLDOUT-window trades
  beat the benchmark? This is the direct test of "copy the top traders."

## Pass bar (pre-committed, CEO-signed)
A hypothesis PASSES only if ALL hold:
1. Lower excess band > 0 in the train window;
2. Lower excess band > 0 in the holdout window (single evaluation);
3. Holdout excess lower bound >= +2.0 pp (economic materiality, not just sign);
4. >= 100 priced trades in each window (H6: >= 30 in holdout).
With six hypotheses, one marginal pass is statistically expectable by luck.
Therefore: a single pass is PROVISIONAL and triggers only a shadow-tracking
quarter (log hypothetical signals forward in real time — no accounts, no
orders, no money) before any Phase B conversation. Two-plus passes, or one
pass at >= +5 pp holdout lower bound, earns the Phase B scoping discussion —
which still begins at paper trading, never live.

## Failure clause (equally binding)
If no hypothesis passes: the finding is recorded in ADR-003 as confirmatory,
the battery is NOT re-run with new hypotheses against the same holdout, and
strategy search ends until an ADR-003 re-test trigger fires (disclosure-lag
legislation; materially new data regime). Data-mining beyond a pre-registered
battery is prohibited — a strategy discovered by search-until-pass is
overfit by construction and will not be implemented regardless of its
backtested numbers.

## Engineering notes
- Implement as composable trade-filter predicates over the existing backtest
  engine; no changes to simulation semantics (gate-checked).
- Tests: each filter against synthetic fixtures with hand-computed membership;
  train/holdout boundary enforcement; H6 leaderboard-cohort selection uses
  train data only (look-ahead guard).
- Suite must remain green; fable-gate before every commit; report committed
  with the results.
