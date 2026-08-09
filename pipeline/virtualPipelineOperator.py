"""
Virtual Pipeline Operator (VPO) for the Rapid Pipeline Operations.

To be executed inside a RAPID-pipeline Docker container.
"""


import sys
import os
import signal
import configparser
import boto3
from datetime import datetime, timezone
from dateutil import tz
import time

to_zone = tz.gettz('America/Los_Angeles')

import database.modules.utils.rapid_db as db
from database.modules.utils.rapid_db_connect import ConnectionExecutor, connection
from pipeline.operator.registrar import production_registrar
from pipeline.operator.submission import (SubmissionBinding, active_definition,
                                          submission_env)
from pipeline.runtime.process import run_tool
from pipeline.seams import submit_gathered
from submission import gathering, routes
from submission.startup import fetch_parameters
from pipeline.runtime.errors import ToolError

# `submission_env` and `production_registrar` EXTRACTED to
# `pipeline.operator.submission` / `pipeline.operator.registrar` (IR-1a).
# Imported here, not duplicated, so the phase logic below keeps calling
# them by their bare names; those modules carry the full reasoning.


swname = "virtualPipelineOperator.py"
swvers = "1.2"
cfg_filename_only = "awsBatchSubmitJobs_launchSingleSciencePipeline.ini"


# Specify python command to use for executing Python scripts.
#
# The interpreter is invoked by bare name against the image's controlled
# PATH rather than by absolute path (co-design, "Also reconsidered":
# interpreter invocation unified).
python_cmd = 'python3.11'

# Script paths are derived from RAPID_SW below, once it has been read.
# They used to be nine '/code/pipeline/...' literals here — a second home
# for the software root that this script ALSO reads from RAPID_SW twenty
# lines further down, so the two could disagree and the literals would
# silently win.


# THIS MODULE IS IMPORTABLE AGAIN (2026-08-08, VPO service-shape
# restructure). Everything from here to the end of the startup block below
# used to run AT IMPORT: reading `sys.argv[1]`, requiring RAPID_SW,
# STARTDATETIME and ENDDATETIME in the environment, and calling `exit(64)`
# when they were absent. So `from pipeline.virtualPipelineOperator import
# mjd_window` — borrowing one pure function — ran the whole of the old
# operator's startup as a side effect.
#
# Found live on rapid-admin: the restructured service's first live
# rehearsal resolved its credential, logged "REHEARSAL MODE", and then died
#
#     datearg = --start
#     *** Error: Env. var. STARTDATETIME not set; quitting...
#
# because the gather step's import re-read the NEW operator's argv, took
# `--start` as this script's legacy positional processing date, and then
# demanded the environment interface the restructure exists to retire. An
# unimportable module in a package that other code must import from is a
# hazard to every future caller, not just that one.
#
# The fix is the standard one and changes nothing about running this file
# as a script: the startup work moves into `_startup()`, called from the
# `__main__` block at the bottom, so `python3 virtualPipelineOperator.py`
# behaves exactly as before while `import` no longer executes it. The
# module-level names it sets are still module-level — `_startup` declares
# them global — because the phase logic below reads them by name.

