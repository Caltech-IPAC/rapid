W5 canary: the first live proof of the new execution surface
============================================================

Four submissions on 2026-08-06, each one finding something. The surface is
the entrypoint, the route matrix, attempt ownership, the configuration
snapshot, and the termination protocol — everything W5 replaced, proven
against real Batch, real S3, real IAM and the real database.

The job type is ``registration``, chosen as the trivial unit: it is routed
and validated exactly like a science job and exercises every operational
step, then raises immediately, because W5 deliberately does not dispatch it
(its record-consuming implementation is W6's, behind the cutover fence). It
therefore closes with a *classified application failure* —
scheduler-SUCCEEDED with application-failure, the representable combination
the whole design was built for.

The four submissions
--------------------

**1. d3bc7568 — exit 70, found the permissions boundary.**
Reached attempt resolution (attempt 29, "claimed pre-created row") and
failed persisting the configuration snapshot::

    AccessDenied ... not authorized to perform: s3:PutObject on
    "arn:aws:s3:::roman-rapid-records/attempts/config-snapshots/..."
    because NO PERMISSIONS BOUNDARY ALLOWS the s3:PutObject action

The job role's own grant had already been corrected in this branch; the
same stale bucket name (``rapid-observability-<account>-<region>``, which
does not exist) was also in ``rapid-service-identity-boundary``, which caps
every service identity. Fixed and applied live.

**2. 9cfd64d8 — SUCCEEDED, exit 0. The full lifecycle.**
Every step, in the design's order, from one log stream::

    environment: job=... attempt=1 queue=rapid-queue-prompt batch=... manifest=s3://...
    route validated: job_type=registration class=prompt queue=rapid-queue-prompt lane=transaction unit=0/2
    database endpoint from the parameter tree: ...:6432/rapid (secret rapid/db/service/pipeline)
    working directory: /tmp/rapid/9cfd64d8-...-attempt-1
    resolving attempt ownership: job=... index=1 logical_job=0/2
    attempt ownership resolved: attempt 30 (0/2 index 1, claimed pre-created row)
    configuration snapshot attempts/config-snapshots/sha256/2fc1713a...json (created) digest 2fc1713a3aec
    attempt 30 started, bound to config 2fc1713a3aec (attempts/config-snapshots/...)
    application failure (config_invalid): the registration job type has no payload yet...
    diagnostics bundle attempts/bundles/.../attempt-30.tar.gz (uploaded), 45 bytes, sha256 8acfcd4e6ef9
    terminal record attempts/records/.../attempt-30/seq-0000.json (written), sha256 d2f051022e88
    attempt 30 closed: outcome=failure disposition=none intended_exit=0
    terminated: outcome=failure disposition=none record=... exit=0

Batch reported SUCCEEDED, exit 0, **one attempt** — no retry on a clean
application failure, which is the retry contract holding.

The sequence-0 record is a complete self-contained account: identity,
provenance (source sha, container digest, job-definition name, config
digest), the snapshot key, the bundle cited by checksum,
``rapid_outcome=failure``, ``product_disposition=none``,
``application_intended_exit=0``, ``error_category=config_invalid``.

**It also found a bug.** The record was written to the right key in the
wrong bucket — ``roman-rapid-diagnostics`` instead of
``roman-rapid-records`` — because ``terminate()`` took one store and used
it for both artifacts. Every log line and every key assertion looked
correct; only listing both buckets showed it. Fixed (``record_store``
parameter, two regression tests).

**3. 3a769712 — exit 70, found the pooler.**
``client_idle_timeout`` closed the connection between ``resolve_attempt``
and the started CAS. See ``pooler_client_idle_timeout.rst``.

It did prove one thing on the way: the configuration snapshot deduped —
``(already present)``, the same digest ``2fc1713a3aec`` as attempt 30's.
Content-addressing works, and a thousand array children will persist one
snapshot between them.

**4. 24729ce6 — exit 70, same pooler cause, one step later.**
The connection died at the *application-closed* transition, after the
terminal record was written and validated::

    the terminal record for attempt 32 is written and valid at
    attempts/records/.../attempt-32/seq-0000.json, but the
    application-closed transition failed: server closed the connection
    unexpectedly

That is precisely the recovery state the protocol's ordering exists to
produce — a started row beside a valid record, which the reconciler
materializes from the record. The runtime exited nonzero because it could
not itself confirm closure. The design behaved as specified under a real
fault it did not cause.

It also confirmed the two-store fix live. One listing shows before and
after::

    roman-rapid-records/attempts/records/.../0_4/attempt-32/seq-0000.json   (after)
    roman-rapid-diagnostics/attempts/bundles/.../0_4/attempt-32.tar.gz      (after)
    roman-rapid-diagnostics/attempts/records/.../0_2/attempt-30/seq-0000.json (before)

What is proven
--------------

- The entrypoint's whole startup sequence, in order, against real
  infrastructure: environment contract, manifest fetch and checksum gate,
  full route validation (job type × class × queue × lane), the parameter
  tree read under the job role, the database endpoint bridge.
- Attempt ownership through W1's resolver, claiming the pre-created row.
- The configuration snapshot: created once, content-addressed, deduped on
  the next attempt, bound into the started CAS.
- The termination protocol end to end (submission 2): bundle, terminal
  record, application-closed transition, exit 0.
- Structured logging with job and attempt identifiers on every line,
  delivered to CloudWatch by the awslogs driver with no wrapper and no
  hand-uploaded logfile.
- The retry contract: one attempt, no retry, on a clean application
  failure.
- The records and diagnostics buckets receiving their own artifacts.

What is not
-----------

A clean full lifecycle on the *current* image. Submission 2 proved the
lifecycle but predates the two-store fix; submissions 3 and 4 carry the fix
but are cut short by the pooler. Re-running the canary once
``client_idle_timeout`` is resolved closes that gap, and is the first thing
W6 should do.
