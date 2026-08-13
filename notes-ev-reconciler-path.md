# Evidence: reconciler's current ambiguity-resolution path

Scope: `pipeline/reconciler/service.py`, `pipeline/reconciler/horizons.py`,
`pipeline/reconciler/scheduler.py`, `pipeline/reconciler/lease.py`, and the
reconciler's own test suite. Read-only; nothing edited. Branch `smdc` @
`3792447`.

---

## 1. The `beyond_submission_horizon` call site

### 1.1 Enclosing function

`pipeline/reconciler/service.py:1531-1643`, method `_reconcile_unresolved(self, row)` on `ReconcilerService`.

```python
# service.py:1531
def _reconcile_unresolved(self, row):
    """A pre-created child the scheduler cannot account for.

    Bounded by the submission-anchored horizon, not the grace horizon:
    there is no scheduler-terminal observation to be graceful after.
    """
    if not beyond_submission_horizon(row.get("submitted_at"),
                                     now=self._now()):
        # Inside the submission-anchored horizon: queue time, not a fault.
        return "waiting"
```
(`service.py:1531-1540`)

`row` is a single dict (one attempt row read from `attempts`), not a
collection — `_reconcile_unresolved` is called once per unresolved row, from
a loop one level up (see 1.2). It is not itself an iterator.

### 1.2 Call chain, main loop down to the horizon check

```
run_forever                          (not shown above line 100; supervised loop)
  ReconcilerService.poll_once()      service.py:380
    rows = self.open_attempts()      service.py:382  — SQL select, no lease
    # partitions rows into by_job (scheduler_job_id set) vs unresolved (not set)
    for row in rows:                 service.py:396-401
        if row.get("scheduler_job_id"): by_job[...] else: unresolved.append(row)
    ...
    for row in unresolved:           service.py:467-476
        outcome = self._reconcile_unresolved(row)   # <-- line 469
          -> _reconcile_unresolved(row)  service.py:1531
               beyond_submission_horizon(...)        # <-- line 1537, the target call
```

Verbatim partition and dispatch loop:

```python
# service.py:394-401
by_job = {}
unresolved = []
for row in rows:
    job_id = row.get("scheduler_job_id")
    if job_id:
        by_job.setdefault(job_id, []).append(row)
    else:
        unresolved.append(row)
```

```python
# service.py:467-476
for row in unresolved:
    try:
        outcome = self._reconcile_unresolved(row)
    except Exception:  # noqa: BLE001 - same reasoning as above
        self._safe_rollback()
        logger.exception("reconciling unresolved attempt %s failed",
                         row.get("attempt_id"))
        summary["errors"] += 1
    else:
        summary[outcome] = summary.get(outcome, 0) + 1
```

There is a **second** call site that can also route into
`_reconcile_unresolved`, from `_reconcile_attempt` (the paired-observation
path), when the scheduler returned the job but no attempt-level observation
could be matched to this row:

```python
# service.py:680-687
def _reconcile_attempt(self, row, observations):
    attempt_id = row["attempt_id"]
    observation = self._pick_observation(row, observations)

    if observation is None:
        # The scheduler returned the job but not an attempt we can pair.
        # Treat as unresolved: the submission-anchored horizon applies.
        return self._reconcile_unresolved(row)
```

So `_reconcile_unresolved` (and therefore `beyond_submission_horizon`) is
reached two ways: (a) rows with `scheduler_job_id IS NULL` at partition time,
handled in the `for row in unresolved` loop; (b) rows with a
`scheduler_job_id` that Batch returned a job for, but whose specific
attempt-history entry could not be paired to this row (`_pick_observation`
returned `None`), redirected from inside `_reconcile_attempt`.

### 1.3 What the horizon decision drives, and each branch

`beyond_submission_horizon` is a single boolean gate at the top of the
method (`service.py:1537-1540`):

- **False** (not yet past horizon) → `return "waiting"` immediately. No
  lease taken, nothing written except the row already sits open. This is the
  entire branch — one early return, three lines.
- **True** (past horizon) → falls through to the rest of the method
  (`service.py:1542-1643`): acquire the attempt lease, reread the row under
  it, write a diagnostics bundle, build and publish a closure record, mark
  the row `terminal_without_start` (or `missing_or_contradictory` if the row
  actually shows application evidence — see §3), and resolve the work unit.
  This whole branch is the "classify it as a child that never resolved" case
  from the module's own top-of-file summary (`service.py:22-23`).

