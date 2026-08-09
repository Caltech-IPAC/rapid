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

THE SINGLE-REGISTRAR PREMISE (integration ruling 4, catalog.md § Promotion).
Exactly one registrar runs against a given attempt at a time — stated, not
assumed. `register_batch` acquires a transaction-scoped advisory lease
(`_acquire_attempt_lease`) on the attempt key as the FIRST statement inside
each attempt's `_transaction(conn)` block, before any read or write the
registration itself performs, and re-reads the watermark under the lease
before deciding whether to proceed. The candidate list `candidates()` builds
is read unlocked and may be stale by the time this attempt's turn comes; the
post-lock re-read is what turns "this was a candidate when the pass started"
into "this is still a candidate now". The operator pass and the dormant
`JOB_TYPE_REGISTRATION` job route both call `register_batch` — see
`pipeline.operator.registration.run_pass` and
`pipeline.entrypoints.job.dispatch_registration` — so both share this one
lease acquisition, one lock namespace, and one key derivation; neither can
double-register an attempt the other is mid-transaction on. This is the
registrar's own instance of the lease primitive, distinct from
`pipeline.reconciler.lease` — the reconciler's closure protocol runs the same
primitive shape over its own prefix-listing, lexical-max selection, and that
machinery belongs there, not here (a separate lock namespace keeps the two
from colliding semantically on the same attempt id).

VALIDATION REJECTIONS ARE DURABLE (integration ruling 4). A rejection from the
registrar's own decide/read/verify path — `MissingRecordFact` (a fact the
record should carry is absent) or `RecordValidationRejected` (a fact the
record carries fails verification: checksum mismatch, attempt-identity
mismatch — both raised only by `read_record`) — is not an infrastructure
failure: the record is what it is, and re-running the same registration body
against the same immutable record produces the same verdict. `register_batch`
catches these two classes apart from everything else, commits a
`registration_outcome` rejection event for the attempt in its own
transaction, and does NOT advance the registration watermark — the attempt
stays a genuine candidate (a fixed provenance path or a corrected record
legitimately changes the verdict on a later pass) while the rejection itself
is permanently on record. The append-once event-identity guard already in
`_RECORD_OUTCOME_SQL`'s sibling for rejections is what keeps a record that
rejects the same way every pass from growing the outcome document without
bound — the SAME rejection at the SAME record key/sequence/checksum appends
once, not once per pass.

