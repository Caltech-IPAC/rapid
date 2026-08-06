Audit: swallowed exit codes from external commands
##################################################

Scope and status
****************

This is an **audit, not a change proposal that has been carried out**. It was
written alongside the g0001 registration fix
(:doc:`gbtds_sim_g0001_registration`), which reached a job that reported
SUCCEEDED with exit 0 while registering zero rows. That job's root cause was
not unique to the registration script, so the same pattern was mapped across
the repo.

``modules/utils/rapid_pipeline_subs.execute_command`` is **deliberately left
unchanged.** Its permissive semantics are load-bearing for science call sites
that may depend on tolerated nonzero exits, and tightening them globally is an
owner decision, not a side effect of a registration fix. The registration path
was fixed by routing around the helper — it now uses boto3 and ``os.remove``
and calls no external commands at all — rather than by changing the helper.

What the helper does
********************

``modules/utils/rapid_pipeline_subs.py``, ``execute_command``:

* Runs the command with ``subprocess.run``, argv-list form, ``shell=False``.
  This part is correct.
* Merges stderr into stdout, prints the merged output, and **returns the
  return code**. It never raises on a nonzero exit, never retries, and never
  exits the process. The exit code is purely advisory.
* Has no ``check`` or ``no_check`` parameter, so a caller cannot opt into
  strictness. Every call is implicitly tolerate-everything.
* A **missing binary** is not a return code at all: with ``shell=False``,
  ``subprocess.run`` raises ``FileNotFoundError`` out of the helper. Callers
  that only inspect a return value never see it. This is the g0001 signature,
  ``[Errno 2] No such file or directory: 'aws'``.

The companion helpers ``execute_command_and_return_stdout`` and
``execute_command_in_shell`` have the same no-raise semantics.

Call sites
**********

Roughly sixty call sites across the pipeline. Only about a dozen inspect the
return value, and those that do compare against ``>= 64`` — so exit codes 1-63,
which covers the normal Unix failure range including the AWS CLI and
SExtractor, pass as success even at the "checking" call sites.

Highest risk: a silent failure yields wrong-but-successful output
================================================================

These are the cases where a failed command produces an empty or stale artifact
that the next stage consumes as if it were real data.

===================================================================== ============== ==============================================================
Call site                                                             Command        Consequence of silent failure
===================================================================== ============== ==============================================================
``modules/utils/rapid_pipeline_subs.py`` (S3 listing helper)          ``aws s3 ls``  A failed CLI returns empty stdout, parsed as "bucket is
                                                                                     empty". Downstream stages process zero inputs and report
                                                                                     success. Same PATH exposure as g0001, higher leverage.
``modules/utils/rapid_pipeline_subs.py`` (resampling)                 ``swarp``      Reference image not resampled; differencing proceeds against
                                                                                     a stale or absent reference.
``modules/utils/rapid_pipeline_subs.py`` (done-file)                  ``touch``      The job's own success sentinel, later uploaded to S3.
                                                                                     Sentinel integrity should never be unchecked.
