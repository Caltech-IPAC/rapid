W9 — the validation ramp
========================

**The ramp passed.** All three steps ran real g0001 work through the
production VPO path and passed every gate: 18, then 90, then 109 children,
**217 of 217 succeeded**, zero failures, zero unexplained records, zero
done-files. The blocker the previous revision recorded — a payload predating
its own fixes — is closed, and closing it exposed and closed a fourth defect
of the same shape.

The one-line state: **the ramp is done; what remains is the science phase and
a population that cannot reach 270.**

What the previous revision was blocked on
------------------------------------------

The deployed payload carried the ``awaicgen`` geometry port but none of the
three defects found behind it. Every reference-image child failed at
``sextractor_catalog``, and failed *unrecordably*. That is closed: the image
was rebuilt onto the fixes, and the ramp then ran clean.

The rebuild took **two iterations**, and the second was not foreseen.

Iteration 1 (smdc ``6728ad3``, ``sha256:d050583…``, revisions 18) published
the three fixes already on the branch. Running it exposed a fourth defect —
**defect 8** — which iteration 2 (smdc ``d97989f``, ``sha256:61aaca42…``,
revisions 19) fixes. Both iterations passed the scan gate by CVE identity:
3 HIGH / 5 MEDIUM / 1 LOW, **0 CRITICAL**, the identical set
(``CVE-2026-15308``, ``CVE-2026-54369``, ``CVE-2026-58016``) revisions 17
carried, as expected for a code-only layer over an unchanged
``base-30984903893``.

Defect 8 — the fix that exposed it
-----------------------------------

**A Decimal made every closure record unpublishable.**
``ClosureRecord.to_bytes`` called ``json.dumps`` with no ``default=``, while
the body it encodes is built straight from database row values.
``attempt_stages.duration_ms`` is ``numeric NOT NULL`` (migration 011) and
psycopg2 maps every ``numeric`` to ``Decimal``, so the encoder raised
``TypeError: Object of type Decimal is not JSON serializable`` for any
attempt that had recorded a stage.

**It was only reachable because defect 7 was fixed.** Until the SAVEPOINT let
``read_attempt_stages`` succeed, that query always failed and ``stages`` was
always ``None`` — so no ``Decimal`` ever reached the encoder. Fixing one
defect is what exposed the next, and the observable transition says so
exactly:

.. code-block:: text

    errors: 36, deferred:  0    # defect 7 live — the cycle aborts
    errors:  0, deferred: 36    # defect 7 fixed — 36 rows now defer instead
    classified: 36, deferred: 0 # defect 8 fixed — all 36 finally close

The middle line is the whole finding: the repin did not break anything, it
moved 36 attempts from one failure mode to the next one behind it.

**This is the fourth defect of one shape** — a numpy repr bound into SQL, a
numpy scalar in the terminal record, a caught query error leaving its
transaction aborted, and now a Decimal in a second record encoder. That is
the argument for the proposed boundary audit rather than a fifth fix.

The fix reuses ``termination._json_default`` rather than growing a second
coercion policy; that helper gains a ``Decimal`` branch, which the numpy path
could not cover because ``Decimal`` has no ``.item()``. Proven on the
deployed image itself, not argued: the shipped code raises, the fixed code
encodes ``145300.0`` — a number, not a string.

The three steps
---------------

.. list-table:: Every step, every child
   :header-rows: 1
   :widths: 30 10 10 12 12 12 14

   * - Run
     - Asked
     - Ran
     - Succeeded
     - Failed
     - Mean latency
     - Gates
   * - ``…20260807T024017Z-step1``
     - 18
     - 18
     - **18**
     - 0
     - 1310.6 s
     - **PASS**
   * - ``…20260807T031527Z-step2``
     - 90
     - 90
     - **90**
     - 0
     - 1309.5 s
     - **PASS**
   * - ``…20260807T034039Z-step3``
     - 270
     - 109
     - **109**
     - 0
     - 1323.3 s
     - **PASS**