def _startup():
    """The script startup this file used to do at import time."""
    # Every name this used to bind at module level stays a module-level
    # name: the phase logic in the `__main__` block below reads them all by
    # name, so a missing `global` here would turn one of them into a local
    # and leave the reader with a NameError at run time. Enumerated in full
    # rather than trimmed to the ones that looked used.
    global awaicgen_output_mosaic_image_file, cfg_path
    global compute_statistics_for_astroobjects_code, config_input
    global config_input_filename, crossmatch_sources_code, datearg
    global datetime_pt_now, datetime_utc_now, debug, enddatetime, istop
    global job_config_filename_base, job_logs_s3_bucket_base
    global load_psfcat_into_db_sources_code, pipeline_code
    global proc_pt_datetime_started, proc_utc_datetime
    global product_config_filename_base, product_s3_bucket_base
    global prune_notbest_merges_code, rapid_sw, rapid_work
    global start_time_benchmark, start_time_benchmark_at_start
    global startdatetime, verbose, zogy_output_diffimage_file

    # Print diagnostics.

    print("swname =", swname)
    print("swvers =", swvers)
    print("cfg_filename_only =", cfg_filename_only)
    print("python_cmd =", python_cmd)


    # Compute start time for benchmark.

    start_time_benchmark = time.time()
    start_time_benchmark_at_start = start_time_benchmark


    # Compute processing datetime (UT) and processing datetime (Pacific time).

    datetime_utc_now = datetime.now(timezone.utc)
    proc_utc_datetime = datetime_utc_now.strftime('%Y-%m-%dT%H:%M:%SZ')
    datetime_pt_now = datetime_utc_now.replace(tzinfo=timezone.utc).astimezone(tz=to_zone)
    proc_pt_datetime_started = datetime_pt_now.strftime('%Y-%m-%dT%H:%M:%S PT')

    print("proc_utc_datetime =",proc_utc_datetime)
    print("proc_pt_datetime_started =",proc_pt_datetime_started)


    # Initialize handler.

    istop = 0

    def signal_handler(signum, frame):
        print('Caught signal', signum)
        global istop
        istop = 1


    # Get processing date of interest from command-line argument.
    # This only needs to be given for running the VPO for just one specific processing date.

    try:
        datearg = (sys.argv)[1]
    except IndexError:
        datearg = None

    print("datearg =",datearg)


    # Read environment variables.

    rapid_sw = os.getenv('RAPID_SW')

    if rapid_sw is None:

        print("*** Error: Env. var. RAPID_SW not set; quitting...")
        exit(64)

    rapid_work = os.getenv('RAPID_WORK')

    if rapid_work is None:

        print("*** Error: Env. var. RAPID_WORK not set; quitting...")
        exit(64)

    cfg_path = rapid_sw + "/cdf"

    print("rapid_sw =",rapid_sw)
    print("cfg_path =",cfg_path)


    # Script paths, derived from the one software root rather than repeating
    # it (W4 single-homing sweep).

    pipeline_code = os.path.join(rapid_sw, "pipeline")
    load_psfcat_into_db_sources_code = os.path.join(pipeline_code, 'loadPSFCatIntoDBSourcesTable.py')
    crossmatch_sources_code = os.path.join(pipeline_code, 'crossMatchSources.py')
    compute_statistics_for_astroobjects_code = os.path.join(pipeline_code, 'computeStatisticsForAstroObjects.py')
    prune_notbest_merges_code = os.path.join(pipeline_code, 'pruneNotBestMerges.py')

    # The launcher and registration script paths that used to live here are gone
    # with the scripts themselves (W6 cutover fence). Submission, the completion
    # wait and registration are now three function calls into `pipeline.seams`
    # rather than three subprocess execs:
    #
    #   launchSciencePipelinesForDateTimeRangeWithRefImageWindow.py  -> seams.submit_units
    #   awsBatchSubmitJobs_launchPostProcPipelinesForProcDate.py     -> seams.submit_units
    #   launchBunchOfReferenceImagePipelines.py                      -> seams.submit_units
    #   parallelRegisterCompletedJobsInDB.py                         -> seams.run_registration
    #   parallelRegisterCompletedJobsInDBAfterPostProc.py            -> seams.run_registration
    #   wait_until_aws_batch_jobs_finished (describe_jobs, one call
    #     per job per poll, no timeout)                              -> seams.wait_for_completion
    #
    # The four scripts still exec'd below (loadPSFCat, crossMatch,
    # computeStatistics, pruneNotBestMerges) are science-layer post-processing,
    # not part of the completion chain, and are untouched by this fence.


    # AWS credentials come from boto3's default chain (job role, instance
    # role, or SSO) — no explicit key pair needed or read here.


    # To process OpenUniverse simulation images, environment variables STARTDATETIME and ENDDATETIME
    # specify observation datetimes.  Later, this will be augmented with code to query the
    # SOCProcs database table for controlling the processing the Roman Space Telescope WFI data.
    #
    # Inputs are observaton start and end datetimes of exposures to be processed.
    # E.g., startdatetime = "2028-09-08 00:18:00", enddatetime = "2028-09-11 00:00:00"

    startdatetime = os.getenv('STARTDATETIME')

    if startdatetime is None:

        print("*** Error: Env. var. STARTDATETIME not set; quitting...")
        exit(64)

    enddatetime = os.getenv('ENDDATETIME')

    if enddatetime is None:

        print("*** Error: Env. var. ENDDATETIME not set; quitting...")
        exit(64)


    # Read input parameters from .ini file.

    config_input_filename = cfg_path + "/" + cfg_filename_only
    config_input = configparser.ConfigParser()
    config_input.read(config_input_filename)

    verbose = int(config_input['JOB_PARAMS']['verbose'])
    debug = int(config_input['JOB_PARAMS']['debug'])
    # `job_info_s3_bucket_base` is NOT read here. It named the legacy IMSS-era
    # bucket (`rapid-pipeline-files`) that this account does not carry; the one
    # submission-path use of it was the reference gather's `job_bucket`, and
    # reading the .ini there made every gather fail at PutObject with
    # AccessDenied (see the manifest-bucket comment below). The buckets come
    # from the parameter tree.
    job_logs_s3_bucket_base = config_input['JOB_PARAMS']['job_logs_s3_bucket_base']
    product_s3_bucket_base = config_input['JOB_PARAMS']['product_s3_bucket_base']
    job_config_filename_base = config_input['JOB_PARAMS']['job_config_filename_base']
    product_config_filename_base = config_input['JOB_PARAMS']['product_config_filename_base']
    awaicgen_output_mosaic_image_file = config_input['AWAICGEN']['awaicgen_output_mosaic_image_file']
    zogy_output_diffimage_file = config_input['ZOGY']['zogy_output_diffimage_file']


    # Print variables.

    print("verbose =",verbose)
    print("debug =",debug)
    print("job_logs_s3_bucket_base =",job_logs_s3_bucket_base)
    print("product_s3_bucket_base =",product_s3_bucket_base)
    print("job_config_filename_base =",job_config_filename_base)
    print("product_config_filename_base =",product_config_filename_base)
    print("awaicgen_output_mosaic_image_file =",awaicgen_output_mosaic_image_file)
    print("zogy_output_diffimage_file =",zogy_output_diffimage_file)
    print("startdatetime =",startdatetime)
    print("enddatetime =",enddatetime)
    print("load_psfcat_into_db_sources_code =", load_psfcat_into_db_sources_code)
    print("crossmatch_sources_code =", crossmatch_sources_code)


    # Set signal hander.

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGQUIT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)


