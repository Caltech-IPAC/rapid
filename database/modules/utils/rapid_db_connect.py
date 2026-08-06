"""One connection path to the RAPID operations database.

The single connection helper the adopted database design requires
(rapid_plan design/database.md, "One connection path, parameterized SQL
only"): credential fetch under the ambient role, connect timeout,
per-component ``application_name``, bounded connect retry with backoff,
context-manager transactions, and errors RAISED — never swallowed into
member flags, never ``exit()`` from library code.

Why a new module beside ``rapid_db.py`` rather than an extension of it
(W1 decision, recorded): ``rapid_db.py``'s ``RAPIDDB`` is built on the
opposite contract. It carries a member ``exit_code`` that callers must
remember to check, it calls ``exit(64)`` from ``__init__`` on a missing
environment variable, and it auto-commits per call. Those are the three
behaviours this helper exists to replace, so growing it inside that class
would mean one module presenting two contradictory error contracts to its
callers — the failure mode where a caller reads ``exit_code`` on an
object that raises, or ignores an exception on an object that flags. A
separate module makes the boundary legible and lets ``rapid_db.py``'s
3,958 lines be converted incrementally (W3's parameterization sweep)
without this file's contract wavering.

``get_db_credentials`` is deliberately NOT re-implemented here: it is
imported from ``rapid_db`` so the Secrets Manager convention (secret id
in ``RAPID_DB_SECRET_ID``, JSON ``SecretString`` with ``username`` /
``password`` keys, ``DBUSER``/``DBPASS`` fallback) has exactly one home.
What this module does add is a raising wrapper around it: the existing
function returns ``(None, None)`` on failure after printing, which is the
swallow this contract forbids at the boundary.

Every dynamic identifier goes through ``psycopg2.sql.Identifier``; there
is no code path in this module that interpolates a value or a name into
SQL text.
"""

import contextlib
import logging
import os
import time

import psycopg2
import psycopg2.extensions
from psycopg2 import sql

# Absolute import, matching the repo's convention for this package
# (`import database.modules.utils.rapid_db as db` at every call site) —
# there are no __init__.py files here, so a relative import would not
# resolve when a script is run from the repo root.
from database.modules.utils.rapid_db import get_db_credentials

logger = logging.getLogger(__name__)

# Starting values, replaceable by evidence without re-ratification (the
# design states the shape — timeout, bounded retry, backoff — not the
# numbers).
DEFAULT_CONNECT_TIMEOUT_S = 10
DEFAULT_CONNECT_ATTEMPTS = 4
DEFAULT_BACKOFF_INITIAL_S = 0.5
DEFAULT_BACKOFF_MULTIPLIER = 2.0
DEFAULT_BACKOFF_CAP_S = 8.0

# The two lanes at the one pooler door (design/database.md, and the route
# matrix in the Batch payload contract). The lane is part of each job
# type's validated route, NOT a caller's free choice — these constants
# name the lanes so a caller records which one its route assigns, they do
# not authorise picking one.
LANE_TRANSACTION = "transaction"
LANE_SESSION = "session"
LANES = frozenset({LANE_TRANSACTION, LANE_SESSION})


class DBError(Exception):
    """Base for every error this module raises.

    Carries ``error_category`` so the runtime's taxonomy serializer can
    map it onto the attempt record's allowlist (migration 013's
    ``attempt_error_categories``) without a second lookup table.
    """

    error_category = "db_error"


class DBUnavailable(DBError):
    """Connect failed or timed out after the bounded retry was exhausted."""

    error_category = "db_unavailable"


class DBCredentialError(DBError):
    """The credential could not be resolved.

    Distinct from ``DBUnavailable``: the database may be perfectly
    healthy. Classified as ``config_invalid`` because a missing secret id
    or an unreadable secret is a configuration fault, not a database one.
    """

    error_category = "config_invalid"


