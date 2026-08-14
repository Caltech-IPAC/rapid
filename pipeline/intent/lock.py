"""The work-unit lock: one discipline, one order, for every disposition.

Conformance rule 9, second sentence: "Cancellation, quarantine, retry and
acceptance all take the same work-unit lock in the same order." Before this
module there was no work-unit lock at all. What existed was two ATTEMPT-keyed
advisory-lock namespaces that deliberately never met —

    W6  0x5732  pipeline.reconciler.lease      keyed (0x5732, attempt_id)
    R4  0x5234  pipeline.registration.consumer keyed (0x5234, attempt_id)

— documented as distinct so "a registrar lease and a reconciler lease on the
same attempt id never collide semantically". That reasoning is correct for
what those leases guard, and this module does not undo it: an attempt's
closure sequence and an attempt's registration sequence are two different
critical sections over ONE ATTEMPT, and serializing them against each other
would be a deadlock risk for no invariant.

**BUT THE ATTEMPT IS NOT THE ARBITRATED RESOURCE.** The state machine rule 9
is about lives on `work_units`, and every transition of it — package A's new
blocked-parking and scheduler-retry edges, quarantine, retry, closure,
acceptance, and now cancellation — was a bare compare-and-set with no lock of
any kind (`pipeline.intent.writer.transition_unit`: `UPDATE work_units SET
state=... WHERE work_unit_id=%s AND state=%s`). Two writers reaching one unit
from the two lease namespaces are, by construction, NOT serialized against
each other: each holds a lock the other does not contend for, and both then
race on the same row. The CAS makes the race safe in the narrow sense that
only one UPDATE can win — but "one wins" is not the invariant. A disposition
is a decision made from a READ (the sibling-attempt series, the watermark, the
retry-policy inputs) and then applied by a WRITE, and a CAS protects only the
write. Two writers can each read a consistent view, each decide, and one's
decision then lands on a unit the other's decision was already based on.

So the discipline this module adds is one lock on the WORK UNIT — the
resource actually being arbitrated — taken by every disposition before it
reads the state it decides from, and held until that decision is written:

    W U  0x5755  keyed (0x5755, work_unit_id)

**THE CAS REMAINS, AND IS NOT REDUNDANT** (brief C3: "the CAS remains as the
state guard under the lock, not as the sole mechanism"). The lock serializes
deciders; the CAS still verifies that the state a decider read is the state it
is transitioning from. They fail differently and both failures are real: the
lock's absence is an interleaving anomaly, the CAS's absence is a lost update
by a writer that never read. Nothing here weakens `transition_unit`'s guard.

## The order, stated because an order that is not written down is not an order

Where both an attempt lease and a work-unit lock are held, the order is
ALWAYS:

    1. attempt lease   (W6 in the reconciler, R4 in the registrar)
    2. work-unit lock  (WU, this module)

and never the reverse. This is not arbitrary — it is the only order the
existing code can take without inverting a call structure: both leases are
acquired as the FIRST statement of their transaction (the registrar's
`_acquire_attempt_lease` documents exactly that, and the reconciler's
`attempt_lease` opens the transaction it spans), and the work unit is not even
known until the attempt row has been read under that lease. A path that took
the work-unit lock first would have to know the work unit before reading the
attempt, which no caller does.

Deadlock freedom follows from that being a total order over the two levels,
not from any claim about which unit a lock is taken on: a holder of an attempt
lease may block waiting for a work-unit lock, but a holder of a work-unit lock
never waits for an attempt lease, so no cycle can form. A caller that acquires
work-unit locks for TWO units in one transaction (no current caller does)
would need an order among units as well — `work_unit_id` ascending is the
convention to adopt if that ever arises, and it is stated here so the first
such caller inherits an answer rather than inventing one.

## Why an advisory lock rather than SELECT ... FOR UPDATE on the row

`FOR UPDATE` would serialize writers on the row and is a real option. The
advisory lock is chosen for consistency with the two leases already in this
codebase and for one property `FOR UPDATE` lacks here: a disposition's
decision reads rows OTHER than `work_units` (the attempt series, the retry
history), and an advisory lock names the ABSTRACT resource "this unit's
disposition" rather than the physical row, so it covers the whole decision
including reads that touch no `work_units` row at all. It is also what lets
the cancellation path, which may find no unit row in the state it expected,
still hold the same mutual exclusion every other disposition holds.

**TRANSACTION-SCOPED, never session-scoped.** `pg_advisory_xact_lock` releases
at commit or rollback, and even if the process dies. Session advisory locks
are forbidden on the transaction-pooled path by the database design — the
pooler hands a session to whichever client needs it next, so a lock tied to
the session outlives the work it guarded and lands on a stranger. The same
reasoning `pipeline.reconciler.lease` gives, for the same pooler.
"""

