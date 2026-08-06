Attempt-writer no-incumbency review (W1, 2026-08-06)
====================================================

Verdict: **keep and reshape.** ``observability/attempts.py`` is retained,
amended to the ratified schema, and given the live executor it always
lacked. It is not replaced.

Why this review exists
----------------------

The Batch payload co-design (ADOPTED, Ben 2026-08-05) states the
no-incumbency principle plainly: *"Existing code has no incumbency.
Structure is judged against these principles regardless of provenance or
session of origin; the built-but-unwired target modules are candidates,
not defaults."* ``observability/`` is named in that sentence — built at
Q4/Q5, never wired to a production call site, zero live writes.

W1's brief therefore required this module to be re-argued on merit before
anything was built on it, with the verdict recorded. That is what follows.

What the module is
------------------

``AttemptWriter`` over a single injected ``execute(sql, params)``
callable, plus frozen dataclasses for identity, provenance and stages, and
enums whose values match the DDL's CHECK vocabularies. Around 500 lines,
with a 395-line unit-test suite that drives it through a recording fake.

The case against keeping it
---------------------------

Taken seriously, four charges:

1. **It was written against the pre-amendment schema.** It knows five
   lifecycle states, one ``process_exit_code``, no attempt index, no
   execution binding, no terminal-record reference, no resolver. Roughly
   half the amended surface is missing.

2. **It has never run.** Migration 012's own header records that the
   grants were absent, so *"attempt-record emission could never have
   written a record"*. Every line of it is untested against a real
   database.

3. **Its ``Executor`` protocol had no implementation.** A boundary with
   no concrete side is a design sketch, and a sketch that has never met a
   real driver is where the optimistic assumptions hide.

4. **Some methods are loose.** ``backfill_scheduler_job_ids`` counts
   statements issued rather than rows affected, and returns that as if it
   were a fact. Several ``UPDATE``s do not check that they matched a row,
   so an update against a nonexistent ``attempt_id`` is a silent no-op.

The case for keeping it
-----------------------

Charges 1 and 2 are arguments for *amending* it, not for discarding it —
they describe work any replacement would also have to do, from a worse
starting point. The question is only whether its structure survives the
ratified design. It does, on four counts:

1. **The injected-callable boundary is exactly what the design wants.**
   The design asks for one connection helper that the writer sits above,
   with errors raised rather than flagged. ``AttemptWriter`` already takes
   its database as a callable and already raises; nothing in it swallows
   an error into a flag, calls ``exit()``, or manages a transaction it
   should not own. A replacement written to the design would arrive at
   the same shape.

2. **Every value is parameterized already.** There is no string
   interpolation of a value into SQL anywhere in the module — the thing
   the design prohibits repo-wide and the thing ``rapid_db.py`` does
   pervasively. This module is the standard the sweep is converting
   *towards*, not one of the sites needing conversion.

3. **The absent-not-sentinel discipline is enforced and tested.** Fields
   a state has not reached are omitted, and the tests assert their
   absence by name (``assertNotIn(column, sql)``). That is the discipline
   the amended CHECK matrix depends on, held from the writer's side.

4. **The tests are real tests.** They assert emitted SQL and parameter
   values against a recording fake, not merely that the code runs.
   Discarding the module discards them too.

Charge 3 is not a defect in the module — it is the missing piece W1 was
scoped to supply, and supplying it is cheaper than rewriting the writer
around a different boundary.

Charge 4 is real and is addressed below rather than used to condemn.

What was changed
----------------

- ``SCHEMA_VERSION`` 1 → 2, matching migration 013. This matters: 013's
  amended constraints gate their new requirements on
  ``schema_version >= 2``, so declaring 2 is a promise the writer now
  keeps.
- ``LifecycleState`` gains ``APPLICATION_CLOSED``.
- ``ExecutionBinding`` added, deliberately separate from ``Provenance``:
  the binding is submission-authored and copied at creation, the
  provenance is the runtime's own startup observation. They are different
  facts by different writers, and disagreement between them is a
  reconciliation signal rather than a duplicate to collapse.
- ``create_logical_job`` records the binding at logical-job scope,
  idempotently — a replayed submission cannot rewrite what a running
  attempt believes it is executing.
- ``resolve_attempt`` calls migration 013's resolver function. This is
  now the only sanctioned acquisition path; neither writer bare-INSERTs.
- ``create_submitted`` requires a binding at version ≥ 2, checked locally
  so the failure names the missing thing instead of arriving as a
  constraint violation after a round trip.
- ``mark_application_closed`` added — the application's own closing
  transition, citing the S3 terminal record it has already written.
- ``mark_terminal_after_start`` reshaped into the **reconciler's**
  transition: it now requires the scheduler-observed exit and scheduler
  state, and applies the application-authored fields with ``COALESCE`` so
  a reconciler pass can never overwrite what the application authored.
  This is the sharpest change — the method kept its name but changed
  owner, which the docstring states outright so no caller assumes the old
  meaning.
- ``mark_abrupt_loss`` writes the scheduler-observed exit and leaves the
  application-intended exit NULL. The application never stated an intent;
  NULL says that, where the old single column forced a fabricated value.
- ``_validate_error_category`` added against the v1 allowlist, mirroring
  the database's foreign key as an early local failure.

What was NOT changed, and why
-----------------------------

The charge-4 looseness (``backfill_scheduler_job_ids``'s misleading count;
unchecked ``UPDATE`` row counts) is **left in place and recorded here**.
Both are real, both are worth fixing, and neither is in W1's scope: they
are pre-existing behaviour on paths the amendments do not touch, and
changing them would mean changing what existing callers observe without a
test to pin the new behaviour. Recorded for the owner rather than fixed
silently in a migration-focused worker.

The recommendation, when someone picks it up: have the executor return
``cursor.rowcount`` for non-RETURNING statements and have the transitions
raise when an update matches zero rows. A lifecycle transition against a
nonexistent attempt is a bug in the caller every time, and it should not
be able to look like success.

Live observation for the owner: an intermittent pooler disconnect
---------------------------------------------------------------

While proving the live round-trip against rapid-db, one run died mid-way
with ``psycopg2.OperationalError: server closed the connection
unexpectedly``, immediately after ``mark_started``. The identical run
succeeded on the next attempt and on every attempt since, so it is
intermittent, not deterministic.

The pooler log names the cause:

.. code-block:: text

   C-0x...: rapid/rapid_pipeline@127.0.0.1:54278 closing because:
            client_idle_timeout (age=0s)
   C-0x...: rapid/rapid_pipeline@127.0.0.1:54278 pooler error:
            client_idle_timeout

That is anomalous on two counts. ``client_idle_timeout`` is not set for
``rapid_pipeline`` — the live ``/etc/pgbouncer/pgbouncer.rapid.ini`` sets
it only in the five per-user ``[users]`` lines for the human roles, and
the global default is 0 (disabled), which is correct per the adopted
two-lane posture. And ``age=0s`` is not an idle connection by any
reading.

Recorded rather than chased: the pooler configuration is rapid_systems
infrastructure, outside W1's schema-and-helper scope, and changing live
pooler settings is not something this worker does unattended. It is worth
someone's attention before the smoke run, because a pipeline connection
dropped between statements is exactly the failure mode the bounded
connect retry in ``rapid_db_connect.connect`` does *not* cover — that
retry wraps the initial connect, not a mid-transaction loss. If this
recurs at scale, the helper may need a retry-the-transaction wrapper as
well, which is a design question rather than a bug fix.
