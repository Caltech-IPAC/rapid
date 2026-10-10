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
from datetime import timedelta

to_zone = tz.gettz('America/Los_Angeles')

import modules.utils.rapid_pipeline_subs as util
import database.modules.utils.rapid_db as db


swname = "virtualPipelineOperator.py"
swvers = "1.9"
cfg_filename_only = "awsBatchSubmitJobs_launchSingleSciencePipeline.ini"


# Specify python command to use for executing Python scripts.

python_cmd = '/usr/bin/python3.11'
launch_science_pipelines_code = '/code/pipeline/launchSciencePipelinesForDateTimeRangeWithRefImageWindow.py'
register_science_pipeline_jobs_code = '/code/pipeline/parallelRegisterCompletedJobsInDB.py'
launch_postproc_pipelines_code = '/code/pipeline/awsBatchSubmitJobs_launchPostProcPipelinesForProcDate.py'
register_postproc_pipeline_jobs_code = '/code/pipeline/parallelRegisterCompletedJobsInDBAfterPostProc.py'
load_psfcat_into_db_sources_code = '/code/pipeline/loadPSFCatIntoDBSourcesTable.py'
load_secat_into_db_xsources_code = '/code/pipeline/loadSECatIntoDBSourcesTable.py'
crossmatch_sources_code = '/code/pipeline/crossMatchSources.py'
crossmatch_xsources_code = '/code/pipeline/crossMatchXSources.py'
compute_statistics_for_astroobjects_code = '/code/pipeline/computeStatisticsForAstroObjects.py'
compute_statistics_for_xastroobjects_code = '/code/pipeline/computeStatisticsForXAstroObjects.py'
prune_notbest_merges_code = '/code/pipeline/pruneNotBestMerges.py'
prune_notbest_xmerges_code = '/code/pipeline/pruneNotBestXMerges.py'
produce_alerts_code = '/code/pipeline/produceAlertsForProcDate.py'
launch_reference_image_pipelines_code = '/code/pipeline/launchBunchOfReferenceImagePipelines.py'
# Python script /code/pipeline/parallelRegisterCompletedJobsInDB.py is dual purposed to
# handle both reference-image pipeline jobs and science pipeline jobs, with PIPEID as parameter.
register_reference_image_pipeline_jobs_code = register_science_pipeline_jobs_code


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

    # Ask the open loop to stop at its next safe point: before starting a processing request,
    # or while sleeping between iterations.  A request already under way is completed first.

    global istop
    print('Caught signal', signum, '; will stop at the next safe point')
    istop = 1


# Get processing date of interest from command-line argument.
# This only needs to be given for running the VPO for just one specific processing date.
# Without it, the VPO runs automatically in an open loop, choosing the observation range of
# each processing request itself (see compute_open_loop_observation_range).

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


# Other required environment variables.

aws_access_key_id = os.getenv('AWS_ACCESS_KEY_ID')
aws_secret_access_key = os.getenv('AWS_SECRET_ACCESS_KEY')

if aws_access_key_id is None:

    print("*** Error: Env. var. AWS_ACCESS_KEY_ID not set; quitting...")
    exit(64)

if aws_secret_access_key is None:

    print("*** Error: Env. var. AWS_SECRET_ACCESS_KEY not set; quitting...")
    exit(64)


# For a run for one specific processing date, environment variables STARTDATETIME and
# ENDDATETIME specify the observation start and end datetimes of the exposures to be processed.
# E.g., startdatetime = "2028-09-08 00:18:00", enddatetime = "2028-09-11 00:00:00"
# In the open loop (no processing date given), they are not used: each iteration computes its
# own observation range from the L2Files and DiffImages database tables.

startdatetime = os.getenv('STARTDATETIME')
enddatetime = os.getenv('ENDDATETIME')

if datearg is not None:

    if startdatetime is None:

        print("*** Error: Env. var. STARTDATETIME not set; quitting...")
        exit(64)

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

# Number of times an AWS Batch job that failed for an infrastructure reason (e.g.,
# CannotPullContainerError) is resubmitted; 0 turns resubmission off.

# VPO settings, from the [VPO_SETTINGS] section (with these defaults if it or a key is missing).

# Number of times an AWS Batch job that failed for an infrastructure reason (e.g.,
# CannotPullContainerError) is resubmitted; 0 turns resubmission off.

n_retry_failed_aws_batch_job = config_input.getint('VPO_SETTINGS','n_retry_failed_aws_batch_job',fallback=3)

# Open loop: an iteration runs only when the unprocessed L2Files span at least
# min_elapsed_observation_seconds of observation time, and covers at most
# max_elapsed_observation_seconds; otherwise the VPO sleeps open_loop_sleep_seconds and checks
# again.  An exposure with L2Files for fewer than n_scas_per_exposure SCAs, one registered within
# the last l2file_settle_seconds, is taken to be still arriving, and the range ends before it.

min_elapsed_observation_seconds = config_input.getfloat('VPO_SETTINGS','min_elapsed_observation_seconds',fallback=900.0)
max_elapsed_observation_seconds = config_input.getfloat('VPO_SETTINGS','max_elapsed_observation_seconds',fallback=7200.0)
open_loop_sleep_seconds = config_input.getfloat('VPO_SETTINGS','open_loop_sleep_seconds',fallback=600.0)
l2file_settle_seconds = config_input.getfloat('VPO_SETTINGS','l2file_settle_seconds',fallback=1800.0)

