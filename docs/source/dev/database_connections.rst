Database connections from pipeline code
####################################################

``rapidpipe.db.connection`` is the one path pipeline code uses to connect
to the run-model tables. This page replaces a narrative incident report
from a predecessor branch, retaining its generalized lessons and tracing
them to the implementing code (see "Two incidents behind the design"
below). The original report is absent from this repository because it
named private infrastructure, package versions and work-item status that
do not exist here.

What the connection module guarantees
************************************************

**Endpoint and credentials.** ``connect()`` accepts an explicit
``endpoint=`` (host, port, dbname) and ``credentials=`` (user, password).
Callers can pass either directly, for example after reading a parameter
tree or resolving a secret under their own role. Missing values fall back
to the standard ``PG*`` environment variables: ``PGHOST``, ``PGPORT``,
``PGDATABASE``, ``PGUSER`` and ``PGPASSWORD``. If ``RAPID_DB_SECRET_ID`` is
set in the environment and no explicit ``credentials=`` was given,
``credentials_from_secret()`` instead resolves the credential from that
AWS Secrets Manager secret. Credentials are never expected as command-line
arguments or read from files committed to the repository.

**Bounded connect retry.** Only ``psycopg2.OperationalError`` triggers
connection retries, up to ``attempts`` times (default
``DEFAULT_CONNECT_ATTEMPTS = 5``). Exponential backoff starts at
``backoff_initial`` (``DEFAULT_BACKOFF_INITIAL_S = 0.5`` seconds), doubles
by ``backoff_multiplier`` (``DEFAULT_BACKOFF_MULTIPLIER = 2.0``) each
attempt, and stops growing at ``backoff_cap``
(``DEFAULT_BACKOFF_CAP_S = 8.0`` seconds). ``jitter=True`` replaces each
wait with a random duration in ``[0, delay]`` to keep callers retrying
after the same event from staying synchronized. All are ordinary keyword
arguments callers can override.

**TCP keepalives.** Every connection sets ``KEEPALIVES``,
``KEEPALIVES_IDLE_S``, ``KEEPALIVES_INTERVAL_S``, ``KEEPALIVES_COUNT`` and
a ``TCP_USER_TIMEOUT_MS`` backstop. These detect a vanished peer at the
socket in about a minute: 30 seconds before the first probe, then up to
three probes ten seconds apart, rather than the kernel's two-hour default.

**Identification and timeout.** Every connection carries an
``application_name``, visibly truncated to PostgreSQL's 63-byte
``NAMEDATALEN - 1`` limit before reaching the server. A ``connect_timeout``
(default ``DEFAULT_CONNECT_TIMEOUT_S = 10`` seconds) bounds each
connection attempt.

What it deliberately does not do
************************************************

Retries cover only connecting, never a statement in flight when a
connection dies. A healthy, established connection closed mid-statement
is a server-side or pooler fault to diagnose and fix there. Silent client
retries would harm the pipeline at its operating scale, where on the
order of a thousand concurrent jobs may each hold a connection, by
masking a pooler that drops payload connections.

The module carries no pooler configuration, connection "lanes", or
``SET ROLE`` role-widening bookkeeping. Pgbouncer (or any pooler) is
server-side infrastructure provisioned and configured by
``rapid_systems``, per the repository boundaries in the
`RAPID specification <https://roman-rapid.readthedocs.io/en/latest/system/specification.html>`_
("Repositories"). The module requests one connection at a time from the
pooler's or database's own listening port. Pooler configuration, including
per-user settings and timeouts, is owned and changed in ``rapid_systems``,
never in this repository.

``rapidpipe.runs.repository`` opens no transactions beyond those this
module provides. Each repository function is documented as exactly one
transaction per call, using ``transaction()`` below.

Two incidents behind the design
************************************************

**August 2026.** A transaction-pooled pgbouncer instance closed freshly
opened pipeline connections at ``age=0`` seconds with a
``client_idle_timeout`` closure. The pipeline's database user had no
per-user timeout setting, and the pooler's global ``client_idle_timeout``
was disabled. The cause was a ``client_idle_timeout`` value on per-user
configuration lines for a handful of other, human-operator database
users. Removing those lines stopped the closures immediately and
completely. That unrelated users' settings reached a user with no
per-user line remains unverified against the pgbouncer issue tracker.
The lesson: diagnose a healthy connection closed mid-statement as a
pooler defect from the pooler's own log, not by adding client
reconnect-and-retry. The pipeline's fail-loud, nonzero exit was and
remained correct.

**September 2026.** A database host was replaced while a long-lived
client connection still pointed at its old address. Without keepalives,
the client could not distinguish a quiet connection from a vanished peer.
The condition went undetected for hours, bounded only by the kernel's
default keepalive timeout. The lesson, embodied in this module's defaults:
set keepalives on every connection to detect a vanished peer at the socket
in about a minute. Socket-level dead-peer detection is distinct from
statement-level retry. It neither addresses nor helps with a pooler
actively closing a healthy connection, as in the first incident above.

Diagnosing a dropped connection
************************************************

* **Look first at the pooler's log.** Its close reason, such as
  ``client_idle_timeout`` versus ``client close request``, is diagnostic
  and not visible from the client.

* **Distinguish connection failure from mid-statement failure.** During
  connect, ``psycopg2.OperationalError`` triggers automatic, bounded
  retries with backoff. Once the budget is exhausted, ``connect()`` wraps
  the error as ``ConnectionUnavailable``, naming the endpoint, user and
  attempt count. A mid-statement closure on an established connection
  instead propagates as a driver exception and is never retried.

* **Rule out a vanished peer through keepalives, not pooler logs.** The
  defaults detect a vanished peer within roughly a minute; a longer hang
  points elsewhere.
