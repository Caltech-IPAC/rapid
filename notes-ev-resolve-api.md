# Resolve API surface — evidence for reconciler wiring

Read-only evidence gathering, branch `smdc` @ `3792447`. No files edited.
File paths corrected from the task brief: the module is at
`submission/protocol.py`, not `pipeline/submission/protocol.py` (there is no
`pipeline/submission/` directory in this repo — `submission/` is top-level,
alongside `pipeline/`).

## 1. `submission/protocol.py` — public surface involved in resolution

All signatures verbatim. `execute` throughout is the repo-wide callable
contract: `execute(statement, params=None)` → rows (list of tuples) when the
statement produced a result set, otherwise an int rowcount. Reference
implementations: `pipeline/contract/fixture.py:95-110` (`executor(conn)`,
real psycopg2 cursor) and `pipeline/reconciler/service.py:1660-1667`
(`_Executor(conn)`, same contract, transaction owned by the caller).

### `is_available(execute)` — `submission/protocol.py:178`
```python
def is_available(execute):
```
- Returns `bool(execute(_AVAILABLE_SQL, []))` — True iff DRAFT migration
  044's `submissions` table exists (queries
  `information_schema.tables`, `submission/protocol.py:167-170`).
- No transaction semantics of its own; runs one read-only SELECT via the
  caller-supplied `execute`.
- Raises nothing beyond whatever `execute` itself raises.
- Docstring (`:179-188`): callers (production and contract tests alike)
  must probe before using the protocol, since the `submissions` table is
  not yet in the deployed migration stream.

### `prepare(...)` — `submission/protocol.py:191-208`
```python
def prepare(execute, *, run_id, job_type, job_name, job_queue, job_definition,
            manifest_checksum, manifest_uri, array_size, now=None):
```
- Not part of the RESOLVE half per se (it's the PREPARED-state entry
  point), included because `resolve`'s callers must have produced rows via
  this path. Inserts one row in state `'prepared'`, returns
  `submission_id` (int, via `RETURNING submission_id`, unwrapped by
  `_single_value`).
- Raises `SubmissionProtocolError` if the INSERT returns no rows
  (`_single_value`, `:437-442`).
- Caller-owned transaction: this function issues one INSERT and returns;
  it neither begins nor commits.

### `mark_calling`, `mark_bound`, `mark_unknown`, `mark_found`, `mark_lost`
These are the CAS-guarded transition primitives `resolve` composes.

```python
def mark_calling(execute, submission_id, now=None):          # :211
def mark_bound(execute, submission_id, scheduler_job_id, now=None):   # :232
def mark_unknown(execute, submission_id, detail, horizon_seconds=None, now=None):  # :243
def mark_found(execute, submission_id, scheduler_job_id, now=None):   # :271
def mark_lost(execute, submission_id, now=None):              # :290
```
- All return `None` (side effect only: one UPDATE via `execute`, then a
  log line).
- All raise `SubmissionStateConflict` (subclass of
  `SubmissionProtocolError`, itself `RuntimeError`) via `_require_one`
  (`:427-434`) when the UPDATE's CAS `WHERE state = '<expected>'` matches
  anything other than exactly one row — i.e. the row doesn't exist, or
  another writer already moved it.
- Each is a single SQL statement executed through the caller's `execute`;
  none of them commit. The module's docstring (`:32-39`) is explicit that
  **the caller must commit between `mark_calling` and the Batch API
  call** — that durability requirement is on the caller, not enforced by
  this function.
- Legal source states per transition (CAS `WHERE` clauses,
  `submission/protocol.py:125-156`):
  - `mark_calling`: `prepared` → `calling`
  - `mark_bound`: `calling` → `bound`
  - `mark_unknown`: `calling` → `unknown`
  - `mark_found`: `calling` OR `unknown` → `found` (admits both — see
    state-machine notes below)
  - `mark_lost`: `unknown` → `lost`

### `open_submissions(execute)` — `submission/protocol.py:308-319`
```python
def open_submissions(execute):
```
- Returns `list[dict]`, one dict per open row, columns: `submission_id,
  run_id, job_type, job_name, job_queue, job_definition, state,
  call_started_at, resolution_deadline, ambiguity_detail` (`:317-318`).
- Selects rows with `state IN ('calling', 'unknown')`, ordered by
  `submission_id` (oldest first) — `_OPEN_SQL`, `:158-165`.
