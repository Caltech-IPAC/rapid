"""The ambiguous-submission protocol: a durable record, not a stopwatch.

Conformance rule 7: "No transaction spans `SubmitJob`. An ambiguous submission
resolves through the durable submission-row protocol (PREPARED -> CALLING ->
BOUND / UNKNOWN -> FOUND / LOST); the API call is never repeated for a
submission row."

## What this replaces

Nothing — that is the point. At 820dd40 there was no protocol: an ambiguous
`submit_job` outcome left pre-created attempt rows with a NULL
`scheduler_job_id`, and the only thing that ever resolved them was
`pipeline.reconciler.horizons.beyond_submission_horizon` — thirty minutes of
elapsed time, pure arithmetic over `submitted_at`. `pipeline.seams`'s
`SubmissionFailed` docstring is exactly right that those rows must not be
deleted ("a `submit_job` that times out on the client side may well have been
accepted"), and exactly that observation is why a timeout cannot resolve them:
if the call may have been accepted, then waiting does not discover whether it
was. A job that IS running and a job that never reached the API look identical
to a clock. Only Batch can tell them apart, and only if something asks.

## The three things this module is careful about

**1. IT WRAPS THE EXISTING ORDER, IT DOES NOT REPLACE IT** (brief C1: "Keep
the existing pre-create-then-bind ordering — the protocol wraps it, it does
not replace it"). `pipeline.seams.submit_units` publishes the manifest, then
pre-creates attempt rows, then calls `submit_batch`, then backfills scheduler
ids, and the ordering comment there explains at length why the rows must exist
before children can start. All of that is untouched. This module adds a row
before that sequence and a state write after it.

**2. THE CALLING WRITE MUST COMMIT BEFORE THE CALL.** `mark_calling` is
useless unless it is durable at the instant the API call goes out — a state
written in an uncommitted transaction is invisible to the pass that later
finds the wreckage, which is the only reader that matters. So this module's
callers commit between `mark_calling` and `submit_batch`. That is not a
transaction spanning SubmitJob (rule 7's first clause, which still holds and
which this must not break); it is the opposite — the transaction is closed
BEFORE the call precisely so no transaction is open during it.

**3. `submit_job` IS NEVER RE-CALLED FOR A ROW.** Resolution is by re-query
only. Where a submission is genuinely `lost`, the work is resubmitted as a NEW
submission row with a new identity — so "how many times did we call Batch for
this work" is answerable by counting rows, and no code path exists that could
call twice for one. DRAFT migration 044 enforces the same thing at the schema
(`submissions_call_once_ck`) rather than trusting this docstring.

## Resolving an UNKNOWN

By deterministic identity. `submission.submit.build_submit_kwargs` names every
job `rapid-{manifest.batch_id}`, a pure function of the batch identity, and
Batch's `ListJobs` filters by job name — so "does a job for this submission
exist" is answerable without ever having received a `jobId`. That is what
makes a POSITIVE re-query possible, and it is why the name is stored on the
row rather than re-derived at resolution time: the query must ask for the name
that WAS used, not the one today's convention would build.

A negative answer is not immediately conclusive. Batch is eventually
consistent, and a job submitted milliseconds before a `ListJobs` call can be
absent from its result. So a negative re-query before the row's
`resolution_deadline` leaves it `unknown` for the next pass, and only a
negative re-query past that deadline concludes `lost`. That is the horizon
surviving as the brief specifies — "a backstop for scheduler-side silence...
it acts on a record that says CALLING/UNKNOWN — the state machine, not the
timestamp, is the truth". The timestamp no longer decides WHAT happened; it
only bounds how long the re-query keeps saying "not yet".
"""

import datetime
import logging

logger = logging.getLogger("rapid.submission.protocol")

# -- the six submission states, verbatim from DRAFT migration 044 ------------
PREPARED = "prepared"
CALLING = "calling"
BOUND = "bound"
UNKNOWN = "unknown"
FOUND = "found"
LOST = "lost"

SUBMISSION_STATES = frozenset({
    PREPARED, CALLING, BOUND, UNKNOWN, FOUND, LOST,
})

