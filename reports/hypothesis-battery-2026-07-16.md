> HYPOTHETICAL BACKTEST. Results do not represent actual trading, ignore liquidity and slippage beyond the modeled cost, and past performance does not indicate future results. This is not investment advice.

# WO-8 pre-registered hypothesis battery — FINAL

Train disclosures: 2024-01-01 to 2025-06-30
Holdout disclosures (evaluated once): 2025-07-01 to 2026-07-16
Scoring: forced realized hold-90, 20 bps, next-close entry, SPY shadow

| Hypothesis | Train trades | Train excess band | Holdout trades | Holdout excess band | Verdict |
|---|---:|---:|---:|---:|---|
| H1 purchases-only | 1197 | [+4.40%, +4.62%] | 1179 | [+0.94%, +2.50%] | FAIL |
| H2 fast-filers (<=15d) | 683 | [+5.59%, +5.88%] | 392 | [+3.37%, +3.81%] | PASS |
| H3 conviction-size (min >= $50,000) | 51 | [+7.08%, +8.21%] | 51 | [+2.43%, +2.48%] | FAIL |
| H4 consensus (3 members / 30d) | 48 | [+6.83%, +7.87%] | 37 | [+3.91%, +6.48%] | FAIL |
| H5-house chamber-split: house | 1197 | [+4.40%, +4.62%] | 1179 | [+0.94%, +2.50%] | FAIL |
| H5-senate chamber-split: senate | 0 | [+0.00%, +0.00%] | 0 | [+0.00%, +0.00%] | FAIL |
| H6 train top-decile skill cohort | 66 | [+8.55%, +9.09%] | 94 | [+5.40%, +5.64%] | FAIL |

Passing scored rows: 1 of 7 (six hypotheses; H5 has separately judged House and Senate rows).

H6 frozen train cohort: Byron Donalds, Cleo Fields, Virginia Foxx