Full fall-through branch, verbatim:

```python
# service.py:1542-1643
attempt_id = row["attempt_id"]
with attempt_lease(self.conn, attempt_id) as held:
    if not held:
        return "skipped"
    current = reread_attempt(self.conn, attempt_id,
                             columns=_OPEN_COLUMNS)
    if current is None or current["lifecycle_state"] not in OPEN_STATES:
        return "skipped"

    # THE BUNDLE COMES FIRST, AND IT IS NOT OPTIONAL (round-4 finding #5).
    try:
        self._stamp_bundle(current, None, None)
    except Exception:  # noqa: BLE001 - deferred, not swallowed (#16)
        self._closure_failures += 1
        logger.exception(...)
        return "deferred"

    record = closure_mod.build_closure_record(
        current, None,
        sequence=self._next_sequence(current),
        predecessor=None,
        rejected_key=None,
        rejected_reason=closure_mod.REJECTED_ABSENT,
        classification=CLASS_NEVER_RESOLVED,
        error_category="scheduler_provisioning",
        now=self._now())
    try:
        written = closure_mod.publish_closure_record(
            self.records_store, self.records_prefix, current, record)
    except Exception:  # noqa: BLE001 - deferred, not swallowed (#16)
        self._closure_failures += 1
        logger.exception(...)
        return "deferred"

    writer = AttemptWriter(_Executor(self.conn))

    # An attempt the scheduler cannot account for is normally one that
    # never ran — but not always. ... a genuine disagreement between the
    # stores, and it gets the state the design has for disagreement.
    if self._attempt_ran(current, None, None):
        writer.mark_missing_or_contradictory(
            attempt_id,
            reconciliation_class="missing",
            reconciliation_sources=["postgres", "batch"],
            detected_at=self._now())
        logger.warning(...)
        return "classified"

    writer.mark_terminal_without_start(
        attempt_id, ended_at=self._now(),
        scheduler_state="FAILED",
        error_category="scheduler_provisioning",
        closure_record_key=written.key,
        closure_record_sequence=written.sequence)
    self._close_work_unit(current, outcome="failed",
                          error_category="scheduler_provisioning")
    logger.info("attempt %s classified never-resolved at the "
                "submission-anchored horizon (closure %s)",
                attempt_id, written.key)
    return "classified"
```

`CLASS_NEVER_RESOLVED = "never_resolved"` (`service.py:114`) is stamped into
the closure record's `classification` field
(`closure_mod.build_closure_record(..., classification=CLASS_NEVER_RESOLVED, ...)`,
line 1581) — this is the literal "ambiguity resolved as lost" outcome the
horizon currently authors on its own, with **no re-query of Batch anywhere
in this branch**. The only scheduler-side lookup that ever happened for this
row was the original `describe_jobs` batch call in `_observe` (see §4),
which is what produced the empty `by_job` entry (or unmatched attempt) in
the first place.

---

## 2. `pipeline/reconciler/horizons.py` — full file