#: How long a `calling`/`unknown` row keeps being re-queried before a negative
#: answer is allowed to mean `lost`. Matches
#: `pipeline.reconciler.horizons.SUBMISSION_HORIZON_SECONDS` deliberately: the
#: horizon's DURATION was never the defect — thirty minutes is a reasonable
#: bound on Batch's visibility lag — and changing it here would silently make
#: two mechanisms disagree while this one is being adopted. What changed is
#: what the horizon DECIDES: it no longer classifies a submission, it only
#: bounds how long "not visible yet" stays credible.
RESOLUTION_HORIZON_SECONDS = 30 * 60


class SubmissionProtocolError(RuntimeError):
    """Base class for this module's errors."""


class SubmissionStateConflict(SubmissionProtocolError):
    """A CAS-guarded submission transition matched no row.

    Either the row does not exist, or it has already left the state this
    transition expected — meaning another writer resolved it first. Mirrors
    `pipeline.intent.writer.WorkUnitNotFound` exactly, for the same reason:
    the interesting case is not "missing" but "someone else got there", and a
    caller that cannot tell those apart from a rowcount will paper over a
    concurrency bug.
    """


_INSERT_SQL = (
    "INSERT INTO submissions"
    "  (run_id, job_type, job_name, job_queue, job_definition,"
    "   manifest_checksum, manifest_uri, array_size, state, created_at,"
    "   updated_at)"
    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'prepared', %s, %s)"
    " RETURNING submission_id"
)

#: Every transition below is a CAS on the expected state, for the same reason
#: `transition_unit`'s is: two passes may reach one row, and the second must
#: discover that rather than overwrite the first's account.
_MARK_CALLING_SQL = (
    "UPDATE submissions"
    "   SET state = 'calling', call_started_at = %s, updated_at = %s"
    " WHERE submission_id = %s AND state = 'prepared'"
)

_MARK_BOUND_SQL = (
    "UPDATE submissions"
    "   SET state = 'bound', scheduler_job_id = %s, resolved_at = %s,"
    "       updated_at = %s"
    " WHERE submission_id = %s AND state = 'calling'"
)

_MARK_UNKNOWN_SQL = (
    "UPDATE submissions"
    "   SET state = 'unknown', ambiguity_detail = %s,"
    "       resolution_deadline = %s, updated_at = %s"
    " WHERE submission_id = %s AND state = 'calling'"
)

_MARK_FOUND_SQL = (
    "UPDATE submissions"
    "   SET state = 'found', scheduler_job_id = %s, resolved_at = %s,"
    "       updated_at = %s"
    " WHERE submission_id = %s AND state IN ('calling', 'unknown')"
)

_MARK_LOST_SQL = (
    "UPDATE submissions"
    "   SET state = 'lost', resolved_at = %s, updated_at = %s"
    " WHERE submission_id = %s AND state = 'unknown'"
)

_OPEN_SQL = (
    "SELECT submission_id, run_id, job_type, job_name, job_queue,"
    "       job_definition, state, call_started_at, resolution_deadline,"
    "       ambiguity_detail"
    "  FROM submissions"
    " WHERE state IN ('calling', 'unknown')"
    " ORDER BY submission_id"
)

_AVAILABLE_SQL = (
    "SELECT 1 FROM information_schema.tables"
    " WHERE table_schema = 'public' AND table_name = 'submissions' LIMIT 1"
)

_ATTACH_SQL = (
    "UPDATE attempts SET submission_id = %s"
    " WHERE attempt_id = ANY(%s) AND submission_id IS NULL"
)

#: The reconciler's ambiguity path needs the submission's STATE (to let a
#: FOUND/LOST record decide over the clock) and its JOB_NAME/JOB_QUEUE (the
#: attempts row does not carry them — `pipeline.reconciler.service._OPEN_
#: COLUMNS` selects no job name at all, since a re-query is keyed by name,
#: not id). Joined through `attempts.submission_id`, the FK
#: `attach_attempts` maintains, so this is one read rather than the
#: reconciler open-coding a second `submissions` query outside this module.
_SUBMISSION_FOR_ATTEMPT_SQL = (
    "SELECT s.submission_id, s.state, s.job_name, s.job_queue,"
    "       s.resolution_deadline"
    "  FROM submissions s"
    "  JOIN attempts a ON a.submission_id = s.submission_id"
    " WHERE a.attempt_id = %s"
)


def is_available(execute):
    """Is DRAFT 044's `submissions` table present in this database?

    PROBED, NEVER ASSUMED. The drafts on this branch are not in the deployed
    migration stream, so every caller — production and contract test alike —
    must be able to ask before using the protocol. `submit_units` degrades to
    its pre-protocol behaviour when this is False, which is what keeps the
    submission path working against the deployed schema while the change
    request is pending.
    """
    return bool(execute(_AVAILABLE_SQL, []))


