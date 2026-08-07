W9 — the validation ramp
========================

What this records: the ramp did not reach its 18/90/270 steps. It stopped
at a science-logic gap that no amount of infrastructure work closes, and
the evidence below is what the attempt established on the way there —
which is the operational layer working, and four defects found by running
it rather than by reading it.

The one-line state: **the operational layer is validated; the
reference-image science is not runnable.**

The blocker, stated first
------------------------

``awaicgen`` needs four per-field geometry values —
``awaicgen_mosaic_size_x``, ``awaicgen_mosaic_size_y``,
``awaicgen_RA_center``, ``awaicgen_Dec_center``. In the master ``.ini``
all four are the literal string ``to_be_filled_by_script``: the deleted
launcher computed them per field from the tessellation and substituted
them before dispatch. **Nothing in the extracted pipeline computes them.**
``rfis.generateReferenceImage`` passes ``awaicgen_dict`` through unchanged
to ``util.build_awaicgen_command_line_args``, which does
``float(awaicgen_dict["awaicgen_mosaic_size_x"])`` and raises.

So every reference-image attempt fails ``internal_error`` after doing
~30 s of real work — downloading its reference PSF, fetching its 48 coadd
inputs, reformatting them — and stops one call short of the coadd.

This is science logic, not configuration: the mosaic centre and extent are
properties of the field's tessellation tile, and deciding where that
computation belongs (gathering, as a per-unit fact in the manifest; or the
stage, from the tile id it already carries) is a design call. It is
recorded here as the ramp's blocking item and left to the owner.

The chain it blocks is the whole ramp: no reference image means science
units take ``_build_reference_image``, which needs the same four values.
Only ``registration`` — which consumes reconciled outcomes and needs no
image inputs — runs to completion today, which is exactly what W8 found
and said.

What the ramp DID establish
---------------------------

Ten real array children were submitted through the production VPO path
across three job-definition revisions, on real g0001 data. Every gate that
does not depend on the coadd passed.

.. list-table:: Ramp attempts 130–139
   :header-rows: 1
   :widths: 8 10 8 20 12 12

   * - Attempt
     - Phase
     - Rev
     - Terminal state
     - Category
     - Submit→terminal
   * - 130, 131
     - science
     - 14
     - terminal_after_start
     - tool_failure
     - 156.1 s
   * - 132, 133
     - science
     - 14
     - terminal_after_start
     - input_invalid
     - 223.7 s
   * - 134, 135
     - reference
     - 14
     - terminal_after_start
     - internal_error
     - 238.5, 239.0 s
   * - 136, 137
     - reference
     - 15
     - terminal_after_start
     - internal_error
     - 65.3 s
   * - 138, 139
     - reference
     - 16
     - application_closed
     - internal_error
     - 177.7, 178.3 s

The error category moves down the stage sequence at each revision, which
is the useful signal in that table: ``tool_failure`` (the download had
silently failed and ``gunzip`` met a partial file) → ``input_invalid``
(download fixed, manifest lacked a fact) → ``internal_error`` on a missing
config key → ``internal_error`` one key further in. Each step is a defect
found and fixed; the last one is the blocker above.

Gate results, all captured against the live database:

.. list-table::
   :header-rows: 1
   :widths: 55 15

   * - Gate
     - Result
   * - Attempts left non-terminal
     - **0 of 10**
   * - Attempts without a terminal record
     - **0 of 10**
   * - Bindings not versioned / not matching their recorded revision
     - **0 of 10**
   * - Done-files or log-grep anywhere in the path
     - **none**
   * - Reconciler poll errors, three consecutive cycles per revision
     - **0**

The binding row is the round-5 fix proven live rather than by test: every
attempt's ``binding_job_definition_arn`` ends in ``:<rev>`` and equals its
``binding_job_definition_rev``, across revisions 14, 15 and 16 and across
both route classes — ``rapid-pipeline-bulk`` for reference-image,
``rapid-pipeline-science`` for science. The submitted ARN, the recorded
ARN and the running revision are one value.

Latency, for the Q8 parameters
------------------------------