**Step 3 ran 109, not 270, and that is a population ceiling rather than a
failure.** The harness reported ``cap: 270, submitted_units: 109``: it
gathered every ready reference unit in the g0001 window
(``2027-10-01``–``2027-10-08``, the staged subset's own window) and there
were 109. ``_capped`` is a plain truncation of the gathered list, so a cap
above the population simply submits the population. The ramp's design target
of 270 is **not reachable with the current staged subset** — recorded below
as an open item, since it is a data-staging question, not a pipeline one.

Per-step gate table
-------------------

Every gate below is scoped to one ``run_id``. The shipped
``cloudformation/rapid-query-attempts.sh`` reports whole-population counts
regardless of the run id passed to it, which reads as a ramp gate result and
is not one.

.. list-table::
   :header-rows: 1
   :widths: 44 18 18 20

   * - Gate
     - Step 1 (18)
     - Step 2 (90)
     - Step 3 (109)
   * - Attempts recorded
     - 18
     - 90
     - 109
   * - Reconciler-terminal within horizon
     - **18/18**
     - **90/90**
     - **109/109**
   * - Not reconciler-terminal
     - **0**
     - **0**
     - **0**
   * - ``missing_or_contradictory``
     - **0**
     - **0**
     - **0**
   * - Attempts without a terminal record
     - **0**
     - **0**
     - **0**
   * - Unexplained terminal records
     - **0**
     - **0**
     - **0**
   * - Distinct image/definition bindings
     - **1**
     - **1**
     - **1**
   * - Reconciler poll errors
     - **0**
     - **0**
     - **0**
   * - Done-files or log-grep anywhere
     - none
     - none
     - none

Every attempt in all three steps is ``terminal_after_start`` with
``rapid_outcome=success``, ``product_disposition=published``,
``error_category=NULL``, ``scheduler_state=SUCCEEDED`` — one homogeneous
outcome class, 217 rows, no exceptions to explain.

All 217 bound to one digest, ``sha256:61aaca42…``, ``source_sha``
``d97989f842e599e733ca135ab862cf14c316d990`` — the pin the definitions name.

**A note on what "terminal" means here.** ``application_closed`` is *not*
terminal for gate purposes: it is the application having written its complete
account, with the reconciler's scheduler-observed facts still to come. Rows
sit there legitimately for up to the 10-minute grace horizon
(``GRACE_HORIZON_SECONDS``) after the scheduler reports them stopped. Every
step was queried before *and* after that horizon; the gate numbers above are
the post-horizon ones. A gate query that counted ``application_closed`` as
terminal would have passed the ramp several minutes early, and one that
counted it as a failure would have failed a healthy run.

Latency and throughput
----------------------

.. list-table:: Submit → terminal, and in-container span
   :header-rows: 1
   :widths: 22 13 13 13 13 13 13

   * - Step
     - min
     - mean
     - max
     - min run
     - mean run
     - max run
   * - 1 (18)
     - 1098.7 s
     - **1310.6 s**
     - 1409.3 s
     - 895.4 s
     - 1102.1 s
     - 1199.5 s
   * - 2 (90)
     - 1016.1 s
     - **1309.5 s**
     - 1476.7 s
     - 778.7 s
     - 1076.7 s
     - 1245.5 s
   * - 3 (109)
     - 1032.3 s
     - **1323.3 s**
     - 1497.1 s
     - 784.3 s
     - 1080.6 s
     - 1254.1 s

**The distribution is flat across a 6× width increase** — 1310.6, 1309.5,
1323.3 s mean, a spread of under 1%. Every child of every step was placed
concurrently (the compute environment scaled to 18, then 90, then 109
simultaneous ``RUNNING`` children), so the steps measure the same per-child
cost at three widths rather than a queue draining.

That is the useful Q8 input: **a reference-image child costs ~22 minutes
wall clock, ~18 minutes of it in-container**, and the difference — roughly
230 s — is placement, which does not grow with step width up to 109.

Wall-clock per step, submission to last child terminal: step 1 ~22 min, step
2 ~14 min, step 3 ~13 min. Steps 2 and 3 are *faster* than step 1 despite
being 5× and 6× larger, because step 1 paid a cold start from zero.

Per-stage timings
-----------------

.. list-table:: Mean seconds per stage, all successes
   :header-rows: 1
   :widths: 40 15 15 15 15

   * - Stage
     - Step 1
     - Step 2
     - Step 3
     - n each
   * - ``download_reference_psf``
     - 0.11
     - 0.11
     - 0.10
     - 18 / 90 / 109
   * - ``build_reference_image``
     - 152.14
     - 144.27
     - 143.13
     - 18 / 90 / 109
   * - ``coverage_and_uncertainty_statistics``
     - 5.81
     - 4.53
     - 4.53
     - 18 / 90 / 109
   * - ``image_statistics``
     - 2.83
     - 2.11
     - 2.11
     - 18 / 90 / 109
   * - ``measure_fwhm``
     - 0.03
     - 0.04
     - 0.04
     - 18 / 90 / 109
   * - ``sextractor_catalog``
     - 3.99
     - 3.98
     - 3.85
     - 18 / 90 / 109
   * - ``psf_catalog``
     - **928.96**
     - **910.60**
     - **915.77**
     - 18 / 90 / 109
   * - ``add_header_keywords``
     - 0.57
     - 0.49
     - 0.49
     - 18 / 90 / 109
   * - ``upload_products``
     - 7.15
     - 6.97
     - 6.92
     - 18 / 90 / 109

Two things worth naming:

**``sextractor_catalog`` now succeeds** — 3.99 s mean across all 217, where
it failed 36 of 36 at 0.00 s before. The 0.00 s was the signature of an
immediate ``KeyError``; ~4 s is the signature of work.

**``psf_catalog`` is the pipeline's cost centre**, ~915 s — about 70% of the
in-container time and 6× the coadd. It had never been reached before this
run, so this is its first measurement. It is not a defect: it succeeded
217/217 with a tight spread. But any future work on reference-image
throughput starts here, not at the coadd.

Reconciler and pooler
---------------------

**Reconciler**: ``active (running)`` on ``sha256:61aaca42…``, ``errors: 0``,
``deferred: 0``, zero ``ERROR`` lines, steady state with ``open`` equal to
``skipped`` (everything closed). It classified each step's attempts as they
cleared their grace horizons — 6, then 4, then 4 per cycle — rather than in
one burst.

**One thing to record, not a gate failure**: ``NRestarts=15``. The service's
health check exits on 5 consecutive unproductive polls ("a working process
doing no work"), and a ramp step reliably produces exactly that while its
attempts sit inside the 10-minute grace horizon — reached, not classifiable
yet. The supervisor restarts it and nothing is lost, but **a normal ramp
trips a health threshold designed to catch an abnormal condition**. Proposed:
either exclude inside-horizon deferrals from the unproductive count, or raise
the threshold above the horizon's worth of polls. Recorded rather than
changed — it is a health-semantics design call.

**Pooler**: pgbouncer ``active``, **zero ``client_idle_timeout`` kills**
across the whole ramp window, zero pool exhaustion, zero ``query_timeout``.
The only closures logged are ``server lifetime over (age=3600s)`` (normal
recycling) and ``client close request`` (the gate queries disconnecting).
Bounded waits throughout.

An eighth finding, still recorded rather than fixed
----------------------------------------------------

**The reconciler's log-tail safety net still has never worked.** It reads
``logs/job-log-group``; that parameter is absent, so it falls back to
``/aws/batch/job`` — which holds no RAPID job logs and which
``rapid-orchestrator-role`` cannot read. Both facts re-observed live during
this run, unchanged from the previous revision. Jobs log to
``/rapid/batch/rapid-queue-{bulk,prompt}`` — **two** groups, so one parameter
cannot name both and the fix is a design call. Operational configuration, so
it needs no image rebuild.

It cost nothing this run: the safety net is only read for a reconstructed
record, and all 217 attempts wrote their own.

What the ramp still owes
------------------------

* **The science phase.** Unrun. It needs a registered reference image, which
  step 1 passing now makes possible — this is the next worker's first item,
  and it is no longer blocked on anything.
* **End-to-end registration with PSFs.** Still owed, behind the science
  phase.
* **A population that reaches 270.** Step 3 exhausted the g0001 window at
  109. Closing the ramp's stated 270 needs either more staged data or an
  explicit decision that 109 is the ceiling and the target should say so.
