# PelosiTracker — Batch 2 Work Orders (Vader Dual-CLI Workflow)
Execution protocol: WO-0 is architecture and belongs to the main agent (no dispatch).
WO-1 through WO-3 are dispatched to `agy` via scripts/delegate.ps1, one at a time, in
order; each result goes through the Verification Gate before the next order is issued.
Standing rules from CLAUDE.md apply to every order: stdlib-only, amount ranges never
collapsed, disclosure_date never fabricated, [APPROVED] tag required to commit.

---

## WO-0 — House data source decision (MAIN AGENT — do not dispatch)
The House S3 bucket 403s. Investigate and write docs/adr-001-house-source.md:
1. Probe https://housestockwatcher.com/api health (same maintainer as dead bucket).
2. Evaluate official House Clerk financial disclosure downloads
   (disclosures-clerk.house.gov) — format, effort to parse, ToS.
3. Evaluate keyed APIs (e.g. Financial Modeling Prep house-trading endpoint) — cost,
   rate limits, schema fit. NOTE: any keyed option requires an env var
   (HOUSE_API_KEY) and my sign-off before implementation.
Recommend one primary + one fallback. STOP after the ADR — implementation is a
future order once the CEO approves the recommendation.

## WO-1 — Read-only query API (DISPATCH to agy)
Build src/pelositracker/api.py using ONLY http.server / json / sqlite3 (stdlib):
- GET /api/v1/trades?limit=&offset=&ticker=&politician_id=&type=&date_from=&date_to=
  (default sort: disclosure_date desc; limit capped at 200)
- GET /api/v1/politicians and /api/v1/politicians/{id} (aggregates: trade_count,
  top 5 tickers; total volume returned as {"min_cents":…,"max_cents":…|null} — a
  RANGE, never a single number)
- GET /api/v1/tickers/{symbol}/trades
- Every JSON response includes "disclaimer": the standing not-investment-advice text,
  and trades include days_to_disclosure computed from the two dates.
- Read-only: reject non-GET with 405. Bind 127.0.0.1 by default (env PT_API_HOST/PORT).
- CLI: python -m pelositracker serve --db pelositracker.db
- Tests: spin server on an ephemeral port against a seeded temp DB; cover filters,
  pagination cap, 404s, 405 on POST, range preservation in aggregates.

## WO-2 — Watchlists (DISPATCH to agy)
- Schema: watchlists(id, kind CHECK('politician','ticker'), politician_id NULL,
  ticker NULL, created_at) with a CHECK enforcing exactly one target set. Additive
  migration logic in db.py (CREATE TABLE IF NOT EXISTS is acceptable).
- CLI: python -m pelositracker watch add --ticker NVDA | add --politician-id 3 |
  list | remove <id>
- API: GET /api/v1/watchlists (read-only view).
- Tests: XOR constraint, CLI round-trip, dedupe on identical entries.

## WO-3 — Alert digest (DISPATCH to agy)
- Watermark table: last_seen(max trade id) updated after each digest run.
- python -m pelositracker digest: finds trades newer than watermark matching any
  watchlist entry, prints a human-readable digest (politician, ticker, type, amount
  RANGE, transaction vs disclosure date, source link) and writes digests/<date>.txt.
- Optional email: ONLY if SMTP_HOST/SMTP_PORT/SMTP_FROM/SMTP_TO env vars are all set,
  send via smtplib; otherwise skip silently. No credentials in code or defaults.
- Tests: watermark advances, no-match produces empty digest, re-run emits nothing new,
  amount ranges rendered as ranges in the digest text.

---

## Gate criteria for every dispatched order (fable-gate checklist)
1. Full type hints; stdlib-only (any new import outside the standard library = reject).
2. All prior tests still pass; new modules ship with tests in the same batch.
3. Amount ranges intact end-to-end; grep for midpoint/average on amount fields.
4. disclosure_date never defaulted, inferred, or fabricated.
5. No secrets, no telemetry, no network calls except documented feeds and
   explicitly-configured SMTP.
6. Commit messages carry [APPROVED] only after the gate passes.

Dispatch reminder: prepend docs/gemini_system_prompt-style rules (stdlib constraint,
output as complete files with === FILE: path === delimiters) to every agy prompt, and
treat agy output as an untrusted draft — read before executing anything.
