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

THE WATERMARK SPLIT (ruling R1, migration 075). The paragraph above described
`registered_record_sequence` alone, and that was the eternal-candidates
defect: a terminal SKIP verdict (products superseded, none, an effect
deferred or unconfirmed-and-parked, or an application failure) never
advanced ANY watermark, because the only one that
existed was scoped to acceptance. So a permanently-skipped attempt — one whose
disposition will never become REGISTER — stayed a candidate forever, reread
and redecided on every single pass, indistinguishable in the query from an
attempt genuinely awaiting its first decision. Migration 075 adds `attempts.
consumed_record_sequence` as the answer to a DIFFERENT question from
`registered_record_sequence`: not "were this sequence's products accepted"
but "has registration already reached a terminal verdict about this sequence
at all". `_CANDIDATE_SQL` below is keyed on the consumed watermark, not the
registered one, and BOTH REGISTER and a terminal SKIP advance it — REGISTER
alongside `registered_at`/`registered_record_sequence` in the same statement
(acceptance semantics are UNCHANGED: only REGISTER ever sets those two), a
terminal SKIP alone. A DEFER never advances either watermark, because a DEFER
is not yet a decision — see `observability.registration.RegistrationDecision`.

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
import time

from observability.attempts import LifecycleState, ProductDisposition
from observability.registration import RegistrationDecision, decide
from pipeline.gc import fence as gc_fence
from pipeline.intent.retry_policy import (
    EFFECT_RETRY_EXHAUSTED_REASON, PARK_BLOCKED, RETRY_READY,
    disposition_for_unconfirmed_effect)
from pipeline.intent.writer import (
    BLOCKED, COMPLETE, READY, SUBMITTED, WRITER_RECONCILER, WorkUnitNotFound,
    WorkUnitWriter)
from pipeline.registration.products import (
    MissingRecordFact, read_record, RecordValidationRejected,
    RegistrationFailed)
from pipeline.runtime import lock_order
from pipeline.runtime.lock_order import REGISTRAR_LEASE_NAMESPACE

logger = logging.getLogger("rapid.registration")

#: The registrar's own lock namespace for `pg_advisory_xact_lock`'s
#: two-argument form — distinct from the reconciler's `LEASE_NAMESPACE`
#: (`pipeline.reconciler.lease`, 0x5732 'W6') so a registrar lease and a
#: reconciler lease on the same attempt id never collide semantically; they
#: guard different concerns; the primitive shape is shared, the namespace is
#: not. 0x5234 is 'R4' — this is integration ruling 4's lease.
#:
#: THE LOCK ORDER (conformance rule 9, brief C3): this lease is LEVEL 1, per
#: attempt, and the work-unit lock (`pipeline.intent.lock`, 'WU') is LEVEL 2,
#: always taken UNDERNEATH this one — never the reverse. The full two-level
#: order is written down ONCE now, in `pipeline.runtime.lock_order`
#: (campaign ruling C3), rather than repeated here and at
#: `pipeline.reconciler.lease.LEASE_NAMESPACE`. Acceptance is one of the
#: dispositions rule 9 names, so a registration that transitions a work unit
#: does so under both: R4 held from the first statement of the per-attempt
#: transaction, WU taken inside `transition_unit` before its CAS.
#:
#: CANONICAL VALUE NOW LIVES IN `pipeline.runtime.lock_order`; this name is
#: kept, and re-exported, so no importer of this module needs to change.
ATTEMPT_LEASE_NAMESPACE = REGISTRAR_LEASE_NAMESPACE

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
    # THE CONSUMED WATERMARK (ruling R1, migration 075). Distinct from the
    # pair above: `registered_record_sequence` keeps its ACCEPTANCE meaning
    # unchanged — "this sequence's products are durably registered" — and is
    # ONLY ever set by the REGISTER path. `consumed_record_sequence` answers
    # a different question, "has registration already made a terminal
    # decision about this sequence at all", and is set by BOTH REGISTER and
    # a terminal SKIP verdict. See the module docstring's "THE WATERMARK
    # SPLIT" for why one column serving both questions was the eternal-
    # candidates defect this migration exists to close.
    "consumed_record_sequence",
    # The intent-layer FK (finding 7 repair). NULL on every pre-intent-layer
    # row and on any attempt whose definition-FK guard held it back at
    # submission time (see `pipeline.seams._attach_work_unit`) — mirroring
    # `pipeline.reconciler.service._OPEN_COLUMNS`'s own comment on this same
    # column verbatim, because it is the identical "absent means absence"
    # guard one layer up: `register_batch` completes the work unit here
    # rather than the reconciler completing it on a bare `rapid_outcome`
    # read, and a row with no work unit has nothing for that completion to
    # act on.
    "work_unit_id",
)

# THE CONSUMED WATERMARK (review finding #5; split from the registered
# watermark by ruling R1 / migration 075 — see the module docstring's "THE
# WATERMARK SPLIT").
#
# Without the last predicate this selected every reconciled attempt on every
# pass, so a registration job re-registered the same attempts forever. The
# watermark is the SEQUENCE consumed rather than a boolean, which is what
# makes the design's "reprocesses on a later supersession" work: an attempt
# consumed at sequence 1 whose reconciler later publishes sequence 2 has a
# record sequence ahead of its watermark and becomes a candidate again.
#: The `ORDER BY` is a separate constant from the `WHERE`-and-earlier text
#: below it (`_CANDIDATE_WHERE_SQL`) precisely so a scoping predicate can be
#: inserted BEFORE it — `candidates()` appends into the WHERE clause, and
#: appending after `ORDER BY` would be invalid SQL. `_CANDIDATE_SQL` itself
#: stays the exact, single concatenation every existing test asserts
#: against (`_CANDIDATE_SQL` is also the literal string `candidates()`
#: issues verbatim for an unscoped call — see that function's docstring).
_CANDIDATE_WHERE_SQL = (
    "SELECT " + ", ".join(_COLUMNS) +
    " FROM attempts"
    " WHERE lifecycle_state = ANY(%s)"
    "   AND terminal_record_sequence >= 1"
    "   AND (consumed_record_sequence IS NULL"
    "        OR consumed_record_sequence < terminal_record_sequence)"
)
_CANDIDATE_ORDER_SQL = " ORDER BY attempt_id"
_CANDIDATE_SQL = _CANDIDATE_WHERE_SQL + _CANDIDATE_ORDER_SQL

