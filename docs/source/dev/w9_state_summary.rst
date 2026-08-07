W9: current state, as the next worker inherits it
==================================================

An inventory, not a narrative. Supersedes the earlier revision entirely.

**The ramp is done.** All three steps ran real g0001 work through the
production VPO path and passed every gate — 18, 90 and 109 children, **217 of
217 succeeded**, zero failures, zero unexplained records. The previous
revision's single blocking item, a payload predating its own fixes, is
closed.

**Nothing blocks the next worker.** The science phase is unrun but no longer
blocked: it needed a registered reference image, and reference images now
build, catalogue and publish end to end.

What landed
-----------

**The image, in two iterations.** The grant named one rebuild with up to two
iterations, and both were needed.

* Iteration 1 — smdc ``6728ad3``, ``sha256:d050583…``, revisions 18. Published
  the three fixes already on the branch. Running it exposed a fourth defect.
* Iteration 2 — smdc ``d97989f``, ``sha256:61aaca42…``, revisions 19. Fixes
  it. This is what is live.

Both scan-gated by CVE identity, not by count: 3 HIGH / 5 MEDIUM / 1 LOW,
**0 CRITICAL**, the identical set (``CVE-2026-15308``, ``CVE-2026-54369``,
``CVE-2026-58016``) revisions 17 carried. Queues proven quiesced 10 × 0
before each repin; the reconciler deployed with all six parameters explicit
both times, so ``ReconcilerEnabled`` survived.

**Defect 8: a Decimal made every closure record unpublishable.**
``ClosureRecord.to_bytes`` called ``json.dumps`` with no ``default=`` while
encoding a body built from database row values.
``attempt_stages.duration_ms`` is ``numeric NOT NULL`` (migration 011) and
psycopg2 maps ``numeric`` to ``Decimal``, so every attempt that recorded a
stage failed to publish its closure record and deferred forever.

**Only reachable because defect 7 was fixed** — until the SAVEPOINT let
``read_attempt_stages`` succeed, ``stages`` was always ``None`` and no
``Decimal`` ever reached the encoder. The transition is the evidence:
``errors:36 → errors:0/deferred:36 → classified:36/deferred:0``.

Fixed by reusing ``termination._json_default`` rather than growing a second
coercion policy; the helper gains a ``Decimal`` branch, which the numpy path
could not cover (``Decimal`` has no ``.item()``). Coerced to ``float``, not
``str``. Six regression tests; proven on the deployed image itself.

**Four defects of one shape now** — a numpy repr bound into SQL, a numpy
scalar in the terminal record, a caught query error leaving its transaction
aborted, and a Decimal in a second record encoder. The proposed boundary
audit below is the response, and it is now the highest-value item on the
list: each of the four was one bad value taking a whole pass or cycle with
it, and the fourth was found only by running the fix for the third.

**The ramp.** 18 → 90 → 109. Every gate passed at every step: all attempts
reconciler-terminal within horizons, zero non-terminal, zero
``missing_or_contradictory``, zero without a terminal record, one binding per
step, reconciler poll errors 0, no done-files or log-grep anywhere. All 217
attempts are ``terminal_after_start`` / ``success`` / ``published`` /
``error_category=NULL``. Latency is flat across a 6× width increase (1310.6,
1309.5, 1323.3 s mean). Full numbers in ``w9_ramp.rst``.

**Step 3 ran 109, not 270** — the g0001 window holds 109 ready reference
units and the cap is a plain truncation, so a cap above the population
submits the population. A data-staging item, not a pipeline one.

**Battery case 34 (scheduler retry) is CLOSED.** Registered a throwaway
``rapid-pullfail-probe`` definition pinned to an absent digest, submitted one
child, and got **the first scheduler retry ever produced in this account**:
two ``AttemptDetail`` entries, both ``CannotPullContainerError``, both
never-started. ``derive_attempt_indices`` numbers them 1 and 2 in list order,
correctly. It also confirms the fixture concern that motivated the case —
**neither entry carries a ``startedAt`` key at all**, so a hand-written
``{"startedAt": None}`` fixture tests a shape the API does not produce. The
probe definition was deregistered; zero ACTIVE remain.

Current pins
------------

Job definitions ``rapid-pipeline-science:19`` and ``rapid-pipeline-bulk:19``,
and the ``rapid-reconciler-service`` stack, all on
``sha256:61aaca42bd2bbde96745686529a516c2e34f46ea7b9bef8cff44fce93f8bf9ae``
(smdc ``d97989f``). The template default in ``rapid-batch.yaml`` names the
same digest.

**All four pin sites agree**, and unlike the previous revision the pins are
**not** behind ``smdc`` — the tip is ``d97989f`` and that is what is
deployed.

Remaining open items, with owners
----------------------------------

