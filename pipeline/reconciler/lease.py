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

from pipeline.runtime import lock_order
from pipeline.runtime.lock_order import RECONCILER_LEASE_NAMESPACE

logger = logging.getLogger("rapid.reconciler.lease")

# Namespace for the two-argument advisory lock form, keeping reconciliation
# leases from colliding with any other advisory lock in the database. The
# resolver uses the one-argument form over hashtextextended(logical_job_id),
# which is a different key space.
#
# CANONICAL VALUE NOW LIVES IN `pipeline.runtime.lock_order` (campaign
# ruling C3), which is also where the full LEVEL-1/LEVEL-2 order this
# namespace participates in is written down once. This name is kept, and
# re-exported, so no importer of this module needs to change.
LEASE_NAMESPACE = RECONCILER_LEASE_NAMESPACE

# THE LOCK ORDER (conformance rule 9, brief C3): this lease is LEVEL 1
# ('W6'). The full two-level order — this lease and the registrar's ('R4')
# both sit above the intent layer's per-work-unit lock ('WU'), always
# underneath, never above — is written down ONCE, in
# `pipeline.runtime.lock_order` (campaign ruling C3), rather than repeated
# here. See that module for the full reasoning, including why the CAS in
# `transition_unit` remains under the lock rather than being replaced by it.

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
                lock_order.acquire_blocking(cur, LEASE_NAMESPACE, attempt_id)
                acquired = True
            else:
                acquired = lock_order.try_acquire(cur, LEASE_NAMESPACE,
                                                  attempt_id)

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