97-line file, read in full (see tool output above; reproduced here for the
report's self-containedness).

### 2.1 Docstring at lines 15-30, verbatim

```
Both starting values are replaceable by evidence without re-ratification. They
are stated here, once, rather than spelled inline at the call sites, so that
changing one is a one-line change with a test that names the value.

**THE SUBMISSION-ANCHORED HORIZON IS NOW A BACKSTOP, NOT THE TRUTH** (rule 7,
brief C1: "The time horizon may remain as a backstop for scheduler-side
silence, but it acts on a record that says CALLING/UNKNOWN — the state
machine, not the timestamp, is the truth"). It used to be the ENTIRE
resolution of an ambiguous submission: a pre-created child with a NULL
scheduler id waited out thirty minutes and was then classified, without anyone
ever asking Batch whether the job existed. That made two genuinely different
situations — a job that was accepted and is running, and a request that never
arrived — indistinguishable, because a clock cannot tell them apart.

`submission.protocol` now carries the durable record (PREPARED -> CALLING ->
BOUND / UNKNOWN -> FOUND / LOST) and resolves ambiguity by positively
re-querying Batch for the submission's deterministic job name. This horizon
survives underneath that, doing the narrower job it is actually suited to:
```

(Lines 15-30 span into the paragraph immediately after; full text through
line 39 quoted below for completeness since it is one continuous thought.)

```
bounding how long a NEGATIVE re-query keeps meaning "not visible yet" before
it is allowed to mean "absent" (`protocol.RESOLUTION_HORIZON_SECONDS`, kept
equal to the value below so the two mechanisms cannot disagree while the
protocol is being adopted), and classifying rows for which no submission
record exists at all — every attempt predating DRAFT migration 044.

So the duration was never the defect and is unchanged. What changed is what
elapsed time is permitted to CONCLUDE: it no longer decides what happened to a
submission, it only bounds how long the evidence is allowed to stay silent.
```

**This docstring explicitly says the design intent already exists**: a
`submission.protocol` module with a `PREPARED -> CALLING -> BOUND / UNKNOWN
-> FOUND / LOST` state machine, and `protocol.RESOLUTION_HORIZON_SECONDS` as
a value kept equal to `SUBMISSION_HORIZON_SECONDS` "while the protocol is
being adopted." **This module's docstring is evidence of what the design
says should exist elsewhere, not proof that `_reconcile_unresolved` itself
calls into it — §1.3 shows `_reconcile_unresolved` has no such call.**
Whether `submission.protocol` exists and is wired anywhere in the codebase
was NOT checked (out of the stated scope of the four named files) — flagged
as undetermined below.

### 2.2 `beyond_submission_horizon` signature, inputs, semantics

```python
# horizons.py:84-97
def beyond_submission_horizon(submitted_at, now=None,
                              horizon=SUBMISSION_HORIZON_SECONDS):
    """Has a never-resolved child outlived its submission-anchored horizon?

    False when the submission time is unknown, for the same reason: with no
    anchor there is no horizon to be past. Such a row is a different fault —
    a submitted attempt with no submitted_at — and is not this predicate's to
    classify.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    elapsed = _elapsed(submitted_at, now)
    if elapsed is None:
        return False
    return elapsed >= horizon
```

- **Inputs**:
  - `submitted_at` — positional, no default. At the one production call site
    (`service.py:1537`) this is `row.get("submitted_at")`, i.e. a column
    read off the `attempts` row (see §3 for the row shape). Not from a
    clock or config — it is stored, per-row, database state.
  - `now` — optional; defaults to `datetime.datetime.now(datetime.timezone.utc)`
    if not passed. At the call site it is always passed explicitly as
    `self._now()` (`service.py:1538`), which is `ReconcilerService.__init__`'s
    injected clock: `self._now = now or (lambda: datetime.datetime.now(datetime.timezone.utc))`
    (`service.py:234-235`) — constructor parameter `now=None`, so tests can
    inject a fixed clock (confirmed in `test_service.py`'s `build()` helper,
    which always passes a fixed `now=` lambda — `test_service.py:16-26`).
  - `horizon` — optional; defaults to the module constant
    `SUBMISSION_HORIZON_SECONDS`. Never overridden at the call site — no
    caller in `service.py` passes an explicit `horizon=`.
- **The horizon value itself**: `SUBMISSION_HORIZON_SECONDS = 30 * 60`
  (`horizons.py:51`, a plain **module-level constant**, not an env var and
  not read from a config object). Sibling constant
  `GRACE_HORIZON_SECONDS = 10 * 60` (`horizons.py:46`) is used by the
  companion predicate `beyond_grace_horizon` (called at `service.py:699`,
  the terminal-observation path, not the ambiguity path this report covers).
- **Semantics**: `_elapsed(since, now)` (`horizons.py:54-66`) returns
  `(now - since).total_seconds()`, or `None` if `since is None`, and
  **raises `ValueError`** if `since` is a naive datetime (`horizons.py:63-65`
  — "every timestamp in this system is stored timestamptz and read back
  aware"). `beyond_submission_horizon` returns `False` whenever `elapsed is
  None` (no `submitted_at` at all) — i.e. absent timestamp is NOT "past
  horizon," it is "not this predicate's problem" per its own docstring.
  Otherwise a plain `elapsed >= horizon` comparison, no state machine, no
  external call.

---

## 3. Data already in hand at the ambiguity decision point

### 3.1 What `row` is

`row` passed into `_reconcile_unresolved` is a plain `dict`, produced by
`ReconcilerService._select()`:

```python
# service.py:335-341
def _select(self, sql, params):
    with self.conn.cursor() as cur:
        cur.execute(sql, params)
        names = [description[0] for description in cur.description]
        rows = [dict(zip(names, row)) for row in cur.fetchall()]
    self.conn.rollback()  # a read-only snapshot; do not hold a transaction
    return rows
```

It comes from `open_attempts()` → `_OPEN_SET_SQL` /
`_SUPERSEDABLE_SQL`, both built from the same column tuple `_OPEN_COLUMNS`
(`service.py:126-154`). **No dataclass wraps this row** — it is a bare dict
keyed by column name throughout the reconciler (confirmed by
`row.get("submitted_at")`, `row["attempt_id"]`, `row.get("scheduler_job_id")`
usage throughout `service.py`).

### 3.2 The fields (`_OPEN_COLUMNS`), verbatim

```python
# service.py:126-154
_OPEN_COLUMNS = (
    "attempt_id", "run_id", "logical_job_id", "scheduler_job_id",
    "lifecycle_state", "application_attempt_index", "scheduler_attempt_index",
    "application_claim_index",
    "exposure_id", "sca", "sky_tile", "submitted_at", "started_at", "ended_at",
    "rapid_outcome", "product_disposition", "application_intended_exit",
    "error_category", "terminal_record_key", "terminal_record_sequence",
    "terminal_record_checksum",
    # Runtime-selected provenance: what the attempt itself observed and bound.
    "source_sha", "container_digest", "job_definition_rev", "config_digest",
    "config_snapshot_key",
    # The reconciler's own closure-record citation.
    "closure_record_key", "closure_record_sequence", "reconciler_materialized",
    # The scheduler-observed facts already recorded, so a supersession pass
    # can tell a changed fact from an unchanged one (#15).
    "scheduler_state", "scheduler_observed_exit",
    # The submission-time execution binding, copied on at row creation.
    "binding_job_definition_arn",
    "binding_job_definition_rev", "binding_image_digest",
    "binding_release_identity", "binding_manifest_checksum",
    # The intent-layer FK (migration 036, integration review ruling 13).
    "work_unit_id",
)
```

Identifying fields present, directly usable by a Batch re-query:

- **`attempt_id`** — the PostgreSQL row identity (int).
- **`run_id`**, **`logical_job_id`** — used to build `terminal_record_key`
  (`termination.terminal_record_key(self.records_prefix, row["run_id"],
  row["logical_job_id"], ...)`, e.g. `service.py:838-839`).
- **`scheduler_job_id`** — a Batch job id **if one was ever received**; for
  the unresolved-via-partition path (§1.2 case (a)) this is `NULL` by
  definition (that is what put the row in `unresolved` at
  `service.py:397-401`). For case (b) (redirect from `_reconcile_attempt`)
  the row DOES carry a `scheduler_job_id` — the scheduler returned a job for
  it, just no attempt observation could be paired.
  **No `job_name` column is selected** — the deterministic Batch job *name*
  (as opposed to the id) is not present in `_OPEN_COLUMNS` at all. Per
  `scheduler.py:181-186`, that name is `submission.submit.build_submit_kwargs`'s
  `job_name or f"rapid-{manifest.batch_id}"`, and would need to be either
  derived the same way from fields the row does carry (`exposure_id`, `sca`,
  `sky_tile` look like manifest-identity fields, but `batch_id` itself is not
  in `_OPEN_COLUMNS`) or read from `submission.protocol`'s own stored
  record (not selected here) — **flagged as undetermined**: whether the row
  as currently selected carries enough to reconstruct the exact job name
  `find_job_by_name` needs was not traced further (out of the four named
  files).
- **`work_unit_id`** — the intent-layer FK, used later in the fall-through
  branch by `_close_work_unit` (§1.3).
- No literal "Batch job id" field beyond `scheduler_job_id` — there is no
  separate submission/attempt id distinct from it in `_OPEN_COLUMNS`.

### 3.3 Transaction state at the decision point

**No transaction is open when `beyond_submission_horizon` is evaluated.**
Evidence, in order:

1. `open_attempts()` (which produced `row`) explicitly rolls back after
   reading: `self.conn.rollback()  # a read-only snapshot; do not hold a
   transaction` (`service.py:340`).
2. `_reconcile_unresolved` performs the horizon check
   (`service.py:1537-1540`) **before** acquiring anything — no
   `attempt_lease` context, no cursor, is opened above that line in the
   method.
3. A transaction/lock is opened only if the horizon check falls through:
   `with attempt_lease(self.conn, attempt_id) as held:` at
   `service.py:1543`. `attempt_lease` opens a `pg_try_advisory_xact_lock`
   inside a cursor and the transaction it implicitly starts
   (`lease.py:79-117`); it is **not yet held** at the moment the horizon
   predicate runs.

So: the horizon decision itself is made connection-idle, against a plain
dict already in Python memory. Any re-query inserted at the horizon
decision point (before the `attempt_lease` block) would likewise run with
no open transaction. A re-query inserted *inside* the existing
`attempt_lease` block (after line 1543) would run **under** the advisory
lock and its transaction — see §5 for what that currently holds.

---

## 4. `pipeline/reconciler/scheduler.py` — whole-module role

### 4.1 Module purpose (docstring, `scheduler.py:1-17`)

Two things: (a) batching `describe_jobs` calls (`describe_in_batches`,
100-id chunks per Batch's ceiling), and (b) deriving the attempt ordinal
Batch's API does not expose (`derive_attempt_indices`).

### 4.2 How the reconciler service is constructed relative to this module

`ReconcilerService.__init__` takes a **raw Batch client**, not anything
from `scheduler.py`:

```python
# service.py:201-206
def __init__(self, conn, batch_client, records_store, diagnostics_store,
             s3_client, records_prefix, diagnostics_bucket,
             logs_client=None, log_group=None, log_groups=None,
             now=None):
    self.conn = conn
    self.batch = batch_client
```

`self.batch` is used exactly once in the whole service, inside `_observe`:

```python
# service.py:547-553
def _observe(self, job_ids):
    """Describe every open job, batched, and index observations by job id."""
    found = {}
    for chunk in describe_in_batches(self.batch, job_ids):
        for job in chunk.jobs:
            found[job.get("jobId")] = observations_for_job(job)
    return found
```

`describe_in_batches` and `observations_for_job` are imported directly from
`scheduler.py` (`service.py:49`:
`from .scheduler import describe_in_batches, observations_for_job`) and
called as **module-level functions**, not through any object the service
holds. **`ReconcilerService` does not receive a `batch_describer` or any
`describe(job_name, job_queue)` callable at all** — nothing in
`__init__`'s parameter list, nothing assigned in `__init__`'s body, and no
attribute named anything like `describer` exists anywhere in
`service.py` (confirmed by the `grep` in §4.3 below).

### 4.3 Where `batch_describer` (scheduler.py:236-247) sits

```python
# scheduler.py:236-247
def batch_describer(client):
    """A `describe(job_name, job_queue)` bound to this Batch client.

    The shape `submission.protocol.resolve` injects. Kept here rather than in
    `submission.protocol` for the layering reason that module's own docstring
    gives: the protocol's resolution LOGIC — which is the part that can be
    wrong — is testable without an AWS account precisely because the lookup
    arrives as a callable, exactly as `submit_batch` takes its client.
    """
    def describe(job_name, job_queue):
        return find_job_by_name(client, job_name, job_queue)
    return describe
```

`batch_describer`'s own docstring names its one intended consumer:
`submission.protocol.resolve` — a module **not** among the four named for
this report, and not otherwise referenced anywhere in
`pipeline/reconciler/service.py` (confirmed: `grep -n "batch_describer\|
protocol\." service.py` returns nothing — see §4.4). So as of this reading,
`batch_describer` is wired to whatever `submission.protocol.resolve` is, and
**not** to the reconciler service at all.

### 4.4 What already exists vs. what would be new, to thread a describer into the service's ambiguity path

Confirmed by direct search of `service.py`:

```
$ grep -n "batch_describer\|find_job_by_name\|submission\.protocol\|describer" service.py
(no matches)
```

**Already exists** (usable building blocks, per §4.2-4.3):
- `scheduler.find_job_by_name(client, job_name, job_queue, states=...)` — the
  actual positive re-query function, fully implemented
  (`scheduler.py:176-233`), with its own `JOB_SEARCH_STATES` covering both
  live and terminal states (`scheduler.py:172-173`) specifically so a job
  that already finished is not misread as lost (`scheduler.py:167-173`
  docstring).
- `scheduler.batch_describer(client)` — a ready-made factory that closes
  over a raw Batch client and returns a `describe(job_name, job_queue)`
  callable with exactly the shape `find_job_by_name` needs
  (`scheduler.py:236-247`).
- `self.batch` — the raw Batch client the service already holds
  (`service.py:206`), which is exactly the `client` argument
  `batch_describer` wants. So `scheduler.batch_describer(self.batch)` could
  be constructed from data the service already has, with no new
  constructor parameter, if the describer were built lazily at the call
  site rather than injected at `__init__` time.

**Would be new** (nothing of this exists in `service.py` today):
- No `__init__` parameter or attribute to hold an injected describer
  callable — `ReconcilerService.__init__`'s full parameter list is:
  `conn, batch_client, records_store, diagnostics_store, s3_client,
  records_prefix, diagnostics_bucket, logs_client, log_group, log_groups,
  now` (`service.py:201-204`). No `describer`/`batch_describer`/`resolver`
  parameter.
- No call to `find_job_by_name` or `batch_describer` anywhere in
  `_reconcile_unresolved`, `_reconcile_attempt`, or any other method —
  the ambiguity path's only interaction with the scheduler is the
  `describe_jobs`-by-id batch call in `_observe` (§1.3, §4.2), which by
  construction only covers rows that already carry a `scheduler_job_id` —
  the unresolved case (a) rows (`scheduler_job_id IS NULL`) are never
  looked up by name anywhere in this file.
- No `job_name` (or the raw materials to reconstruct it, per §3.2) is
  selected in `_OPEN_COLUMNS`, so even with a describer in hand,
  `_reconcile_unresolved`'s `row`/`current` dict does not currently carry
  the argument `find_job_by_name(client, job_name, job_queue)` needs.
- No `job_queue` value is threaded into `ReconcilerService` either —
  `grep -n "job_queue\|jobQueue" service.py` returns no matches (checked
  below).

```
$ grep -n "job_queue\|jobQueue" service.py
(no matches)
```

So threading a describer from scheduler construction into the service's
ambiguity path today would require, at minimum: (1) a new constructor
parameter or lazily-built attribute for the describer callable (trivially
`scheduler.batch_describer(self.batch)`, reusing the client already held),
(2) a job-queue value the service does not currently have anywhere, (3) a
job-name value or its source columns added to `_OPEN_COLUMNS`/the row shape,
and (4) an actual call to the describer inserted into `_reconcile_unresolved`
(or a new method it calls) — none of which exist today.

---

## 5. Transaction discipline in this loop

### 5.1 Where transactions open/commit/rollback in the ambiguity path

- **Before any lease**: `open_attempts()` → `_select()` runs its query and
  immediately `self.conn.rollback()`s (`service.py:340`) — "a read-only
  snapshot; do not hold a transaction" is the comment's own words.
- **The horizon check itself** (`service.py:1537-1540`) runs with no
  transaction open (§3.3).
- **On fall-through**, `attempt_lease(self.conn, attempt_id)` is entered
  (`service.py:1543`). Per its own docstring
  (`lease.py:1-24`, `lease.py:79-117`):

  ```python
  # lease.py:79-117
  @contextlib.contextmanager
  def attempt_lease(conn, attempt_id, blocking=False):
      """Hold the reconciliation lease for one attempt across a transaction.
      ...
      The transaction is committed on clean exit and rolled back on any
      exception, and the lock is released either way because it is
      transaction-scoped.
      """
      acquired = False
      try:
          with conn.cursor() as cur:
              if blocking:
                  cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", ...)
                  acquired = True
              else:
                  cur.execute("SELECT pg_try_advisory_xact_lock(%s, %s)", ...)
                  row = cur.fetchone()
                  acquired = bool(row[0]) if row else False

          if not acquired:
              logger.debug(...)
              conn.rollback()
              yield False
              return

          yield True
          conn.commit()
      except Exception:
          conn.rollback()
          raise
  ```

  Everything the fall-through branch does — reread, stamp bundle, publish
  closure record, `mark_missing_or_contradictory` /
  `mark_terminal_without_start`, `_close_work_unit` — runs inside this one
  `with` block (`service.py:1543-1643`), i.e. one PostgreSQL transaction,
  committed at `lease.py:114` on clean exit or rolled back at `lease.py:116`
  on any exception.

### 5.2 Documented invariants constraining where an AWS re-query may sit

**No comment in `service.py`, `lease.py`, `horizons.py`, or `scheduler.py`
states a rule of the literal form "no transaction spans an AWS call."**
Search performed and came up empty (see tool call in this session: grep for
`"no transaction\|span an AWS call\|does not hold a transaction\|holds no
transaction\|AWS call"` across `pipeline/reconciler/*.py` — zero matches).
**This is flagged, not asserted-negative from silence alone**: the closest
documented statements are:

1. The lease module's own framing of what it protects — S3 tag-set rewrites
   plus the row transition, guarded because "S3's tagging API has no
   compare-and-set" (`lease.py:3-6`) — i.e. the lease exists to make a
   sequence that **already includes AWS calls** (S3 writes) atomic with the
   DB transition. Concretely, inside today's lease block: `_stamp_bundle`
   (`service.py:1565`, called inside the `with attempt_lease` block) does
   write to S3 (`self.diagnostics_store`, `self.s3` — confirmed at
   `service.py:1070`, `1119-1120`, `1136` inside the sibling `_stamp_bundle`
   method used by both `_close` and `_reconcile_unresolved`), and
   `publish_closure_record` (`service.py:1586`, also inside the lease) writes
   to `self.records_store`. **So the existing code already holds AWS S3
   calls under the transaction-scoped advisory lock** — there is no
   standing prohibition on AWS calls happening inside a lease/transaction in
   this codebase as it exists today.
2. By contrast, the **Batch `describe_jobs` call** (`self.batch`, via
   `_observe`, `service.py:547-553`) happens in `poll_once`
   (`service.py:403`) **before** any row-level lease is taken for any
   attempt — it runs once per poll cycle over the whole open set, entirely
   outside any `attempt_lease` block. This is a structural fact about the
   current code, not a documented rule: no comment states Batch calls must
   stay outside a lease; it is simply where the one existing Batch call
   happens to sit today.
3. `_select()`'s comment ("a read-only snapshot; do not hold a transaction",
   `service.py:340`) is the only explicit "don't hold a transaction" language
   in the file, and it concerns the **open-set read**, not any AWS call or
   the horizon predicate.

**Conclusion for this section**: there is no found documented rule
forbidding an AWS call inside the existing lease; S3 calls already happen
there. A Batch `find_job_by_name` re-query inserted inside the
`attempt_lease` block in `_reconcile_unresolved` would be consistent with
the existing pattern (S3 calls already sit there) but would be a **new**
kind of call in that specific block (Batch, not S3) with no test coverage of
that shape today (§6) and no documented invariant either permitting or
forbidding it explicitly by name.

---

## 6. Reconciler tests covering this loop and the horizon path

### 6.1 Test files in `pipeline/reconciler/test/`

```
test_closure.py, test_horizons.py, test_intent_closure.py, test_main.py,
test_retention.py, test_scheduler.py, test_service.py,
test_supersede_lost_evidence.py
```
plus non-`test_*` live-probe scripts: `live_fixa_probe.py`,
`live_fixc_crash_boundary.py`, `live_w8_battery.py` (and matching
`run-*.sh` launchers) — these look like scripts run against a live/`rapid-db`
target rather than unit tests, based on their names; not read in full (out
of scope for the ambiguity-path question, flagged for completeness only).

### 6.2 Files exercising `_reconcile_unresolved` / the submission horizon directly

- **`test_service.py`** — the primary coverage:
  - `test_an_unresolved_child_inside_its_horizon_waits` (line 152):
    builds a row with `scheduler_job_id=None`, `submitted_at` 10 minutes
    before a fixed `now`, asserts `summary["waiting"] == 1`,
    `summary["deferred"] == 0`.
  - `test_an_unresolved_child_past_its_horizon_is_classified` (line 457):
    same shape but `submitted_at` 1 hour before `now` (past the 30-minute
    horizon), asserts `summary["classified"] == 1` and
    `conn.rows[1]["lifecycle_state"] == "terminal_without_start"`.
  - `test_a_never_resolved_attempt_gets_its_bundle_before_it_closes`
    (line 1107) and
    `test_a_never_resolved_attempt_defers_if_its_bundle_cannot_be_written`
    (line 1137) — cover the bundle-first ordering inside the fall-through
    branch (§1.3).
  - A reference at line 1275 to the "unresolved path, which eventually
    closed it `never_resolved`" in a comment for a different test (not
    read in full; flagged as a further citation not yet traced).
  - `test_an_unresolvable_id_on_a_row_that_ran_is_contradictory` (line 507,
    in `ConstraintFidelityTests`) — covers the `_attempt_ran` branch
    (`missing_or_contradictory` outcome) inside the same fall-through, for
    a row with a `scheduler_job_id` Batch never heard of.
