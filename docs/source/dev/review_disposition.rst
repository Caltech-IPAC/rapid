.. _review_disposition:

External implementation review — disposition
============================================

An external review of the W1–W7 implementation raised 29 findings. This page
records what was done about each one, by whom, and against what authority.

The work was split between two rounds running concurrently: **FixA** owns the
protocol and runtime findings (the entrypoint, seams, reconciler, registration,
submission and observability), **FixB** owns the science-fidelity findings
(the extracted stage bodies) and the tessellation certification honesty item.
Rows below are grouped by owner; each round appends its own.

.. note::

   Where a finding says the extracted code deviates from the deleted monolith,
   the monolith is the authority — ``5664024^:pipeline/awsBatchSubmitJobs_
   runSingleSciencePipeline.py`` and ``...runSinglePostProcPipeline.py``. The
   ratified skeleton-rewrite ruling was that algorithmic stage bodies were
   extracted as-is, and that only three things were allowed to change:
   parameter passing, configuration source, and tool invocation. Every other
   difference is a defect unless the monolith itself was broken — and where
   that was the case it is called out explicitly below.

FixB — science fidelity
-----------------------

.. list-table::
   :header-rows: 1
   :widths: 4 26 40 30

   * - #
     - Finding
     - Disposition
     - Authority
   * - 7
     - The SFFT invocation cannot be parsed.
     - **Fixed.** The argv carried six positional inputs and the flags
       ``--sci_star_list``, ``--ref_star_list``, ``--crossconv_flag``; the
       tool's parser takes two positionals and ``--scicat``/``--refcat``/
       ``--crossconv`` (the last a ``store_true``, so it never takes a value).
       Every SFFT-enabled science job exited argparse with status 2. The
       branch was also inverted: the monolith passes the gain-match catalogues
       and gentle bright-source masking (50.0 over 100 px) in the
       *non*-rimtimsim case, and no catalogues with hard masking (20000.0 over
       30 px) in the rimtimsim case. Restored, including ``--scipsf``
       unconditionally and ``--refpsf``/``--scisegm``/``--refsegm`` under
       cross-convolution, plus the crossconv-dependent output filenames the
       tool actually writes.
     - Monolith 1849-1892; parser
       ``modules/sfft/sfft_rapid_rimtimsim.py:312-328``
   * - 8
     - Reference-image paths deterministically raise ``KeyError``.
     - **Fixed.** ``clippedmed`` (four sites), ``datascale``, ``gmin``,
       ``gmax``, ``npixsat`` and ``npixnan`` are not keys the statistics
       helper returns. The real names are ``gmed``, ``gsigma``, ``gdatamin``,
       ``gdatamax``, ``satcount`` and ``nancount``, which is what the monolith
       read. Every dedicated reference-image job died here, as did any science
       job that had to build its own reference.
     - Monolith 476, 482, 448-454; helper
       ``modules/utils/rapid_pipeline_subs.py:291-301``
   * - 19
     - SFFT and naive branches silently use ZOGY's detection and uncertainty
       model.
     - **Fixed.** Both variants detected on ZOGY's Scorr image, weighted with
       ZOGY's uncertainty image and fitted with ZOGY's difference PSF. Each
       now builds its own uncertainty image via ``compute_diffimage_
       uncertainty``, uses its own PSF — SFFT's difference PSF for SFFT, the
       *reference* PSF for naive, carrying the monolith's own ``TODO`` — and
       detects on the right image: the cross-convolved image when SFFT
       cross-convolves, the SFFT difference when it does not, and the naive
       image itself for naive. The naive branch had also dropped its
       coverage-map masking entirely, and its fake-source header stamping.
     - Monolith 1972-1984, 2031-2034, 2084-2087, 2171, 2469-2476, 2527-2539,
       2651, 2750
   * - 20
     - Positive and negative PSF catalogs overwrite each other, and their
       schemas were reduced.
     - **Fixed.** Both signs resolved to one configured filename, so the
       negative invocation overwrote the positive catalogue and both product
       names carried negative bytes; the negative variants now derive their
       three filenames by ``.replace(".txt", "_negative.txt")`` (and
       ``.fits`` → ``_negative.fits`` for the residual), as the monolith did.
       The schema was separately reduced: the extraction wrote ``phot`` alone
       in a different ascii format, never computed sky coordinates, never
       wrote the finder catalogue, and parqueted the *unjoined* table.
       Restored: ``ra``/``dec`` columns from
       ``computeSkyCoordsFromPixelCoords``, the finder catalogue as a second
       product, and the parquet written from the inner join of photometry with
       finder results on ``id``.
     - Monolith 1500-1537, 1551-1562, 2286-2288, 2751-2753
   * - 21
     - Fake-source injection changes the uncertainty calculation.
     - **Fixed.** The clipped science average feeding the uncertainty model
       was computed *after* injection, so injected pixels entered the
       background/noise term for injection-enabled runs. The monolith computed
       it at line 798 and opened the injection block at 806, passing the
       pre-injection value into the reformat at 912. Split into its own
       ``science_image_statistics`` stage, sequenced ahead of
       ``inject_fake_sources`` — a sequence position is visible in the stage
       record where an inline computation's ordering is not.
     - Monolith 788-798, 806, 886, 899-914
   * - 22
     - Inline science reference construction dropped the PhotUtils reference
       catalog.
     - **Fixed.** ``generatePhotUtilsReferenceImageCatalog`` was absent, so a
       science job building its own reference produced no reference PSF or
       finder catalogues and no corresponding checksums — while the dedicated
       reference-image pipeline, running the same coadd, produced both. Call
       restored with its products and checksums recorded. The same block's
       saturation-rate divisor was also corrected: see "Monolith defects" below.
     - Monolith 487-535
   * - 23
     - Post-processing stamps the reference PPID into the difference image.
     - **Fixed.** The monolith read a single ``ppid`` from ``[SCI_IMAGE]``
       (value 15, the image-differencing pipeline) and stamped that one value
       into both the reference-image and difference-image headers. The
       extraction invented a ``reference_image_ppid`` manifest fact and used it
       for both, with different defaults (12 and 15) — so with a reference
       built by the dedicated pipeline the difference product was labelled as
       produced by pipeline 12. Now ``ppid_for(JOB_TYPE_SCIENCE)``, the route
       matrix's own value, which needs no manifest fact: the identifier is a
       property of the pipeline, not of the unit.
     - Post-process monolith 158, 238, 297; ``.ini`` ``[SCI_IMAGE] ppid = 15``
   * - 27
     - The tessellation certification claims more coverage than it performs.
     - **Fixed (documentation), and the open question recorded rather than
       closed.** ``RomanTessellationClosedForm``'s docstring claimed the
       neighbour expansion "was checked tile-for-tile against the SQLite
       class's own answers". It is not: ``certify.py:check_adjacency`` draws
       3,004 tiles and checks *invariants* on them — symmetry, no
       self-reference, declination spans that touch — and never compares a
       neighbour set against SQLite. Tile identity genuinely is exhaustive
       over all 6,291,458 rows. The docstring now distinguishes the two
       depths and points at the open mathematical-completeness question
       instead of implying it is closed. See
       :ref:`tessellation_certification_open` in ``rapid_systems``.
     - ``rapid_systems/tools/tessellation/certify.py:138``

