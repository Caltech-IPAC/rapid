"""The per-attempt reconciliation lease.

S3's tagging API has no compare-and-set. Neither does "write the closure record
then rewrite the tag set then transition the row" as a whole. So the thing that
makes those three steps safe under two concurrent reconcilers — or one
reconciler replaying after a crash — is a lock held across all of them.

It is a **transaction-scoped** advisory lock, not a session one. Session
advisory locks are forbidden on the transaction-pooled path by the database
design: the pooler hands a session to whichever client needs it next, so a lock
tied to the session outlives the work it was meant to guard and lands on a
stranger. `pg_advisory_xact_lock` releases at commit or rollback, which is
exactly the lifetime wanted, and it releases even if the process dies.

The lease spans, in order:

  reread the row under the lock  →  publish the closure record
  →  rewrite the full tag set    →  transition the row

The reread is not redundant. Between deciding an attempt needs reconciling and
acquiring its lease, another reconciler may have done the whole job; the
post-lock reread is what turns "I decided this 200 ms ago" into "this is true
now".
"""

import contextlib
import logging
import re

logger = logging.getLogger("rapid.reconciler.lease")

# Namespace for the two-argument advisory lock form, keeping reconciliation
# leases from colliding with any other advisory lock in the database. The
# resolver uses the one-argument form over hashtextextended(logical_job_id),
# which is a different key space.
LEASE_NAMESPACE = 0x5732  # 'W6'

# THE LOCK ORDER (conformance rule 9, brief C3). Three advisory-lock
# namespaces now exist, at two levels, and the order between the levels is
# fixed:
#
#   level 1, per ATTEMPT     W6  0x5732  this module (the reconciler's lease)
#                            R4  0x5234  pipeline.registration.consumer
#   level 2, per WORK UNIT   WU  0x5755  pipeline.intent.lock
#
# W6 and R4 are siblings and deliberately never serialize against each other:
# an attempt's closure sequence and its registration sequence are different
# critical sections over one attempt, and making them contend would risk
# deadlock for no invariant. That reasoning is unchanged.
#
# What changed is that the WORK UNIT — the resource rule 9's dispositions
# actually arbitrate — now has a lock of its own, and it is taken UNDERNEATH
# whichever attempt lease is held:
#
#     attempt lease (W6 or R4)  ->  work-unit lock (WU)   ALWAYS
#     work-unit lock            ->  attempt lease         NEVER
#
# That is the only order the code can take: both leases are acquired as the
# first statement of their transaction, and the work unit is not known until
# the attempt row has been read under the lease. Because no holder of WU ever
# waits for W6 or R4, no cycle can form. See `pipeline.intent.lock` for the
# full reasoning, including why the CAS in `transition_unit` remains under the
# lock rather than being replaced by it.

# A PostgreSQL identifier this code is willing to interpolate: lower-case
# ASCII, digits and underscores, starting with a letter. Every column in the
# attempts table matches; anything that does not is refused rather than
# quoted-and-hoped.
_IDENTIFIER = re.compile(r"\A[a-z][a-z0-9_]*\Z")


def _safe_identifier(name):
    if not _IDENTIFIER.match(name or ""):
        raise ValueError(
            f"refusing to interpolate {name!r} as a column identifier")
    return name


@contextlib.contextmanager
def attempt_lease(conn, attempt_id, blocking=False):
    """Hold the reconciliation lease for one attempt across a transaction.

    Yields True when the lease is held and False when `blocking` is False and
    another reconciler holds it — in which case the caller skips this attempt
    and picks it up next poll rather than queueing behind the other worker.
    Skipping is the right default for a polling service: there is always a next
    cycle, and blocking would let one slow attempt stall the whole batch.

    The transaction is committed on clean exit and rolled back on any
    exception, and the lock is released either way because it is
    transaction-scoped.
    """
    acquired = False
    try:
        with conn.cursor() as cur:
            if blocking:
                cur.execute("SELECT pg_advisory_xact_lock(%s, %s)",
                            (LEASE_NAMESPACE, int(attempt_id)))
                acquired = True
            else:
                cur.execute("SELECT pg_try_advisory_xact_lock(%s, %s)",
                            (LEASE_NAMESPACE, int(attempt_id)))
                row = cur.fetchone()
                acquired = bool(row[0]) if row else False

        if not acquired:
            logger.debug("attempt %s is leased by another reconciler; skipping",
                         attempt_id)
            conn.rollback()
            yield False
            return

        yield True
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def reread_attempt(conn, attempt_id, columns=None):
    """Read the attempt row under the lease, as a dict.

    Called *after* the lock is held. What it returns is the state the rest of
    the lease acts on; anything read before acquiring the lock is stale by
    definition.

    Column names are quoted through `psycopg2.sql.Identifier` — the repo-wide
    rule from the parameterization sweep — but only when psycopg2 is actually
    present. The unit suite runs where it is not (it is not installed on the
    laptop and would not be the right build if it were), and a caller there
    passes a literal tuple of known column names, so the fallback validates
    the names against a strict pattern instead of quoting them. A name that
    does not match is refused rather than interpolated.
    """
    if columns:
        try:
            from psycopg2 import sql
        except ImportError:
            selected = ", ".join(_safe_identifier(name) for name in columns)
            statement = f"SELECT {selected} FROM attempts WHERE attempt_id = %s"  # noqa: S608
        else:
            statement = sql.SQL(
                "SELECT {} FROM attempts WHERE attempt_id = %s").format(
                    sql.SQL(", ").join(sql.Identifier(n) for n in columns))
    else:
        statement = "SELECT * FROM attempts WHERE attempt_id = %s"

    with conn.cursor() as cur:
        cur.execute(statement, (attempt_id,))
        row = cur.fetchone()
        if row is None:
            return None
        names = [description[0] for description in cur.description]
        return dict(zip(names, row))
