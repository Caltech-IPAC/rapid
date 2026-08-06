"""The VPO's three seams onto the new chain.

The operations design owns the ground-up VPO; this co-design takes only the
touchpoints the payload forces. Those are exactly three, and they are here
rather than inline in `virtualPipelineOperator.py` so each is testable on its
own and so the VPO's own structure is left alone.

  submit_units      submission through the manifest/array layer, with attempt
                    rows pre-created BEFORE the children can start
  wait_for_completion   the completion wait, reading the attempt table the
                    reconciler maintains, with a stated timeout
  run_registration  registration invoked as the records consumer

What each replaces:

  submit_units      `run_tool([python, awsBatchSubmitJobs_launch*.py, ...])`
                    — a subprocess exec of a launcher that submitted one
                    `submit_job` per unit and wrote a per-job `.ini` to S3.
  wait_for_completion   a `while True` polling `describe_jobs` ONE JOB PER
                    CALL from a one-shot database snapshot, with no timeout —
                    a stuck job waited forever.
  run_registration  `run_tool([python, parallelRegisterCompletedJobsInDB.py,
                    date])` — the log-grep chain.
"""

import datetime
import logging
import time

from observability.attempts import (
    AttemptWriter, ExecutionBinding, LifecycleState)
from submission.batching import Batch
from submission.manifest import Manifest
from submission.submit import S3ManifestStore, publish_manifest, submit_batch

logger = logging.getLogger("rapid.seams")

#: How long the VPO waits for a batch to finish before handing it to
#: reconciliation and moving on. The old wait had no timeout at all: a stuck
#: job blocked the operator forever. This turns that into a bounded wait and a
#: reconciliation case — the reconciler will classify the stragglers within
#: its own horizons whether or not anyone is watching.
DEFAULT_COMPLETION_TIMEOUT = 6 * 60 * 60

#: Cadence of the completion poll. The attempt table is cheap to query and the
#: reconciler updates it every 60s, so there is nothing to gain from polling
#: faster than it writes.
DEFAULT_POLL_SECONDS = 60

#: Lifecycle states that mean the reconciler is done with an attempt.
_TERMINAL = (
    LifecycleState.TERMINAL_AFTER_START.value,
    LifecycleState.TERMINAL_WITHOUT_START.value,
)

_PROGRESS_SQL = (
    "SELECT lifecycle_state, count(*) FROM attempts"
    " WHERE run_id = %s GROUP BY lifecycle_state"
)


class CompletionTimeout(RuntimeError):
    """The batch did not finish inside the stated timeout.

    Not an error in the pipeline: a bounded wait expiring is the *designed*
    outcome for a stuck job. The caller records it and moves on; the
    reconciler owns what happens to the attempts.
    """

    def __init__(self, message, run_id=None, outstanding=None):
        super().__init__(message)
        self.run_id = run_id
        self.outstanding = outstanding


class SubmissionFailed(RuntimeError):
    """`SubmitJob` failed after the attempt rows were pre-created.

    Not a lost-work case and deliberately not a rollback: the rows exist and
    are correct, they simply have no scheduler job to point at. They sit in
    `submitted` with a NULL scheduler_job_id and are classified by the
    reconciler at the submission-anchored horizon — which is exactly the case
    that horizon was built for ("a pre-created child whose scheduler
    identifier never resolves is bounded by a submission-anchored horizon").

    Deleting the rows instead would be the wrong repair: it destroys the only
    evidence that work was intended, and it races a child that may in fact be
    running (a `submit_job` that times out on the client side may well have
    been accepted).
    """

    def __init__(self, message, run_id=None, attempt_ids=()):
        super().__init__(message)
        self.run_id = run_id
        self.attempt_ids = tuple(attempt_ids)


