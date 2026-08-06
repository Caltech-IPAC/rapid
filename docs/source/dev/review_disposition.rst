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

FixA — protocol and runtime
---------------------------

Twenty-one findings: #1–6, 9–18, 24, 25, 26, 28. Every row below was verified
against the code before anything was changed, and the citation in the
"Disposition" column is the line that proves the verdict — not the line the
review named. That distinction earned its place: of the three findings the
prior partial run sampled, two differed from their summaries and one
materially, so this round re-verified every citation rather than fixing from
the summary.

.. list-table::
   :header-rows: 1
   :widths: 4 26 40 30

   * - #
     - Finding
     - Disposition
     - Test
   * - 1
     - The live Batch definitions pin a payload image that predates required
       fixes.
     - **Confirmed, not FixA's to fix.** The ``recorder.failed`` defect the
       review cites is already correct at HEAD (``job.py`` reads the property,
       it does not call it); what is stale is the IMAGE. W8 owns the rebuild.
       FixA's obligation is that everything the rebuilt image needs is merged,
       and it now is: both FixA commits plus migration 017, applied. **W8 must
       not treat this row as satisfied** — the rebuild has not happened.
     - n/a (image build)
   * - 2
     - Submission happens before attempt rows are created.
     - **Confirmed and fixed.** ``seams.py`` called ``submit_batch`` first and
       ``_precreate`` after it — the exact race its own docstring says the seam
       exists to prevent. The order is now the documented one. Because Batch
       has assigned no child ids at that point, rows are created without them
       and backfilled after ``SubmitJob`` — the ordering
       ``observability/attempts.py`` was built for and documents. A
       ``SubmitJob`` failure raises ``SubmissionFailed`` and leaves the rows:
       they are correct, they simply have no scheduler job, which is the case
       the submission-anchored horizon exists to classify. Rolling them back
       would destroy the only evidence work was intended and would race a
       child that may in fact be running.
     - ``test_seams.py``:
       ``test_the_rows_are_created_before_submit_job`` (a shared call clock,
       because order is not observable from the recorded arguments),
       ``test_the_scheduler_job_ids_are_backfilled_after_submit_job``,
       ``test_a_submit_job_failure_leaves_reconciliation_cases_not_orphans``
   * - 3
     - Logical-job identity collides globally across runs.
     - **Confirmed and fixed.** ``ProcessingUnit.key`` is ``exposure/sca`` and
       ``logical_jobs`` has a global primary key on it, so reprocessing one
       exposure/SCA under a second run hit ``ON CONFLICT DO NOTHING`` and
       silently kept the FIRST run's binding. Both halves were individually
       right — the conflict clause exists so a replayed submission cannot
       rewrite a binding a running attempt believes in — which is why this
       was invisible. The key is run-scoped now
       (``ProcessingUnit.logical_job_key``), defined once because both the
       submitter and the runtime must compute it identically, and a conflict
       is VERIFIED against the recorded binding rather than ignored
       (``LogicalJobConflict``).
     - ``test_attempts.py``: ``LogicalJobConflictTests`` (3);
       ``test_seams.py``:
       ``test_rows_are_keyed_by_the_id_the_runtime_will_resolve_with``,
       ``test_the_run_scoped_key_is_the_one_the_runtime_computes``
   * - 4
     - Scheduler retry histories are misnumbered and incompletely
       represented.
     - **Confirmed and fixed, both halves.** The index derivation sorted by
       start time with never-started attempts LAST, so the reviewer's exact
       case — attempt 1 fails provisioning, attempt 2 succeeds — numbered the
       successful second attempt 1 and every row paired with the wrong
       observation. Batch appends attempts as they are made, so list position
       IS scheduler order; the two derivations agree on any history where
       every attempt started, which is why sorting looked right. Separately,
       every observation received the JOB's status, so a job that failed once
       and then succeeded reported SUCCEEDED for both attempts and a
       started-then-reclaimed attempt was categorised ``internal_error``
       rather than ``scheduler_reclaimed``. Each attempt now carries its own
       derived state.
     - ``test_scheduler.py``:
       ``test_a_never_started_attempt_keeps_its_own_position``,
       ``test_each_attempt_carries_its_own_state_not_the_jobs``,
       ``test_a_started_then_reclaimed_attempt_is_categorised_as_reclaimed``
   * - 5
     - Production registration is a dry run that reports registrations as
       successful.
     - **Confirmed, and broader than reported.** The review says both dispatch
       paths omit the callback; in fact they disagree — ``seams.py`` threads
       one through, the ENTRYPOINT path passed none, so that path was an
       unconditional dry run returning ``registered=N`` and exit 0 while
       writing nothing. Three changes close it: a missing callback is refused
       unless ``dry_run=True``; a dry run counts into ``would_register``,
       never ``registered``; and the registered watermark (migration 017)
       advances only on a real registration. The watermark is a SEQUENCE, not
       a boolean, which is what makes "reprocesses on a later supersession"
       expressible — the package docstring already claimed that behaviour and
       nothing implemented it. **Not fixed:** no product registrar exists in
       the tree, so the job type runs as a labelled decision pass. Writing one
       means inventing the operation-table schema, which belongs to the layer
       that decides what a registered product is.
     - ``test_consumer.py``:
       ``test_a_missing_callback_is_refused_unless_a_dry_run_is_asked_for``,
       ``WatermarkTests`` (3);
       ``test_seams.py``:
       ``test_a_decision_pass_leaves_every_attempt_a_candidate``
   * - 6
     - Science products and product provenance never reach the terminal
       record.
     - **Confirmed and fixed.** Stages accumulate into ``StageContext`` and
       the entrypoint passed only the runtime ``Provenance``. Both
       ``context.provenance`` and ``context.products`` now reach
       ``build_terminal_record``; products as a list of named entries, because
       a consumer iterates them and each needs its name beside its facts.
       Absent rather than empty where a job produced none — a registration job
       has no science products, and ``[]`` would claim it looked and found
       none.
     - ``test_termination.py``: ``ScienceProvenanceAndProductsTests`` (3)
   * - 9
     - A claim-before-start crash creates a state the reconciler cannot
       legally recover.
     - **Confirmed and fixed; the review's citation was imprecise.** The write
       is not in ``job.py`` — the entrypoint's order is already
       snapshot-then-start. It is ``resolve_ownership`` passing
       ``application_attempt_index`` into the resolver, which writes it at
       CLAIM time, before both. That column is the DDL's evidence the
       application RAN (``013``:622 forbids it in ``terminal_without_start``),
       so a container killed in that window left a row that could not legally
       be closed as never-started — the specification's own legal window
       ("a crash before that write is terminal-without-start, no work
       occurred, by construction"). Migration 017 splits the claim into
       ``application_claim_index``; the started CAS writes the attempt index.
     - Resolver battery case 10 (claimed-but-unstarted row closes as
       ``terminal_without_start``); ``test_attempts.py``:
       ``test_the_started_index_falls_back_to_the_resolvers_claim``
   * - 10
     - The started/application-closed writes are not compare-and-set, and the
       snapshot key is not bound in the database.
     - **Confirmed and fixed, both halves.** Both transitions matched on
       ``attempt_id`` alone; both are now guarded on the state the transition
       may leave, so a second writer gets ``AttemptNotFound`` instead of
       overwriting the first's account. And ``start_attempt`` took the
       snapshot key and only LOGGED it — the row bound the digest and not the
       object holding it, so the key had to be re-derived from the mutable
       records prefix, which is exactly what content-addressing exists to
       avoid. Migration 017 adds ``config_snapshot_key``; ``mark_started``
       writes it in the same statement.
     - ``test_attempts.py``:
       ``test_started_is_a_compare_and_set_on_the_submitted_state``,
       ``test_started_binds_the_configuration_snapshot_key``,
       ``test_a_started_row_that_left_submitted_is_not_overwritten``
   * - 11
     - The "complete" execution binding is optional and never cross-checked
       against Batch.
     - **Confirmed and fixed.** ``job_definition_rev`` and
       ``release_identity`` were optional, so an incomplete binding was
       accepted, copied onto every retry, and reconciliation recorded
       agreement because there was nothing to compare against. All five fields
       are required at construction: the DDL's minimum is not the design's
       requirement ("the COMPLETE submission-time execution binding"). The
       reconciler now compares the observed ``jobDefinition`` against the
       recorded binding — the observation carried it all along and nothing
       looked — and writes drift INTO the closure record, not merely a log.
     - ``test_attempts.py``:
       ``test_an_incomplete_binding_is_refused_at_construction``
   * - 12
     - The accepted route vocabulary includes payloads with no
       implementation.
     - **Confirmed and fixed.** A manifest naming reprocessing, catalog-load
       or crossmatch passed validation, CLAIMED AND STARTED an attempt, and
       only then raised inside ``_execute`` — becoming an application failure:
       a row, a bundle, a terminal record and a failed attempt, all describing
       a submission that should never have been accepted. Rejected at the
       route boundary now, before ownership. The matrix is NOT trimmed: it
       carries each type's class, queue and lane, which stay true while the
       implementation catches up, so ``IMPLEMENTED_JOB_TYPES`` is a separate
       set held to the sequence registry by a test.
     - ``test_routes.py``:
       ``test_a_routable_but_unimplemented_job_type_is_rejected_at_the_boundary``,
       ``test_the_implemented_set_matches_what_the_payload_actually_has``
   * - 13
     - Release-content and tessellation provenance is neither recorded nor
       enforced.
     - **Confirmed and fixed.** The entrypoint called ``load()`` where
       ``load_with_digest()`` exists. And ``check_version`` — whose own
       docstring says it "must not be ignored" — had no production caller at
       all, so nothing decided anything. Both are wired: the release digest
       goes into provenance, the tessellation version is read from RELEASE
       CONTENT (never the mutable tree, per the placement criterion) and
       checked, and a mismatch fails before any work.
     - Covered by the entrypoint suite; the check is a startup failure path
   * - 14
     - Reconciler-first closure records are not complete snapshots and claim
       evidence they never read.
     - **Confirmed and fixed, three parts.** ``_OPEN_COLUMNS`` omitted the
       runtime provenance columns, so a reconciler-first record for a started
       attempt that died dropped the source sha, container digest, config
       digest and snapshot key the row held — and ``config_digest`` was
       already being READ by ``_attempt_ran`` without ever being selected, so
       that evidence branch could never fire. ``reconstructed_from`` claimed
       ``log_stream`` while no CloudWatch content was fetched; the stream's
       NAME is now a pointer marked ``read: false``. ``reconciler_materialized``
       is set. And a never-started attempt cites its closure record, which 013
       left unreferenced from the row it accounts for (017 adds the
       reconciler-authored pair, separate from the application's, per the
       one-writer-per-field rule).
     - ``test_attempts.py``:
       ``test_a_never_started_attempt_cites_its_closure_record`` (+2 guards);
       reconciler suite's closure tests
   * - 15
     - Supersession is unreachable, and collision climbing corrupts the
       sequence embedded in the record.
     - **Confirmed and fixed, both halves.** Polling selected only the open
       states, so a terminal row was never reconsidered and "corrected
       scheduler facts produce sequence 2" could not happen. A BOUNDED
       requery revisits terminal and flagged rows closed inside 24h — past
       Batch's own retention there are no new facts to learn — and re-closes
       only where the scheduler now says something the row does not record.
       Separately ``publish_closure_record`` serialized the body ONCE and then
       climbed keys, so a superseding record was written at the sequence-2 key
       while declaring ``record_sequence: 1``, and the row stored the stale
       sequence too. The body is re-serialized at each sequence and the LANDED
       sequence goes on the row.
     - ``test_service.py``:
       ``test_a_terminal_row_whose_facts_changed_is_superseded``,
       ``test_a_terminal_row_whose_facts_agree_is_left_alone``,
       ``test_terminal_rows_outside_the_window_are_not_revisited``;
       ``test_closure.py``:
       ``test_a_superseded_record_declares_the_sequence_it_landed_at``
   * - 16
     - Bundle recovery and retention tagging fail open.
     - **Confirmed and fixed.** The reconciler caught every tagging and
       recovery failure and terminalized the row anyway — and terminal rows
       are outside the open set, so a bundle whose retention was never stamped
       then expires under the wrong lifecycle rule with nothing left to
       notice. Closure failures now DEFER: the row stays open, the next poll
       retries, and a counter surfaces a persistent one. Tag reads
       distinguish absence (``NoSuchKey``/``NoSuchTagSet`` → ``None``, nothing
       to protect) from failure (``TagsUnreadable``), because converting the
       second into the first is what let a transient read failure replace a
       failure-class retention with the shorter success expiry. The
       orchestrator's missing ``GetObject`` — which the create-once validation
       path needs — is granted in ``rapid-service-identities.yaml``.
       **Staged for W8:** bundle RECONSTRUCTION from the stream is not
       implemented. The fail-open it was hiding behind is removed either way,
       which is what the finding turns on.
     - ``test_retention.py``:
       ``test_an_unreadable_tag_set_raises_rather_than_reading_as_untagged``,
       ``test_an_unreadable_tag_set_never_permits_a_shortening_rewrite``,
       ``test_an_absent_bundle_is_not_an_error_to_stamp``;
       ``test_service.py``:
       ``test_a_closure_failure_leaves_the_row_open_rather_than_terminal``
   * - 17
     - No live stage spans are written to PostgreSQL.
     - **Confirmed and fixed.** ``StageRecorder()`` was constructed with no
       write callback and the recorder silently returns when ``_write`` is
       absent, so spans accumulated in memory and were written only at
       termination — meaning exactly the attempts that most need
       stage-boundary evidence (the ones that die before termination) had
       none. The callback is wired. A failed span write is logged, never
       fatal: it is diagnostic evidence about work that already happened, and
       the spans still reach the terminal record.
     - Covered by the entrypoint suite; the writer path is
       ``stage_writer_for``
   * - 18
     - Product object identities overwrite across runs and retries.
     - **Confirmed and fixed.** Upload prefixes were
       ``job_type/exposure/sca`` — no run, no attempt — so reprocessing or
       retrying one exposure/SCA overwrote the earlier attempt's objects and
       left old records and checksums pointing at keys whose bytes had
       changed, which is what the storage design's immutable-keys rule
       forbids. ``StageContext.product_prefix`` is the one place that key is
       built and carries both. A context with no identity produces a prefix
       that SAYS so rather than silently reproducing the colliding shape. The
       two consuming call sites are FixB's files; FixB merged without
       consuming the API, so the two-line swap was made here rather than
       leaving the finding half-fixed.
     - ``test_context.py``: ``ProductPrefixTests`` (5)
   * - 24
     - The supervised reconciler can remain "healthy" while doing no work.
     - **Confirmed and fixed.** ``run_forever`` caught every poll exception
       forever, so a dead connection or an expired rotated credential made
       every poll fail while systemd saw a healthy process. Consecutive
       failures past a stated threshold now raise ``ReconcilerUnhealthy`` and
       exit for restart — the bounded mechanism the observability policy asks
       for (a threshold, a state change when crossed, a supervisor that acts).
       A successful poll resets the count so transients cannot accumulate
       across unrelated minutes. Restart is the right response because the
       conditions that produce it are exactly the ones a fresh process
       re-establishes.
     - ``test_service.py``: ``HealthTests`` (3)
   * - 25
     - The static scheduler retry contract omits a documented pre-application
       failure.
     - **Confirmed and fixed.** Both definitions retry selected host and
       container reasons then apply a catch-all EXIT, so ``DockerTimeoutError``
       terminated the child after attempt 1 with no runtime row, no record and
       no retry. AWS names it beside ``CannotPullContainerError`` — "Jobs fail
       before the application runs — some jobs might fail because of a
       DockerTimeoutError error or a CannotPullContainerError error"
       (``bestpractice7.html``, read 2026-08-06). Matched ``OnReason`` like
       its siblings: it is a container-level reason, not a job statusReason.
     - cfn-lint/cfn-guard; the rule is declarative
   * - 26
     - Migration 015 does not enforce its tessellation version relationship.
     - **Confirmed and fixed.** ``ON DELETE RESTRICT`` is principle 5's "a
       version is retired only when no attempt references it" in DDL. Added
       ``NOT VALID`` then validated as a separate statement, so the 6.3M-row
       scan takes ``SHARE UPDATE EXCLUSIVE`` rather than holding
       ``ACCESS EXCLUSIVE`` across it.
     - Rehearsal cycle 3, full stream 000 → 017 from empty
   * - 28
     - The ownership resolver can return an identity-contradictory row.
     - **Confirmed and fixed; the review's low-confidence label on
       reachability was right at the time and is now moot.** (a) The
       application-index fast path matched on
       ``(scheduler_job_id, application_attempt_index)`` alone and never
       checked run or logical job, so a mismatched manifest resolved to
       another run's row. (b) The scheduler-index path attempted a
       compare-and-set and returned the row REGARDLESS of whether it matched,
       handing a row already claimed as attempt 1 to a caller claiming
       attempt 2. Both now raise, naming the conflict. The review noted the
       path was unreachable because the runtime omits the scheduler index and
       the reconciler does not call the resolver — but #4 wires the reconciler
       through it in this same round, so the path becomes reachable exactly
       when the fix lands. That is the argument for fixing now rather than
       recording it as latent.
     - Resolver battery cases 8 and 9 (four assertions: cross-logical-job,
       cross-run, second claimant, and the first claim surviving)

Two things the verification found on its own
--------------------------------------------

**The reconciler suite was already red at HEAD.** Every test whose path
reaches ``lease.reread_attempt`` failed with ``TypeError: argument 2 must be a
connection or a cursor`` — ``Composed.as_string()`` needs a real connection and
``FakeConnection`` is not one. It reproduces on the unmodified branch. It
stayed invisible because W5's in-image runner never ran
``pipeline/reconciler/test``; FixA's runner does, and the stub now renders
composed SQL without a connection.

**Several test doubles could not tell a fix from its absence.** The runtime
stub executor returned 1 whatever the ``WHERE`` clause said, so a
compare-and-set looked identical to the unconditional ``UPDATE`` it replaced
and #10's tests would have passed against either. ``FakeS3Tagging`` raised a
bare ``KeyError``, so absence and failure were indistinguishable and #16's
distinction could not be asserted. Both now model the behaviour the fixes turn
on. This is the same class of problem as FixB's "the W5 gate could not fail":
a test that cannot fail is not evidence.

Verification
------------

Nine suites in-image on ``rapid-admin``, 870 tests, exit 0 throughout
(``FIXA-UNITS-OK``): reconciler 103, seams 16, entrypoint 21, stages 74,
runtime 296, submission 128, observability 127, registration 18, database 87.

Migration 017 rehearsed green on a throwaway across three cycles — full stream
000 → 017 from empty, the constraint matrix, and a 31-assertion resolver
battery — then applied to ``rapid-db`` on the first attempt. The rehearsal
caught three defects, none visible on inspection: a ``SET ROLE`` that could not
own ``attempts``, a claim-uniqueness key that made a retry collide with its own
predecessor, and a fast path that stopped finding a replaying claimant's row.
Each would have half-migrated the live database.

Live proof
~~~~~~~~~~

``pipeline/reconciler/test/live_fixa_probe.py``, run against **rapid-db**
(2026-08-06, after migration 017 was applied): **18 of 18 assertions proven**,
``FIXA-LIVE-OK``. It drives the REAL resolver as amended by 017, the REAL
lifecycle constraints, and the REAL rowcount contract — none of which a stub
can stand in for, because a stub cannot refuse a state.

What it proved that the unit suite could not:

* the claim/index split makes ``terminal_without_start`` REACHABLE — a
  claimed-but-unstarted row was driven into that state, which is the window
  013 made illegal (#9);
* the started transition is a compare-and-set against the real driver's
  rowcount: the second starter matched nothing (#10);
* the snapshot key is bound in the same statement (#10);
* two runs over one exposure/SCA keep their OWN bindings, and a conflicting
  binding raises rather than being absorbed (#3);
* the resolver refuses to resolve across identities, and refuses a second
  claimant on a row already claimed — the error naming the conflict (#28);
* a never-started attempt cites its closure record (#14);
* the open set includes recently-closed terminal rows, which is what makes
  supersession reachable at all, and it selects the runtime provenance
  columns a reconciler-first record is built from (#15, #14).

**Two defects in the probe's own first run were real and are recorded rather
than quietly fixed.** ``3/a-conflicting-binding-raises`` failed because the
probe asserted the wrong substring; ``28b/refuses-a-second-claimant`` failed
because the scenario reached the resolver's create path rather than the
scheduler-index path the finding is about — the row has to be reachable BY
SCHEDULER INDEX for 013's defective branch to be taken at all. Both were
probe bugs, not fix bugs, and both are the kind that would have made a
passing probe meaningless.

**What the live run did NOT prove.** One full ``poll_once`` against live Batch
needs ``batch:DescribeJobs``, which belongs to the ORCHESTRATOR role — the
reconciler's real host — not to ``rapid-db-instance-role``, and the probe runs
on rapid-db because that is where the pooler is. The orchestrator host is not
currently running. The cycle is attempted and its absence REPORTED by the
probe rather than skipped silently; widening a role for a probe would have
been the wrong trade. **W8 owns running it on the orchestrator host.**
