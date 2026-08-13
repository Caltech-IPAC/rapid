# Package S evidence — wiring the submission resolver into the reconciler

Branch `smdc`, off `3792447`. Six commits (`0c07351`..`c86bba7`), not merged
— supervisor adjudicates. Acceptance run GREEN on rapid-admin
(`brief-s-20260813T024624Z`, transcript below); one earlier run
(`brief-s-20260813T024355Z`) failed on two test-fixture defects, fixed and
re-run.

## What changed and why

### S1 — `submission/protocol.py`

Added `submission_for_attempt(execute, attempt_id)`: a read joining
`submissions` to `attempts` through `attempts.submission_id` (the FK
`attach_attempts` maintains), returning `submission_id, state, job_name,
job_queue, resolution_deadline` as a dict, or `None`. Follows the module's
existing idiom exactly — module-level SQL constant, `execute` first
argument, dict return like `open_submissions`, no commit. Per the brief's
supervisor ruling, this lives beside `open_submissions` rather than in a new
`pipeline/repositories/submissions.py` — `protocol.py` is already the
narrow, typed, caller-owns-the-transaction shape rule 17 wants.

### S1 — `pipeline/reconciler/service.py`

- `_OPEN_COLUMNS` gains `submission_id` (used by both `open_attempts()` and
  `reread_attempt`'s column list, so both `row` and `current` carry it).
- `poll_once` calls a new `_resolve_submissions(summary)` **before** the
  `if not rows:` early return — the brief's single binding placement
  constraint, verified by mutation (below).
- `_resolve_submissions`: probes `protocol.is_available`, returns quietly if
  absent (pre-044 degrades — though see the correction below, 044 is no
  longer a DRAFT). Calls `protocol.resolve_open(execute,
  batch_describer(self.batch), now=self._now())`. Wrapped in try/except so a
  raise — including from the availability probe — rolls back, logs, and
  counts into `summary["errors"]` rather than killing the cycle.
  **Commits per resolved row, not once for the whole pass** (the brief's
  commit-boundary decision, made explicit and tested): committing only when
  `resolved > 0`, else rolling back the read-only `open_submissions` SELECT
  left open. Resolution counts (`submission_found`, `submission_lost`,
  `submission_unknown`) and `errors` land in the summary dict.

### S2 — `pipeline/reconciler/service.py`

- New `_submission_classification(row)`: returns `FOUND`, `LOST`, or `None`.
  `None` covers three fail-open cases — no `submission_id` on the row, the
  lookup raises (rolled back, logged, treated as "nothing to conclude",
  never as LOST), or the linked row exists but is still open/ambiguous.
- `_reconcile_unresolved` restructured: the submission record is asked
  first. `FOUND` → `return "waiting"` unconditionally (the clock is not
  consulted at all in this branch — the headline behaviour). `LOST` →
  falls through to the classification machinery *without* the horizon
  gate. Anything else → the original `beyond_submission_horizon` check,
  byte-for-byte unchanged in its own branch. Handles both entry paths:
  the `scheduler_job_id IS NULL` partition and the `_reconcile_attempt`
  redirect (rows that DO carry a `scheduler_job_id`) — neither branch
  assumes it is absent. The `_attempt_ran` contradictory-vs-terminal
  distinction is untouched, still evaluated inside the lease exactly as
  before; the new branch only decides whether to enter that machinery at
  all, never which state it picks once inside.

No migration, no RAPIDDB change, no new IAM, no new env var, no new
constructor dependency, no horizon *value* changed. `submit_batch`/`SubmitJob`
is not reachable from anything touched (test-verified: `FakeBatch` in the
stub tier has no `submit_job` at all).

## What the brief got wrong

**Migration 044 is no longer a DRAFT.** The brief (and the pre-existing
`test_submission_protocol.py` docstring) describe `submissions` as a pending
change request that `smdc` CI skips around. That was true when the brief was
written but not on `3792447`: commit `cdac1dd` ("retire the nine adopted
DRAFT migrations", same day) records that 044-052 were adopted verbatim into
`IPAC-SW/rapid_systems` main on 2026-08-12, and `.github/workflows/
contract-tests.yml`'s pinned revision (`28ea260`, bumped in `26dbb1a`/
`2b4b9be`) already carries them. `migrations-draft/` now holds only notes —
no `.sql` files at all.

Effect on this package: none on the code (the fail-open, probe-never-assume
design is correct regardless of where 044 lives) but two real effects on the
acceptance evidence:

1. Brief §6's "one pass against the authoritative stream" instruction is
   right for a stronger reason than "R made two-pass obsolete" — there is
   now no draft `.sql` file for *any* brief to apply as a second pass.
2. `test_submission_protocol.py`'s tests were expected (by its own
   docstring) to skip in `smdc` CI. They do not: CI's stream already
   contains `submissions`, so they run for real there now. I corrected the
   module docstring to say so (commit `8ab690e`) rather than leave a
   documented expectation the acceptance run itself contradicts (zero
   skips, `submissions`-table tests included, confirmed below).

