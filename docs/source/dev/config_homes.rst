Configuration homes: one fact, one place (W4, 2026-08-06)
=========================================================

The batch-payload co-design's fourth principle is that every
configuration fact lives in exactly one of three homes, and that
"literals duplicated across ``.ini``/Python/shell are deleted, not
synchronized". This document records where each fact now lives, and why.

It is the reference for a question that used to have no answer: given a
value the pipeline uses, where is it defined? Before W4 the honest answer
for most of the values below was "in two to four places, and nothing
keeps them equal".

The three homes
---------------

**The pipeline parameter tree** — ``/rapid/pipeline/*`` in SSM Parameter
Store, deployed from ``rapid_systems`` ``cloudformation/rapid-pipeline-params.yaml``.
Operational configuration: bucket names, endpoints, Batch targets, mode
toggles, submission cadence. Mutable between releases; read at job startup
and hashed into the attempt record's configuration digest, so an edit is
provenance-visible.

**Release content** — ``cdf/science/pipeline.toml`` in this repo, plus the
SExtractor ``.conv`` / ``.nnw`` / ``.inp`` files beside it in ``cdf/``.
Anything that can alter a science product. Carried by the image and
therefore identified by the image digest already recorded in every
attempt's provenance.

**The submission manifest** — built by ``submission/`` and written to S3
per submission. Per-invocation facts: which SCA, which exposure, which
reference image, and the database-derived answers the launcher used to
write into a per-job ``.ini``.

The placement criterion
-----------------------

Between the first two homes, one question decides: **can this value alter
a science product?**

If yes, it is release content. The reason is reproducibility rather than
tidiness: a science-affecting value that can change without any release
changing makes the release identity insufficient to reproduce a product.

If no, it is operational configuration and belongs in the tree, where it
can be tuned without an image rebuild.

Per-invocation facts are neither: they differ for every child of an
array, so folding them into either of the other two homes would make one
configuration describe many different jobs.

The fact table
--------------

.. list-table::
   :header-rows: 1
   :widths: 22 16 32 30

   * - Fact
     - Home
     - Where exactly
     - Notes
   * - Product / alert bucket names
     - tree
     - ``s3/products-bucket``, ``s3/alerts-bucket``
     - Names, not ARNs; permanent, never renamed.
   * - Manifest key prefix
     - tree
     - ``s3/manifest-prefix``
     -
   * - Batch queue names
     - tree
     - ``batch/queue-prompt``, ``batch/queue-bulk``
     - The route matrix carries the parameter KEYS, never the names.
   * - Job-definition names
     - tree
     - ``batch/job-definition-science``, ``batch/job-definition-bulk``
     - Unversioned on purpose: SubmitJob resolves the active revision.
   * - Kafka endpoint, registry, topic, max request size
     - tree
     - ``kafka/*``
     -
   * - Submission cadence
     - tree
     - ``submission/max-batch-size``, ``submission/max-wait-seconds``
     - PROPOSED values; the smoke run sets them for real.
   * - Database endpoint and secret id
     - tree
     - ``db/server``, ``db/port``, ``db/name``, ``db/secret-id``
     - **Added by W4.** The connection helper already demanded these come
       from the tree; nothing supplied them. ``db/server`` is PROPOSED —
       see "Open items" below.
   * - Minimum images to coadd
     - release content
     - ``[science] min_images_to_coadd``
     - **Left the tree in W4.** Coadd depth changes the reference image.
   * - Differencing flavour
     - release content
     - ``[science] diff_flavor``
     - **Left the tree in W4.** Selects which product feeds the alert
       cutout.
   * - Reference-image pixel scale
     - release content
     - ``[ref_image] cdelt1_refimage``, ``cdelt2_refimage``
     - Was also an argparse default in ``modules/coadd/coadd.py``, now
       read from here.
   * - Reference-image geometry
     - release content
     - ``[ref_image] naxis1_refimage``, ``naxis2_refimage``,
       ``crota2_refimage``
     -
   * - Detector gain and read noise
     - release content
     - ``[instrument] sca_gain``, ``sca_readout_noise``
     -
   * - SExtractor GAIN sentinel
     - release content
     - ``sextractor_gain`` in each of the four SExtractor sections
     - Four copies of ``999999999999999.9`` in one ``.ini``; still four
       keys, because the four sections are genuinely four tool
       invocations with independently settable parameters — but now one
       file, typed, and covered by the round-trip test.
   * - Source-matching radius
     - release content
     - ``[source_matching] match_radius``
     - Derived from the Roman WFI pixel scale.
   * - All other tool tuning
     - release content
     - ``[sextractor_*]``, ``[swarp]``, ``[awaicgen]``, ``[bkgest]``,
       ``[gainmatch]``, ``[psfcat_*]``, ``[zogy]``, ``[sfft]``,
       ``[forced_photometry]``, ``[fake_sources]``
     - 325 values in all.
   * - SExtractor ``.conv`` / ``.nnw`` / ``.inp``
     - release content
     - ``cdf/`` (unchanged location)
     - Release content by location; identity recorded through the image
       digest by ``science_config.auxiliary_identity()``.
   * - ppid map (12 / 15 / 17)
     - manifest vocabulary
     - ``submission/routes.py``
     - Was an ``if``/``elif`` in ``virtualPipelineOperator``, three
       ``ppid`` keys in the master ``.ini``, and bare literals in SQL.
   * - Job type → class → queue → DB lane
     - manifest vocabulary
     - ``submission/routes.py`` ``ROUTES``
     - One validated tuple; the entrypoint rejects any submission off it.
   * - Which SCA / exposure this job processes
     - manifest
     - ``ProcessingUnit.exposure``, ``.sca``
     -
   * - L2 file identity and metadata
     - manifest
     - ``UnitFacts.rid``, ``.expid``, ``.field``, ``.mjdobs``,
       ``.exptime``, ``.infobits``, ``.status``
     - Was written into a per-job ``.ini`` from ``get_info_for_l2file``.
   * - Filter identity
     - manifest
     - ``UnitFacts.fid``, ``.filter_name``
     -
   * - Science image location
     - manifest
     - ``UnitFacts.science_image_uri``
     -
   * - Best PSF
     - manifest
     - ``UnitFacts.psfid``, ``.psf_uri``
     - From ``get_best_psf``.
   * - Best reference image
     - manifest
     - ``UnitFacts.reference_image_id``, ``.reference_image_uri``,
       ``.reference_image_infobits``, ``.reference_image_ppid``
     - From ``get_best_reference_image``. ``None`` means one must be
       built.
   * - Coadd input count and listing
     - manifest
     - ``UnitFacts.images_to_coadd``, ``.coadd_inputs_uri``
     -
   * - Sky positions (image, tile, reference)
     - manifest
     - ``UnitFacts.sky_position``, ``.tile_position``,
       ``.reference_position``
     - Centre plus four corners each; from ``L2FileMeta`` and the
       tessellation database.
   * - Overlapping tessellation ids
     - manifest
     - ``UnitFacts.overlapping_fields``,
       ``.reference_overlapping_fields``
     -
   * - Software root (``/code``)
     - environment
     - ``RAPID_SW``
     - Not a configuration home of its own: it is where the release
       content IS, so it has to be known before any of it can be read.
       ``virtualPipelineOperator`` derived nine script paths from a
       ``/code`` literal while separately reading ``RAPID_SW``; the
       literals now come from the environment variable.

What was deliberately not converted
-----------------------------------

**The two payload scripts and the ``.sh`` wrappers.** Rewritten at W5 and
deleted there; the nine legacy launcher scripts and the four log-grep
registration scripts were deleted at W6's cutover fence.

**The master ``.ini`` itself — still present after W6.**
``cdf/awsBatchSubmitJobs_launchSingleSciencePipeline.ini`` was expected to
go with the fence. It did not, and the reason is recorded here rather
than left as a surprise.

W6's fence makes that deletion conditional: *"the master .ini (science
sections now in pipeline.toml — verify no surviving reader first,
repo-wide grep)"*. The grep was run. Thirty-five files reference the
file; removing the launchers, the four registration scripts and the VPO
still leaves **twenty-three readers**, none of them in the completion
chain — the science-layer scripts (``crossMatchSources``,
``forcedPhotometryForField``, ``loadPSFCatIntoDBSourcesTable``, the
``prune*`` and ``compute*`` and ``generate*HATSCatalog`` family), the
``scripts/`` and ``sims/`` trees, three ``database/scripts`` utilities,
and two test files.

They read sections the release content does not carry — bucket names, the
ppid map, database facts — so ``cdf/science/pipeline.toml`` does not
substitute for them. Migrating twenty-three science-facing readers is its
own work item with a real risk of silent behaviour change, and it belongs
with the configuration re-homing rather than inside the completion
chain's fence.

While both it and the release content exist they cannot drift: the
round-trip test in ``pipeline/runtime/test/test_science_config.py``
compares every extracted value against it. That test therefore stays too.

**The sims' own bucket names** (``sims-sn-*``, ``socsims-fakesrc-*``,
``rimtimsim-*``). Each names a specific simulation dataset, not a
pipeline destination — one home each already, and that home is the script
that processes that dataset.

**The legacy bucket literals in** ``scripts/generate_refim_psfs.py`` **and**
``scripts/download_files.py``. These name ``rapid-pipeline-files`` and
``rapid-product-files``, which are not in the parameter tree — the tree
carries the ``roman-rapid-*`` names. Pointing them at the tree would
change which bucket they use, which is a behaviour change rather than a
re-homing. Recorded under "Open items".

Open items
----------

1. **The database server name is instance-bound.** ``db/server`` holds
   ``rapid-db``'s EC2-assigned private DNS name, which changes if the
   instance is rebuilt, exactly as its IP would. A private hosted-zone
   record for ``rapid-db`` would make the parameter durable. Until then a
   rebuild requires a parameter edit, which the configuration digest will
   show. PROPOSED by W4, not ratified.

2. **A live divergence in the minimum coadd count.** The parameter tree
   carried ``3``; the master ``.ini`` carries ``2``. The tree's value is
   taken as authoritative in ``[science] min_images_to_coadd``, and the
   ``.ini``'s is preserved verbatim in ``[ref_image]`` so the round-trip
   proof stays honest. They converge when W5 switches the payload onto
   the release content. If ``2`` was in fact the intended operating
   value, ``[science] min_images_to_coadd`` is the one line to change.

3. **The legacy buckets above** have no home in the tree. Either they are
   retired with the legacy paths at W6, or they are added to the tree as
   operational configuration. Not decided here, because deciding it means
   deciding whether those scripts survive.

4. **The VPO's job-type vocabulary differs from the manifest's**:
   ``postproc``/``refimage`` against ``post-process``/``reference-image``.
   ``look_up_ppid_of_job_type`` accepts both, resolving each to the one
   ppid map, so neither spelling is a second home for the number.
   Converting the callers to one spelling is a separate change.