n_scas_per_exposure = 18          # Roman WFI


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
print("n_retry_failed_aws_batch_job =",n_retry_failed_aws_batch_job)
print("min_elapsed_observation_seconds =",min_elapsed_observation_seconds)
print("max_elapsed_observation_seconds =",max_elapsed_observation_seconds)
print("open_loop_sleep_seconds =",open_loop_sleep_seconds)
print("l2file_settle_seconds =",l2file_settle_seconds)
print("startdatetime =",startdatetime)
print("enddatetime =",enddatetime)
print("launch_science_pipelines_code =", launch_science_pipelines_code)
print("register_science_pipeline_jobs_code =", register_science_pipeline_jobs_code)
print("launch_postproc_pipelines_code =", launch_postproc_pipelines_code)
print("register_postproc_pipeline_jobs_code =", register_postproc_pipeline_jobs_code)
print("produce_alerts_code =", produce_alerts_code)


# Set signal hander.

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGQUIT, signal_handler)


#-------------------------------------------------------------------------------------------------------------
# Method to look up ppid of Jobs database records associated with pipeline instances.
#-------------------------------------------------------------------------------------------------------------

def look_up_ppid_of_job_type(job_type):

    if job_type == "science":
        ppid = 15
    elif job_type == "postproc":
        ppid = 17
    elif job_type == "refimage":
        ppid = 12
    else:
        print(f"Job type undefined ({job_type}); quitting")
        exit(64)

    return ppid


#-------------------------------------------------------------------------------------------------------------
# Method to close out the ProcReqs record and then terminate.
#-------------------------------------------------------------------------------------------------------------

def finalize_procreqs_and_exit(dbh,reqid,status,exitcode):

    """
    Close out the ProcReqs record for the current processing request, close the database
    connection, and then terminate with the given exit code.  Set status=1 if the processing
    request finished normally, or status=-1 if it terminated early because of an error.
    """

    dbh.finalize_procreqs_record(reqid,status)

    if dbh.exit_code >= 64:
        print(f"*** Error: Could not finalize ProcReqs record (reqid={reqid},status={status}); continuing...")

    dbh.close()

    exit(exitcode)


#-------------------------------------------------------------------------------------------------------------
# Method to wait until common set of AWS Batch jobs have finished.
#-------------------------------------------------------------------------------------------------------------

def wait_until_aws_batch_jobs_finished(job_type,proc_date,config_input,dbh,reqid):

    """
    Wait until AWS Batch jobs of a given job type and processing date have finished.
    """

    print("Parameter values from method wait_until_aws_batch_jobs_finished:")
    print("job_type =",job_type)
    print("proc_date =",proc_date)

    ppid = look_up_ppid_of_job_type(job_type)

    print("ppid =",ppid)


    # Query database for Jobs records that are unclosed out on the given processing date.

    jobs_records = dbh.get_unclosedout_jobs_for_processing_date(ppid,proc_date)

    if dbh.exit_code >= 64:
        finalize_procreqs_and_exit(dbh,reqid,-1,dbh.exit_code)


    # Count only Jobs records where awsbatchjobid is not None.
    # Will make software changes elsewhere to ensure this never happens.

    njobs_total = 0

    for jobs_record in jobs_records:

        jid = jobs_record[0]
        awsbatchjobid = jobs_record[1]

        if awsbatchjobid is not None:
            njobs_total += 1

    print("njobs_total =",njobs_total)

    if njobs_total == 0:
        return


    # Initialize iteration number.

    n_iter = 0


    # Define job definitions.    Use AWS Batch Console to set them up once.

    if job_type == "science":
        job_definition = config_input['AWS_BATCH']['job_definition']
    elif job_type == "postproc":
        job_definition = config_input['AWS_BATCH']['postproc_job_definition']
    elif job_type == "refimage":
        job_definition = config_input['AWS_BATCH']['refimage_job_definition']
    else:
        print(f"*** Error: job_type not recognized (job_type={job_type}); quitting...")
        finalize_procreqs_and_exit(dbh,reqid,-1,64)


    # Define job queue.  Use AWS Batch Console to set this up once.

    job_queue = config_input['AWS_BATCH']['job_queue']


    # Get job name base.    Example job name: rapid_postproc_pipeline_20250404_jid997

    if job_type == "science":
        job_name_base = config_input['AWS_BATCH']['job_name_base']
    elif job_type == "postproc":
        job_name_base = config_input['AWS_BATCH']['postproc_job_name_base']
    elif job_type == "refimage":
        job_name_base = config_input['AWS_BATCH']['refimage_job_name_base']
    else:
        print(f"*** Error: job_type not recognized (job_type={job_type}); quitting...")
        finalize_procreqs_and_exit(dbh,reqid,-1,64)


    # Print more parameters.

    print("job_type =",job_type)
    print("job_queue =",job_queue)
    print("job_definition =",job_definition)
    print("job_name_base =",job_name_base)


    # Get Batch.Client object.

    client = boto3.client('batch')

    while True:

        # Get description of jobs.

        n_succeeded = 0
        n_failed = 0
        n_checked = 0

        for jobs_record in jobs_records:

            jid = jobs_record[0]
            awsbatchjobid = jobs_record[1]

            if awsbatchjobid is None:
                continue

            if njobs_total < 3000 or n_checked % 100 == 0:
                print(f"Calling client.describe_jobs for jobs={awsbatchjobid}, n_checked={n_checked}")

            try:
                response = client.describe_jobs(jobs=[awsbatchjobid,])

                if n_checked < 5:
                    print(f"response={response}")

                n_checked += 1

                try:
                    job_status = response['jobs'][0]['status']
                except IndexError as error:
                    print(f'*** Error: IndexError raised because of empty jobs list (e.g., job ID not found or expired) ' +
                          f'running client.describe_jobs (error={error},awsbatchjobid={awsbatchjobid}); quitting...')
                    finalize_procreqs_and_exit(dbh,reqid,-1,64)

                if njobs_total < 3000 or n_checked % 100 == 0:
                    print("job_status =",job_status)

                if job_status == "SUCCEEDED":
                    n_succeeded += 1
                elif job_status == "FAILED":
                    n_failed += 1
                elif job_status == "RUNNABLE":
                    pass
                elif job_status == "STARTING":
                    pass
                elif job_status == "RUNNING":
                    pass
                elif job_status == "SUBMITTED":
                    pass
                elif job_status == "PENDING":
                    pass
                else:
                    print(f"*** Error: Unexpected job_status ({job_status}); quitting...")
                    finalize_procreqs_and_exit(dbh,reqid,-1,64)

            except Exception as error:
                print('*** Error running client.describe_jobs ({}); continuing...'.format(error))


        print(f"n_succeeded,n_failed = {n_succeeded},{n_failed}")

        njobs_succeeded_failed = n_succeeded + n_failed

        print("njobs_succeeded_failed =",njobs_succeeded_failed)

        if njobs_total == njobs_succeeded_failed:
            break

        n_iter += 1
        print(f"From method wait_until_aws_batch_jobs_finished after iteration n_iter={n_iter}: " +\
               "Sleeping 60 seconds and then will check again...")
        time.sleep(60)

    return


