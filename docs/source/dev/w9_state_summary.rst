W9: current state, as the next worker inherits it
==================================================

An inventory, not a narrative. Supersedes the earlier revision entirely.

**The science item that blocked the ramp is closed.** The ``awaicgen`` field
geometry — the previous revision's single blocking item, owned by science —
was ported verbatim from the deleted launcher and is proven live:
``build_reference_image`` succeeded on 36 of 36 real g0001 children, mean
145.2 s, where it had never once completed before.

**The ramp is now blocked on one image rebuild**, not on science. That first
real coadd exposed three defects behind it — two in the payload, one in the
reconciler — all fixed, tested and pushed, and none of them in the image the
job definitions are pinned to.

The one thing that blocks the ramp now
---------------------------------------

The deployed payload (``sha256:30e8c352…``, smdc ``7125f4de``) carries the
geometry port but **not** the three fixes committed after it (smdc
``bc8509e`` and ``fff9296``). Verified by grepping the pinned image's own
filesystem, not inferred from commit order:

.. list-table::
   :header-rows: 1
   :widths: 56 22 22

   * - In the deployed image
     - Expected
     - Found
   * - ``pipeline/mosaic_geometry.py``
     - present
     - **present**
   * - ``awaicgen_num_threads`` in release content
     - present
     - **present**
   * - ``_json_default`` in ``termination.py``
     - absent
     - **absent** (0)
   * - ``sextractor_catalog_type`` in release content
     - absent
     - **absent** (0)
   * - the SAVEPOINT'd stage read in ``closure.py``
     - absent
     - **absent** — the reconciler runs this same image

So every reference-image child on the deployed image fails at
``sextractor_catalog``, and fails *unrecordably*. No ramp step can pass its
gates until the payload is rebuilt onto ``bc8509e`` and the definitions
repinned.

**Owner: next worker, with an authorization that includes a second image
rebuild.** This run's grant named one rebuild, and it was spent publishing
the geometry port — which was the right call at the time, because the
geometry was the known blocker and the three defects behind it were not
discoverable until it ran. Nothing else is needed: the fixes are committed,
the harness is exercised, and the repin procedure ran cleanly twice today.

What landed
-----------

**The awaicgen geometry port.** Extracted from
``e03f22c^:…_launchSingleReferenceImagePipeline.py`` with line-level
citations in ``review_disposition.rst``. The extent stays release content
(it does not vary with sky location); the centre became the ``tile_position``
manifest fact, which the vocabulary already declared and nothing populated.
Regression-tested against the launcher's formula written out longhand over
seven fields including both poles — 30 tests. Proven live, 36/36.

**Three defects found by the first real coadd**, all fixed with regression
tests:

1. **A numpy scalar made attempts unrecordable.** ``numpy.float32`` in the
   terminal record, ``json.dumps`` raises, and ``write_terminal_record`` runs
   on the failure path — so an attempt could not record that it had failed.
   36 non-terminal rows with no record. Fixed at the serialization boundary
   with ``.item()`` coercion. Same class as the earlier numpy-repr-into-SQL
   defect, different serializer — **two now, and a boundary audit is
   proposed**.
2. **Eleven more W4B-dropped keys** across all four ``[sextractor_*]``
   sections, found by walking the command-line builder's full key list rather
   than one live attempt per key.
3. **The reconciler's stage read names a column that never existed.**
   ``read_attempt_stages`` selects ``error_category`` from ``attempt_stages``,
   which has six columns and has never had it — verified against the live
   ``information_schema``. Every reconciliation of a started attempt failed,
   and because the caught exception cannot un-abort a PostgreSQL transaction,
   one real error became 36 ``InFailedSqlTransaction`` ones per cycle. Fixed
   with a SAVEPOINT; six tests added where there had been none.

**Three cascades of one shape now** — a numpy repr bound into SQL (defect 1),
a numpy scalar in the terminal record (defect 5), and a caught query error
leaving its transaction aborted (defect 3 above). Each was one bad value that
took a whole pass or cycle down with it. The proposed audit below is the
response.

**The completeness tests now walk one call deeper.** The pre-existing test
checked only stage-body modules, so keys read inside the command-line
builders were invisible — which is why these drops were being found one
``KeyError`` per live attempt. Both builders are now walked directly; that is
how ``awaicgen_num_threads`` and the eleven sextractor keys were found
without spending attempts.

**rev-16 → rev-17.** One rebuild, scan-gated (3 HIGH / 5 MEDIUM / 1 LOW, **0
CRITICAL** — the identical base-OS CVE set rev-14 and rev-16 carried). Queues
quiesced first (10 × 0). Both job definitions repinned, and the reconciler
service repinned to the same digest with all six parameters explicit —
``ReconcilerEnabled`` survived, so the partial-update trap did not recur.
Reconciler verified ``active (running)`` on the new digest, ``errors: 0`` at
the time of the repin — it began reporting ``errors: 36`` only once the
ramp's attempts gave it started-but-unclosed rows to reconstruct, which is
defect 3 above and not a fault of the repin.