def submit_units(units, job_type, queue, job_definition, binding,
                 manifest_bucket, manifest_prefix, s3_client, batch_client,
                 execute, run_id=None, reason="vpo", job_name=None,
                 now=None):
    """Submit one array job for `units`, with its attempt rows pre-created.

    ORDER MATTERS, and it is the reason this is one function rather than two
    calls the VPO makes in sequence: the rows are created BEFORE `submit_job`
    returns children that could start. A child that starts before its row
    exists finds no logical job, cannot copy an execution binding, and is
    flagged `missing_or_contradictory` by the resolver — correct behaviour on
    the resolver's part, and a self-inflicted wound on the submitter's.

    That was the stated contract and the code did the opposite (review finding
    #2): `submit_batch` ran first and `_precreate` after it, so the race this
    function exists to prevent was live. The order below is now the documented
    one, and the ordering itself is asserted by a test.

    **The scheduler job ids therefore arrive second, and that is the point.**
    A row cannot carry a child job id that Batch has not assigned yet, so the
    rows are created without one and backfilled after `SubmitJob` returns —
    the ordering `observability.attempts` was built for and documents ("Array
    children are rows at submission time... then backfilled by
    `backfill_scheduler_job_ids`. That ordering is the point: a child whose
    identifier never resolves is left as a detectable reconciliation case
    rather than never existing at all").

    The manifest is published first, before the rows, because the rows carry
    its checksum in their execution binding — an attempt must always know
    exactly which manifest it was submitted under.
    """
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    manifest = Manifest(units=list(units), batch_id=run_id, job_type=job_type)
    batch = Batch(manifest=manifest, reason=reason)

    store = S3ManifestStore(manifest_bucket, prefix=manifest_prefix,
                            client=s3_client)

    # 1. Publish the manifest. Its checksum is part of the binding the rows
    #    carry, so it has to exist before they do.
    manifest_uri = publish_manifest(batch.manifest, store)

    bound = ExecutionBinding(
        job_definition_arn=binding.job_definition_arn,
        job_definition_rev=binding.job_definition_rev,
        image_digest=binding.image_digest,
        release_identity=binding.release_identity,
        manifest_checksum=batch.manifest.checksum(),
    )

    # 2. The rows, BEFORE SubmitJob. No scheduler job ids yet — nothing has
    #    assigned any.
    writer = AttemptWriter(execute)
    attempt_ids = _precreate(writer, batch.manifest, run_id, bound, moment)

    # 3. Submit. A failure here leaves the rows as reconciliation cases, not
    #    orphans: they are correct, they simply never got a scheduler job.
    try:
        submission = submit_batch(
            batch=batch, job_queue=queue, job_definition=job_definition,
            store=store, client=batch_client, job_name=job_name,
            manifest_uri=manifest_uri)
    except Exception as exc:
        logger.error(
            "SubmitJob failed for run %s after %d attempt row(s) were "
            "pre-created; the rows remain as reconciliation cases and are "
            "classified at the submission-anchored horizon: %s",
            batch.manifest.batch_id, len(attempt_ids), exc)
        raise SubmissionFailed(
            f"SubmitJob failed for run {batch.manifest.batch_id} after "
            f"{len(attempt_ids)} attempt row(s) were pre-created: {exc}",
            run_id=batch.manifest.batch_id,
            attempt_ids=attempt_ids) from exc

    # 4. Backfill the child job ids the scheduler has now assigned.
    _bind_scheduler_jobs(writer, submission, attempt_ids)

    logger.info("submitted %s batch %s as job %s (%d children, %d rows)",
                job_type, submission.batch_id, submission.job_id,
                submission.array_size, len(attempt_ids))
    return submission, attempt_ids


def _precreate(writer, manifest, run_id, binding, moment):
    """One logical job and one attempt row per array child, before SubmitJob.

    The logical_job_id MUST be the id the runtime will resolve with — the
    manifest unit's RUN-SCOPED key — because `resolve_attempt` claims the
    pre-created row by matching on it. A submitter that keys rows differently
    creates rows the runtime can never claim: every child then makes a second
    row, and every pre-created row is orphaned in `submitted` until a horizon
    classifies it. The two sides agreeing is the whole mechanism.

    **The key is run-scoped (review finding #3).** It used to be `unit.key` —
    exposure/SCA — which is a global identity: `logical_jobs` has a global
    primary key on it, so reprocessing one exposure/SCA under a second run hit
    the `ON CONFLICT DO NOTHING` and silently retained the FIRST run's
    execution binding. A scheduler retry then copied that stale manifest,
    image, release and run identity onto a row belonging to the new run. The
    key now carries the run, so two runs over one exposure/SCA are two logical
    jobs, which is what they are.

    No `scheduler_job_id` is passed: Batch has not assigned one yet, and that
    is precisely why this runs before `SubmitJob`. `_bind_scheduler_jobs`
    fills them in afterwards.
    """
    from observability.attempts import AttemptIdentity

    attempt_ids = []
    for index, unit in enumerate(manifest.units):
        logical_job_id = unit.logical_job_key(manifest.batch_id)

        writer.create_logical_job(
            logical_job_id, manifest.batch_id, binding)

        attempt_ids.append(writer.create_submitted(
            AttemptIdentity(
                run_id=manifest.batch_id,
                logical_job_id=logical_job_id,
                exposure_id=unit.exposure, sca=unit.sca,
                sky_tile=getattr(unit.facts, "rtid", None)),
            created_at=moment, submitted_at=moment,
            binding=binding))
    return attempt_ids