Beyond the review's sample
--------------------------

The review sampled the extracted bodies; FixB swept all of them. Two further
defect classes surfaced, both of the same shape as findings the review did
catch.

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Defect
     - Disposition
   * - Thirteen release-content keys dropped by the W4B configuration
       migration while stage code still read them by name.
     - **Fixed.** The migration from the master ``.ini`` to
       ``cdf/science/pipeline.toml`` carried the numeric knobs and left behind
       every output-filename key: the psfcat triples for all three
       difference-image variants (9) and for the reference image (3), ZOGY's
       three outputs, awaicgen's three, bkgest's two, and the naive
       difference's one. Each is a ``KeyError`` in a stage that has already
       done real work — the same failure mode as finding 8, in a different
       place. All restored from the ``.ini`` verbatim, each with a comment
       citing its source line.
   * - ``prepare_zogy_inputs`` clipped ZOGY's input reference statistics at the
       saturation *rate*.
     - **Fixed.** The monolith read ``saturation_level_refimage`` once,
       undivided, and used it for these statistics (lines 304, 1117). The
       extraction reached for the ``saturation_level_refimage`` *product*,
       which is that level already divided by an exposure time, and fell back
       to a ``ref_image.saturation_level`` key that does not exist in release
       content whenever the reference had been downloaded rather than built.
       Now read straight from release content, undivided, in both branches.

Monolith defects, fixed deliberately in both directions
-------------------------------------------------------

One finding turned on a difference between the two monoliths rather than on an
extraction error. Recorded here because the conservative default — reproduce
monolith behaviour — is ambiguous when the monoliths disagree.

