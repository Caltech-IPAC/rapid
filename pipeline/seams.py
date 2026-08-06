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
from submission.submit import S3ManifestStore, submit_batch

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

    The manifest is published first so its checksum is known; the rows carry
    that checksum in their execution binding, so an attempt always knows
    exactly which manifest it was submitted under.
    """
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    manifest = Manifest(units=list(units), batch_id=run_id, job_type=job_type)
    batch = Batch(manifest=manifest, reason=reason)

    store = S3ManifestStore(manifest_bucket, prefix=manifest_prefix,
                            client=s3_client)
    submission = submit_batch(
        batch=batch, job_queue=queue, job_definition=job_definition,
        store=store, client=batch_client, job_name=job_name)

    bound = ExecutionBinding(
        job_definition_arn=binding.job_definition_arn,
        job_definition_rev=binding.job_definition_rev,
        image_digest=binding.image_digest,
        release_identity=binding.release_identity,
        manifest_checksum=submission.manifest_checksum,
    )

    writer = AttemptWriter(execute)
    attempt_ids = _precreate(writer, submission, bound, moment)

    logger.info("submitted %s batch %s as job %s (%d children, %d rows)",
                job_type, submission.batch_id, submission.job_id,
                submission.array_size, len(attempt_ids))
    return submission, attempt_ids


def _precreate(writer, submission, binding, moment):
    """One logical job and one attempt row per array child.

    The logical_job_id MUST be the id the runtime will resolve with — the
    manifest unit's key — because `resolve_attempt` claims the pre-created row
    by matching on it. A submitter that keys rows differently (by batch and
    index, say) creates rows the runtime can never claim: every child then
    makes a second row, and every pre-created row is orphaned in `submitted`
    until a horizon classifies it. The two sides agreeing is the whole
    mechanism.
    """
    attempt_ids = []
    for index, unit in enumerate(submission.manifest.units):
        logical_job_id = unit.key
        child = submission.child_job_id(index) if submission.array_size > 1 \
            else submission.job_id

        writer.create_logical_job(
            logical_job_id, submission.batch_id, binding,
            scheduler_job_id=child)

        from observability.attempts import AttemptIdentity

        attempt_ids.append(writer.create_submitted(
            AttemptIdentity(
                run_id=submission.batch_id,
                logical_job_id=logical_job_id,
                exposure_id=unit.exposure, sca=unit.sca,
                sky_tile=getattr(unit.facts, "rtid", None)),
            created_at=moment, submitted_at=moment,
            scheduler_job_id=child, binding=binding))
    return attempt_ids


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
    return register_batch(conn, rows, register=register)
