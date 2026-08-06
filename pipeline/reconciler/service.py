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

# Terminal states a correction may still reach (review finding #15).
#
# Supersession was unreachable: polling selected only the open states, so a
# terminal or flagged row was never reconsidered and "corrected scheduler
# facts produce sequence 2" could not happen. The design requires records to
# "supersede deterministically", which needs the row to be revisitable.
#
# The requery is BOUNDED rather than a second full scan of history: only rows
# whose scheduler facts could still change are candidates, which means rows
# closed recently enough that Batch still knows about them. Batch's own
# retention is the natural bound — beyond it there are no new facts to learn,
# so a row out there can never be superseded by anything.
SUPERSEDABLE_STATES = (
    LifecycleState.TERMINAL_AFTER_START.value,
    LifecycleState.TERMINAL_WITHOUT_START.value,
    LifecycleState.MISSING_OR_CONTRADICTORY.value,
)

#: How far back a terminal row is still revisited for supersession. Batch
#: retains job detail for 24 hours after completion; past that a requery
#: learns nothing, so the window closes.
SUPERSESSION_WINDOW = datetime.timedelta(hours=24)

#: Consecutive poll failures after which the service reports itself unhealthy
#: (review finding #24). Three is one bad minute at the 60s cadence — long
#: enough that a single transient does not flap the service, short enough that
#: a dead connection or an expired credential is caught in minutes rather than
#: never.
POLL_FAILURE_THRESHOLD = 3

# Classifications this service records, so a row says *why* it was closed.
CLASS_AGREED = "agreed"
CLASS_MATERIALIZED = "materialized_from_record"
CLASS_ABRUPT_LOSS = "abrupt_loss"
CLASS_NEVER_STARTED = "never_started"
CLASS_NEVER_RESOLVED = "never_resolved"

# Every column a closure record may need to fold in. Completeness here is not
# cosmetic (review finding #14): a reconciler-first record is built FROM THE
# ROW, so a column the reconciler does not select is a fact the record cannot
# carry — and the runtime provenance columns were missing, which meant a
# started attempt that died before writing its own record got a closure record
# with no source sha, no container digest, no configuration digest and no
# snapshot key. The row held all four.
#
# `config_digest` in particular was already being READ by `_attempt_ran`
# without ever being selected, so that evidence branch could never fire.
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

