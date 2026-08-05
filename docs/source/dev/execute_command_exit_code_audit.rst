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