#: SCOPING PREDICATES (scoped-registration entrypoint, `pipeline.registration.
#: scoped`). Appended to `_CANDIDATE_SQL` only when a scope is actually
#: passed to `candidates()` — see that function's docstring for why an
#: unscoped call must stay byte-for-byte the SQL above, unchanged. `run_id`
#: uses `LIKE '<prefix>%'` rather than `=` because `pipeline.seams.
#: submit_gathered` splits a batch too large for one manifest across several
#: run_ids by appending `-<index>` (`f"{run_id}-{index}"`), so a caller
#: scoping to one submitted run must match every suffixed child, not just an
#: exact run_id that may never appear alone. `attempt_ids` is the other,
#: orthogonal way to bound a scope — an explicit list rather than a run's
#: prefix — for a caller that already knows exactly which attempts it means.
_RUN_ID_PREFIX_SQL = "run_id LIKE %s"
_ATTEMPT_IDS_SQL = "attempt_id = ANY(%s)"


def _escape_like(prefix):
    """Escape `%`/`_`/`\\` in a LIKE prefix so it matches literally.

    `run_id_prefix` is caller-supplied text, not a wildcard pattern — a
    prefix containing a literal `%` or `_` (neither is disallowed in a
    run_id) must not silently become a wildcard in the WHERE clause.
    """
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

#: REGISTER: both watermarks advance together, in one statement — acceptance
#: (`registered_at`/`registered_record_sequence`) and consumption
#: (`consumed_record_sequence`) are the same fact for a REGISTER verdict, so
#: there is no window in which one is ahead of the other for this path.
_MARK_REGISTERED_SQL = (
    "UPDATE attempts"
    "   SET registered_at = %s, registered_record_sequence = %s,"
    "       consumed_record_sequence = %s"
    " WHERE attempt_id = %s"
    "   AND (registered_record_sequence IS NULL"
    "        OR registered_record_sequence < %s)"
)

#: A terminal SKIP: consumption advances ALONE. `registered_at` and
#: `registered_record_sequence` are left exactly as they are — a SKIP is
#: definitionally never an accepted result, so writing either would claim an
#: acceptance that did not happen, corrupting `pipeline.reconciler.service.
#: _work_unit_series`'s "has any sibling been accepted" read.
_MARK_CONSUMED_SQL = (
    "UPDATE attempts"
    "   SET consumed_record_sequence = %s"
    " WHERE attempt_id = %s"
    "   AND (consumed_record_sequence IS NULL"
    "        OR consumed_record_sequence < %s)"
)

