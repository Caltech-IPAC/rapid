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
from dataclasses import dataclass
from pipeline.runtime.process import run_tool
from pipeline.seams import submit_gathered
from submission import gathering, routes
from submission.startup import fetch_parameters
from pipeline.runtime.errors import ToolError


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
job_info_s3_bucket_base = config_input['JOB_PARAMS']['job_info_s3_bucket_base']
job_logs_s3_bucket_base = config_input['JOB_PARAMS']['job_logs_s3_bucket_base']
product_s3_bucket_base = config_input['JOB_PARAMS']['product_s3_bucket_base']
job_config_filename_base = config_input['JOB_PARAMS']['job_config_filename_base']
product_config_filename_base = config_input['JOB_PARAMS']['product_config_filename_base']
awaicgen_output_mosaic_image_file = config_input['AWAICGEN']['awaicgen_output_mosaic_image_file']
zogy_output_diffimage_file = config_input['ZOGY']['zogy_output_diffimage_file']


# Print variables.

print("verbose =",verbose)
print("debug =",debug)
print("job_info_s3_bucket_base =",job_info_s3_bucket_base)
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
    '''

    from astropy.time import Time

    return (Time(start.replace(" ", "T"), format='isot', scale='utc').mjd,
            Time(end.replace(" ", "T"), format='isot', scale='utc').mjd)


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


#-------------------------------------------------------------------------------------------------------------
# Method to resolve the submission-time execution binding and its clients.
#-------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class SubmissionBinding:

    '''
    The four binding facts the OPERATOR knows, before there is a manifest.

    NOT an `ExecutionBinding`, and that is the point. `ExecutionBinding`
    requires `manifest_checksum` and refuses to be constructed without it —
    deliberately, because an attempt row must always name the manifest it was
    submitted under. But the checksum is a property of a BATCH, and the
    operator resolves its binding once per phase, before any batch has been
    assembled. `submission_env` used to build an `ExecutionBinding` with
    `manifest_checksum=None` anyway, which raised `ValueError` on every
    production call — the operator could not submit anything at all. (Found
    while writing the round-4 finding #1 routing tests, which construct this
    binding for real rather than stubbing it.)

    `submit_gathered` is where the two meet: it publishes the manifest, reads
    these four fields off whatever it was handed, and builds the real
    `ExecutionBinding` with the checksum it now has. So this carries exactly
    what the operator can know and nothing it cannot, and the validation
    stays where the complete fact exists.
    '''

    job_definition_arn: str
    image_digest: str
    job_definition_rev: int
    release_identity: str

    def __post_init__(self) -> None:
        missing = [name for name in
                   ("job_definition_arn", "image_digest",
                    "job_definition_rev", "release_identity")
                   if getattr(self, name) in (None, "")]
        if missing:
            raise ValueError(
                "the submission binding is incomplete; missing: "
                + ", ".join(missing)
                + ". These are the facts the CI pipeline produces and the "
                "attempt row must record to be reproducible.")


def active_definition(batch_client, family):

    '''
    The one ACTIVE revisioned job-definition ARN for a definition family.

    THE EXECUTION BINDING MUST NAME WHAT ACTUALLY RAN (round-5 finding). The
    parameter tree carries a definition FAMILY — `rapid-pipeline-science` —
    and submitting that bare name lets Batch resolve whichever revision is
    ACTIVE at the instant of submission. Nothing records which one that was:
    the revision was carried separately, as a process-wide
    `RAPID_JOB_DEFINITION_REV`, and a single integer cannot be right for two
    independently revisioned families at once. The science and bulk
    definitions revise on their own schedules, so whichever number the
    environment held, at least one class recorded a revision it did not run.

    That is not a bookkeeping detail. `ExecutionBinding.definition_identity`
    synthesizes `<name>:<rev>` from the recorded pair, and the reconciler
    compares its observation of the real job against it — so a binding whose
    revision came from the environment makes the reconciler record DRIFT on
    attempts that ran under exactly the definition they were submitted to. At
    ramp scale that is a false-positive per attempt, against a gate that
    requires zero unexplained terminal records.

    So the revision is RESOLVED, not declared, and resolved once per family at
    env build. The `describe_job_definitions` call filters to ACTIVE and the
    exact family name; Batch returns revisions oldest-first, so the last is
    the one a bare-name submission would have reached — the same revision,
    now named explicitly and recorded.

    AMBIGUITY IS REFUSED rather than resolved by guessing. `jobDefinitionName`
    is an exact-match filter, so more than one distinct family coming back
    means the account holds something this code does not model, and picking
    one would submit real work under a definition nobody chose. None coming
    back means the family does not exist and every submission under it would
    fail at Batch with a far less legible error.

    Parameters
    ----------
    batch_client : botocore client
        Batch client, injected so the tests can drive this without AWS.
    family : str
        The definition family name from the parameter tree.

    Returns
    -------
    dict
        `arn` (the versioned ARN), `revision` (int), and `image_digest`
        (the digest the definition's container actually names).

    Raises
    ------
    RuntimeError
        No ACTIVE revision, or more than one family in the response.
    '''

    described = batch_client.describe_job_definitions(
        jobDefinitionName=family, status="ACTIVE")
    definitions = described.get("jobDefinitions", [])

    if not definitions:
        raise RuntimeError(
            "no ACTIVE revision of job definition family {!r}; a submission "
            "under it could not run, and binding it to a revision that does "
            "not exist would record a job that never was".format(family))

    names = {definition["jobDefinitionName"] for definition in definitions}
    if len(names) > 1:
        raise RuntimeError(
            "job definition family {!r} resolved to more than one "
            "definition ({}); refusing to choose, because submitting real "
            "work under a definition nobody selected is worse than not "
            "submitting it".format(family, ", ".join(sorted(names))))

    # Batch returns revisions in ascending order; the last ACTIVE one is what
    # a bare-name submission would have resolved to.
    latest = definitions[-1]
    image = latest.get("containerProperties", {}).get("image", "")

    return {
        "arn": latest["jobDefinitionArn"],
        "revision": int(latest["revision"]),
        "image_digest": image.split("@", 1)[-1] if "@" in image else "",
    }


def submission_env(job_type, parameters=None, batch_client=None,
                   s3_client=None):

    '''
    The queue, job definition, binding and clients one submission needs.

    THE QUEUE AND DEFINITION ARE PER JOB TYPE (round-4 finding #1), and they
    come from the route matrix rather than from the environment. This function
    used to take `job_type` and ignore it, returning one singular
    `RAPID_JOB_QUEUE`/`RAPID_JOB_DEFINITION` pair to all three phases. The
    matrix does not allow that: reference-image runs on the BULK class and
    science and post-process on PROMPT, so whichever single pair was
    configured, at least one phase was submitted to a queue whose job
    definition names the other class — and `validate_route` rejects it at the
    entrypoint, before any processing, exactly as it is designed to.

    `routes.Route` names the parameter-tree KEYS (`batch/queue-bulk`,
    `batch/job-definition-science`, ...) and deliberately does not carry the
    names themselves, so this resolves them through `fetch_parameters` — the
    same read the entrypoint validates against. One fact, one home: were the
    names duplicated into the environment they could disagree with the tree
    the entrypoint checks, and a disagreement there is a rejected submission.

    The rest stay in the ENVIRONMENT, because they are deployment facts that
    change with every image build: the image digest and the release identity
    are what the CI pipeline produces and what the attempt row must record to
    be reproducible.

    THE REVISION IS NOT AMONG THEM (round-5 finding). It used to be, as a
    process-wide `RAPID_JOB_DEFINITION_REV`, and that is exactly the defect:
    one integer declared for two independently revisioned families, recorded
    beside a bare family name that Batch resolved to whatever was ACTIVE.
    `active_definition` resolves it per route class instead, and the SAME
    versioned ARN is both submitted and recorded — which is the property that
    makes the reconciler's comparison meaningful rather than a coin flip.

    Every one is REQUIRED. A submission that cannot name its own binding is
    exactly what migration 013's amended submitted-state constraint refuses,
    and defaulting any of them would create rows whose binding does not
    describe the job that ran.

    Parameters
    ----------
    job_type : str
        The phase being submitted. Selects the route, and through it the
        queue and job definition.
    parameters : dict, optional
        Parameter-tree values, relative-keyed, as `fetch_parameters`
        returns them. Injected by the tests and by a caller that has
        already read the tree; fetched here when omitted.
    batch_client : botocore client, optional
        Batch client, used to resolve the ACTIVE revision and returned for
        the submission itself. Injected by the tests; built here when
        omitted, so one client serves both.
    s3_client : botocore client, optional
        S3 client for the manifest write. Injected by the tests for the same
        reason as `batch_client`: resolving a binding is a decision, and a
        test of that decision should not need AWS credentials or a region to
        construct a client the resolution never calls.
    '''

    required = ("RAPID_IMAGE_DIGEST",
                "RAPID_RELEASE_IDENTITY", "RAPID_MANIFEST_BUCKET")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        print("*** Error: the submission environment is incomplete; "
              "missing {}; quitting...".format(", ".join(missing)))
        exit(64)

    from submission.startup import fetch_parameters

    if parameters is None:
        parameters = fetch_parameters()

    route = routes.route_for(job_type)

    # A tree that does not carry this route's keys cannot bind the phase, and
    # guessing one would submit to whatever the last phase happened to use —
    # which is the defect this replaces. One clear message, before submission.
    binding_names = {}
    for kind, key in (("queue", route.queue_parameter),
                      ("job_definition", route.definition_parameter)):
        value = parameters.get(key)
        if not value:
            print("*** Error: the parameter tree does not carry {}, so the "
                  "{} for job type {} cannot be resolved; quitting...".format(
                      key, kind.replace("_", " "), job_type))
            exit(64)
        binding_names[kind] = value

    if batch_client is None:
        batch_client = boto3.client('batch')
    if s3_client is None:
        s3_client = boto3.client('s3')

    # The family from the tree, resolved to the one ACTIVE revision. What is
    # submitted and what is recorded are now the same string by construction,
    # rather than two values that agree only while an env var happens to be
    # right.
    active = active_definition(batch_client, binding_names["job_definition"])
    job_definition = active["arn"]

    return {
        "queue": binding_names["queue"],
        "job_definition": job_definition,
        "workload_class": route.workload_class,
        "binding": SubmissionBinding(
            job_definition_arn=job_definition,
            job_definition_rev=active["revision"],
            image_digest=os.environ['RAPID_IMAGE_DIGEST'],
            release_identity=os.environ['RAPID_RELEASE_IDENTITY']),
        "manifest_bucket": os.environ['RAPID_MANIFEST_BUCKET'],
        "manifest_prefix": os.environ.get('RAPID_MANIFEST_PREFIX',
                                          'submissions'),
        "s3_client": s3_client,
        "batch_client": batch_client,
    }


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
        batch_run_id = getattr(submission, "run_id", None)
        if not batch_run_id:
            print("*** Warning: a submission carries no run id; cannot wait "
                  "for it. It remains a reconciliation case.")
            continue
        with connection("rapid-vpo-wait", lane="transaction") as waitconn:
            results.append(_wait_on(waitconn, batch_run_id, timeout))
    return results


def production_registrar():
    """A factory for the real registration callback: connection -> callback.

    IT TAKES THE PASS'S CONNECTION (round-4 finding #2), and that is the whole
    point of the extra layer. This used to return a callback built over
    `registrar(rapid_db.RAPIDDB, store)` — the class, as a factory — which
    meant the registrar opened a SECOND, autocommitting connection of its own
    while `run_registration(regconn, ...)` advanced the watermark on the
    first. Two connections cannot be one transaction, so product rows became
    durable before the watermark was attempted, and a crash between them left
    rows written with the attempt still a candidate: the next pass registered
    the same products all over again. That is round-3 finding #8, fixed in the
    registration job and reintroduced here.

    Handed a connection, the registrar builds its handle over that one
    (`RAPIDDB.borrowing`), whose commits are suppressed, so the product rows
    and the watermark land in one transaction — the same shape as
    `entrypoints.job.registrar_for(context, conn)`, which is the pattern this
    now follows deliberately rather than by coincidence.

    A FACTORY rather than a callback because the operator's three phases each
    open their own registration connection. One callback built once could only
    ever borrow one of them, which would put the split back for the other two.
    The expensive parts — reading the bucket name, building the S3 client and
    the store — still happen once, here; only the per-connection binding is
    deferred.

    `registrar` takes its database handle as a CALLABLE, so the VPO can build
    one from what it already has — a records bucket name and an S3 client —
    without standing up the stage machinery a job entrypoint has. Without this
    the VPO's only options were a dry run or nothing, which is exactly how
    registration came to report success while writing no rows.

    The bucket is read from the environment like every other deployment fact
    this module needs, and is REQUIRED: a registrar that cannot find the
    records it registers from would fail per-attempt, deep inside a pass,
    rather than here where it is one clear message.

    Returns None where DRY-RUN is explicitly asked for. Production defaults to
    production — the flag has to be set to get a rehearsal, never the other
    way round.
    """
    if os.environ.get('RAPID_VPO_DRY_RUN', '').lower() in ('1', 'true', 'yes'):
        print("*** RAPID_VPO_DRY_RUN is set: registration will DECIDE only "
              "and write no operation-table rows.")
        return None

    records_bucket = os.environ.get('RAPID_RECORDS_BUCKET')
    if not records_bucket:
        print("*** Error: Env. var. RAPID_RECORDS_BUCKET not set; the "
              "registrar reads each attempt's terminal record from it and "
              "cannot be built without it; quitting...")
        exit(64)

    from database.modules.utils import rapid_db
    from pipeline.registration.products import registrar
    from pipeline.runtime.boundaries import S3ObjectStore

    store = S3ObjectStore(records_bucket, client=boto3.client("s3"))

    def for_connection(conn):
        """The callback for ONE registration pass, on ITS connection."""
        return registrar(lambda: rapid_db.RAPIDDB.borrowing(conn), store)

    return for_connection


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

        reference_units = gathering.gather_reference_units(
            dbh, startdatetime, enddatetime,
            start_mjdobs=start_mjdobs, end_mjdobs=end_mjdobs,
            min_images_to_coadd=min_images_to_coadd(),
            s3_client=submission_context["s3_client"],
            job_bucket=job_info_s3_bucket_base,
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
                run_id=run_id)
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
