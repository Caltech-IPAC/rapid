"""The per-(association_set, lane) association watermark (rule 19, brief F1/F3).

This mirrors the registrar's watermark discipline
(`pipeline/registration/consumer.py:100-220`) deliberately and closely, because
the two solve the same problem at different grains and a second, differently
shaped answer to "how does a watermark advance safely" would be a liability.
The shape, in order:

    lease first          `_acquire_lane_lease` is the FIRST statement of the
                         acceptance transaction (mirroring
                         `_acquire_attempt_lease`).
    re-read under lock   `read_watermark` again once held (mirroring
                         `_reread_watermark`): the claim was made by an
                         earlier UNLOCKED read in the gathering pass, so by
                         the time this unit holds the lane another writer may
                         have moved the frontier.
    CAS advance          `advance` guards monotonicity in the UPDATE's own
                         WHERE clause (mirroring `_MARK_REGISTERED_SQL`),
                         committing in the same transaction as the rows it
                         gates.

What is NOT shared: none of this touches C's code. The registration watermark
is attempt-scoped and answers a different question; this one is set-scoped and
orders association claims. They are separate rows in separate tables with
separate locks.
"""

import logging

logger = logging.getLogger("rapid.association.watermark")

#: The association lane's advisory-lock namespace for `pg_advisory_xact_lock`'s
#: two-argument form. 0x414C is 'AL' — association lane — named in the same
#: convention as the three namespaces it sits below ('R4' the registrar's, 'W6'
#: the reconciler's, 'WU' the work unit's), so a `pg_locks` reading shows which
#: discipline a waiter is in from the classid alone.
#:
#: THE LOCK ORDER (conformance rule 9; extended here by brief F1). The existing
#: order is unchanged and unreordered — this ADDS a level beneath it:
#:
#:     LEVEL 1  R4 0x5234  the registrar's per-attempt lease
#:     LEVEL 1  W6 0x5732  the reconciler's per-attempt lease (distinct from
#:                         R4 so the two never serialize against each other)
#:     LEVEL 2  WU 0x5755  the per-work-unit lock, always taken UNDERNEATH a
#:                         level-1 lease
#:     LEVEL 3  AL 0x414C  this lease, per (association_set, lane), always
#:                         taken UNDERNEATH any of the above
#:
#: AL IS THE LOWEST LEVEL and that is what keeps the order total, hence
#: deadlock-free. A path already holding WU may take AL beneath it; nothing
#: holding AL may then reach upward for WU. The acceptance transaction takes AL
#: first and holds nothing above it, so it never tests that rule in anger.
#:
#: The key is (association_set, lane): two small dense integer spaces, so it
#: collides with no attempt id and no work_unit_id — the same reasoning
#: `pipeline.intent.lock.WORK_UNIT_NAMESPACE` records for itself.
LANE_LEASE_NAMESPACE = 0x414C  # 'AL'

_READ_SQL = (
    "SELECT watermark_proc_date, watermark_field"
    "  FROM association_watermarks"
    " WHERE association_set = %s AND lane = %s"
)

#: The CAS advance. The guard lives in the UPDATE's own WHERE clause — never a
#: read-then-write in the application — exactly like `_MARK_REGISTERED_SQL`
#: (`pipeline/registration/consumer.py:168-171`): a predicate the application
#: evaluates holds until the first concurrent writer; a predicate the database
#: evaluates as part of the UPDATE is the guard.
#:
#: The comparison is ROW-WISE on `(proc_date, field)`, which in PostgreSQL is
#: lexicographic — the canonical claim order itself. So the refusal predicate
#: and the ORDER BY the claim path uses cannot drift apart: they are the same
#: comparison written twice.
_ADVANCE_SQL = (
    "UPDATE association_watermarks"
    "   SET watermark_proc_date = %s, watermark_field = %s, advanced_at = now()"
    " WHERE association_set = %s AND lane = %s"
    "   AND (watermark_proc_date IS NULL"
    "        OR (watermark_proc_date, watermark_field) < (%s, %s))"
)


