pgbouncer closes RAPID payload connections at age=0s
====================================================

**Status: RESOLVED on the live host 2026-08-06 (W6) and SYNCED to the
packaged config the same day (W6b, rapid-pgbouncer 1.0-4) — the drift is
closed at both ends. Found live by the W5 canary. Not a payload defect —
a pooler configuration defect. The upstream question below remains
UNVERIFIED against the pgbouncer issue tracker.**

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
firing.**

This section originally offered two candidate explanations — a stale
running configuration, or an ungrepped global setting. **Both were wrong,
and the answer turned out to be neither** (W6, 2026-08-06): the running
configuration matched the file, and there was no global setting anywhere.
What stopped the kills was removing ``client_idle_timeout`` from the five
per-user lines belonging to *other* users — the human logins — none of
which ``rapid_pipeline`` matches. See "The fix, and what is still owed"
below. Kept here rather than deleted because the reasoning is a fair record
of what the evidence supported at the time, and because "the obvious two
explanations were both wrong" is worth knowing next time.

What it blocks
--------------

The canary cannot demonstrate a full clean lifecycle while this stands.
Everything up to and including the terminal record is proven live (see the
W5 ledger); the application-closed transition is not, because the
connection does not survive to make it.

It also blocks W6 and W8 directly: the reconciler is a polling service
holding pooled connections, and W8's battery asserts on exactly the
lifecycle transitions that are failing here.

**Unblocked 2026-08-06.** W6's probe children reached
``application_closed`` — the transition this defect was preventing — and
the reconciler then ran a full cycle over live attempts with zero
connection failures. The clean full lifecycle the W5 evidence listed under
"what is not proven" is now proven.

Not to be worked around
-----------------------

The tempting fix — a reconnect-and-retry around the attempt writer's calls
— would be wrong here. The connection helper already has bounded connect
retry with backoff (W1); what is failing is not the connect but an
established connection being closed under an in-flight statement. Papering
over that would hide a pooler that drops payload connections, at a scale
where ~1,000 concurrent jobs will each hold one.

The fix, and what is still owed
-------------------------------

**What was wrong.** ``client_idle_timeout=7200`` was set on five *per-user*
lines in ``/etc/pgbouncer/pgbouncer.rapid.ini`` — and every one of those
five is a **human** login (bostroem, everetts, laher, rivera, rusholme).
``rapid_pipeline`` has no per-user line at all; the file's own comment says
automated jobs are deliberately not listed, and there is no
``client_idle_timeout`` in the ``[pgbouncer]`` global section either.

So a setting written for five human sessions was killing the payload's
connections. That per-user settings on *other* users affect
``rapid_pipeline`` at all looks like an upstream pgbouncer defect —
**labelled unverified: not checked against the pgbouncer issue tracker.**
Anyone adding a per-user line should check the tracker first.

**The change**, applied live at 10:49Z (backup:
``pgbouncer.rapid.ini.pre-idle-fix.bak``), was exactly and only the removal
of ``client_idle_timeout=7200`` from those five lines. The per-user
``idle_transaction_timeout=900`` entries were left alone, and did not need
to be touched.

**The evidence** (W6, 2026-08-06):

============================  ==========================================
Before the fix                6 ``client_idle_timeout`` closures in the
                              journal, **all** of them ``rapid_pipeline``;
                              last at 10:31:22Z
After the fix                 **ZERO** closures, against 17+
                              ``rapid_pipeline`` connections in the same
                              window — so the zero is not vacuous
Connection close reason       now ``client close request`` (the client's
                              own disconnect), never ``client_idle_timeout``
Probe children                SUCCEEDED, where the canary hit exit 70
============================  ==========================================

The probe reused the canary's submission shape — short-lived VPC clients
through the pooler as ``rapid_pipeline`` — spread across submissions at
11:13, 11:15, 11:28, 11:33 and 12:30, comfortably past the fifteen minutes
required to call it.

**The gap that remained, now closed (W6b, 2026-08-06).** The live file was
fixed and the *packaged* one was not: ``rpm -V rapid-pgbouncer`` reported
``S.5....T.`` on ``/etc/pgbouncer/pgbouncer.rapid.ini``, so a package
reinstall or a rebuilt host would have silently restored the defect. The
same removal is now in the rapid_systems pgbouncer source, published as
``rapid-pgbouncer`` 1.0-4 through the promoter and installed on rapid-db.
Evidence in the W6b ledger.

Publishing it surfaced a second, unrelated defect worth knowing about,
because it made the drift **unclosable until fixed**: ``build-rpms.yml``
verified its baseline package set before pruning duplicates, so the
release bump broke main, and the promoter refuses to publish while a
newer main run is red. A red main blocked the release that would clear
it. See ``w6b_state_summary.rst``.

**What is not closed** is the upstream question. That a per-user
``client_idle_timeout`` on five *human* logins reached ``rapid_pipeline``
— a user with no per-user line at all — is still **unverified against the
pgbouncer issue tracker**. The packaged config now carries that warning
inline, where someone would re-add the setting.
