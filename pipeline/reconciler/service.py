"""The reconciler service: the polling loop and one attempt's reconciliation.

Structure, top down:

  run_forever            the supervised loop — poll, sleep, repeat, never exit
    ReconcilerService.poll_once      one cycle over the open set
      _reconcile_attempt             one attempt, under its lease

`poll_once` is the unit of work and is deliberately callable on its own: the
tests drive it directly, and an operator can run exactly one cycle to see what
the service would do without leaving anything running.

The open set is the attempts that are not terminal. For each, the reconciler
asks the scheduler what it saw, and then does the least it can:

- The scheduler is still running it → record the observation, leave it open.
- The scheduler is terminal, but the grace horizon has not elapsed → record the
  observation and wait. The job may be writing its record right now.
- The scheduler is terminal and the horizon has elapsed → classify: publish a
  closure record, stamp retention on the bundle, transition the row.
- The scheduler has never heard of it and it is past the submission-anchored
  horizon → classify it as a child that never resolved.

Everything after "classify" happens inside the per-attempt lease.
"""

import datetime
import logging
import time

from observability.attempts import (
    AttemptWriter, LifecycleState, ProductDisposition, RapidOutcome)
from pipeline.runtime import termination

from . import closure as closure_mod
from . import retention as retention_mod
from .horizons import beyond_grace_horizon, beyond_submission_horizon
from .lease import attempt_lease, reread_attempt
from .scheduler import describe_in_batches, observations_for_job

logger = logging.getLogger("rapid.reconciler.service")

POLL_SECONDS = 60

# The lifecycle states that still need something from the reconciler.
OPEN_STATES = (
    LifecycleState.SUBMITTED.value,
    LifecycleState.STARTED.value,
    LifecycleState.APPLICATION_CLOSED.value,
)

# Classifications this service records, so a row says *why* it was closed.
CLASS_AGREED = "agreed"
CLASS_MATERIALIZED = "materialized_from_record"
CLASS_ABRUPT_LOSS = "abrupt_loss"
CLASS_NEVER_STARTED = "never_started"
CLASS_NEVER_RESOLVED = "never_resolved"

_OPEN_COLUMNS = (
    "attempt_id", "run_id", "logical_job_id", "scheduler_job_id",
    "lifecycle_state", "application_attempt_index", "scheduler_attempt_index",
    "exposure_id", "sca", "sky_tile", "submitted_at", "started_at", "ended_at",
    "rapid_outcome", "product_disposition", "application_intended_exit",
    "error_category", "terminal_record_key", "terminal_record_sequence",
    "terminal_record_checksum", "binding_job_definition_arn",
    "binding_job_definition_rev", "binding_image_digest",
    "binding_release_identity", "binding_manifest_checksum",
)

# Built once, from a module-level tuple of literal identifiers — there is no
# caller-supplied text anywhere in it, and the column names are validated by
# the database itself the first time the statement runs. Deliberately NOT
# composed through `psycopg2.sql`: doing so would make this module import
# psycopg2 merely to interpolate constants, and psycopg2 is absent wherever
# the unit suite runs. Dynamic identifiers — where a caller chooses the
# columns — do go through `sql.Identifier`; see `lease.reread_attempt`.
_OPEN_SET_SQL = (
    "SELECT " + ", ".join(_OPEN_COLUMNS) +
    " FROM attempts WHERE lifecycle_state = ANY(%s) ORDER BY attempt_id"
)