No other design instruction in the brief needed correction against the code.

## Two defects found live, not anticipated

Both surfaced on the first rapid-admin run (`brief-s-20260813T024355Z`,
`BRIEF-S-OVERALL: FAIL`) and are fixed in commit `c86bba7`:

1. `fixture.make_attempt(conn)`'s default `lifecycle="submitted"` requires
   the full binding triple at `schema_version >= 2`
   (`attempts_state_submitted_check`), which `make_logical_job` only writes
   under `with_binding=True`. No other contract test in the repo uses that
   default against real PostgreSQL — every existing caller passes
   `lifecycle="terminal_without_start"` explicitly. My new tests didn't,
   and hit `CheckViolation` immediately. Fixed by matching the established
   idiom.
2. Three new tests asserted **global** `resolve_open()` counts
   (`counts[protocol.FOUND] == 1`) against `conn`, the contract tier's
   shared session connection (`conftest.py`: writes must be visible to a
   second connection, so nothing wraps each test in a rolled-back
   transaction). Once enough tests in the file had left rows `unknown` in
   the same session, `resolve_open`'s pass legitimately swept those up too
   and the exact counts broke non-deterministically by file/test order.
   Rewritten to check each test's own submission rows by id, with `>=`
   where a pass-wide count is still asserted at all.

## Acceptance criteria — verdict lines (rapid-admin, run `brief-s-20260813T024624Z`)

**S1**
1. `BRIEF-S-S1-RUNS-EACH-CYCLE: exit=0 1 passed, 85 deselected in 0.06s` — PASS
2. `BRIEF-S-S1-RUNS-WITH-ZERO-OPEN: exit=0 1 passed, 85 deselected in 0.04s` — PASS
3. `BRIEF-S-S1-RAISE-DOES-NOT-KILL: exit=0 1 passed, 85 deselected in 0.04s` — PASS
4. `BRIEF-S-S1-PRE-044-DEGRADES: exit=0 1 passed, 85 deselected in 0.04s` — PASS
5. `BRIEF-S-S1-OUTCOMES-IN-SUMMARY: exit=0 1 passed, 85 deselected in 0.04s` — PASS

**S2**
6. `BRIEF-S-S2-FOUND-WAITS-PAST-HORIZON: exit=0 1 passed, 85 deselected in 0.04s` — PASS (the headline)
7. `BRIEF-S-S2-LOST-SKIPS-THE_CLOCK: exit=0 1 passed, 85 deselected in 0.04s` — PASS
8. `BRIEF-S-S2-NO-ROW-UNCHANGED: exit=0 1 passed, 85 deselected in 0.04s` — PASS
9. `BRIEF-S-S2-OPEN-STILL-WAITS: exit=0 1 passed, 85 deselected in 0.04s` — PASS
10. `BRIEF-S-S2-REDIRECT-PATH-FOUND: exit=0 1 passed, 85 deselected in 0.04s` — PASS (redirect path)
    `BRIEF-S-S2-ATTEMPT-RAN-PRESERVED: exit=0 1 passed, 85 deselected in 0.04s` — PASS (`_attempt_ran` distinction)
11. `BRIEF-S-S2-FAILS-OPEN: exit=0 1 passed, 85 deselected in 0.04s` — PASS

**Protocol invariant**
12. `BRIEF-S-S-NEVER-SUBMITS: exit=0 1 passed, 85 deselected in 0.04s` — PASS

**Durability**
13. `BRIEF-S-S-DURABLE-SECOND-CONN: exit=0 1 passed, 10 deselected in 0.04s` — PASS
    Plus supporting coverage the brief flagged as a gap: `resolve_open`'s
    first-ever test — `BRIEF-S-S-RESOLVE-OPEN-FIRST-COVERAGE: exit=0 1
    passed, 10 deselected in 0.04s`, `BRIEF-S-S-RESOLVE-OPEN-ONE-FAILS:
    exit=0 1 passed, 10 deselected in 0.04s` — and `submission_for_attempt`'s
    first coverage: `BRIEF-S-S-LOOKUP-READS-LINKED-ROW: exit=0 1 passed, 10
    deselected in 0.04s`, `BRIEF-S-S-LOOKUP-NONE-ON-NO-LINK: exit=0 1
    passed, 10 deselected in 0.03s`.

