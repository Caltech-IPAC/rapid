W9 — the validation ramp
========================

What this records: the ramp still did not reach its 18/90/270 steps, but the
blocker that stopped the previous attempt is **closed** — the reference-image
coadd now runs, on real g0001 data, thirty-six times over. Three further
defects behind it were found by that step — two in the payload, one in the
reconciler — all three fixed and pushed, and none of them in the image the
job definitions are pinned to. The ramp is one image rebuild away from its
first passing step.

The one-line state: **the science that blocked the ramp is done; the ramp is
blocked on rebuilding the payload onto the fixes it found.**

What changed since the last revision
------------------------------------

The previous revision's blocking item was the four ``awaicgen`` geometry
values — ``awaicgen_mosaic_size_x/y`` and ``awaicgen_RA_center/Dec_center`` —
computed by the deleted launcher and by nothing since, so
``build_reference_image`` raised ``KeyError`` after ~30 s of real work and
every reference-image attempt died ``internal_error``.

**That item is closed.** The computation was ported verbatim from the deleted
launcher rather than re-derived, and the port's own split follows the
launcher's: the extent is release content (it does not vary with sky
location, launcher lines 226-228), the centre is a per-invocation manifest
fact (it is the tessellation tile's, launcher lines 352-382). Citations and
the regression evidence are in ``review_disposition.rst``.

Measured, live: ``build_reference_image`` **succeeded on 36 of 36 real
children**, mean 145.2 s, across two independent submissions of the same 18
units. It had never once completed before.

The ramp's first step, and what it found
-----------------------------------------

Step 1 was submitted twice — the first run's output was lost to macOS
``tar`` xattr noise on the operator's terminal, so it was re-run to capture
a clean log. Both submissions are real work against real data and both are
reported here; their agreement is itself the reproducibility evidence.

.. list-table:: Step 1 — 18 reference-image children, twice
   :header-rows: 1
   :widths: 26 12 12 12 12 26

   * - Run
     - Children
     - Attempts
     - Coadds OK
     - Mean coadd
     - Terminal state
   * - ``…20260807T011721Z-step1``
     - 18
     - 140–157
     - **18/18**
     - 145.0 s
     - FAILED, unrecordable
   * - ``…20260807T011745Z-step1b``
     - 18
     - 158–175
     - **18/18**
     - 145.3 s
     - FAILED, unrecordable

Per-stage, identical in both runs:

.. list-table::
   :header-rows: 1
   :widths: 44 14 14 14

   * - Stage
     - Outcome
     - n
     - Mean
   * - ``download_reference_psf``
     - success
     - 18
     - 0.12 s
   * - ``build_reference_image``
     - **success**
     - 18
     - 145.3 s
   * - ``coverage_and_uncertainty_statistics``
     - success
     - 18
     - 4.51 s
   * - ``sextractor_catalog``
     - failure
     - 18
     - 0.00 s

The coadd is doing real work: each child downloads its reference PSF, fetches
and reformats its 48 coadd inputs, and produces a 7000×7000 mosaic. The 0.00 s
on ``sextractor_catalog`` is the signature of an immediate ``KeyError``, not
of work attempted.

Step-1 gate table
-----------------

.. list-table::
   :header-rows: 1
   :widths: 52 18 30

   * - Gate
     - Result
     - Note
   * - Binding versioned and matching its recorded revision
     - **0 of 36 bad**
     - PASS
   * - Image/definition binding actually used
     - 36/36 on rev 17
     - PASS — one digest
   * - Attempts left non-terminal
     - **36 of 36**
     - **FAIL** — see below
   * - Attempts without a terminal record
     - **36 of 36**
     - **FAIL** — same cause
   * - Latency, submit → terminal
     - not measurable
     - no ``ended_at`` was ever written
   * - Reconciler poll errors
     - **36 per cycle**
     - **FAIL** — defect 7, a separate cause
   * - Done-files or log-grep anywhere
     - none
     - PASS

**The gate failures are three defects, not a hundred and eight.** Every
attempt failed at
``sextractor_catalog`` for a missing configuration key, and then could not
write the terminal record *saying* it had failed, because the record carried
a numpy scalar that ``json`` refuses. The second defect is what turned a
clean recorded failure into a non-terminal row with no record — which is the
state the attempt-record contract exists to make impossible, and which would
read as a reconciler fault to anyone who had not seen the container log.

Steps 2 and 3 (90 and 270 children) were **not submitted**. Submitting them
would have reproduced the same failure ninety and two hundred and seventy
times at real compute cost, and proved nothing that eighteen had not.

The three defects the first real coadd found
---------------------------------------------

All three are fixed, tested and pushed to ``smdc`` (defects 5 and 6 in
``bc8509e``, defect 7 in ``fff9296``). None is in the deployed image — proven
for the first two, not inferred, by grepping the pinned digest's own
filesystem (both counts zero); defect 7 is in the reconciler, which runs the
same image.

**5. A numpy scalar made the attempt unrecordable.** The extracted stage
bodies compute in numpy, so ``coverage_and_uncertainty_statistics`` records
``reference_cov5percent`` as a ``numpy.float32``, and ``json.dumps`` raises
``TypeError: Object of type float32 is not JSON serializable`` inside
``write_terminal_record``. That function runs on the failure path as well as
the success path, so an attempt failing for an unrelated reason could not
record that it had failed. Fixed by coercing at the serialization boundary —
``.item()``, so an integer scalar stays an integer, and *not* ``default=str``,
which would keep the write working while silently retyping a numeric field.
A value with no scalar equivalent still raises.

This is the same *class* as defect 1 (the numpy repr bound into SQL), a
different consumer. Two numpy-scalar defects in two different serializers
suggests the boundary is worth an audit rather than a third fix.

**6. Eleven more W4B-dropped keys, in every** ``[sextractor_*]`` **section.**
``sextractor_catalog_type`` failed the step *after* the 145-second coadd
succeeded. Rather than pay one live attempt per key, the whole of
``build_sextractor_command_line_args``' key list was walked against release
content: 67 required, 7 supplied at runtime by the stage body, 50 present,
**11 missing**. All eleven restored to all four sections from the master
``.ini``.

**7. The reconciler's stage read names a column that never existed.** Found
after the step, by watching the service rather than the attempts.
``read_attempt_stages`` selects ``error_category`` from ``attempt_stages``;
that table has six columns and has never had it (verified against the live
``information_schema``, not read off the migration). Every reconciliation of
a started attempt therefore failed, the service polled ``errors: 36`` each
cycle, and it reached 4 consecutive unproductive polls against a health
threshold of 5.

The ``except`` around that query looked like it made the failure safe. It did
not: PostgreSQL aborts the whole transaction on any statement error, so
catching it and returning ``None`` left every later statement in the cycle
raising ``InFailedSqlTransaction``. **One real error became thirty-six
misleading ones**, with the honest warning naming the missing column buried
underneath. That is the same cascade shape as defect 1 — the error handled
locally, the transaction not — which makes it the third instance of a pattern
worth a sweep rather than a fourth fix.

Fixed with a SAVEPOINT, so a failed read costs the read and not the cycle.
Six tests added; the function previously had none, which is how a column name
that never existed survived to be found by a live run. The query is only
reached for a STARTED attempt with no sequence-0 record, and the ramp's 36
were the first such rows the reconciler had ever been asked to reconstruct.

The attribution is arithmetic, not assumption — two poll lines, before the
ramp and after::

    01:16:14  {'open':  78, 'skipped': 78, 'errors':  0}   # after the repin
    01:42:53  {'open': 114, 'skipped': 78, 'errors': 36}   # after the ramp

``open`` grows by exactly the ramp's 36 while ``skipped`` stays at 78, so the
78 pre-existing rows are skipped rather than attempted and every one of the 36
errors is a ramp attempt. The reconciler was not broken by the repin; it was
handed the first work of this kind it had ever been asked to do.

The service is degraded but **not** failing: it is ``active``, has never
reported itself unhealthy, and the unproductive counter oscillates (4 → 3)
rather than climbing, because it resets on any poll that closes something or
has nothing to close. It clears when the 36 attempts close.

An eighth finding, recorded rather than fixed
----------------------------------------------

**The reconciler's log-tail safety net has never worked.** It reads
``logs/job-log-group`` from the parameter tree; that parameter is **absent**,
so it falls back to ``/aws/batch/job`` — a log group that holds no RAPID job
logs, and on which ``rapid-orchestrator-role`` has no ``logs:GetLogEvents``
grant. Both facts observed live: the AccessDenied in the reconciler's own
warning, and a ``get-log-events`` against that group returning
``ResourceNotFoundException`` for a stream that exists under the real one.

The jobs log to ``/rapid/batch/rapid-queue-bulk`` and
``/rapid/batch/rapid-queue-prompt`` — **two** groups, one per queue
(``rapid-batch.yaml`` ``LogConfiguration``), so a single ``job-log-group``
parameter cannot name both. That makes the fix a design call, not a value to
paste in, which is why it is proposed rather than taken. It is operational
configuration, so it needs no image rebuild.

Latency, for the Q8 parameters
------------------------------

The numbers that can honestly be given from this run are **stage** times, not
submit-to-terminal times: no attempt wrote ``ended_at``, so the end-to-end
figure the previous revision reported cannot be recomputed here.

* Cold-start placement, submit → container start: **~215 s** (01:17:48 →
  01:21:22 on attempt 158), a compute environment scaling from zero.
* In-container work, reference-image: **150 s** to the failure point, of
  which the coadd is 145 s.
* The coadd's spread is remarkably tight — 145.0 s and 145.3 s in two
  independent runs, max 150.6 s — which is the useful Q8 input: **a
  reference-image child costs ~2.5 minutes of real compute**, and the
  cold-start placement roughly doubles a small step's wall clock.

No throughput figure is offered. Eighteen children that all failed at the
same stage measure a stage, not a pipeline.

What the ramp still owes
------------------------

* **Steps 1, 2 and 3, passing.** Unrun against a payload carrying the two
  fixes. Everything the harness needs is committed and exercised; what is
  missing is one image rebuild and the repin behind it.
* The science phase, which needs a registered reference image — and so is
  downstream of step 1 passing.
* The scheduler-retry case (forced pull failure), which needs a job
  definition pinned to an absent image; see ``w8_battery.rst`` case 34.
