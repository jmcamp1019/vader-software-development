# PelosiTracker — Batch 3 Work Orders (Vader Workflow)
Sequence is mandatory: WO-4 → WO-5 → WO-6. All standing CLAUDE.md rules apply,
including the agy demotion (boilerplate-only, sandboxed wrapper, post-dispatch
git status check, 3-round budget — but see per-order assignments below).
Stdlib-only remains law for all Python. Nothing merges without a gate pass and
[APPROVED]. Report to the CEO after each work order lands.

---

## WO-4 — Scheduled ingest runner (CLAUDE-DIRECT — touches ingestion semantics)
Goal: the database stays fresh without a human running ingest commands.
- `python -m pelositracker run --interval-hours N` (default 6, env
  PT_RUN_INTERVAL_HOURS): loop of senate ingest → house ingest → digest,
  with structured one-line log output per cycle (timestamp, per-source stats,
  quarantine count, digest matches).
- Single-instance lock (stdlib: O_CREAT|O_EXCL lockfile with PID, stale-lock
  detection) so overlapping runs are impossible.
- Failure semantics: a source failing (including house fail-closed on Clerk
  index unavailability) logs and continues to the next cycle — one bad cycle
  never kills the runner. Consecutive-failure counter surfaces in logs.
- Quarantine tripwire: if house quarantined exceeds 2% of house records in a
  cycle, log a prominent warning line (this is the poisoned-mirror alarm).
- `docs/scheduling.md`: how to register it with Windows Task Scheduler
  (schtasks example) as the alternative to the in-process loop.
- Tests: lock exclusivity and stale-lock recovery, failure-isolation between
  sources (monkeypatched ingest raising), tripwire threshold math. No live
  network in tests.

## WO-5 — Web dashboard (agy MAY draft HTML/CSS/JS presentational layer only)
Goal: a human-friendly face on the existing API, zero new dependencies.
Architecture (fixed, not agy's to decide): static files in `web/` served by the
EXISTING api.py server via GET /ui/* (read-only, same 127.0.0.1 bind); vanilla
JS fetches the existing /api/v1 endpoints. No frameworks, no CDN, no npm, no
build step, no cookies, no localStorage, no external requests of any kind.
Pages/views:
- Feed: latest disclosures (paged), each card showing politician, chamber badge,
  ticker, type badge, amount RANGE (never a midpoint), transaction date,
  disclosure date, "filed N days later" chip, link to official source PDF.
- Politician view: aggregates from /api/v1/politicians/{id} (trade count, top
  tickers, total volume rendered as a range), trade table.
- Ticker view: /api/v1/tickers/{symbol}/trades.
- Watchlist view: read-only display of /api/v1/watchlists.
- Global footer disclaimer on every page (the standing not-investment-advice
  text) — non-negotiable, gate-checked.
Dispatch protocol: agy drafts index.html/style.css/app.js against a written
API contract excerpt; gate rejects on: any external resource, any storage API,
any endpoint not in the contract, any amount rendered as a single number, or
missing disclaimer. Server-side wiring (serving /ui/*, path traversal guard —
resolved path must stay inside web/) is CLAUDE-DIRECT.
Tests: /ui/ serves index with correct content-type, traversal attempts (e.g.
/ui/../config.py) return 404, disclaimer string present in served HTML.

## WO-6 — House PDF deep-parse (ADR SPIKE FIRST — no implementation this batch
unless the ADR is approved with headroom remaining)
Goal: decide, with evidence, whether per-transaction detail can be extracted
from official Clerk PTR PDFs under our constraints.
- Sample ~10 recent e-filed PTR PDFs (text-based) via their official URLs.
- Evaluate stdlib-only text extraction honestly (PDFs are compressed; zlib is
  stdlib — is robust extraction feasible, or is this the first justified
  dependency exception?). Paper/scanned filings: out of scope permanently
  (OCR), quantify what fraction of filings that abandons.
- Deliverable: docs/adr-002-house-pdf-parse.md with a recommendation, effort
  estimate, and an explicit "do nothing, mirror suffices" option analyzed.
  STOP after the ADR for CEO sign-off. If a dependency exception is
  recommended, that is a constitutional change to stdlib-only law and requires
  explicit CEO approval with the tradeoff spelled out.

---

## Gate additions for this batch
- WO-4: runner must never fabricate a cycle result; a skipped source is
  reported as skipped.
- WO-5: view-source audit — served HTML/JS contains no external URLs except
  official disclosure links from the data itself.
- All: suite must remain green end-to-end (currently 69 tests) plus new
  coverage; working tree clean after every dispatch.