def prepare(execute, *, run_id, job_type, job_name, job_queue, job_definition,
            manifest_checksum, manifest_uri, array_size, now=None):
    """Open a submission record in PREPARED. Returns its submission_id.

    Called BEFORE anything is submitted and before the API call is even
    contemplated — the manifest is published and the attempt rows exist, but
    nothing has been asked of Batch. A crash with a row in this state loses
    nothing, because no job can exist: that is the whole meaning of the state.
    """
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    rows = execute(_INSERT_SQL, [
        run_id, job_type, job_name, job_queue, job_definition,
        manifest_checksum, manifest_uri, int(array_size), moment, moment,
    ])
    submission_id = _single_value(rows)
    logger.info("submission %s prepared for run %s (%s, %d children)",
                submission_id, run_id, job_type, array_size)
    return submission_id


def mark_calling(execute, submission_id, now=None):
    """PREPARED -> CALLING. **The caller must COMMIT before calling Batch.**

    This is the load-bearing write of the whole protocol and the only one
    whose durability timing matters. A row that says `calling` means "a
    request was in flight"; if that fact is still sitting in an uncommitted
    transaction when the process dies, the next pass sees `prepared` and
    concludes no call was made — which is the exact wrong answer, and worse
    than having no protocol at all, because it is a confident wrong answer.

    Committing here does NOT violate rule 7's "no transaction spans
    SubmitJob". It is the mechanism by which no transaction spans it: the
    transaction is closed before the call goes out, leaving none open across
    it.
    """
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    _require_one(execute(_MARK_CALLING_SQL, [moment, moment, submission_id]),
                 submission_id, PREPARED)
    logger.info("submission %s calling", submission_id)


def mark_bound(execute, submission_id, scheduler_job_id, now=None):
    """CALLING -> BOUND. The happy path's end."""
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    _require_one(
        execute(_MARK_BOUND_SQL,
                [str(scheduler_job_id), moment, moment, submission_id]),
        submission_id, CALLING)
    logger.info("submission %s bound to job %s",
                submission_id, scheduler_job_id)


def mark_unknown(execute, submission_id, detail, horizon_seconds=None,
                 now=None):
    """CALLING -> UNKNOWN: the call's outcome could not be judged.

    `detail` is the exception's class and message, recorded so an operator can
    tell a client-side timeout from a throttle from a credential failure
    without reading logs — three ambiguities with very different likelihoods
    of the job actually existing.

    The deadline is set here rather than derived at read time so the horizon
    that applies to a row is the one in force when it became ambiguous. A
    deployment that changes `RESOLUTION_HORIZON_SECONDS` then does not
    retroactively re-judge rows already waiting under the old bound.
    """
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    seconds = (RESOLUTION_HORIZON_SECONDS if horizon_seconds is None
               else horizon_seconds)
    deadline = moment + datetime.timedelta(seconds=seconds)
    _require_one(
        execute(_MARK_UNKNOWN_SQL,
                [str(detail)[:2000], deadline, moment, submission_id]),
        submission_id, CALLING)
    logger.warning(
        "submission %s is UNKNOWN (%s); it resolves by re-querying Batch for "
        "its job name, never by re-calling submit_job. Deadline %s",
        submission_id, detail, deadline)


def mark_found(execute, submission_id, scheduler_job_id, now=None):
    """CALLING/UNKNOWN -> FOUND: the re-query located the job.

    Admits `calling` as well as `unknown` because a resolution pass may reach
    a row that was interrupted mid-call and never judged — the process died
    between the API call and either outcome write. That row is exactly as
    ambiguous as an `unknown` one and is resolved identically; requiring it to
    be moved to `unknown` first would be a state transition recording nothing
    but bookkeeping.
    """
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    _require_one(
        execute(_MARK_FOUND_SQL,
                [str(scheduler_job_id), moment, moment, submission_id]),
        submission_id, f"{CALLING} or {UNKNOWN}")
    logger.info("submission %s FOUND as job %s by identity re-query",
                submission_id, scheduler_job_id)


