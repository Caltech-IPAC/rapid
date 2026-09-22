Database connections from pipeline code
####################################################

This page documents ``rapidpipe.db.connection``, the one connection path
pipeline code uses to reach the run-model tables. It replaces an earlier
narrative incident report carried on a predecessor branch (see "Two
incidents behind the design" below); that document does not appear in
this repository because it named private infrastructure, package
versions and work-item status that do not exist here. The design lessons
it recorded are kept, generalized, and traced to the code that
implements them.

What the connection module guarantees
************************************************

**Where the endpoint and credentials come from.** ``connect()`` accepts
an explicit ``endpoint=`` (host, port, dbname) and ``credentials=``
(user, password). A caller that already holds either -- for example
because it just read a parameter tree or resolved a secret under its own
role -- passes it directly. Anything not passed falls back to the
standard ``PG*`` environment variables (``PGHOST``, ``PGPORT``,
``PGDATABASE``, ``PGUSER``, ``PGPASSWORD``), and if ``RAPID_DB_SECRET_ID``
is set in the environment and no explicit ``credentials=`` was given, the
credential is instead resolved from that AWS Secrets Manager secret via
``credentials_from_secret()``. A credential is never expected as a
command-line argument or read from a file committed to the repository.

**Bounded connect retry.** Connecting retries on ``psycopg2.OperationalError``
only, up to ``attempts`` times (default ``DEFAULT_CONNECT_ATTEMPTS = 5``),
with exponential backoff starting at ``backoff_initial``
(``DEFAULT_BACKOFF_INITIAL_S = 0.5`` seconds), doubling by
``backoff_multiplier`` (``DEFAULT_BACKOFF_MULTIPLIER = 2.0``) each attempt
up to a cap of ``backoff_cap`` (``DEFAULT_BACKOFF_CAP_S = 8.0`` seconds).
Passing ``jitter=True`` replaces each wait with a random duration in
``[0, delay]`` so that many callers retrying off the same event do not
stay synchronized. Every one of these is an ordinary keyword argument a
caller can override.

**TCP keepalives.** Every connection this module opens sets keepalive
parameters (``KEEPALIVES``, ``KEEPALIVES_IDLE_S``,
``KEEPALIVES_INTERVAL_S``, ``KEEPALIVES_COUNT``) and a
``TCP_USER_TIMEOUT_MS`` backstop, sized so a vanished peer is detected at
the socket in about a minute (30 seconds before the first probe, then up
to three probes ten seconds apart), rather than the kernel's own default
of two hours.

**Identification and timeout.** Every connection carries an
``application_name`` (truncated to PostgreSQL's 63-byte
``NAMEDATALEN - 1`` limit, visibly, before it reaches the server) and a
``connect_timeout`` (default ``DEFAULT_CONNECT_TIMEOUT_S = 10`` seconds)
bounding how long a single connection attempt can take.

What it deliberately does not do
************************************************

It does not reconnect and retry around a statement that was already in
flight when the connection died. The connect-retry described above
covers only the act of connecting; a connection that was established and
healthy, then closed mid-statement, is a server-side or pooler fault to
be diagnosed and fixed there, not papered over on the client. Hiding it
behind a silent retry would be actively harmful at the pipeline's
operating scale, where on the order of a thousand concurrent jobs may
each hold a connection: a pooler that is dropping payload connections
needs to be visible and fixed, not masked one retry at a time.

It carries no pooler configuration, connection "lanes", or ``SET ROLE``
role-widening bookkeeping. Pgbouncer (or any pooler) is server-side
infrastructure, provisioned and configured by ``rapid_systems``, per the
repository boundaries in the
`RAPID specification <https://roman-rapid.readthedocs.io/en/latest/system/specification.html>`_
("Repositories"). This module only ever asks the pooler's, or the
database's, own listening port for one connection at a time.

The repository layer, ``rapidpipe.runs.repository``, opens no
transactions of its own beyond what this module provides: each of its
functions is documented as exactly one transaction per call, using
``transaction()`` below.

Two incidents behind the design
************************************************

**August 2026.** A transaction-pooled pgbouncer instance began closing
the pipeline's freshly opened connections at ``age=0`` seconds with a
``client_idle_timeout`` closure, even though the pipeline's own database
user had no per-user timeout setting and the pooler's global
``client_idle_timeout`` was disabled. The cause turned out to be a
``client_idle_timeout`` value set on per-user configuration lines
belonging to a handful of other, human-operator database users; removing
those lines from the affected users stopped the closures immediately and
completely. That a per-user setting on unrelated users reached a user
with no per-user line of its own at all is recorded as unverified against
the pgbouncer issue tracker. The transferable lesson: a healthy
connection closed while a statement was in flight is a pooler defect, to
be diagnosed from the pooler's own log, not a reason to add
reconnect-and-retry on the client. The pipeline's fail-loud, nonzero exit
in the face of that closure was the correct behavior, and stayed
correct.

**September 2026.** A database host was replaced while a long-lived
client connection was still pointed at the address it had been using.
With no keepalives set, the client had no way to distinguish "quiet
connection" from "connection to a peer that no longer exists," and the
condition went undetected for hours, bounded only by the kernel's
default keepalive timeout. The lesson embodied in this module's
keepalive defaults: a client should set keepalive parameters on every
connection it opens so that a vanished peer is noticed at the socket in
about a minute. This is dead-peer detection at the socket level, wholly
distinct from statement-level retry; it says nothing about, and does
nothing for, a connection that a pooler actively closes while healthy,
which is the first incident above.

Diagnosing a dropped connection
************************************************

#. The caller sees ``psycopg2.OperationalError`` (during connect, wrapped
   by this module as ``ConnectionUnavailable`` once the retry budget is
   exhausted) or a driver error surfaced mid-statement on an already
   established connection.

#. Look first at the pooler's own log for the connection's close reason;
   the reason string (for example ``client_idle_timeout`` versus
   ``client close request``) is diagnostic and is not visible from the
   client side.

#. A connect-time failure is retried automatically, bounded, with
   backoff; if every attempt fails, ``connect()`` raises
   ``ConnectionUnavailable`` naming the endpoint, user, and attempt
   count.

#. A close of an already-open connection, in the middle of a statement,
   is never retried by this module; it propagates as a driver exception.

#. Ruling out a vanished peer is a keepalive question, not a pooler-log
   question: with the defaults above, a truly vanished peer is detected
   within roughly a minute, so a hang longer than that points elsewhere.

#. Pooler configuration, including per-user settings and timeouts, is
   owned and changed in ``rapid_systems``, never in this repository.