#-------------------------------------------------------------------------------------------------------------
# Resubmission of AWS Batch jobs that failed for infrastructure reasons.
#
# A FAILED job is resubmitted only when it failed before or outside the pipeline code: AWS Batch
# reports one of the errors below (e.g., the container image could not be pulled), or the
# container never produced an exit code.  A job whose pipeline exited with its own error code
# (>= 64) is reported but not resubmitted, since it would most likely fail the same way again,
# and neither is a job cancelled or terminated by an operator.
#-------------------------------------------------------------------------------------------------------------

infrastructure_failure_patterns = ("CannotPullContainerError",
                                   "CannotStartContainerError",
                                   "CannotCreateContainerError",
                                   "CannotInspectContainerError",
                                   "ResourceInitializationError",
                                   "DockerTimeoutError",
                                   "Host EC2")


def classify_failed_aws_batch_job(job):

    """
    Decide whether a FAILED AWS Batch job failed for an infrastructure reason, and describe why
    it failed.

    Parameters
    ----------
    job : dict
        One element of the "jobs" list returned by Batch.Client.describe_jobs.

    Returns
    -------
    is_infrastructure_failure : bool
        True if the job should be resubmitted.
    failure_reason : str
        The job's status reason, container reason, and exit code, for logging.
    """

    container = job.get('container') or {}
    attempts = job.get('attempts') or []
    last_attempt_container = (attempts[-1].get('container') or {}) if attempts else {}

    texts = [job.get('statusReason') or '', container.get('reason') or '']

    for attempt in attempts:
        texts.append(attempt.get('statusReason') or '')
        texts.append((attempt.get('container') or {}).get('reason') or '')

    exit_code = last_attempt_container.get('exitCode', container.get('exitCode'))

    failure_reason = (f"statusReason={job.get('statusReason')}, "
                      f"container reason={last_attempt_container.get('reason', container.get('reason'))}, "
                      f"exitCode={exit_code}")

    if job.get('isCancelled') or job.get('isTerminated'):
        return False,failure_reason + " (cancelled or terminated)"

    if any(pattern in text for pattern in infrastructure_failure_patterns for text in texts):
        return True,failure_reason

    if exit_code is None:
        return True,failure_reason + " (container produced no exit code)"

    return False,failure_reason


def resubmit_aws_batch_job(client,job):

    """
    Submit a new AWS Batch job with the same name, queue, job-definition revision, and
    environment variables as a failed one.

    Parameters
    ----------
    client : Batch.Client
        boto3 AWS Batch client.
    job : dict
        The failed job, as returned by Batch.Client.describe_jobs.

    Returns
    -------
    new_aws_batch_job_id : str
        AWS Batch job ID of the resubmitted job.
    """

    environment = [env for env in (job.get('container') or {}).get('environment',[])
                   if not env['name'].startswith('AWS_BATCH_')]

    response = client.submit_job(jobName=job['jobName'],
                                 jobQueue=job['jobQueue'],
                                 jobDefinition=job['jobDefinition'],
                                 containerOverrides={'environment': environment})

    return response['jobId']


