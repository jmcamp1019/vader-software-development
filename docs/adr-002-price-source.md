# ADR-002: Backtest price source — Stooq replaced by Yahoo chart API

Status: ACCEPTED (CEO ruling, 2026-07-16)

## Context

WO-7 (docs/batch4_orders.md) specified Stooq's free daily CSV endpoint
(`https://stooq.com/q/d/l/?s={symbol}&i=d`) as the price source for the
copy-trading backtest. On the first live run (2026-07-16), every request —
including AAPL, MSFT, XOM — returned an HTML page containing a JavaScript
anti-bot browser-verification challenge instead of CSV. Verified directly:
HTTP 200, `Content-Type: text/html`, challenge script in the body, identical
under multiple User-Agents. A stdlib-only client cannot execute JavaScript,
so the endpoint is dead to this project, not merely throttled.

That failed run also exposed a defect: the fetch layer recorded every failure
as permanently "unpriced", writing all 1,083 tickers in the backtest window
into `unpriced_tickers`, which subsequent runs skip. A systemic outage must
never poison per-ticker state.

## Decision

Replace Stooq with Yahoo Finance's chart API
(`https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={range}&interval=1d`).

Selection criteria (same properties the work order wanted from Stooq):

- free, no API key, no account;
- fetchable and parseable with the stdlib only (urllib + json);
- split-adjusted daily closes (a raw-close series across e.g. NVDA's 2024
  10-for-1 split would fabricate a -90% loss);
- accepts the project's honest User-Agent (`PelosiTracker/0.1 ...`) —
  verified HTTP 200; no browser masquerade needed.

Alongside the swap, the unpriced/transient distinction is now first-class:

- Only a definitive per-ticker failure marks a ticker unpriced: HTTP 404
  (symbol not found) or a well-formed response with no usable data.
- Every other failure (429 throttle, 401/403 block, 5xx, network errors) is
  transient: retried once after a 10x-sleep backoff, then skipped for that
  run only and reported in the `skipped` coverage count — never written to
  `unpriced_tickers`.
- The 1,083 rows poisoned by the Stooq outage were purged.

Fetching stays respectful: sequential, one request per ticker, small sleep
between requests (`PT_PRICE_FETCH_SLEEP`, default 0.3s), honest User-Agent,
backoff on rate-limit responses as above.

## Risks and constraints

- The chart API is **unofficial and undocumented**; Yahoo does not support it
  and its terms of use for programmatic access are gray. It may be walled off
  without notice, exactly as Stooq was.
- **CEO constraint (binding):** this source is approved for LOCAL RESEARCH
  ONLY. Before any paper-trading or live-trading phase is even considered,
  price data must be re-sourced from a licensed market-data provider. This
  ADR does not authorize any Phase B/C work (see WO-7 explicit non-goals).

## Alternatives considered

- Stay on Stooq: impossible without a JavaScript runtime (new dependency —
  prohibited) or challenge circumvention (rejected outright).
- Alpha Vantage / Tiingo / Polygon free tiers: require API keys (secret
  handling) and have per-day request caps below the ~1,100 tickers needed.
- Do nothing: WO-7's live run stays blocked and the CEO's Phase B/C decision
  has no evidence base.
