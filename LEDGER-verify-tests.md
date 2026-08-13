# Adversarial test-integrity verify — Package S

Read-only review of `notes-s-evidence.md` against the actual test bodies,
stubs, and mutation script on branch `smdc`. No edits made.

## Per-criterion verdicts

1. **S1-RUNS-EACH-CYCLE** — GENUINE. `test_resolve_open_runs_once_per_cycle_via_the_batch_describer`
   (`test_service.py:1651`) asserts on `batch.list_jobs_calls` and
   `conn.submissions[100]["state"] == "found"` — real code-under-test state,
   not the stub's own bookkeeping.

2. **S1-RUNS-WITH-ZERO-OPEN** — GENUINE. `test_the_pass_runs_even_when_zero_attempts_are_open`
   (`:1671`) builds with `rows=[]`, asserts `summary["open"]==0` AND
   `batch.list_jobs_calls` fired. Would fail if the call sat after the
   early return. Matches the mutation check (below).

3. **S1-RAISE-DOES-NOT-KILL** — GENUINE. `test_a_raising_describe_does_not_kill_the_cycle`
   (`:1688`) uses `FakeBatch(list_jobs_raises=...)`, a real refusal-capable
   double, and asserts both `summary["errors"]==1` and that the unrelated
   open attempt still got reconciled (`summary["waiting"]==1`) — exercises
   the try/except in `_resolve_submissions` (`service.py:543`), not a stub
   tautology.

4. **S1-PRE-044-DEGRADES** — GENUINE. `submissions_available=False` (the
   default), asserts zero `list_jobs_calls` — proves `is_available` gates
   the whole pass, not just a log line.

5. **S1-OUTCOMES-IN-SUMMARY** — GENUINE. Asserts `summary["submission_found"]`
   and `summary["submission_lost"]` both land correctly from a mixed pass.

6. **S2-FOUND-WAITS-PAST-HORIZON (headline)** — GENUINE, and mutation-confirmed.
   `test_a_found_submission_waits_however_late_the_clock_is` (`:1777`) sets
   `submitted_at` 4 hours before `now` (horizon is 30 min) and asserts
   `summary["waiting"]==1`, `classified==0`, and the row's
   `lifecycle_state` is UNCHANGED (`"submitted"`, not
   `terminal_without_start`). This is the strongest of the 14 — it directly
   contradicts what the pre-package code would do.

7. **S2-LOST-SKIPS-THE_CLOCK** — GENUINE. `submitted_at` only 5 min old
   (well inside the 30-min horizon) yet LOST state still classifies —
   proves the horizon gate is genuinely bypassed, not just "eventually
   would classify anyway."

8. **S2-NO-ROW-UNCHANGED** — GENUINE regression pin. `submission_id=None`,
   past horizon, classifies exactly as pre-package code — confirms the
   backstop path is untouched.

9. **S2-OPEN-STILL-WAITS** — GENUINE. UNKNOWN state with a future
   `resolution_deadline`, inside horizon → `"waiting"`. Correctly named in
   its own comment as testing `_reconcile_unresolved`'s read, not a race
   with the resolution pass in the same cycle.

10. **S2-REDIRECT-PATH-FOUND / ATTEMPT-RAN-PRESERVED** — GENUINE, not a
    tautology. Verified independently: the redirect test's row has
    `scheduler_job_id="job-abc"` and `lifecycle_state="submitted"`, so
    `poll_once` routes it into `by_job` (`service.py:407-409`), not
    `unresolved`. Two unindexed Batch observations make `_pick_observation`
    return `None` (`service.py:717,737`), which redirects to
    `_reconcile_unresolved` at `service.py:748` — the actual redirect path,
    not a direct call dressed up as one. `ATTEMPT-RAN-PRESERVED` separately
    confirms LOST + full application account → `missing_or_contradictory`,
    not `terminal_without_start` — the `_attempt_ran` distinction genuinely
    exercised through the new branch.

11. **S2-FAILS-OPEN** — WEAK (passes, but the double is not the one that's
    "refusal-capable" by design — see stub finding below). The test
    monkeypatches `conn.route` directly to raise on the submission-join SQL,
    bypassing `FakeConnection`'s declared API. It still exercises the real
    `_submission_classification` except-path and asserts the correct
    fallthrough (`classified==1`, `terminal_without_start`), so the
    assertion is genuine, but the double itself offered no sanctioned way to
    do this — see Stub finding.

