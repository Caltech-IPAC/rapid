"""The registration consumer: query reconciled attempts, decide, register.

The query is the interesting part, because it is what "consumes reconciled
outcomes only" actually means in SQL:

    lifecycle_state IN (terminal_after_start, terminal_without_start)
    AND terminal_record_sequence >= 1

Both halves are load-bearing. The lifecycle state alone is not enough: the
reconciler transitions the row and publishes the closure record inside one
lease, but a consumer that checked only the state could still read a row mid
lease. Requiring a record sequence at or above 1 means the *reconciler's* own
record exists — sequence 0 is the application's, and its presence proves
nothing about scheduler truth.

Reprocessing on supersession falls out of the same query rather than needing a
mechanism: a superseding classification raises `terminal_record_sequence`, so
an attempt already registered at sequence 1 becomes a candidate again at
sequence 2. The registered-watermark column is what makes that idempotent —
an attempt is skipped when its registered sequence already matches the current
one, and re-registered when it does not.
"""

import datetime
import logging

from observability.attempts import LifecycleState
from observability.registration import RegistrationDecision, decide

logger = logging.getLogger("rapid.registration")

EXIT_OK = 0
EXIT_FAILURES = 65

#: The states the reconciler leaves behind. `missing_or_contradictory` is
#: deliberately absent: it is the state for attempts the stores disagree
#: about, and registering products on a disagreement is exactly the quiet
#: wrongness the taxonomy exists to prevent.
RECONCILED_STATES = (
    LifecycleState.TERMINAL_AFTER_START.value,
    LifecycleState.TERMINAL_WITHOUT_START.value,
)

#: The columns the decision function reads, plus the identity it reports on.
_COLUMNS = (
    "attempt_id", "run_id", "logical_job_id", "lifecycle_state",
    "rapid_outcome", "product_disposition", "scheduler_state",
    "application_intended_exit", "scheduler_observed_exit", "error_category",
    "exposure_id", "sca", "sky_tile", "terminal_record_key",
    "terminal_record_sequence",
    # The watermark, so a caller can see what a candidate was last registered
    # at without a second query (review finding #5).
    "registered_at", "registered_record_sequence",
)

# THE REGISTERED WATERMARK (review finding #5).
#
# Without the last predicate this selected every reconciled attempt on every
# pass, so a registration job re-registered the same attempts forever. The
# watermark is the SEQUENCE registered rather than a boolean, which is what
# makes the design's "reprocesses on a later supersession" work: an attempt
# registered at sequence 1 whose reconciler later publishes sequence 2 has a
# record sequence ahead of its watermark and becomes a candidate again.
_CANDIDATE_SQL = (
    "SELECT " + ", ".join(_COLUMNS) +
    " FROM attempts"
    " WHERE lifecycle_state = ANY(%s)"
    "   AND terminal_record_sequence >= 1"
    "   AND (registered_record_sequence IS NULL"
    "        OR registered_record_sequence < terminal_record_sequence)"
    " ORDER BY attempt_id"
)

_MARK_REGISTERED_SQL = (
    "UPDATE attempts"
    "   SET registered_at = %s, registered_record_sequence = %s"
    " WHERE attempt_id = %s"
    "   AND (registered_record_sequence IS NULL"
    "        OR registered_record_sequence < %s)"
)


class _Row:
    """Attribute access over a result row, which is what `decide` expects."""

    def __init__(self, mapping):
        self.__dict__.update(mapping)

    def __repr__(self):
        return f"<attempt {getattr(self, 'attempt_id', '?')}>"


class RegistrationRun:
    """One registration pass. Counts what happened; exit code follows."""

    def __init__(self):
        self.registered = 0
        self.skipped = 0
        self.deferred = 0
        self.failed = 0
        self.refused_application_failed = 0
        #: Candidates a DRY RUN would have registered. Deliberately not
        #: folded into `registered` (review finding #5): the two were the
        #: same counter, so a run that wrote nothing reported registrations.
        self.would_register = 0

    @property
    def exit_code(self):
        """Nonzero when this run could not do its job.

        The four scripts this replaces all hardcoded exit 0, so a registration
        run that registered nothing because every call raised looked exactly
        like a clean run with nothing to do.
        """
        return EXIT_FAILURES if self.failed else EXIT_OK

    def as_dict(self):
        return {
            "registered": self.registered,
            "skipped": self.skipped,
            "deferred": self.deferred,
            "failed": self.failed,
            "refused_application_failed": self.refused_application_failed,
            "would_register": self.would_register,
            "exit_code": self.exit_code,
        }


