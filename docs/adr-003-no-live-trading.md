# ADR-003: Phase B decision — no paper- or live-trading phase

Status: ACCEPTED (CEO ruling, 2026-07-17)

## Context

WO-7 (docs/batch4_orders.md) was pre-committed as the evidence gate for any
Phase B/C conversation: brokerage integration, order placement, paper or live
trading "exist only if this work order's numbers earn them." The full backtest
(2024-01-01 → 2026-07-17, 20 bps round-trip, entry at the next trading close
strictly after disclosure, SPY shadow on identical flows, min/max sizing
bands) produced a headline mirror-sells excess of [+12.17, +13.96] pp over
SPY — and a robustness pass that dismantled it:

| Variant | Excess vs SPY (band) | Hit rate |
|---|---|---|
| Mirror-sells, marked-to-market | [+12.17%, +13.96%] | 60.8% |
| Fixed hold-30 (forced realization) | [+0.46%, +0.59%] | 56.6% |
| Fixed hold-90 | [+2.98%, +3.61%] | 59.0% |
| Fixed hold-180 | [+2.11%, +3.01%] | 61.0% |
| Realized-only (politician-closed) | [-5.29%, -4.34%] | 37.2% |

The outperformance lives almost entirely in open, unrealized marks accumulated
during a bull window. The only realized round-trips — positions closed by the
politicians' own sell disclosures — UNDERPERFORMED the benchmark by 4–5 pp
with a 37% hit rate. Forced-realization variants are positive but modest
(+0.5 to +3.6 pp). The deduplicated leaderboard (ADR-002 identity migration)
shows only 11 of 32 qualifying members beating SPY even at the optimistic
bound, with heavy concentration at the top.

## Decision (the pre-committed rule, applied)

The numbers did not earn Phase B. **Verdict: no paper-trading and no
live-trading phase.** The product remains informational only. No brokerage
integration, no order placement, no credential handling, no auto-execution of
any kind — reaffirming the standing constitutional constraint in CLAUDE.md
and the WO-7 explicit non-goals. ADR-002's price source remains approved for
local research only, and this decision removes the near-term reason to
procure a licensed feed.

WO-8 (docs/batch5_wo8.md) completed its pre-registered, single-evaluation
holdout. H2 — purchases disclosed within 15 calendar days of transaction —
was the one registered pass. That pass did not authorize Phase B, hypothetical
orders, paper trading, live trading, or any integration that can transact.

The sole authorized follow-on is WO-9 (docs/batch6_wo9.md): one fixed 90-day
prospective shadow-tracking campaign that records H2 disclosure observations
only. It may preserve the public disclosure cohort and scan audit; it may not
model orders, balances, positions, or returns. No other strategy search or
operational phase is authorized by the WO-8 result.

## Re-test triggers (exhaustive)

This decision stands until one of the following fires, at which point a new
pre-registered evaluation (new work order, new ADR) may be commissioned:

1. **Disclosure-lag legislation:** a statutory change materially shortening
   STOCK Act reporting/publication delays (e.g. near-real-time disclosure),
   changing the latency structure the backtest showed to be decisive.
2. **A materially different out-of-sample window:** a future period whose
   market regime differs substantially from the 2024–2026 bull tape (e.g. a
   full bear cycle), evaluated as true out-of-sample data — never by
   re-running variants against the window that produced this verdict.
