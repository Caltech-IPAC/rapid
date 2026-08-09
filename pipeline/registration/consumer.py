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

ONE TRANSACTION PER ATTEMPT (round-3 finding #8). The watermark only means
what the paragraph above says if it moves in the same transaction as the
product rows it is a watermark FOR. It did not: the registrar wrote products
on its own autocommitting connection and this module committed the watermark
on another, so between the two there was a durable window in which the rows
existed and the attempt was still a candidate. Every crash in that window
produced a duplicate registration on the next pass. `register_batch` now wraps
each attempt's registration and its watermark in one `transaction(conn)`, and
the registrar borrows this connection rather than opening its own — the two
halves of the same fix, neither of which works alone.
"""

import contextlib
import datetime
import json
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
    # The record's checksum, so a registrar can VERIFY the bytes it reads
    # rather than trusting whatever is at the key. Registration acts on what
    # the record says; reading it unverified would reintroduce the
    # trust-mutable-external-state problem the record exists to remove.
    "terminal_record_checksum",
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


@contextlib.contextmanager
def _transaction(conn):
    """`rapid_db_connect.transaction`, resolved at call time.

    The sanctioned unit-of-work context manager lives in
    `database.modules.utils.rapid_db_connect`, which imports psycopg2 at module
    scope. This module does not otherwise need a driver — it is handed a
    connection — and importing one at the top would make the consumer
    unimportable anywhere psycopg2 is absent, which is most of the test suite
    and every reader trying to run it on a laptop. So the import happens on
    first use.

    The fallback is not a convenience: it is the same contract written out, so
    a caller passing a plain connection object gets commit-on-success and
    rollback-and-re-raise-on-error whether or not the driver module could be
    loaded. A silently weaker boundary here would be the defect this fix exists
    to close, arriving by a different door.
    """
    try:
        from database.modules.utils.rapid_db_connect import transaction
    except ImportError:
        cur = conn.cursor()
        try:
            yield cur
        except Exception:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                logger.exception("rollback failed; the original error follows")
            raise
        else:
            conn.commit()
        finally:
            cur.close()
        return

    with transaction(conn) as cur:
        yield cur


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
    """Attempts the reconciler has closed and published a closure record for.

    The `rollback()` here is still right, and is worth saying why now that the
    registration below runs inside an explicit transaction. This read is the
    only thing that has touched the connection at this point, it wrote nothing,
    and psycopg2 has nonetheless opened a transaction to do it. Ending it means
    each attempt's `transaction(conn)` block starts from no transaction at all,
    so the unit of work it commits contains that attempt's writes and nothing
    else — not a snapshot taken minutes earlier when the candidate list was
    read. Leaving it open would also hold a snapshot for the whole pass, which
    on a pass over many attempts is a long idle-in-transaction.
    """
    with conn.cursor() as cur:
        cur.execute(_CANDIDATE_SQL, (list(states),))
        names = [description[0] for description in cur.description]
        rows = [dict(zip(names, row)) for row in cur.fetchall()]
    conn.rollback()  # read-only; do not hold a transaction open
    return rows


#: Append-once by EVENT IDENTITY, in one statement. The document is built
#: server-side from the existing value so a replay cannot double an entry:
#: an event whose key is already present is not appended again, and the
#: observed-sequence high-water mark only ever advances
#: (design/catalog.md § Promotion, "The registration outcome").
_RECORD_OUTCOME_SQL = (
    "UPDATE attempts"
    "   SET registration_outcome = jsonb_build_object("
    "         'promotions',"
    "         CASE WHEN COALESCE(registration_outcome->'promotions', '[]'::jsonb)"
    "                   @> jsonb_build_array(%(event)s::jsonb)"
    "              THEN registration_outcome->'promotions'"
    "              ELSE COALESCE(registration_outcome->'promotions', '[]'::jsonb)"
    "                   || jsonb_build_array(%(event)s::jsonb) END,"
    "         'observed_sequence',"
    "         GREATEST("
    "           COALESCE((registration_outcome->>'observed_sequence')::int, 0),"
    "           %(sequence)s)"
    "       )"
    " WHERE attempt_id = %(attempt_id)s"
)


def record_registration_outcome(attempt_id, outcome, record_sequence,
                                cursor=None):
    """Append this registration's account to the attempt's outcome document.

    Migration 024 created `registration_outcome` and deliberately left the
    writer owed ("column first, writer after"); this is that writer. The
    document is an OBJECT — the CHECK constraint requires one — carrying
    the promotion events and the observed-sequence high-water mark.

    Append-once keyed by event identity, so the replay that populated
    `diffimages` cannot also grow this document on every pass. A body that
    returned nothing structured (the reference path today) writes no event
    and only advances the high-water mark.
    """
    if cursor is None or not isinstance(outcome, dict):
        return None
    sequence = int(record_sequence if record_sequence is not None else 1)
    event = {"type": "promotion",
             "pid": outcome.get("pid"),
             "version": outcome.get("version"),
             "product": outcome.get("product"),
             "role_resolved_from": outcome.get("role_resolved_from"),
             "sequence": sequence}
    cursor.execute(_RECORD_OUTCOME_SQL,
                   {"event": json.dumps(event, sort_keys=True),
                    "sequence": sequence, "attempt_id": attempt_id})
    return event


def mark_registered(conn, attempt_id, record_sequence, now=None, cursor=None):
    """Record that this attempt was registered at `record_sequence`.

    The watermark write (review finding #5). Guarded so a replay or a
    concurrent pass cannot move the watermark backwards — the sequence only
    ever advances, which is what makes "reprocesses on a later supersession"
    safe to run repeatedly. That CAS predicate is untouched by the change
    below; it is the reason two passes racing on the same attempt cannot
    disagree about what has been registered.

    **THIS NO LONGER COMMITS** (round-3 finding #8). It used to end with
    `conn.commit()`, which made the watermark its own transaction — separate
    from the transaction the product rows were written in, on a separate
    connection. The two could not be atomic by construction, so the comment in
    `register_batch` promising that "a failure leaves the attempt a candidate
    rather than marking work that did not happen" was false in the one
    direction that matters: the product rows were already durable, and only the
    watermark was lost. The caller now owns the boundary and commits both
    together, so the honest version of that promise holds.

    `cursor` lets the caller pass the cursor its `transaction(conn)` block
    already yielded, rather than opening a second one on the same connection.
    Either works — cursors on one connection share its transaction — but
    reusing it keeps the whole unit of work visibly on one cursor.
    """
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    sequence = int(record_sequence if record_sequence is not None else 1)
    if cursor is not None:
        cursor.execute(_MARK_REGISTERED_SQL,
                       (moment, sequence, attempt_id, sequence))
        return sequence
    with conn.cursor() as cur:
        cur.execute(_MARK_REGISTERED_SQL,
                    (moment, sequence, attempt_id, sequence))
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

        # ALL OR NOTHING, PER ATTEMPT (round-3 finding #8).
        #
        # These two statements used to be two transactions, and worse than
        # that, two transactions on two CONNECTIONS. `registrar_for` handed the
        # product bodies `rapid_db.RAPIDDB` as a factory, and that class opens
        # its own psycopg2 connection and commits after every single call — so
        # by the time `register()` returned, the reference image or difference
        # image and its catalogues were already durable. The watermark write
        # that followed was on this connection, and committed separately. A
        # crash, a lost connection or a failing watermark UPDATE in between
        # left the product rows written and the attempt still a candidate, so
        # the next pass registered the same products again.
        #
        # The comment that used to sit here said a failure "leaves the attempt
        # a candidate rather than marking work that did not happen". The
        # attempt did stay a candidate — but the work HAD happened, and saying
        # otherwise is what let the duplicate registration look impossible.
        #
        # `transaction(conn)` commits on the way out and rolls back and
        # re-raises on the way through, and the registrar now writes on THIS
        # connection (see `products.registrar` and `job.registrar_for`), so
        # everything inside the block is one unit: the product rows and the
        # watermark commit together, or neither does and the attempt is a
        # candidate with nothing written.
        #
        # The `except` is still per-attempt rather than around the whole pass.
        # One attempt whose record is incomplete must not roll back the
        # registrations of the attempts before it — those are finished units of
        # work, already committed by their own blocks.
        try:
            with _transaction(conn) as cur:
                outcome = register(row, verdict)
                # THE REGISTRATION OUTCOME, inside the same envelope as the
                # product rows and the watermark (migration 024 left the
                # column with its write site owed: "column first, writer
                # after"). It has to be here and not after the block —
                # an account of a commit that did not happen is worse than
                # no account, and this way the three land or none do.
                record_registration_outcome(
                    verdict.attempt_id, outcome,
                    row.get("terminal_record_sequence"), cursor=cur)
                mark_registered(conn, verdict.attempt_id,
                                row.get("terminal_record_sequence"),
                                cursor=cur)
        except Exception:  # noqa: BLE001 - counted, not swallowed
            run.failed += 1
            logger.exception(
                "registering attempt %s failed; its transaction was rolled "
                "back, so no product rows and no watermark were written and "
                "it remains a candidate", verdict.attempt_id)
        else:
            run.registered += 1
            logger.info("attempt %s registered: %s",
                        verdict.attempt_id, verdict.reason)

    logger.info("registration pass: %s", run.as_dict())
    return run