``saturation_level_refimage_rate``
    The science monolith divides the reference saturation level by the science
    image's exposure time (line 549); the dedicated reference-image pipeline
    divides by a literal ``60.0`` (its line 428). Both carry a ``TODO`` calling
    the stopgap incorrect. The extraction had copied ``/60.0`` into *both*
    paths, so the science pipeline's inline reference build silently used the
    other pipeline's constant.

    **Disposition:** each path reproduces its own authority — ``science.py``
    divides by ``exptime``, ``reference_image.py`` by ``60.0`` — and the
    divergence is left in place rather than unified. Unifying them would be a
    science change, and the ``TODO`` both files carry says the right answer is
    neither: the reference image's exposure time needs standardising first.
    **Proposed decision, for the owner:** decide the standardisation, then make
    both paths agree. Not taken unattended.

Systematic fidelity audit
-------------------------

The review sampled; this is the sweep. Every extracted stage body in
``pipeline/stages/`` was compared against its monolith lines: **28 stages**
across three job types (17 science, 8 reference-image, 3 post-process), plus
the two parameterized catalogue templates and the sequence table.

Counts: **13 deviations found**, of which **13 fixed** (the eight review
findings above, plus the five in "Beyond the review's sample" and the monolith
divergence, counting the dropped-config-keys class as one). **Four differences
are intentional** under the ratified ruling and are listed below so that a
future reader does not mistake them for drift.

.. list-table:: Intentional differences — the three permitted categories, and
                the structural collapse declared at extraction
   :header-rows: 1
   :widths: 22 78

   * - Category
     - Where it appears, and why it is not a defect
   * - Configuration source
     - Every ``ConfigParser`` section dict becomes
       ``context.science_section(...)`` (release content) or
       ``context.parameter(...)`` (operational tree), and every per-invocation
       ``.ini`` value becomes ``context.fact(...)``. This is the W4B placement
       criterion. It also removes the monolith's save/revert protocol around
       ``sextractor_diffimage_dict``: each call now starts from a copy, so
       there is nothing to revert and nothing to miss.
   * - Tool invocation
     - ``util.execute_command`` / ``execute_command_in_shell`` become
       ``run_tool`` / ``run_shell``, which raise ``ToolError`` instead of
       returning an exit code that the monolith's unreachable ``>= 64`` test
       discarded. ``/usr/bin/python3.11`` becomes ``sys.executable`` — the name
       in the image is a legacy symlink to Python 3.14, so the literal was
       already lying.
   * - Prefix and path API
     - Every cwd-relative filename becomes ``context.scratch(...)`` in the
       per-attempt tree, which is what lets two array children share a host.
       Products flow through ``context.products`` instead of being rebound as
       module globals. The six interleaved S3 upload blocks collapse to one
       upload of everything produced.

       This category also covers SFFT's dropped ``"./"`` positional prefix.
       The monolith prepended ``"./"`` to both positionals, calling it "a quirk
       in the SFFT software"; the tool derives its output paths from
       ``os.path.dirname(sciim)``, so ``"./name.fits"`` put outputs in the
       container's cwd. Absolute scratch paths put them in the per-attempt
       tree, which is the point of the tree. Verified against
       ``sfft_rapid_rimtimsim.py:206-217``.
   * - Structural (documented at extraction)
     - The six SExtractor blocks and six PSF-catalogue blocks — about 1,000 of
       the monolith's 2,961 lines — collapse into two parameterized helpers,
       with each call site passing exactly what its block hardcoded. This was
       declared in ``science.py``'s module docstring at extraction time and is
       not a new difference; it is however the mechanism by which findings 19
       and 20 were introduced, because a parameter that every call site passes
       identically stops being a parameter in practice. Both helpers now take
       the per-variant weight image, PSF, detection image and sign explicitly.

Verification
------------

The full W1–W7 suites were run in-image on ``rapid-admin`` at the base branch
state, before and after: 619 tests, all green, ``W5-UNITS-OK``. The stages
suite grew from 27 to 69 tests. The new regression tests assert against real
artefacts rather than restated expectations — the SFFT argv is parsed by the
tool's own parser, lifted from its source; the statistics key names are read
out of the helper's own source; every release-content key a stage reads is
checked against the real TOML. Both sides were wrong together before, which is
exactly what a test of the code against itself cannot catch.

Running that verification also found that it could not fail: see the W5 runner
and exit-code-proof fixes recorded in the commit log for this round.