- No transaction of its own — one SELECT.
- Does not raise beyond what `execute` raises.

### `attach_attempts(execute, submission_id, attempt_ids)` — `:322-332`
```python
def attach_attempts(execute, submission_id, attempt_ids):
```
- Not part of resolution; links pre-created attempt rows to a submission
  row (`submission_id IS NULL` guard so a replay can't move rows).
  Returns 0 immediately if `attempt_ids` is falsy, otherwise whatever
  `execute` returns for the UPDATE (rowcount).

### `resolve(execute, row, describe, now=None)` — `submission/protocol.py:335-391`
```python
def resolve(execute, row, describe, now=None):
```
- **Parameters:**
  - `execute` — the callable contract described above.
  - `row` — one dict from `open_submissions` (must contain at least
    `submission_id`, `job_name`, `job_queue`, `state`,
    `resolution_deadline`).
  - `describe` — injected callable `describe(job_name, job_queue) ->
    scheduler_job_id | None`. See DI shape, section 4.
  - `now` — optional `datetime`; defaults to
    `datetime.datetime.now(datetime.timezone.utc)`.
- **Return type:** `str`, one of the module-level state constants `FOUND`,
  `UNKNOWN`, or `LOST` (`"found"`, `"unknown"`, `"lost"`).
- **Return value per outcome** (`:345-391`):
  - `describe(...)` returns a truthy `scheduler_job_id` → calls
    `mark_found(execute, submission_id, scheduler_job_id, now=moment)`,
    returns `FOUND`.
  - `describe(...)` returns falsy AND `row["state"] == CALLING` (i.e. the
    row was interrupted mid-call, never reached `unknown`) → calls
    `mark_unknown(execute, submission_id, detail="interrupted before the
    call outcome was recorded", now=moment)`, returns `UNKNOWN`. This
    branch does NOT check the deadline — a `CALLING` row has none yet.
  - `describe(...)` returns falsy, row already `unknown`, and
    `moment < row["resolution_deadline"]` → no DB write at all, just a
    log line; returns `UNKNOWN`.
  - `describe(...)` returns falsy, row already `unknown`, and deadline has
    passed (or `deadline is None`, which the `is not None` guard treats
    as "no deadline to wait for" and falls through) → calls
    `mark_lost(execute, submission_id, now=moment)`, returns `LOST`.
- **Exceptions:** if `describe` raises, `resolve` does not catch it — "A
  `describe` that RAISES leaves the row untouched and re-raises"
  (docstring `:358-362`). No `mark_*` call happens in that path, so the
  row is left exactly as `open_submissions` found it.
- **Transactions:** `resolve` itself opens/commits nothing. It calls one
  of `mark_found` / `mark_unknown` / `mark_lost` (or neither, in the
  early-unknown branch), each of which issues one UPDATE via the
  caller-supplied `execute` and returns without committing. Caller must
  commit (confirmed by every contract test calling `conn.commit()`
  immediately after `protocol.resolve(...)`,
  e.g. `pipeline/contract/test_submission_protocol.py:141-143`).

### `resolve_open(execute, describe, now=None)` — `submission/protocol.py:394-424`
```python
def resolve_open(execute, describe, now=None):
```
- **Parameters:** same `execute`/`describe`/`now` shapes as `resolve`.
- **Return type:** `dict[str, int]`, always containing keys `FOUND`
  (`"found"`), `LOST` (`"lost"`), `UNKNOWN` (`"unknown"`), and `"errors"`,
  each a count, initialized to 0 (`:409`).
- **Behavior:** calls `open_submissions(execute)` once, then loops over
  each row calling `resolve(execute, row, describe, now=moment)` inside a
  `try/except Exception`. On success, increments `counts[state]`. On
  exception, increments `counts["errors"]` and logs a warning
  (`:415-420`) — the row is left open, matching `resolve`'s own
  no-write-on-raise behavior; the exception is swallowed at this level
  (does not propagate out of `resolve_open`).
- One row's failure does not stop the pass over the rest — explicit
  per-row `try/except`.
- Logs a summary line only `if any(counts.values())` (`:422-423`) —
  silent when there was nothing to resolve.
- **Transactions:** does not commit; each `resolve` call's `mark_*` write
  is uncommitted when `resolve_open` returns unless the caller commits
  per-row or the injected `execute` auto-commits. Nothing in
  `resolve_open`, `resolve`, or the `mark_*` functions calls
  `conn.commit()` — commit discipline is entirely the caller's, exactly
  as for the transition primitives. **Not directly evidenced**: whether a
  real caller is expected to commit once after the whole batch or once
  per resolved row is not stated in this module; the contract tests
  commit after each single `resolve` call, but `resolve_open` has no test
  exercising commit boundaries across multiple rows (see section 6).

## 2. The six-state machine

Verbatim state constants (`submission/protocol.py:75-84`):
```python
PREPARED = "prepared"
CALLING  = "calling"
BOUND    = "bound"
UNKNOWN  = "unknown"
FOUND    = "found"
LOST     = "lost"

SUBMISSION_STATES = frozenset({PREPARED, CALLING, BOUND, UNKNOWN, FOUND, LOST})
```
Sourced "verbatim from DRAFT migration 044" per the comment at `:74`; this
report does not independently confirm the migration file's contents (out of
scope per the task brief, which pointed only at `protocol.py`).

