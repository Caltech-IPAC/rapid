pgbouncer closes RAPID payload connections at age=0s
====================================================

**Status: OPEN, found live 2026-08-06 by the W5 canary. Not a payload
defect — a pooler configuration defect. Routed to the database/operations
owner; W6's reconciler and W8's live battery both depend on it.**

What happens
------------

A Batch job's connection to the pooler is accepted, authenticates, serves
one or two statements, and is then closed by pgbouncer mid-sequence::

    C-0x...: rapid/rapid_pipeline@10.100.161.70:37860 login attempt:
             db=rapid user=rapid_pipeline tls=no replication=no
    C-0x...: rapid/rapid_pipeline@10.100.161.70:37860 closing because:
             client_idle_timeout (age=0s)
    C-0x...: rapid/rapid_pipeline@10.100.161.70:37860 pooler error:
             client_idle_timeout

``age=0s``. The client had been connected for under a second and was in
the middle of a statement sequence.

The application sees ``psycopg2.OperationalError: server closed the
connection unexpectedly``, and — correctly — exits nonzero, because a
records-path failure is the one case the fail-loud posture reserves a
nonzero exit for.

Reproduced on two consecutive canary submissions:

===============  ==========  ================================================
Job              Attempt     Where the connection died
===============  ==========  ================================================
3a769712         31          between ``resolve_attempt`` and the started CAS
24729ce6         32          at the application-closed CAS, after the
                             terminal record was written and validated
===============  ==========  ================================================

Attempt 32 is worth reading twice: it is the exact recovery state the
termination protocol's ordering was designed to produce. The S3 terminal
record is written BEFORE the application-closed database transition, so a
failure between them leaves *a started row beside a valid record*, which
the reconciler materializes from the record. The design worked. The pooler
is what failed.

Why it did not appear before
----------------------------

W2's live ownership proof and W1's suites ran **on rapid-db itself**,
connecting to the pooler over ``127.0.0.1``. The W5 canary is the first
client to reach the pooler **across the VPC** from a Batch host, which is
how production will always reach it.

Connections from rapid-admin in the same window close cleanly::

    10:22:12 ... 10.100.151.86:40264 closing because: client close request (age=0s)
    10:26:47 ... 10.100.151.86:57094 closing because: client close request (age=0s)

while the two Batch-host connections in that same window both hit
``client_idle_timeout``. Same user, same database, same pool mode.

What the configuration says
---------------------------

``/etc/pgbouncer/pgbouncer.rapid.ini`` sets ``pool_mode = transaction``
globally and lists per-user overrides only for the five named human users,
each with ``client_idle_timeout=7200``. ``rapid_pipeline`` is deliberately
absent from ``[users]`` — the file's own comment says "automated jobs
(rapid_pipeline) are NOT listed", which is the intended design: they take
the global transaction-pooled defaults.

So the global ``client_idle_timeout`` is what applies to payload
connections, and pgbouncer's own default for it is ``0`` (disabled). The
commented template line in ``/etc/pgbouncer/pgbouncer.ini`` also reads
``;client_idle_timeout = 0``. **A disabled timeout cannot fire, and it is
firing** — so the running value is not the file value, or the timer is
being applied against something other than idle time. Determining which
needs ``SHOW CONFIG`` on the pgbouncer admin console, which needs the
admin credential this investigation did not have.

Two candidate explanations, neither verified:

1. The running configuration differs from the file on disk — a reload
   never happened after an edit, or an edit landed in one of the two
   ``.bak`` siblings and the live file was reloaded from something else.
2. ``client_idle_timeout`` is set globally somewhere not grepped, with a
   value small enough that a sub-second client trips it. That would still
   be wrong for the payload, whose connections are short but not
   instantaneous.

What it blocks
--------------

The canary cannot demonstrate a full clean lifecycle while this stands.
Everything up to and including the terminal record is proven live (see the
W5 ledger); the application-closed transition is not, because the
connection does not survive to make it.

It also blocks W6 and W8 directly: the reconciler is a polling service
holding pooled connections, and W8's battery asserts on exactly the
lifecycle transitions that are failing here.

Not to be worked around
-----------------------

The tempting fix — a reconnect-and-retry around the attempt writer's calls
— would be wrong here. The connection helper already has bounded connect
retry with backoff (W1); what is failing is not the connect but an
established connection being closed under an in-flight statement. Papering
over that would hide a pooler that drops payload connections, at a scale
where ~1,000 concurrent jobs will each hold one.
