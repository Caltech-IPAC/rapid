Retiring the baked tessellation database
========================================

:Status: recorded by W7 (2026-08-06); the image change itself is **W8's**
:Scope: the exact edits required, and why nothing here was executed

W7 replaced the baked SQLite tessellation with a closed-form computation.
The pipeline no longer opens ``roman_tessellation_nside512.db`` at
runtime, so the 1.4 GiB layer that carries it and the environment
variable that points at it are both dead weight. This page records
exactly what has to change to remove them.

**No image was built and no job definition was touched by W7**, by
instruction: the bake retirement rides the proven image/job-definition
path, which is W8's. Every edit below is stated so W8 can apply it
without re-deriving anything.


Why it can go
-------------

Certification proved the tessellation *regular*: ``rtid(ra, dec)`` is
closed-form computable from the declination-bin structure, checked by
exhaustive equivalence over all 6,291,458 tiles. The recorded battery run
is ``tools/tessellation/certification-2026-08-06.txt`` in ``rapid_systems``.

The three payload scripts —
``pipeline/loadPSFCatIntoDBSourcesTable.py``,
``pipeline/crossMatchSources.py``,
``pipeline/forcedPhotometryForField.py`` — now construct
``RomanTessellationClosedForm`` instead of ``RomanTessellationNSIDE512``
and compute every answer arithmetically. The closed form was verified
against the carried copy tile-for-tile: identical rtids, identical
neighbour sets, identical centres, and corners agreeing to within the
2-float32-ULP outward widening SQLite's R-tree applies by design.


The image change, exactly
-------------------------

**1. Remove the COPY layer.**
``rapid_systems containers/rapid-pipeline/Containerfile``, the block
commented "The Roman tessellation database (D-tessellation-baked)" and
the line it introduces::

    COPY roman_tessellation_nside512.db /work/roman_tessellation_nside512.db

Delete the ``COPY`` and its comment block. Nothing else in the
Containerfile references the file.

**2. Remove the build-context staging.**
``rapid_systems containers/rapid-pipeline/build.sh``: the
``# ---- tessellation database (build input, not source) ----`` block
(the ``tess_bucket`` / ``tess_key`` / ``tess_db`` / ``tess_bytes`` /
``tess_sha256`` variables) and the ``# ---- 3b. stage the tessellation
database into the build context ----`` step that downloads and verifies
it. Removing 3b takes ~1.4 GiB and its size/sha256 checks out of every
build.

**3. Remove the environment variable from both job definitions.**
``ROMANTESSELLATIONDBNAME`` was added to the science and bulk job
definitions on 2026-08-05 (revisions 5 → 6). It is read by no surviving
payload path. Removing it is a job-definition revision, the same shape
as the change that added it — two ``Modify`` / ``Replacement False``,
nothing wider.

**4. Leave the S3 copy in place.**
``s3://rapid-build-artifacts-<account>/tessellation/roman_tessellation_nside512.db``
should NOT be deleted. It is the artifact the builder was proved
row-identical against, and that proof is re-runnable
(``certify.py --compare-sqlite``) only while the file exists. It costs
~$0.03/month in the build-artifacts bucket, which is not worth trading
for the ability to re-verify a derivation.


What must NOT be removed
------------------------

``database/modules/utils/roman_tessellation_db.py`` and its
``RomanTessellationNSIDE512`` class stay. The class is marked deprecated
in its docstring, but it is still constructed by the legacy-fenced
launcher scripts ``awsBatchSubmitJobs_launchSingleSciencePipeline.py``
and ``awsBatchSubmitJobs_launchSingleReferenceImagePipeline.py``, and by
``database/scripts/compute_fields.py``,
``database/sims/db_register_socsim_files.py`` and
``soc/apt/sca_breakdown_of_fields_imaged.py``.

Those all run **outside** the container, on hosts where the SQLite file
is available by other means, so removing the bake does not break them —
but removing the class would. The class goes when the legacy launchers
go, behind W6's cutover fence, not with the image layer.


Expected effect
---------------

The image shrinks by the size of the tessellation layer (1,535,762,432
bytes uncompressed). The practical wins are on the pull and the hot path
rather than on disk:

* Every host pulling the image transfers ~1.4 GiB less.
* Builds stop downloading and checksumming a 1.4 GiB build input.
* ``loadPSFCatIntoDBSourcesTable`` no longer issues one R-tree query per
  detected source — thousands per SCA — and instead resolves the whole
  catalog in one vectorized pass with no I/O.

A correctness gain comes with it. SQLite's R-tree rounds bounding boxes
outward, so adjacent tiles overlapped by one float32 ULP and a source
landing in that band was assigned to whichever tile the R-tree traversed
first. The closed form is defined against the canonical tile edge and is
deterministic everywhere.


Verifying the retirement
------------------------

After the rebuild, one job per type on the production queue should run to
completion with ``ROMANTESSELLATIONDBNAME`` unset and ``/work`` empty.
The 2026-08-05 carry record
(``rapid_systems docs/history/2026-08-05-tessellation-carry.md``)
established the proof shape for the opposite direction; the same job,
inverted, is the proof here — the stages that used to abort on the unset
variable now run without it.