**Legal transitions** (all CAS-guarded UPDATEs, `:113-156`):
| Function | From | To |
|---|---|---|
| `mark_calling` | `prepared` | `calling` |
| `mark_bound` | `calling` | `bound` |
| `mark_unknown` | `calling` | `unknown` |
| `mark_found` | `calling` **or** `unknown` | `found` |
| `mark_lost` | `unknown` | `lost` |

No function transitions `unknown` back to `calling`, and nothing
transitions out of `bound`, `found`, or `lost` — those three are terminal
from this module's perspective (not declared as a `TERMINAL_STATES`
constant here, unlike `pipeline/reconciler/scheduler.py:30`'s scheduler-side
one; this module has no equivalent frozenset).

**Open / ambiguous states, i.e. resolution candidates:** exactly `calling`
and `unknown`, per `open_submissions`'s `_OPEN_SQL` filter (`state IN
('calling', 'unknown')`, `:163`) and the module docstring's summary
("`calling` and `unknown` are the two open states — one interrupted, one
judged ambiguous", `:311`). `prepared` is not open (nothing has been asked
of Batch yet); `bound`/`found`/`lost` are resolved outcomes.

**The CAS guard mechanism** (`submission/protocol.py:74-84` is the state
constants; the actual CAS logic is `:122-156` and `:427-434`, not `:74-84`
as the task brief's line range suggested — noting the discrepancy):
- Every transition SQL statement is `UPDATE submissions SET ... WHERE
  submission_id = %s AND state = '<expected-prior-state>'`
  (`_MARK_CALLING_SQL` etc., `:125-156`). PostgreSQL's row-level UPDATE
  guarantees this either matches the row (if it's still in the expected
  state) or matches nothing (if another writer already moved it, or the
  row doesn't exist).
- `_require_one(result, submission_id, expected_state)` (`:427-434`) is
  the shared post-check: normalizes `result` to a count (int rowcount, or
  `len()` of a returned row list), and raises `SubmissionStateConflict`
  if that count is not exactly 1. The exception message names both
  possibilities ("Either it does not exist or another writer has already
  resolved it") without distinguishing them — the CAS alone cannot tell
  which occurred.
- `SubmissionStateConflict` docstring (`:101-110`) draws the explicit
  parallel to `pipeline.intent.writer.WorkUnitNotFound`: the interesting
  case is concurrent resolution, not a missing row, and a caller that
  can't distinguish "0 rows because gone" from "0 rows because someone
  else already resolved it" would mask a concurrency bug.
- Also enforced at the schema level (per the module docstring, `:45-46`
  and the contract test file's header, `test_submission_protocol.py:16-22`):
  a CHECK constraint `submissions_call_once_ck`, verified only by
  PostgreSQL, not by this Python code — `test_the_call_is_never_repeated_
  for_one_row` (`pipeline/contract/test_submission_protocol.py:193-212`)
  is the test that exercises the schema-level guard, expecting
  `SubmissionStateConflict` on a second `mark_calling` for the same row.

## 3. `batch_describer` — `pipeline/reconciler/scheduler.py:236-247`

```python
def batch_describer(client):
    """A `describe(job_name, job_queue)` bound to this Batch client. ..."""
    def describe(job_name, job_queue):
        return find_job_by_name(client, job_name, job_queue)
    return describe
