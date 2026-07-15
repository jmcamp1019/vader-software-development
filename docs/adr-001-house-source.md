# ADR-001: House Trade Data Source

- **Status:** ACCEPTED with amendment (CEO ruling, 2026-07-14)
- **Date:** 2026-07-14
- **Author:** Executive Architect (WO-0, Batch 2)

## Context

`HOUSE_ALL_TRANSACTIONS_URL` (the house-stock-watcher S3 bucket) returns
**HTTP 403** on anonymous access, so `ingest --source house` is dead. The Senate
side had the same failure and was fixed by switching to the upstream project's
GitHub mirror (commit 42a0b84). WO-0 asks for an investigation of replacement
sources and a primary + fallback recommendation.

All probe results below were verified live on 2026-07-14.

## Options investigated

### 1. housestockwatcher.com API (same maintainer as the dead bucket) — DEAD

- `housestockwatcher.com` does not resolve at all (DNS NXDOMAIN), so the `/api`
  health probe cannot even connect. The domain registration has lapsed or been
  dropped; combined with the S3 403, the original project is abandoned.
- **Verdict: not viable.**

### 2. Community successor mirror: `TattooedHead/house-stock-watcher-data` (GitHub)

A new open-data project (explicitly created because the original died):
`https://raw.githubusercontent.com/TattooedHead/house-stock-watcher-data/main/data/all_transactions.json`

- **Freshness:** repo created 2026-05-29, last push 2026-07-14 (today); newest
  trade rows carry `disclosure_date` 07/09/2026. Actively maintained.
- **Schema (verified by sampling):** `transaction_date`, `disclosure_date`,
  `ticker`, `asset_description`, `asset_type`, `type`, `amount` (official range
  string, e.g. `"$1,001 - $15,000"`), `representative`, `district`, `owner`,
  `filing_id`, `source_url` (points at the official Clerk PTR PDF). This is
  nearly identical to the old house-stock-watcher feed, so the existing
  normalizer pattern applies with minimal changes, and every trade natively
  carries the three required fields (transaction_date, disclosure_date,
  source_url).
- **Gate hazard:** rows include an `amount_mid` field (a midpoint, e.g. `8000`).
  This MUST be ignored; we parse only the `amount` range string into min/max
  cents (open max = NULL), per Verification Gate rule 2.
- **Data quality:** it scrapes the Clerk's PTR PDFs, and extraction is imperfect —
  sampled rows show null-byte/control-character artifacts bleeding into
  `asset_description`, and the repo ships a 5 MB `jammed_rows.jsonl` of rows its
  own pipeline could not parse cleanly. Ingest must validate and reject
  malformed rows rather than trust the feed.
- **Sustainability risk:** single anonymous maintainer, 1 star, six weeks old.
  Same bus-factor failure mode that killed the original project.
- **Cost/keys:** none. Same access pattern as our Senate source.

### 3. Official House Clerk downloads (disclosures-clerk.house.gov)

- **Format:** yearly bulk ZIP at
  `https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{YEAR}FD.zip`
  (verified HTTP 200 for 2025 and 2026). Each ZIP holds a TSV + XML **filing
  index** (Name, FilingType, StateDst, FilingDate, DocID). 2026 index: 1,364
  filings, of which 295 are FilingType `P` (periodic transaction reports).
  Individual filings live at
  `https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{YEAR}/{DocID}.pdf`
  (verified 200, `application/pdf`).
- **Effort to parse:** the index gives filer, filing (disclosure) date, and the
  source PDF URL — cheap to ingest with stdlib. But the actual **transactions
  are inside the PDFs**: electronically-filed PTRs are text PDFs (a stdlib-only
  extractor via `zlib` FlateDecode is possible but a substantial, brittle
  effort), and paper filings are scanned images (OCR — not feasible stdlib-only).
- **Access quirk:** the site returns 403 to non-browser user agents (our probe
  succeeded only with a browser UA string). The fetcher's UA handling would
  need attention, and we should stay polite/low-volume.
- **ToS/legal:** these are public records mandated by the Ethics in Government
  Act / STOCK Act. The statute restricts *use* of the data (e.g. for commercial
  solicitation, credit determinations); PelosiTracker's informational,
  non-advice purpose is compatible. No API key, no cost.