class ReconcilerService:
    """One reconciler. Owns no state between polls beyond its connections.

    Deliberately not a daemon class with an internal thread: `poll_once` is a
    pure-ish function of (database, scheduler, stores) so it can be tested with
    all three stubbed, and `run_forever` is the thin supervised wrapper.
    """

    def __init__(self, conn, batch_client, records_store, diagnostics_store,
                 s3_client, records_prefix, diagnostics_bucket,
                 now=None):
        self.conn = conn
        self.batch = batch_client
        self.records_store = records_store
        self.diagnostics_store = diagnostics_store
        self.s3 = s3_client
        self.records_prefix = records_prefix
        self.diagnostics_bucket = diagnostics_bucket
        self._now = now or (
            lambda: datetime.datetime.now(datetime.timezone.utc))

    # -- the open set ----------------------------------------------------

    def open_attempts(self):
        """Every attempt the reconciler might still owe something.

        Read outside any lease: this is the candidate list, and each candidate
        is rechecked under its own lock before anything is written.
        """
        with self.conn.cursor() as cur:
            cur.execute(_OPEN_SET_SQL, (list(OPEN_STATES),))
            names = [description[0] for description in cur.description]
            rows = [dict(zip(names, row)) for row in cur.fetchall()]
        self.conn.rollback()  # a read-only snapshot; do not hold a transaction
        return rows

    # -- one cycle -------------------------------------------------------

    def poll_once(self):
        """One reconciliation cycle. Returns a summary dict for the log."""
        rows = self.open_attempts()
        summary = {"open": len(rows), "observed": 0, "classified": 0,
                   "skipped": 0, "deferred": 0, "errors": 0}
        if not rows:
            return summary

        by_job = {}
        unresolved = []
        for row in rows:
            job_id = row.get("scheduler_job_id")
            if job_id:
                by_job.setdefault(job_id, []).append(row)
            else:
                unresolved.append(row)

        observations = self._observe(list(by_job))
        summary["observed"] = len(observations)

        for job_id, attempts in by_job.items():
            for row in attempts:
                try:
                    outcome = self._reconcile_attempt(
                        row, observations.get(job_id, []))
                except Exception:  # noqa: BLE001 - one bad attempt must not
                    # take the cycle down; the next poll retries it.
                    logger.exception("reconciling attempt %s failed",
                                     row.get("attempt_id"))
                    summary["errors"] += 1
                else:
                    summary[outcome] = summary.get(outcome, 0) + 1

        for row in unresolved:
            try:
                outcome = self._reconcile_unresolved(row)
            except Exception:  # noqa: BLE001
                logger.exception("reconciling unresolved attempt %s failed",
                                 row.get("attempt_id"))
                summary["errors"] += 1
            else:
                summary[outcome] = summary.get(outcome, 0) + 1

        logger.info("poll: %s", summary)
        return summary

    def _observe(self, job_ids):
        """Describe every open job, batched, and index observations by job id."""
        found = {}
        for chunk in describe_in_batches(self.batch, job_ids):
            for job in chunk.jobs:
                found[job.get("jobId")] = observations_for_job(job)
        return found

    # -- one attempt -----------------------------------------------------

    def _pick_observation(self, row, observations):
        """Which of a job's attempt observations belongs to this row.

        Matched on the attempt index where both sides have one — a job with a
        retry history has several observations and several rows, and pairing
        them wrongly would attribute one attempt's exit code to another. Where
        the row has no index yet, a single observation is unambiguous.
        """
        if not observations:
            return None
        index = row.get("application_attempt_index") or row.get(
            "scheduler_attempt_index")
        if index is not None:
            for observation in observations:
                if observation.attempt_index == index:
                    return observation
            # An index that matches nothing means the scheduler's history and
            # our rows disagree about how many attempts there were. Fall
            # through to the single-observation case only when there is one.
        if len(observations) == 1:
            return observations[0]
        return None

    def _reconcile_attempt(self, row, observations):
        attempt_id = row["attempt_id"]
        observation = self._pick_observation(row, observations)

        if observation is None:
            # The scheduler returned the job but not an attempt we can pair.
            # Treat as unresolved: the submission-anchored horizon applies.
            return self._reconcile_unresolved(row)

        # Scheduler observations are recorded whatever the state — they are
        # this service's to author, and recording them early means an operator
        # can see queue and start times before anything terminal happens.
        self._record_observation(attempt_id, observation)

        if not observation.is_terminal:
            return "deferred"

        if not beyond_grace_horizon(observation.stopped_at, now=self._now()):
            logger.debug("attempt %s is scheduler-terminal but inside the "
                         "grace horizon; leaving it open", attempt_id)
            return "deferred"

        return self._classify(row, observation)

    def _record_observation(self, attempt_id, observation):
        writer = AttemptWriter(_Executor(self.conn))
        writer.record_scheduler_observation(
            attempt_id,
            scheduler_state=observation.state,
            created_at=observation.created_at,
            started_at=observation.started_at,
            stopped_at=observation.stopped_at,
            attempt_index=observation.attempt_index)
        self.conn.commit()

    # -- classification, under the lease ---------------------------------

    def _classify(self, row, observation):
        attempt_id = row["attempt_id"]
        with attempt_lease(self.conn, attempt_id) as held:
            if not held:
                return "skipped"

            current = reread_attempt(self.conn, attempt_id,
                                     columns=_OPEN_COLUMNS)
            if current is None or current["lifecycle_state"] not in OPEN_STATES:
                # Someone else closed it between the poll and the lease.
                return "skipped"

            writer = AttemptWriter(_Executor(self.conn))
            return self._close(current, observation, writer)

    def _close(self, row, observation, writer):
        """Publish the closure record, stamp retention, transition the row."""
        attempt_id = row["attempt_id"]
        state = row["lifecycle_state"]

        predecessor_key = termination.terminal_record_key(
            self.records_prefix, row["run_id"], row["logical_job_id"],
            attempt_id, termination.APPLICATION_RECORD_SEQUENCE)
        predecessor, rejected = closure_mod.read_predecessor(
            self.records_store, predecessor_key, attempt_id)

        if predecessor is not None:
            classification = (CLASS_AGREED if state ==
                              LifecycleState.APPLICATION_CLOSED.value
                              else CLASS_MATERIALIZED)
            error_category = predecessor.get("error_category")
        elif observation.never_ran:
            classification = CLASS_NEVER_STARTED
            error_category = observation.reconciler_category()
        else:
            classification = CLASS_ABRUPT_LOSS
            error_category = observation.reconciler_category() or "internal_error"

        record = closure_mod.build_closure_record(
            row, observation,
            sequence=self._next_sequence(row),
            predecessor=predecessor,
            rejected_key=(predecessor_key if predecessor is None else None),
            rejected_reason=rejected,
            classification=classification,
            error_category=error_category,
            now=self._now())
        written = closure_mod.publish_closure_record(
            self.records_store, self.records_prefix, row, record)

        self._stamp_bundle(row, observation, predecessor)

        self._transition(row, observation, writer, record, written,
                         classification, error_category)
        return "classified"

    def _next_sequence(self, row):
        """The next record sequence for this attempt.

        The application owns 0; the reconciler only ever writes higher. The
        row's recorded sequence is the floor, so a replayed correction writes
        the same sequence and dedupes on the create-once put rather than
        climbing forever.
        """
        current = row.get("terminal_record_sequence")
        if current is None:
            return termination.APPLICATION_RECORD_SEQUENCE + 1
        return max(int(current) + 1,
                   termination.APPLICATION_RECORD_SEQUENCE + 1)

    def _stamp_bundle(self, row, observation, predecessor):
        """Stamp the reconciled retention class onto the attempt's bundle."""
        key = termination.bundle_key(
            self.records_prefix, row["run_id"], row["logical_job_id"],
            row["attempt_id"])
        outcome = (predecessor or {}).get("rapid_outcome") or row.get(
            "rapid_outcome")
        retention_class = retention_mod.retention_class_for(
            outcome, observation.state)
        try:
            retention_mod.stamp_retention(
                self.s3, self.diagnostics_bucket, key, row, retention_class)
        except Exception:  # noqa: BLE001 - a missing bundle is a recorded
            # reconciliation fact, not a reason to abandon the closure.
            logger.warning("could not stamp retention on %s/%s",
                           self.diagnostics_bucket, key, exc_info=True)

    def _transition(self, row, observation, writer, record, written,
                    classification, error_category):
        attempt_id = row["attempt_id"]
        ended_at = (observation.stopped_at or row.get("ended_at")
                    or self._now())

        if classification == CLASS_NEVER_STARTED:
            writer.mark_terminal_without_start(
                attempt_id, ended_at=ended_at,
                scheduler_state=observation.state,
                error_category=error_category)
            return

        if classification == CLASS_ABRUPT_LOSS:
            writer.mark_abrupt_loss(
                attempt_id, ended_at=ended_at,
                scheduler_state=observation.state,
                error_category=error_category,
                scheduler_observed_exit=observation.exit_code,
                terminal_record_key=written.key,
                terminal_record_sequence=record.sequence)
            return

        body = record.body
        writer.mark_terminal_after_start(
            attempt_id, ended_at=ended_at,
            scheduler_observed_exit=observation.exit_code,
            scheduler_state=observation.state,
            rapid_outcome=_enum(RapidOutcome, body.get("rapid_outcome")),
            product_disposition=_enum(ProductDisposition,
                                      body.get("product_disposition")),
            application_intended_exit=body.get("application_intended_exit"),
            error_category=error_category,
            terminal_record_key=written.key,
            terminal_record_sequence=record.sequence)

    # -- the never-resolved case -----------------------------------------

    def _reconcile_unresolved(self, row):
        """A pre-created child the scheduler cannot account for.

        Bounded by the submission-anchored horizon, not the grace horizon:
        there is no scheduler-terminal observation to be graceful after.
        """
        if not beyond_submission_horizon(row.get("submitted_at"),
                                         now=self._now()):
            return "deferred"

        attempt_id = row["attempt_id"]
        with attempt_lease(self.conn, attempt_id) as held:
            if not held:
                return "skipped"
            current = reread_attempt(self.conn, attempt_id,
                                     columns=_OPEN_COLUMNS)
            if current is None or current["lifecycle_state"] not in OPEN_STATES:
                return "skipped"

            record = closure_mod.build_closure_record(
                current, None,
                sequence=self._next_sequence(current),
                predecessor=None,
                rejected_key=None,
                rejected_reason=closure_mod.REJECTED_ABSENT,
                classification=CLASS_NEVER_RESOLVED,
                error_category="scheduler_provisioning",
                now=self._now())
            closure_mod.publish_closure_record(
                self.records_store, self.records_prefix, current, record)

            writer = AttemptWriter(_Executor(self.conn))
            writer.mark_terminal_without_start(
                attempt_id, ended_at=self._now(),
                scheduler_state="FAILED",
                error_category="scheduler_provisioning")
            logger.info("attempt %s classified never-resolved at the "
                        "submission-anchored horizon", attempt_id)
            return "classified"