`RecordValidationRejected` is a NARROWER subclass of the plainer
`RegistrationFailed` that `products._check` raises for a stored-procedure
call reporting `dbh.exit_code >= 64` — deliberately not caught here, because
that code covers catalog.md § Promotion's "Conflicts" (the natural-unique and
partial `vbest`-index violations, both explicitly RETRYABLE, not a permanent
verdict on the record) as well as a genuine database fault, and `exit_code`
alone cannot tell those apart from a validation problem. Catching the broad
`RegistrationFailed` here would misfile a retryable constraint conflict as a
durable rejection. Every exception that is not `MissingRecordFact` or
`RecordValidationRejected` — a bare `RegistrationFailed` from `_check`
included — keeps the prior behavior exactly: counted as a failure, no
outcome event, watermark untouched, retried next pass.
"""

import contextlib
import datetime
import json
import logging

from observability.attempts import LifecycleState
from observability.registration import RegistrationDecision, decide
from pipeline.registration.products import (
    MissingRecordFact, RecordValidationRejected, RegistrationFailed)

logger = logging.getLogger("rapid.registration")

#: The registrar's own lock namespace for `pg_advisory_xact_lock`'s
#: two-argument form — distinct from the reconciler's `LEASE_NAMESPACE`
#: (`pipeline.reconciler.lease`, 0x5732 'W6') so a registrar lease and a
#: reconciler lease on the same attempt id never collide semantically; they
#: guard different concerns; the primitive shape is shared, the namespace is
#: not. 0x5234 is 'R4' — this is integration ruling 4's lease.
ATTEMPT_LEASE_NAMESPACE = 0x5234

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

#: The post-lock re-read (integration ruling 4). Only the watermark columns —
#: the candidate query's own predicate is `registered_record_sequence IS NULL
#: OR registered_record_sequence < terminal_record_sequence`, so re-checking
#: exactly that pair under the lease is what tells this attempt's turn "another
#: writer already registered this sequence while I waited" from "I am still
#: the one to do this".
_REREAD_WATERMARK_SQL = (
    "SELECT registered_record_sequence, terminal_record_sequence"
    " FROM attempts WHERE attempt_id = %s"
)


def _acquire_attempt_lease(cursor, attempt_id):
    """Acquire the registrar's per-attempt lease. Must be the FIRST statement
    inside the attempt's transaction (integration ruling 4).

    `pg_advisory_xact_lock` (no `try_`) blocks until held and releases at
    commit or rollback of the CURRENT transaction — exactly the envelope
    `_transaction(conn)` already opened for the product/outcome/watermark
    write, so no separate lease context manager is needed here the way the
    reconciler's `attempt_lease` needs one: that helper opens and owns its own
    transaction because it is not already inside one; this call is already
    inside `register_batch`'s per-attempt transaction and simply adds a lock
    to it.

    Blocking rather than skipping is the right choice here, unlike the
    reconciler's poll-and-skip: a registration pass is a short, bounded batch
    over a candidate list already in hand, not an unbounded poll loop, and the
    other writer holding this lease is either the operator pass or the
    registration job route doing the SAME kind of bounded work — it will
    release within one attempt's registration, not indefinitely.
    """
    cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)",
                   (ATTEMPT_LEASE_NAMESPACE, int(attempt_id)))


def _reread_watermark(cursor, attempt_id):
    """The post-lock watermark re-read (integration ruling 4).

    Returns (registered_record_sequence, terminal_record_sequence), or None if
    the attempt row is gone. Called immediately after the lease is acquired
    and before any registration work: the candidate list was built by an
    unlocked read at the start of the pass, so by the time this attempt's
    lease is held, another writer — the other call path, or an earlier
    iteration of this same pass on a re-entrant connection — may already have
    advanced the watermark to or past this record's sequence.
    """
    cursor.execute(_REREAD_WATERMARK_SQL, (attempt_id,))
    row = cursor.fetchone()
    return tuple(row) if row is not None else None


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
        #: VALIDATION REJECTIONS (integration ruling 4). Counted apart from
        #: `failed`: a rejection commits a durable outcome event and is a
        #: verdict on the record's own content, not an infrastructure failure
        #: — folding it into `failed` would make `exit_code` treat a
        #: permanently-wrong record the same as a transient database error
        #: worth alerting on.
        self.rejected = 0

    @property
    def exit_code(self):
        """Nonzero when this run could not do its job.

        The four scripts this replaces all hardcoded exit 0, so a registration
        run that registered nothing because every call raised looked exactly
        like a clean run with nothing to do.

        Rejections do not contribute: a rejection is a recorded, durable
        verdict on the attempt's own data, reached without error — the
        opposite of a run that "could not do its job".
        """
        return EXIT_FAILURES if self.failed else EXIT_OK

    def as_dict(self):
        return {
            "registered": self.registered,
            "skipped": self.skipped,
            "deferred": self.deferred,
            "failed": self.failed,
            "rejected": self.rejected,
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


#: The rejection sibling of `_RECORD_OUTCOME_SQL` (integration ruling 4):
#: same append-once-by-identity, GREATEST-high-water-mark idiom, a distinct
#: top-level key so a rejection event can never be mistaken for a promotion
#: (design/catalog.md § Promotion, "The registration outcome" names
#: "validation rejections" as one of the outcome document's own event kinds,
#: distinct from promotions). The identity a rejection appends once under is
#: (type, record key, sequence, checksum, error class): the SAME rejection of
#: the SAME immutable record on a later pass produces byte-identical JSON and
#: is contained, not appended again — which is what keeps a record that
#: rejects the same way every pass from growing this document without bound.
_RECORD_REJECTION_SQL = (
    "UPDATE attempts"
    "   SET registration_outcome = jsonb_build_object("
    "         'validation_rejections',"
    "         CASE WHEN COALESCE(registration_outcome->'validation_rejections',"
    "                            '[]'::jsonb)"
    "                   @> jsonb_build_array(%(event)s::jsonb)"
    "              THEN registration_outcome->'validation_rejections'"
    "              ELSE COALESCE(registration_outcome->'validation_rejections',"
    "                            '[]'::jsonb)"
    "                   || jsonb_build_array(%(event)s::jsonb) END,"
    "         'observed_sequence',"
    "         GREATEST("
    "           COALESCE((registration_outcome->>'observed_sequence')::int, 0),"
    "           %(sequence)s)"
    "       )"
    " WHERE attempt_id = %(attempt_id)s"
)


def record_validation_rejection(attempt_id, error, record_key, record_sequence,
                                record_checksum, cursor=None):
    """Append a validation rejection to the attempt's outcome document.

    Companion to `record_registration_outcome`, for the OTHER event kind the
    document carries (integration ruling 4; catalog.md § Promotion, "A
    validation rejection commits its own registration-outcome entry ...
    without advancing the registration watermark"). Called in the rejection's
    OWN transaction — the one `register_batch` opens for this attempt after
    the lease is held — never in the same commit as a product write, because
    a rejection means no product rows exist for this pass.

    `error` is the caught `MissingRecordFact` or `RecordValidationRejected`;
    its class name is part of the event identity so a checksum-mismatch
    rejection and a missing-field rejection on the same record/sequence are
    two distinct, both-durable events rather than one clobbering the other.
    """
    if cursor is None:
        return None
    sequence = int(record_sequence if record_sequence is not None else 1)
    event = {"type": "rejection",
             "error_class": type(error).__name__,
             "reason": str(error),
             "record_key": record_key,
             "record_checksum": record_checksum,
             "sequence": sequence}
    cursor.execute(_RECORD_REJECTION_SQL,
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
                # THE SINGLE-REGISTRAR LEASE, FIRST (integration ruling 4).
                # Acquired before any read or write this attempt's
                # registration performs, inside the very transaction that
                # will commit the product rows and the watermark — so the
                # lease and the work it guards share one commit/rollback and
                # the lock releases automatically either way. See the module
                # docstring ("THE SINGLE-REGISTRAR PREMISE").
                _acquire_attempt_lease(cur, verdict.attempt_id)

                # THE POST-LOCK RE-READ. `candidates()` read this row
                # unlocked, before this attempt's turn in the loop and before
                # the lease was held; the operator pass and the registration
                # job route both reach this same point, so by now another
                # writer may already have registered this exact sequence.
                # Re-checking the watermark under the lease is what makes
                # that "stale by the time I got here" visible instead of
                # silently re-registering.
                watermark = _reread_watermark(cur, verdict.attempt_id)
                if watermark is None:
                    # The row is gone; nothing to register or skip.
                    run.skipped += 1
                    logger.info(
                        "attempt %s vanished between the candidate read and "
                        "its lease; skipping", verdict.attempt_id)
                    continue
                registered_sequence, current_terminal_sequence = watermark
                target_sequence = row.get("terminal_record_sequence")
                if (registered_sequence is not None
                        and target_sequence is not None
                        and registered_sequence >= target_sequence):
                    # Another writer — the other call path, or an earlier
                    # iteration racing on a re-entrant connection — already
                    # registered this attempt at this sequence or later while
                    # this one waited on the lease. A clean no-op, not a
                    # failure: the work this attempt exists to do is already
                    # done.
                    run.skipped += 1
                    logger.info(
                        "attempt %s already registered at sequence %s "
                        "(target was %s) by another writer under the same "
                        "lease; skipping", verdict.attempt_id,
                        registered_sequence, target_sequence)
                    continue
                # `current_terminal_sequence` re-confirms the candidacy
                # predicate's OTHER half under the lease: a supersession
                # published between the candidate read and this lease raises
                # it further still, and the row remains a candidate at the
                # (now newer) sequence rather than being registered against a
                # target this pass's candidate read is stale about.
                if (current_terminal_sequence is not None
                        and target_sequence is not None
                        and current_terminal_sequence > target_sequence):
                    run.skipped += 1
                    logger.info(
                        "attempt %s superseded to sequence %s (candidate "
                        "read saw %s) while waiting on the lease; leaving it "
                        "a candidate for a fresh pass", verdict.attempt_id,
                        current_terminal_sequence, target_sequence)
                    continue

                try:
                    outcome = register(row, verdict)
                except (MissingRecordFact, RecordValidationRejected) as rejection:
                    # A VALIDATION REJECTION (integration ruling 4): the
                    # registrar's own decide/read/verify path refused this
                    # attempt's record — a fact it needed is absent, or a
                    # fact it has fails verification (checksum mismatch,
                    # attempt-identity mismatch). Durable and NOT an
                    # infrastructure failure: re-running the same body
                    # against the same immutable record reaches the same
                    # verdict. Caught HERE, inside the `with _transaction`
                    # block rather than at the outer `try`, precisely so it
                    # does NOT propagate to the outer `except` (which rolls
                    # back and counts a failure) — a rejection commits its
                    # outcome event in this same transaction and leaves the
                    # watermark untouched, which needs the block to exit
                    # cleanly (`continue`, not re-raise) so `_transaction`
                    # commits rather than rolls back.
                    record_validation_rejection(
                        verdict.attempt_id, rejection,
                        row.get("terminal_record_key"), target_sequence,
                        row.get("terminal_record_checksum"), cursor=cur)
                    run.rejected += 1
                    logger.info(
                        "attempt %s REJECTED registration (durable, "
                        "watermark unchanged): %s", verdict.attempt_id,
                        rejection)
                    continue

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
