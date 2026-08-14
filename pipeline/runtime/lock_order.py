"""The advisory-lock order, in one place (campaign ruling C3).

Three `pg_advisory_xact_lock` namespaces exist in this codebase, at two
levels, and the order between the levels is fixed. Before this module the
order was stated three times — once each in `pipeline.reconciler.lease`,
`pipeline.registration.consumer`, and `pipeline.intent.lock` — in prose that
had to stay in agreement across three files by discipline alone. This module
is the extraction: the three namespace constants and the order they obey now
live here, and the three call sites import them rather than each holding
their own copy. **This is extraction and centralization, not redesign** — no
site's actual acquisition order changes; every acquisition below happens
exactly where and in the same sequence it always did.

## The two levels

    LEVEL 1, per ATTEMPT      W6  0x5732  the reconciler's lease
                              R4  0x5234  the registrar's lease
    LEVEL 2, per WORK UNIT    WU  0x5755  the intent layer's lock

W6 and R4 are siblings and deliberately never serialize against each other:
an attempt's closure sequence (the reconciler) and its registration sequence
(the registrar) are different critical sections over one attempt, and making
them contend would risk deadlock for no invariant that protects. Each is
still LEVEL 1 — the work-unit lock is taken UNDERNEATH whichever of the two
is held, never above:

    attempt lease (W6 or R4)  ->  work-unit lock (WU)   ALWAYS
    work-unit lock            ->  attempt lease         NEVER

That is the only order the existing code can take without inverting a call
structure: both attempt leases are acquired as the FIRST statement of their
transaction, and the work unit is not even known until the attempt row has
been read under that lease — a path that took the work-unit lock first would
have to know the work unit before reading the attempt, which no caller does.

Deadlock freedom follows from that being a TOTAL order over the two levels,
not from any claim about which unit a lock is taken on: a holder of an
attempt lease may block waiting for a work-unit lock, but a holder of a
work-unit lock never waits for an attempt lease, so no cycle can form. A
caller that acquires work-unit locks for TWO units in one transaction (no
current caller does) would need an order among units as well —
`work_unit_id` ascending is the convention to adopt if that ever arises,
stated here so the first such caller inherits an answer rather than
inventing one.

## Why an advisory lock rather than `SELECT ... FOR UPDATE`

`FOR UPDATE` would serialize writers on the row and is a real option. The
advisory lock is chosen for one property `FOR UPDATE` lacks: a decision at
either level reads rows OTHER than the row the lock is nominally "on" (the
attempt series, the retry history, the watermark), so an advisory lock names
the ABSTRACT resource — "this attempt's reconciliation", "this unit's
disposition" — rather than a physical row, and covers the whole decision
including reads that touch no row of that table at all.

## Adopters

`pipeline.reconciler.lease` (W6), `pipeline.registration.consumer` (R4),
`pipeline.intent.lock` (WU) — each imports its namespace constant from here
rather than defining its own, and each keeps its own acquisition helper
(`attempt_lease`, `_acquire_attempt_lease`, `work_unit_lock`/
`lock_work_unit`) exactly as it was, since the three have genuinely
different shapes (a context manager that owns commit/rollback, a bare
blocking acquisition already inside a caller's transaction, an
executor-callable path with no live connection). This module does not
collapse those three shapes into one — doing so would be the redesign this
extraction is explicitly not — it only removes the duplicated CONSTANTS and
the duplicated PROSE explaining their order.

If a fourth advisory-lock namespace is ever added, it is declared here
first, and its level relative to these two stated in the same paragraph this
docstring uses — an order that is not written down in one place is not an
order.
"""

#: LEVEL 1 — the reconciler's per-attempt reconciliation lease. Distinct
#: from R4 so an attempt's closure sequence and its registration sequence
#: never serialize against each other (see module docstring). Canonical
#: home: `pipeline.reconciler.lease.LEASE_NAMESPACE`.
RECONCILER_LEASE_NAMESPACE = 0x5732  # 'W6'

#: LEVEL 1 — the registrar's per-attempt lease (integration ruling 4).
#: Sibling of W6, never the same namespace, for the identical reason.
#: Canonical home: `pipeline.registration.consumer.ATTEMPT_LEASE_NAMESPACE`.
REGISTRAR_LEASE_NAMESPACE = 0x5234  # 'R4'

#: LEVEL 2 — the intent layer's per-work-unit lock (rule 9, brief C3),
#: always taken UNDERNEATH whichever LEVEL 1 lease is held, never above.
#: Canonical home: `pipeline.intent.lock.WORK_UNIT_NAMESPACE`.
WORK_UNIT_NAMESPACE = 0x5755  # 'WU'

#: The two LEVEL 1 namespaces, for a caller that wants to name "an attempt
#: lease of either kind" without enumerating both constants — e.g. a
#: `pg_locks` audit query, or a future assertion that WU is never granted
#: while EITHER is held by the same session.
ATTEMPT_LEASE_NAMESPACES = (RECONCILER_LEASE_NAMESPACE, REGISTRAR_LEASE_NAMESPACE)

#: Human-readable level tags, keyed by namespace — for log lines and
#: `pg_locks` reporting that want to say WHICH discipline a waiter is in
#: from the classid alone, matching the two-letter convention each
#: namespace's own module already uses in its constant's inline comment.
LEVEL_NAME = {
    RECONCILER_LEASE_NAMESPACE: "W6",
    REGISTRAR_LEASE_NAMESPACE: "R4",
    WORK_UNIT_NAMESPACE: "WU",
}


def acquire_blocking(execute_or_cursor, namespace, key):
    """Take a LEVEL-tagged advisory lock, blocking, over a cursor-shaped or
    executor-shaped caller.

    A caller passes either a DB-API cursor (has `.execute`) or a bare
    `execute(sql, params)` callable (the intent layer's `Executor`
    contract) — both are called the same way here, `cursor.execute` and a
    bare callable sharing the single-argument-pair calling convention this
    function needs. Returns nothing; a blocking `pg_advisory_xact_lock`
    call has nothing to report beyond "it returned", which means the lock
    is held.

    This is a THIN convenience, not a replacement for the three adopters'
    own helpers: `attempt_lease`/`work_unit_lock` still own their
    try-vs-block choice, their commit/rollback discipline, and their own
    logging — this function exists so a caller that only needs "block
    until this namespace/key is mine" (as `lock_work_unit` and
    `_acquire_attempt_lease` both are, at the SQL level) does not have to
    restate the statement text.
    """
    statement = "SELECT pg_advisory_xact_lock(%s, %s)"
    params = (namespace, int(key))
    execute = getattr(execute_or_cursor, "execute", execute_or_cursor)
    execute(statement, params)


def try_acquire(cursor, namespace, key):
    """Take a LEVEL-tagged advisory lock, non-blocking, over a DB-API cursor.

    Returns True when acquired, False when another session already holds
    it. Cursor-shaped only (unlike `acquire_blocking`): every current
    non-blocking caller (`attempt_lease`'s poll-and-skip,
    `work_unit_lock`'s `blocking=False` path) already holds a live cursor,
    and a try-acquire needs `fetchone()` to read the boolean back, which the
    bare-executor callers of `acquire_blocking` have no matching need for.
    """
    cursor.execute("SELECT pg_try_advisory_xact_lock(%s, %s)",
                   (namespace, int(key)))
    row = cursor.fetchone()
    return bool(row[0]) if row else False
