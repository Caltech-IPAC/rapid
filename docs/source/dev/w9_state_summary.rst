W9: current state, as the next worker inherits it
==================================================

An inventory, not a narrative. W9 was scoped as the validation ramp, and
**the ramp still did not run** — but for a different reason than the two
earlier runs recorded, and behind a much shorter remaining list. Both
earlier blockers are gone. The evidence and the numbers are in
``w9_ramp.rst``; this document is the state and the ownership.

Supersedes the earlier revision of this file entirely: the SSO blocker and
the IMSS-gate blocker below are recorded as RESOLVED, not as current.

The one thing that blocks the ramp now
---------------------------------------

``awaicgen`` needs four per-field geometry values —
``awaicgen_mosaic_size_x``, ``awaicgen_mosaic_size_y``,
``awaicgen_RA_center``, ``awaicgen_Dec_center``. In the master ``.ini``
all four are the literal ``to_be_filled_by_script``: the deleted launcher
computed them per field from the tessellation and substituted them before
dispatch. **Nothing in the extracted pipeline computes them**, so
``util.build_awaicgen_command_line_args`` raises ``KeyError`` and every
reference-image attempt dies ``internal_error`` after ~30 s of real work.

No reference image means science units take ``_build_reference_image``,
which needs the same four values — so the blocker is the whole ramp, not
just its first phase.

**Owner: science.** The mosaic centre and extent are properties of the
field's tessellation tile, and where the computation belongs is a design
call — a per-unit fact from gathering (the manifest already carries
``sky_position`` and ``rtid``), or the stage deriving it from the tile id
it already has. ``roman_tessellation_db`` exists and can supply the
centre. Not decided here.

Resolved since the last revision
---------------------------------