- **`test_horizons.py`** — exists specifically for `horizons.py`; not read
  in full in this pass (out of the four explicitly named files, though
  clearly relevant) — **flagged as not yet read**, likely covers
  `beyond_submission_horizon`/`beyond_grace_horizon` in isolation given the
  module's own stated purpose ("a one-line change with a test that names
  the value", `horizons.py:15-16`).

### 6.3 Fakes/fixtures for time, DB, and AWS (from `stubs.py`)

- **Time**: `ReconcilerService(..., now=lambda: now)` — every test's
  `build()` helper injects a fixed `now` via the constructor's `now=`
  parameter (`test_service.py:16-26`); no wall-clock dependency. `utc(...)`
  helper constructs aware UTC datetimes (imported from `stubs`,
  `test_service.py:8`).
- **DB**: `FakeConnection` (`stubs.py:121`) and `FakeCursor` (`stubs.py:86`)
  — in-memory row store standing in for `psycopg2`'s connection/cursor
  interface; `conn.rows`, `conn.statements`, `conn.rollbacks` are inspected
  directly by tests (e.g. `conn.rows[1]["lifecycle_state"]` at line 467,
  `conn.rollbacks` at line 114). `FakeConnection(rows=rows,
  lease_granted=lease_granted)` — takes an explicit `lease_granted` flag,
  meaning the fake can simulate the advisory-lock contention path too.