def mark_lost(execute, submission_id, now=None):
    """UNKNOWN -> LOST: a negative re-query, past the deadline.

    Only from `unknown`, and only the caller that has checked the deadline
    should call this — `resolve` below is that caller, and it is where the
    two conditions are enforced together. Declaring `lost` is the one
    conclusion that authorizes resubmitting the work, so it is the one that
    must not be reached casually.
    """
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    _require_one(execute(_MARK_LOST_SQL, [moment, moment, submission_id]),
                 submission_id, UNKNOWN)
    logger.warning(
        "submission %s is LOST: no job of its name exists past its deadline. "
        "The work may be resubmitted as a NEW submission row; this one is "
        "never re-called", submission_id)


def open_submissions(execute):
    """Every submission awaiting resolution, oldest first.

    `calling` and `unknown` are the two open states — one interrupted, one
    judged ambiguous — and both need the same re-query, so the resolution pass
    takes them together.
    """
    rows = execute(_OPEN_SQL, [])
    columns = ("submission_id", "run_id", "job_type", "job_name", "job_queue",
               "job_definition", "state", "call_started_at",
               "resolution_deadline", "ambiguity_detail")
    return [dict(zip(columns, row)) for row in (rows or [])]


def attach_attempts(execute, submission_id, attempt_ids):
    """Link this submission's pre-created attempt rows to it.

    Guarded on `submission_id IS NULL` so a replayed attach cannot move rows
    from one submission to another — the same posture
    `backfill_scheduler_job_ids` takes for scheduler ids ("a replayed
    submission cannot overwrite an id already recorded").
    """
    if not attempt_ids:
        return 0
    return execute(_ATTACH_SQL, [submission_id, list(attempt_ids)])


def submission_for_attempt(execute, attempt_id):
    """The submission record linked to one attempt, or `None`.

    `None` covers two cases the caller must treat alike: no submission row
    exists for this attempt at all (every pre-044 attempt, and any attempt a
    submission pass could not open a record for — submissions fails OPEN),
    or the join found nothing for another reason. Either way there is no
    durable evidence to consult, and the caller's own backstop applies.

    Read-only, no transaction of its own — one SELECT, exactly like
    `open_submissions`.
    """
    rows = execute(_SUBMISSION_FOR_ATTEMPT_SQL, [attempt_id])
    if not rows:
        return None
    columns = ("submission_id", "state", "job_name", "job_queue",
               "resolution_deadline")
    return dict(zip(columns, rows[0]))