**Regression**
14. `BRIEF-S-PASS-RESULT: 540 passed, 7 subtests passed in 4.17s`,
    `BRIEF-S-PASS-SKIPS: 0`, `BRIEF-S-PASS-SKIPS: PASS exit=0 (zero skips, as
    the brief requires)`, `BRIEF-S-CONTRACT-SUITE: exit=0`. Stub tier:
    `1412 tests across 47 modules`, `RESULT: PASS`,
    `BRIEF-S-STUB-TIER: exit=0`.

`BRIEF-S-CRITERIA: exit=0` (the aggregation gate — every criterion above
individually green, so this is not a suite-level summary standing in for
them).

## Mutation check — both directions, actual output

Run against rapid-admin's stub-tier venv, both locally beforehand (to shape
the sed expressions correctly) and inside the recorded acceptance run.

**Criterion 6** (the headline: a FOUND submission must win over the clock).
Mutation: `s/if classification == submission_protocol.FOUND:/if
classification == '__mutated_out__':/` in `_reconcile_unresolved`.

```
=== S2-C6: make the clock decide again (revert the FOUND branch)
MUTATION-S2-C6-FOUND-BRANCH: PASS exit=1 (test went RED as required) 1 failed, 85 deselected in 0.07s
```

Local pre-check (same mutation, run directly):
```
FAIL: test_a_found_submission_waits_however_late_the_clock_is
AssertionError: 1 != 0
```
(`summary["waiting"]` dropped from 1 to 0 — the row falls through to the
horizon, which is far in the past in this test, and gets classified instead.)