import contextlib
import logging

from pipeline.runtime import lock_order
from pipeline.runtime.lock_order import WORK_UNIT_NAMESPACE

logger = logging.getLogger("rapid.intent.lock")

#: The work-unit lock namespace for `pg_advisory_xact_lock`'s two-argument
#: form. 0x5755 is 'WU', named in the same convention as the two attempt-lease
#: namespaces it sits below ('W6' the reconciler's, 'R4' the registrar's) so a
#: `pg_locks` reading shows which discipline a waiter is in from the classid
#: alone.
#:
#: DISTINCT FROM BOTH LEASE NAMESPACES ON PURPOSE, and the distinction now
#: carries a different meaning than theirs does. W6 and R4 are distinct from
#: each other so they never serialize; WU is distinct from both so it can be
#: held UNDERNEATH either one. A work-unit lock that shared a namespace with
#: an attempt lease would collide whenever a work_unit_id happened to equal an
#: attempt_id — two unrelated identifier spaces, both dense from 1.
#:
#: CANONICAL VALUE NOW LIVES IN `pipeline.runtime.lock_order` (campaign
#: ruling C3): this name is kept, and re-exported, so no importer of this
#: module needs to change — see that module for the full two-level order
#: this namespace is LEVEL 2 of.


@contextlib.contextmanager
def work_unit_lock(conn, work_unit_id, blocking=True):
    """Hold the work-unit lock across the caller's transaction.

    Yields True when the lock is held, False when `blocking` is False and
    another writer holds it.

    **BLOCKING IS THE DEFAULT, and the default is the right one for a
    disposition.** The reconciler's attempt lease defaults to try-and-skip
    because it polls: there is always a next cycle, and blocking would let one
    slow attempt stall a whole batch. A disposition is not a poll — an
    operator cancelling a unit, or a closure applying a retry-policy verdict,
    has one thing to do and no next cycle to defer to. Skipping would mean
    silently not cancelling, which is the failure mode rule 9 exists to
    prevent. `blocking=False` is offered for a caller that genuinely wants
    poll-and-skip semantics, and the reconciler's sweep is the one plausible
    such caller.

    THIS OPENS NO TRANSACTION AND COMMITS NOTHING. `pg_advisory_xact_lock` is
    scoped to the transaction the CALLER already owns — every current call
    site is already inside one (the reconciler's lease block, the registrar's
    `_transaction(conn)`, the mutation API's own transaction) — and the lock
    releases at that transaction's commit or rollback. A context manager that
    committed here would end the caller's transaction underneath it and
    release the lock while the work it guards was still running, which is
    precisely the bug the lease's own docstring warns about. Contrast
    `pipeline.reconciler.lease.attempt_lease`, which DOES commit: that helper
    opens the transaction it spans, because its caller is not already in one.
    """
    acquired = False
    with conn.cursor() as cur:
        if blocking:
            lock_order.acquire_blocking(cur, WORK_UNIT_NAMESPACE, work_unit_id)
            acquired = True
        else:
            acquired = lock_order.try_acquire(cur, WORK_UNIT_NAMESPACE,
                                              work_unit_id)

    if not acquired:
        logger.debug(
            "work unit %s is held by another disposition; skipping",
            work_unit_id)
        yield False
        return

    logger.debug("work-unit lock held for %s", work_unit_id)
    yield True


def lock_work_unit(execute, work_unit_id):
    """Take the work-unit lock through an `execute(sql, params)` callable.

    The same lock as `work_unit_lock`, for the callers that hold an injected
    executor rather than a raw connection — which is every intent-layer writer
    path, since `WorkUnitWriter` takes `execute` and never a connection. There
    is no context manager to exit: the lock is transaction-scoped, so it is
    released by the caller's own commit or rollback and there is nothing to
    undo on the way out.

    Blocking only. A caller with an executor and no connection cannot inspect
    a `try_` result without a round trip it has no use for, and every such
    caller is a disposition, which should block (see `work_unit_lock`).
    """
    lock_order.acquire_blocking(execute, WORK_UNIT_NAMESPACE, work_unit_id)
    logger.debug("work-unit lock held for %s (executor path)", work_unit_id)
    return True