12. **S-NEVER-SUBMITS** — GENUINE but weaker than it looks. Asserts
    `not hasattr(batch, "submit_job")` then calls `poll_once()` and expects
    no `AttributeError`. This proves the reconciler's *own* resolution path
    never reaches a submit call in this scenario, but it is not the same
    strength as `test_submission_protocol.py`'s `_FakeBatch.submit_job`,
    which actively raises `AssertionError` on any call — `hasattr` absence
    only catches a call that used the object as given; it would not catch
    code that separately imported a real Batch client and called
    `submit_job` on that instead. Acceptable given `_NEVER-SUBMITS` in the
    contract tier layer already covers the stronger form, but the stub-tier
    criterion by itself is the weakest of the fail-safety checks.

13. **S-DURABLE-SECOND-CONN** — GENUINE. `test_a_resolution_pass_is_visible_from_a_second_connection`
    (`test_submission_protocol.py:405`) uses the real `second_conn` fixture
    against real PostgreSQL — commits on `conn`, reads on `second_conn`.
    This is a real durability proof, not a stub artifact.

14. **Regression / zero-skip** — GENUINE. See zero-skip verdict below.

## Stub / double refusal-capability verdict

**`FakeBatch`** (stubs.py:16) — GENUINE refusal capability: `list_jobs_raises`
lets a test make Batch unreachable (used by criteria 3, 11's neighbor
scenarios). `describe_jobs` has no raise path but nothing in package S
routes through it. Adequate.

**`FakeConnection`/`FakeCursor`** (stubs.py:158) — **WEAK / gap**. Unlike
`FakeBatch`, `FakeConnection.route()` has **no built-in way to raise** on
any branch — `_select_attempts`, `_select_submission_for_attempt`,
`_select_open_submissions`, `_update_submission` all either return rows or
`None`; none can be told to fail a query. This is a real asymmetry against
the project's own refusal-capable-double rule cited in the brief
(§5: "Test doubles must be refusal-capable... The fakes here must be able
to... raise"). The evidence claims this rule was followed, but only
`FakeBatch` actually got the treatment; `FakeConnection` did not.

Criterion 11 (`test_a_raising_submission_lookup_falls_through_to_the_horizon`,
`:1909`) works around the gap by monkeypatching `conn.route` in the test
body itself rather than the fake exposing a declared `submission_lookup_raises`
parameter. This still exercises the real except-path in
`_submission_classification` (verified: the raise happens inside the real
`route()` call chain that `_Executor`/`submission_for_attempt` invoke), so
the assertion is not hollow — but it means the "database read fails" shape
is proven only for this one criterion, ad hoc, rather than being a reusable,
declared double capability the way `list_jobs_raises` is. A later test
that wants a `_select_open_submissions` or `_update_submission` raise has
no sanctioned way to get one. This is a **defect of omission**, not a
defect that invalidates criterion 11's own result.

## Mutation checks

**Criterion 6 mutation** — GENUINE. `sed` replaces
`classification == submission_protocol.FOUND` with a string that can never
match (`'__mutated_out__'`), which forces the FOUND branch dead without
touching any other logic. Confirmed the sed target string exists verbatim
at `service.py:1645`. This is a clean, single-purpose mutation.

**Criterion 2 mutation** — GENUINE, and the "delete is equivalent to move"
claim holds up under inspection. The `sed` command
(`/self\._resolve_submissions(summary)/d`) deletes the call at
`service.py:395`, which sits immediately before `if not rows:` at line 396.
Deleting the line and moving it below the early-return are behaviorally
identical for what criterion 2 measures (whether the pass runs on the
zero-rows path) — in both cases the call does not execute when `rows` is
empty. It is a *weaker* mutation than "move" only in the sense that it
also removes the call from the non-empty-rows path, but criterion 2's test
(`test_the_pass_runs_even_when_zero_attempts_are_open`) builds with
`rows=[]` specifically, so that difference is not observable by this test
and does not weaken what's being proven here.

**Restore verification** — GENUINE. `sha256sum` of `service.py` is taken
before any mutation and re-checked after both mutations are applied and
reverted (`mutation-brief-s-on-rapid-admin.sh:26,72`); each `run_mutation`
call restores from its own `.bak` immediately after the pytest run
regardless of pass/fail, so the checksum gate is a real end-to-end proof
the file is unmodified, not merely an assertion that a restore step exists.
The "matched nothing" guard (`diff -q` before running, line 33) also
correctly prevents a silently-inert sed from reporting a false pass.

**Both mutations are true single-purpose reverts** — confirmed no other
test in the selected suite (`85 deselected`) shares the mutated line, so a
red result is attributable to the intended criterion.

## Zero-skip verdict

GENUINE, not merely claimed. `contract-brief-s-on-rapid-admin.sh:134`
computes `pass_skips` via `grep -cE '^SKIPPED'` against the actual pytest
output log and fails the run (`pass_rc=1`) on any nonzero count — this is
an enforced gate, not a report of a number. Per-criterion selections
(lines 176-193) pass `-k <exact test name>` and `-m <not live|contract>`
as single quoted fields via the `name:target:kexpr:marker` colon-split
idiom, avoiding the word-splitting bug the comment says a prior brief hit.
I independently confirmed each of the 18 `-k` substrings names a test that
actually exists in `test_service.py` or `test_submission_protocol.py`
(grep-verified above) — none of the 18 selections is selecting a
nonexistent name that would exit 5 while looking selected. The "1 passed,
85 deselected" / "1 passed, 10 deselected" shapes are internally
consistent with one test matching out of the stub-tier (test_service.py,
~86 tests) and contract-tier (test_submission_protocol.py, ~11 tests)
files respectively.

## Coverage gaps (not tested, arguably should be)

- **Concurrent polls / advisory lock interaction with `_resolve_submissions`**
  — untested. `_resolve_submissions` runs before the lease/lock machinery
  that guards individual attempt rows; no test exercises two overlapping
  `poll_once` calls both attempting a resolution pass, so any race in
  `resolve_open`'s own per-row CAS (`_update_submission`'s WHERE-state
  guard) is only proven by the CAS's contract-tier design, not by an
  integration test at the reconciler layer.
