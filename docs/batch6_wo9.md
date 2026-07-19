# Batch 6 — WO-9 prospective H2 shadow tracking

Status: AUTHORIZED FOLLOW-ON TO WO-8; INFORMATIONAL ONLY

## Evidence and authority

WO-8 produced one pre-registered pass: H2, purchases disclosed within 15
calendar days of their transaction date. Under the frozen WO-8 rule, that
result authorizes one prospective shadow-tracking quarter and nothing else.
ADR-003 continues to prohibit paper trading, live trading, brokerage access,
orders, accounts, credentials, deposits, and money movement.

## Objective

Record H2 signals exactly when PelosiTracker first observes them during a
fixed 90-day forward window. Produce an auditable, append-only signal cohort
that can be evaluated after the window closes without backfilling, selecting,
or rewriting observations.

## Constitutional constraints

1. Activation is explicit. `shadow start` records the UTC activation time and
   the current maximum trade ID. Every pre-existing trade is outside WO-9.
2. The observation window is exactly 90 calendar days. It is not configurable
   after results begin.
3. A signal must be a purchase disclosed 0–15 calendar days after its
   transaction, using the same H2 filter implemented for WO-8.
4. A row inserted after activation whose disclosure date predates the UTC
   activation date is an upstream backfill. It is counted in the scan audit
   but never entered into the signal cohort.
5. Every qualifying disclosure is logged, including records without a ticker.
   Missing tickers remain visible as unresolved observations; they are never
   silently discarded.
6. Signals and scan audits are append-only. There is no reset, delete, edit,
   or recompute command.
7. WO-9 records disclosures only. It does not model or place hypothetical,
   paper, or live orders; track balances or positions; connect to a broker;
   accept credentials; move money; or promise returns.
8. Amounts remain disclosed min/max integer-cent ranges. No midpoint,
   average, or inferred amount may be stored or displayed.
9. Existing ingestion integrity controls remain mandatory. House disclosures
   still fail closed against the official Clerk index.

## Persistence contract

Add a stdlib-only `pelositracker.shadow` module with three additive tables.

### `shadow_tracking_state`

One immutable campaign identity row (`id = 1`) containing:

- strategy key and version (`H2_FAST_FILERS`, version `1`)
- activation UTC timestamp
- scheduled end UTC timestamp (activation + 90 days)
- activation trade-ID boundary
- last scanned trade ID
- optional completion UTC timestamp

The state may advance only its watermark and completion timestamp. No public
reset function or CLI exists.

### `shadow_signals`

One append-only snapshot per qualifying source trade, with a unique source
trade ID and:

- observation timestamp and strategy identity
- politician ID/name and chamber
- ticker (nullable) and asset name
- transaction/disclosure dates and disclosure-lag days
- transaction type
- disclosed amount min/max cents
- source URL and provenance

The table must not depend on a foreign key that would prevent a provenance
purge. SQLite triggers reject updates and deletes.

### `shadow_scans`

An append-only audit entry for every active scan:

- scan timestamp and before/after watermarks
- number of rows examined
- number of H2 signals appended
- number rejected as pre-activation backfills
- campaign status

SQLite triggers reject updates and deletes.

## Runtime contract

### CLI

- `python -m pelositracker shadow start`: activate once, baselining all current
  trade IDs. A second activation fails without changing state.
- `python -m pelositracker shadow scan`: scan only IDs above the durable
  watermark, append eligible H2 signals atomically, record an audit row, and
  advance the watermark. Before activation it reports `not started` and logs
  no signal.
- `python -m pelositracker shadow status`: print campaign timestamps,
  watermarks, scan/signal counts, and the informational disclaimer.

### Scheduled runner

After Senate and House ingestion and before the existing digest, each runner
cycle invokes the shadow scanner. Its structured log segment must distinguish
`not-started`, `active`, and `completed`, and report appended signals plus
rejected backfills. A shadow failure is isolated and counted like a source
failure; it must not fabricate success or prevent the digest from running.

## Transaction and time rules

- UTC timestamps are ISO-8601 with an explicit `+00:00` offset and seconds.
- The maximum trade ID is captured at the beginning of each scan. Rows arriving
  concurrently above that boundary wait for the next scan.
- Signal inserts, scan audit insertion, and watermark advancement happen in
  one SQLite transaction.
- Re-running a scan without new trades appends a zero-count audit but no
  duplicate signal.
- Once the scheduled end is reached, the campaign records completion once and
  never accepts another signal.

## Required tests

Use only fictional politicians and an in-memory or temporary SQLite database.

1. Activation baselines existing trades and makes them permanently ineligible.
2. A second activation is rejected without changing the original boundary.
3. A new H2 buy is logged once with exact ranges, provenance, lag, and UTC
   observation timestamp.
4. H2 boundary lags of 0 and 15 pass; lag 16, negative lag, and sells fail.
5. A post-activation insert carrying a pre-activation disclosure date is
   audited as a rejected backfill and never becomes a signal.
6. A qualifying disclosure without a ticker remains in the cohort.
7. Scans are idempotent through the durable watermark.
8. Signal and scan rows reject update/delete attempts.
9. The 90-day end prevents later signals and records completion once.
10. Runner ordering is ingest → shadow scan → digest; not-started and failures
    are reported honestly.
11. Source audit confirms no brokerage, account, credential, order, deposit,
    portfolio, or money-movement implementation is introduced.
12. The entire existing test suite remains green.

## Acceptance gate

- Focused WO-9 tests pass.
- Full `python -m unittest discover -s tests -v` passes.
- No existing report artifacts are recomputed or modified.
- Diff review confirms the WO-8 H2 definition is reused rather than forked.
- ADR-003 names WO-9 as the sole authorized follow-on and reiterates that the
  pass did not authorize Phase B.
- Approved commit message contains `[APPROVED]` only after the gate passes.