.. list-table::
   :header-rows: 1
   :widths: 30 12 48

   * - Item
     - Owner
     - State
   * - The science phase
     - next worker
     - **The first item, and no longer blocked.** Needs a registered
       reference image; reference images now build and publish end to end,
       217 times over.
   * - End-to-end registration with PSFs
     - next worker
     - Still owed. Behind the science phase.
   * - Serialization / transaction-boundary audit
     - **ratified 2026-08-06; DONE**
     - FOUR defects of one shape, and the argument that a fifth was waiting
       was right: the sweep found seven more, all live or one call site from
       it. Ratified in the disposition batch and executed by the pre-Q8
       hardening run — ``preq8_hardening.rst`` carries the boundary
       inventory, the findings and the fixes.
   * - Reconciler health vs the grace horizon
     - **ratified 2026-08-06; DONE**
     - ``NRestarts=15`` this run: a normal ramp step tripped the
       5-consecutive-unproductive-poll check while its attempts sat inside
       the 10-minute grace horizon — reached, not yet classifiable. Closed
       by the first of the two options, which is what the ratified
       disposition asks for: health counts only actionable-unclosed work.
       An attempt still running, or inside either horizon, is now a distinct
       ``waiting`` outcome that health does not count; a closure step that
       tried and failed still counts, on the same threshold.
       Evidence: ``preq8_hardening.rst``.
   * - Ramp step 3 cannot reach 270
     - data staging
     - The g0001 window (``2027-10-01``–``2027-10-08``) holds 109 ready
       reference units. Either stage more data or restate the target.
   * - Reconciler log-tail group
     - **ratified 2026-08-06; DONE**
     - ``logs/job-log-group`` was ABSENT, so the reconciler fell back to
       ``/aws/batch/job`` — which holds no RAPID job logs and which
       ``rapid-orchestrator-role`` cannot read (``implicitDeny``, simulated
       live 2026-08-07). Jobs log to
       ``/rapid/batch/rapid-queue-{bulk,prompt}`` — TWO groups, so no single
       parameter could ever have named both. Closed by deriving the group
       PER ATTEMPT from ``binding_job_definition_arn``, which the row already
       carries: the job definition owns the ``awslogs-group`` option, so the
       derivation reads the fact at its source and needs no schema change.
       Both groups were already readable (``/rapid/batch/*``, both simulate
       ``allowed``). Cost nothing this run: all 217 attempts wrote their own
       records. Evidence: ``preq8_hardening.rst``.
   * - ``scheduler_job_id`` absent from the ramp summary
     - minor
     - The ``W9-RAMP-SUMMARY`` JSON reports ``scheduler_job_id: null`` while
       the array job id is logged one line above it and lands correctly in
       the database. A reporting gap in the summary only; it briefly reads
       as a failed submission.
   * - ``describe_jobs did not return 50 of 100``
     - pre-existing
     - Warned every poll, unchanged by this run's work. Not investigated
       here.
   * - Coadd-input list location
     - design
     - Unchanged: ``roman-rapid-products/submissions/<run>/coadd-inputs/``,
       recorded as **proposed** for the VPO/operations co-design.
   * - ``rapid-inputs-gbtds-sim-coadd-inputs`` policy
     - cleanup
     - Unchanged. Can never take effect; should be removed as a misleading
       grant. Not deleted — this run's authority excludes deletes.
   * - Q8 smoke run
     - Ben
     - Double-gated as before: explicit go, and after W8/W9 close. W9 is now
       closed, so only the explicit go remains.
   * - RPM / runner chain
     - corrected
     - The runner half of this row was a **misdiagnosis**, corrected here.
       CI runs entirely on GitHub-hosted runners — every workflow in
       ``rapid_systems`` declares ``runs-on: ubuntu-latest`` — so the count
       of registered self-hosted runners is zero because none is wanted,
       not because one is missing, and the promoter is retriable like any
       other workflow. Verified live 2026-08-06 and re-verified 2026-08-07:
       ``gh api repos/Caltech-IPAC/rapid/actions/runners`` returns
       ``total_count: 0`` while workflow runs complete green, and the
       pooler RPM built successfully. rapid-db still on pooler 1.0-2 —
       that half of the row stands, and its closure is road-map step 2b
       (verify the scheduled promoter consumed the rebuilt RPM).
   * - Scratch-object cleanups
     - cleanup
     - ``s3://roman-rapid-build/psf-carry-staging/``; the ``coadd-inputs``
       objects under ``roman-rapid-products/submissions/w9-ramp-*`` (now
       three more runs' worth); the orphaned ``roman-rapid-inputs-socsim``
       bucket; ``rapid-build-artifacts-…/w9-run-probes/``. All left in place
       — deletes are outside this run's authority.

Reproducing the ramp harness
-----------------------------

One step per invocation, on rapid-admin, in the pinned image::

    export RAPID_ACCOUNT=<the SMDC account id>
    ./pipeline/test/run-w9-ramp-on-rapid-admin.sh <image-digest-ref> \
        <reference|science> <child-cap> [tag]

It prints a ``W9-RAMP-SUMMARY`` JSON line carrying the run id, the attempt
ids, the array job id and the binding actually used.

**Redirect its output to a file.** On macOS the staging ``tar`` emits one
``LIBARCHIVE.xattr.com.apple.provenance`` warning per file, which buries the
summary line; ``COPYFILE_DISABLE=1`` reduces but does not eliminate it. The
remote script also pipes through ``tail -120``, so the driver's own
``gathered N unit(s)`` line is lost on a large step — read
``submitted_units`` from the summary JSON instead.

The gate queries are per-``run_id`` and are **not** the shipped
``cloudformation/rapid-query-attempts.sh``, which reports whole-population
counts regardless of the run id passed to it. The eight used here are
reproduced in ``w9_ramp.rst``; the one subtlety worth carrying forward is
that ``application_closed`` is not terminal for gate purposes — it is the
application's account written, with the reconciler's scheduler-observed facts
still to come, and it clears within the 10-minute grace horizon.