``modules/utils/rapid_pipeline_subs.py``, ``pipeline/*Subs.py``,      ``sex``        A failed run writes no catalog or a truncated one; the
``pipeline/awsBatchSubmitJobs_runSingleSciencePipeline.py``,                         parser then yields zero sources, which reaches the database
``scripts/generate_sexcat.py`` (~12 sites)                                           as a legitimately-empty detection list.
``pipeline/referenceImageSubs.py``                                    ``awaicgen``   Coadded reference image never built; the reference-image
                                                                                     pipeline produces nothing and reports success.
``pipeline/awsBatchSubmitJobs_runSingleSciencePipeline.py``           ``bkgest``     Background not subtracted; the difference image is
                                                                                     numerically wrong rather than obviously broken.
``pipeline/awsBatchSubmitJobs_runSingleSciencePipeline.py``           ZOGY subproc.  The core differencing step, unchecked.
``database/sims/db_register_rimtimsim_files.py``                      ``aws s3 cp``  The same shape as the g0001 defect, unfixed. See below.
``sims/src/socsims/*``, ``sims/src/rimtimsim/*``                      ``aws``,       Conversion and fake-source injection: a failed gunzip
                                                                      ``gzip``,      leaves a still-compressed or missing file; a failed gzip
                                                                      ``gunzip``     means the subsequent upload pushes a partial object.
===================================================================== ============== ==============================================================

Medium risk: failure surfaces later
===================================

* ``pipeline/virtualPipelineOperator.py`` — ten sub-stage invocations. The
  return value *is* checked, but only for ``>= 64``, so a sub-stage exiting 1
  (an uncaught Python traceback exits 1) passes as success. These are top-level
  orchestration, so a swallowed 1-63 propagates broadly.
* ``pipeline/launchSciencePipelinesForDateTimeRangeWithRefImageWindow.py``,
  ``pipeline/launchBunchOfReferenceImagePipelines.py`` — same ``>= 64``
  threshold.
* The ``awsBatchSubmitJobs_launch*`` scripts — return value unchecked entirely;
  a failure means jobs are never submitted, which shows up as an absence of
  downstream work.
* ``pipeline/generateSourceHATSCatalog.py``,
  ``pipeline/generateLightCurveHATSCatalog.py`` — catalog copy, return value
  not even assigned.

Low risk
========

``rm -f`` cleanup and ``ls -ltr`` diagnostics, in roughly a dozen places. A
nonzero exit from these is genuinely harmless.

**With one caveat:** under the g0001 condition the binary is *missing*, so
these raise ``FileNotFoundError`` rather than returning nonzero — and an
exception inside a ``ProcessPoolExecutor`` worker aborts that worker's whole
loop, taking the remaining files with it. A best-effort cleanup call can
therefore take down a batch. The registration fix avoids this by using
``os.remove`` inside its own ``try``.

Related: duplicated local ``execute_command`` copies
****************************************************

Six files under ``sims/src/`` and ``database/sims/`` define their own
``execute_command`` with different semantics from the shared one: it retries
five times with 30-second sleeps and calls ``exit(1)`` on persistent failure,
with a ``no_check`` flag to suppress that.

Those copies call ``subprocess.Popen(cmd, shell=True, ...)`` with ``cmd`` as a
**list**. On POSIX that runs ``/bin/sh -c <cmd[0]>`` and passes the remaining
elements as positional parameters to that shell — every argument after the
first is silently discarded. Wherever a list is passed, the wrong command runs.
This is a latent wrong-command bug, not merely an unchecked one, and each such
call site needs checking for whether its ``cmd`` is a string or a list.

The copy in ``database/sims/db_register_socsim_files.py`` was dead code and was
deleted as part of the registration fix. The copy in
``database/sims/db_register_rimtimsim_files.py`` is also dead code — that file
likewise calls ``util.execute_command`` — and remains in place.

PATH dependencies
*****************

Every external binary below is invoked by bare name, so each resolves against
the ``PATH`` that ``subprocess`` inherits — which is not a login shell's
``PATH``, and does not include anything added only by a conda activation
script.

* AWS: ``aws``
* Coreutils and archive: ``rm``, ``gzip``, ``gunzip``, ``touch``, ``ls``
* Astronomy: ``sex``, ``swarp``, ``awaicgen``, ``computeOverlapArea``
* Python interpreters: inconsistently ``/usr/bin/python3.11`` (absolute) in
  some places and bare ``python3.11``/``python3`` in others

``bkgest`` is the one binary referenced by absolute path, built from the
``RAPID_SW`` environment variable rather than found on ``PATH``. That is the
better pattern for the rest.

Two notes worth confirming against the image:

* No ``ENV PATH`` directive exists in either Dockerfile. The only PATH
  manipulation appends to ``/root/.bash_profile``, which a Batch container
  entrypoint never sources and a non-shell ``subprocess`` call never sees.
  This is the mechanism behind the g0001 failure: the AWS CLI is installed in
  the image but invisible to the subprocess.
* SExtractor is invoked as ``sex``. Debian and SExtractor++ packages install
  the binary as ``source-extractor`` or ``sextractor``. Whether the image
  provides the ``sex`` name specifically is worth verifying.

Left for the owner
******************

None of the following were changed:

#. Whether ``execute_command`` should gain a ``check``-style parameter, or
   convert ``FileNotFoundError`` into a distinguishable return code such as
   127, so callers can opt into strictness without changing existing behavior.
#. Whether the ``>= 64`` threshold in ``virtualPipelineOperator.py`` and the
   launcher scripts should become ``!= 0``.
#. The ``shell=True``-with-a-list defect in the five remaining local
   ``execute_command`` copies.
#. ``database/sims/db_register_rimtimsim_files.py``, which has the same
   download-and-swallow shape that this audit's companion fix removed from the
   socsim registration script — **and the same destination-path mismatch**: its
   ``aws s3 cp`` writes to the bare filename (the process working directory)
   while ``get_fits_header`` and ``compute_checksum`` read
   ``subdir_work + "/" + file``, i.e. ``/work/``. That script would fail to
   find its downloads even with the AWS CLI on the ``PATH``. Not fixed here,
   as this session's scope was the socsim registration path.
#. Adding an ``ENV PATH`` to the container image, or moving the remaining bare
   binary names to absolute paths. This one is a ``rapid_systems`` change, not
   a change in this repo.

W3: the sweep closing this audit
*********************************

The Batch payload co-design (``rapid_plan/research/batch-payload-proposal.md``)
named this audit's call-site inventory and risk table as W3's worklist. That
sweep landed on ``smdc`` via branch ``w3-swallow-sweep`` and resolves the
items above as follows.

**Item 1 — resolved.** ``execute_command``/``execute_command_and_return_stdout``/
``execute_command_in_shell`` are deleted from ``rapid_pipeline_subs.py``.
Every call site across ``pipeline/``, ``sims/``, and ``database/sims/`` — the
three excluded Batch payload entrypoint scripts and their ``.sh`` wrappers
aside, which are W5's scope — now goes through
``pipeline.runtime.process.run_tool`` or its named shell variant,
``run_shell``. Both raise ``pipeline.runtime.errors.ToolError`` on a nonzero
exit, a missing binary, or a non-executable target; there is no
``check=False`` and no way to call either without checking. This closes the
question of a ``check`` parameter by removing the unchecked path entirely
rather than adding an opt-in.

**Item 2 — resolved by the same conversion.** The ``>= 64`` convention in
``virtualPipelineOperator.py`` and the launcher scripts is gone along with the
return-code checks it lived in: a ``run_tool``/``run_shell`` failure now
raises, so a caller that used to compare a return code against 64 either lets
the exception propagate (the launcher scripts, whose previously-unchecked
submissions now fail loud by construction) or catches ``ToolError``
explicitly where the file already had catch-and-continue infrastructure
(``virtualPipelineOperator.py``'s ten sub-stage sites, ``launchBunchOf
ReferenceImagePipelines.py``, ``launchSciencePipelinesForDateTimeRangeWith
RefImageWindow.py``), replicating the prior failure handling instead of the
prior threshold.

**Item 3 — resolved.** All five file-local ``Popen(cmd, shell=True)``-with-a-list
copies (``sims/src/awsBatchJobLowLevelScript_CompressTroxelFitsFiles.py``,
``awsBatchSubmitJobs_CompressTroxelFitsFiles.py``, ``batchCompress
TroxelFitsFiles.py``, ``compressTroxelFitsFiles.py``,
``database/sims/db_register_troxel_sim_files.py``) are deleted along with
their local ``execute_command`` definitions; call sites route through
``run_tool``/``run_shell``. None of these five files' own call sites had
passed a genuine multi-argument list into the buggy shape (each call was
either a single-token command or already a string), so no silently-dropped
argument was found to have been actually executing differently — the risk was
latent, not triggered, at every site checked.

**Item 4 — resolved.** ``db_register_rimtimsim_files.py``'s dead local
``execute_command`` copy is deleted. Its live download path is rewritten from
an ``aws s3 cp`` subprocess call to a per-call boto3 client (workers are
forked by ``ProcessPoolExecutor``, so clients cannot be shared), writing
directly to ``subdir_work + "/" + file``, closing the destination-path
mismatch — the download now lands where ``get_fits_header`` and
``compute_checksum`` actually read from. This mirrors the pattern already
used by the fixed ``db_register_socsim_files.py``.

Two more files carrying the identical dead/live shape,
``sims/src/socsims/convert_socsims.py`` and
``sims/src/socsims/inject_fake_sources_into_l2_asdf_files.py``, were found
during this sweep's verification pass (they had not been named in the
original audit's inventory) and converted the same way: local
``execute_command_in_shell`` copies deleted, call sites moved to
``run_tool``/``run_shell``. Likewise
``sims/src/rimtimsim/convert_rimtimsim.py``, which called the shared
``rapid_pipeline_subs.execute_command`` (now deleted) directly.

**Item 5 — out of scope, unchanged.** The container image's ``ENV PATH`` is a
``rapid_systems`` change and remains for that repo. It is a hard dependency
of this sweep's fail-loud posture actually working in production (a missing
binary now raises instead of silently returning garbage, but the binary
still needs to be found), tracked in the batch-payload-proposal under
"Entrypoint, override, and environment contract".

**Also landed in this sweep, beyond the audit's own inventory:**

- The ``aws s3 ls``/``aws s3 cp``/``touch``-based helpers in
  ``rapid_pipeline_subs.py`` (``get_datetime_of_last_file_written_to_bucket``,
  ``write_done_file_to_s3_bucket``) are rewritten to boto3-native,
  raising implementations — no subprocess involved at all, closing the
  "failed CLI returns empty stdout, parsed as empty bucket" failure mode this
  audit named as the highest-risk pattern's sibling case.
- The ``ProcessPoolExecutor`` print-and-forget pattern (this audit's sibling
  finding, not itself enumerated here since it is a different failure class)
  is fixed at all ≥13 remaining sites: workers return ``(n_ok, n_failed)``,
  orchestrators sum across futures and count a future that raised outright as
  a failure, and the four registration scripts' hardcoded ``exit(0)`` is
  replaced with a real conditional exit.
- ``database/modules/utils/rapid_db.py``'s string-substituted SQL (a separate
  defect from this audit's subprocess focus, named by the same co-design
  proposal) is fully parameterized: every method converts from an f-string or
  regex-template query to ``%s`` placeholders and ``psycopg2.sql.Identifier``
  composition for dynamic table/column names. Seven post-DB pipeline scripts
  building raw SQL through ``dbh.execute_sql_queries`` are parameterized the
  same way. Each method's ``exit_code``-member error contract is preserved
  unchanged; migrating that contract to raise-on-error rides the W5/W6
  call-path conversion.

**Left for later work, recorded rather than fixed in this sweep:**

- ``db_register_rimtimsim_files.py``'s per-file registration loop has no
  try/except around download/registration — a single file's failure raises
  uncaught and aborts the whole run. This is the same *shape* of issue the
  ``ProcessPoolExecutor`` fix addresses elsewhere, but this loop is plain
  sequential, not pooled, so it fell outside this sweep's ProcessPoolExecutor
  mandate.
- ``pipeline/parallelRegisterCompletedJobsInDBAfterPostProc.py`` references an
  undefined name ``done_filename`` in one warning message (it should read
  ``science_pipeline_done_filename``), a pre-existing latent bug unrelated to
  subprocess/ProcessPool/SQL, found by this sweep's ruff pass but out of its
  mechanical scope.
- ``copy_data_from_file_into_database`` in ``rapid_db.py`` no longer calls
  ``exit()`` on failure (it raises instead), which means any out-of-file
  caller relying on process termination there now needs its own
  ``try/except``. Those callers were not located or modified in this sweep.
- The module docstring for ``pipeline/runtime/process.py`` states a caller
  "catches ``ToolError`` and reads ``exc.returncode``"; the actual attribute
  is ``exc.details["returncode"]`` (``ToolError`` has no top-level
  ``returncode``). Found live during this sweep; not fixed here since
  ``pipeline/runtime/`` is W2's module, not W3's.

Verification for this sweep: repo-wide grep confirms zero remaining call
sites of the deleted helper trio and the five file-local copies (outside the
three excluded entrypoint scripts), and zero ``shell=True`` outside
``pipeline/runtime/process.py``'s own checked ``run_shell``. ``ruff`` on every
touched file shows no new findings against the pre-sweep baseline (several
pre-existing unused-import and unused-variable findings remain, all present
before this sweep touched those files). The full test suite — W1's three
suites, W2's runtime suite, and this sweep's ``test_rapid_db.py`` — ran
in-image on ``rapid-admin`` via SSM; see the ledger for exit codes.

Closed by W5 (2026-08-06)
-------------------------

The three excluded entrypoint scripts are **deleted**, and with them the
last unconverted ``execute_command`` call sites this audit found.

``pipeline/awsBatchSubmitJobs_runSingle{Science,ReferenceImage,PostProc}Pipeline.py``
and their three ``.sh`` wrappers are replaced by
``pipeline/entrypoints/job.py`` over ``pipeline/stages/``. W3 deliberately
left them alone — converting call sites in files already slated for
replacement would have been work done twice — so between W3 and W5 they
carried twelve calls to helpers that no longer existed. **They were dead
code on ``smdc`` for that interval**: ``util.execute_command`` and
``util.execute_command_in_shell`` were removed by W3 sweep A, so any of
these scripts would have raised ``AttributeError`` at its first tool
invocation. Deleting them removes code that could not have run.

Each of the audit's numbered findings, and where it now stands:

1. **A science job cannot fail.** Gone with ``terminating_exitcode``.
   The SFFT branch that mapped to exit 4 — which the ``>= 64`` test then
   discarded and the wrapper flattened to 64 — is now a ``ToolError``
   raised by ``run_tool``, recorded as ``tool_failure`` in the attempt
   record with the job exiting 0. Scheduler-SUCCEEDED with
   application-failure is the representable combination the schema was
   built for.

2. **~60 unchecked ``execute_command`` sites.** Closed by W3 for the
   helper layer and by W5 for the three entrypoints. Zero call sites
   remain.

3. **The ``.sh`` wrappers, and no ``ENV PATH``.** Both closed. The
   wrappers are deleted — stdout and stderr flow to the CloudWatch safety
   stream through the awslogs driver, and the diagnostics bundle replaces
   the hand-uploaded logfile, removing the exit-66 inversion where a
   failed log upload outranked a successful science run.
   ``ENV PATH`` and ``ENV LD_LIBRARY_PATH`` are set explicitly in
   ``containers/rapid-pipeline/Containerfile`` (rapid_systems).

4. **Registration by log-grep.** Not closed here. The record-consuming
   registration path is W6's, behind the cutover fence; W5's entrypoint
   routes the registration job type but refuses to run it rather than
   dispatching to the legacy log-parsing script.

5. **``print()``-only, no ``logging``.** Closed for the converted layer:
   the entrypoint and every stage log through the runtime's configured
   logger, with job and attempt identifiers on every line.

6. **Bifurcated configuration.** Closed by W4's three homes and W5's
   consumption of them. No per-job ``.ini`` is downloaded, and none is
   written: what the product ``.ini`` carried is in the terminal record,
   keyed by attempt identity and immutable.

7. **``rapid_db.py``.** Parameterized by W3.

8, 9. **The VPO, and ``ProcessPoolExecutor`` swallowing.** The VPO
   touchpoints are W6's; the pool sites were fixed by W3.

10. **The tessellation.** W7's, running in parallel with this work. The
    bake stays in the Containerfile for now.

One correction this sweep's own record needs: the note above about
``pipeline/runtime/process.py``'s docstring naming ``exc.returncode``
where the attribute is ``exc.details["returncode"]`` still stands — W5
did not touch it either, for the same reason W3 did not.