**A stale template default, corrected.** ``PipelineImageDigest``'s ``Default:``
in ``rapid-batch.yaml`` still named the W5 rev-10 digest while the live stack
and both definitions were at rev 16 — six revisions of drift. Every repin
since W5 updated the deployed parameter and left the default beside it, so a
deploy that omitted the parameter would have silently rolled the pipeline
back to W5 code. The Q6 pin-consistency discipline was checking the three
live sites against each other and never the default against them.

**Battery closure.** Case 35 (terminal record survives a numpy scalar) closed
with 4 tests. Case 34 (scheduler retry via forced pull failure) is **blocked
with a specific reason**, not merely unrun — see below.

Remaining open items, with owners
----------------------------------

.. list-table::
   :header-rows: 1
   :widths: 30 12 48

   * - Item
     - Owner
     - State
   * - Rebuild the payload onto ``bc8509e`` and repin
     - next worker
     - **Blocks the ramp**, and is the only thing that does. Needs an
       authorization naming a second image rebuild.
   * - THE RAMP: 18 → 90 → 270
     - next worker
     - Step 1 ran twice on real data and reached the coadd both times;
       steps 2 and 3 deliberately not submitted, because they would have
       reproduced one defect 360 more times at real cost.
   * - Scheduler-retry case (forced pull failure)
     - next worker
     - Blocked on job-definition registration: ``submit-job`` refuses an
       ``image`` container override (captured live), and no job in the
       account has ever been retried, so it cannot be closed by
       observation either. Index derivation now proven against a REAL
       ``AttemptDetail``; the end-to-end pairing remains owed.
       ``w8_battery.rst`` case 34 has the proposed procedure.
   * - End-to-end registration with PSFs
     - next worker
     - Still owed. Behind step 1 passing.
   * - Serialization / transaction-boundary audit
     - proposed
     - THREE defects of one shape now: a numpy repr bound into SQL, a numpy
       scalar in the terminal record, and a caught query error leaving its
       transaction aborted. Each turned one bad value into a whole failed
       pass. Worth one sweep of every boundary a stage value crosses, and of
       every ``except`` around a statement inside a transaction, rather than
       a fourth fix.
   * - Reconciler log-tail group
     - proposed
     - The safety net has never worked: ``logs/job-log-group`` is ABSENT
       from the parameter tree, so the reconciler falls back to
       ``/aws/batch/job`` — which holds no RAPID job logs and which
       ``rapid-orchestrator-role`` cannot read (AccessDenied observed live).
       Jobs log to ``/rapid/batch/rapid-queue-{bulk,prompt}`` — TWO groups,
       so one parameter cannot name both and the fix is a design call.
       Operational config, so it needs no image rebuild.
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
     - Double-gated as before: explicit go, and after W8/W9 close.
   * - RPM / runner chain
     - unchanged
     - Still zero registered self-hosted runners; the promoter cannot be
       retried. rapid-db still on pooler 1.0-2.
   * - Scratch-object cleanups
     - cleanup
     - ``s3://roman-rapid-build/psf-carry-staging/`` (153 objects +
       manifest); the ``coadd-inputs`` objects under
       ``roman-rapid-products/submissions/w9-ramp-*``; the orphaned
       ``roman-rapid-inputs-socsim`` bucket. All left in place — deletes
       are outside this run's authority, and the PSF staging is still the
       cheaper side of an IMSS round trip until a reference image
       completes end to end.

Current pins
------------

Job definitions ``rapid-pipeline-science:17`` and ``rapid-pipeline-bulk:17``,
and the ``rapid-reconciler-service`` stack, all on
``sha256:30e8c35284386855091f4a9ebe23421b9c5a5522e8c1f06c1f59f6b360ab84da``
(smdc ``7125f4de``). The template default in ``rapid-batch.yaml`` now names
the same digest.

**These pins are one commit behind ``smdc``**, deliberately and knowably: the
tip is ``bc8509e``, and closing that gap is the rebuild item above.

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
summary line in a terminal; ``COPYFILE_DISABLE=1`` reduces but does not
eliminate it. The first step-1 run was re-run for this reason alone.

The run-scoped gate queries are not in the shipped
``cloudformation/rapid-query-attempts.sh`` — that tool reports whole-population
counts regardless of the run id passed to it, which reads as a ramp gate
result and is not one. The eight gate queries used here are per-``run_id``;
they are reproduced in ``w9_ramp.rst``.