def resubmit_failed_aws_batch_jobs(job_type,proc_date,config_input,dbh,reqid,n_retry):

    """
    Resubmit the AWS Batch jobs of a given job type and processing date that failed for an
    infrastructure reason (e.g., CannotPullContainerError), and wait for them to finish, up to
    n_retry times.

    Parameters
    ----------
    job_type : str
        "refimage", "science", or "postproc".
    proc_date : str
        Processing date (yyyy-mm-dd).
    config_input : configparser.ConfigParser
        Parsed awsBatchSubmitJobs_launchSingleSciencePipeline.ini.
    dbh : RAPIDDB
        Open database connection.
    reqid : int
        ProcReqs record of the current processing request.
    n_retry : int
        Maximum number of resubmissions of each job; 0 resubmits nothing.

    Notes
    -----
    Each resubmitted job keeps its Jobs record, whose awsbatchjobid is updated to the new AWS
    Batch job, so that wait_until_aws_batch_jobs_finished and the job-registration scripts
    follow the new job.  Jobs still failed after n_retry resubmissions are left for the
    job-registration scripts to close out as before.
    """

    ppid = look_up_ppid_of_job_type(job_type)

    client = boto3.client('batch')

    for n_try in range(n_retry + 1):


        # Look up the failed jobs among the Jobs records not yet closed out.

        jobs_records = dbh.get_unclosedout_jobs_for_processing_date(ppid,proc_date)

        if dbh.exit_code >= 64:
            finalize_procreqs_and_exit(dbh,reqid,-1,dbh.exit_code)

        jid_of_awsbatchjobid = {jobs_record[1]: jobs_record[0] for jobs_record in jobs_records
                                if jobs_record[1] is not None}

        awsbatchjobids = list(jid_of_awsbatchjobid.keys())

        jobs_to_resubmit = []
        n_failed = 0

        for i in range(0,len(awsbatchjobids),100):        # describe_jobs takes up to 100 job IDs

            try:
                response = client.describe_jobs(jobs=awsbatchjobids[i:i + 100])
            except Exception as error:
                print(f"*** Warning: client.describe_jobs failed ({error}); continuing...")
                continue

            for job in response['jobs']:

                if job['status'] != "FAILED":
                    continue

                n_failed += 1

                jid = jid_of_awsbatchjobid[job['jobId']]

                is_infrastructure_failure,failure_reason = classify_failed_aws_batch_job(job)

                print(f"Failed AWS Batch job: job_type={job_type}, jid={jid}, awsbatchjobid={job['jobId']}, "
                      f"jobName={job['jobName']}: {failure_reason}; "
                      f"{'infrastructure failure' if is_infrastructure_failure else 'not resubmitted'}")

                if is_infrastructure_failure:
                    jobs_to_resubmit.append((jid,job))

        print(f"resubmit_failed_aws_batch_jobs: job_type={job_type}, n_try={n_try}: "
              f"{n_failed} failed jobs, {len(jobs_to_resubmit)} with infrastructure failures")

        if len(jobs_to_resubmit) == 0:
            return

        if n_try == n_retry:
            print(f"*** Warning: {len(jobs_to_resubmit)} {job_type} AWS Batch jobs still failed for infrastructure "
                  f"reasons after {n_retry} resubmissions (jids={[jid for jid,job in jobs_to_resubmit]}); continuing...")
            return


        # Resubmit them, pointing their Jobs records at the new AWS Batch jobs.

        for jid,job in jobs_to_resubmit:

            try:
                new_awsbatchjobid = resubmit_aws_batch_job(client,job)
            except Exception as error:
                print(f"*** Warning: Could not resubmit AWS Batch job (jid={jid},jobName={job['jobName']}) "
                      f"({error}); continuing...")
                continue

            print(f"Resubmitted AWS Batch job (resubmission {n_try + 1} of {n_retry}): jid={jid}, "
                  f"jobName={job['jobName']}, old awsbatchjobid={job['jobId']}, new awsbatchjobid={new_awsbatchjobid}")

            dbh.update_job_with_aws_batch_job_id(jid,new_awsbatchjobid)

            if dbh.exit_code >= 64:
                print(f"*** Error: Could not update Jobs record with resubmitted AWS Batch job ID (jid={jid}); quitting...")
                finalize_procreqs_and_exit(dbh,reqid,-1,dbh.exit_code)


        # Wait for the resubmitted jobs to finish.

        print(f"Waiting until resubmitted AWS Batch jobs have finished for job_type={job_type}, proc_date={proc_date}...")

        wait_until_aws_batch_jobs_finished(job_type,proc_date,config_input,dbh,reqid)



#-------------------------------------------------------------------------------------------------------------
# Open loop: choice of the observation range of each processing request, and sleeping between them.
#-------------------------------------------------------------------------------------------------------------

def compute_open_loop_observation_range(dbh,
                                        min_elapsed_seconds,
                                        max_elapsed_seconds,
                                        settle_seconds,
                                        n_scas = 18):

    """
    Compute the observation range of the next open-loop processing request: from the earliest
    L2File that still needs a difference image, for at most max_elapsed_seconds and ending
    before any exposure that is still arriving, provided the L2Files that still need one in that
    range span at least min_elapsed_seconds.

    Parameters
    ----------
    dbh : RAPIDDB
        Open database connection.
    min_elapsed_seconds : float
        The unprocessed L2Files must span at least this much observation time [s] for a
        processing request to be made.
    max_elapsed_seconds : float
        Maximum length of the observation range [s].
    settle_seconds : float
        An exposure with L2Files for fewer than n_scas SCAs, one of them registered within this
        many seconds, is taken to be still arriving [s].
    n_scas : int, optional
        Number of SCAs in a complete exposure.

    Returns
    -------
    startdatetime, enddatetime : str or None
        Observation range, as the launch scripts use it: dateobs >= startdatetime and
        dateobs < enddatetime.  Both are None when there is nothing to process yet.

    Notes
    -----
    An L2File still needs a difference image when it has vbest > 0 and status > 0 and no
    DiffImages record with vbest > 0.  Only L2Files observed at or after the end of the latest
    processing request that finished normally (ProcReqs status = 1) are considered, so an
    observation range is never covered twice, even if some of its L2Files could not be
    processed (e.g., no reference image for their field); those are left for a processing-date
    run by hand.

    Because of that, an exposure must not be processed while some of its SCAs are still to be
    registered: all its L2Files share one dateobs, so SCAs registered after the request that
    covered it would fall before the high-water mark and never be processed.  The range
    therefore ends before the earliest exposure that is still arriving.  An exposure still
    incomplete after settle_seconds without a new L2File (e.g., an SCA never delivered) is
    processed with the SCAs it has.
    """

    high_water_mark = dbh.get_procreqs_observation_high_water_mark()

    if dbh.exit_code >= 64:
        return None,None

    settling = dbh.get_earliest_settling_exposure(high_water_mark,n_scas,settle_seconds)

    if dbh.exit_code >= 64:
        return None,None

    before = None

    if settling is not None:
        settling_expid,before,settling_nscas,settling_created = settling
        print(f"compute_open_loop_observation_range: exposure expid={settling_expid} (dateobs={before}) "
              f"is still arriving: L2Files for {settling_nscas} of {n_scas} SCAs, latest registered "
              f"{settling_created}; the observation range ends before it")

    record = dbh.get_unprocessed_l2files_observation_range(high_water_mark,before)

    if dbh.exit_code >= 64:
        return None,None

    earliest,latest,n_l2files = record

    print(f"compute_open_loop_observation_range: high_water_mark={high_water_mark}, "
          f"unprocessed L2Files={n_l2files}, earliest dateobs={earliest}, latest dateobs={latest}")

    if n_l2files == 0 or earliest is None:
        if before is None:
            print("compute_open_loop_observation_range: no unprocessed L2Files")
        else:
            print("compute_open_loop_observation_range: no unprocessed L2Files before the exposure "
                  "still arriving; waiting for it")
        return None,None

    span_seconds = (latest - earliest).total_seconds()

    if span_seconds < min_elapsed_seconds:
        print(f"compute_open_loop_observation_range: unprocessed L2Files span {span_seconds} s, "
              f"less than min_elapsed_observation_seconds = {min_elapsed_seconds}; waiting for more data")
        return None,None


    # The range is half-open, so it must end just after the latest L2File it is to include.
    # Postgres timestamps resolve microseconds.

    if span_seconds < max_elapsed_seconds:
        end = latest + timedelta(microseconds=1)
    else:
        end = earliest + timedelta(seconds=max_elapsed_seconds)

    startdatetime = earliest.strftime('%Y-%m-%d %H:%M:%S.%f')
    enddatetime = end.strftime('%Y-%m-%d %H:%M:%S.%f')

    print(f"compute_open_loop_observation_range: startdatetime={startdatetime}, enddatetime={enddatetime}")

    return startdatetime,enddatetime