def resolve(execute, row, describe, now=None):
    """Resolve one open submission by POSITIVE RE-QUERY. Returns its new state.

    `describe(job_name, job_queue)` is the injected Batch lookup, returning
    the scheduler job id if a job of that name exists and None if it does not.
    Injected rather than constructed so the resolution logic — which is the
    part that can be wrong — is testable without an AWS account, exactly as
    `submit_batch` takes its client (`submission.submit`'s module docstring:
    "it is what makes the submit path testable without an AWS account").

    The three outcomes:

    * **Found.** The job exists. Recorded as `found` with its id; the work is
      running and must not be resubmitted.
    * **Not found, before the deadline.** Left open. Batch is eventually
      consistent — a job submitted moments ago can be missing from a listing —
      so an early negative is "not visible yet", not "absent". The row is
      re-queried on the next pass.
    * **Not found, past the deadline.** Recorded as `lost`. This is the ONLY
      path that authorizes resubmission, and it requires both a negative
      answer AND the elapsed backstop — never elapsed time alone, which is
      precisely the defect this protocol replaces.

    A `describe` that RAISES leaves the row untouched and re-raises. An
    unreachable Batch is not evidence of anything: concluding `lost` from a
    failed query would be the same error as concluding it from a clock, and
    would authorize a duplicate submission on the strength of a network
    problem.
    """
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    submission_id = row["submission_id"]

    scheduler_job_id = describe(row["job_name"], row["job_queue"])

    if scheduler_job_id:
        # KNOWN GAP, DOCUMENTED RATHER THAN SILENTLY LEFT (finding 4,
        # fix-txn-core investigation — punted, not fixed, see
        # LEDGER-fix-txn-core.md's "punted" section for the full analysis).
        #
        # This records the PARENT submission as FOUND, with the parent's own
        # `scheduler_job_id` — but for an array submission it does NOT
        # backfill any CHILD attempt's `scheduler_job_id`, the way
        # `pipeline.seams._bind_scheduler_jobs` does for the direct
        # (never-ambiguous) success path. Note that `_OPEN_SQL` does not even
        # SELECT `array_size` today — `resolve`/`resolve_open` cannot
        # currently tell an array submission from a single-job one without
        # a further query, which any real fix here would also need to add.
        # `pipeline.seams` derives each child id as
        # `f"{parent_job_id}:{index}"` using the Python loop position
        # (`enumerate(attempt_ids)`) at submission time — state that exists
        # only in that process's memory and was never written anywhere this
        # function, running later in a different process with only a
        # `submissions` row in scope, can recover. The `attempts` rows this
        # submission created carry a `logical_job_id` keyed by the manifest
        # unit's declared SUBJECT (exposure/SCA, or another job type's typed
        # fields — see `submission.manifest.ProcessingUnit.logical_job_key`),
        # never by array index, so there is no existing column or string
        # format to parse an index back out of. Closing this gap for real
        # needs either a new `attempts` column recording each row's array
        # index at `_precreate` time, or an ordered index->logical_job_id
        # mapping persisted on the `submissions` row itself — a schema change
        # (a new DRAFT migration) that was out of scope for this pass.
        #
        # PRACTICAL IMPACT, STATED PRECISELY: only submissions that were
        # ambiguous (`calling`/`unknown`) AND array jobs (`array_size > 1`)
        # AND positively found here are affected. Their children keep NULL
        # `scheduler_job_id` even after this marks the parent FOUND — exactly
        # the same unaddressable-by-scheduler-id state
        # `SubmissionBookkeepingFailed` now prevents on the direct path, but
        # here it is not raised, because there is nothing to retry into: the
        # index mapping needed to backfill correctly does not exist yet. The
        # rows remain findable by logical job and are not lost — reconciler
        # closure keys on `attempts`, not on `submissions.scheduler_job_id` —
        # but they are not addressable by scheduler id, which the reconciler
        # uses to correlate CloudWatch/Batch events per-child.
        mark_found(execute, submission_id, scheduler_job_id, now=moment)
        return FOUND

    deadline = row.get("resolution_deadline")
    if row["state"] == CALLING:
        # Interrupted mid-call and never judged. Move it to `unknown` so it
        # carries a deadline like every other ambiguous row, and let the NEXT
        # pass apply that deadline — this pass has no bound to test against.
        mark_unknown(execute, submission_id,
                     detail="interrupted before the call outcome was recorded",
                     now=moment)
        return UNKNOWN

    if deadline is not None and moment < deadline:
        logger.info(
            "submission %s not visible in Batch yet (deadline %s); leaving it "
            "unknown for the next pass rather than concluding absence from an "
            "eventually-consistent listing", submission_id, deadline)
        return UNKNOWN

    mark_lost(execute, submission_id, now=moment)
    return LOST


def resolve_open(execute, describe, now=None):
    """Resolve every open submission. Returns a count per outcome state.

    The resolution pass. Runs wherever reconciliation runs — it is the same
    kind of work, converging a durable record against what the scheduler
    actually holds (rule 1: "Batch state, object listings, events and logs are
    evidence reconciled into it").

    One submission's failure does not stop the others: each is resolved in its
    own try, and a `describe` that raises for one row leaves that row open for
    the next pass. A pass that abandoned the remaining rows because one
    lookup timed out would make an unreachable Batch look like a stalled
    protocol.
    """
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    counts = {FOUND: 0, LOST: 0, UNKNOWN: 0, "errors": 0}

    for row in open_submissions(execute):
        try:
            state = resolve(execute, row, describe, now=moment)
            counts[state] = counts.get(state, 0) + 1
        except Exception as exc:  # noqa: BLE001 - one row must not stop the pass
            counts["errors"] += 1
            logger.warning(
                "could not resolve submission %s (%s); it stays open for the "
                "next pass — an unreachable scheduler is not evidence of "
                "anything", row.get("submission_id"), exc)

    if any(counts.values()):
        logger.info("submission resolution pass: %s", counts)
    return counts


def _require_one(result, submission_id, expected_state):
    """Verify a CAS transition matched exactly one row."""
    count = result if isinstance(result, int) else len(result or [])
    if count != 1:
        raise SubmissionStateConflict(
            f"submission {submission_id} was not in {expected_state!r}: the "
            f"compare-and-set matched {count} rows. Either it does not exist "
            f"or another writer has already resolved it.")


def _single_value(rows):
    if not rows:
        raise SubmissionProtocolError(
            "expected one returned row from the submissions INSERT, got none")
    first = rows[0]
    return first[0] if isinstance(first, (list, tuple)) else first