def _bind_scheduler_jobs(writer, submission, attempt_ids):
    """Backfill the child job ids Batch assigned, after SubmitJob returned.

    Batch names array children `<parent>:<index>`, so the ids are derivable
    from the parent and the index. A single-unit batch is a plain job (Batch
    rejects arraySize 1) and its one row takes the parent id itself.

    The backfill is guarded in SQL (`WHERE scheduler_job_id IS NULL`), so a
    replayed submission cannot overwrite an id already recorded, and the
    writer raises if it cannot verify the row count — an unverifiable backfill
    is not a backfill.
    """
    assignments = []
    for index, attempt_id in enumerate(attempt_ids):
        child = submission.child_job_id(index) if submission.array_size > 1 \
            else submission.job_id
        assignments.append((attempt_id, child))

    updated = writer.backfill_scheduler_job_ids(assignments)
    if updated != len(assignments):
        # Not fatal: the rows exist and the reconciler can still find them by
        # logical job. But it means some child is unaddressable by scheduler
        # id until it resolves itself, which is worth saying loudly.
        logger.warning(
            "backfilled %d of %d scheduler job ids for run %s; the remainder "
            "are reconciliation cases until their runtimes resolve them",
            updated, len(assignments), submission.batch_id)
    return updated


def wait_for_completion(conn, run_id, timeout=DEFAULT_COMPLETION_TIMEOUT,
                        poll_seconds=DEFAULT_POLL_SECONDS, sleep=time.sleep,
                        monotonic=time.monotonic, on_poll=None):
    """Wait until every attempt of `run_id` is reconciler-terminal.

    Reads the attempt table, not Batch. Three reasons the table is the right
    source and `describe_jobs` is not:

    - The reconciler already maintains it, and it is the *reconciled* view: an
      attempt is terminal here only once its scheduler truth is known.
    - It is one query for the whole batch, where the old wait made one
      `describe_jobs` call per job per poll.
    - It survives the scheduler forgetting. Batch drops jobs from its history
      eventually; the attempt row is permanent.

    Raises `CompletionTimeout` when the timeout expires. That is a designed
    outcome, not a failure: the caller records it and moves on, and the
    reconciler classifies the stragglers on its own horizons.
    """
    deadline = monotonic() + timeout

    while True:
        counts = _progress(conn, run_id)
        outstanding = sum(n for state, n in counts.items()
                          if state not in _TERMINAL)
        if on_poll is not None:
            on_poll(counts, outstanding)

        if not counts:
            logger.warning("no attempt rows for run %s; nothing to wait for",
                           run_id)
            return counts
        if outstanding == 0:
            logger.info("run %s complete: %s", run_id, counts)
            return counts

        if monotonic() >= deadline:
            raise CompletionTimeout(
                f"run {run_id} still has {outstanding} attempt(s) outstanding "
                f"after {timeout}s: {counts}. This is a reconciliation case — "
                f"the reconciler classifies them on its own horizons.",
                run_id=run_id, outstanding=outstanding)

        sleep(poll_seconds)


def _progress(conn, run_id):
    with conn.cursor() as cur:
        cur.execute(_PROGRESS_SQL, (run_id,))
        counts = {state: count for state, count in cur.fetchall()}
    conn.rollback()
    return counts


def run_registration(conn, register=None):
    """Invoke registration as the records consumer.

    A function call, not a subprocess: the four scripts this replaces had no
    importable entry point at all — each was a `__main__` block — which is
    precisely why the VPO had to exec them and why their exit codes were the
    only channel back. The run's counts are returned directly.
    """
    from pipeline.registration import candidates, register_batch

    rows = candidates(conn)
    logger.info("registration: %d reconciled attempt(s) to consider", len(rows))
    # A caller with no registrar gets a DECISION pass, asked for explicitly
    # (review finding #5): omitting the callback used to become a dry run
    # whose decisions were reported as registrations.
    return register_batch(conn, rows, register=register,
                          dry_run=register is None)