**SSO expiry — resolved, and the fix is worth keeping.** The earlier runs
concluded that ``aws sso login`` blocks on human consent and cannot be
completed unattended. True, but it was the wrong lever. The CLI caches
role credentials in ``~/.aws/cli/cache`` for their full lifetime, so a
login does not extend them; ``deploy-stack.sh``'s own guard documents the
remedy in the failure message. Deleting the cached *role* credential
(NOT the SSO token cache in ``~/.aws/sso/cache``) makes the still-valid
token mint fresh ones::

    rm -f ~/.aws/cli/cache/*.json

Measured: 20 minutes remaining before, 2 hours after, no interaction. An
unattended worker hitting the 30-minute guard should do this rather than
report a blocker.

**The IMSS gate — resolved by division of labour, not by evasion.** The
``ask``-listed ``--profile imss*`` pattern is still there and was still
not worked around. The supervisor performed the cross-account read and
staged the set in-account at ``s3://roman-rapid-build/psf-carry-staging/``;
W9 did the in-account half. No worker touched an IMSS profile.

**PSFs and RefImages are no longer both empty.** ``PSFs`` has 18 rows.

What landed
-----------

**The PSF carry (item 1), complete.** 153 objects — 144 WFI science PSFs
and 9 reference PSFs — verified against their SHA-256 manifest (153/153,
zero mismatches), landed as generation ``g0002-psf`` of
``roman-rapid-inputs-gbtds-sim`` by the g0001 runbook's unseal-load-reseal.
Manifest written last, ``checksum_basis: sha256`` (an upgrade on g0001's
etag basis, free because the carry computed digests at upload). Generation
resealed, and the seal proven by a denied probe write —
``AccessDenied ... explicit deny in a resource-based policy``, and the
probe object absent afterwards (404). Provenance in the generation
manifest's ``notes``. Staging prefix left in place — see "Owed".

The 18 F146 science PSFs are registered in ``PSFs`` at ``version=1,
vbest=1, status=1`` — the exact predicate ``get_best_psf`` selects on
(``vbest > 0 and status > 0 and sca = %s and fid = %s``). F146 is fid 8:
the database calls that filter ``W146`` and the repo's own converter does
``filter.replace("F146","W146")``. fid 8 is the filter **all** 5,166
g0001 L2 files are in, so the registered set covers the staged data
exactly. Registration used the schema's own ``addPSF``/``updatePSF`` pair.

Proven consumed, not merely present: ``download_reference_psf`` succeeds
in every reference-image attempt in ``w9_ramp.rst``.

The 9 reference PSFs are deliberately NOT ``PSFs`` rows: they are derived
products that ``scripts/generate_refim_psfs.py`` builds *from* the science
PSFs, and they are recorded in the generation manifest.

**rev-14 → rev-16 (item 2), three times.** Each scan-gated, 0 CRITICAL
throughout. Both job-definition families repinned to one digest each time
with queues quiesced first; the reconciler stack updated to the same digest
with **all six parameters explicitly pinned** (the partial-update trap that
reverted ``ReconcilerEnabled`` earlier did not recur); association re-run;
service verified ``active (running)`` with ``errors: 0`` polls.

Current pins: job definitions ``rapid-pipeline-science:16`` and
``rapid-pipeline-bulk:16``, both on
``sha256:2c3188170aa28476161cc33d17a72e7ca7690685cd48a6753b49d7a9e03d6e87``
(smdc ``833eeca``), reconciler on the same digest.

**Four defects found live and fixed** — the numpy-repr window binding, the
coadd-list bucket assumption, the two dropped ``[awaicgen]`` release keys,
and the staged-input grant naming a bucket that does not exist. Each with
a regression test, each verified in the pinned image. Detail in
``w9_ramp.rst``.

**The operational layer validated.** Ten real array children through the
production VPO path across three revisions: 0 non-terminal, 0 without a
terminal record, 0 binding mismatches, no done-files or log-grep anywhere.
That is what W9 was for, and it holds — what is missing is the science
that would let the ramp scale it.

Remaining open items, with owners
----------------------------------

.. list-table::
   :header-rows: 1
   :widths: 30 12 48

   * - Item
     - Owner
     - State
   * - ``awaicgen`` field geometry (the four values)
     - science
     - **Blocks the ramp.** Design call, above.
   * - THE RAMP: 18 → 90 → 270
     - next worker
     - Unrun. Downstream of the geometry item; the harness
       (``pipeline/test/live_w9_ramp.py`` + its runner) is written,
       committed and exercised — it takes a phase and a child cap.
   * - Scheduler-retry case (forced pull failure)
     - next worker
     - Owed from W8, still unforced. Downstream of the ramp.
   * - End-to-end registration with PSFs
     - next worker
     - Owed from W8. The PSF half is now done; the reference half is
       behind the geometry item.
   * - Coadd-input list location
     - design
     - W9 moved it to ``roman-rapid-products/submissions/<run>/coadd-inputs/``
       because the staged-input bucket is read-only for service identities
       by design and sealed create-once. Recorded as **proposed**, for
       ratification in the VPO/operations co-design.
   * - ``rapid-inputs-gbtds-sim-coadd-inputs`` policy
     - cleanup
     - Attached to ``rapid-orchestrator-role`` by W9 before the boundary
       was understood; it can never take effect (the shared boundary
       forbids write on staged inputs) and should be **removed** as a
       misleading grant. Left in place rather than deleted: W9's authority
       excludes deletes.
   * - Q8 smoke run
     - Ben
     - Double-gated as before: explicit go, and after W8/W9 close.
   * - RPM / runner chain
     - unchanged
     - Still zero registered self-hosted runners; ``build-rpms.yml``
       requires them, so the promoter cannot be retried. rapid-db still on
       pooler 1.0-2. Unchanged by W9.
   * - Scratch-object cleanups
     - cleanup
     - ``s3://roman-rapid-build/psf-carry-staging/`` (153 objects +
       manifest) — the carry inputs. W9 was authorized to remove them once
       the generation verified, and the generation did verify; left in
       place because the reference-image path has not yet consumed a PSF
       end-to-end through a completed coadd, and re-staging costs an IMSS
       round trip the gate makes expensive. Removable once the ramp runs.
       Also: the ``coadd-inputs`` objects under
       ``roman-rapid-products/submissions/w9-ramp-*``, and the orphaned
       ``roman-rapid-inputs-socsim`` rehearsal bucket (owner break-glass,
       tracked at Q-storage-buildout, unchanged).

Reproducing the ramp harness
-----------------------------

One step per invocation, on rapid-admin, in the pinned image::

    export RAPID_ACCOUNT=<the SMDC account id>
    ./pipeline/test/run-w9-ramp-on-rapid-admin.sh <image-digest-ref> \
        <reference|science> <child-cap> [tag]

It prints a ``W9-RAMP-SUMMARY`` JSON line carrying the run id, the
attempt ids, the array job id and the binding actually used. The gate
queries are in the ramp evidence document.