def resolve_credentials():
    """Fetch (user, password) under the ambient role, or raise.

    Wraps ``rapid_db.get_db_credentials``, which returns ``(None, None)``
    on failure after printing to stdout. That swallow is the thing this
    contract forbids, so it is converted to a raise here — the one place
    the conversion has to happen for every caller of this module.
    """
    try:
        user, password = get_db_credentials()
    except Exception as exc:  # noqa: BLE001 - re-raised as our own type
        raise DBCredentialError(f"credential lookup failed: {exc}") from exc

    if user is None or password is None:
        secret_id = os.getenv("RAPID_DB_SECRET_ID")
        if secret_id:
            raise DBCredentialError(
                f"could not resolve database credentials from Secrets Manager "
                f"secret {secret_id!r} (expected a JSON SecretString with "
                f"'username' and 'password')")
        raise DBCredentialError(
            "could not resolve database credentials: neither "
            "RAPID_DB_SECRET_ID nor DBUSER/DBPASS is usable")
    return user, password


def _require_env(name):
    value = os.getenv(name)
    if value is None or value == "":
        raise DBCredentialError(
            f"environment variable {name} is not set; it is operational "
            f"configuration and must come from the parameter tree, not a "
            f"default compiled in here")
    return value


def connect(application_name,
            lane=LANE_TRANSACTION,
            connect_timeout=DEFAULT_CONNECT_TIMEOUT_S,
            attempts=DEFAULT_CONNECT_ATTEMPTS,
            backoff_initial=DEFAULT_BACKOFF_INITIAL_S,
            backoff_multiplier=DEFAULT_BACKOFF_MULTIPLIER,
            backoff_cap=DEFAULT_BACKOFF_CAP_S,
            sleep=time.sleep,
            connect_fn=None):
    """Open one connection, with bounded retry and backoff. Raises on failure.

    ``application_name`` is required, not defaulted: it is what makes
    pooler-side attribution possible (which component is holding which
    server connection), and a default would collapse every component into
    one indistinguishable name at exactly the moment someone is trying to
    tell them apart.

    ``lane`` records which pooled lane this connection's job type is
    routed to. It does not change the connection parameters — both lanes
    arrive at the same pooler door on the same port; the pooler applies
    per-user pool_mode. It is carried into ``application_name`` so the
    lane a connection believes it is on is visible in ``pg_stat_activity``
    and the pooler's admin interface, which is how a job that transacts
    long on the transaction lane gets caught.

    ``sleep`` and ``connect_fn`` are injection points for tests; nothing
    in production passes them.

    Retry covers ``OperationalError`` only — the class that means "could
    not reach or authenticate to the server". A ``ProgrammingError`` or a
    constraint violation is not retried: repeating a query the server
    already rejected changes nothing and only delays the failure.
    """
    if not application_name:
        raise ValueError(
            "application_name is required: it is the pooler's attribution "
            "handle, and defaulting it would make every component "
            "indistinguishable in pg_stat_activity")
    if lane not in LANES:
        raise ValueError(
            f"{lane!r} is not a database lane; expected one of "
            + ", ".join(sorted(LANES)))
    if attempts < 1:
        raise ValueError(f"attempts must be >= 1; got {attempts}")

    connect_fn = connect_fn or psycopg2.connect

    host = _require_env("DBSERVER")
    port = _require_env("DBPORT")
    dbname = _require_env("DBNAME")
    user, password = resolve_credentials()

    # PostgreSQL truncates application_name at NAMEDATALEN-1 (63 bytes) and
    # would silently lose the lane suffix on a long component name, so the
    # composed name is trimmed here where the truncation is visible rather
    # than server-side where it is not.
    composed_name = f"{application_name}[{lane}]"[:63]

    delay = backoff_initial
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            conn = connect_fn(
                host=host,
                port=port,
                dbname=dbname,
                user=user,
                password=password,
                connect_timeout=connect_timeout,
                application_name=composed_name,
            )
        except psycopg2.OperationalError as exc:
            last_exc = exc
            if attempt == attempts:
                break
            logger.warning(
                "database connect attempt %d/%d failed (%s); retrying in %.1fs",
                attempt, attempts, exc, delay)
            sleep(delay)
            delay = min(delay * backoff_multiplier, backoff_cap)
            continue

        # Transactions are explicit here (context managers below), so
        # autocommit stays off — the opposite of rapid_db.py's per-call
        # autocommit, and the reason a multi-statement unit of work in this
        # module is actually atomic.
        conn.autocommit = False
        logger.info("connected to %s:%s/%s as %s (application_name=%s)",
                    host, port, dbname, user, composed_name)
        return conn

    raise DBUnavailable(
        f"could not connect to {host}:{port}/{dbname} as {user} after "
        f"{attempts} attempt(s): {last_exc}") from last_exc


