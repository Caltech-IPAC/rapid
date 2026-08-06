W9prep: current state, as the next worker inherits it
=======================================================

An inventory, not a narrative: what is true of the live system on
2026-08-06 after W9prep's rebuild, repin and canary re-proof. Everything
below was observed, not inferred. Supersedes ``w8_state_summary.rst``'s
"What is still owed" list where noted; leaves the rest of that document's
history untouched.

The image and the pins
-----------------------

=========================  ===================================================
Deployed digest            ``sha256:3bcd8978d263ff0010123269582ceb2dce86a04041e0ea03551a62c85afb9145``,
                            tag ``f0d7039-20260806``, built from smdc
                            ``f0d7039d05ae93bbb7bba6b142d69d810fdb8c1d``
Scan gate                  HIGH 3 / MEDIUM 5 / LOW 1 — identical to the
                            rev-12 baseline by CVE identity, zero CRITICAL
Job definitions             ``rapid-pipeline-science`` and
                            ``rapid-pipeline-bulk``, **both revision 13**,
                            both pinned to that digest
Quiesce                     proven before the revision: zero children in any
                            of SUBMITTED/PENDING/RUNNABLE/STARTING/RUNNING on
                            both queues
=========================  ===================================================

**The image now carries both fixes W8 left uncommitted-to-image:**
``tessellation_provenance``'s import fix (``df214ff``) and the
reconciler's ``reconciler_materialized`` flag-clearing fix (``aa067cc``).
Both are proven live, not merely present in source — see below.

Canary: the registration lifecycle W8 could not close
-------------------------------------------------------

W8's own live proof (``w8-live-20260806T170745Z``) died on
``ImportError: cannot import name 'RomanTessellation'`` before reaching
the register step. This run repeated the identical canary
(``pipeline/registration/test/live_w8_registration.py``, the production
seam, ``seams.submit_units``) against the rev-13 image. **It SUCCEEDED —
the rev-12 blocker is gone.**

Job ``4b251fba-00bf-4623-aa88-129e35661cda``, run
``w8-live-20260806T182925Z``, attempt 118, exit 0:

* route validated — ``job_type=registration class=prompt
  queue=rapid-queue-prompt lane=transaction``;
* the pre-created row claimed (attempt 118);
* configuration snapshot bound in the started CAS;
* tessellation resolved cleanly — ``nside512-v2 pinned by release
  content`` — no crash;
* the registration pass actually ran: 16 reconciled attempts evaluated,
  all 16 correctly REFUSED by taxonomy (each a genuine prior application
  failure), ``registration pass: {'registered': 0, 'skipped': 16, ...
  'exit_code': 0}``;
* terminal record ``seq-0000.json`` written, attempt 118 closed
  ``outcome=success disposition=published``.

Verified live in the database: ``lifecycle_state=application_closed``,
``terminal_record_sequence=0``, ``registered_record_sequence=NULL``,
``reconciler_materialized=false``. Sequence-0 is the application's own
record; the row correctly stays outside the registration consumer's
candidate pool (``RECONCILED_STATES`` names only
``terminal_after_start``/``terminal_without_start``, not
``application_closed``) unless the reconciler later supersedes it — which
a clean success never needs.

**"A successful registration is still owed"** (W8's own words) is
therefore closed: this is that successful registration.

Why the canary was safe to re-run against a fully-registered g0001
---------------------------------------------------------------------

Checked before running, not assumed: ``registrar_for()`` in
``pipeline/entrypoints/job.py`` returns ``None`` unconditionally — no
science-layer registrar is wired into this tree yet, anywhere. That
forces ``register_batch(..., register=None, dry_run=True)`` regardless of
how many candidates are found, so **every registration run in this image
is a decision-only pass**: no ``addL2File``/``L2FileMeta`` write is
reachable. Re-running the identical canary against the already-registered
g0001 population carried zero duplication risk — this is the "battery's
no-op registration shape" by construction, not by a special flag.

The reconciler: agreed, but still on the OLD image
-----------------------------------------------------

**Not fixed by this run, flagged rather than silently worked around.**
The ``rapid-reconciler-service`` CloudFormation stack pins its own image
by a separate parameter (``ImageRef``), deliberately independent of the
Batch job definitions — the reconciler is designed to detect version skew
between itself and the payload, so its pin is not meant to move in
lockstep automatically. This run's authorized scope named the two Batch
job definitions specifically, not this stack, so it was left alone.

Consequence, observed live during this session: the running reconciler
(still rev-12 code, ``sha256:8c10d1e3…``) hit the exact
``attempts_reconciler_materialized_check`` CheckViolation on attempt 112
(a ``w8-battery`` row) on every 60s poll — the fix for this
(``aa067cc``) is confirmed present and correct in the new rev-13 image
(the SQL unconditionally clears the flag, verified by reading
``observability/attempts.py``), but the reconciler service itself hasn't
been restarted onto it. **Proposed:** a follow-up authorized to touch
``rapid-reconciler-service`` should redeploy it with ``ImageRef`` pointed
at the rev-13 digest.

Attempt 118 (this run's own canary) was unaffected by this — it never
entered the reconciler's unresolved set, since the application closed it
directly before any poll needed to intervene.

Suites
------

All in-image on the rev-13 digest, exit 0 on all eight: **808 tests
green** (296+7st runtime, 103 reconciler, 22 entrypoints, 74+40st stages,
529+47st pipeline, 151 submission, 18 registration, 129+30st
observability) — same suite shape and count as W8's own run.

Validators
----------

``rapid_systems/cloudformation/validate.sh`` (all layers): PASS, exit 0.
Pre-existing ``docs hygiene: WARN`` lines on unrelated register entries
(line-count budgets) are untouched by this run.

What is still owed
-------------------

Carried forward unchanged from W8, not re-investigated this run (outside
scope):

* the pooler RPM promoter publish (blocked on the CI runner-registration
  gap noted 2026-08-06 in ``f0d7039``'s own docs commit);
* PSFs/RefImages for g0001, blocking the science/reference-image/
  post-process live proofs;
* a scheduler-retry child forced against real ``AttemptDetail`` data.

Newly owed by this run:

* the reconciler-service stack's own image pin, one revision behind the
  fix it needs (see above).

Ephemeral state left behind
-----------------------------

All staging scripts used during this session
(``w9prep_canary_env.py``, ``w9prep_run_suite.py``, DB-query helpers)
were written under ``/tmp`` on rapid-admin, which is wiped on reboot; none
were installed persistently. The canary job's own manifest and terminal
record (``attempts/records/.../attempt-118/seq-0000.json``) are legitimate
attempt artifacts, not scratch, and were kept.