Submission to terminal, per attempt, from the attempt rows:

* science, 2 children: 156 s and 224 s
* reference-image, 2 children: 65 s, 178 s, 239 s

The spread is scheduling, not work: the in-container stage time is 0.5–36 s
(the terminal records carry per-stage durations), and the rest is Batch
placing the job — a cold compute environment scaling from zero. The 65 s
case is the one that landed on warm capacity. **For Q8's drip parameters
the number that matters is that a cold start costs ~2–4 minutes before any
work begins**, so a step's wall-clock floor is that plus its stage time,
not its stage time.

No throughput figure is offered: two children is not a measurement of
throughput, and reporting one from this sample would be inventing it. The
ramp's 18/90/270 steps are what would have produced it.

Defects found by running it
---------------------------

Four, all now fixed and pushed, each found live and none catchable by the
suite as it stood:

1. **The readiness window was bound as a numpy repr.** ``mjd_window``
   returned ``numpy.float64``; psycopg2 has no adapter for it, so it fell
   back to ``repr()``, which under NumPy 2 is ``np.float64(61679.0)``.
   Postgres read that as a schema-qualified name — ``schema "np" does not
   exist`` — which aborted the transaction, so every later query in the
   pass was skipped and gathering reported "0 (field, filter) pairs".
   Indistinguishable from a night with no data. The window held 5,166
   registered L2 files across 109 fields.

2. **The coadd-input list was read from an assumed bucket.** Both
   consuming stages took the bucket from ``s3/inputs-bucket`` and split
   that name off the URI — so a list anywhere else was fetched with the
   whole ``s3://`` string as the key. It also forced a submission-authored
   file into the sealed, read-only staged-input bucket, which the shared
   permissions boundary refuses by design (``no permissions boundary
   allows the s3:PutObject action``). Fixed by reading the bucket from the
   URI that names it.

3. **Release content was missing the coadd's two input list filenames** —
   the same W4B migration drop as the three output names beside them, and
   invisible to the completeness test because the ``[awaicgen]`` section is
   handed to a helper whole and subscripted directly rather than read
   through ``science_value``. The test now walks that access pattern too.

4. **The staged-input grant named a bucket that does not exist.**
   ``StagedInputBucketArnPattern`` was still ``arn:aws:s3:::rapid-socsim-input-*``
   after the dataset was re-ruled to ``gbtds-sim``, so the job role could
   not read its own input data — which is what made defect 1's symptom look
   like a tool failure. Corrected to the real bucket, both ARNs.

Infrastructure landed
---------------------

* **PSFs carried and registered.** 153 objects (144 WFI science PSFs, 9
  reference PSFs) verified against their SHA-256 manifest, landed as
  generation ``g0002-psf`` of ``roman-rapid-inputs-gbtds-sim`` by the
  runbook's unseal-load-reseal, manifest written last, generation resealed
  and the seal proven by a denied probe write. The 18 F146 science PSFs
  (fid 8 — the filter g0001's data is entirely in) are registered in
  ``PSFs`` with ``vbest=1, status=1``, which is the exact predicate
  ``get_best_psf`` selects on. Proven consumed, not just present:
  ``download_reference_psf`` succeeds in every reference attempt above.

* **rev-14 → rev-16.** Three builds, each scan-gated (0 CRITICAL
  throughout; rev-15 was 2 HIGH/1 MEDIUM, rev-14 and rev-16 3 HIGH/5
  MEDIUM/1 LOW, all base-OS CVEs identical to the previously deployed
  image). Both job-definition families repinned to one digest each time,
  queues quiesced first, reconciler stack updated to the same digest with
  all six parameters explicitly pinned, association re-run, service
  verified active with clean polls.

Owed
----

* The four ``awaicgen`` geometry values — the blocker above. Until it is
  closed no reference image can be built, so the ramp cannot start.
* The ramp itself: 18 → 90 → 270, unrun.
* The scheduler-retry case (forced pull failure) and the end-to-end
  registration proof, both owed from W8 and both downstream of the ramp.