#-------------------------------------------------------------------------------------------------------------
# Method to look up ppid of Jobs database records associated with pipeline instances.
#-------------------------------------------------------------------------------------------------------------

def look_up_ppid_of_job_type(job_type):

    # Single-homed in submission/routes.py (W4): the ppid map used to be
    # this if/elif, three `ppid` keys in the master .ini, and bare integer
    # literals in SQL. A ppid is a routing fact — it says which pipeline a
    # row belongs to — so it lives with the rest of the routing
    # vocabulary.
    #
    # This function's own names are kept as an alias layer: the VPO's
    # callers say "postproc" and "refimage" where the manifest vocabulary
    # says "post-process" and "reference-image". Both spellings resolve
    # here, so converting the callers is a separate change and neither
    # spelling is a second home for the number.
    aliases = {"postproc": routes.JOB_TYPE_POST_PROCESS,
               "refimage": routes.JOB_TYPE_REFERENCE_IMAGE}
    try:
        return routes.ppid_for(aliases.get(job_type, job_type))
    except routes.RouteError as exc:
        print(f"Job type undefined ({job_type}): {exc}; quitting")
        exit(64)


#-------------------------------------------------------------------------------------------------------------
# Methods to turn the operator's one window into what the gathering queries take.
#-------------------------------------------------------------------------------------------------------------

def mjd_window(start, end):

    '''
    (start_mjdobs, end_mjdobs) for the operator's STARTDATETIME/ENDDATETIME.

    The readiness query selects (field, filter) pairs by mjdobs while the L2
    file selection is by timestamp. Both describe the SAME window, so it is
    converted here rather than accepted as two more environment variables that
    nothing keeps equal.

    Returned as built-in floats, NOT the `numpy.float64` astropy hands back.
    These two values are bound straight into the readiness query, and
    psycopg2 has no adapter for a numpy scalar, so it falls back to repr() —
    which under NumPy 2 is `np.float64(61679.0)` rather than `61679.0`. That
    is pasted into the SQL as a schema-qualified name and Postgres rejects it
    with `schema "np" does not exist`, aborting the transaction so every
    later query in the pass is skipped too. The failure is silent in the
    worst way: `get_field_fid_nframes_records_for_mjdobs_range` catches it,
    prints, and returns None, so gathering reports "0 (field, filter) pairs"
    — indistinguishable from a night with no data.
    '''

    from astropy.time import Time

    return (float(Time(start.replace(" ", "T"), format='isot', scale='utc').mjd),
            float(Time(end.replace(" ", "T"), format='isot', scale='utc').mjd))


def min_images_to_coadd():

    '''
    The release's minimum coadd depth.

    Read from release CONTENT (cdf/science/pipeline.toml), which is the home
    the W4 re-homing gave it — not from the master .ini, whose copy is the
    duplicate that re-homing was undoing.
    '''

    from pipeline.runtime import science_config

    science = science_config.load()
    return int(science_config.value(science, "ref_image",
                                    "min_n_images_to_coadd"))


def reference_window_override_for_run():

    '''
    This run's reference-observation-window override, or None.

    None on every ordinary run, which is the point: the window's
    authoritative value is release content, and an override exists for
    rehearsal and validation runs only — a product built under one is barred
    from a community surface (design/compute.md § Job definitions).

    There is deliberately NO environment variable here. The window used to
    be STARTREFIMMJDOBS/ENDREFIMMJDOBS, and retiring that path is the whole
    point of the change: nothing science-affecting is reachable from the
    environment. The manifest is the sole carrier, and this function is the
    seam an operator-input surface fills — the VPO's operator interface is
    the O4 restructure's scope, so until it lands an override is set by a
    caller constructing `ReferenceObservationWindow` and passing it, not by
    this process reading anything.
    '''

    return None


#-------------------------------------------------------------------------------------------------------------
# `SubmissionBinding`, `active_definition` and `submission_env` moved to
# `pipeline.operator.submission` (IR-1a); imported at module top.
#-------------------------------------------------------------------------------------------------------------
#-------------------------------------------------------------------------------------------------------------
# Method to wait until common set of AWS Batch jobs have finished.
#-------------------------------------------------------------------------------------------------------------