@contextlib.contextmanager
def transaction(conn):
    """Run a unit of work in one transaction: commit on success, rollback on error.

    psycopg2's own ``with conn:`` commits or rolls back but does NOT close
    the cursor, and — more importantly — it does not re-raise as this
    contract wants for a failed rollback. This wrapper yields a cursor,
    always closes it, and lets the original exception propagate.
    """
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


@contextlib.contextmanager
def connection(application_name, **kwargs):
    """Open a connection for the duration of a block, then close it.

    The connection is closed on every exit path, including an exception
    during the block — a leaked server connection on the pooled path costs
    a slot in a small, explicitly budgeted pool.
    """
    conn = connect(application_name, **kwargs)
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            logger.exception("closing the database connection failed")


def qualified_identifier(*parts):
    """Compose a dotted SQL identifier from parts, safely quoted.

    The only sanctioned way to put a name into SQL text in this codebase.
    Every part goes through ``sql.Identifier``, so a name containing a
    quote, a dot, or a semicolon is quoted rather than interpreted —
    which is what makes the design's "String-substituted SQL is
    prohibited" rule enforceable rather than aspirational.
    """
    if not parts:
        raise ValueError("an identifier needs at least one part")
    return sql.SQL(".").join(sql.Identifier(p) for p in parts)


def select_columns(table, columns, where_column=None):
    """Build a SELECT whose table and column names are dynamic but quoted.

    Returned as a ``psycopg2.sql.Composable``; the caller passes it to
    ``cursor.execute`` with its parameters. Values NEVER travel in the
    composed text — the optional predicate emits a ``%s`` placeholder.
    """
    if not columns:
        raise ValueError("select_columns needs at least one column")

    stmt = sql.SQL("SELECT {cols} FROM {tbl}").format(
        cols=sql.SQL(", ").join(sql.Identifier(c) for c in columns),
        tbl=qualified_identifier(*(table if isinstance(table, (list, tuple))
                                   else (table,))),
    )
    if where_column is not None:
        stmt = sql.SQL(" ").join([
            stmt,
            sql.SQL("WHERE {col} = %s").format(col=sql.Identifier(where_column)),
        ])
    return stmt


class ConnectionExecutor:
    """An ``Executor`` over one connection: ``execute(sql, params) -> rows``.

    This is the concrete implementation of the callable boundary
    ``observability.attempts.AttemptWriter`` has always taken and nothing
    has ever supplied — the missing piece that kept ``observability/``
    unwired.

    One statement, one transaction, by default. That is deliberate: the
    attempt writer's calls are individually meaningful state transitions
    (claim the row, mark started, close it), and a crash between two of
    them must leave the earlier one durable — which is exactly what the
    reconciler's recovery assumes. A caller that genuinely needs several
    statements to be atomic passes ``autocommit_each=False`` and manages
    the transaction itself with :func:`transaction`.

    Errors propagate. There is no ``exit_code`` member and no ``exit()``.
    """

    def __init__(self, conn, autocommit_each=True):
        self._conn = conn
        self._autocommit_each = autocommit_each

    def __call__(self, statement, params):
        return self.execute(statement, params)

    def execute(self, statement, params):
        """Run one parameterized statement; return its rows, or None.

        ``statement`` may be a ``str`` or a ``psycopg2.sql.Composable``.
        Values are ALWAYS passed as ``params`` — this method has no branch
        that puts one into the statement text.
        """
        cur = self._conn.cursor()
        try:
            cur.execute(statement, params)
            rows = cur.fetchall() if cur.description is not None else None
        except Exception:
            if self._autocommit_each:
                try:
                    self._conn.rollback()
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "rollback failed; the original error follows")
            raise
        else:
            if self._autocommit_each:
                self._conn.commit()
            return rows
        finally:
            cur.close()
