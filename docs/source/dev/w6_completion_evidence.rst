W6: the completion chain proven live
====================================

The reconciler, the records-consuming registration path, and the cutover
fence, each proven against real Batch, real S3, real IAM and the real
database on 2026-08-06.

The pooler, first
-----------------

W5's canary could not demonstrate a clean full lifecycle: pgbouncer was
closing payload connections mid-statement with ``client_idle_timeout
(age=0s)``. That is resolved and verified — zero closures against 17+
``rapid_pipeline`` connections after the fix, where there had been six
before, all of them ``rapid_pipeline``. Details, including the cause (a
per-user setting on five *human* logins reaching a user that has no
per-user line at all) in ``pooler_client_idle_timeout.rst``.

Every proof below depends on that, and none of them was possible before it.

The reconciler against the live backlog
---------------------------------------

One cycle over the real attempt table drained everything W1, W2 and W5 had
left open: **17 open attempts to 0**, and **22 scheduler observations
recorded where there had never been a single one** — no reconciler had ever
run against this database.

Two states are worth naming, because they are the ones the design's ordering
exists to produce:

**Attempt 32** — the W5 canary's ``started`` row beside a valid terminal
record, left when the pooler killed the connection between the record write
and the application-closed transition. The reconciler materialized it to
``terminal_after_start``, carrying the scheduler's exit 70, at sequence 1.
The protocol's two-store ordering worked exactly as specified under a fault
it did not cause.

**Attempts 35-37** — scheduler ``SUCCEEDED`` and exit 0 beside application
``rapid_outcome=failure``. The representable combination the whole taxonomy
was built for, preserved rather than collapsed to one status.

Six defects the live run found
-------------------------------

Each is now a regression test. They are listed because every one passed the
unit suite first:

1. A ``submitted`` row must have ``scheduler_state IS NULL``. Writing the
   scheduler's verdict beside a row that still claims nothing started
   violates the DDL — correctly.
2. A failed statement poisons the whole PostgreSQL transaction. Without a
   rollback in the per-attempt handler, one bad row became twelve
   ``InFailedSqlTransaction`` failures.
3. Closure records must be **byte-identical** for a given classification. A
   wall-clock ``reconciled_at`` made every replay a different object, so
   create-once refused it — idempotence inverted into a hard error.
4. A sequence already holding a *different* account is supersession, not
   failure: the new account goes to the next free sequence.
5. That supersession check first matched only the in-memory store's
   structured details. It passed the suite and did nothing in production,
   because ``S3ObjectStore`` reports the same condition differently.
6. "Did this attempt run?" cannot be read off the scheduler alone. A
   job-scoped observation can report a start belonging to a different
   attempt, and an application-observed attempt index means the application
   claimed the row from inside a running container.

The end-to-end proof
--------------------

One array, two children, submitted through the real manifest/array path with
attempt rows pre-created **before** the children could start.

============================  =============================================
Both children                 SUCCEEDED, exit 0, **one attempt each** — the
                              retry contract holding on a clean application
                              failure
Rows per child                **exactly one.** The pre-created rows were
                              *claimed*, not duplicated
Artifacts                     sequence-0 records in ``roman-rapid-records``,
                              bundles in ``roman-rapid-diagnostics``
Before the horizon            reconciler recorded scheduler observations and
                              **deferred** classification
After the horizon             classified 2, closure records written 2,
                              retention stamped 2, **errors 0**
Final state                   ``terminal_after_start``, sequence 1,
                              application ``failure`` beside scheduler
                              ``SUCCEEDED``
============================  =============================================

The claimed-not-duplicated row is worth dwelling on. Pre-creation and the
runtime must derive the same ``logical_job_id`` — the manifest unit's key,
``<exposure>/<sca>`` — or ``resolve_attempt`` cannot match the pre-created
row, every child creates a second one, and every pre-created row is orphaned
in ``submitted`` until a horizon classifies it as a lost child. An earlier
probe reproduced exactly that by submitting before the rows existed.

Registration as a consumer
--------------------------

The candidate query — terminal lifecycle **and**
``terminal_record_sequence >= 1`` — run against the live database:

- **14 candidates**, up from 12 as the two end-to-end attempts were
  reconciled
- **all 14 refused** by taxonomy, because every one is
  ``rapid_outcome=failure``. Batch reported ``SUCCEEDED`` and exit 0 for
  them; the log-grep chain would have registered their products
- **0 held back** at the end, having been 2 while reconciliation was pending

That middle row is the whole point of the gate. Nothing parses a log, and no
``.done`` file is written or read anywhere in the chain.

Retention tags
--------------

Stamped on real bundles as a canonical full set — ``retention-class``,
``attempt-id``, ``producing-release`` — not a delta, because S3's tagging
API replaces the whole set. ``failure`` is the correct class here: the
application failed under a ``SUCCEEDED`` scheduler state, and the
diagnostics for a failure must not expire on the success clock.

The fence
---------

Executed in the ratified order, evidence captured at each step: legacy
submission inhibited **first** (nine launchers deleted, 3,684 lines), then
quiesce proven at both ends — zero nonterminal Batch children across all
five states in both queues, and the legacy ``Jobs`` table verified to hold
zero rows at all — then the legacy readers deleted (four registration
scripts, 2,850 lines).

Post-fence greps: zero legacy launchers, zero registration scripts, zero
code searching for ``terminating_exitcode``, zero done-file writes in the
chain, zero ``.sh`` wrappers.

Two deletions the fence's own conditions refused, both recorded rather than
forced: the master ``.ini`` has 23 surviving readers outside the completion
chain, and ``RomanTessellationNSIDE512`` has 11 surviving constructors, not
zero. See ``config_homes.rst`` and ``tessellation_bake_retirement.rst``.

What is not proven here
-----------------------

- **The reconciler as a running service.** It has a systemd unit
  (rapid_systems ``rapid-reconciler-service.yaml``) and it is deployed
  disabled, because ``rapid_orchestrator`` does not exist as a PostgreSQL
  role — see ``016-orchestrator-service-role.sql``. Every cycle above was
  driven as a one-shot under ``rapid_pipeline``.
- **A scheduler-retry child.** No pull failure was forced, so the
  attempt-index derivation is proven by unit test and by its
  single-attempt live behaviour, not against a real ≥2-attempt job. That
  belongs in W8's battery.
- **A successful registration.** All live attempts are application
  failures, so the refusal path is proven and the register path is not.
