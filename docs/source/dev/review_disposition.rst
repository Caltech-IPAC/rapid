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

A **second external review** then read that implementation and found six of
those dispositions incomplete, plus new findings. **FixC** owns round 2; its
section is at the end of this page.

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

FixC — round-2 external review
------------------------------

A second external review read the round-1 implementation and found **six
dispositions incomplete** (#4, #6, #14, #16, #18, #24), plus new findings and
the still-absent registrar. FixC owns them. Every citation was re-verified
against the code before anything was changed — the round-1 lesson was that a
review summary can be imprecise while the defect it points at is real, and in
this round every cited line was accurate.

.. list-table::
   :header-rows: 1
   :widths: 8 32 60

   * - #
     - What round 1 left incomplete
     - What round 2 did
   * - 14
     - The reconciler could not recover the record-written/row-not-closed
       crash boundary. It read ``terminal_record_key`` and
       ``terminal_record_checksum`` from the sequence-0 body or the row —
       both NULL in precisely that state, because the application sets them
       in the transition that just failed and a record cannot contain its own
       key or the checksum of its own bytes. 013 requires a non-null key for
       ``application_closed``, so every pass attempted an illegal transition
       and left the attempt ``started`` forever. Registration never saw it.
     - ``read_predecessor`` returns a ``Predecessor`` carrying the key it read
       from and the checksum it computed over those bytes; materialization
       supplies both. The reconciler already knew them — it had just located,
       read and checksummed that object to validate it. **Proven live**:
       ``live_fixc_crash_boundary.py``, 6/6 on rapid-db.
   * - 4
     - Scheduler retries still did not acquire one row per attempt. The
       service iterated rows already in PostgreSQL and never called
       ``resolve_attempt`` for a scheduler-discovered attempt, so an attempt
       Batch performed but no row represented got no binding, no category, no
       closure record and no retention account. Migration 017 says this round
       wires it ("#4 wires the reconciler through the resolver").
     - ``_resolve_discovered`` compares the scheduler's attempt indexes
       against the rows' and resolves one row per missing index — through
       ``resolve_attempt``, never a bare INSERT. Failures are per attempt.
       ``_pick_observation`` now resolves the several-observations /
       unindexed-row case deterministically to the FIRST attempt, which is
       the same rule the resolver applies, rather than returning None and
       letting the row be closed ``never_resolved``.
   * - 16
     - Recovery still failed open. Any S3 HEAD/GET exception — AccessDenied,
       a timeout, a throttle — became a rejected "unreadable" predecessor, so
       the service published an authoritative reconciler-first record WITHOUT
       facts sitting intact in the bucket and terminalized the row, which
       nothing revisits. Reconstructions also read neither ``attempt_stages``
       nor the CloudWatch stream, and an absent bundle was accepted as
       "nothing to retain" for any attempt.
     - Store faults DEFER (bounded, counted, visible in ``health()``); only a
       record that was read and failed validation is rejected. Reconstructions
       read ``attempt_stages`` and the log stream, and ``reconstructed_from``
       names each only where it actually answered. "Nothing to retain" is now
       gated on the attempt genuinely never having started; a missing bundle
       for an attempt that RAN is counted and surfaced.
   * - 24
     - Health still reported healthy during persistent per-row failure.
       ``poll_once`` catches every per-attempt exception by design, so a
       service that closed NOTHING returned normally each minute and the
       poll-failure counter never left zero.
     - A poll that attempts closures and completes none is unproductive;
       ``CLOSURE_FAILURE_POLL_THRESHOLD`` consecutive unproductive polls flip
       ``healthy``. Five polls at the 60s cadence is the stated bound.
   * - 6, 18
     - Product identity and terminal provenance remained unsafe.
       ``StageContext.products`` is the stage-to-stage channel — downloaded
       inputs and scratch files included — and the upload stages published
       every on-disk entry of it while the terminal record serialized the
       same mapping as local paths and scalars. The reference path keyed its
       uploads ``job_type/unit`` (no run, no attempt), and its helper's
       diagnostic uploads used legacy ``jid`` keys with a swallowed
       ``ClientError``.
     - Published products are a distinct thing: ``publish`` records one entry
       per uploaded object with its immutable URI and the checksum of the
       bytes uploaded, and sequence 0 serializes THAT. One helper
       (``pipeline/stages/publishing.py``) uploads for every job type, keys
       from ``product_prefix()`` and nowhere else, and RAISES on a failed
       upload. The reference stage's docstring already claimed that last
       property; it did not hold.
   * - 5
     - Registration remained a labelled no-op: ``registrar_for`` returned
       None unconditionally and production dispatch ran
       ``register_batch(..., dry_run=True)``. Honest reporting, but a
       successful science attempt stayed a candidate forever.
     - ``pipeline/registration/products.py`` ports the registration bodies
       from the four ``__main__``-only scripts deleted at the W6D fence
       (``e03f22c^``), call sequence and argument order preserved. Re-keyed
       off the attempt's terminal record — fetched by the cited key and
       VERIFIED against the cited checksum — instead of the per-job ``.ini``,
       the product-bucket listing and the stdout log those scripts read.
   * - new
     - Submission manifests were overwriteable: ``S3ManifestStore.put`` used
       an unconditional ``put_object`` despite its own write-once contract.
     - Conditional put (``IfNoneMatch``). An identical body is an ordinary
       replay; different content under a used batch identity raises
       ``ManifestConflict``. The seams suite's ``FakeS3`` now enforces
       create-once too — a fake that accepted every put could not tell a
       store that overwrites from one that refuses, which is why the contract
       went unimplemented.
   * - new
     - Completion waits timed out on a final contradiction:
       ``missing_or_contradictory`` was absent from the terminal set, so a
       correctly-flagged attempt stayed "outstanding" and the VPO waited out
       all six hours over a decision the reconciler had already made.
     - It joins ``_TERMINAL``. It is the design's final outcome for stores
       that disagree, not a state on the way somewhere.
   * - new
     - The real-work submission path was neither connected nor runnable: the
       production VPO exited 64 immediately, reference units never populated
       ``coadd_inputs_uri`` (mandatory in their second stage), the science
       no-reference path required ``reference_image_uri`` two stages BEFORE
       the branch meant to build a missing reference, and post-process
       gathering built ``UnitFacts()`` with nothing in it while its first
       stage requires both products' URIs and six identities.
     - ``gather_reference_units`` aggregates and publishes the coadd inputs
       (the launcher's overlap query, status/vbest exclusions and field
       sanity check preserved). ``download_inputs`` takes the reference URI
       optionally. ``post_process_facts`` derives from real queries.
       ``submit_gathered`` batches a gathered list and submits each batch, and
       the VPO's early exit is gone.

Findings recorded rather than fixed
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**The terminal record carried neither ``job_type`` nor ``ppid``.** Found while
porting the registrar: registration dispatches on the job type, and every
operations-table insert takes a ppid. The legacy bodies read both from the
per-job ``.ini`` they were handed. A registrar reading records alone could not
have told a reference-image attempt from a science one. Fixed through the
provenance path — both are authored into sequence 0 — rather than reconstructed
at registration time from a route matrix consulted later, which would be a
second home for a value nothing keeps equal to the first.

**The manifest vocabulary had no home for four post-process facts.**
``pipeline/stages/post_process.py`` requires ``pid`` and
``difference_image_uri`` through ``context.fact``, and ``UnitFacts`` carried
neither — so the facts the stage demanded could not have been supplied by any
gatherer. Added, with the two version facts the header stamps need.

**``RAPIDDB.get_job_record`` did not exist.** ``gather_post_process_units``
called it behind a ``hasattr`` guard that was therefore ALWAYS false against
the real handle, so every post-process unit fell back to the jid-as-exposure
degenerate case. Added, its column order verified against the deployed table.

Live evidence, and what it could not cover
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``live_fixc_crash_boundary.py`` on **rapid-db**, 6/6: the #14 P0 proven against
the real database, the real lifecycle constraints, a real S3 record store and
the real ``ReconcilerService.poll_once``. The attempt went ``started ->
terminal_after_start`` where it previously stayed ``started`` forever, carrying
``reconciler_materialized=True`` and the checksum the reconciler computed.

**One probe assertion of mine was wrong before the code was.** I expected the
row to end citing sequence 0. It should not: ``mark_terminal_after_start`` runs
immediately after materialization and advances the citation to the reconciler's
own sequence-1 closure record, which is by then the highest-sequence and
therefore authoritative account. The checksum is the evidence that
materialization happened, and it survives that later write because sequence 1
folds the predecessor's facts in verbatim.

``live_fixc_schema_probe.py`` on rapid-db, query-only: 5166 ``l2files`` rows,
and the ``Jobs`` / ``DiffImages`` / ``RefImages`` / ``RefImCatalogs`` /
``DiffImMeta`` column sets the ported bodies write. **``PSFs`` is EMPTY (0
rows)**, so the reference-image live probe this round was gated on was not
possible; those paths are unit-tested only.

**PROPOSED DECISION — the full W8 battery cannot complete from rapid-db.** The
pooler is host-local there (5432 is not reachable off-host by design), so the
battery must run on that host; but ``rapid-db-instance-role`` has no grant on
``roman-rapid-records`` and can assume only ``rapid-migration-runner-role``,
not ``rapid-orchestrator-role``. Pointed at the real records bucket the battery
403s on its first record write — and does so on the UNMODIFIED ``smdc``
baseline too, verified against a staged checkout at the same line. Pointed at
``rapid-build-artifacts`` it reaches case 6c and then needs
``s3:GetObjectTagging``, which that policy does not carry. Either a grant delta
on the identity stack or a host with both the pooler route and records access
would close it. Both are beyond what this round was authorized to change, so
the gap is recorded rather than worked around, and the one case the round-2
review named — the crash boundary — was proven by the narrower probe above.

FixD — round-3 external review
------------------------------

A third external review read the round-2 implementation and raised **nine
findings** against the completion chain: three P0, five P1, one P2. FixD owns
them.

Every citation was re-verified against the code before anything was changed,
and this round that mattered more than either previous one. Two of the nine
summaries were materially wrong about the mechanism while being right that a
defect existed, and one was wrong about half its claim:

* **#8's headline claim is false.** ``dispatch_registration`` *does* use the
  approved connection helper. The real defect is worse than bypassing it —
  there are TWO concurrent connections on contradictory contracts, and the
  transaction boundary falls between them.
* **#5's "FixA staged it" is false.** No bundle-reconstruction code existed
  anywhere. ``retention.py``'s ``canonical_tag_set`` reconstructs *tags*, not
  bundles, which is the likeliest source of the confusion. The reconstruction
  had to be written from scratch.
* **#5's read-failure half does not hold.** Store faults and genuine absence
  *are* distinguished, and the record-store path already defers. Only the
  bundle half is a defect.

Line citations had also drifted in four findings; each is corrected in the row
that uses it.

.. list-table::
   :header-rows: 1
   :widths: 6 34 60

   * - #
     - The finding, as verified
     - What FixD did
   * - 1
     - **P0.** The reconciler advanced ``terminal_record_key`` and
       ``terminal_record_sequence`` to its own sequence-1 closure record and
       left the sequence-0 checksum beside them.
       ``mark_terminal_after_start`` had no checksum parameter, so the triple
       could not be written coherently even in principle. The registrar
       fetches the cited key and hashes exactly those bytes, and the consumer
       selects precisely the reconciler-advanced rows
       (``terminal_record_sequence >= 1``) — so this was the ORDINARY path and
       every normal registration failed on it. (Write site is
       ``service.py:1024-1036``, not 1004-1036: 1004-1023 is the
       materialization branch, a different writer that does carry a checksum.)
     - The citation moves as a TRIPLE. ``mark_terminal_after_start`` carries
       the advancing checksum, on the ``COALESCE(new, existing)`` side of the
       rule — the reconciler's record supersedes the application's, exactly as
       the key already did. The mechanism the review did not name is that the
       ``CLASS_MATERIALIZED`` branch has no ``return``, so **both** writers run
       on the crash boundary and the second stranded the first's checksum.
       The earlier reasoning — that sequence 1 folds the predecessor's facts in
       verbatim, so the checksum survives — is true about FACTS and false about
       BYTES, and a checksum hashes bytes. A NULL checksum also stopped being a
       silent pass in the registrar: the docstring already called it a
       not-ready row and now the code agrees.
   * - 2
     - **P0.** ``context.provenance`` started empty and the entrypoint seeded
       exactly three keys. ``UnitFacts`` were never copied in, and ``sca``
       lives on the UNIT, so it was not even reachable from there. Everything
       the registrar asks about WHICH piece of sky an attempt was — ``field``,
       ``fid``, ``rid``, ``sca``, ``sky_position`` — was absent from every
       record production could author. **No production record could ever have
       satisfied the registrar.** (Seed is ``job.py:569-570``, not 557.)
     - Unit facts reach the record through the provenance path, following the
       ``job_type``/``ppid`` precedent: derived once, at record-authoring time,
       in a named place (``pipeline/registration/facts.py``). Two amendments
       beyond the report: **hp6/hp9 existed NOWHERE** in the pipeline — not in
       ``UnitFacts``, not in a stage, not in gathering — so they are DERIVED,
       transcribed from ``loadPSFCatIntoDBSourcesTable.py``, the file that
       actually populated the columns they register into. And **``scalefacref``
       is the RECIPROCAL** of the recorded ``gainmatch_scalefac``; traced
       through the legacy registrar (which does no arithmetic) to the science
       monolith that wrote ``1./scalefac``. Both are plausible positive floats,
       so getting it backwards would have silently disagreed with every row
       already in the column. Metric names are fixed at the RECORDING sites,
       not translated at read time — the live schema settled it, since
       ``diffimmeta``'s real columns *are* the registrar's vocabulary.
       Registrar tests are rebuilt through ``build_terminal_record``.
   * - 3
     - **P0.** The production VPO was unrunnable past reference submission.
       All three waits omitted ``run_id`` and so returned ``{}`` immediately;
       all three registration calls omitted the callback and were therefore
       explicit dry runs; science and post-process submission were
       ``exit(64)`` stubs; and an unconditional ``exit(65)`` sat on the
       science-registration success path.
     - ``wait_for_submitted`` waits on each submission's OWN run id. Passing
       the parent id would only have half-fixed it: ``submit_gathered``
       re-scopes to ``<run_id>-<n>`` from two batches on, so one batch would
       have worked and two would not. ``production_registrar`` builds the real
       callback from a records bucket and an S3 client — all ``registrar``
       needs, since it takes its handle as a callable. The stubs are gone:
       ``gather_science_units``' own representative/rest split maps exactly
       onto the stage-one/stage-two loop that was already there. A dry run must
       now be asked for by name (``RAPID_VPO_DRY_RUN``); **production defaults
       to production**. The vestigial ``DRYRUN`` env var, read by nothing since
       the W6 fence, is removed rather than left looking operative.
   * - 4
     - **P1.** Reference gathering passed the representative's own ``rid`` and
       ``mjdobs`` to the overlap query, turning on two controls the deleted
       launcher deliberately turned OFF. Worse than the report said: the
       representative is ``rows[0]``, the EARLIEST frame, and the window is
       ``[0, its_mjd)`` — so at exactly ``min_images_to_coadd`` the query
       returns **0** rows, not N-1. Separately, ``exit_code=67`` is a silent
       ``return None``, so a database outage was reported as "this field is not
       ready". (Return is ``rapid_db.py:1323-1326``, not 1308-1328.)
     - Wrapped at the gathering boundary rather than editing the legacy query,
       so its other callers keep a byte-identical query. The launcher's two
       sentinels are restored and named. Worth recording why ``rid='null'``
       works at all: it selects the ``a.rid is not 'null'`` branch, which is a
       PostgreSQL **type predicate**, not a comparison — it asks whether an
       integer column's value is distinguishable from a string literal, which
       is always true, so it excludes nothing. An accident the launcher relied
       on, left in ``rapid_db`` and explained at the one call site that depends
       on it. ``NotReadyYet`` splits ``GatheringError``'s two meanings so the
       catch narrows to the genuinely ordinary case.
   * - 5
     - **P1.** ``_stamp_bundle`` noticed a ran-but-bundle-less attempt,
       incremented a counter, logged, and returned — and the caller went
       straight to the terminal transition. Terminal rows are outside the open
       set, so nothing ever revisited them. The design rule has no exception in
       it: the bundle exists before the attempt is closed, whichever way it
       died.
     - ``pipeline/reconciler/reconstruction.py``, written from scratch — the
       claim that FixA had staged it was false. Reads from the HEAD of the
       CloudWatch stream, not the tail closure records use: a record wants how
       an attempt ended, a bundle wants how it ran. Uploaded through
       ``termination.upload_bundle``, so a real bundle landing between the
       check and now is KEPT and the salvage discarded — not hypothetical, a
       container slow to flush can do exactly that. **Deferring could not
       terminate**: deferral here is unbounded and the stream expires at 14
       days, so an attempt deferred past that horizon could never be closed by
       anyone. The two failures are therefore split — an unreadable stream is
       permanent and the bundle is written anyway with the gap recorded inside
       it; a store that refuses the write is a condition a later poll may
       resolve, and only that defers.
   * - 6
     - **P1.** Rounds 1 and 2 built the entire health mechanism — two
       counters, two thresholds, ``healthy``, ``health()`` — and then
       ``run_forever`` asked only whether ``poll_once`` threw. The second
       threshold governed nothing.
     - The check is on the SUCCESS path, because that is the path the
       condition occurs on, and it asks ``healthy`` rather than re-deriving one
       of its terms. ``_resolve_discovered`` was the one per-row loop
       swallowing its failures instead of reporting them into
       ``summary["errors"]`` — so a resolver failing every attempt contributed
       nothing to the health it should have degraded. Beyond the report:
       ``ReconcilerUnhealthy`` subclasses ``RuntimeError`` and was caught by
       ``main.py``'s start-failure handler, telling the journal "the reconciler
       could not start" about a process that had been polling for hours. Own
       handler, own exit code (71), and the two docstrings that still promised
       the loop never exits on error now describe what it does.
   * - 7
     - **P1.** Post-process called the legacy boolean-returning uploader and
       discarded the result — which also silently skips a missing file — and
       never called ``publish_products`` or ``context.publish``, so a failed
       upload closed ``success``/``published`` with an empty product set. Its
       FITS ``S3OBJPRF`` named ``job_type/unit`` while the bytes went to the
       run/attempt prefix. And ``success``+``published`` being the SOLE
       registering pair meant every successful registration pass became an
       unsupported candidate on the next one.
     - Post-process publishes through ``publish_products``/``context.publish``
       with raising uploads and a consistent ``S3OBJPRF``. The job-type gate
       had to be a record-body filter rather than a SQL predicate, because the
       ``attempts`` table has no ``job_type`` column at all — which also makes
       ``products.py``'s ``row.get("job_type")`` fallback dead code, now
       removed. Dispositions are derived from what was actually published, so
       registration jobs close with one that never becomes a candidate.
   * - 8
     - **P1.** The report says the callback bypasses the approved connection
       helper. **It does not.** The real defect: ``registrar_for`` passes
       ``rapid_db.RAPIDDB`` as a lazy factory, and ``RAPIDDB.__init__`` always
       opens its OWN connection with autocommit-per-call, while the watermark
       commits on the approved helper's. Two concurrent connections on
       contradictory contracts; they cannot be one transaction by construction,
       which makes ``consumer.py``'s own comment about leaving the attempt a
       candidate false.
     - See the schema half below and the code half in the commit. The version-
       incrementing procs gained attempt-idempotence keyed on
       ``(attempt_id, registered_record_sequence)`` — the pair, not the attempt
       alone, because idempotence keyed on the attempt would also block
       supersession, which 017 made the watermark a sequence precisely to
       allow.
   * - 9
     - **P2.** Product publication used unconditional ``upload_file`` and
       coadd-input publication unconditional ``put_object``, against a storage
       design whose mutability table calls both write-once and says
       immutability is enforced, not promised.
     - Conditional creates on both, matching the two templates already in the
       tree. ``upload_file`` **cannot** carry a condition: boto3 validates
       ``ExtraArgs`` against s3transfer's ``ALLOWED_UPLOAD_ARGS``, which has
       ``ChecksumSHA256`` but neither ``IfNoneMatch`` nor ``IfMatch`` —
       confirmed in-image against boto3 1.43.46 / s3transfer 0.19.2, not from
       recall — so the file path calls ``put_object`` itself, streaming the
       handle and hashing in chunks so a large mosaic is never resident. That
       trades multipart parallelism and the 5 GiB ceiling for correctness,
       which is the right trade at RAPID's product sizes and is noted in the
       code for whoever first exceeds them. ``coadd_inputs_checksum`` joins
       ``UnitFacts``, because a URI names a key and this fix is about a key
       whose bytes could change.

Migration 018, and what the rehearsal caught
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``018-registration-attempt-idempotence.sql``, applied to rapid-db **2026-08-06
20:05:57 UTC, first attempt**, after three rehearsal cycles on a throwaway.

The rehearsal earned its place on cycle 2, on a defect invisible to inspection:
**``CREATE OR REPLACE`` does not replace a function whose argument list
changed — it creates an OVERLOAD.** The two defaulted parameters left 008's
9-argument ``addrefimage`` in place beside the new 11-argument one, so every
legacy 9-argument call failed with "function addrefimage(...) is not unique".
The mechanism chosen specifically to leave existing callers untouched was the
thing that broke them, and nothing about it shows on reading: the stream
applies clean and every new-signature call works. Fixed by dropping the old
signatures in the same transaction that recreates them.

The two function bodies are 008's **verbatim** plus the guard and the two
INSERT columns — diffed mechanically against 008 rather than eyeballed, because
a tidied reimplementation is how behaviour changes that nobody meant to change.
Even 008's misleading ``*** Error in addProcImage: RawImages record`` message
is left exactly as it is.

Findings recorded rather than fixed
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**healpy was never a declared dependency.** It has always been installed in the
Dockerfiles and never listed in ``requirements.txt``, so "registration works"
depended on a fact no dependency list stated. With ``hp6``/``hp9`` NOT NULL on
both product tables, an environment without healpy cannot register at all —
and it would fail per-attempt, deep inside a pass, rather than at build time.
Added to ``requirements.txt``; the deployed image carries 1.20.0, verified
rather than assumed.

**``rapid_db.py`` calls ``exit()`` from library code.** ``get_overlapping_l2files``
exits 64 when ``ENDREFIMMJDOBS`` is set without ``STARTREFIMMJDOBS``
(``rapid_db.py:1278``), and ``virtualPipelineOperator`` reads ``RAPID_SW`` /
``RAPID_WORK`` / ``STARTDATETIME`` / ``ENDDATETIME`` at MODULE scope with the
same treatment — so importing the operator terminates the interpreter when any
is unset. Both are the "swallows errors into exit codes from library code"
pattern the payload proposal's as-is finding 7 names. Neither is fixed here:
moving those checks is a change to the operator's startup contract, which the
operations design owns. Recorded so the next round does not rediscover it.

Live evidence
~~~~~~~~~~~~~

``live_fixc_crash_boundary.py`` on **rapid-db, 6/6**: the #1 P0 proven against
the real database. Case 4 now asserts COHERENCE the way a consumer checks it —
fetch the cited key, hash those bytes, compare to the cited checksum — and it
passes, where before the fix the row cited a sequence-1 key beside a
sequence-0 checksum and the registrar refused every such attempt. The probe's
own assertion had to move with the code: it previously pinned the incoherent
pair as intended behaviour.

``live_fixd_mini_chain.py`` on **rapid-db, 9/9**: one unit from a terminal
record to a registered product. The record is authored through the real
serializer, a real ``ReconcilerService.poll_once`` classifies it to
``terminal_after_start`` at sequence 1, the registrar writes a real
``RefImages`` row through the real stored procedure, the watermark advances,
and a replay writes NO second version — migration 018's guard proven against
the live ``addRefImage`` rather than a fake.

**The probe found a defect on its first complete run that nothing else could
have.** ``refimcatalogs.cattype`` is a ``smallint`` and
``registerRefImCatalog`` casts its third argument to one, but the ported
registrar passed the strings ``"sextractor"`` and ``"photutils"``. PostgreSQL
refused outright, so *every* reference-image registration failed at its first
catalogue. The unit suite could not see it: its fake database accepts whatever
it is handed, so a type the real column cannot hold looks exactly like one it
can.

.. note::

   **RATIFIED 2026-08-06 (Ben, disposition batch): the ``cattype``
   vocabulary is ``1 = SExtractor, 2 = PhotUtils``**, verified against the
   deleted monolith in git history rather than left as an ordering this repo
   chose.

   Recorded as proposed when written, because the legacy body read these from
   ``product_config['REF_IMAGE'][...cattype]`` — a per-job product config the
   W6 cutover deleted, which never lived in this repo — and neither repo
   carries a lookup table, a CHECK constraint, or seeded values for the
   column. The two constants live in ``pipeline/registration/products.py``.
   They remain the only place to change if the archive is ever found to
   number these differently, and ``refimcatalogspk (rfid, ppid, cattype)``
   means a wrong number collides rather than silently duplicating.

What the live evidence could not cover
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**PSFs is still EMPTY on rapid-db** (0 rows, and RefImages/DiffImages were both
0 before this round's probe). No genuine reference-image attempt is possible
until the survey produces PSF data, so the mini-chain registers a
battery-shaped SYNTHETIC product. What is synthetic is the science content —
the FITS bytes are a placeholder — and not any part of the chain under test:
the attempt row, the record, the reconciliation, the procedures and the S3
objects are all real. The science-fidelity question of whether a *correct*
reference image is built is a different one, and this probe does not touch it.

**The grant gap FixC recorded is partly closed.** ``rapid-db-instance-role``
still has no grant on ``roman-rapid-records``, so both live probes point at
``rapid-build-artifacts`` as their records store. FixD added
``s3:GetObjectTagging``/``s3:PutObjectTagging`` on THAT bucket, which is what
the crash-boundary probe needed — it had been failing on AccessDenied from
``GetObjectTagging``, invisible until round 3 made bundle reconstruction real
and gave the tagging path something to tag. The records-bucket grant itself is
still outstanding, and closing it is what would let these probes run against
the production bucket.

FixE — round-4 external review
------------------------------

A fourth external review read the round-3 implementation and raised **five
findings**: one P0, four P1. FixE owns them. The review's own resolution
matrix records six of the nine round-3 findings as holding outright; the three
it reopened are reopened for new reasons rather than because the earlier fix
was wrong — #3 and #8 by a defect in the VPO path that the round-3 work did
not reach, #4 by a branch of SQL nothing had ever executed, and #9 by the one
conflict case that size could decide and bytes could not.

Every citation was re-verified against the code before anything was changed.
This round, unlike the last two, **all five summaries were accurate about the
mechanism, and every line citation resolved**. What re-verification added was
one further defect that no finding named, described under the table.

.. list-table::
   :header-rows: 1
   :widths: 6 34 60

   * - #
     - The finding, as verified
     - What FixE did
   * - 1
     - **P0.** ``submission_env(job_type)`` takes the job type and ignores
       it, returning one singular ``RAPID_JOB_QUEUE``/``RAPID_JOB_DEFINITION``
       pair (``virtualPipelineOperator.py:277``). All three phases call it —
       reference-image, science, post-process — and the route matrix puts
       reference on the **bulk** class and the other two on **prompt**
       (``routes.py:128``). Whichever pair is configured, at least one phase
       reaches a queue whose job definition names the other class, and
       ``validate_route`` rejects it at the entrypoint before any processing.
     - The queue and job definition are resolved PER PHASE from the route
       matrix, through the parameter tree. ``Route`` already named the tree
       KEYS and deliberately not the names, so this reads them with
       ``fetch_parameters`` — the same read the entrypoint validates against,
       so a submission cannot disagree with the check that will meet it. The
       four keys are live and were verified rather than assumed
       (``/rapid/pipeline/batch/{queue,job-definition}-{bulk,prompt,science}``,
       2026-08-06). The image digest, definition revision and release identity
       stay in the environment, where the CI pipeline puts them.

       Tested at the **submit-call boundary**: what ``submission_env`` returns
       is what the three call sites pass straight into ``submit_gathered`` as
       ``queue=``/``job_definition=``, so which queue a phase would reach is
       decidable without submitting anything. One test per phase, plus the
       property that makes them a routing test rather than three constants —
       reference and science must DIFFER — plus a cross-check that each
       resolved queue is the one ``validate_route`` re-derives.
   * - 2
     - **P1.** ``production_registrar()`` returned
       ``registrar(rapid_db.RAPIDDB, store)`` — the class, as a factory — so
       the registrar opened its own autocommitting connection
       (``virtualPipelineOperator.py:434``) while
       ``run_registration(regconn, register=...)`` advanced the watermark on
       another. Two connections cannot be one transaction: product rows became
       durable before the watermark was attempted, and a crash between them
       left rows written with the attempt still a candidate, so the next pass
       registered the same products again. This is round-3 finding #8, fixed
       in the registration job (``job.py:331`` uses ``RAPIDDB.borrowing``) and
       reintroduced on the VPO path the mini-chain never exercised.
     - ``production_registrar`` is now a FACTORY taking the pass's connection,
       and ``registration_callback(factory, conn)`` is the named seam the three
       call sites use. A factory rather than one callback because each phase
       opens its own registration connection — one callback built once could
       borrow only one of them, which would leave the split in place for the
       other two. The expensive parts (bucket, S3 client, store) still happen
       once; only the per-connection binding is deferred. The shape is
       ``entrypoints.job.registrar_for(context, conn)``, followed deliberately
       rather than arrived at again.

       The test **fails on two connections**: it inspects the database handle
       the registrar would build and asserts it borrowed the connection the
       pass holds. A second test drives three connections through the factory
       and asserts three distinct bindings, so a regression to one shared
       callback shows up as the same connection borrowed three times.
   * - 3
     - **P1.** ``gathering`` passed the string ``'null'`` as the "no
       exclusion" sentinel (``gathering.py:343``), which selected
       ``a.rid is not %s`` in the database method (``rapid_db.py:1396``). After
       the pre-delta parameterization sweep, that string was BOUND through the
       placeholder rather than substituted into the text, so PostgreSQL
       received ``a.rid IS NOT 'null'`` — invalid. The overlap query failed
       with ``exit_code`` 67 and the reference stage gathered nothing.
       Historically it parsed as ``IS NOT null``, a type predicate true for
       every row of an integer column, which is why it "worked": it excluded
       nothing by accident.
     - The string sentinel is gone. ``REFERENCE_OVERLAP_NO_EXCLUSION`` is
       ``None``, and the method emits **no exclusion clause at all** for it —
       which is what "exclude nothing" means, and is not something the binding
       of a value can break. A single ``exclude_rid`` decides both the SQL and
       whether a parameter is appended, so the text and the tuple cannot
       disagree about how many placeholders there are.

       Tested on both branches, twice over. The unit test asserts the emitted
       shape *and* that the placeholder count matches the parameter count —
       the bug in one assertion. The live probe
       (``submission/test/live_fixe_overlap_sql.py``) **EXECUTES** both
       branches against the real PostgreSQL on rapid-db, because the defect is
       entirely in what the server makes of the text and a mocked cursor
       accepts any string, including one that cannot parse. That is exactly
       how a query that could not run survived three green rounds. The probe
       creates its own schema and fixture and drops them; it touches no
       operational table.
   * - 4
     - **P1.** After a conditional create fails, an existing object carrying
       no ``ChecksumSHA256`` was accepted as identical when its LENGTH matched
       the local file (``publishing.py:133``). ``publish_products`` then
       recorded the LOCAL digest — so a same-length, different-content legacy
       object stayed in S3 while the terminal record cited a checksum for
       bytes that were never stored. The registrar fetches the cited key and
       hashes exactly those bytes, so it would refuse every such product. The
       branch contradicted the "only by the bytes" invariant stated two lines
       above it, and the test blessed the fallback explicitly.
     - Where S3 has no stored digest to compare, the object's BYTES are
       fetched and hashed. Streamed in chunks, so a multi-hundred-megabyte
       mosaic is never resident — the same reason the local digest is chunked.
       One GET, on a path reached only when a key is already occupied, which
       is rare and is precisely when being right is worth more than being
       quick. A digest that cannot be COMPUTED is not a match: the failure
       propagates and the attempt is not closed as having published something
       nothing verified.

       The blessing test is replaced by the case it got wrong — twelve bytes
       against twelve different bytes, asserted equal in length and required
       to raise — plus the identical-bytes replay, which now also asserts the
       bytes were actually READ rather than inferred from a HEAD.
   * - 5
     - **P1.** The adopted design requires reconstruction for "abrupt loss, or
       never started" and puts the bundle before every close
       (``observability.md:206``). ``_stamp_bundle`` reconstructed only when
       ``_attempt_ran(...)`` was true (``service.py:923``), and the
       never-resolved path published its closure and transitioned the row
       without invoking bundle handling at all (``service.py:1149``). Round 3
       closed this for attempts that RAN; the unconditional rule was still
       unmet, and the one class of attempt the design names explicitly for
       reconstruction was the class that closed with nothing.
     - The rule now has no exception in it. A never-started attempt gets the
       minimal reconstructed bundle, marked reconstructed and carrying the
       submission facts, the scheduler state (or the absence of one) and the
       reason. That is also the more useful truth: what is retained for such
       an attempt is not its output but the account of its NON-execution,
       which is otherwise nowhere — terminal rows are outside the open set, so
       nothing ever comes back to explain a provisioning failure. It is not
       counted as a ``missing_bundles`` alarm, which means something worse: an
       attempt that ran and lost its evidence.

       In the never-resolved path the bundle is stamped **before** the closure
       record, so the ordering matches the rule rather than satisfying it by
       coincidence. A bundle that cannot be written defers with nothing
       published; the reverse order would leave a published closure citing a
       bundle that never appeared, which no later poll could repair.

What re-verification added
~~~~~~~~~~~~~~~~~~~~~~~~~~

**``submission_env`` could not build its binding at all, and no finding named
it.** It constructed ``ExecutionBinding(..., manifest_checksum=None)``, and
``ExecutionBinding.__post_init__`` rejects every empty field by design — so
the call raised ``ValueError`` unconditionally and the production operator
could not submit anything, on any phase. It surfaced only because the routing
tests construct the binding for real instead of stubbing it, which is the
argument for testing at the seam rather than around it.

The validation is right and stays where it is: an attempt row must always name
the manifest it was submitted under. But a manifest checksum is a property of
a BATCH, and the operator resolves its binding once per phase, before any
batch exists. ``SubmissionBinding`` therefore carries the four facts the
operator does know; ``submit_gathered`` already rebuilt the real
``ExecutionBinding`` with the checksum once the manifest was published, so
nothing downstream changed.

Live evidence
~~~~~~~~~~~~~

``live_fixe_overlap_sql.py`` on **rapid-db, 6/6**: both branches of the
exclusion clause executed against the real PostgreSQL, against real
``L2FileMeta``/``L2Files`` rows. At (268.0267, -29.1634) fid 8 — a position
taken from the table rather than chosen, so the probe follows the data
wherever it is run — the open branch returned **47 rows and excluded
nothing**, and the exclusion branch returned **46** with the named rid absent.
Both reported ``exit_code`` 0, where before the fix the open branch returned
None and set 67 because the server refused to parse it.

The emitted SQL is in the run log and is the finding in one line: the open
branch ends ``and a.mjdobs < %s order by dist`` with 46 parameters and no
exclusion predicate at all; the exclusion branch adds ``and a.rid != %s`` and
a 47th.

**The probe's first run found something no finding named:** ``rapid_pipeline``
has no CREATE privilege on the database. The probe originally built its own
fixture schema and was refused with ``InsufficientPrivilege``. That is the
role behaving correctly — it is a least-privilege service account — and the
fixture was never the point, so the probe was rewritten READ-ONLY against the
deployed table. That is the better witness anyway: it is the actual schema the
query names, with the actual q3c extension, so drift shows up rather than
being faithfully reproduced in a stand-in.

No Batch children were submitted. Finding #1 is a routing question, and
routing is decidable at the submit-call boundary from what
``submission_env`` resolves — submitting jobs would have proven the same fact
more slowly and less precisely.


The awaicgen mosaic geometry — extraction, with citations
---------------------------------------------------------

The four values ``awaicgen_mosaic_size_x``, ``awaicgen_mosaic_size_y``,
``awaicgen_RA_center`` and ``awaicgen_Dec_center`` blocked the whole W9 ramp:
the master ``.ini`` leaves all four as the literal ``to_be_filled_by_script``
because the deleted launchers computed them and substituted them before
dispatch, so the W4B migration to release content had nothing to migrate —
the keys' VALUES never existed in a file. Nothing computed them afterwards,
and ``build_awaicgen_command_line_args`` raised ``KeyError`` on the first of
them after the PSF and all 48 coadd inputs had been downloaded and
reformatted.

This was ported as **extraction, not invention**. The authority is the
deleted reference-image launcher; the science launcher beside it carries the
identical lines and the two agree exactly, which is why one function serves
both callers.

Citations
~~~~~~~~~

All line numbers are in
``e03f22c^:pipeline/awsBatchSubmitJobs_launchSingleReferenceImagePipeline.py``
(the science launcher's equivalents, in
``…_launchSingleSciencePipeline.py``, are given second).

.. list-table::
   :header-rows: 1
   :widths: 30 20 50

   * - What
     - Lines
     - The code
   * - Inputs to the extent
     - 164-168 / 150-154
     - ``naxis1_refimage``, ``naxis2_refimage``, ``cdelt1_refimage``,
       ``crota2_refimage`` read from ``[REF_IMAGE]``
   * - The extent
     - 226-231 / 213-217
     - ``pixel_scale = math.fabs(cdelt1_refimage)`` then
       ``mosaic_size_x = pixel_scale * naxis1_refimage`` and
       ``mosaic_size_y = pixel_scale * naxis2_refimage``
   * - The rotation
     - 232
     - ``awaicgen_mosaic_rotation = str(crota2_refimage)``
   * - The tile centre
     - 352-355 / (same block)
     - ``rtid = field``;
       ``roman_tessellation_db.get_center_sky_position(rtid)``;
       ``ra0_field``, ``dec0_field``
   * - The mosaic centre
     - 370-371, 381-382 / 508-509
     - ``ra0_refimage = ra0_field`` and
       ``awaicgen_RA_center = str(ra0_refimage)``, likewise Dec

Two details are ported rather than re-derived, and both would be got wrong by
anyone reasoning from first principles:

1. ``pixel_scale`` is ``fabs(cdelt1)`` and multiplies **both** axes (226-228).
   ``cdelt2_refimage`` is read at line 167 and is **not** used for the
   extent. The reference image is square with equal magnitudes so the two
   agree numerically today, but the ported code follows the launcher.
2. The centre carries **no offset**: ``ra0_refimage = ra0_field`` (370-371),
   under the comment "the reference image is centered on the sky tile with
   zero rotation" (368). The ``crpix``/corner arithmetic immediately below
   feeds the reference image's own WCS, not awaicgen's ``-R``/``-D`` — and it
   is the obvious thing to mistake for a half-mosaic offset.

Where the values landed, and why
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The launcher's own split decides the placement, and it matches the placement
criterion exactly:

* **Extent → release content.** Computed once at module scope, under the
  comment "quantities that do not vary with sky location" (224). It is a
  function of ``[ref_image]`` alone, identical for every field in a
  submission, so it stays a config read.
* **Centre → per-invocation manifest fact.** Computed inside the per-field
  submit loop, from the tessellation. It varies per field, so it is a fact.
  ``UnitFacts.tile_position`` already declared it, with exactly the
  ``ra0``/``dec0`` shape needed, and was never populated by anything;
  ``submission/gathering.py`` now fills it from the closed form, which opens
  no connection and issues no query, so W7's retirement of the per-unit
  R-tree lookup is not reintroduced.

Resolution happens in the build branch only. ``_download_reference_image``
reads the ``[awaicgen]`` section for its three output filenames alone, and
requiring ``tile_position`` there would fail a unit that legitimately reuses
an existing reference image over a fact its own work never touches.

Regression evidence
~~~~~~~~~~~~~~~~~~~

``pipeline/test/test_mosaic_geometry.py``, 30 tests, all passing. The
expected values are recomputed from the launcher's arithmetic **written out
longhand** — not by calling the module under test, and not as hardcoded
constants, so a change that alters the derivation fails even where it happens
to preserve today's 7000×7000 square numbers.

Field coverage is seven: three g0001 tiles (511, 3145729, 4096), both pole
tiles (1 and 6291458) and both pole-adjacent tiles (2 and 6291457). The poles
are the case ``center_of`` treats specially — it returns early rather than
going through the ring arithmetic — so a port that assumed the general branch
would pass on mid-latitude fields alone. Specific tests pin the two
easy-to-invert details above: ``test_both_axes_take_cdelt1_as_the_launcher_did``
uses an anisotropic pixel to prove ``cdelt2`` is not consulted, and
``test_the_centre_is_unoffset_from_the_tile`` pins the absence of the offset.

**Live**: ``build_reference_image`` succeeded on 36 of 36 real g0001 children
across two independent submissions, mean 145.2 s. It had never once completed
before this port.

A fifth key, found by reading rather than by running
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``awaicgen_num_threads`` is read by ``build_awaicgen_command_line_args``
(line 629) and was absent from ``[awaicgen]`` — the same W4B drop, and the
next ``KeyError`` the ramp would have hit once the four geometry keys were
supplied. It was found by walking that function's full key list against
release content instead of waiting for a fifth live attempt, and restored at
the master ``.ini``'s value of 2.

That walk is now a test. The pre-existing completeness test checked only the
keys subscripted in the STAGE-BODY modules, so everything read one call
deeper — inside the command-line builders, which is where the real
requirement lives — was invisible to it. That blind spot is why the ramp met
these drops one ``KeyError`` per live attempt. Both builders are now walked
directly, and the same walk applied to SExtractor immediately found eleven
more missing keys (``w9_ramp.rst``, defect 6).