#: The post-lock re-read (integration ruling 4). Only the watermark columns —
#: the candidate query's own predicate is `consumed_record_sequence IS NULL
#: OR consumed_record_sequence < terminal_record_sequence`, so re-checking
#: exactly that pair under the lease is what tells this attempt's turn "another
#: writer already consumed this sequence while I waited" from "I am still
#: the one to do this". Reads the CONSUMED watermark (ruling R1), not the
#: registered one — this re-read now guards SKIP's own re-entry into the
#: transactional block exactly as it already guarded REGISTER's, which is the
#: point of moving SKIP under the same lease (see `register_batch`).
_REREAD_WATERMARK_SQL = (
    "SELECT consumed_record_sequence, terminal_record_sequence"
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
    lock_order.acquire_blocking(cursor, ATTEMPT_LEASE_NAMESPACE, attempt_id)


def _reread_watermark(cursor, attempt_id):
    """The post-lock watermark re-read (integration ruling 4).

    Returns (consumed_record_sequence, terminal_record_sequence), or None if
    the attempt row is gone. Called immediately after the lease is acquired
    and before any registration OR skip work (ruling R1 moved SKIP under this
    same guard — see `register_batch`): the candidate list was built by an
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


def candidates(conn, states=RECONCILED_STATES, run_id_prefix=None,
              attempt_ids=None):
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

    **SCOPING IS OPT-IN AND ADDITIVE** (`pipeline.registration.scoped`, the
    standalone bounded-registration entrypoint). `run_id_prefix` and
    `attempt_ids` are both `None` by every existing caller —
    `pipeline.entrypoints.job.dispatch_registration` (the production
    registration job route) and `pipeline.operator.registration.run_pass`
    (the operator's own pass) — and when neither is passed this function
    issues the EXACT SAME SQL text and the EXACT SAME parameters it always
    has: `_CANDIDATE_SQL` unmodified, `(list(states),)`. That is deliberate,
    not incidental: this is a widely-called function and the unscoped path
    is still the only production path an operator or the registration job
    route reaches, so it must not change shape for callers that never asked
    for a scope. Passing either argument appends the matching predicate (or
    both, ANDed) to the query text and its own parameter to the tuple —
    scoping narrows an already-reconciled candidate set, it does not widen
    or replace the reconciled-state gate above it.

    `run_id_prefix` matches `run_id LIKE '<prefix>%'` (escaped so the
    prefix's own `%`/`_` are literal — see `_escape_like`), because
    `pipeline.seams.submit_gathered` splits a batch too large for one
    manifest into `-0`/`-1`/... suffixed child run_ids
    (`f"{run_id}-{index}"`); an exact match on the bare run_id a caller
    submitted could miss every suffixed child. `attempt_ids` is a plain
    `= ANY(%s)` over an explicit, caller-known sequence — the other,
    orthogonal way to bound a scope, usable together with or instead of a
    prefix.
    """
    if run_id_prefix is None and attempt_ids is None:
        # BYTE-FOR-BYTE THE UNSCOPED PATH (see the docstring's "SCOPING IS
        # OPT-IN AND ADDITIVE"): the exact SQL text and parameter tuple this
        # function has always issued, untouched by the scoping machinery
        # below.
        sql = _CANDIDATE_SQL
        params = [list(states)]
    else:
        sql = _CANDIDATE_WHERE_SQL
        params = [list(states)]
        if run_id_prefix is not None:
            sql += " AND " + _RUN_ID_PREFIX_SQL
            params.append(_escape_like(run_id_prefix) + "%")
        if attempt_ids is not None:
            sql += " AND " + _ATTEMPT_IDS_SQL
            params.append(list(attempt_ids))
        sql += _CANDIDATE_ORDER_SQL

    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
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


#: The `l2_available` milestone's find-before-write guard. `milestones` carries
#: no unique constraint on (milestone_name, exposure_id, sca) — migration 011
#: defines only the scope CHECK — so a re-registration under a later
#: supersession would otherwise append a second row for a unit that reached the
#: milestone once. Scoped to the unit, not the attempt, exactly as
#: `AttemptWriter.record_milestone` documents ("a unit may reach a milestone
#: through more than one attempt across retries").
_L2_AVAILABLE_EXISTS_SQL = (
    "SELECT 1 FROM milestones"
    " WHERE milestone_name = 'l2_available'"
    "   AND exposure_id = %s AND sca = %s LIMIT 1"
)

_RECORD_L2_AVAILABLE_SQL = (
    "INSERT INTO milestones"
    "  (milestone_name, exposure_id, sca, reached_at, producing_attempt_id)"
    " VALUES ('l2_available', %s, %s, %s, %s)"
)

#: The milestone's timestamp source, read on the acceptance connection.
#:
#: The science stage reached this through `RAPIDDB.get_l2file_created(rid)`,
#: keyed by the unit's `rid` fact. An attempt row carries no `rid`, so the
#: acceptance side keys by `(expid, sca)` — the identity `L2Files` itself is
#: keyed on (migration 006: `expid integer NOT NULL, sca smallint NOT NULL`)
#: and the identity the attempt row already carries. `vbest` picks the current
#: version where a unit has been re-delivered, matching the exclusion
#: `submission.gathering.coadd_input_rows` applies to the same table
#: ("`vbest == 0` is a superseded version"); `created` is ascending-stable per
#: row, so the ORDER BY only decides between co-current versions.
_L2_CREATED_SQL = (
    "SELECT created FROM L2Files"
    " WHERE expid = %s AND sca = %s AND vbest <> 0"
    " ORDER BY created LIMIT 1"
)


def record_l2_available(attempt_id, row, cursor=None):
    """The `l2_available` milestone, written in the acceptance transaction.

    **WHY IT MOVED HERE** (conformance rule 8, brief C2). This was
    `pipeline.stages.science._record_l2_available_milestone`, called from
    `download_inputs` — the first science stage, in the same function that
    then downloads the science image and both PSFs. It called
    `context.require_connection()` for the attempt-long BORROWED connection,
    read, wrote through an `AttemptWriter`, and committed, all inside a
    product-producing sequence. `pipeline.stages.context` states the rule that
    forbids exactly this: "the post-DB job types produce database state rather
    than S3 products, so they need that connection" — database effects belong
    to post-DB job types, and a science stage is not one. Rule 8 states it
    from the other side: "Pixel/transform workers hold no database connection.
    They upload artifacts and one sealed, checksummed result manifest, then
    exit."

    So the worker now holds no connection on this path at all, and the
    milestone lands on the ACCEPTANCE side, inside `register_batch`'s
    per-attempt transaction — the one that already commits the product rows,
    the registration outcome and the watermark together. That co-commit is the
    acceptance criterion: a crash between registration and the milestone is
    not a window that can be hit, because there is no second commit to crash
    between. It is one transaction or none.

    **THE TIMING SEMANTICS CHANGE, AND NOTHING OBSERVES IT** (brief C2:
    "verify no consumer of `milestones.l2_available` depends on the earlier
    timing; record what you find"). The milestone previously carried
    `L2Files.created` — a proxy for SOC availability, fetched fresh at
    download time — and was written at the start of the science attempt. It
    now carries the same authoritative fact where one is available and is
    written at acceptance. Searched at 820dd40: the only references to
    `l2_available` anywhere in the tree are its writer, the writer's own
    docstring, and `RAPIDDB.get_l2file_created`'s docstring naming itself as
    the source. There is NO READER — no query, no view, no dashboard, no
    latency clock, no test asserting its timestamp. The four latency clocks
    the minimal-viable target names (arrival→admission, admission→transform
    result, acceptance→outbox, outbox→broker acknowledgement) do not read this
    table. So the change of timing is unobserved today, and this docstring is
    the record of that check for whoever wires the first reader.

    **WHY NOT DERIVE IT FROM REGISTRATION STATE INSTEAD** (the brief offers
    that alternative: "or is derived from state registration already writes if
    a separate milestone row is redundant"). The row is kept. Registration
    writes `registered_at` — when RAPID ACCEPTED the result — which answers a
    different question from "when did this unit's L2 input become durably
    known", and the milestone's whole purpose per design/observability.md is
    to carry "the authoritative source-availability timestamp" as the chain's
    first end. Deriving one from the other would silently redefine the
    milestone as an acceptance clock and lose the only fact it exists to hold.

    `reached_at` is `L2Files.created` — the SAME authoritative-source proxy
    the science stage read, reached by `(expid, sca)` rather than by `rid`
    because that is the identity an attempt row carries (see `_L2_CREATED_SQL`)
    — and falls back to the acceptance moment when no L2Files row answers. The
    fallback is explicit rather than skipping the milestone: an accepted unit
    HAS reached the milestone, that fact is worth recording even where the
    proxy timestamp is unavailable, and a NULL `reached_at` is refused by the
    schema anyway. The fallback is logged so a milestone carrying an
    acceptance time rather than a source time is visible as such.
    """
    if cursor is None:
        return False

    exposure_id = row.get("exposure_id")
    sca = row.get("sca")
    if exposure_id is None or sca is None:
        # The milestone is scoped by (exposure, sca) — migration 011's
        # `milestones_scope_check` demands at least one scope column, and a
        # half-scoped row would be unfindable by the find-before-write guard
        # above. A non-science attempt (reference construction, the post-DB
        # chain) legitimately has neither; it has no L2 input to be available.
        return False

    cursor.execute(_L2_AVAILABLE_EXISTS_SQL, (int(exposure_id), int(sca)))
    if cursor.fetchone() is not None:
        return False

    cursor.execute(_L2_CREATED_SQL, (int(exposure_id), int(sca)))
    found = cursor.fetchone()
    reached_at = found[0] if found is not None else None
    if reached_at is None:
        reached_at = datetime.datetime.now(datetime.timezone.utc)
        logger.info(
            "no current L2Files row for %s/%s; the l2_available milestone "
            "carries the acceptance time rather than the source proxy",
            exposure_id, sca)

    cursor.execute(_RECORD_L2_AVAILABLE_SQL,
                   (int(exposure_id), int(sca), reached_at, attempt_id))
    logger.info("l2_available milestone recorded for %s/%s at acceptance "
                "(attempt %s)", exposure_id, sca, attempt_id)
    return True


def _cursor_executor(cursor):
    """A `pipeline.intent.writer.Executor` over a plain DB-API cursor.

    `WorkUnitWriter` takes a bare `execute(sql, params)` callable — the same
    contract `pipeline.reconciler.service._Executor(conn)` provides for the
    reconciler's own writes — so completion can share `register_batch`'s
    already-open cursor rather than opening a second one: this IS the same
    transaction the product rows, the registration outcome and the watermark
    are committing in, which is the whole point (see `_complete_work_unit`).
    """
    def execute(statement, params=None):
        cursor.execute(statement, params)
        if cursor.description is not None:
            return cursor.fetchall()
        return cursor.rowcount

    return execute


def _complete_work_unit(attempt_id, work_unit_id, cursor):
    """The `submitted -> complete` transition, now that the result is
    ACCEPTED (FINDING 7).

    **WHY THIS MOVED HERE FROM THE RECONCILER.** The reconciler used to
    complete a work unit the moment `rapid_outcome == success` — before
    registration had run at all; `pipeline.registration.consumer.
    candidates()`'s own query only selects rows the reconciler has ALREADY
    left terminal (`lifecycle_state IN (terminal_after_start,
    terminal_without_start) AND terminal_record_sequence >= 1`), so
    registration is strictly downstream of that closure. A unit marked
    `complete` on the application's own say-so could then have its
    registration fail or be rejected while sitting `complete`, with nothing
    left able to retry it — `submitted -> complete` is the only edge into
    `complete` the transition graph admits (`pipeline.intent.writer.
    _TRANSITION_GRAPH`), so once wrongly taken there is no way back to
    `submitted` short of the mutation API.

    Rule 4 (`pipeline.intent.retry_policy`'s module docstring) says a unit
    closes "only from an accepted result" — and `_work_unit_series` in the
    reconciler already reads `registered_at`, not `rapid_outcome`, when
    judging whether a SIBLING attempt succeeded, for exactly that reason.
    This function applies the identical standard to the attempt's own
    outcome: this call site is reached only after `mark_registered` has
    already run in THIS SAME transaction (see `register_batch`), so
    `registered_at` and the completion below share one commit — an accepted
    result and the fact of its acceptance land together, or neither lands.

    **WRITER IDENTITY.** `WRITER_RECONCILER` — migration 036's closed
    four-writer vocabulary (`pipeline.intent.writer.WRITERS`) has no fifth
    "registrar" class, and the module's own docstring states writer
    identity names a TRANSITION CLASS, not a call site: "the SAME Python
    function is invoked by more than one transition-class writer in
    practice... the design's exclusivity rule is a CODE discipline over WHO
    passes which value". Completion is the "the reconciler closes" class
    that same docstring names, wherever in the codebase the call happens to
    live now.

    **THE LOCK, AND ITS ORDER.** `transition_unit` takes the work-unit lock
    itself (`pipeline.intent.lock.lock_work_unit`) before its CAS — this
    function does not take it separately, matching the "one choke point"
    discipline `transition_unit`'s own docstring states. The order is R4
    (this module's per-attempt lease, held since `_acquire_attempt_lease`
    at the top of `register_batch`'s transaction) then WU (taken here,
    underneath it) — exactly the order `pipeline.intent.lock`'s module
    docstring requires and names this registration path as an example of
    ("a registration that transitions a work unit does so under both").

    **A CAS MISS IS LOGGED, NOT RAISED.** Mirrors `_close_work_unit`'s own
    posture in the reconciler: a unit not currently `submitted` (finding
    6's own guard left it there because a sibling was still open, or an
    operator already force-transitioned it, or a second registration pass
    reaches an attempt another pass already completed) means another
    writer already resolved it. The registration this call is part of has
    already committed the product rows and the watermark by the time this
    runs; refusing to complete a unit someone else has already dispositioned
    is not a reason to roll all of that back.
    """
    if work_unit_id is None:
        # Every pre-intent-layer row, and every row whose job type has no
        # loaded workflow_definitions row yet — mirrors
        # `pipeline.reconciler.service._close_work_unit`'s identical guard
        # verbatim: absent means absence, not a sentinel, one layer up.
        return
    work_writer = WorkUnitWriter(_cursor_executor(cursor))
    try:
        work_writer.transition_unit(
            work_unit_id, SUBMITTED, COMPLETE, writer=WRITER_RECONCILER,
            detail={"deciding_attempt_id": attempt_id,
                    "disposition": "close_complete"})
    except WorkUnitNotFound:
        logger.info(
            "work unit %s (attempt %s) was not in 'submitted' when "
            "registration accepted its result; another writer already "
            "resolved it, so this registration's own product writes and "
            "watermark still commit without forcing the work unit",
            work_unit_id, attempt_id)


#: The three effect dispositions, as plain strings — `_EFFECT_ATTEMPT_COUNT_
#: SQL`'s `= ANY(%s)` argument and `_apply_skip_disposition`'s own dispatch
#: both need the bare values, not the enum members (ruling R1).
_EFFECT_DISPOSITIONS = (
    ProductDisposition.EFFECT_CONFIRMED.value,
    ProductDisposition.EFFECT_UNCONFIRMED.value,
    ProductDisposition.EFFECT_DEFERRED.value,
)

_EFFECT_ATTEMPT_COUNT_SQL = (
    "SELECT count(*) FROM attempts"
    " WHERE work_unit_id = %s AND product_disposition = ANY(%s)"
)


def _effect_attempt_count(work_unit_id, cursor):
    """How many of this unit's attempts have already closed with an
    `effect_*` disposition (ruling R1).

    The series count `pipeline.intent.retry_policy.disposition_for_
    unconfirmed_effect` needs for its own ceiling — this module's own query,
    distinct from the reconciler's `_work_unit_series`: that method answers
    ATTEMPT-level questions (has a sibling been accepted, is one still open)
    the reconciler's closure policy needs, on the reconciler's own connection
    and transaction; this answers a narrower question registration alone
    needs (how many effect attempts has THIS unit absorbed) on the
    registration transaction, under the R4 lease already held. Counting
    INCLUDES this attempt's own row — unlike `_work_unit_series`'s `exclude_
    attempt_id`, this attempt's row has already been written by the time
    this is called (`mark_terminal_after_start`/`mark_application_closed` ran
    before registration ever saw it), so it is already part of the series
    being measured, not a row still being decided.
    """
    cursor.execute(_EFFECT_ATTEMPT_COUNT_SQL,
                   (work_unit_id, list(_EFFECT_DISPOSITIONS)))
    row = cursor.fetchone()
    return int(row[0]) if row is not None else 0


def _apply_skip_disposition(attempt_id, work_unit_id, disposition,
                            rapid_outcome, cursor):
    """The unit-state half of a terminal SKIP verdict, per ruling R1's table.

    Called under the SAME lease and the SAME per-attempt transaction as a
    REGISTER verdict's `_complete_work_unit` — see `register_batch`, which
    moved SKIP under that guard specifically so this could run there rather
    than racing a stale terminal sequence. `disposition` and `rapid_outcome`
    are the row's own columns, read before the lease (candidates() is
    unlocked) but re-validated by the caller's own post-lock reread of the
    watermark pair before this is reached — this function trusts them for
    the CHOICE OF BRANCH, not for the CAS itself, which `transition_unit`
    performs fresh.

    Every branch below CONSUMES (the caller's `mark_consumed` call, made
    unconditionally regardless of which branch fires) — this function's only
    job is the unit's own state, which three of its six cases leave
    untouched at `submitted`.

        confirmed   -> complete (the effect landed; same standard
                       `_complete_work_unit` applies to a REGISTER verdict)
        unconfirmed -> retry policy: ready under the ceiling, park over it
        deferred    -> submitted, untouched (a live owner is authoritative)
        superseded  -> submitted, untouched (the superseding attempt settles
                       the unit under its own terminal record)
        none        -> submitted, untouched, and LOGGED — at ANOMALY
                       severity for a product-producing route (a route that
                       mints object keys and succeeded with nothing to
                       register is unexpected), at ordinary INFO for
                       `registration` (the one live job type this branch
                       reaches that legitimately closes success+none: it
                       publishes no products by design, see below)
        (any failure outcome) -> submitted, untouched (the reconciler's own
                       retry policy, `disposition_for_terminal_attempt`,
                       already owns this unit's disposition for a failed
                       attempt; registration adds nothing)

    `withheld` is REMOVED (was: complete, a deliberate accepted
    non-publication). No stage or entrypoint code path has ever produced it
    — `pipeline.entrypoints.job._execute` derives disposition from either the
    effect-outcome mapping (effect-class job types) or `PUBLISHED if
    published_products else NONE` (everything else), and neither can yield
    `withheld`. It was a dead branch this function could never reach; the
    schema-level enum member and completion-trigger predicate that still
    accept the string are a separate, schema-side removal (migration 084,
    out of this module's scope).
    """
    if work_unit_id is None:
        # Mirrors `_complete_work_unit`'s identical guard: absent means
        # absence, one layer up, for every pre-intent-layer or
        # undefined-workflow row.
        return

    work_writer = WorkUnitWriter(_cursor_executor(cursor))

    if disposition == ProductDisposition.EFFECT_CONFIRMED.value:
        _transition_or_log(work_writer, work_unit_id, attempt_id, COMPLETE,
                           disposition="close_complete_effect")
        return

    if disposition == ProductDisposition.EFFECT_UNCONFIRMED.value:
        count = _effect_attempt_count(work_unit_id, cursor)
        verdict = disposition_for_unconfirmed_effect(
            effect_attempt_count=count)
        if verdict == RETRY_READY:
            _transition_or_log(work_writer, work_unit_id, attempt_id, READY,
                               disposition="retry_ready_unconfirmed_effect")
        else:
            assert verdict == PARK_BLOCKED
            _transition_or_log(
                work_writer, work_unit_id, attempt_id, BLOCKED,
                disposition="park_blocked_unconfirmed_effect",
                blocked_reason=EFFECT_RETRY_EXHAUSTED_REASON)
        return

    if disposition == ProductDisposition.EFFECT_DEFERRED.value:
        # A live owner (a concurrent or later claimant) is authoritative for
        # this unit's effect. Left `submitted`, untouched — that other
        # attempt's own terminal verdict, when it lands, is what disposes
        # the unit; this attempt has nothing further to say about it.
        logger.info(
            "attempt %s: effect deferred to a live owner; work unit %s left "
            "submitted", attempt_id, work_unit_id)
        return

    if disposition == ProductDisposition.SUPERSEDED.value:
        # The superseding attempt's own terminal record is what settles this
        # unit — that attempt (at a higher terminal_record_sequence) is
        # either already a candidate or will become one, and its own SKIP or
        # REGISTER verdict is authoritative. This attempt only consumes.
        logger.info(
            "attempt %s: products superseded; work unit %s left submitted "
            "for the superseding attempt to settle", attempt_id,
            work_unit_id)
        return

    if disposition == ProductDisposition.NONE.value:
        if rapid_outcome == "success":
            # SUCCESS + NONE IS NO LONGER EXCLUSIVELY A PRODUCT-PRODUCING
            # ANOMALY (2026-08-14 ruling extended effect-class to all seven
            # non-product-producing job types, not just alert production).
            # The old message here asserted "on a product-producing route"
            # unconditionally, which was already wrong for `registration` —
            # `observability.registration.is_registrable`'s own docstring
            # names it as a route that legitimately "closes `none`, which
            # `decide` skips", by design, every time it runs. That was true
            # before this wave too; this wave's ruling is what makes it
            # worth fixing HERE, because after this wave the claim is wrong
            # for a much larger set of routes than before: `submission.
            # subjects.is_product_producing` is False for all seven
            # non-product-producing job types, and after task 1 every one
            # of them reports its outcome through the EFFECT_CONFIRMED/
            # EFFECT_UNCONFIRMED/EFFECT_DEFERRED branches above — job.
            # _execute's fail-closed guard refuses to close success+none for
            # any of them — so a genuine product-producing anomaly (science
            # or reference-image losing its output silently) is now the
            # ONLY science-pipeline case that can reach here on success,
            # `registration` being the one routed job type that reaches it
            # by design rather than by defect.
            #
            # This function has no cheap way to tell the two apart per
            # attempt: the `attempts` table carries no `job_type` column
            # (`pipeline.registration.products` documents the identical
            # constraint for the REGISTER path, which reads it from the
            # terminal record body through a store fetch this SKIP path does
            # not make) and `_COLUMNS`/`row` selects none. So the message
            # states what IS verifiable — this outcome is normal for
            # `registration` and an anomaly for anything that mints product
            # keys — rather than asserting a job type this call site cannot
            # see.
            logger.warning(
                "attempt %s succeeded with product_disposition='none'; work "
                "unit %s left submitted. Normal for job type 'registration' "
                "(it registers other attempts' products and mints none of "
                "its own, by design). An ANOMALY for anything else: a job "
                "type that mints object keys (science, reference-image) "
                "succeeding with nothing to register is neither a normal "
                "completion nor a normal failure, and a database-effect job "
                "type (the six post-DB types, alert production) cannot "
                "reach this branch on success at all any more — it would "
                "have closed through EFFECT_CONFIRMED/EFFECT_UNCONFIRMED/"
                "EFFECT_DEFERRED above instead", attempt_id, work_unit_id)
        # A failure/partial outcome with disposition NONE needs no comment
        # here beyond the function docstring's own: the reconciler's retry
        # policy already owns this unit's disposition.
        return

    # Every remaining SKIP reason (`decide`'s "application reported
    # {outcome}" catch-all, and `terminal_without_start`'s "never started")
    # is a FAILURE-shaped verdict at the attempt level. The reconciler's own
    # `disposition_for_terminal_attempt` already ran for this attempt's
    # closure (or, for `terminal_without_start`, there was no application
    # outcome to begin with) — registration adds nothing beyond consuming
    # the sequence so it stops being reread forever.


def _transition_or_log(work_writer, work_unit_id, attempt_id, to_state, *,
                       disposition, blocked_reason=None):
    """`transition_unit(SUBMITTED, to_state, ...)`, CAS-miss logged not
    raised — the identical posture `_complete_work_unit` already documents
    at length for its own single case. Shared here across `_apply_skip_
    disposition`'s several closing branches rather than repeating that
    try/except per branch.
    """
    try:
        work_writer.transition_unit(
            work_unit_id, SUBMITTED, to_state, writer=WRITER_RECONCILER,
            blocked_reason=blocked_reason,
            detail={"deciding_attempt_id": attempt_id,
                    "disposition": disposition})
    except WorkUnitNotFound:
        logger.info(
            "work unit %s (attempt %s) was not in 'submitted' when "
            "registration's SKIP verdict (%s) reached it; another writer "
            "already resolved it, so this attempt's own consumption still "
            "commits without forcing the work unit", work_unit_id,
            attempt_id, disposition)


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

    **BOTH WATERMARKS, ONE STATEMENT** (ruling R1 / migration 075). REGISTER
    is the one verdict where acceptance and consumption are the same fact —
    see the module docstring's "THE WATERMARK SPLIT" — so `_MARK_REGISTERED_
    SQL` sets `registered_at`/`registered_record_sequence` AND `consumed_
    record_sequence` together rather than needing a second call to `mark_
    consumed` after this one. The CAS predicate still guards only the
    registered pair, unchanged: a REGISTER verdict for a sequence already
    accepted is a no-op exactly as before, and the consumed watermark simply
    rides along with whatever this statement decides.

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
                       (moment, sequence, sequence, attempt_id, sequence))
        return sequence
    with conn.cursor() as cur:
        cur.execute(_MARK_REGISTERED_SQL,
                    (moment, sequence, sequence, attempt_id, sequence))
    return sequence


def mark_consumed(conn, attempt_id, record_sequence, cursor=None):
    """Record that this attempt's terminal SKIP verdict CONSUMED `record_
    sequence`, without accepting a result (ruling R1 / migration 075).

    The SKIP-side sibling of `mark_registered`: advances `consumed_record_
    sequence` ALONE, so a permanently-skipped attempt (products superseded,
    none, an effect deferred or unconfirmed-and-parked, or an application
    failure) stops being reread and
    redecided on every pass — the eternal-candidates defect the module
    docstring's "THE WATERMARK SPLIT" names — without ever writing
    `registered_at` or `registered_record_sequence`, which would misstate a
    SKIP as an accepted result.

    Same CAS shape as `mark_registered`'s: the sequence only ever advances,
    so two passes racing on the same attempt cannot disagree about what has
    been consumed. `cursor` is required, not optional with a fallback — this
    is called only from `register_batch`'s per-attempt transaction, under the
    lease, and there is no legitimate caller reaching it any other way (see
    `register_batch`'s SKIP handling for why: ruling R1 moved SKIP under the
    same lock discipline REGISTER already had).
    """
    sequence = int(record_sequence if record_sequence is not None else 1)
    cursor.execute(_MARK_CONSUMED_SQL, (sequence, attempt_id, sequence))
    return sequence


def _parse_s3_uri(uri):
    """Split an `s3://bucket/key` URI. Returns `(bucket, key)` or `None`.

    The one format every product URI this module reads is written in
    (`pipeline.gc.inventory.InventoryObject.uri`: `"s3://%s/%s" %
    (self.bucket, self.key)`), and the only format the fence's
    `(bucket, object_key)` columns are keyed by. A URI that is not this
    shape (a record authored by something that published elsewhere, or a
    malformed entry) returns `None` rather than raising — the fence is a
    defence-in-depth layer over deletion, not the record's own
    completeness check, and `MissingRecordFact` already owns raising on a
    record that cannot support registration at all.
    """
    prefix = "s3://"
    if not uri or not uri.startswith(prefix):
        return None
    rest = uri[len(prefix):]
    bucket, _, key = rest.partition("/")
    if not bucket or not key:
        return None
    return bucket, key


def _bind_fence_keys(record, attempt_id=None):
    """Every `(bucket, object_key)` this attempt's registration will bind.

    Read straight off `record["products"]` — the SAME list `published()`
    indexes for the registrar bodies — rather than re-deriving which
    entries actually get written: registration reads the record and
    nothing else (this module's own governing rule, restated in
    `products.py`'s header), and a fence scope that tried to predict
    which of a reference image's up-to-three URIs or a difference image's
    role-selected URI actually gets bound would be a second, possibly
    disagreeing account of what `published()` already knows. Fencing
    every published URI is a superset of what one attempt binds, never a
    subset — which is the safe direction for a fence: over-fencing costs
    GC a skipped item it re-verifies next pass, under-fencing is the race
    this module exists to close.

    Returns a list, possibly empty (a record with no `products` list, or
    one whose entries are not `s3://` URIs, fences nothing — `register()`
    will raise `MissingRecordFact` on the same absence moments later, so
    this is not silently proceeding unguarded over a real product).
    """
    from pipeline.registration.products import published

    try:
        products = published(record, attempt_id=attempt_id)
    except Exception:                                 # noqa: BLE001
        # `published()` raises `MissingRecordFact` for a record with no
        # `products` list at all. Not this function's failure to report —
        # `register()` will raise the identical exception moments later
        # inside the caller's own try/except, and this fences nothing
        # rather than duplicating that raise here.
        return []
    keys = []
    for entry in products.values():
        parsed = _parse_s3_uri(entry.get("uri"))
        if parsed is not None:
            keys.append(parsed)
    return keys


def _fence_conn_executor(conn):
    """A bare `execute(sql, params)` over `conn`, committing after every call.

    **DELIBERATELY NOT `_cursor_executor` OVER THE PER-ATTEMPT TRANSACTION'S
    OWN CURSOR.** `register_batch`'s per-attempt `with _transaction(conn) as
    cur:` block does not commit until the block exits — that is the whole
    point of round-3 finding #8, "the product rows and the watermark commit
    together, or neither does" — so a fence row written on that cursor would
    stay invisible to every OTHER connection (GC's, or another registration
    pass's) until the exact moment the fence is no longer needed. A fence
    that only becomes visible after it is released fences nothing. This
    executor instead opens its own tiny statement-and-commit on `conn`
    directly, exactly like `Executor._acquire_fence`/`_release_fence`'s own
    calls through `GCPlanRepository._query` do on the GC side — each fence
    operation is its own unit of work, deliberately outside the bind's
    product-row transaction, so the fence row's visibility is not gated on
    that transaction's own commit.
    """
    def execute(statement, params=None):
        with conn.cursor() as cur:
            cur.execute(statement, params)
            if cur.description is not None:
                result = cur.fetchall()
            else:
                result = cur.rowcount
        conn.commit()
        return result
    return execute


@contextlib.contextmanager
def _bind_fence(conn, record, attempt_id,
                lease_seconds=gc_fence.DEFAULT_LEASE_SECONDS):
    """Hold the registration fence over every key this bind will touch.

    **THE BIND CRITICAL SECTION, FENCED — NOT THE WHOLE UPLOAD-TO-BIND
    INTERVAL.** Upload happens in the payload job, long before this
    consumer ever runs; fencing that whole span would hold a lease for
    the length of a Batch job, far past `pipeline.gc.fence`'s
    short-lease design (GC's own `FENCE_LEASE_SECONDS = 120` is sized for
    "one object's re-verify-and-delete", the same order of magnitude this
    scope needs). What is fenced here is exactly the caller's `with`
    body — `register_batch` wraps its per-attempt `with _transaction(conn)
    as cur:` block in this, the transaction-bound call that reads the
    record's URIs and writes them into the operations tables — which is
    the short section `pipeline.gc.execute`'s module docstring names as
    mitigation layer 4.

    Takes `conn`, not a cursor already inside another transaction: see
    `_fence_conn_executor`'s docstring for why the fence's own visibility
    cannot be gated on the bind transaction's commit.

    Uses `gc_fence.HOLDER_REGISTRATION`, the SAME `gc_fences` table and
    the SAME acquire/release SQL shape GC's own `Executor` uses (via
    `pipeline.gc.fence`, extracted from `Executor._acquire_fence` so
    the two dialects cannot drift apart) — a `holder_kind='gc'` fence
    live over any of this bind's keys makes acquisition here fail
    exactly the way a `holder_kind='registration'` fence already makes
    GC's acquisition fail (`test_the_fence_fails_closed_when_it_cannot_be_
    acquired`), because expiry is judged by the ONE shared `WHERE
    gc_fences.expires_at < now()` clause regardless of which holder_kind
    is asking.

    **FAILS CLOSED.** A key whose fence cannot be acquired — because GC
    holds it, live and unexpired — raises `BindFenced` rather than
    proceeding: registering a product whose bytes GC may be mid-delete on
    would bind a URI that stops existing under it, which is worse than
    refusing and letting the next pass retry (this attempt stays a
    candidate; `register_batch`'s outer `except` already treats any
    exception raised before or during the transaction as "counted as
    failed, rolled back, still a candidate" — see that function's comment
    on the `try` around the whole `with _transaction(conn)` block).

    Released on every exit, success or exception — `holder=actor` scopes
    the release so a lease that already expired and was reclaimed by
    someone else is never deleted out from under its new holder (see
    `pipeline.gc.fence.release_fence`'s own docstring).

    `lease_seconds` (default stays `gc_fence.DEFAULT_LEASE_SECONDS`, 120 —
    no behaviour change for an existing caller) is now a parameter rather
    than an unstated default this function silently inherited from
    `acquire_fence`'s own signature: sizing the lease correctly needs to
    know how long the fenced section actually runs in practice, and that
    was previously unmeasured. The elapsed time of the fenced `with` body
    is logged at INFO on every exit, success or exception — instrumentation
    for that sizing decision, not a correctness change.
    """
    keys = _bind_fence_keys(record, attempt_id=attempt_id)
    actor = "registrar:%s" % attempt_id
    execute = _fence_conn_executor(conn)
    acquired = []
    started = time.monotonic()
    try:
        for bucket, object_key in keys:
            if not gc_fence.acquire_fence(
                    execute, bucket=bucket, object_key=object_key,
                    holder=actor, holder_kind=gc_fence.HOLDER_REGISTRATION,
                    lease_seconds=lease_seconds):
                raise BindFenced(bucket, object_key, attempt_id=attempt_id)
            acquired.append((bucket, object_key))
        yield
    finally:
        for bucket, object_key in acquired:
            gc_fence.release_fence(execute, bucket=bucket,
                                   object_key=object_key, holder=actor)
        elapsed = time.monotonic() - started
        logger.info(
            "attempt %s: bind fence held %d key(s) for %.3fs (lease_seconds="
            "%s)", attempt_id, len(acquired), elapsed, lease_seconds)


class BindFenced(RuntimeError):
    """A bind key's fence could not be acquired — GC holds it.

    Raised by `_bind_fence`, OUTSIDE the per-attempt `with
    _transaction(conn) as cur:` block (`register_batch` wraps that block
    in `_bind_fence`, not the reverse) — there is no product-row
    transaction open yet for this attempt when this can be raised, so
    there is nothing to roll back beyond the fence acquisitions
    `_bind_fence`'s own `finally` already releases. `register_batch`'s
    outer `except Exception` still catches it exactly like any other
    registration failure: counted as failed, the attempt stays a
    candidate, and a later pass retries once GC's fence has released or
    expired.
    """

    def __init__(self, bucket, object_key, attempt_id=None):
        self.bucket = bucket
        self.object_key = object_key
        self.attempt_id = attempt_id
        super().__init__(
            f"attempt {attempt_id}: could not acquire the registration "
            f"fence over s3://{bucket}/{object_key} — GC holds it live; "
            f"this attempt's registration is deferred to a later pass")


def register_batch(conn, rows, register=None, run=None, dry_run=False,
                   store=None):
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

    `store` (brief H, GC fence registration half) is the SAME records store
    `pipeline.registration.products.registrar` reads from — this function
    reads the record a SECOND time through it, independently of whatever
    `register` does internally, purely to learn which `(bucket, object_key)`
    pairs this attempt's bind is about to touch, so it can hold the
    registration fence over exactly them for exactly the `register()` call
    (see `_bind_fence`). `store=None` (every caller predating this brief)
    fences nothing and registers exactly as before — an explicit opt-in
    rather than a silent behaviour change for existing call sites, mirroring
    this module's own `execute is None` precedent elsewhere in the codebase.
    A record unreadable or invalid for fencing purposes is not raised here:
    `register()` reads the SAME record moments later through the SAME
    verified path and raises there, which is the one place this module
    already handles that failure as a registration failure.
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
            # SKIP NOW RUNS UNDER THE SAME LEASE AS REGISTER (ruling R1).
            # Before migration 075 a SKIP verdict never touched the database
            # at all — nothing but a counter and a log line — which is
            # exactly why it needed no lock: there was nothing to protect. It
            # now WRITES (the consumed watermark, and for several
            # dispositions the work unit's own state), and an
            # `effect_unconfirmed` verdict specifically must not act on a
            # terminal_record_sequence a newer attempt has already
            # superseded — the identical staleness hazard REGISTER's lease
            # and post-lock reread already guard against. So SKIP takes the
            # SAME `_transaction(conn)` block, the SAME
            # `_acquire_attempt_lease`, and the SAME re-read below, rather
            # than a second, parallel locking discipline.
            try:
                with _transaction(conn) as cur:
                    _acquire_attempt_lease(cur, verdict.attempt_id)
                    watermark = _reread_watermark(cur, verdict.attempt_id)
                    if watermark is None:
                        run.skipped += 1
                        logger.info(
                            "attempt %s vanished between the candidate read "
                            "and its lease; skipping", verdict.attempt_id)
                        continue
                    consumed_sequence, current_terminal_sequence = watermark
                    target_sequence = row.get("terminal_record_sequence")
                    if (consumed_sequence is not None
                            and target_sequence is not None
                            and consumed_sequence >= target_sequence):
                        # Another writer already consumed this exact sequence
                        # (or later) while this one waited on the lease — the
                        # SKIP-side mirror of REGISTER's identical check.
                        run.skipped += 1
                        logger.info(
                            "attempt %s already consumed at sequence %s "
                            "(target was %s) by another writer under the "
                            "same lease; skipping", verdict.attempt_id,
                            consumed_sequence, target_sequence)
                        continue
                    if (current_terminal_sequence is not None
                            and target_sequence is not None
                            and current_terminal_sequence > target_sequence):
                        # Superseded while waiting on the lease — leave it a
                        # candidate at the newer sequence, exactly as
                        # REGISTER's identical check does.
                        run.skipped += 1
                        logger.info(
                            "attempt %s superseded to sequence %s (candidate "
                            "read saw %s) while waiting on the lease; "
                            "leaving it a candidate for a fresh pass",
                            verdict.attempt_id, current_terminal_sequence,
                            target_sequence)
                        continue

                    # THE FINAL UNIT STATE PER TERMINAL VERDICT (ruling R1's
                    # table) — before the watermark write, in the SAME
                    # envelope, matching `_complete_work_unit`'s own
                    # "commits together or not at all" discipline on the
                    # REGISTER side.
                    _apply_skip_disposition(
                        verdict.attempt_id, row.get("work_unit_id"),
                        row.get("product_disposition"),
                        row.get("rapid_outcome"), cur)
                    mark_consumed(conn, verdict.attempt_id, target_sequence,
                                 cursor=cur)
            except Exception:  # noqa: BLE001 - counted, not swallowed
                run.failed += 1
                logger.exception(
                    "consuming attempt %s's SKIP verdict failed; its "
                    "transaction was rolled back, so no watermark and no "
                    "unit transition were written and it remains a "
                    "candidate", verdict.attempt_id)
                continue

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
        #
        # THE BIND FENCE WRAPS THE TRANSACTION, NOT THE REVERSE (brief H,
        # GC fence registration half). Read BEFORE `_transaction(conn)`
        # opens: the fence's own acquire/release are each their own
        # committed statement on `conn` (`_fence_conn_executor`), so they
        # must not be nested inside the bind's uncommitted product-row
        # transaction, where the fence row would stay invisible to every
        # other connection until the exact moment it is released. A
        # `store is None` caller (every one predating this brief) fences
        # nothing and reaches `_transaction(conn)` exactly as before.
        fence_record = None
        if store is not None:
            try:
                fence_record = read_record(store, row)
            except Exception:                          # noqa: BLE001
                # Unreadable or invalid — `register()` reads the SAME
                # record moments later, inside the transaction below, and
                # raises there (`MissingRecordFact`/
                # `RecordValidationRejected`), which this attempt's own
                # except-clauses already handle correctly. Nothing to
                # fence yet is not a fencing failure.
                fence_record = None

        fence_cm = (_bind_fence(conn, fence_record, verdict.attempt_id)
                    if fence_record is not None
                    else contextlib.nullcontext())

        try:
            with fence_cm, _transaction(conn) as cur:
                # THE SINGLE-REGISTRAR LEASE, FIRST (integration ruling 4).
                # Acquired before any read or write this attempt's
                # registration performs, inside the very transaction that
                # will commit the product rows and the watermark — so the
                # lease and the work it guards share one commit/rollback and
                # the lock releases automatically either way. See the module
                # docstring ("THE SINGLE-REGISTRAR PREMISE").
                _acquire_attempt_lease(cur, verdict.attempt_id)

                # THE POST-LOCK RE-READ, on the CONSUMED watermark (ruling
                # R1 — see `_REREAD_WATERMARK_SQL`'s own docstring). REGISTER
                # sets both watermarks together in one statement (`mark_
                # registered`), so re-reading the consumed one here catches
                # the identical race the registered one used to: `candidates(
                # )` read this row unlocked, before this attempt's turn in
                # the loop and before the lease was held; the operator pass
                # and the registration job route both reach this same point,
                # so by now another writer may already have consumed this
                # exact sequence. Re-checking under the lease is what makes
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
                consumed_sequence, current_terminal_sequence = watermark
                target_sequence = row.get("terminal_record_sequence")
                if (consumed_sequence is not None
                        and target_sequence is not None
                        and consumed_sequence >= target_sequence):
                    # Another writer — the other call path, or an earlier
                    # iteration racing on a re-entrant connection — already
                    # consumed this attempt at this sequence or later while
                    # this one waited on the lease. A clean no-op, not a
                    # failure: the work this attempt exists to do is already
                    # done.
                    run.skipped += 1
                    logger.info(
                        "attempt %s already consumed at sequence %s "
                        "(target was %s) by another writer under the same "
                        "lease; skipping", verdict.attempt_id,
                        consumed_sequence, target_sequence)
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

                # `register()` runs INSIDE `fence_cm` (opened above, around
                # this whole `with`) whenever `store` gave us a record to
                # fence — the bind fence has to be held for this call and
                # released only once it returns, but acquired/released as
                # its OWN committed statements on `conn`, never nested
                # inside this uncommitted transaction (see `_bind_fence`'s
                # docstring on why).
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
                # THE `l2_available` MILESTONE, IN THIS TRANSACTION (rule 8,
                # brief C2). It used to be written by the science stage's
                # `download_inputs` on the worker's borrowed connection —
                # inside the product-producing sequence, against that
                # module's own product/database-effect split. It is written
                # here instead, in the envelope that already commits the
                # product rows, the outcome and the watermark, so "a crash
                # between registration and the milestone" is not a state the
                # system can reach: there is one commit, and the milestone is
                # inside it. See `record_l2_available` for the timing change
                # this implies and why nothing observes it.
                record_l2_available(verdict.attempt_id, row, cursor=cur)
                # THE WORK UNIT'S OWN `submitted -> complete` (FINDING 7),
                # LAST and in the SAME envelope as everything above. The
                # reconciler no longer completes a unit on a bare
                # `rapid_outcome == success` read — this is the one and only
                # place that transition happens now, reached only once the
                # product rows, the outcome document and the watermark have
                # all landed in this same uncommitted transaction. See
                # `_complete_work_unit` for the full reasoning.
                _complete_work_unit(verdict.attempt_id,
                                    row.get("work_unit_id"), cur)
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
