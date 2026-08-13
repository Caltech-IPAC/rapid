"""The reconciler service: the polling loop and one attempt's reconciliation.

Structure, top down:

  run_forever            the supervised loop — poll, sleep, repeat; exits only
                         when it cannot do its work, for the supervisor to act
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
import socket
import time

from observability.attempts import (
    RECONCILER_ERROR_CATEGORIES, AttemptWriter, LifecycleState,
    ProductDisposition, RapidOutcome)
from pipeline.intent.retry_policy import (
    CLOSE_COMPLETE, PARK_BLOCKED, RETRY_READY, blocked_reason_for,
    disposition_for_terminal_attempt, policy_version)
from pipeline.intent.writer import (
    BLOCKED, COMPLETE, FAILED, READY, SUBMITTED, WRITER_RECONCILER,
    WorkUnitNotFound, WorkUnitWriter)
from pipeline.runtime import termination
from submission import protocol as submission_protocol

from . import closure as closure_mod
from . import reconstruction
from . import retention as retention_mod
from .horizons import beyond_grace_horizon, beyond_submission_horizon
from .lease import attempt_lease, reread_attempt
from .scheduler import batch_describer, describe_in_batches, observations_for_job

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

#: Consecutive polls in which EVERY attempt that reached a closure step failed
#: it, after which the service reports itself unhealthy (review finding #24).
#:
#: The poll-failure counter above only sees exceptions that escape the whole
#: cycle, and `poll_once` catches every per-attempt exception by design — one
#: bad row must not take the cycle down. The consequence was a service that
#: could fail to close every single attempt, forever, while reporting healthy:
#: the records bucket denied writes, or every materialization transition was
#: rejected, and each poll dutifully logged errors and returned normally.
#:
#: "Running" was never the health question; "capable of work" is. A poll that
#: attempted closures and completed none of them is a poll that did no work,
#: and enough of those in a row is a unit that needs restarting. The bound is
#: stated rather than open-ended so the unit flips within a knowable time —
#: five minutes at the 60s cadence.
CLOSURE_FAILURE_POLL_THRESHOLD = 5

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
    # The intent-layer FK (migration 036, integration review ruling 13).
    # NULL on every pre-intent-layer row and on any attempt whose
    # definition-FK guard held it back at submission time (see
    # `pipeline.seams._attach_work_unit`) — `_close`'s work-unit-closure
    # step (below) reads this and skips silently when it is absent, exactly
    # the "only FK-carrying attempts drive transitions" rule the task brief
    # states.
    "work_unit_id",
    # The submission-protocol FK (DRAFT migration 044, `protocol.
    # attach_attempts`). NULL on every pre-044 row and on any attempt a
    # submission pass could not open a durable record for (submissions fails
    # OPEN — see `pipeline.seams._open_submission`). `_reconcile_unresolved`
    # reads this to ask the submission record, not the clock, what happened
    # to an ambiguous child (rule 7 package S).
    "submission_id",
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


class BundleReconstructionFailed(RuntimeError):
    """An attempt ran, has no bundle, and one could not be written either.

    Raised so the caller's existing handler DEFERS the attempt (round-3
    finding #5). The distinction this draws is between two failures that look
    alike and are not: a CloudWatch stream that cannot be read is permanent —
    the stream expires — and the bundle is written anyway with the gap
    recorded inside it, because an attempt deferred past that horizon could
    never be closed by anyone. A store that will not accept the write is a
    condition a later poll may find resolved, and that is this.
    """


class ReconcilerService:
    """One reconciler. Owns no state between polls beyond its connections.

    Deliberately not a daemon class with an internal thread: `poll_once` is a
    pure-ish function of (database, scheduler, stores) so it can be tested with
    all three stubbed, and `run_forever` is the thin supervised wrapper.
    """

    def __init__(self, conn, batch_client, records_store, diagnostics_store,
                 s3_client, records_prefix, diagnostics_bucket,
                 logs_client=None, log_group=None, log_groups=None,
                 now=None):
        self.conn = conn
        self.batch = batch_client
        self.records_store = records_store
        self.diagnostics_store = diagnostics_store
        self.s3 = s3_client
        self.records_prefix = records_prefix
        self.diagnostics_bucket = diagnostics_bucket
        #: CloudWatch Logs, read only when reconstructing a record with no
        #: predecessor (#16). Optional: a missing log is a thinner
        #: reconstruction, not a failed closure, so the service works without
        #: one and says so in `reconstructed_from`.
        self.logs = logs_client
        self.log_group = log_group
        #: Job-definition NAME -> log group, for per-attempt derivation.
        #:
        #: A single service-wide group cannot be right: the two class-fixed job
        #: definitions log to two different groups
        #: (`/rapid/batch/rapid-queue-{prompt,bulk}`), so whichever one a
        #: parameter named, attempts of the other class would be read from a
        #: group that does not hold their streams. The absent
        #: `logs/job-log-group` fell back to `/aws/batch/job`, which holds no
        #: RAPID logs at all and which `rapid-orchestrator-role` cannot read —
        #: so every reconstruction that needed a log got nothing, silently.
        #:
        #: Derived from `binding_job_definition_arn`, which the row already
        #: carries: the definition is what owns the `awslogs-group` option, so
        #: this reads the fact at its source rather than inferring a class.
        #: Names live in the parameter tree, as queue and definition names do.
        self.log_groups = dict(log_groups or {})
        self._now = now or (
            lambda: datetime.datetime.now(datetime.timezone.utc))
        #: Consecutive polls that raised. Reset by any poll that completes.
        #: The health gate reads this (review finding #24).
        self.consecutive_poll_failures = 0
        #: Closure steps that failed and deferred their attempt (#16). A
        #: persistent nonzero here means the reconciler is running but not
        #: closing anything — work-incapable while process-alive.
        self._closure_failures = 0
        #: Attempts whose observed job definition disagreed with their
        #: recorded execution binding (#11).
        self._binding_drift = 0
        #: Consecutive polls that attempted at least one closure and completed
        #: none of them (#24). Reset by any poll that closes something, or that
        #: had nothing to close.
        self.consecutive_unproductive_polls = 0
        #: Attempts that RAN but had no diagnostics bundle to stamp (#16).
        #: Distinct from the never-started case, where there is genuinely
        #: nothing to retain.
        self._missing_bundles = 0
        #: Of those, how many the reconciler built from the CloudWatch stream
        #: (round-3 #5). Reported separately from `missing_bundles` because
        #: they answer different questions: the first is how often attempts
        #: die without leaving their diagnostics, which is a fleet problem
        #: worth watching, and this is how many of those were salvaged.
        self._reconstructed_bundles = 0

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

        TWO counters, because there are two ways to be work-incapable, and the
        original only saw one (review finding #24 again, round 2). A cycle that
        raises is caught above; a cycle in which every closure fails returns
        normally, so the poll counter stays at zero forever. Persistent
        per-row failure is exactly as fatal as a failing poll — the attempts
        never close, registration never sees them — so it flips the unit too,
        within `CLOSURE_FAILURE_POLL_THRESHOLD` polls.

        What counts is ACTIONABLE-UNCLOSED work (ratified disposition,
        2026-08-06): attempts the reconciler owes something to right now and
        failed to deliver. An attempt still running, or inside either horizon,
        is owed nothing yet and never counts against health — see `poll_once`.
        """
        return (self.consecutive_poll_failures < POLL_FAILURE_THRESHOLD
                and self.consecutive_unproductive_polls
                < CLOSURE_FAILURE_POLL_THRESHOLD)

    def health(self):
        """The health facts, for a log line or a metric."""
        return {
            "healthy": self.healthy,
            "consecutive_poll_failures": self.consecutive_poll_failures,
            "closure_failures": self._closure_failures,
            "consecutive_unproductive_polls":
                self.consecutive_unproductive_polls,
            "binding_drift": self._binding_drift,
            "missing_bundles": self._missing_bundles,
            "reconstructed_bundles": self._reconstructed_bundles,
            "poll_failure_threshold": POLL_FAILURE_THRESHOLD,
            "unproductive_poll_threshold": CLOSURE_FAILURE_POLL_THRESHOLD,
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

    def write_heartbeat(self, summary):
        """Record that this poll RAN, whether or not it found work.

        THE LIVENESS FACT, replacing an inference. `derived.region4_watcher_
        liveness` used to read `max(attempts.reconciliation_detected_at)` —
        a timestamp written only when a pass finds something to classify. A
        reconciler polling perfectly against a quiet pipeline wrote nothing,
        so its liveness decayed to 'stale', and because that view GATES
        region 4, every target-zero panel in the region rendered untrusted.
        Health and quiet were indistinguishable, and the healthy case was the
        one that looked broken.

        One row per poll, including the nothing-to-do poll — which is the
        whole point, and is why this is called before the early return above
        as well as at the end of a full cycle.

        Never fatal. A heartbeat that cannot be written is a monitoring
        problem; failing the reconciliation cycle over it would turn a
        cosmetic outage into a real one.
        """
        try:
            with self.conn.cursor() as cursor:
                cursor.execute(
                    "insert into reconciler_runs "
                    "(rows_classified, poll_seconds, reconciler_host) "
                    "values (%s, %s, %s);",
                    (int(summary.get("classified", 0)),
                     getattr(self, "poll_seconds", None),
                     socket.gethostname()))
            self.conn.commit()
        except Exception:  # noqa: BLE001 - liveness must not break the loop
            self._safe_rollback()
            logger.exception("could not write the reconciler heartbeat; the "
                             "cycle itself is unaffected")

    def poll_once(self):
        """One reconciliation cycle. Returns a summary dict for the log."""
        rows = self.open_attempts()
        summary = {"open": len(rows), "observed": 0, "classified": 0,
                   "skipped": 0, "deferred": 0, "waiting": 0, "errors": 0,
                   "discovered": 0, "submission_found": 0, "submission_lost": 0,
                   "submission_unknown": 0}
        self._resolve_submissions(summary)
        if not rows:
            # Nothing to do is not a failure to work (#24) — and it is still a
            # poll, so it still heartbeats. This is exactly the case the old
            # liveness inference could not see.
            self.consecutive_unproductive_polls = 0
            self.write_heartbeat(summary)
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

        # EVERY SCHEDULER ATTEMPT GETS A ROW (review finding #4).
        #
        # The loop below iterates rows that are ALREADY in PostgreSQL, so an
        # attempt the scheduler performed but that no row represents was
        # invisible: it received no row, no binding, no category, no closure
        # record and no retention account. That is the ordinary retry shape —
        # attempt 1 fails during provisioning and attempt 2 runs — and the
        # design requires one row per attempt, not one per job.
        #
        # Migration 017 states this wiring as part of this round in so many
        # words ("#4 wires the reconciler through the resolver"). Acquisition
        # goes through `resolve_attempt` and nowhere else: it is the only
        # sanctioned path, and its advisory lock plus partial unique indexes are
        # what make a reconciler-discovered retry and a late-starting runtime
        # resolve to the same row instead of racing to two.
        try:
            discovered, resolve_errors = self._resolve_discovered(
                by_job, observations)
        except Exception:  # noqa: BLE001 - discovery must not kill the cycle
            self._safe_rollback()
            logger.exception("resolving scheduler-discovered attempts failed")
            summary["errors"] += 1
            discovered = 0
        else:
            # Per-attempt resolution failures count too (round-3 finding #6):
            # this used to see only a total collapse of the whole call, so a
            # resolver failing every single attempt individually was invisible.
            summary["errors"] += resolve_errors
        summary["discovered"] = discovered
        if discovered:
            # Re-read so the new rows are reconciled in this same cycle rather
            # than waiting a poll: they are already terminal at the scheduler.
            rows = self.open_attempts()
            by_job = {}
            unresolved = []
            for row in rows:
                job_id = row.get("scheduler_job_id")
                if job_id:
                    by_job.setdefault(job_id, []).append(row)
                else:
                    unresolved.append(row)
            summary["open"] = len(rows)

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

        # Did this cycle actually DO anything (review finding #24)? A poll that
        # tried to close attempts and completed none of them is work-incapable,
        # however calmly it returned. A single classified attempt proves the
        # service can still work and clears the counter.
        #
        # Health counts ACTIONABLE-UNCLOSED work only (ratified disposition,
        # 2026-08-06). An attempt still running, one inside its grace horizon,
        # and one inside its submission-anchored horizon are all `waiting`:
        # the reconciler owes them nothing yet, so they are not evidence that
        # it cannot work. Only `deferred` — a closure step that TRIED and
        # failed — and `errors` are actionable. Counting waiting attempts here
        # is what made a normal ramp step trip a check meant for an unhealthy
        # service: W9 ran at NRestarts=15 because every poll during a step's
        # first ten minutes attempted nothing but horizon-deferrals and was
        # scored as having failed to close them.
        attempted = summary["classified"] + summary["deferred"] \
            + summary["errors"]
        if attempted and not summary["classified"]:
            self.consecutive_unproductive_polls += 1
            logger.warning(
                "poll closed nothing while attempting %s actionable (deferred="
                "%s errors=%s, waiting=%s not counted); %s consecutive "
                "unproductive polls, threshold %s",
                attempted, summary["deferred"], summary["errors"],
                summary["waiting"], self.consecutive_unproductive_polls,
                CLOSURE_FAILURE_POLL_THRESHOLD)
        else:
            self.consecutive_unproductive_polls = 0

        logger.info("poll: %s", summary)
        self.write_heartbeat(summary)
        return summary

    def _resolve_submissions(self, summary):
        """Re-query Batch for every open submission (rule 7, package S).

        Run once per cycle, BEFORE `poll_once`'s `if not rows:` early return
        (S1's binding constraint): open submissions can exist when zero
        attempts are in an open lifecycle state, and a pass placed after that
        return would silently never run in exactly that case.

        Fails OPEN, unlike `_resolve_discovered`'s admission-style caller:
        `submission.protocol` degrades to a no-op on a pre-044 database
        (`is_available`), and a raise anywhere in this method — the
        availability probe included — is caught here so the pass itself
        cannot kill the cycle. `resolve_open` already isolates each row's own
        failure; this guards the pass as a whole, the same posture
        `_resolve_discovered`'s caller in `poll_once` takes for discovery.
        """
        try:
            if not submission_protocol.is_available(_Executor(self.conn)):
                return
            counts = submission_protocol.resolve_open(
                _Executor(self.conn), batch_describer(self.batch),
                now=self._now())
        except Exception:  # noqa: BLE001 - the pass must not kill the cycle
            self._safe_rollback()
            logger.exception("resolving open submissions failed")
            summary["errors"] += 1
            return

        # ONE COMMIT PER PASS THAT RESOLVED SOMETHING — not per row. The
        # distinction is worth stating because the weaker property is the one
        # that holds: `resolve_open` returns before anything is committed, so
        # this pass's UPDATEs share a transaction that stays open across every
        # Batch re-query the pass makes. That is not rule 7's SubmitJob
        # prohibition (nothing here submits, and re-query is idempotent), but
        # it does mean a crash mid-pass loses the whole pass's resolutions
        # rather than keeping the rows already resolved.
        #
        # ACCEPTED, NOT OVERLOOKED: losing them is harmless. Every lost row
        # simply stays `calling`/`unknown` and is re-queried next pass — the
        # protocol's own posture for an interrupted resolution — whereas
        # committing per row would need `resolve_open` to own the boundary,
        # which it does not. Revisit if the pass ever grows long enough that
        # holding one transaction across its Batch calls starts to matter.
        resolved = counts[submission_protocol.FOUND] \
            + counts[submission_protocol.LOST] \
            + counts[submission_protocol.UNKNOWN]
        if resolved:
            self.conn.commit()
        else:
            # Nothing to commit, but `resolve_open` ran a read-only SELECT
            # (`open_submissions`) through this connection; leave no
            # transaction open behind it.
            self.conn.rollback()

        summary["submission_found"] += counts[submission_protocol.FOUND]
        summary["submission_lost"] += counts[submission_protocol.LOST]
        summary["submission_unknown"] += counts[submission_protocol.UNKNOWN]
        summary["errors"] += counts["errors"]

    def _log_group_for(self, row):
        """Which CloudWatch group holds this attempt's stream.

        The job definition owns the `awslogs-group` option, and the row records
        the definition it was submitted under, so the derivation reads a fact
        rather than guessing a class. `binding_job_definition_arn` is a full
        ARN ending `job-definition/<name>:<revision>`; the revision is dropped
        because every revision of a definition logs to the same group.

        Falls back to the configured service-wide group when the row carries no
        binding (rows created before the binding columns landed) or names a
        definition the tree does not map. That is a thinner reconstruction, not
        a failed closure — the same posture as a missing logs client.
        """
        arn = row.get("binding_job_definition_arn")
        if arn:
            name = str(arn).rsplit("/", 1)[-1].split(":", 1)[0]
            group = self.log_groups.get(name)
            if group:
                return group
            if self.log_groups:
                logger.warning(
                    "no log group is mapped for job definition %s; falling "
                    "back to %s, which may not hold this attempt's stream",
                    name, self.log_group)
        return self.log_group

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

    def _resolve_discovered(self, by_job, observations):
        """Give every scheduler attempt a row (review finding #4).

        For each open job, compare the attempt indexes the scheduler reports
        against the indexes the rows already carry, and resolve a row for each
        one that is missing. Returns how many were newly resolved.

        The identity comes from an EXISTING row of the same job — same run,
        same logical job, same exposure/sca/sky_tile — because a retry is
        another attempt at that same logical job, and the reconciler has no
        other way to learn those fields. A job with no row at all is not
        reachable here: the open set is what named the job in the first place.

        Resolution is per attempt and failures are per attempt: one index that
        cannot be resolved must not stop the others from getting their rows.

        Returns `(resolved, errors)`. The error count is not decoration
        (round-3 finding #6): every other per-row loop in this class reports its
        failures into `summary["errors"]`, and this one alone swallowed them
        internally. Because unproductive-poll health is computed from
        `classified + deferred + errors`, resolution failures that never reached
        the summary could not make a poll look unproductive — so a reconciler
        failing to resolve every discovered attempt, poll after poll, still
        counted as perfectly healthy.
        """
        resolved = 0
        errors = 0
        for job_id, rows in by_job.items():
            attempt_observations = observations.get(job_id) or []
            if len(attempt_observations) < 2:
                # One observation is the no-retry case: whatever row exists
                # already accounts for it, and a job whose history Batch does
                # not break out gives nothing to reconcile against.
                continue

            known = {row.get("application_attempt_index") for row in rows}
            known.update(row.get("scheduler_attempt_index") for row in rows)
            known.discard(None)

            template = rows[0]
            for observation in attempt_observations:
                index = observation.attempt_index
                if index is None or index in known:
                    continue
                try:
                    attempt_id = self._resolve_one(template, job_id, index)
                except Exception:  # noqa: BLE001 - per attempt, not per job
                    self._safe_rollback()
                    errors += 1
                    logger.exception(
                        "could not resolve a row for scheduler attempt %s of "
                        "job %s; the next poll retries it", index, job_id)
                    continue
                known.add(index)
                resolved += 1
                logger.info(
                    "resolved attempt row %s for scheduler-discovered attempt "
                    "%s of job %s", attempt_id, index, job_id)
        return resolved, errors

    def _resolve_one(self, template, job_id, scheduler_attempt_index):
        """Claim-or-create one scheduler-discovered attempt's row."""
        from observability.attempts import AttemptIdentity

        identity = AttemptIdentity(
            run_id=template["run_id"],
            logical_job_id=template["logical_job_id"],
            exposure_id=template.get("exposure_id"),
            sca=template.get("sca"),
            sky_tile=template.get("sky_tile"))
        writer = AttemptWriter(_Executor(self.conn))
        attempt_id = writer.resolve_attempt(
            identity,
            created_at=template.get("submitted_at") or self._now(),
            submitted_at=template.get("submitted_at") or self._now(),
            scheduler_job_id=job_id,
            scheduler_attempt_index=scheduler_attempt_index)
        self.conn.commit()
        return attempt_id

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

        # SEVERAL OBSERVATIONS, AN UNINDEXED ROW (review finding #4).
        #
        # Returning None here sent the row down the unresolved path, where the
        # submission-anchored horizon eventually closed it `never_resolved` —
        # asserting the attempt never ran while the scheduler's own history
        # says otherwise. That is the pre-created-row case: a row created at
        # submission, before any attempt existed, so it carries no index.
        #
        # The pick must be deterministic, and one choice is: the row was
        # created by the submitter, so it stands for the job's FIRST attempt.
        # Every later attempt is a retry, and retries get their own rows from
        # `_resolve_discovered` above rather than competing for this one.
        # Choosing the lowest index is therefore not a heuristic tiebreak — it
        # is the same rule the resolver applies, so both agree about which row
        # means which attempt.
        indexed = [observation for observation in observations
                   if observation.attempt_index is not None]
        if not indexed:
            return None
        return min(indexed, key=lambda observation: observation.attempt_index)

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
            # Running, not stuck. The reconciler owes this attempt nothing yet.
            return "waiting"

        if not beyond_grace_horizon(observation.stopped_at, now=self._now()):
            logger.debug("attempt %s is scheduler-terminal but inside the "
                         "grace horizon; leaving it open", attempt_id)
            return "waiting"

        return self._classify(row, observation)

    def _check_binding_drift(self, row, observation):
        """Compare the recorded execution binding with the scheduler's view.

        Review finding #11. The submission-time binding records the exact
        job-definition ARN and revision the submitter intended; the scheduler
        reports the definition a job actually ran under. Those disagreeing
        means the job ran under a definition nobody recorded — a different
        image, different resources, a different command — and the design
        says the reconciler "cross-checks the binding against the scheduler's
        view, flagging drift".

        Flagged, not fatal: the attempt DID run, its outcome is real, and
        refusing to close it would strand a finished job. What drift changes
        is that the disagreement is recorded and visible rather than silently
        absent.

        Returns the observed identity when it disagrees, else None.
        """
        observed = getattr(observation, "job_definition", None)
        if not observed:
            return None

        recorded_arn = row.get("binding_job_definition_arn")
        recorded_rev = row.get("binding_job_definition_rev")
        if not recorded_arn:
            return None

        base, _, suffix = recorded_arn.rpartition(":")
        expected = recorded_arn if (base and suffix.isdigit()) \
            else f"{recorded_arn}:{recorded_rev}"

        # Batch may report the bare name rather than the full ARN depending on
        # how the job was submitted; compare on the definition NAME and
        # revision, which both forms carry, rather than on string equality
        # that would flag a formatting difference as drift.
        if _definition_identity(observed) == _definition_identity(expected):
            return None

        self._binding_drift += 1
        logger.warning(
            "attempt %s ran under job definition %s but its submission-time "
            "binding records %s; recording drift",
            row["attempt_id"], observed, expected)
        return observed

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
        read = closure_mod.read_predecessor(
            self.records_store, predecessor_key, attempt_id)

        # A store that could not answer is not evidence (review finding #16).
        # Closing here would publish an authoritative reconciler-first record
        # that omits facts which exist and are readable a second later, and
        # terminalize the row so nothing ever revisits it. Defer instead: the
        # row stays open, the next poll retries, and the failure is counted so
        # a PERSISTENT store fault is visible in health rather than silently
        # converting every attempt into a lossy record.
        if read.deferred:
            self._closure_failures += 1
            logger.warning(
                "the records store could not be read for attempt %s (%s); "
                "deferring rather than closing on absent evidence",
                attempt_id, predecessor_key)
            return "deferred"

        predecessor, rejected = read.body, read.reason

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

        # A RECONSTRUCTION READS WHAT SURVIVED (review finding #16). Where
        # there is no predecessor to fold in, the record is built from
        # whatever the attempt left behind — and the attempt left more than
        # the row: `attempt_stages` rows written as each stage finished, and
        # the CloudWatch stream. Reading them is what makes "complete
        # canonical snapshot" true of a reconstructed record rather than
        # merely claimed by it.
        stages = None
        log_tail = None
        if predecessor is None:
            stages = closure_mod.read_attempt_stages(self.conn, attempt_id)
            log_tail = closure_mod.read_log_stream(
                self.logs, self._log_group_for(row),
                observation.log_stream if observation is not None else None)

        record = closure_mod.build_closure_record(
            row, observation,
            sequence=self._next_sequence(row),
            predecessor=predecessor,
            rejected_key=(predecessor_key if predecessor is None else None),
            rejected_reason=rejected,
            classification=classification,
            error_category=error_category,
            stages=stages, log_tail=log_tail,
            # Drift goes INTO the record, not only into a log line (#11): the
            # closure record is the durable terminal account, and "this
            # attempt ran under a definition nobody recorded" belongs in it.
            binding_drift=self._check_binding_drift(row, observation),
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
                         classification, error_category, read)
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

        An absent bundle is not a failure — but only for an attempt that
        genuinely NEVER STARTED (round 2 of #16). Such an attempt had no
        container in which to build one, so "nothing to retain" is the literal
        truth. For an attempt that DID run, an absent bundle means the
        diagnostics for a real execution are gone.

        Round 2 noted that gap and continued. Round 3 CLOSES it: the design
        rule is unconditional — the bundle exists before the attempt is closed,
        whichever way it died — so where the attempt did not write one, the
        reconciler builds it from the CloudWatch stream and marks it
        reconstructed. See `reconstruction`, which also explains why an
        unrecoverable log writes a bundle recording the gap rather than
        deferring an attempt nothing could ever close.
        """
        key = termination.bundle_key(
            self.records_prefix, row["run_id"], row["logical_job_id"],
            row["attempt_id"])
        outcome = (predecessor or {}).get("rapid_outcome") or row.get(
            "rapid_outcome")
        retention_class = retention_mod.retention_class_for(
            outcome, observation.state if observation is not None else None)
        stamped = retention_mod.stamp_retention(
            self.s3, self.diagnostics_bucket, key, row, retention_class)

        if stamped is None:
            ran = self._attempt_ran(row, predecessor, observation)
            if ran:
                self._missing_bundles += 1
                logger.warning(
                    "attempt %s ran but has no diagnostics bundle at %s; the "
                    "evidence for a real execution is absent, which is NOT the "
                    "never-started 'nothing to retain' case",
                    row["attempt_id"], key)
            else:
                # THE RULE HAS NO EXCEPTION IN IT (round-4 finding #5). A
                # never-started attempt used to be left with no bundle at all,
                # on the reasoning that it had no container in which to build
                # one and so "nothing to retain" was the literal truth. The
                # adopted design says otherwise — it names "abrupt loss, or
                # never started" as the reconstruction cases together, and
                # puts the bundle before EVERY close.
                #
                # It is also the more useful truth. What is retained for an
                # attempt that never started is not its output but the account
                # of its non-execution: what was submitted, what the scheduler
                # said (or that it said nothing), and why the reconciler closed
                # it. Without that the only trace of a provisioning failure is
                # a terminal row, and terminal rows are outside the open set —
                # nothing ever comes back to explain them.
                logger.info(
                    "attempt %s never started and has no diagnostics bundle "
                    "at %s; building the minimal reconstructed bundle that "
                    "records its non-execution", row["attempt_id"], key)

            # AND THEN BUILD ONE (round-3 finding #5). Noticing the gap and
            # continuing to the terminal transition is what left abruptly
            # killed attempts permanently terminal with no diagnostics at all
            # — and terminal rows are outside the open set, so nothing ever
            # came back for them. The design's rule has no exception in it:
            # the bundle exists before the attempt is closed, whichever way it
            # died, and where the attempt did not write one the reconciler
            # builds it from the CloudWatch stream and marks it reconstructed.
            #
            # A reconstruction that cannot be UPLOADED is a different kind of
            # failure from one whose log has expired: the first is a condition
            # a later poll may find resolved, so it defers; the second is
            # permanent, and the bundle is written anyway with the gap
            # recorded inside it. Deferring on the second could never
            # terminate — deferral here is unbounded, and the CloudWatch
            # stream expires at 14 days, so the raw material is on a clock.
            rebuilt = reconstruction.reconstruct_bundle(
                self.diagnostics_store, key, row, observation,
                self.logs, self._log_group_for(row), now=self._now())
            if rebuilt is None:
                raise BundleReconstructionFailed(
                    f"attempt {row['attempt_id']} "
                    f"{'ran' if ran else 'never started'}, has no diagnostics "
                    f"bundle at {key}, and one could not be written there. "
                    f"Deferring rather than closing an attempt whose evidence "
                    f"is missing and unrecoverable.")
            self._reconstructed_bundles += 1

            # Stamp the retention class onto what was just created, so a
            # reconstructed bundle is governed by the same lifecycle rules as
            # any other. Skipping this would leave it untagged and therefore
            # outside every retention rule, which is how a bundle quietly
            # outlives or predeceases the account that cites it.
            stamped = retention_mod.stamp_retention(
                self.s3, self.diagnostics_bucket, key, row, retention_class,
                extra={"reconstructed": "true"})
        return stamped

    def _transition(self, row, observation, writer, record, written,
                    classification, error_category, read=None):
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
            # A child that never started never produced an outcome. It used
            # to close the work unit `failed` unconditionally, on the reading
            # that "the design gives no third disposition beyond
            # complete/failed" — but the design DOES: `blocked` is the third
            # disposition, and retry policy v1 names which failures reach it
            # (rule 4 repair). A container that never started is the
            # scheduler-visible case par excellence — nothing was learned
            # about the work — so the category on the row now decides, and
            # for a provisioning failure that means a NEW attempt rather than
            # a tombstone on the logical work.
            self._close_work_unit(row, outcome="failed",
                                  error_category=error_category)
            return

        if classification == CLASS_ABRUPT_LOSS:
            writer.mark_abrupt_loss(
                attempt_id, ended_at=ended_at,
                scheduler_state=observation.state,
                error_category=error_category,
                scheduler_observed_exit=observation.exit_code,
                terminal_record_key=written.key,
                terminal_record_sequence=landed_sequence,
                # The checksum of the bytes just written (migration 022's
                # column, catalog design § Promotion). The reconciler authored
                # this record, so it is the one writer that knows the digest
                # without re-reading the object — and a cited key with no
                # checksum is a pointer a reader is told to distrust and given
                # no way to verify.
                terminal_record_checksum=written.checksum)
            # ABRUPT LOSS IS THE ARCHETYPE THIS REPAIR EXISTS FOR (rule 4).
            # An OOM kill or a Spot reclaim is a physical event about a
            # container, carrying no verdict on the logical work; closing the
            # unit `failed` here was the single most damaging instance of
            # "closes from an intermediate physical failure". The category
            # (scheduler_reclaimed for a reclaim) now routes it back to
            # `ready` for a new attempt, under the ceiling.
            self._close_work_unit(row, outcome="failed",
                                  error_category=error_category)
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
            # THE CRASH BOUNDARY THIS EXISTS FOR (review finding #14).
            #
            # Materialization is reached precisely when sequence 0 was written
            # and `mark_application_closed` did not land. In that state the row
            # holds NULL for both `terminal_record_key` and
            # `terminal_record_checksum` — the application sets them in the very
            # transition that failed — and the record BODY has never carried
            # them either, because a record cannot contain its own key or the
            # checksum of its own bytes. Reading them from the body or the row
            # therefore yielded NULL exactly here, and 013 requires a non-null
            # key for `application_closed`: every pass attempted an illegal
            # transition, caught it as a per-row error, and left the attempt
            # `started` forever. Registration never saw it.
            #
            # The reconciler does not need either value handed to it. It just
            # located that object, read its bytes and checksummed them to
            # validate it — so it KNOWS the key it read from and the checksum it
            # computed. Those are supplied here, with the row and body only as
            # fallbacks for the ordinary already-closed case.
            key = (read.key if read is not None and read.usable else None) \
                or body.get("terminal_record_key") \
                or row.get("terminal_record_key")
            checksum = (read.checksum
                        if read is not None and read.usable else None) \
                or body.get("terminal_record_checksum") \
                or row.get("terminal_record_checksum")
            writer.mark_application_closed(
                attempt_id, ended_at=ended_at,
                application_intended_exit=body.get(
                    "application_intended_exit") or 0,
                rapid_outcome=_enum(RapidOutcome, body.get("rapid_outcome")),
                product_disposition=_enum(
                    ProductDisposition, body.get("product_disposition")),
                terminal_record_key=key,
                terminal_record_sequence=body.get(
                    "terminal_record_sequence")
                or termination.APPLICATION_RECORD_SEQUENCE,
                terminal_record_checksum=checksum,
                error_category=error_category,
                reconciler_materialized=True)

        # The CITATION MOVES AS A TRIPLE (round-3 finding #1). Materialization
        # above cites sequence 0 — the record the application left behind — and
        # this call then supersedes it with the reconciler's own closure
        # record. It is NOT an `else`: both writers run on the crash boundary.
        # Advancing key and sequence while leaving the sequence-0 checksum in
        # place left every materialized row citing bytes that do not hash to
        # the value beside them, and the registrar — which fetches
        # `terminal_record_key` and checksums exactly those bytes — refused
        # them all. Sequence 1 folding the predecessor's FACTS in verbatim does
        # not make the two records' BYTES equal.
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
            terminal_record_sequence=landed_sequence,
            terminal_record_checksum=written.checksum)

        # SUCCESS PROPOSES complete; EVERY OTHER TERMINAL DISPOSITION GOES TO
        # RETRY POLICY (rule 4 repair). The earlier rule here was "any other
        # terminal disposition -> submitted->failed", which tombstoned the
        # logical work on one attempt's application failure — the case policy
        # v1 explicitly parks instead ("never tombstoned"). `rapid_outcome` is
        # still the application's OWN verdict
        # (`RapidOutcome.SUCCESS`/`FAILURE`/`PARTIAL`); what changed is that a
        # non-SUCCESS verdict is now an INPUT to the policy decision rather
        # than a synonym for the unit's death.
        #
        # PARTIAL still does not complete the unit — the work unit's six
        # states have no partial-success cell, and "the smallest affected
        # unit" the failure-path design scopes retries to is the
        # ATTEMPT/candidate level. It now parks rather than tombstones, which
        # is the same judgment call one state to the left: an operator can see
        # it and act, and nothing is lost.
        #
        # Note `_close_work_unit` also checks the series before applying any
        # of this: if a sibling attempt was already accepted, this row's
        # failure changes nothing about the unit.
        outcome = body.get("rapid_outcome")
        self._close_work_unit(
            row, outcome="complete" if outcome == RapidOutcome.SUCCESS.value
            else "failed",
            error_category=error_category)

    def _work_unit_series(self, work_unit_id, exclude_attempt_id):
        """The sibling attempts of one work unit: succeeded-yet? and loss count.

        **WHY THE SERIES AND NOT THE ROW** (rule 4, verbatim: "closure
        decisions consult the current attempt series, not the single attempt
        row that triggered reconciliation"). Reconciliation is per-attempt
        and its ordering is not guaranteed: a unit whose attempt 1 was
        OOM-killed and whose attempt 2 succeeded can have attempt 1's
        terminal disposition reconciled AFTER attempt 2's acceptance —
        supersession requeries deliberately re-examine terminal rows. A
        closure decision made from attempt 1's row alone would then
        overwrite a legitimately `complete` unit with a verdict about a
        container that died, which is precisely the "closes from an
        intermediate physical failure" the rule forbids.

        So the closure sites ask the series two questions this method
        answers in ONE round trip:

          * has any OTHER attempt at this work already been accepted? If so
            the logical work is done and this row's failure is history.
          * how many scheduler-visible losses has the series absorbed? —
            the input to `retry_policy`'s ceiling, which is a property of
            the UNIT's history, not of any one attempt.

        `exclude_attempt_id` keeps the triggering row out of its own census:
        its disposition is the caller's argument, and at this point in the
        transaction its own UPDATE has already landed, so counting it would
        double-count the very loss being classified.

        Runs on the same connection and therefore the same transaction as
        the closure it informs (see `_close_work_unit`'s atomicity note), so
        it observes the caller's own uncommitted writes — the correct
        reading, not a stale snapshot.
        """
        rows = self._execute_on_conn(
            "SELECT registered_at, error_category"
            "  FROM attempts"
            " WHERE work_unit_id = %s AND attempt_id <> %s",
            [work_unit_id, exclude_attempt_id])
        sibling_accepted = False
        scheduler_losses = 0
        for registered_at, error_category in rows or ():
            # ACCEPTANCE, NOT "the row looks successful": rule 4 admits
            # `complete` only "from an accepted (registered) result", and
            # `registered_at` is the column the registrar stamps inside the
            # same transaction as the product rows and the watermark (see
            # `pipeline.registration.consumer.mark_registered`). An attempt
            # that closed with a SUCCESS outcome but was never registered has
            # produced no accepted result and must not complete the unit —
            # which is exactly why the successful-attempt check reads this
            # column and not `rapid_outcome`.
            if registered_at is not None:
                sibling_accepted = True
            if error_category in RECONCILER_ERROR_CATEGORIES:
                scheduler_losses += 1
        return sibling_accepted, scheduler_losses

    def _execute_on_conn(self, sql, params):
        """One read on the caller's open transaction. See `_Executor`."""
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            if cur.description is not None:
                return cur.fetchall()
            return ()

    def _close_work_unit(self, row, outcome, error_category=None):
        """Resolve this attempt's work unit under retry policy v1.

        **THIS METHOD NO LONGER MEANS "CLOSE"** — it means "hand the
        attempt's terminal disposition to retry policy and apply the
        answer". The rename was not made because every caller and every
        test names it, but the four outcomes it can now produce are stated
        here rather than implied by the argument:

          accepted result        -> submitted -> complete   (the only
                                    complete path — rule 4)
          scheduler-visible loss -> submitted -> ready      (a NEW attempt
                                    follows; rule 5)
          application failure    -> submitted -> blocked    (park-until-
                                    change; "never tombstoned")
          policy exhausted       -> submitted -> failed     (the ceiling)

        **WHAT CHANGED AND WHY** (rule 4 repair). Every call site used to
        pass `outcome="failed"` for any terminal disposition that was not
        SUCCESS, and this method transitioned straight to FAILED. That
        tombstoned the logical work on the first physical failure of any
        attempt at it — an OOM kill, a Spot reclaim, a missing input — while
        `observability.attempts`'s own stated policy said application
        failures are "never tombstoned" and scheduler failures are the
        automatic-retry surface. The code and its policy statement
        contradicted each other; `pipeline.intent.retry_policy` is now the
        one executable authority and this method consults it.

        The defect was MASKED, not absent: with no workflow definition
        loaded, `work_unit_id` was NULL on every row and every call here
        returned at the guard below. The definition-loading step makes the
        intent layer live, so this repair lands with it.

        **NULL work_unit_id IS SKIPPED SILENTLY** (task brief, verbatim
        rule): every attempt row predating migration 036, and every attempt
        whose job type has no loaded workflow_definitions row yet (see
        `pipeline.seams._attach_work_unit`'s FK-guard catch — as of this
        writer, EVERY job type is in that state), carries `work_unit_id
        IS NULL`. Those rows have no work unit to transition, and reaching
        into the intent layer for them would either no-op against nothing
        or, worse, raise on a row this reconciler has no business touching.
        This is the one guard that keeps the intent-layer integration inert
        for the whole fleet of pre-intent-layer and not-yet-defined-job-type
        attempts, exactly mirroring `AttemptWriter`'s own "absent means
        absence, not a sentinel" posture one layer up.

        **ATOMICITY — stated exactly, not assumed** (task brief: "report
        EXACTLY what atomicity you achieved"). This method is called from
        `_transition`, which runs inside `_close`'s caller's
        `attempt_lease(self.conn, attempt_id)` block (see `_classify` and
        `_reconcile_unresolved`, both of which hold the lease across the
        WHOLE closure sequence — closure record publish, retention stamp,
        row transition — and commit or roll back the lease's transaction as
        one unit). `WorkUnitWriter(_Executor(self.conn))` below opens no
        new transaction and shares the exact connection `AttemptWriter`'s
        own `writer` was built from earlier in the same `_classify`/
        `_reconcile_unresolved` call — so the work-unit transition and its
        unit_event land in the SAME database transaction as the attempt's
        own closing UPDATE, and a crash between them is impossible by
        construction (either both commit, at the lease's `conn.commit()`,
        or neither does, at its `conn.rollback()`). This is genuine
        same-transaction atomicity, not best-effort-after-commit — verified
        by reading `lease.attempt_lease`'s own docstring and body rather
        than assumed from the call shape.

        A work unit that is not currently 'submitted' (an operator already
        force-transitioned it, or a second reconciliation pass reaches a
        row whose work unit a first pass already closed — the supersession
        requery's own re-closure path, SUPERSEDABLE_STATES) raises
        `WorkUnitNotFound` from the CAS guard; this is caught and logged
        rather than propagated, because the ATTEMPT'S own closure is the
        transition this method's caller must not fail on — a work unit
        already resolved by another writer is not this reconciler's
        problem to force.
        """
        work_unit_id = row.get("work_unit_id")
        if work_unit_id is None:
            return

        attempt_id = row["attempt_id"]
        succeeded = outcome == "complete"
        if error_category is None:
            error_category = row.get("error_category")

        # THE SERIES DECIDES, NOT THIS ROW. A sibling attempt that was
        # already accepted means the logical work is done: this row's
        # failure is an intermediate physical event with no verdict left to
        # cast. Rule 4's "a later successful attempt of the same logical
        # work must be able to complete the unit" also runs the other way —
        # an EARLIER-reconciled success must not be undone by a
        # later-reconciled failure.
        sibling_accepted, scheduler_losses = self._work_unit_series(
            work_unit_id, attempt_id)
        if not succeeded and sibling_accepted:
            logger.info(
                "attempt %s failed but work unit %s already has an accepted "
                "attempt; leaving the unit as it stands rather than casting "
                "an intermediate physical failure as the unit's verdict",
                attempt_id, work_unit_id)
            return

        disposition = disposition_for_terminal_attempt(
            succeeded=succeeded,
            error_category=error_category,
            scheduler_loss_count=scheduler_losses)

        blocked_reason = None
        if disposition == CLOSE_COMPLETE:
            to_state = COMPLETE
        elif disposition == RETRY_READY:
            to_state = READY
        elif disposition == PARK_BLOCKED:
            to_state = BLOCKED
            blocked_reason = blocked_reason_for(error_category)
        else:
            to_state = FAILED

        work_writer = WorkUnitWriter(_Executor(self.conn))
        try:
            work_writer.transition_unit(
                work_unit_id, SUBMITTED, to_state,
                writer=WRITER_RECONCILER,
                blocked_reason=blocked_reason,
                # The decision's own provenance, so an operator reading the
                # unit_event can tell which policy version and which
                # observed category produced this edge — the same reasoning
                # `observability.attempts` gives for stamping
                # retry_policy_version on every attempt.
                detail={
                    "disposition": disposition,
                    "retry_policy_version": policy_version(),
                    "error_category": error_category,
                    "scheduler_loss_count": scheduler_losses,
                    "deciding_attempt_id": attempt_id,
                },
                now=self._now())
        except WorkUnitNotFound:
            logger.info(
                "work unit %s (attempt %s) was not in 'submitted' when the "
                "reconciler tried to move it to %s; another writer already "
                "resolved it, so the attempt's own closure proceeds without "
                "forcing the work unit", work_unit_id, attempt_id, to_state)

    # -- the never-resolved case -----------------------------------------

    def _submission_classification(self, row):
        """What the durable submission record says about this attempt, if
        anything — `FOUND`, `LOST`, or `None` (open/ambiguous or no record).

        FAILS OPEN, unlike admission's fail-closed posture (`pipeline.seams`,
        "a protocol failure NEVER blocks a submission"). Three ways this
        degrades to `None` rather than raising:

        * No `attempts.submission_id` on the row (pre-044, or a row a
          submission pass could not attach) — `submission_for_attempt`
          itself returns `None` for that case.
        * The lookup raises — logged and treated as "nothing to conclude
          from", never as evidence of absence. A bookkeeping read failing is
          not the same fact as a job not existing, and must not block
          reconciliation or be mistaken for a LOST verdict.
        """
        attempt_id = row.get("attempt_id")
        submission_id = row.get("submission_id")
        if not submission_id:
            return None
        try:
            submission = submission_protocol.submission_for_attempt(
                _Executor(self.conn), attempt_id)
        except Exception:  # noqa: BLE001 - fail open, never block on this
            self._safe_rollback()
            logger.exception(
                "could not read the submission record for attempt %s; "
                "falling through to the submission-anchored horizon",
                attempt_id)
            return None
        if submission is None:
            return None
        state = submission.get("state")
        if state in (submission_protocol.FOUND, submission_protocol.LOST):
            return state
        return None

    def _reconcile_unresolved(self, row):
        """A pre-created child the scheduler cannot account for.

        Reached two ways (S2, rule 7 package): (a) partitioned unresolved at
        `poll_once` because `scheduler_job_id IS NULL`; (b) redirected from
        `_reconcile_attempt` when Batch returned the job but no attempt
        observation could be paired — those rows DO carry a
        `scheduler_job_id`, so nothing here may assume it is absent.

        THE SUBMISSION RECORD DECIDES FIRST, THE HORIZON IS THE BACKSTOP
        (`horizons.py`'s own docstring). A durable FOUND/LOST record is
        positive evidence; a clock is not — so the clock is consulted only
        where there is no such evidence to consult (open/ambiguous, or no
        submission row at all: every pre-044 attempt).
        """
        classification = self._submission_classification(row)
        if classification == submission_protocol.FOUND:
            # The re-query is positive: the job exists and is running. This
            # is the case the clock got wrong — it must not be classified
            # never-resolved, however far past the horizon `submitted_at` is,
            # and it must not be resubmitted.
            return "waiting"
        if classification == submission_protocol.LOST:
            # A negative re-query past ITS OWN deadline
            # (`RESOLUTION_HORIZON_SECONDS`, enforced inside `protocol.
            # resolve`) is positive evidence of absence. Classification may
            # proceed without also waiting on the submission-anchored
            # horizon — the two horizons are kept equal, but requiring both
            # to elapse would let the slower of two agreeing clocks delay a
            # conclusion the evidence already supports.
            pass
        elif not beyond_submission_horizon(row.get("submitted_at"),
                                           now=self._now()):
            # No submission evidence either way (still open/ambiguous, or no
            # submission row at all — the backstop's two legitimate roles).
            # Inside the submission-anchored horizon: queue time, not a fault.
            return "waiting"

        attempt_id = row["attempt_id"]
        with attempt_lease(self.conn, attempt_id) as held:
            if not held:
                return "skipped"
            current = reread_attempt(self.conn, attempt_id,
                                     columns=_OPEN_COLUMNS)
            if current is None or current["lifecycle_state"] not in OPEN_STATES:
                return "skipped"

            # THE BUNDLE COMES FIRST, AND IT IS NOT OPTIONAL (round-4 finding
            # #5). This path used to publish a closure record and transition
            # the row without invoking bundle handling at all — so the one
            # class of attempt the design explicitly names for reconstruction,
            # "never started", was the one class that closed with no
            # diagnostics whatsoever. The rule the design states is
            # unconditional: the bundle exists before the attempt is closed.
            #
            # BEFORE the closure record, not after, so the ordering matches
            # the rule rather than merely satisfying it by coincidence. If the
            # bundle cannot be written the attempt defers with nothing
            # published, which is recoverable; the reverse order would leave a
            # published closure citing an attempt whose bundle never appeared.
            try:
                self._stamp_bundle(current, None, None)
            except Exception:  # noqa: BLE001 - deferred, not swallowed (#16)
                self._closure_failures += 1
                logger.exception(
                    "could not write or stamp the diagnostics bundle for "
                    "unresolved attempt %s; it stays open and the next poll "
                    "retries it rather than closing with no evidence",
                    attempt_id)
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
            # A child that never resolved never did its work unit's work
            # either — but "never did the work" is not "the work can never be
            # done", and this site had been closing the unit `failed` at the
            # horizon (rule 4: closure only from acceptance or policy
            # exhaustion — a horizon is neither). The category written one
            # statement above is `scheduler_provisioning`: scheduler-visible,
            # so policy v1 returns the unit to `ready` for a new attempt, and
            # a unit whose attempts keep vanishing hits the ceiling and fails
            # explicitly instead of on the first disappearance.
            #
            # Reached through the SAME open `current` row (and therefore the
            # same `work_unit_id`, if any) `reread_attempt` fetched under this
            # method's own lease.
            self._close_work_unit(current, outcome="failed",
                                  error_category="scheduler_provisioning")
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


def _definition_identity(reference):
    """`<name>:<revision>` from a job-definition ARN or bare name.

    Batch reports `jobDefinition` as a full ARN or as `name:revision`
    depending on how the job was submitted, and the binding records an ARN.
    Comparing on name and revision means a formatting difference is not
    mistaken for drift while a real difference still is (#11).
    """
    if not reference:
        return None
    return str(reference).rsplit("/", 1)[-1]


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

    A POLL THAT RAISES IS NOT THE ONLY WAY TO STOP WORKING (round-3 finding
    #6). A poll can return perfectly normally having classified nothing — every
    row it touched deferred or errored — and repeat that forever. Round 2 added
    `consecutive_unproductive_polls` and folded it into `service.healthy`, but
    nothing ever READ the property, so the second threshold governed nothing and
    the unit stayed up with `CLOSURE_FAILURE_POLL_THRESHOLD`'s stated purpose
    ("the unit flips within a knowable time") unrealized. The check belongs on
    the SUCCESS path, because that is the path the condition occurs on.
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

            # The poll returned, and the service may still be incapable of
            # work. `healthy` is the whole judgement — both thresholds — so it
            # is asked rather than re-deriving one of its terms here.
            if not service.healthy:
                raise ReconcilerUnhealthy(
                    f"the reconciler polled successfully but is not healthy: "
                    f"{service.health()}. Attempts are being reached and none "
                    f"is being classified, which is a working process doing no "
                    f"work — the exact condition a liveness check cannot see. "
                    f"Exiting so the supervisor restarts it.")

        elapsed = time.monotonic() - started
        remaining = poll_seconds - elapsed
        if remaining > 0:
            sleep(remaining)
