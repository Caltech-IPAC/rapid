"""The `gc_fences` protocol: one table, two holder kinds, one SQL shape.

Extracted from `pipeline.gc.execute.Executor._acquire_fence`/`_release_fence`
(brief H3/H4's GC half), which owned this SQL first. The registration side
(`pipeline.registration.consumer`) needs the IDENTICAL discipline over the
SAME table for the fence to mean anything — a fence GC observes but
registration acquires under a different expiry rule, or a different
ON CONFLICT reclaim condition, is two dialects of "I hold this key" that
happen to share a table name, and the second registration-side race test
this module exists for is exactly what would catch that divergence arriving
by drift. So this module is the one place both sides call, rather than a
second copy of the INSERT living in `consumer.py`.

**THE FENCE FAILS CLOSED** (see `pipeline.gc.execute`'s module docstring for
the four-layer mitigation this is layer 4 of). `acquire_fence` returns
`False` for the one genuine "not acquired" outcome — a live conflicting row,
the `ON CONFLICT ... WHERE expires_at < now()` clause finding a holder
already present and unexpired — and RAISES for everything else: a caller
that cannot tell "someone else holds this" from "I could not even ask"
would otherwise have to guess, silently, between two operator responses
that are not interchangeable (skip this item and move on, versus stop and
fix why the query failed).

**THE STATIC-GRANT CASE THIS MUST NOT MASK.** If the role calling
`acquire_fence` is missing `INSERT`/`UPDATE` on `gc_fences` — a grant gap,
not a held fence — PostgreSQL raises `InsufficientPrivilege` on every
attempt. Swallowing that into `False` identically to a live conflicting row
(the earlier shape of this function) would render as "the fence is always
held", which SILENTLY DISABLES ALL OF GC EXECUTION under that role: every
item is skipped as fenced, the run reports a clean "nothing to do", and
nothing in the output says why. That is a worse failure than a raised
exception — a raised `InsufficientPrivilege` is loud and points straight at
the grant; a fleet of `False`s looks like ordinary contention. So a
database error here propagates rather than folding into the boolean.

**EXPIRY IS JUDGED IN THE INSERT'S OWN `WHERE` CLAUSE, NOT BY A SWEEPER.** A
sweeper that has not run yet would make an expired fence look live, which is
exactly the crash-recovery gap holder_kind='registration' needs closed too:
a registrar that crashes mid-bind leaves its fence row behind, and the NEXT
acquirer — GC or another registration pass — reclaims it the instant the
lease elapses, via the same conflicting INSERT, with no separate recovery
path to have forgotten to run.
"""

import datetime

#: How long a fence is held by default. Callers may override — GC's own
#: `FENCE_LEASE_SECONDS` (`pipeline.gc.execute`) is deliberately kept as
#: that module's own constant rather than re-exported from here, so a
#: change to GC's lease is a one-line diff in the module that owns GC's
#: timing and does not, by import, also move registration's.
DEFAULT_LEASE_SECONDS = 120

#: The two holders this table's `holder_kind` distinguishes. Any other
#: string is a caller bug, not a third kind this module knows how to
#: reconcile — GC's `_still_discharged` and the registration-held check
#: below both assume exactly two.
HOLDER_GC = "gc"
HOLDER_REGISTRATION = "registration"