def acquire_lane_lease(cursor, association_set, lane):
    """Acquire the per-(set, lane) claim lease.

    MUST BE THE FIRST STATEMENT inside the acceptance transaction, for the
    reason `_acquire_attempt_lease` gives: the lease is what makes the
    re-read-then-CAS sequence a critical section, and a statement executed
    before it is a statement executed outside that section.

    `pg_advisory_xact_lock` (no `try_`) blocks until held and releases at
    commit or rollback of the CURRENT transaction — the envelope the stage's
    own `transaction(conn)` already opened for the association load. It opens
    no transaction and commits nothing, so it cannot end the caller's
    transaction underneath the work it guards.

    BLOCKING, NOT TRY-AND-SKIP. At one lane per set there is at most one
    claimable unit in flight, so a waiter here is a duplicate attempt at the
    SAME unit — a retry landing beside its predecessor. Skipping would mean
    silently not writing associations the unit was claimed to write; blocking
    means the second attempt waits, re-reads, and finds the work already done,
    which is the convergence F3 requires.

    The lock key is a pair of small integers, so it is passed through `int()`
    for the same reason the other lock helpers do: a numpy integer or a
    string-shaped id would otherwise reach psycopg2 as an unexpected type.
    """
    cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)",
                   (LANE_LEASE_NAMESPACE, _lane_lock_key(
                       association_set, lane)))


def _lane_lock_key(association_set, lane):
    """Pack (set, lane) into the advisory lock's single 32-bit object id.

    The two-argument `pg_advisory_xact_lock(classid, objid)` gives one integer
    for the key, and the key here is a PAIR. Sixteen bits each: the set in the
    high half, the lane in the low half. Both spaces are small and dense — sets
    are registered by hand, lanes are counted from 0 — so 65535 of each is
    far beyond what the design contemplates, and exceeding either is a bug
    worth raising rather than silently aliasing two lanes onto one lock.
    """
    association_set = int(association_set)
    lane = int(lane)
    if not 0 <= association_set <= 0xFFFF:
        raise ValueError(
            f"association_set {association_set} does not fit the 16-bit half "
            f"of the lane lock key")
    if not 0 <= lane <= 0xFFFF:
        raise ValueError(
            f"lane {lane} does not fit the 16-bit half of the lane lock key")
    # Signed 32-bit: PostgreSQL's advisory lock ids are int4, so a set above
    # 0x7FFF would otherwise overflow into a negative the caller did not write.
    key = (association_set << 16) | lane
    return key - 0x100000000 if key > 0x7FFFFFFF else key


def read_watermark(cursor, association_set, lane):
    """Return this lane's watermark as `(proc_date, field)`, or None.

    `None` means "no row" — a database without DRAFT 049, or a set/lane pair
    that was never registered. `(None, None)` means the row exists and nothing
    has been accepted in it yet, which is the origin of the canonical order
    and a perfectly normal state: every unit is ahead of it.

    Callers must keep those two apart. A missing row is a schema or
    registration fault; an origin watermark is day one.
    """
    cursor.execute(_READ_SQL, (int(association_set), int(lane)))
    row = cursor.fetchone()
    if row is None:
        return None
    proc_date, field = row
    return (None if proc_date is None else str(proc_date),
            None if field is None else int(field))


def advance(cursor, association_set, lane, proc_date, field):
    """Advance this lane's watermark to `(proc_date, field)`. CAS-guarded.

    Returns True when the watermark moved, False when the guard refused.

    A REFUSAL IS A NORMAL OUTCOME, NOT AN ERROR, and both of its causes are
    expected traffic:

      * a stale retry landing late — an acceptance for a unit at or behind the
        frontier, which must leave the watermark exactly where it is (F3);
      * a concurrent duplicate attempt at the same unit — the first advance
        moves the frontier, the second finds itself no longer ahead of it and
        becomes a no-op.

    Both converge on the same final state, which is the whole reason the
    watermark is sequence-shaped rather than boolean.

    The caller decides what a False means for ITS work; this function refuses
    to guess, because "refused because someone else did it" and "refused
    because I am stale" are the same row-count from here and are told apart by
    the re-read the caller already holds.
    """
    proc_date = str(proc_date)
    field = int(field)
    cursor.execute(_ADVANCE_SQL,
                   (proc_date, field, int(association_set), int(lane),
                    proc_date, field))
    return cursor.rowcount == 1


def is_ahead_of(watermark, proc_date, field):
    """Is `(proc_date, field)` strictly ahead of `watermark`?

    The canonical order's comparison, in Python, for the claim path — which
    must decide what to yield BEFORE it has anything to CAS. It is the same
    lexicographic comparison `_ADVANCE_SQL` makes in the database, and the
    contract tier asserts the two agree rather than trusting that they do.

    `watermark` is `(None, None)` at the origin, and everything is ahead of the
    origin.
    """
    if watermark is None:
        raise ValueError(
            "no watermark row for this (set, lane); a missing row is a "
            "registration fault, not an origin watermark")
    wm_date, wm_field = watermark
    if wm_date is None:
        return True
    return (str(proc_date), int(field)) > (str(wm_date), int(wm_field))