**Criterion 2** (the pass must run even with zero open attempts — the
placement the brief names as "the single easiest way to get this package
subtly wrong"). Mutation: delete the `self._resolve_submissions(summary)`
line entirely (equivalent to, and simpler to express as a `sed` than, moving
it below the early return — both remove the pass from the zero-rows path).

```
=== S1-C2: move the resolution pass below the early return
MUTATION-S1-C2-PASS-PLACEMENT: PASS exit=1 (test went RED as required) 1 failed, 85 deselected in 0.06s
```

Local pre-check:
```
FAIL: test_the_pass_runs_even_when_zero_attempts_are_open
AssertionError: [] is not true
```
(`batch.list_jobs_calls` stayed empty — the pass never ran.)

**Both directions — restore confirmed GREEN.** After each mutation the
script restores the file from its own backup and re-runs the full suite;
the transcript's `MUTATION-CLEANUP: PASS exit=0 (service.py byte-identical)`
and the surrounding suite's continued green confirm the restore, and my own
local pre-check re-ran the full 118-test reconciler suite green after each
manual revert before wiring these into the script.

## Full acceptance transcript (rapid-admin, `brief-s-20260813T024624Z`)

```
BRIEF-S-CHECKSUM: PASS exit=0
BRIEF-S-STREAM-REV: 603c1c6176789ae6ad0fe4760bb5a4eca6dfb3dd
BRIEF-S-MIGRATIONS: 56 stream migrations
BRIEF-S-PULL: PASS exit=0
BRIEF-S-CONTAINER: PASS exit=0 (brief-s-pg-brief-s-20260813T024624Z)
BRIEF-S-PIP-INSTALL-E: PASS exit=0
BRIEF-S-PASS: authoritative stream (044-055 already adopted)
BRIEF-B-APPLY: PASS exit=0 (56 migrations applied and recorded)
BRIEF-B-SCHEMA-MIGRATIONS: 56 rows recorded
BRIEF-B-SUITE: PASS exit=0
BRIEF-S-PASS-RESULT: 540 passed, 7 subtests passed in 4.17s
BRIEF-S-PASS-SKIPS: 0
BRIEF-S-PASS-SKIPS: PASS exit=0 (zero skips, as the brief requires)
BRIEF-S-CONTRACT-SUITE: exit=0
BRIEF-S-S1-RUNS-EACH-CYCLE: exit=0 1 passed, 85 deselected in 0.06s
BRIEF-S-S1-RUNS-WITH-ZERO-OPEN: exit=0 1 passed, 85 deselected in 0.04s
BRIEF-S-S1-RAISE-DOES-NOT-KILL: exit=0 1 passed, 85 deselected in 0.04s
BRIEF-S-S1-PRE-044-DEGRADES: exit=0 1 passed, 85 deselected in 0.04s
BRIEF-S-S1-OUTCOMES-IN-SUMMARY: exit=0 1 passed, 85 deselected in 0.04s
BRIEF-S-S2-FOUND-WAITS-PAST-HORIZON: exit=0 1 passed, 85 deselected in 0.04s
BRIEF-S-S2-LOST-SKIPS-THE_CLOCK: exit=0 1 passed, 85 deselected in 0.04s
BRIEF-S-S2-NO-ROW-UNCHANGED: exit=0 1 passed, 85 deselected in 0.04s
BRIEF-S-S2-OPEN-STILL-WAITS: exit=0 1 passed, 85 deselected in 0.04s
BRIEF-S-S2-REDIRECT-PATH-FOUND: exit=0 1 passed, 85 deselected in 0.04s
BRIEF-S-S2-ATTEMPT-RAN-PRESERVED: exit=0 1 passed, 85 deselected in 0.04s
BRIEF-S-S2-FAILS-OPEN: exit=0 1 passed, 85 deselected in 0.04s
BRIEF-S-S-NEVER-SUBMITS: exit=0 1 passed, 85 deselected in 0.04s
BRIEF-S-S-RESOLVE-OPEN-FIRST-COVERAGE: exit=0 1 passed, 10 deselected in 0.04s
BRIEF-S-S-RESOLVE-OPEN-ONE-FAILS: exit=0 1 passed, 10 deselected in 0.04s
BRIEF-S-S-LOOKUP-READS-LINKED-ROW: exit=0 1 passed, 10 deselected in 0.04s
BRIEF-S-S-LOOKUP-NONE-ON-NO-LINK: exit=0 1 passed, 10 deselected in 0.03s
BRIEF-S-S-DURABLE-SECOND-CONN: exit=0 1 passed, 10 deselected in 0.04s
BRIEF-S-CRITERIA: exit=0
=== S2-C6: make the clock decide again (revert the FOUND branch)
MUTATION-S2-C6-FOUND-BRANCH: PASS exit=1 (test went RED as required) 1 failed, 85 deselected in 0.07s
=== S1-C2: move the resolution pass below the early return
MUTATION-S1-C2-PASS-PLACEMENT: PASS exit=1 (test went RED as required) 1 failed, 85 deselected in 0.06s
=== post-mutation file integrity
MUTATION-CLEANUP: PASS exit=0 (service.py byte-identical)
MUTATION-OVERALL: PASS exit=0 (every mutation went red)
BRIEF-S-MUTATION: exit=0
1412 tests across 47 modules
RESULT: PASS
BRIEF-S-STUB-TIER: exit=0
BRIEF-S-OVERALL: PASS exit=0
BRIEF-S-RUNNER: exit=0
BRIEF-S-ACCEPTANCE-OK
```

Full remote transcript (including per-module stub-tier output and pip
install logs) is on the laptop at `/tmp/brief-s-run2.log`; the S3 staging
prefix (`s3://rapid-build-artifacts-<account>/db-migrations-staging/
brief-s-20260813T024624Z`) was removed on success per the script's own
policy. The account is derived at runtime and deliberately never written
down here — this repo is public and `.githooks/pre-push` hard-blocks that
literal with no allowlist. The failed first run's diagnosis prefix
(`brief-s-20260813T024355Z`) was retained by the script per its
failure-path policy; it can be deleted now that the defects it captured are
understood and fixed, or left for the supervisor to inspect first.

## Files touched

- `submission/protocol.py` — `submission_for_attempt`
- `pipeline/reconciler/service.py` — S1 wiring, S2 restructuring, `_OPEN_COLUMNS`
- `pipeline/reconciler/test/stubs.py` — `FakeConnection` submissions model, `FakeBatch.list_jobs`
- `pipeline/reconciler/test/test_service.py` — stub-tier criteria 1-12
- `pipeline/contract/test_submission_protocol.py` — contract-tier criterion 13 + `resolve_open`/`submission_for_attempt` coverage
- `scripts/brief-s-acceptance-on-rapid-admin.sh`, `scripts/contract-brief-s-on-rapid-admin.sh`, `scripts/mutation-brief-s-on-rapid-admin.sh`