def acquire_fence(execute, *, bucket, object_key, holder, holder_kind,
                  lease_seconds=DEFAULT_LEASE_SECONDS):
    """Take the fence over `(bucket, object_key)`, or report failure.

    `execute(sql, params)` is the caller's own query boundary — GC's
    `GCPlanRepository._query` and registration's per-attempt cursor both
    fit this shape trivially, which is the point: this module does not
    open a cursor or own a transaction, it only issues one statement
    inside whichever transaction the caller already has open. The fence
    row therefore commits or rolls back WITH the caller's own unit of
    work — for GC, with the item's outcome write; for registration, with
    the product rows and the watermark — never as a side channel that
    could commit while the work it is guarding does not.

    A plain INSERT against `gc_fences_key_uq`. A conflicting row means
    someone else — the other holder kind, or another instance of this
    same one — holds it, and the caller must treat that as "not
    acquired", never as a reason to proceed unguarded. Expired leases are
    reclaimed by the SAME statement, so a crashed holder cannot block a
    key forever; expiry is judged HERE, in the `WHERE` clause, rather
    than by a sweeper (see the module docstring).

    Returns True/False for the genuine "is this key free" question only:
    an empty `RETURNING` — the `ON CONFLICT ... WHERE expires_at < now()`
    clause finding a live holder already there — is "not acquired", and
    nothing else is. A DATABASE ERROR RAISES. It used to be swallowed here
    identically to a conflicting row, which is exactly wrong under a
    static-grant gap (see the module docstring's threat model, and P-H
    wherever `rapid_operator` is missing `INSERT`/`UPDATE` on `gc_fences`):
    a bare `InsufficientPrivilege` would then render as "fence held",
    silently disabling every GC execution under that role while reporting
    the ordinary, expected outcome. A caller cannot tell "someone holds
    this" from "I am not permitted to even ask" if both come back `False`
    — and the two demand opposite operator responses, skip this item versus
    fix the grant. Only the empty-RETURNING case is swallowed into `False`;
    everything else — permission, connectivity, a broken statement —
    propagates so the caller sees it.
    """
    expires = datetime.timedelta(seconds=lease_seconds)
    rows = execute(
        "INSERT INTO gc_fences"
        " (bucket, object_key, holder, holder_kind, expires_at)"
        " VALUES (%s, %s, %s, %s, now() + %s)"
        " ON CONFLICT (bucket, object_key) DO UPDATE"
        "    SET holder = EXCLUDED.holder,"
        "        holder_kind = EXCLUDED.holder_kind,"
        "        acquired_at = now(),"
        "        expires_at = EXCLUDED.expires_at"
        "  WHERE gc_fences.expires_at < now()"
        " RETURNING fence_id",
        (bucket, object_key, holder, holder_kind, expires))
    return bool(rows)


def release_fence(execute, *, bucket, object_key, holder):
    """Release the fence this exact holder took, if any.

    Scoped by `holder` as well as `(bucket, object_key)` — the same
    defensive shape `Executor._release_fence` already used — so a holder
    releasing late, after its own lease already expired and was reclaimed
    by someone else, deletes nothing rather than deleting the NEW
    holder's live fence out from under it.

    Never raises: a fence left behind expires on its own lease, and
    failing to release cleanly is not worth failing the caller's run
    over — the same judgment `Executor._release_fence`'s `finally` block
    already makes, reused here rather than re-derived.
    """
    try:
        execute(
            "DELETE FROM gc_fences"
            " WHERE bucket = %s AND object_key = %s AND holder = %s",
            (bucket, object_key, holder))
    except Exception:                                 # noqa: BLE001
        pass


def held_by(execute, *, bucket, object_key):
    """The live (unexpired) fence over this key, if any — `(holder,
    holder_kind, expires_at)`, or `None`.

    Read-only observation, for a caller that needs to know WHO holds a
    key rather than to acquire it — GC's counterpart-verification
    ("ensure a registration-held fence is actually observed by GC") reads
    this to confirm a `holder_kind='registration'` row exists over a
    candidate before treating the key as free, distinct from
    `acquire_fence`'s own conflict check, which only tells GC that IT did
    not get the fence, not who has it or why that matters for the
    decision GC is about to make.
    """
    rows = execute(
        "SELECT holder, holder_kind, expires_at FROM gc_fences"
        " WHERE bucket = %s AND object_key = %s AND expires_at >= now()",
        (bucket, object_key))
    if not rows:
        return None
    holder, holder_kind, expires_at = rows[0]
    return holder, holder_kind, expires_at