def wait_until_aws_batch_jobs_finished(job_type, proc_date, config_input, dbh,
                                       run_id=None, conn=None,
                                       timeout=None):

    """
    Wait until the attempts of a submitted batch are reconciler-terminal.

    NO LONGER THE OPERATOR'S PATH (round-3 finding #3). The three call sites in
    the loop below now use `wait_for_submitted`, which waits on each
    submission's OWN run id — the id `submit_gathered` actually stamped on the
    rows. This signature takes `run_id` as an OPTIONAL keyword and answers "no
    run_id given; nothing to wait for" when it is omitted, which is exactly
    what all three call sites did: the operator waited for nothing at all and
    proceeded to register jobs that were still running. A wait whose no-op case
    is reachable by forgetting an argument is a wait that will be forgotten
    again, so the callers no longer choose.

    Kept because it is the documented seam entry point and takes a caller's
    connection, which `wait_for_submitted` deliberately does not.

    W6 seam. What this replaced polled AWS Batch directly: one
    `describe_jobs` call PER JOB per poll, from a one-shot database snapshot
    taken before the loop (so a job that appeared later was never waited
    for), with no timeout at all — a stuck job blocked the operator forever.

    It now reads the attempt table the reconciler maintains. That table is the
    reconciled view: an attempt is terminal in it only once its scheduler
    truth is known, which is exactly the condition registration must wait for.
    One query covers the whole batch.

    The wait is bounded. On expiry the batch becomes a reconciliation case and
    the operator moves on: the reconciler classifies the stragglers on its own
    horizons whether or not anyone is waiting. That is a designed outcome, not
    a failure, and it is returned rather than raised past the caller.
    """

    from pipeline import seams

    print("Parameter values from method wait_until_aws_batch_jobs_finished:")
    print("job_type =", job_type)
    print("proc_date =", proc_date)
    print("run_id =", run_id)

    if run_id is None:
        print("no run_id given; nothing to wait for")
        return {}

    if conn is None:
        from database.modules.utils.rapid_db_connect import connection
        with connection("rapid-vpo-wait", lane="transaction") as owned:
            return _wait_on(owned, run_id, timeout)

    return _wait_on(conn, run_id, timeout)


def _wait_on(conn, run_id, timeout):
    from pipeline import seams

    kwargs = {}
    if timeout is not None:
        kwargs["timeout"] = timeout

    def report(counts, outstanding):
        print("waiting on run", run_id, "outstanding =", outstanding,
              "states =", counts)

    try:
        counts = seams.wait_for_completion(conn, run_id, on_poll=report,
                                           **kwargs)
    except seams.CompletionTimeout as exc:
        # Bounded wait expiring is the designed outcome for a stuck job.
        print("completion wait timed out:", exc)
        print("the batch is a reconciliation case; moving on")
        return {"timed_out": True, "outstanding": exc.outstanding}

    print("run", run_id, "is reconciler-terminal:", counts)
    return counts


def wait_for_submitted(submitted, timeout=None):
    """Wait for every batch a submission pass produced.

    Waits on each SUBMISSION'S OWN run id, not on the parent string the
    operator built (round-3 finding #3). `submit_gathered` re-scopes per batch
    — `<run_id>-<n>` wherever there is more than one — so with two or more
    batches the parent id matches NO attempt row, `wait_for_completion` finds
    zero attempts and returns immediately, and the operator would proceed to
    registration while the jobs were still running. One batch would have
    worked and two would not, which is the worst shape a bug can have.

    Returns a list of per-batch results, and never raises for a timeout: a
    bounded wait expiring is the designed outcome for a stuck job, and the
    reconciler classifies stragglers on its own horizons whether or not
    anyone is waiting.
    """
    results = []
    for submission, _attempt_ids in submitted:
        # `Submission.batch_id`, not `run_id`: the batch's run-scoped identity
        # is named `batch_id` on the submission and `run_id` on the attempt row
        # (`seams._precreate` stamps `run_id=manifest.batch_id`), and
        # `wait_for_completion` queries the row's column. A `getattr(...,
        # "run_id", None)` here read an attribute `Submission` has never had,
        # so it was None for EVERY submission: the wait skipped every batch and
        # the operator registered over jobs that were still running — the same
        # failure round-3 finding #3 fixed, reintroduced by reading the right
        # value under the wrong name. Attribute access, not `getattr` with a
        # default: a submission that cannot name its batch is a fault to raise,
        # not a batch to silently skip.
        batch_run_id = submission.batch_id
        if not batch_run_id:
            print("*** Warning: a submission carries no batch id; cannot wait "
                  "for it. It remains a reconciliation case.")
            continue
        with connection("rapid-vpo-wait", lane="transaction") as waitconn:
            results.append(_wait_on(waitconn, batch_run_id, timeout))
    return results


# `production_registrar` moved to `pipeline.operator.registrar` (IR-1a);
# imported at module top.


def registration_callback(factory, conn):
    """The callback `run_registration` should be given on `conn`.

    A named seam rather than an inline conditional at three call sites: the
    dry-run factory is None and must stay None (that is what makes
    `run_registration` pass `dry_run=True`), while a production factory has to
    be bound to the pass's own connection. Getting that wrong at any one of
    the three sites reintroduces finding #2 for that phase alone, which is
    precisely the kind of defect that hides.
    """
    if factory is None:
        return None
    return factory(conn)


#-------------------------------------------------------------------------------------------------------------
# Main program.
#-------------------------------------------------------------------------------------------------------------

