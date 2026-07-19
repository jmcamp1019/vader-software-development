# Scheduling PelosiTracker ingestion

Two ways to keep the database fresh. Pick one — the lockfile makes overlap
harmless (the second instance exits immediately), but running both is noise.

## Option A — in-process loop

```
python -m pelositracker run --interval-hours 6
```

- Interval precedence: `--interval-hours` flag, then `PT_RUN_INTERVAL_HOURS`
  env var, then the default of 6.
- Each cycle: senate ingest → house ingest → WO-9 shadow scan → digest, one
  structured log line per cycle. A failing source (including the house
  fail-closed path when the official Clerk index is unreachable) or shadow
  scan is logged and the loop continues; the digest still runs, and
  `consecutive_failures=` in the log line tracks unhealthy streaks.
- A `WARNING:` line fires when house quarantines exceed 2% of the cycle's
  officially indexable house records — the poisoned-mirror alarm. Pre-2015
  rows remain quarantined as `legacy_unindexed` but are excluded from both the
  numerator and denominator because the official 2014 bulk archive has no PTR
  coverage. Any unmatched 2015+ row still contributes to the alarm. Treat a
  warning as an incident signal.
- Single instance enforced via `<db>.runner.lock` (PID + heartbeat timestamp,
  rewritten each cycle). A lock with no heartbeat for two intervals is
  considered stale and stolen; delete the file manually only if you are sure
  no runner is alive.
- Stop with Ctrl+C; the lock is released on exit.

## Option B — Windows Task Scheduler (one-shot cycles)

Use `--once` so each trigger runs exactly one cycle and exits; the lockfile
still guards against overlap if a cycle outlives the schedule interval.

```
schtasks /Create /TN "PelosiTracker Ingest" ^
  /TR "\"C:\Path\To\python.exe\" -m pelositracker run --once" ^
  /SC HOURLY /MO 6 /ST 06:00 /F
```

Notes:
- Set the task's "Start in" directory to the repo root (or wherever
  `pelositracker.db` and `digests/` should live), e.g. via
  `schtasks /Create ... /RU <user>` plus editing the task in the UI, or
  create the task pointing at a small .cmd wrapper that does
  `cd /d C:\path\to\repo` first. The runner resolves the database and the
  digests output directory relative to its working directory.
- If the package is not pip-installed, the wrapper must also set
  `PYTHONPATH=src`.
- Inspect runs: `schtasks /Query /TN "PelosiTracker Ingest" /V /FO LIST`.
- Remove: `schtasks /Delete /TN "PelosiTracker Ingest" /F`.

## Log line format

```
2026-07-19T16:53:29+00:00 senate ok inserted=0 duplicates=8030 skipped=0 quarantined=0 legacy_unindexed=0 | house ok inserted=0 duplicates=22676 skipped=160 quarantined=839 legacy_unindexed=839 | shadow not-started examined=0 appended=0 rejected_backfills=0 | digest new=0 | consecutive_failures=0
```

A skipped source is reported as `<name> skipped`, a failed one as
`<name> FAILED (<reason>)` — a cycle result is never fabricated. The shadow
segment reports `not-started`, `active`, `completed`, or `FAILED` separately.