def sleep_unless_stopped(seconds):

    """
    Sleep for the given number of seconds, returning early if a stop signal arrives.

    Parameters
    ----------
    seconds : float
        Time to sleep [s].
    """

    end = time.time() + seconds

    while istop == 0 and time.time() < end:
        time.sleep(min(10.0, end - time.time()))


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
            print(f"*** Error: Could not open DB connection (dbh.exit_code = {dbh.exit_code}); quitting...")
            exitcode_from_dbh = dbh.exit_code      # Preserve the code, since dbh.close() overwrites it.
            dbh.close()
            exit(exitcode_from_dbh)


        # In the open loop, choose the observation range of this processing request, or sleep and
        # check again if there is not yet enough unprocessed data.

        if datearg is None:

            if istop == 1:
                print("Terminating gracefully before starting a new processing request...")
                dbh.close()
                break

            startdatetime,enddatetime = compute_open_loop_observation_range(dbh,
                                                                            min_elapsed_observation_seconds,
                                                                            max_elapsed_observation_seconds,
                                                                            l2file_settle_seconds,
                                                                            n_scas_per_exposure)

            if dbh.exit_code >= 64:
                print(f"*** Error: Could not compute open-loop observation range (dbh.exit_code = {dbh.exit_code}); quitting...")
                exitcode_from_dbh = dbh.exit_code
                dbh.close()
                exit(exitcode_from_dbh)

            if startdatetime is None:
                dbh.close()
                print(f"Sleeping {open_loop_sleep_seconds} seconds before checking for unprocessed L2Files again...")
                sleep_unless_stopped(open_loop_sleep_seconds)
                continue

            print("Open-loop observation range: startdatetime =",startdatetime,", enddatetime =",enddatetime)


        # Create record in ProcReqs database table.

        reqid = dbh.insert_procreqs_record(startdatetime,enddatetime)

        if dbh.exit_code >= 64:
            print(f"*** Error: Could not insert ProcReqs record (dbh.exit_code = {dbh.exit_code}); quitting...")
            exitcode_from_dbh = dbh.exit_code      # Preserve the code, since dbh.close() overwrites it.
            dbh.close()
            exit(exitcode_from_dbh)


        # Load environment variable PROCREQ to specify the processing request.  All codes
        # launched from here file their S3 objects under "<proc_date>/req<reqid>", so that
        # two processing requests for the same processing date keep separate files.

        os.environ['PROCREQ'] = str(reqid)

        print("reqid =",reqid)


        # Launch reference-image pipelines.

        fname_out = "launch_reference_image_pipelines_code" + "_" + proc_date + ".out"
        launch_reference_image_pipelines_cmd = [python_cmd,
                                                launch_reference_image_pipelines_code]

        exitcode_from_launch_reference_image_pipelines_cmd = util.execute_command(launch_reference_image_pipelines_cmd,fname_out)

        if exitcode_from_launch_reference_image_pipelines_cmd >= 64:
            print(f"*** Error: {launch_reference_image_pipelines_cmd} returned exit code = {exitcode_from_launch_reference_image_pipelines_cmd}; quitting...")
            finalize_procreqs_and_exit(dbh,reqid,-1,64)


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("VPO Elapsed time in seconds to launch reference-image pipelines =",
            end_time_benchmark - start_time_benchmark)
        start_time_benchmark = end_time_benchmark


        # Wait for all reference-image pipelines to complete under AWS Batch.

        job_type = "refimage"

        print(f"Waiting until AWS Batch jobs have finished for job_type={job_type}, proc_date={proc_date}...")

        wait_until_aws_batch_jobs_finished(job_type,proc_date,config_input,dbh,reqid)

        print(f"Okay, all AWS Batch jobs have finished for job_type={job_type}, proc_date={proc_date}...")


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("VPO Elapsed time in seconds to wait for reference-image-pipeline AWS Batch jobs to finish =",
            end_time_benchmark - start_time_benchmark)
        start_time_benchmark = end_time_benchmark


        # Resubmit refimage AWS Batch jobs that failed for infrastructure reasons, such as
        # CannotPullContainerError, and wait for them to finish.

        resubmit_failed_aws_batch_jobs(job_type,proc_date,config_input,dbh,reqid,n_retry_failed_aws_batch_job)


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("VPO Elapsed time in seconds to resubmit failed refimage AWS Batch jobs =",
            end_time_benchmark - start_time_benchmark)
        start_time_benchmark = end_time_benchmark


        # Register metadata from reference-image pipelines into operations database.

        os.environ['MAKEREFIMAGESFLAG'] = "True"

        ppid_refimage = look_up_ppid_of_job_type(job_type)
        print("ppid_refimage =",ppid_refimage)
        os.environ['PIPEID'] = str(ppid_refimage)          # Required by register_reference_image_pipeline_jobs_code, which
                                                           # is dual purposed to handle both reference-image pipeline jobs
                                                           # and science pipeline jobs.

        fname_out = "register_reference_image_pipeline_jobs_code" + "_" + proc_date + ".out"
        register_reference_image_pipeline_jobs_cmd = [python_cmd,
                                                      register_reference_image_pipeline_jobs_code,
                                                      proc_date]

        exitcode_from_register_reference_image_pipeline_jobs_cmd = util.execute_command(register_reference_image_pipeline_jobs_cmd,fname_out)

        if exitcode_from_register_reference_image_pipeline_jobs_cmd >= 64:
            print(f"*** Error: {register_reference_image_pipeline_jobs_cmd} returned exit code = {exitcode_from_register_reference_image_pipeline_jobs_cmd}; quitting...")
            finalize_procreqs_and_exit(dbh,reqid,-1,64)


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("VPO Elapsed time in seconds to register reference-image pipeline metadata into operations database =",
            end_time_benchmark - start_time_benchmark)
        start_time_benchmark = end_time_benchmark


        # Launch science pipelines, which requires that all reference images are generated above.
        # The pipeline launch script requires DRYRUN to False.

        os.environ['DRYRUN'] = "False"


        # Launch science pipelines.
        #
        # Load environment variables STARTDATETIME and ENDDATETIME to specify observation datetimes.

        os.environ['STARTDATETIME'] = startdatetime
        os.environ['ENDDATETIME'] = enddatetime

        fname_out = "launch_science_pipelines_code" + "_" + proc_date + ".out"
        launch_science_pipelines_cmd = [python_cmd,
                                        launch_science_pipelines_code]

        exitcode_from_launch_science_pipelines_cmd = util.execute_command(launch_science_pipelines_cmd,fname_out)

        if exitcode_from_launch_science_pipelines_cmd >= 64:
            print(f"*** Error: {launch_science_pipelines_cmd} returned exit code = {exitcode_from_launch_science_pipelines_cmd}; quitting...")
            finalize_procreqs_and_exit(dbh,reqid,-1,64)


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("VPO Elapsed time in seconds to launch science pipelines =",
            end_time_benchmark - start_time_benchmark)
        start_time_benchmark = end_time_benchmark


        # Wait for all science pipelines to complete under AWS Batch.

        job_type = "science"

        print(f"Waiting until AWS Batch jobs have finished for job_type={job_type}, proc_date={proc_date}...")

        wait_until_aws_batch_jobs_finished(job_type,proc_date,config_input,dbh,reqid)

        print(f"Okay, all AWS Batch jobs have finished for job_type={job_type}, proc_date={proc_date}...")


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("VPO Elapsed time in seconds to wait for science-pipeline AWS Batch jobs to finish =",
            end_time_benchmark - start_time_benchmark)
        start_time_benchmark = end_time_benchmark


        # Resubmit science AWS Batch jobs that failed for infrastructure reasons, such as
        # CannotPullContainerError, and wait for them to finish.

        resubmit_failed_aws_batch_jobs(job_type,proc_date,config_input,dbh,reqid,n_retry_failed_aws_batch_job)


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("VPO Elapsed time in seconds to resubmit failed science AWS Batch jobs =",
            end_time_benchmark - start_time_benchmark)
        start_time_benchmark = end_time_benchmark


        # Register metadata from science pipelines into operations database.

        ppid = look_up_ppid_of_job_type(job_type)
        print("ppid =",ppid)
        os.environ['PIPEID'] = str(ppid)              # Required by register_science_pipeline_jobs_code

        fname_out = "register_science_pipeline_jobs_code" + "_" + proc_date + ".out"
        register_science_pipeline_jobs_cmd = [python_cmd,
                                              register_science_pipeline_jobs_code,
                                              proc_date]

        exitcode_from_register_science_pipeline_jobs_cmd = util.execute_command(register_science_pipeline_jobs_cmd,fname_out)

        if exitcode_from_register_science_pipeline_jobs_cmd >= 64:
            print(f"*** Error: {register_science_pipeline_jobs_cmd} returned exit code = {exitcode_from_register_science_pipeline_jobs_cmd}; quitting...")
            finalize_procreqs_and_exit(dbh,reqid,-1,64)


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("VPO Elapsed time in seconds to register science-pipeline metadata into operations database =",
            end_time_benchmark - start_time_benchmark)
        start_time_benchmark = end_time_benchmark


        # Launch post-processing pipelines.
        #
        # Load environment variable JOBPROCDATE to specify processing date.

        fname_out = "launch_postproc_pipelines_code" + "_" + proc_date + ".out"
        launch_postproc_pipelines_cmd = [python_cmd,
                                        launch_postproc_pipelines_code]

        exitcode_from_launch_postproc_pipelines_cmd = util.execute_command(launch_postproc_pipelines_cmd,fname_out)

        if exitcode_from_launch_postproc_pipelines_cmd >= 64:
            print(f"*** Error: {launch_postproc_pipelines_cmd} returned exit code = {exitcode_from_launch_postproc_pipelines_cmd}; quitting...")
            finalize_procreqs_and_exit(dbh,reqid,-1,64)


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("VPO Elapsed time in seconds after launching postproc pipelines =",
            end_time_benchmark - start_time_benchmark)
        start_time_benchmark = end_time_benchmark


        # Wait for all post-processing pipelines to complete under AWS Batch.

        job_type = "postproc"

        print(f"Waiting until AWS Batch jobs have finished for job_type={job_type}, proc_date={proc_date}...")

        wait_until_aws_batch_jobs_finished(job_type,proc_date,config_input,dbh,reqid)

        print(f"Okay, all AWS Batch jobs have finished for job_type={job_type}, proc_date={proc_date}...")


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("VPO Elapsed time in seconds after waiting for postproc-pipeline AWS Batch jobs to finish =",
            end_time_benchmark - start_time_benchmark)
        start_time_benchmark = end_time_benchmark


        # Resubmit postproc AWS Batch jobs that failed for infrastructure reasons, such as
        # CannotPullContainerError, and wait for them to finish.

        resubmit_failed_aws_batch_jobs(job_type,proc_date,config_input,dbh,reqid,n_retry_failed_aws_batch_job)


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("VPO Elapsed time in seconds to resubmit failed postproc AWS Batch jobs =",
            end_time_benchmark - start_time_benchmark)
        start_time_benchmark = end_time_benchmark


        # Register metadata from post-processing pipelines into operations database.

        fname_out = "register_postproc_pipeline_jobs_code" + "_" + proc_date + ".out"
        register_postproc_pipeline_jobs_cmd = [python_cmd,
                                              register_postproc_pipeline_jobs_code,
                                              proc_date]

        exitcode_from_register_postproc_pipeline_jobs_cmd = util.execute_command(register_postproc_pipeline_jobs_cmd,fname_out)

        if exitcode_from_register_postproc_pipeline_jobs_cmd >= 64:
            print(f"*** Error: {register_postproc_pipeline_jobs_cmd} returned exit code = {exitcode_from_register_postproc_pipeline_jobs_cmd}; quitting...")
            finalize_procreqs_and_exit(dbh,reqid,-1,64)


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

        exitcode_from_load_psfcat_into_db_sources_cmd = util.execute_command(load_psfcat_into_db_sources_cmd,fname_out)

        if exitcode_from_load_psfcat_into_db_sources_cmd >= 64:
            print(f"*** Error: {load_psfcat_into_db_sources_cmd} returned exit code = {exitcode_from_load_psfcat_into_db_sources_cmd}; quitting...")
            finalize_procreqs_and_exit(dbh,reqid,-1,64)


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("VPO Elapsed time in seconds after loading Sources database records =",
            end_time_benchmark - start_time_benchmark)
        start_time_benchmark = end_time_benchmark


        # Launch script to load the SExtractor catalogs for both the positive and
        # negative difference images into the database xsources tables.
        #
        # Environment variable JOBPROCDATE to specify processing date is required.

        fname_out = "load_secat_into_db_xsources_code" + "_" + proc_date + ".out"
        load_secat_into_db_xsources_cmd = [python_cmd,
                                           load_secat_into_db_xsources_code]

        exitcode_from_load_secat_into_db_xsources_cmd = util.execute_command(load_secat_into_db_xsources_cmd,fname_out)

        if exitcode_from_load_secat_into_db_xsources_cmd >= 64:
            print(f"*** Error: {load_secat_into_db_xsources_cmd} returned exit code = {exitcode_from_load_secat_into_db_xsources_cmd}; quitting...")
            finalize_procreqs_and_exit(dbh,reqid,-1,64)


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("VPO Elapsed time in seconds after loading XSources database records =",
            end_time_benchmark - start_time_benchmark)
        start_time_benchmark = end_time_benchmark


        # Launch script to crossmatch sources and astroobjects database tables.
        #
        # Environment variable JOBPROCDATE to specify processing date is required.

        fname_out = "crossmatch_sources_code" + "_" + proc_date + ".out"
        crossmatch_sources_cmd = [python_cmd,
                                  crossmatch_sources_code]

        exitcode_from_crossmatch_sources_cmd = util.execute_command(crossmatch_sources_cmd,fname_out)

        if exitcode_from_crossmatch_sources_cmd >= 64:
            print(f"*** Error: {crossmatch_sources_cmd} returned exit code = {exitcode_from_crossmatch_sources_cmd}; quitting...")
            finalize_procreqs_and_exit(dbh,reqid,-1,64)


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("VPO Elapsed time in seconds after crossmatching Sources and AstroObjects database records =",
            end_time_benchmark - start_time_benchmark)
        start_time_benchmark = end_time_benchmark


        # Launch script to crossmatch xsources and xastroobjects database tables.
        #
        # Environment variable JOBPROCDATE to specify processing date is required.

        fname_out = "crossmatch_xsources_code" + "_" + proc_date + ".out"
        crossmatch_xsources_cmd = [python_cmd,
                                   crossmatch_xsources_code]

        exitcode_from_crossmatch_xsources_cmd = util.execute_command(crossmatch_xsources_cmd,fname_out)

        if exitcode_from_crossmatch_xsources_cmd >= 64:
            print(f"*** Error: {crossmatch_xsources_cmd} returned exit code = {exitcode_from_crossmatch_xsources_cmd}; quitting...")
            finalize_procreqs_and_exit(dbh,reqid,-1,64)


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("VPO Elapsed time in seconds after crossmatching XSources and XAstroObjects database records =",
            end_time_benchmark - start_time_benchmark)
        start_time_benchmark = end_time_benchmark


        # Launch script to compute statistics for astroobjects database tables.
        #
        # Environment variable JOBPROCDATE to specify processing date is required.

        fname_out = "compute_statistics_for_astroobjects_code" + "_" + proc_date + ".out"
        compute_statistics_for_astroobjects_cmd = [python_cmd,
                                                   compute_statistics_for_astroobjects_code]

        exitcode_from_compute_statistics_for_astroobjects_cmd = util.execute_command(compute_statistics_for_astroobjects_cmd,fname_out)

        if exitcode_from_compute_statistics_for_astroobjects_cmd >= 64:
            print(f"*** Error: {compute_statistics_for_astroobjects_cmd} returned exit code = {exitcode_from_compute_statistics_for_astroobjects_cmd}; quitting...")
            finalize_procreqs_and_exit(dbh,reqid,-1,64)


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("VPO Elapsed time in seconds after computing statistics for AstroObjects database records =",
            end_time_benchmark - start_time_benchmark)
        start_time_benchmark = end_time_benchmark


        # Launch script to compute statistics for xastroobjects database tables.
        #
        # Environment variable JOBPROCDATE to specify processing date is required.

        fname_out = "compute_statistics_for_xastroobjects_code" + "_" + proc_date + ".out"
        compute_statistics_for_xastroobjects_cmd = [python_cmd,
                                                    compute_statistics_for_xastroobjects_code]

        exitcode_from_compute_statistics_for_xastroobjects_cmd = util.execute_command(compute_statistics_for_xastroobjects_cmd,fname_out)

        if exitcode_from_compute_statistics_for_xastroobjects_cmd >= 64:
            print(f"*** Error: {compute_statistics_for_xastroobjects_cmd} returned exit code = {exitcode_from_compute_statistics_for_xastroobjects_cmd}; quitting...")
            finalize_procreqs_and_exit(dbh,reqid,-1,64)


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("VPO Elapsed time in seconds after computing statistics for XAstroObjects database records =",
            end_time_benchmark - start_time_benchmark)
        start_time_benchmark = end_time_benchmark


        # Launch script to delete not-best Merges database records.

        fname_out = "prune_notbest_merges_code" + "_" + proc_date + ".out"
        prune_notbest_merges_cmd = [python_cmd,
                                    prune_notbest_merges_code]

        exitcode_from_prune_notbest_merges_cmd = util.execute_command(prune_notbest_merges_cmd,fname_out)

        if exitcode_from_prune_notbest_merges_cmd >= 64:
            print(f"*** Error: {prune_notbest_merges_cmd} returned exit code = {exitcode_from_prune_notbest_merges_cmd}; quitting...")
            finalize_procreqs_and_exit(dbh,reqid,-1,64)


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("VPO Elapsed time in seconds after deleting not-best Merges database records =",
            end_time_benchmark - start_time_benchmark)
        start_time_benchmark = end_time_benchmark


        # Launch script to delete not-best XMerges database records.

        fname_out = "prune_notbest_xmerges_code" + "_" + proc_date + ".out"
        prune_notbest_xmerges_cmd = [python_cmd,
                                     prune_notbest_xmerges_code]

        exitcode_from_prune_notbest_xmerges_cmd = util.execute_command(prune_notbest_xmerges_cmd,fname_out)

        if exitcode_from_prune_notbest_xmerges_cmd >= 64:
            print(f"*** Error: {prune_notbest_xmerges_cmd} returned exit code = {exitcode_from_prune_notbest_xmerges_cmd}; quitting...")
            finalize_procreqs_and_exit(dbh,reqid,-1,64)


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("VPO Elapsed time in seconds after deleting not-best XMerges database records =",
            end_time_benchmark - start_time_benchmark)
        start_time_benchmark = end_time_benchmark


        # Launch script to produce alert packets for all difference images processed on this date.
        # It only reads the database products populated by the preceding stages, so unlike them
        # a failure here is reported but does not abort the processing request.
        #
        # Environment variable JOBPROCDATE to specify processing date is required.

        fname_out = "produce_alerts_code" + "_" + proc_date + ".out"
        produce_alerts_cmd = [python_cmd,
                              produce_alerts_code]

        exitcode_from_produce_alerts_cmd = util.execute_command(produce_alerts_cmd,fname_out)

        if exitcode_from_produce_alerts_cmd >= 64:
            print(f"*** Error: {produce_alerts_cmd} returned exit code = {exitcode_from_produce_alerts_cmd}; continuing...")
        elif exitcode_from_produce_alerts_cmd > 0:
            print(f"*** Warning: {produce_alerts_cmd} returned exit code = {exitcode_from_produce_alerts_cmd}; continuing...")


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("VPO Elapsed time in seconds after producing alerts =",
            end_time_benchmark - start_time_benchmark)
        start_time_benchmark = end_time_benchmark


        # Close out record in ProcReqs database table with status = 1.

        dbh.finalize_procreqs_record(reqid,1)

        if dbh.exit_code >= 64:
            print(f"*** Error: Could not finalize ProcReqs record (reqid={reqid}); continuing...")
            dbh.exit_code = 0            # The pipelines themselves all succeeded, so a failure to
                                         # close out the bookkeeping record is not fatal here.  Clear
                                         # the code so it is not mistaken for a dbh.close() failure below.


        # Close database connection.

        dbh.close()

        if dbh.exit_code >= 64:
            exit(dbh.exit_code)


        # Break out of open loop if running the VPO for just one specific processing date.

        if datearg is not None:
            print(f"Terminating normally since this VPO run is just for one specific processing date: datearg={datearg}...")
            break


        # Open loop: go on to the next processing request, unless asked to stop.

        if istop == 1:
            print("Terminating gracefully after completing the processing request...")
            break

        i += 1
        print(f"Open loop: completed processing request reqid={reqid}; starting iteration i = {i}...")


        #
        # End of open loop.
        #




    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("VPO Elapsed total time in seconds to run VPO =",
        end_time_benchmark - start_time_benchmark_at_start)


    # Termination.

    print("Terminating: exitcode =",exitcode)

    exit(exitcode)
