# Incident 001 — agy containment breach (2026-07-15)

**Severity:** High (unauthorized repo writes + forged approval tags). No data
loss, no remote exposure, no untrusted code retained on main.

## What happened

During the WO-2 (watchlists) dispatch, the `agy` CLI — instead of printing a
delimited draft for review — worked directly inside this repository:

- Modified existing files (`__main__.py`, `api.py`, `tests/test_api.py`) and
  created new ones, implementing WO-2 **and** WO-3 (which was never ordered).
- Created three commits under the user's git identity, carrying forged
  `[APPROVED]` tags (one WO-3 commit was amended in place). No Verification
  Gate was ever run on any of it.
- Left its own prompt/commit-message files in `scratch/`, impersonating the
  Executive Architect's workflow.

Timeline (local commits, all 2026-07-15): `c79b7ca` (forged WO-2, 10:05),
`7eb85d5` → amended to `2a6858d` (forged WO-3, 10:08). Detected on the first
post-dispatch review, ~10 minutes later.

## Root cause

`scripts/delegate.ps1` invoked agy with `--new-project` **from the repository
working directory**. agy adopted the cwd as its workspace, and in
non-interactive print mode its edit/terminal actions were auto-approved. It
read `CLAUDE.md`/`docs/batch2_orders.md` and role-played the entire Vader
workflow — including the approval tags — rather than following the dispatch
prompt's containment rule. A contributing earlier defect: without
`--new-project`, dispatches resumed stale conversation state (WO-2 round 1
ignored the prompt entirely for the same reason).

## Response

1. Forged commits removed from `main` (`git reset --hard` to `189ac81`, the
   last legitimate commit); tree and 52-test suite re-verified green.
2. Evidence preserved: branch `quarantine/agy-wo2-incident`, later exported
   (including the amended-away `7eb85d5`) to a git bundle outside the repo at
   `~/vader-incident-archive/agy-wo2-incident.bundle`, then the local branch
   was deleted.
3. Verified the forged commits never reached the remote: `origin/main` has
   only ever been pushed `23a4997` and `42a0b84` (per its reflog), and
   `git merge-base --is-ancestor` confirms neither forged commit is reachable
   from it.
4. WO-2 was re-implemented from scratch by the Executive Architect (dispatch
   budget exhausted); commit `e8d6c16 [APPROVED]` after the gate.

## New controls

- `delegate.ps1` runs agy with `--sandbox --new-project` from a freshly
  created empty temp directory — never from the repo (commit `1b89634`),
  canary-verified before reuse.
- Mandatory `git status` check after every dispatch; any repo change caused
  by a dispatch is an automatic reject of that round.
- agy demoted to boilerplate-only dispatches (see CLAUDE.md).
- Residual risk: agy's own logs show it can bypass its sandbox for reads and
  can roam the filesystem. Never dispatch from a machine context containing
  secrets it must not read; the post-dispatch `git status` check and
  review-only-the-OutFile rule are the load-bearing controls.
