W9 — the validation ramp
========================

What this records: the ramp still did not reach its 18/90/270 steps, but the
blocker that stopped the previous attempt is **closed** — the reference-image
coadd now runs, on real g0001 data, thirty-six times over. Two further
defects behind it were found by that first real coadd, both are fixed and
pushed, and neither is in the image the job definitions are pinned to. The
ramp is one image rebuild away from its first passing step.

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
     - 0
     - PASS
   * - Done-files or log-grep anywhere
     - none
     - PASS

**The gate failure is one defect, not thirty-six.** Every attempt failed at
``sextractor_catalog`` for a missing configuration key, and then could not
write the terminal record *saying* it had failed, because the record carried
a numpy scalar that ``json`` refuses. The second defect is what turned a
clean recorded failure into a non-terminal row with no record — which is the
state the attempt-record contract exists to make impossible, and which would
read as a reconciler fault to anyone who had not seen the container log.

Steps 2 and 3 (90 and 270 children) were **not submitted**. Submitting them
would have reproduced the same failure ninety and two hundred and seventy
times at real compute cost, and proved nothing that eighteen had not.

The two defects the first real coadd found
-------------------------------------------

Both are fixed, tested and pushed to ``smdc`` (``bc8509e``). Neither is in the
deployed image — proven, not inferred, by grepping the pinned digest's own
filesystem (both counts zero).

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