# The bounded supersession requery (review finding #15). Terminal rows whose
# scheduler facts could still change — closed inside the window, and carrying
# a scheduler job id there is anything to re-ask about.
_SUPERSEDABLE_SQL = (
    "SELECT " + ", ".join(_OPEN_COLUMNS) +
    " FROM attempts WHERE lifecycle_state = ANY(%s)"
    "   AND scheduler_job_id IS NOT NULL"
    "   AND ended_at IS NOT NULL AND ended_at >= %s"
    " ORDER BY attempt_id"
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
        #: Consecutive polls that raised. Reset by any poll that completes.
        #: The health gate reads this (review finding #24).
        self.consecutive_poll_failures = 0
        #: Closure steps that failed and deferred their attempt (#16). A
        #: persistent nonzero here means the reconciler is running but not
        #: closing anything — work-incapable while process-alive.
        self._closure_failures = 0

    # -- health ----------------------------------------------------------

    @property
    def healthy(self):
        """Is this service CAPABLE OF WORK, not merely running?

        Review finding #24: the supervised loop caught every poll exception,
        logged, slept and continued indefinitely, so a dead database
        connection or an expired rotated credential made every poll fail
        forever while systemd saw a healthy process and never restarted it.
        The reconciler exists to catch exactly the conditions likeliest to
        make a cycle throw, so "still running" is the wrong health signal.

        The bounded mechanism the observability policy asks for: consecutive
        failures, a stated threshold, and a state change when it is crossed —
        not an unbounded retry that never reports.
        """
        return self.consecutive_poll_failures < POLL_FAILURE_THRESHOLD

    def health(self):
        """The health facts, for a log line or a metric."""
        return {
            "healthy": self.healthy,
            "consecutive_poll_failures": self.consecutive_poll_failures,
            "closure_failures": self._closure_failures,
            "poll_failure_threshold": POLL_FAILURE_THRESHOLD,
        }

    # -- the open set ----------------------------------------------------

    def open_attempts(self):
        """Every attempt the reconciler might still owe something.

        Read outside any lease: this is the candidate list, and each candidate
        is rechecked under its own lock before anything is written.

        Includes the bounded supersession requery (review finding #15):
        terminal and flagged rows closed inside `SUPERSESSION_WINDOW` are
        revisited, because corrected scheduler facts must be able to produce a
        higher-sequence record. Without it, "records supersede
        deterministically" was unreachable — polling selected only the open
        states, so a terminal row was never reconsidered at all.
        """
        rows = self._select(_OPEN_SET_SQL, (list(OPEN_STATES),))
        rows.extend(self._supersedable())
        return rows

    def _supersedable(self):
        """Terminal rows whose scheduler facts could still change."""
        horizon = self._now() - SUPERSESSION_WINDOW
        return self._select(_SUPERSEDABLE_SQL,
                            (list(SUPERSEDABLE_STATES), horizon))

    def _select(self, sql, params):
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
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
                    # take the cycle down; the next poll retries it. The
                    # rollback is not optional: without it a failed statement
                    # leaves the transaction aborted and every subsequent
                    # attempt fails with InFailedSqlTransaction, turning one
                    # bad row into a dead cycle.
                    self._safe_rollback()
                    logger.exception("reconciling attempt %s failed",
                                     row.get("attempt_id"))
                    summary["errors"] += 1
                else:
                    summary[outcome] = summary.get(outcome, 0) + 1

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

        logger.info("poll: %s", summary)
        return summary

    def _safe_rollback(self):
        """Clear an aborted transaction. A rollback that itself fails (the
        connection is gone) must not mask the error being handled."""
        try:
            self.conn.rollback()
        except Exception:  # noqa: BLE001
            logger.debug("rollback after a failed attempt also failed",
                         exc_info=True)

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
        self._record_observation(attempt_id, row["lifecycle_state"],
                                 observation)

        if not observation.is_terminal:
            return "deferred"

        if not beyond_grace_horizon(observation.stopped_at, now=self._now()):
            logger.debug("attempt %s is scheduler-terminal but inside the "
                         "grace horizon; leaving it open", attempt_id)
            return "deferred"

        return self._classify(row, observation)

    def _record_observation(self, attempt_id, lifecycle_state, observation):
        """Record what the scheduler saw, without violating the row's state.

        A `submitted` row must have `scheduler_state IS NULL` — the DDL says
        so, and it is right: a row that still claims nothing has started must
        not carry a scheduler verdict beside that claim. The scheduler
        *timestamps* are permitted there and are worth having early (they are
        the queue interval), so they are written and the state is withheld
        until the same pass that transitions the row out of `submitted`.
        """
        writer = AttemptWriter(_Executor(self.conn))
        state = (None if lifecycle_state == LifecycleState.SUBMITTED.value
                 else observation.state)
        try:
            writer.record_scheduler_observation(
                attempt_id,
                scheduler_state=state,
                created_at=observation.created_at,
                started_at=observation.started_at,
                stopped_at=observation.stopped_at,
                attempt_index=observation.attempt_index)
            self.conn.commit()
        except Exception:
            # A failed statement poisons the whole transaction in PostgreSQL:
            # every later statement raises InFailedSqlTransaction until the
            # block ends. Rolling back here is what keeps one bad row from
            # taking down the rest of the cycle.
            self.conn.rollback()
            raise

    # -- classification, under the lease ---------------------------------

    def _classify(self, row, observation):
        attempt_id = row["attempt_id"]
        with attempt_lease(self.conn, attempt_id) as held:
            if not held:
                return "skipped"

            current = reread_attempt(self.conn, attempt_id,
                                     columns=_OPEN_COLUMNS)
            if current is None:
                return "skipped"

            state = current["lifecycle_state"]
            if state not in OPEN_STATES:
                # A terminal row reached here through the supersession requery
                # (review finding #15). It is reconsidered only if the
                # scheduler now says something the row does not already
                # record; otherwise this is the ordinary case of re-observing
                # an attempt that is genuinely finished, and re-publishing an
                # identical record every minute would be noise.
                if state not in SUPERSEDABLE_STATES:
                    return "skipped"
                if not self._facts_changed(current, observation):
                    return "skipped"
                logger.info(
                    "attempt %s is %s but the scheduler's facts have changed; "
                    "superseding", attempt_id, state)

            writer = AttemptWriter(_Executor(self.conn))
            return self._close(current, observation, writer)

    @staticmethod
    def _facts_changed(row, observation):
        """Does the scheduler now say something the row does not record?

        The supersession trigger (review finding #15). Deliberately narrow:
        only the scheduler-observed facts a correction could actually change,
        compared against what the row already stores. Anything wider would
        re-close every terminal attempt on every poll.
        """
        if observation is None:
            return False
        comparisons = (
            (row.get("scheduler_state"), observation.state),
            (row.get("scheduler_observed_exit"), observation.exit_code),
        )
        for recorded, observed in comparisons:
            if observed is not None and recorded != observed:
                return True
        return False

    def _close(self, row, observation, writer):
        """Publish the closure record, stamp retention, transition the row."""
        attempt_id = row["attempt_id"]
        state = row["lifecycle_state"]

        predecessor_key = termination.terminal_record_key(
            self.records_prefix, row["run_id"], row["logical_job_id"],
            attempt_id, termination.APPLICATION_RECORD_SEQUENCE)
        predecessor, rejected = closure_mod.read_predecessor(
            self.records_store, predecessor_key, attempt_id)

        # Did this attempt ever start? Both stores get a say, and the ROW is
        # decisive where they disagree. The scheduler's per-job view can show
        # a start time that belongs to a different attempt of the same job,
        # and a row carrying application facts — an outcome, a disposition, a
        # config digest — has demonstrably run whatever the scheduler says.
        # Getting this wrong writes a state the DDL forbids:
        # terminal_without_start requires started_at IS NULL *and* no
        # application-authored fields at all. (Found live, 2026-08-06: rows
        # with rapid_outcome=success were being classified never-started, and
        # attempts whose own started_at was NULL were being classified as
        # abrupt losses.)
        ran = self._attempt_ran(row, predecessor, observation)

        if predecessor is not None:
            classification = (CLASS_AGREED if state ==
                              LifecycleState.APPLICATION_CLOSED.value
                              else CLASS_MATERIALIZED)
            error_category = predecessor.get("error_category")
        elif not ran:
            classification = CLASS_NEVER_STARTED
            error_category = observation.reconciler_category() or \
                "scheduler_provisioning"
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

        # THE CLOSURE STEPS NO LONGER FAIL OPEN (review finding #16).
        #
        # Publishing the record and stamping the bundle's retention are both
        # part of closing an attempt, and both used to be able to fail while
        # the row was terminalized anyway — the tagging failure was caught,
        # logged, and the attempt marked terminal, never to be revisited,
        # because terminal rows are outside the open set. A bundle whose
        # retention was never stamped then expires under the wrong lifecycle
        # rule with nothing left to notice.
        #
        # A failure now DEFERS the attempt: the row stays open, the next poll
        # retries it, and the failure is counted so service health can see a
        # persistent one. Every step here is idempotent by identity, so a
        # retry re-derives the same key and either creates or validates.
        try:
            written = closure_mod.publish_closure_record(
                self.records_store, self.records_prefix, row, record)
        except Exception:  # noqa: BLE001 - deferred, not swallowed
            self._closure_failures += 1
            logger.exception(
                "could not publish the closure record for attempt %s; the row "
                "stays open and the next poll retries it", attempt_id)
            return "deferred"

        try:
            self._stamp_bundle(row, observation, predecessor)
        except Exception:  # noqa: BLE001 - deferred, not swallowed
            self._closure_failures += 1
            logger.exception(
                "could not stamp retention for attempt %s; the closure record "
                "is published (idempotently re-derivable) but the row stays "
                "open so the tag is retried rather than lost", attempt_id)
            return "deferred"

        self._transition(row, observation, writer, record, written,
                         classification, error_category)
        return "classified"

    @staticmethod
    def _attempt_ran(row, predecessor, observation):
        """Did the application ever begin executing this attempt?

        Evidence, strongest first:

        1. The row's own `started_at`. The runtime writes it in the same
           compare-and-set that marks the attempt started, so its presence is
           first-hand and its absence means no started-CAS ever landed.
        2. Any application-authored fact on the row or in a predecessor
           record — an outcome, a disposition, a resolved config digest. None
           of those can exist unless the application ran.
        3. Only then the scheduler's start time, which is the weakest signal
           because a job-scoped view can report a start belonging to a
           different attempt of the same job.
        """
        if row.get("started_at") is not None:
            return True
        for field in ("rapid_outcome", "product_disposition", "config_digest",
                      "application_intended_exit"):
            if row.get(field) is not None:
                return True
        # An application-observed attempt index means the APPLICATION claimed
        # this row — it read its own AWS_BATCH_JOB_ATTEMPT and wrote it, which
        # it can only do from inside a running container. The DDL agrees:
        # terminal_without_start requires application_attempt_index IS NULL,
        # precisely because a claimed row is not one that never started.
        if row.get("application_attempt_index") is not None:
            return True
        if predecessor:
            return True
        if row.get("lifecycle_state") in (
                LifecycleState.STARTED.value,
                LifecycleState.APPLICATION_CLOSED.value):
            return True

        # Nothing on the row says it ran, and the row is still `submitted` —
        # meaning no started-CAS ever landed. The scheduler's start time is
        # NOT enough to overrule that: on a job-scoped observation it may
        # belong to a different attempt, and `terminal_after_start` would
        # then be written for an attempt whose started_at is NULL, which the
        # DDL rejects. Only a per-attempt observation (one carrying its own
        # index, so it is scoped to this attempt) is trusted here.
        if observation is None or observation.started_at is None:
            return False
        return observation.attempt_index is not None

    @staticmethod
    def _is_contradictory(row, classification):
        """Application facts with no start time — neither terminal state fits.

        `terminal_after_start` requires `started_at IS NOT NULL`;
        `terminal_without_start` requires the application fields be NULL. A
        row with an outcome but no start satisfies neither, and the disagreement
        is real rather than a classification mistake.
        """
        if row.get("started_at") is not None:
            return False
        # From here the row has no start time, so `terminal_after_start` is
        # unavailable whatever else is true — its constraint requires one.
        # Any classification that would land there is a contradiction, as is
        # any row carrying application facts it could only have authored by
        # running.
        if classification in (CLASS_AGREED, CLASS_MATERIALIZED,
                              CLASS_ABRUPT_LOSS):
            return True
        return any(row.get(field) is not None
                   for field in ("rapid_outcome", "product_disposition",
                                 "application_intended_exit", "config_digest"))

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
        """Stamp the reconciled retention class onto the attempt's bundle.

        RAISES on failure (review finding #16). This used to catch everything
        and continue to the row transition, so a tagging failure terminalized
        the attempt with an unstamped bundle and no way back — terminal rows
        are outside the open set, so nothing ever revisited it. The caller now
        defers the attempt instead, and the next poll retries the stamp.

        An ABSENT bundle is still not a failure: an attempt that died before
        uploading one has nothing to tag, and that is a recorded fact rather
        than an error. `stamp_retention` distinguishes the two.
        """
        key = termination.bundle_key(
            self.records_prefix, row["run_id"], row["logical_job_id"],
            row["attempt_id"])
        outcome = (predecessor or {}).get("rapid_outcome") or row.get(
            "rapid_outcome")
        retention_class = retention_mod.retention_class_for(
            outcome, observation.state if observation is not None else None)
        return retention_mod.stamp_retention(
            self.s3, self.diagnostics_bucket, key, row, retention_class)

    def _transition(self, row, observation, writer, record, written,
                    classification, error_category):
        attempt_id = row["attempt_id"]
        ended_at = (observation.stopped_at or row.get("ended_at")
                    or self._now())

        # A row can be internally inconsistent: application facts present but
        # no started_at, so neither terminal state's constraint can hold —
        # terminal_after_start requires started_at, terminal_without_start
        # forbids the application facts. Forcing it into either would mean
        # deleting evidence to fit a state. The adopted state for stores that
        # disagree is missing_or_contradictory, and that is what it gets:
        # flagged for a human, with the closure record already published.
        if self._is_contradictory(row, classification):
            writer.mark_missing_or_contradictory(
                attempt_id,
                reconciliation_class="contradictory",
                reconciliation_sources=["postgres", "batch"],
                detected_at=self._now())
            logger.warning(
                "attempt %s carries application facts with no start time; "
                "flagged contradictory rather than forced into a terminal "
                "state", attempt_id)
            return

        # The sequence the record actually LANDED at, which differs from the
        # one it was built for whenever a supersession climb happened (review
        # finding #15 — the row used to store the pre-climb sequence, so it
        # cited a key holding a record that declares a different one).
        landed_sequence = written.sequence

        if classification == CLASS_NEVER_STARTED:
            # The closure record IS cited now (review finding #14): 013 left
            # the reconciler's record unreferenced from the row it accounts
            # for, so it was findable only by reconstructing its key.
            writer.mark_terminal_without_start(
                attempt_id, ended_at=ended_at,
                scheduler_state=observation.state,
                error_category=error_category,
                closure_record_key=written.key,
                closure_record_sequence=landed_sequence)
            return

        if classification == CLASS_ABRUPT_LOSS:
            writer.mark_abrupt_loss(
                attempt_id, ended_at=ended_at,
                scheduler_state=observation.state,
                error_category=error_category,
                scheduler_observed_exit=observation.exit_code,
                terminal_record_key=written.key,
                terminal_record_sequence=landed_sequence)
            return

        body = written.record.body

        # MATERIALIZATION IS RECORDED (review finding #14). Where the row is
        # still `started` and the reconciler is projecting the application's
        # own facts onto it from a validated record, that projection is "the
        # one sanctioned projection of application facts by another writer"
        # and the row must say so. Nothing set the flag before, so a
        # reconciler-materialized row was indistinguishable from one the
        # application closed itself.
        if classification == CLASS_MATERIALIZED:
            writer.mark_application_closed(
                attempt_id, ended_at=ended_at,
                application_intended_exit=body.get(
                    "application_intended_exit") or 0,
                rapid_outcome=_enum(RapidOutcome, body.get("rapid_outcome")),
                product_disposition=_enum(
                    ProductDisposition, body.get("product_disposition")),
                terminal_record_key=body.get("terminal_record_key")
                or row.get("terminal_record_key"),
                terminal_record_sequence=body.get(
                    "terminal_record_sequence") or 0,
                terminal_record_checksum=body.get("terminal_record_checksum"),
                error_category=error_category,
                reconciler_materialized=True)

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
            terminal_record_sequence=landed_sequence)

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
            try:
                written = closure_mod.publish_closure_record(
                    self.records_store, self.records_prefix, current, record)
            except Exception:  # noqa: BLE001 - deferred, not swallowed (#16)
                self._closure_failures += 1
                logger.exception(
                    "could not publish the closure record for unresolved "
                    "attempt %s; it stays open and the next poll retries it",
                    attempt_id)
                return "deferred"

            writer = AttemptWriter(_Executor(self.conn))

            # An attempt the scheduler cannot account for is normally one that
            # never ran — but not always. A row can carry a full application
            # account (it started, it closed itself) while its scheduler id
            # resolves to nothing: the id was wrong, or the job aged out of
            # Batch's retention. `terminal_without_start` forbids exactly
            # those application fields, so writing it would mean asserting the
            # attempt never ran while the row proves it did. That is a genuine
            # disagreement between the stores, and it gets the state the
            # design has for disagreement. (Found live 2026-08-06 against
            # W2-era rows whose synthetic job ids Batch never knew.)
            if self._attempt_ran(current, None, None):
                writer.mark_missing_or_contradictory(
                    attempt_id,
                    reconciliation_class="missing",
                    reconciliation_sources=["postgres", "batch"],
                    detected_at=self._now())
                logger.warning(
                    "attempt %s has an application account but its scheduler "
                    "id %s resolves to nothing; flagged contradictory",
                    attempt_id, current.get("scheduler_job_id"))
                return "classified"

            writer.mark_terminal_without_start(
                attempt_id, ended_at=self._now(),
                scheduler_state="FAILED",
                error_category="scheduler_provisioning",
                closure_record_key=written.key,
                closure_record_sequence=written.sequence)
            logger.info("attempt %s classified never-resolved at the "
                        "submission-anchored horizon (closure %s)",
                        attempt_id, written.key)
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