- **AWS**: `FakeBatch` (`stubs.py:16`) stands in for the Batch client
  (`self.batch`); `FakeS3Tagging` (`stubs.py:56`) stands in for
  `self.s3`; `InMemoryObjectStore` (imported from
  `pipeline.runtime.boundaries`, `test_service.py:10`) stands in for
  `records_store`/`diagnostics_store`. `FakeClientError` (`stubs.py:37`)
  simulates AWS-style client errors.
- **`attempt_row(...)` / `batch_job(...)`** (`stubs.py:258`, `:296`) — the
  row/job builder helpers used throughout; `attempt_row`'s docstring is
  "A submitted attempt row with the fields the reconciler reads" and its
  default dict is a full `_OPEN_COLUMNS`-shaped row (confirms §3.2's field
  list against the fixture actually used in tests).

**Not found**: no fake or stub for `find_job_by_name` /
`scheduler.batch_describer` in `stubs.py`'s grep output for the reconciler's
own tests — consistent with §4.4's finding that nothing in `service.py`
calls that function today. (`test_scheduler.py` may test
`find_job_by_name`/`batch_describer` directly against `scheduler.py` in
isolation — not read in this pass; flagged as not yet checked, but it is a
`scheduler.py`-scoped test file, not a `service.py`-integration one, per its
name.)

---

## Flagged as undetermined (not traced — outside the four named files or outside this read)

1. Whether `submission.protocol` (named in the `horizons.py` docstring,
   §2.1) actually exists in this repo and is wired to anything — not
   checked; `service.py` itself has zero references to it (§4.4).
2. Whether `_OPEN_COLUMNS`/the row as selected carries enough (directly or
   via joinable fields) to reconstruct the exact Batch job **name** (as
   opposed to job **id**) `find_job_by_name` requires — `batch_id` and
   `job_name` are not in `_OPEN_COLUMNS`; whether they exist elsewhere on
   the `attempts` table or a joined table was not checked.
3. Full contents of `test_horizons.py` and `test_scheduler.py` — not read;
   likely relevant to a reviewer replacing the truth source but out of the
   four explicitly named files for this pass.
4. The comment at `test_service.py:1275` referencing "the unresolved path,
   which eventually closed it `never_resolved`" — the surrounding test was
   not read in full.
5. The `live_fix*` / `live_w8_battery.py` scripts and their `run-*.sh`
   launchers in the test directory — not opened; appear to be live-DB
   probes rather than unit tests based on naming alone.