```
- **Signature:** takes one positional `client` (an AWS Batch boto3-shaped
  client, or a stub with the same call surface). Returns a closure
  `describe(job_name, job_queue)`.
- The returned `describe` closure is exactly the shape `resolve`/
  `resolve_open` expect as their `describe` parameter (confirmed by the
  docstring at `:238-239`, "The shape `submission.protocol.resolve`
  injects").
- The docstring (`:240-243`) states the layering reason it lives in
  `scheduler.py` rather than `submission/protocol.py`: keeping the AWS
  call out of `protocol.py` is what keeps the protocol's resolution logic
  (the part that can be wrong) testable without an AWS account — the same
  posture `submission.submit`'s module docstring takes for `submit_batch`
  and its injected client.

### The underlying call: `find_job_by_name` — `scheduler.py:176-233`
```python
def find_job_by_name(client, job_name, job_queue, states=JOB_SEARCH_STATES):
```
- **How it talks to AWS Batch:**
  - AWS API: `ListJobs`, called once per state in `states` via boto3's
    `list_jobs` (paginated form preferred).
  - Client construction/injection: `find_job_by_name` does not construct
    a client — it receives `client` as a parameter, exactly as
    `batch_describer(client)` passes through. Nothing in `scheduler.py`
    or `protocol.py` shows where the real boto3 client is constructed for
    production use; **not directly evidenced** in the files this task
    scoped (`protocol.py`, `scheduler.py`) — out of scope per the repo
    isolation rule and the brief's file list.
  - Pagination: tries `client.get_paginator("list_jobs")` inside a
    `try/except Exception` (`:204-207`); on any exception (the comment
    says "a stub client may expose no paginator"), falls back to a single
    direct `client.list_jobs(**kwargs)` call wrapped in a one-element
    list, so the same iteration code (`for page in pages`) handles both
    shapes.
  - Per-state call kwargs (`:210-216`): `jobQueue=job_queue,
    jobStatus=state, filters=[{"name": "JOB_NAME", "values":
    [job_name]}]` — looped over `JOB_SEARCH_STATES` (module constant,
    `:172-173`) = `("SUBMITTED", "PENDING", "RUNNABLE", "STARTING",
    "RUNNING", "SUCCEEDED", "FAILED")`, i.e. every state a submitted job
    can be in, terminal states included. The docstring at `:164-173`
    explains why terminal states are searched too: a job may have run to
    completion between submission and the ambiguity check, and
    concluding "lost" because it's no longer running would authorize a
    duplicate submission of already-completed work.
- **Return value:** the job id (`str`) of the first match by creation
  time, or `None` if no job of that name exists in any searched state
  (`:222-224`, logs at INFO and returns `None`).
  - Matching: for every page/state, appends `(createdAt, jobId)` for
    every `jobSummaryList` entry whose `jobName == job_name` and that has
    a `jobId` (`:217-220`).
  - If more than one match is found (a reused batch identity — the
    docstring notes this is otherwise refused upstream by the manifest
    store's conditional create), logs a WARNING (`:225-231`) and returns
    the **oldest** match (`found.sort()` then `found[0][1]`, sorting by
    the `(createdAt, jobId)` tuple, `:232-233`).
- **Error/exception behavior when Batch is unreachable or the job is
  absent:**
  - Job absent (in every searched state): not an exception — returns
    `None` (the "negative" answer `resolve` interprets as not-yet-found
    or lost depending on the deadline).
  - Batch unreachable / API error: **not caught** by `find_job_by_name`
    itself. The only `try/except` in the function is narrowly around
    `client.get_paginator(...)` (paginator-construction fallback only,
    `:204-207`) — any exception raised by `paginator.paginate(...)` or
    `client.list_jobs(...)` (e.g. a boto3 `ClientError`, throttling,
    connection error) propagates uncaught out of `find_job_by_name`, and
    therefore uncaught out of the `describe` closure `batch_describer`
    returns. This is exactly what `resolve`'s docstring assumes ("A
    `describe` that RAISES leaves the row untouched and re-raises",
    `protocol.py:358`) and what `resolve_open` catches per-row
    (`scheduler.py`'s docstring doesn't state this; the catch is in
    `protocol.py:412-420`).

## 4. Dependency-injection shape

- `resolve(execute, row, describe, now=None)` and `resolve_open(execute,
  describe, now=None)` both take `describe` as a plain **positional
  callable parameter** — no ABC, no Protocol class, no named type. There
  is no `typing.Protocol` or abstract base class defined anywhere in
  `submission/protocol.py` for this.
- Required call signature (by usage, not by declared type):
  `describe(job_name: str, job_queue: str) -> str | None` — a scheduler
  job id if found, `None`/falsy otherwise. `resolve` calls it positionally
  as `describe(row["job_name"], row["job_queue"])` (`:367`).
  `batch_describer`'s returned closure matches this exactly
  (`scheduler.py:245-246`).
- Test doubles confirm the same shape from the consumer side: contract
  test file's `_FakeBatch.describe(self, job_name, job_queue)`
  (`test_submission_protocol.py:69-71`) is a bound method matching the
  same two-positional-arg call; `resolve` is invoked with `batch.describe`
  (e.g. `:141`, `:173`, `:181`), i.e. any callable with that signature —
  free function, closure, or bound method — satisfies the contract.

## 5. FOUND vs LOST — concrete meaning

**FOUND** (`mark_found`, `protocol.py:271-287`):
- DB write: `UPDATE submissions SET state = 'found', scheduler_job_id =
  %s, resolved_at = %s, updated_at = %s WHERE submission_id = %s AND
  state IN ('calling', 'unknown')` (`_MARK_FOUND_SQL`, `:145-150`).
- Caller-expected action per the module docstring (`:347-348`): "the work
  is running and must not be resubmitted." No further caller action is
  specified in this module — `found` appears to be treated as a resolved,
  reconciled-into terminal state whose `scheduler_job_id` now lets normal
  scheduler-observation machinery (e.g. `scheduler.py`'s
  `SchedulerObservation`/`describe_in_batches`) pick the job up like any
  other bound job. Not evidenced in `protocol.py`: no code path reads
  `found` rows back out for further action — `open_submissions` excludes
  them by definition (`state IN ('calling','unknown')` only).

**LOST** (`mark_lost`, `protocol.py:290-305`):
- DB write: `UPDATE submissions SET state = 'lost', resolved_at = %s,
  updated_at = %s WHERE submission_id = %s AND state = 'unknown'`
  (`_MARK_LOST_SQL`, `:152-156`) — note: no `scheduler_job_id` is written
  (stays NULL), unlike `found`/`bound`.
- Caller-expected action, stated explicitly in the docstring (`:296-297`,
  `:302-305`): "Declaring `lost` is the one conclusion that authorizes
  resubmitting the work" — but as "a NEW submission row" with "a new
  identity", never by re-calling `submit_job` for the same row. The
  module's rule-7 framing (`:41-46`) is that this is enforced so
  "how many times did we call Batch for this work" is answerable by
  counting rows — no code in this module performs the resubmission
  itself; that is left to a caller outside this module (not identified —
  `resolve`/`resolve_open` are not yet called from anywhere in the repo,
  see section 6).

Both are reached only through `resolve` (never called directly by a
`describe` result — `mark_found`/`mark_lost` are plain public functions
too, but `resolve` is the only site in this module that calls them based
on a `describe` outcome).

## 6. Existing tests

**Single test file exercising the resolve half:**
`pipeline/contract/test_submission_protocol.py` (267 lines). This is a
**contract-tier** test file (imports `psycopg2` via `pipeline.contract.
fixture`, connects to a real PostgreSQL database) — it is not a pure unit
test file, and it **skips entirely** unless DRAFT migration 044's
`submissions` table exists in the target database (`_requires_submissions_
table` fixture, `:41-47`, applied via `pytestmark =
pytest.mark.usefixtures(...)` at `:38`). Per the file's own docstring
(`:9-14`), this table is a pending change request, not part of the
deployed schema — these tests do NOT run in ordinary CI; they run on
rapid-admin where base + drafts are applied.

Fixture/fake used throughout: `_FakeBatch` (`:50-71`) — records every
`submit_job` and `describe` call; `submit_job` unconditionally raises
`AssertionError` (a double that REFUSES, per the file's header discipline
citation at `:24-28`) since no resolve-path test is entitled to call it;
`describe(job_name, job_queue)` looks up `job_name` in a dict of
`known_jobs` supplied at construction and returns the mapped scheduler id
or `None`. `execute` comes from `fixture.executor(conn)` (real cursor,
`pipeline/contract/fixture.py:95-110`).

Test functions and what each proves:

| Test | Line | Proves |
|---|---|---|
| `test_the_bound_path_is_unchanged` | `:97-117` | The non-ambiguous happy path `prepared → calling → bound` (via `mark_calling`/`mark_bound` directly, not via `resolve`) is untouched by the protocol's addition. Confirms `call_started_at` is durable, `scheduler_job_id`/`resolved_at` are NULL until `mark_bound`. Uses no `describe` at all. |
| `test_unknown_resolves_found_by_identity_requery` | `:120-150` | `resolve` on an `unknown` row whose job name IS in `_FakeBatch.known_jobs` returns `FOUND`, writes `scheduler_job_id`, sets `resolved_at`, and — critically — asserts `batch.describe_calls == [(job_name, "contract-queue")]` (exactly one describe call, by name) and `batch.submit_calls == []` (submit never touched). |
| `test_unknown_resolves_lost_only_past_the_deadline` | `:153-190` | Same negative `describe` answer (`_FakeBatch()` with no known jobs) yields `UNKNOWN` when called before `resolution_deadline` and `LOST` when called with `now` past the deadline (`row["resolution_deadline"] + timedelta(seconds=1)`, passed via `resolve`'s `now=` param). Confirms two describe calls total, zero submit calls, and that `scheduler_job_id` stays NULL on `LOST`. |
| `test_the_call_is_never_repeated_for_one_row` | `:193-212` | A second `mark_calling` on an already-`calling` row raises `SubmissionStateConflict` — the CAS guard (both Python-level `_require_one` and, per the file header, the schema's `submissions_call_once_ck`). Does not go through `resolve` — directly tests the transition primitive. |
| `test_an_unreachable_scheduler_resolves_nothing` | `:215-238` | A `describe` that raises `RuntimeError` propagates uncaught out of `resolve` (`pytest.raises(RuntimeError)`), and the row's state is confirmed still `UNKNOWN` afterward (`conn.rollback()` then re-check) — no partial/incorrect write occurred. |
| `test_an_interrupted_call_is_as_ambiguous_as_a_judged_one` | `:241-267` | A row left in `CALLING` (never reached `unknown`) is resolved via `resolve` with a negative `_FakeBatch()` describe, returns `UNKNOWN`, and is confirmed to now carry a non-NULL `resolution_deadline` — exercising the `row["state"] == CALLING` branch inside `resolve` (`:374-381`) that the interrupted-mid-call case takes. |

**Not covered by any test found in this task's scope:**
- `resolve_open` itself (the batching/looping wrapper) has no dedicated
  test in this file — every test above calls `protocol.resolve` directly
  on one row fetched via `protocol.open_submissions`, never
  `protocol.resolve_open`. Commit-boundary behavior across multiple rows
  in one `resolve_open` pass is therefore unverified by anything found.
- `is_available`, `prepare`, `attach_attempts` have no dedicated test
  visible in this file (they're used as setup helpers — e.g. `_prepare`
  wraps `protocol.prepare` — but not asserted on independently).
- `batch_describer` and `find_job_by_name`
  (`pipeline/reconciler/scheduler.py:236-247`, `:176-233`) — searched
  `pipeline/reconciler/test/test_scheduler.py` (all 20 test function names
  listed) and found **no test function for either**. `test_scheduler.py`
  covers `describe_in_batches`, `derive_attempt_indices`,
  `_attempt_state`/`SchedulerObservation`-related behavior, but not the
  name-search/describer-construction path. **Not evidenced**: any test of
  the real `ListJobs`-based lookup, its multi-state loop, its pagination
  fallback, or its "most recent match wins" tie-break exists anywhere in
  the repo within this task's scope.

## Open items / ambiguities flagged, not resolved

1. Where the production Batch `client` passed to `batch_describer` is
   constructed and injected into a running reconciler process is not
   shown in either `protocol.py` or `scheduler.py` — out of scope per the
   brief's file list, but material to "wiring into the reconciler."
2. `resolve_open`'s commit-boundary contract (per-row vs. once-per-pass)
   is not stated in the docstring and not tested — the caller must decide
   and verify this independently.
3. Line range `protocol.py:74-84` given in the task brief for "the CAS
   guard mechanism" is actually the six state-name constants; the real
   CAS logic lives at `:113-156` (SQL) and `:427-434` (`_require_one`).
   Flagged rather than silently substituted.
4. No code in the scope reviewed here performs the actual resubmission
   that a `LOST` outcome is said to authorize — confirmed absent, not
   merely unfound, by grepping for `resolve_open`/`resolve(`/
   `batch_describer` callers across `pipeline/reconciler/` and
   `submission/` (section on FOUND/LOST, and confirmed no callers exist
   anywhere outside `protocol.py` itself).