- **Verdict: authoritative and free; index ingest is easy, full transaction
  parsing is a large future effort.**

### 4. Keyed API: Financial Modeling Prep (`/stable/house-trading`, `/stable/house-latest`)

- Documented endpoints exist and the API is live (a bad key returns a clean
  "Invalid API KEY" JSON error). Pagination via `page`/`limit`.
- **Cost/limits:** free tier is 250 requests/day with a 500 MB bandwidth cap;
  paid tiers raise limits. **Unverified:** whether the congressional-trading
  endpoints are included in the free tier or gated to a paid plan — FMP's docs
  and pricing pages block automated retrieval, and confirming requires creating
  an account. Also unverified: whether amounts are preserved as ranges; if FMP
  collapses them to a single number it fails Gate rule 2 outright.
- **Constraint (from WO-0):** any keyed option requires an `HOUSE_API_KEY` env
  var and CEO sign-off before implementation. Adds a third-party commercial
  dependency to a project whose other sources are all free public feeds.
- **Verdict: viable contingency, not a first choice.**

## Decision (CEO ruling, 2026-07-14)

- **Primary: the community GitHub mirror (`TattooedHead/house-stock-watcher-data`).**
  It restores `ingest --source house` with the least new code (mirrors the
  Senate fix exactly), needs no key, and its rows natively satisfy the gate's
  required fields. Implementation conditions:
  1. Parse only the `amount` range string; `amount_mid` is ignored entirely
     (never read, never stored, never hashed).
  2. Reject rows containing control characters or unparseable
     dates/amounts/tickers (the feed demonstrably contains them).
  3. Keep `source_url` as the official Clerk PDF the mirror provides, so every
     trade remains traceable to the government record.

- **AMENDMENT — the Clerk index is an integrity anchor, not just a freshness
  check.** On every house ingest, also fetch the official Clerk `{YEAR}FD.zip`
  filing index (using a browser-like User-Agent, since the site 403s
  non-browser agents) for every filing year present in the mirror feed. A
  mirror trade is inserted **only if** its filing/PTR reference (the mirror's
  `filing_id`, falling back to the DocID in its `source_url`) matches a DocID
  in the official index. Non-matching trades are **quarantined**: counted and
  reported in the ingest summary, never inserted. If the Clerk index cannot be
  fetched at all, the house ingest **fails closed** — nothing is inserted.
  A missing index for one specific year (HTTP 404) quarantines that year's
  trades without aborting the rest.

- **AMENDMENT — provenance tracking.** Every trade row records which source
  feed produced it (e.g. `house-stock-watcher-mirror`,
  `senate-stock-watcher-github`, `fixtures`), so that if a community mirror is
  ever compromised, purging everything it contributed is a single-statement
  operation. Added as an additive migration; pre-existing rows are backfilled
  by chamber (source and chamber were 1:1 before this ADR).

- **Fallback: the official House Clerk yearly ZIP index** (already fetched on
  every ingest as the anchor). If the mirror goes stale or dies, the Clerk
  filing index (filer, disclosure date, PTR PDF link) is authoritative and
  stdlib-parseable today. Full PDF transaction extraction is deferred to a
  future work order if ever needed.

- **Rejected for now: FMP keyed API.** Cost/tier and range-preservation are
  unverified, it requires a key and CEO sign-off, and two free sources cover
  the need. Revisit only if both free options fail.

## Consequences

- `config.py` gains a `HOUSE_ALL_TRANSACTIONS_URL` pointing at the GitHub raw
  mirror (replacing the dead S3 URL), the Clerk index URL template, and a
  browser-like User-Agent used only for the Clerk endpoint; no new
  dependencies, no secrets.
- New `clerk.py` module fetches and parses the official index; the pipeline
  gains a quarantine path and per-source provenance; `db.py` gains an additive
  `provenance` column migration and a purge helper.
- Every house ingest costs one extra small HTTP request per filing year
  (~50–100 KB each) against the official Clerk endpoint.
- The normalizer gains row-validation guards (artifact rejection) — slightly
  lower row yield, in exchange for gate-clean data.
- We accept a bus-factor risk on the mirror, mitigated by the Clerk anchor
  (which now also bounds the damage a compromised mirror could do: it cannot
  invent filings that the official index does not corroborate, and its
  contributions are trivially purgeable via provenance).
