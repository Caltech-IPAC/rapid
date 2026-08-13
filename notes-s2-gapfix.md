# Closing three verifier-found test gaps — package S

Two commits on `smdc`, off `0922242` (package S's evidence-notes commit):
`2fff9b3` (stub capability), `fc2bc31` (the three gap closures). No
production logic touched — `pipeline/reconciler/service.py` and
`submission/protocol.py` are unchanged by either commit (confirmed via
`git show --stat`).

## What changed

**Gap 2 (`stubs.py`)** — `FakeConnection` gains `route_raises`, a declared
`{branch: exception}` capability mirroring `FakeBatch.list_jobs_raises`.
`route()`'s four SELECT/UPDATE dispatches (`select_attempts`,
`submission_for_attempt`, `select_open_submissions`, `update_submission`)
each check it via a new `_maybe_raise(branch)` helper before answering.
Criterion 11's test (`test_a_raising_submission_lookup_falls_through_to_the_
horizon`) now passes `route_raises={"submission_for_attempt": RuntimeError(...)}`
to `build()` instead of monkeypatching `conn.route` in the test body.
Assertions unchanged.

**Gap 3 (`test_service.py`)** — `test_never_calls_submit_job_reaching_this_
path` no longer just asserts `not hasattr(batch, "submit_job")`. Added a
module-level `_refusing_submit_job(**kwargs)` that raises `AssertionError`
unconditionally (the same shape `test_submission_protocol.py`'s
`_FakeBatch.submit_job` uses), assigned onto the fake as
`batch.submit_job = _refusing_submit_job` before `poll_once()`. Any submit
attempt through this client is now a hard failure, not an absence check.

**Gap 1 (`test_service.py`, new class `S1FeedsS2WithinOneCycleTests`)** —
two tests proving S1 and S2 compose within one `poll_once` call:

- `test_s1s_own_found_resolution_feeds_s2_within_the_same_cycle`: the
  submission starts `state="calling"` (no pre-seeded `found`), the linked
  attempt has `scheduler_job_id=None` and `submitted_at` far past the
  horizon. `_resolve_submissions` (runs first in `poll_once`) resolves it
  to FOUND via `FakeBatch.named_jobs`; the same cycle's `_reconcile_
  unresolved` then reads that FOUND state and returns `"waiting"`, not
  classified.
- `test_s1s_own_lost_resolution_feeds_s2_within_the_same_cycle`: the LOST
  counterpart — submission starts `state="unknown"` with a deadline already
  past `now` and no pre-seeded state; `submitted_at` is only 5 minutes old
  (well inside the 30-minute horizon), so a `classified` result only
  happens if S2 is reading this cycle's own LOST verdict, not falling
  through to the clock.

Both assert on `batch.list_jobs_calls` (S1 actually ran) and
`conn.submissions[100]["state"]` (S1's write landed) in addition to the
S2-side summary/lifecycle assertions, so a test that passed only because S2
happened to reach its own conclusion independently would not be mistaken
for proof of composition.

## Stub-tier suite — actual output

```
$ uv run --with pytest -m pytest pipeline/reconciler/ -v
...
============================= 200 passed in 0.09s ==============================
```

Targeted run of the touched classes:

```
$ uv run --with pytest -m pytest pipeline/reconciler/test/test_service.py -v \
    -k "SubmissionResolutionPassTests or SubmissionRecordDecidesOverTheClockTests or S1FeedsS2WithinOneCycleTests"
...
17 passed, 71 deselected in 1.44s
```

All stub-tier, no I/O — ran on the laptop under carve-out A-3. No SSM/
rapid-admin run needed or performed for this change.

(Note: `pipeline/` run as a whole under `uv run`'s ephemeral venv collects
2105 items but selects 0 — reproduces identically on the unmodified branch
via `git stash`, so it is a pre-existing `uv run`/conftest interaction, not
something these commits introduced. `pipeline/reconciler/` targeted directly
is unaffected and is the relevant scope for this change.)

## Mutation check — gap 1's composition test

Target: prove the new tests can fail, by breaking composition specifically
(not reusing criterion 2's or criterion 6's own mutations, which test
different properties).

**Mutation.** `pipeline/reconciler/service.py:1652`:
```python
classification = self._submission_classification(row)
```
→
```python
classification = None  # __mutated_out__: S2 no longer reads S1's result
```
This breaks "S1's result is visible to S2 within the cycle" without
touching whether S1's pass itself runs (that's criterion 2's placement
mutation) or the FOUND-branch logic once a classification is supplied
(that's criterion 6's mutation).

**Result — RED, both new tests:**
```
$ uv run --with pytest -m pytest pipeline/reconciler/test/test_service.py -v -k S1FeedsS2WithinOneCycleTests
FAILED ...test_s1s_own_found_resolution_feeds_s2_within_the_same_cycle
FAILED ...test_s1s_own_lost_resolution_feeds_s2_within_the_same_cycle
2 failed, 86 deselected in 0.08s

AssertionError: 1 != 0   (found-test: summary["waiting"] dropped to 0)
AssertionError: 1 != 0   (lost-test: summary["classified"] dropped to 0)
```

**Restore.** File copied back from a pre-mutation backup;
`shasum -a 256 pipeline/reconciler/service.py` matched before and after
(`78ae24f362051498b50b24db1d1acadc99351685f6f9176798e06cc92725c6d0`), and
`grep -c __mutated_out__` returned 0 post-restore.

**Result — GREEN again:**
```
$ uv run --with pytest -m pytest pipeline/reconciler/test/test_service.py -v -k S1FeedsS2WithinOneCycleTests
2 passed, 86 deselected in 0.03s
```

`git diff` on `pipeline/reconciler/service.py` is empty throughout — the
mutation was applied and reverted only in the working tree, never staged or
committed.

## Anything the verifier got wrong

Nothing substantive. One clarification: the ledger's gap-1 wording asks for
proof that "a submission resolved to FOUND by this same cycle's S1 pass is
then read as FOUND by this same cycle's S2 classification in the same
poll_once call" for an attempt with a "resolution_deadline far past the
horizon" — I read "the horizon" there as the submission-anchored horizon
(`submitted_at`), which is what `_reconcile_unresolved` actually gates on,
not `resolution_deadline` (which only bounds `protocol.resolve`'s own
FOUND/UNKNOWN decision). Built the FOUND test with `submitted_at` far in
the past and left `resolution_deadline` unset, since a `state="calling"`
row does not carry one yet in `submission_row`'s realistic shape. This
matches criterion 6's own test's use of `submitted_at`, so I'm confident
it's what was meant.

## STOP items

None. All three gaps closed without touching `pipeline/reconciler/
service.py` or `submission/protocol.py`.