if __name__ == '__main__':

    # The startup that used to run at import (see `_startup`). Running it
    # here and only here is what makes this module importable, and it runs
    # before anything else exactly as the module-level version did.
    _startup()


    # Open loop.

    exitcode = 0

    i = 0

    while True:


        # Get current date and time.

        datetime_utc_now = datetime.now(timezone.utc)
        proc_utc_datetime = datetime_utc_now.strftime('%Y-%m-%dT%H:%M:%SZ')
        datetime_pt_now = datetime_utc_now.replace(tzinfo=timezone.utc).astimezone(tz=to_zone)
        proc_pt_datetime_started = datetime_pt_now.strftime('%Y-%m-%dT%H:%M:%S PT')
        proc_pt_date = datetime_pt_now.strftime('%Y-%m-%d')

        print("proc_utc_datetime =",proc_utc_datetime)
        print("proc_pt_datetime_started =",proc_pt_datetime_started)
        print("proc_pt_date =",proc_pt_date)

        if datearg is None:
            proc_date = proc_pt_date
        else:
            proc_date = datearg

        os.environ['JOBPROCDATE'] = proc_date


        # Open database connection.

        dbh = db.RAPIDDB()

        if dbh.exit_code >= 64:
            exit(dbh.exit_code)


        # Launch reference-image pipelines.

        # W6 seam: submission goes through pipeline.seams.submit_gathered,
        # which batches the gathered units, publishes a manifest per batch,
        # submits ONE array job each, and pre-creates the attempt rows before
        # any child can start.
        #
        # The unit-gathering query this used to refuse for is now
        # `submission.gathering`, rebuilt against the surviving rapid_db
        # methods — so the refusal has become the thing it was standing in
        # for. The operations design still owns the ground-up VPO (workload
        # classes, versioned retry policy, the problems path); this is the
        # minimal loop that drives the seams, not that rebuild.
        # The registrar FACTORY is built ONCE for the whole operator loop, and
        # each phase binds it to that phase's own registration connection
        # (round-4 finding #2). Building it here means a misconfiguration is
        # one message at the top rather than three identical ones spread
        # through the run; binding it per connection is what keeps the product
        # rows and the watermark in one transaction.
        registrar_factory = production_registrar()

        # ONE parameter-tree read for the whole pass. All three phases resolve
        # their queue and job definition from it, each through its own route,
        # so the bindings differ by phase (finding #1) while the tree is read
        # once rather than three times.
        submission_parameters = fetch_parameters()

        submission_context = submission_env(routes.JOB_TYPE_REFERENCE_IMAGE,
                                            parameters=submission_parameters)
        run_id = f"vpo-{proc_date}-refimage-{datetime_utc_now:%H%M%S}"

        # The window in both forms the queries need. The (field, filter)
        # readiness query is by mjdobs and the L2 file selection is by
        # timestamp, so both come from the ONE window the operator gave —
        # derived here rather than taken as two more environment variables
        # that could disagree with each other.
        start_mjdobs, end_mjdobs = mjd_window(startdatetime, enddatetime)

        # The reference image's OWN observation window — a different window
        # from the one above, which selects units. Release content unless
        # this submission carries the manifest override; resolved once here
        # so the gathering pass and the manifest it is submitted under
        # cannot describe two different windows.
        reference_window_override = reference_window_override_for_run()
        reference_window = gathering.reference_observation_window(
            reference_window_override)

        reference_units = gathering.gather_reference_units(
            dbh, startdatetime, enddatetime,
            start_mjdobs=start_mjdobs, end_mjdobs=end_mjdobs,
            reference_window=reference_window,
            min_images_to_coadd=min_images_to_coadd(),
            s3_client=submission_context["s3_client"],
            # The manifest bucket, NOT the legacy `job_info_s3_bucket_base`
            # from the .ini (`rapid-pipeline-files`, an IMSS-era bucket this
            # account does not carry). The coadd-input list is written under
            # `submissions/<run_id>/coadd-inputs/`, beside the manifest that
            # cites it, so it belongs in the same bucket the manifest goes to
            # — which is also the only prefix the submitting identity is
            # granted (`roman-rapid-products/submissions/*`). Reading the
            # .ini here made every reference gather fail at PutObject with
            # AccessDenied, correctly: the grant is scoped to the design's
            # location and the .ini named a different one.
            job_bucket=submission_context["manifest_bucket"],
            run_id=run_id)

        with connection("rapid-vpo-submit", lane="transaction") as subconn:
            submitted = submit_gathered(
                reference_units,
                job_type=routes.JOB_TYPE_REFERENCE_IMAGE,
                queue=submission_context["queue"],
                job_definition=submission_context["job_definition"],
                binding=submission_context["binding"],
                manifest_bucket=submission_context["manifest_bucket"],
                manifest_prefix=submission_context["manifest_prefix"],
                s3_client=submission_context["s3_client"],
                batch_client=submission_context["batch_client"],
                execute=ConnectionExecutor(subconn).execute,
                run_id=run_id,
                reference_observation_window=reference_window_override)
        print(f"submitted {len(submitted)} reference-image batch(es) "
              f"under run {run_id}")


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("VPO Elapsed time in seconds to launch reference-image pipelines =",
            end_time_benchmark - start_time_benchmark)
        start_time_benchmark = end_time_benchmark


        # Wait for all reference-image pipelines to complete under AWS Batch.

        job_type = "refimage"

        print(f"Waiting until AWS Batch jobs have finished for job_type={job_type}, proc_date={proc_date}...")

        # Waits on each SUBMISSION'S own run id, not the parent string built
        # above (round-3 finding #3). This call omitted `run_id` entirely, so
        # the wait hit its "nothing to wait for" guard and returned {}
        # immediately — the operator went straight on to register jobs that
        # had not run. Passing the parent id would only have worked for a
        # single batch: `submit_gathered` re-scopes to `<run_id>-<n>` from two
        # batches on, so the parent then matches no attempt row at all.
        wait_for_submitted(submitted)

        print(f"Okay, all AWS Batch jobs have finished for job_type={job_type}, proc_date={proc_date}...")


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("VPO Elapsed time in seconds to wait for reference-image-pipeline AWS Batch jobs to finish =",
            end_time_benchmark - start_time_benchmark)
        start_time_benchmark = end_time_benchmark


        # Register metadata from reference-image pipelines into operations database.

        os.environ['MAKEREFIMAGESFLAG'] = "True"

        ppid_refimage = look_up_ppid_of_job_type(job_type)
        print("ppid_refimage =",ppid_refimage)
        # W6 seam: registration is a function call into the records
        # consumer, not a subprocess exec of a log-grep script. The four
        # scripts it replaced had no importable entry point at all — each was
        # a `__main__` block — which is exactly why this had to be an exec and
        # why their exit codes were the only channel back.
        #
        # WITH the registrar (round-3 finding #3). All three of these calls
        # omitted it, and `run_registration` passes `dry_run=register is None`
        # — so the production path decided, counted, reported success and wrote
        # no operation-table rows, leaving every attempt a candidate forever. A
        # rehearsal now has to be asked for by name (RAPID_VPO_DRY_RUN); the
        # default is production, which is the only safe direction for that
        # default to point.
        from pipeline.seams import run_registration
        from database.modules.utils.rapid_db_connect import connection

        with connection("rapid-vpo-registration", lane="transaction") as regconn:
            reg_run = run_registration(
                regconn,
                register=registration_callback(registrar_factory, regconn))
        print("registration pass:", reg_run.as_dict())
        if reg_run.failed:
            print(f"*** Error: {reg_run.failed} registration(s) failed; quitting...")
            dbh.close()
            exit(65)


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("VPO Elapsed time in seconds to register reference-image pipeline metadata into operations database =",
            end_time_benchmark - start_time_benchmark)
        start_time_benchmark = end_time_benchmark


        # For efficiency, the science pipelines are launched in two stages.  In the
        # first stage, only one representative science image per field/filter combination
        # is processed to, if needed, initially make the required reference image for the
        # other science images with the same field and filter.  In the second stage, all
        # other science images are processed (i.e., except for the aforementioned
        # representative science images).  A representative science image is the first in
        # a time-ordered, SCA-ordered list that is returned from a database query for a
        # given field and filter for the observation time range of interest.
        #
        # The science pipeline for a representative science image may not necessarily
        # generate the required reference image, only if it does not exist.
        #
        # MAKEREFIMAGESFLAG controls whether either the representative science images or
        # all the other science images are processed under AWS Batch.

        make_refimages_flags = ["True","False"]
        stage_labels = {"True":"StageOne","False":"StageTwo"}

        start_time_benchmark_at_loop_start = start_time_benchmark

        for make_refimages_flag in make_refimages_flags:

            stage_label = stage_labels[make_refimages_flag]
            print("stage_label =",stage_label)


            # MAKEREFIMAGESFLAG is still read downstream. DRYRUN is not: it was
            # set here for the pipeline launch script that the W6 cutover fence
            # deleted, and nothing in the tree has read it since. Removed
            # rather than left as a variable that looks like it controls
            # something — the real rehearsal switch is RAPID_VPO_DRY_RUN, which
            # gates the registrar and defaults to production (round-3 #3).

            os.environ['MAKEREFIMAGESFLAG'] = make_refimages_flag


            # Launch science pipelines.
            #
            # Load environment variables STARTDATETIME and ENDDATETIME to specify observation datetimes.

            os.environ['STARTDATETIME'] = startdatetime
            os.environ['ENDDATETIME'] = enddatetime

            # SUBMITTED FOR REAL (round-3 finding #3). This was an exit(64)
            # standing in for the unit-gathering query the deleted launcher
            # owned — a refusal that was right when it was written, because
            # fabricating a query would have submitted the wrong work silently.
            # `submission.gathering.gather_science_units` is now that query,
            # rebuilt against the surviving rapid_db methods, so the thing the
            # refusal was waiting for exists and the refusal is what is now
            # wrong: nothing past this line had ever executed.
            #
            # `make_references` is the stage-one/stage-two distinction this
            # loop already expresses. Stage one takes the representative image
            # per (field, filter) so a reference can be built where none
            # exists; stage two takes the rest. `gather_science_units` splits
            # exactly that way (`rows[:1]` versus `rows[1:]`), so the flag maps
            # straight onto it rather than needing a second notion of the same
            # idea.
            science_units = gathering.gather_science_units(
                dbh, startdatetime, enddatetime,
                start_mjdobs=start_mjdobs, end_mjdobs=end_mjdobs,
                min_images_to_coadd=min_images_to_coadd(),
                make_references=(make_refimages_flag == "True"))

            science_context = submission_env(
                routes.JOB_TYPE_SCIENCE, parameters=submission_parameters)
            science_run_id = (f"vpo-{proc_date}-science-{stage_label}-"
                              f"{datetime_utc_now:%H%M%S}")
            with connection("rapid-vpo-submit", lane="transaction") as subconn:
                submitted = submit_gathered(
                    science_units,
                    job_type=routes.JOB_TYPE_SCIENCE,
                    queue=science_context["queue"],
                    job_definition=science_context["job_definition"],
                    binding=science_context["binding"],
                    manifest_bucket=science_context["manifest_bucket"],
                    manifest_prefix=science_context["manifest_prefix"],
                    s3_client=science_context["s3_client"],
                    batch_client=science_context["batch_client"],
                    execute=ConnectionExecutor(subconn).execute,
                    run_id=science_run_id)
            print(f"submitted {len(submitted)} science batch(es) under run "
                  f"{science_run_id} ({stage_label})")


            # Code-timing benchmark.

            end_time_benchmark = time.time()
            print("VPO Elapsed time in seconds to launch science pipelines =",
                end_time_benchmark - start_time_benchmark)
            start_time_benchmark = end_time_benchmark


            # Wait for all science pipelines to complete under AWS Batch.

            job_type = "science"

            print(f"Waiting until AWS Batch jobs have finished for job_type={job_type}, proc_date={proc_date}, stage_label={stage_label}...")

            wait_for_submitted(submitted)

            print(f"Okay, all AWS Batch jobs have finished for job_type={job_type}, proc_date={proc_date}, stage_label={stage_label}...")


            # Code-timing benchmark.

            end_time_benchmark = time.time()
            print("VPO Elapsed time in seconds to wait for science-pipeline AWS Batch jobs to finish =",
                end_time_benchmark - start_time_benchmark)
            start_time_benchmark = end_time_benchmark


            # Register metadata from science pipelines into operations database.

            ppid = look_up_ppid_of_job_type(job_type)
            # W6 seam: registration is a function call into the records
            # consumer, not a subprocess exec of a log-grep script. The four
            # scripts it replaced had no importable entry point at all — each
            # was a `__main__` block — which is exactly why this had to be an
            # exec and why their exit codes were the only channel back.
            from pipeline.seams import run_registration
            from database.modules.utils.rapid_db_connect import connection

            with connection("rapid-vpo-registration",
                            lane="transaction") as regconn:
                reg_run = run_registration(
                    regconn,
                    register=registration_callback(registrar_factory,
                                                   regconn))
            print("registration pass:", reg_run.as_dict())
            if reg_run.failed:
                print(f"*** Error: {reg_run.failed} registration(s) failed; quitting...")
                dbh.close()
                exit(65)

            # The unconditional `exit(65)` that stood here is gone (round-3
            # finding #3). It sat at the `if`'s own indentation, so even a
            # wholly successful science registration exited 65 — a failure code
            # on the success path. It was unreachable only because the
            # submission stub above it exited 64 first, which is precisely the
            # kind of dead code that comes back to life the moment the thing in
            # front of it is fixed.


            # Code-timing benchmark.

            end_time_benchmark = time.time()
            print("VPO Elapsed time in seconds to register science-pipeline metadata into operations database =",
                end_time_benchmark - start_time_benchmark)
            start_time_benchmark = end_time_benchmark


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("VPO Elapsed time in seconds after all science pipelines ran and database metadata loaded =",
            end_time_benchmark - start_time_benchmark_at_loop_start)
        start_time_benchmark = end_time_benchmark


        # Launch post-processing pipelines.
        #
        # Load environment variable JOBPROCDATE to specify processing date.

        # SUBMITTED FOR REAL (round-3 finding #3), for the same reason the
        # science stub above went: `gather_post_process_units` exists, so the
        # refusal is standing in for something that is no longer missing.
        #
        # Post-process work is keyed by JOB rather than by rid — the unit is
        # "close out what this science job produced" — which is why this
        # gatherer takes the processing date and nothing else.
        postproc_units = gathering.gather_post_process_units(dbh, proc_date)

        postproc_context = submission_env(
            routes.JOB_TYPE_POST_PROCESS, parameters=submission_parameters)
        postproc_run_id = (f"vpo-{proc_date}-postproc-"
                           f"{datetime_utc_now:%H%M%S}")
        with connection("rapid-vpo-submit", lane="transaction") as subconn:
            submitted = submit_gathered(
                postproc_units,
                job_type=routes.JOB_TYPE_POST_PROCESS,
                queue=postproc_context["queue"],
                job_definition=postproc_context["job_definition"],
                binding=postproc_context["binding"],
                manifest_bucket=postproc_context["manifest_bucket"],
                manifest_prefix=postproc_context["manifest_prefix"],
                s3_client=postproc_context["s3_client"],
                batch_client=postproc_context["batch_client"],
                execute=ConnectionExecutor(subconn).execute,
                run_id=postproc_run_id)
        print(f"submitted {len(submitted)} post-process batch(es) under run "
              f"{postproc_run_id}")


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("VPO Elapsed time in seconds after launching postproc pipelines =",
            end_time_benchmark - start_time_benchmark)
        start_time_benchmark = end_time_benchmark


        # Wait for all post-processing pipelines to complete under AWS Batch.

        job_type = "postproc"

        print(f"Waiting until AWS Batch jobs have finished for job_type={job_type}, proc_date={proc_date}...")

        wait_for_submitted(submitted)

        print(f"Okay, all AWS Batch jobs have finished for job_type={job_type}, proc_date={proc_date}...")


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("VPO Elapsed time in seconds after waiting for postproc-pipeline AWS Batch jobs to finish =",
            end_time_benchmark - start_time_benchmark)
        start_time_benchmark = end_time_benchmark


        # Register metadata from post-processing pipelines into operations database.

        # W6 seam: registration is a function call into the records
        # consumer, not a subprocess exec of a log-grep script. The four
        # scripts it replaced had no importable entry point at all — each was
        # a `__main__` block — which is exactly why this had to be an exec and
        # why their exit codes were the only channel back.
        from pipeline.seams import run_registration
        from database.modules.utils.rapid_db_connect import connection

        with connection("rapid-vpo-registration", lane="transaction") as regconn:
            reg_run = run_registration(
                regconn,
                register=registration_callback(registrar_factory, regconn))
        print("registration pass:", reg_run.as_dict())
        if reg_run.failed:
            print(f"*** Error: {reg_run.failed} registration(s) failed; quitting...")
            dbh.close()
            exit(65)


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("VPO Elapsed time in seconds after registering postproc-pipeline metadata into operations database =",
            end_time_benchmark - start_time_benchmark)
        start_time_benchmark = end_time_benchmark


        # Launch script to load PSF-fit catalogs into database sources tables.
        #
        # Environment variable JOBPROCDATE to specify processing date is required.

        fname_out = "load_psfcat_into_db_sources_code" + "_" + proc_date + ".out"
        load_psfcat_into_db_sources_cmd = [python_cmd,
                                           load_psfcat_into_db_sources_code]

        try:
            run_tool(load_psfcat_into_db_sources_cmd,capture_path=fname_out)
        except ToolError as exc:
            print(f"*** Error: {load_psfcat_into_db_sources_cmd} failed ({exc}); quitting...")
            dbh.close()
            exit(64)


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("VPO Elapsed time in seconds after loading Sources database records =",
            end_time_benchmark - start_time_benchmark)
        start_time_benchmark = end_time_benchmark


        # Launch script to crossmatch sources and astroobjects database tables.
        #
        # Environment variable JOBPROCDATE to specify processing date is required.

        fname_out = "crossmatch_sources_code" + "_" + proc_date + ".out"
        crossmatch_sources_cmd = [python_cmd,
                                  crossmatch_sources_code]

        try:
            run_tool(crossmatch_sources_cmd,capture_path=fname_out)
        except ToolError as exc:
            print(f"*** Error: {crossmatch_sources_cmd} failed ({exc}); quitting...")
            dbh.close()
            exit(64)


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("VPO Elapsed time in seconds after crossmatching Sources and AstroObjects database records =",
            end_time_benchmark - start_time_benchmark)
        start_time_benchmark = end_time_benchmark


        # Launch script to compute statistics for astroobjects database tables.
        #
        # Environment variable JOBPROCDATE to specify processing date is required.

        fname_out = "compute_statistics_for_astroobjects_code" + "_" + proc_date + ".out"
        compute_statistics_for_astroobjects_cmd = [python_cmd,
                                                   compute_statistics_for_astroobjects_code]

        try:
            run_tool(compute_statistics_for_astroobjects_cmd,capture_path=fname_out)
        except ToolError as exc:
            print(f"*** Error: {compute_statistics_for_astroobjects_cmd} failed ({exc}); quitting...")
            dbh.close()
            exit(64)


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("VPO Elapsed time in seconds after computing statistics for AstroObjects database records =",
            end_time_benchmark - start_time_benchmark)
        start_time_benchmark = end_time_benchmark


        # Launch script to delete not-best Merges database records.

        fname_out = "prune_notbest_merges_code" + "_" + proc_date + ".out"
        prune_notbest_merges_cmd = [python_cmd,
                                    prune_notbest_merges_code]

        try:
            run_tool(prune_notbest_merges_cmd,capture_path=fname_out)
        except ToolError as exc:
            print(f"*** Error: {prune_notbest_merges_cmd} failed ({exc}); quitting...")
            dbh.close()
            exit(64)


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("VPO Elapsed time in seconds after deleting not-best Merges database records =",
            end_time_benchmark - start_time_benchmark)
        start_time_benchmark = end_time_benchmark


        # Close database connection.

        dbh.close()

        if dbh.exit_code >= 64:
            exit(dbh.exit_code)


        # Break out of open loop if running the VPO for just one specific processing date.

        if datearg is not None:
            print(f"Terminating normally since this VPO run is just for one specific processing date: datearg={datearg}...")
            break


        print("Sleeping 30 seconds...")
        time.sleep(30)
        print("Waking up...")

        if istop == 1:
            print("Terminating gracefully now...")
            exitcode = 7
            exit(exitcode)

        i += 1
        print("i = ",i)


        #
        # End of open loop (but we are not iterating because of break above).
        #




    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("VPO Elapsed total time in seconds to run VPO =",
        end_time_benchmark - start_time_benchmark_at_start)


    # Termination.

    print("Terminating: exitcode =",exitcode)

    exit(exitcode)
