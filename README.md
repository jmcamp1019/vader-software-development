# PelosiTracker

Ingests public STOCK Act congressional trade disclosures (House and Senate Stock
Watcher S3 datasets) into SQLite. Python 3.12, standard library only - nothing to
install.

## Quick start
```
python -m unittest discover -s tests -v      # run the test suite
python -m pelositracker ingest --source fixtures   # smoke-test with offline fixtures
python -m pelositracker ingest --source house      # pull live House data
python -m pelositracker ingest --source senate     # pull live Senate data
python -m pelositracker stats                       # database summary
```

## Design notes
- Disclosed amounts are ranges; stored as integer-cent min/max, open-ended max = NULL.
  They are never collapsed to point estimates anywhere in the pipeline.
- Every trade stores transaction_date, disclosure_date (filings can lag 45 days),
  and a source_url back to the original PTR.
- Ingestion is idempotent via a unique sha256 ingest_hash - re-runs are safe.
- Malformed feed rows are skipped and counted, never silently dropped.

## Known Issues
- The house-stock-watcher S3 bucket (`house-stock-watcher-data.s3-us-west-2.amazonaws.com`)
  currently returns `403 Forbidden` on direct/anonymous access. `ingest --source house`
  detects this and exits with a clear message instead of a traceback; there is no
  workaround yet since no maintained mirror was found.
- The senate-stock-watcher S3 bucket has the same issue, so `ingest --source senate`
  now pulls from the upstream project's GitHub mirror
  (`raw.githubusercontent.com/timothycarambat/senate-stock-watcher-data`) instead.
  That mirror's flat `all_transactions.json` has no `disclosure_date` field at
  all, so senate ingestion uses the per-filing `all_daily_summaries.json` feed
  instead: each filing carries `date_recieved` (used as disclosure_date) and a
  nested `transactions` array, flattened by `normalize_senate_filing`.

## Disclaimer
This project displays public disclosure data for informational purposes only.
It is not investment advice.

Built by Vader Software Development.