class ReconcilerUnhealthy(RuntimeError):
    """Consecutive polls failed past the threshold: the service cannot work.

    Raised out of `run_forever` so the process EXITS and its supervisor
    restarts it (review finding #24). A reconciler that stays up while every
    poll fails is the failure mode this replaces: systemd saw a running
    process, the service saw a dead database connection or an expired rotated
    credential, and nothing reconciled anything for as long as that lasted.

    A restart is the right response because the conditions that produce it —
    a stale connection, a rotated credential, a broken client — are exactly
    the ones a fresh process re-establishes. Tolerating a bounded number of
    transients first is what keeps that from flapping.
    """


def run_forever(service, poll_seconds=POLL_SECONDS, sleep=time.sleep,
                should_continue=None,
                failure_threshold=POLL_FAILURE_THRESHOLD):
    """Poll until told to stop, or until the service cannot do its work.

    A cycle that raises is logged and retried — the failures the reconciler
    exists to catch are exactly the conditions likely to make one throw, so a
    single exception must not take the service down.

    But CONSECUTIVE failures are different (review finding #24). This loop
    used to catch every poll exception forever, so "the process is alive" and
    "the service is working" came apart with nothing to notice. Past
    `failure_threshold` consecutive failures the service is not doing its job
    and says so by exiting, which is the bounded mechanism the observability
    policy asks for: a stated threshold, a state change when it is crossed,
    and a supervisor that acts on it.
    """
    should_continue = should_continue or (lambda: True)
    while should_continue():
        started = time.monotonic()
        try:
            service.poll_once()
        except Exception:  # noqa: BLE001 - retried, but counted
            service.consecutive_poll_failures += 1
            logger.exception(
                "reconciler poll failed (%d consecutive, threshold %d)",
                service.consecutive_poll_failures, failure_threshold)
            if service.consecutive_poll_failures >= failure_threshold:
                raise ReconcilerUnhealthy(
                    f"{service.consecutive_poll_failures} consecutive poll "
                    f"failures (threshold {failure_threshold}); the "
                    f"reconciler is running but reconciling nothing. Exiting "
                    f"so the supervisor restarts it — a stale connection or a "
                    f"rotated credential is re-established by a fresh "
                    f"process, and staying up would mean no attempt is "
                    f"classified for as long as this lasts.")
        else:
            if service.consecutive_poll_failures:
                logger.info("reconciler recovered after %d failed poll(s)",
                            service.consecutive_poll_failures)
            service.consecutive_poll_failures = 0

        elapsed = time.monotonic() - started
        remaining = poll_seconds - elapsed
        if remaining > 0:
            sleep(remaining)