def candidates(conn, states=RECONCILED_STATES):
    """Attempts the reconciler has closed and published a closure record for."""
    with conn.cursor() as cur:
        cur.execute(_CANDIDATE_SQL, (list(states),))
        names = [description[0] for description in cur.description]
        rows = [dict(zip(names, row)) for row in cur.fetchall()]
    conn.rollback()  # read-only; do not hold a transaction open
    return rows


def mark_registered(conn, attempt_id, record_sequence, now=None):
    """Record that this attempt was registered at `record_sequence`.

    The watermark write (review finding #5). Guarded so a replay or a
    concurrent pass cannot move the watermark backwards — the sequence only
    ever advances, which is what makes "reprocesses on a later supersession"
    safe to run repeatedly.
    """
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    sequence = int(record_sequence if record_sequence is not None else 1)
    with conn.cursor() as cur:
        cur.execute(_MARK_REGISTERED_SQL,
                    (moment, sequence, attempt_id, sequence))
    conn.commit()
    return sequence


def register_batch(conn, rows, register=None, run=None, dry_run=False):
    """Decide and register each candidate. Returns the `RegistrationRun`.

    `register(row, decision)` performs the actual product registration and is
    injected: what registering *means* is the science layer's business, and
    this module's job is deciding whether it may happen at all.

    **A dry run must be ASKED FOR (review finding #5).** Omitting `register`
    used to make this a dry run that counted every candidate as `registered`
    and logged "would register" — and the production dispatch path omitted it,
    so the registration job returned `registered=N`, outcome success and exit
    0 while writing no operation-table rows. Every later pass then selected
    and "registered" the same attempts again, forever.

    Now a missing callback is an error unless `dry_run=True` says the caller
    meant it. The dry-run counts are also kept apart from the real ones, so a
    rehearsal can never be mistaken for a registration in a log or a metric.
    """
    if register is None and not dry_run:
        raise ValueError(
            "register_batch needs a `register` callback: without one it can "
            "only decide, not register, and reporting those decisions as "
            "registrations is what made the registration job return "
            "registered=N while writing nothing. Pass dry_run=True to ask "
            "for a rehearsal deliberately.")

    run = run or RegistrationRun()

    for row in rows:
        attempt = _Row(row)
        verdict = decide(attempt)

        if verdict.decision is RegistrationDecision.DEFER:
            run.deferred += 1
            logger.info("attempt %s deferred: %s",
                        verdict.attempt_id, verdict.reason)
            continue

        if verdict.decision is RegistrationDecision.SKIP:
            run.skipped += 1
            # An application failure under a SUCCEEDED scheduler state is the
            # case the old chain got wrong by construction: its log-grep saw
            # exit 0 and registered the products of a failed run. Counted
            # separately so the refusal is visible, not merely correct.
            if row.get("rapid_outcome") == "failure":
                run.refused_application_failed += 1
                logger.info(
                    "attempt %s REFUSED registration: application reported "
                    "failure (scheduler said %s, intended exit %s) — %s",
                    verdict.attempt_id, row.get("scheduler_state"),
                    row.get("application_intended_exit"), verdict.reason)
            else:
                logger.info("attempt %s skipped: %s",
                            verdict.attempt_id, verdict.reason)
            continue

        if register is None:
            # Reached only under dry_run=True (checked above). Counted apart
            # from the real registrations so a rehearsal cannot be read as one.
            run.would_register += 1
            logger.info("attempt %s WOULD register (dry run) (%s)",
                        verdict.attempt_id, verdict.reason)
            continue

        try:
            register(row, verdict)
        except Exception:  # noqa: BLE001 - counted, not swallowed
            run.failed += 1
            logger.exception("registering attempt %s failed",
                             verdict.attempt_id)
        else:
            # The watermark is written only after the registration itself
            # succeeded, so a failure leaves the attempt a candidate for the
            # next pass rather than marking work that did not happen.
            try:
                mark_registered(conn, verdict.attempt_id,
                                row.get("terminal_record_sequence"))
            except Exception:  # noqa: BLE001 - counted, not swallowed
                run.failed += 1
                logger.exception(
                    "attempt %s was registered but its watermark could not "
                    "be written; it will be re-selected next pass",
                    verdict.attempt_id)
            else:
                run.registered += 1
                logger.info("attempt %s registered: %s",
                            verdict.attempt_id, verdict.reason)

    logger.info("registration pass: %s", run.as_dict())
    return run