- **`_safe_rollback` call count/behavior during `_resolve_submissions`'s
  failure path** — the test (`test_a_raising_describe_does_not_kill_the_cycle`)
  checks the *effect* (no write, errors counted) but never asserts
  `conn.rollbacks` incremented, so a future refactor that dropped the
  rollback call but happened to leave the DB unwritten for other reasons
  would not be caught here.
- **`_reconcile_attempt`'s non-redirect paths interaction with S2** —
  only the redirect-to-unresolved path is tested; ordinary paired
  observations that also happen to have a submission_id attached are not
  tested for whether S2's classification logic is correctly *not*
  consulted there (it shouldn't be, per the design, but there's no
  regression pin for "a resolvable attempt with a FOUND submission record
  still uses the observation, not the submission state").
- **Multiple open submissions resolving in the same pass with mixed FOUND/LOST/UNKNOWN feeding back into the SAME poll's `_reconcile_unresolved` calls** — S1 and S2 are tested independently (S1's tests check `summary["submission_found"]` etc.; S2's tests pre-seed `submissions` state directly rather than letting S1's own pass resolve it first). No test proves that a submission resolved to FOUND by *this same cycle's* S1 pass is correctly read as FOUND by *this same cycle's* S2 classification — the two halves are unit-tested but not proven to compose within one `poll_once` call.

## Bottom line

13 of 14 criteria are GENUINE with real, non-tautological assertions
against actual code paths (confirmed by direct code reading, not test
inspection alone). Criterion 11 is WEAK only in that its fault-injection
method (monkeypatching `conn.route`) is ad hoc rather than a declared
double capability — the assertion itself is genuine. Both mutation checks
are real and the restore verification is sound. The zero-skip claim is
enforced, not decorative, and independently checked against the actual
test names. The most consequential real gap is `FakeConnection`'s total
lack of a declared raise capability, which is a latent defect risk for any
*future* test needing a database-read failure shape, even though it did
not invalidate anything claimed for this package.