def _Executor(conn):  # noqa: N802 - reads as a type at the call sites
    """An AttemptWriter executor that runs on the caller's open transaction.

    `AttemptWriter` takes a *callable*, not an object with `.execute` — the
    repo's `ConnectionExecutor` is passed as `executor.execute`. This returns
    the same shape of callable, honouring the same contract (rows when the
    statement produced a result set, rowcount otherwise).

    Deliberately NOT `ConnectionExecutor`, which commits per statement: that
    would break the lease, whose entire purpose is that the closure record, the
    tag rewrite and the row transition share one transaction. Committing is the
    lease's job, at its own boundary.
    """

    def execute(statement, params=None):
        with conn.cursor() as cur:
            cur.execute(statement, params)
            if cur.description is not None:
                return cur.fetchall()
            return cur.rowcount

    return execute


def _enum(enum_class, value):
    if value is None:
        return None
    try:
        return enum_class(value)
    except ValueError:
        logger.warning("ignoring unknown %s value %r",
                       enum_class.__name__, value)
        return None


def run_forever(service, poll_seconds=POLL_SECONDS, sleep=time.sleep,
                should_continue=None):
    """Poll until told to stop. A cycle that raises is logged, not fatal.

    A reconciler that exits on error is worse than useless: the failures it
    exists to catch are exactly the conditions likely to make a cycle throw.
    """
    should_continue = should_continue or (lambda: True)
    while should_continue():
        started = time.monotonic()
        try:
            service.poll_once()
        except Exception:  # noqa: BLE001 - deliberately never fatal
            logger.exception("reconciler poll failed; continuing")
        elapsed = time.monotonic() - started
        remaining = poll_seconds - elapsed
        if remaining > 0:
            sleep(remaining)
