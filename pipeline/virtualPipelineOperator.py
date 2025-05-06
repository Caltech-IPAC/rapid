"""
Virtual Pipeline Operator (VPO) for the Rapid Pipeline Operations.

To be executed inside a RAPID-pipeline Docker container.
"""


import sys
import os
import signal
import configparser
import boto3
import re
from datetime import datetime, timezone
from dateutil import tz
import time

to_zone = tz.gettz('America/Los_Angeles')

import modules.utils.rapid_pipeline_subs as util
import database.modules.utils.rapid_db as db


swname = "virtualPipelineOperator.py"
swvers = "1.0"
cfg_filename_only = "awsBatchSubmitJobs_launchSingleSciencePipeline.ini"


# Specify python command to use for executing Python scripts.

python_cmd = '/usr/bin/python3.11'
launch_science_pipelines_code = '/code/pipeline/awsBatchSubmitJobs_launchSciencePipelinesForDateTimeRange.py'
register_science_pipeline_jobs_code = '/code/pipeline/parallelRegisterCompletedJobsInDB.py'
launch_postproc_pipelines_code = '/code/pipeline/awsBatchSubmitJobs_launchPostProcPipelinesForProcDate.py'
register_postproc_pipeline_jobs_code = '/code/pipeline/parallelRegisterCompletedJobsInDBAfterPostProc.py'


# Print diagnostics.

print("swname =", swname)
print("swvers =", swvers)
print("cfg_filename_only =", cfg_filename_only)
print("python_cmd =", python_cmd)
print("launch_science_pipelines_code =", launch_science_pipelines_code)
print("register_science_pipeline_jobs_code =", register_science_pipeline_jobs_code)
print("launch_postproc_pipelines_code =", launch_postproc_pipelines_code)
print("register_postproc_pipeline_jobs_code =", register_postproc_pipeline_jobs_code)


# Compute start time for benchmark.

start_time_benchmark = time.time()
start_time_benchmark_at_start = start_time_benchmark


# Compute processing datetime (UT) and processing datetime (Pacific time).

datetime_utc_now = datetime.utcnow()
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


# Other required environment variables.

aws_access_key_id = os.getenv('AWS_ACCESS_KEY_ID')
aws_secret_access_key = os.getenv('AWS_SECRET_ACCESS_KEY')

if aws_access_key_id is None:

    print("*** Error: Env. var. AWS_ACCESS_KEY_ID not set; quitting...")
    exit(64)

if aws_secret_access_key is None:

    print("*** Error: Env. var. AWS_SECRET_ACCESS_KEY not set; quitting...")
    exit(64)


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


# Set signal hander.

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGQUIT, signal_handler)


#-------------------------------------------------------------------------------------------------------------
# Method to wait until common set of AWS Batch jobs have finished.
#-------------------------------------------------------------------------------------------------------------

def wait_until_aws_batch_jobs_finished(job_type,proc_date,config_input,dbh):

    """"
    Wait until AWS Batch jobs of a given job type and processing date have finished.
    """

    print("Parameter values from method wait_until_aws_batch_jobs_finished:")
    print("job_type =",job_type)
    print("proc_date =",proc_date)

    if job_type == "science":
        ppid = 15
    elif job_type == "postproc":
        ppid = 17
    else:
        print(f"Job type undefined ({job_type}); quitting")
        exit(64)

    print("ppid =",ppid)


    # Query database for Jobs records that are unclosed out on the given processing date.

    jobs_records = dbh.get_unclosedout_jobs_for_processing_date(ppid,proc_date)

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)

    njobs_total = len(jobs_records)
    print("njobs_total =",njobs_total)

    if njobs_total == 0:
        return


    # Initialize iteration number.

    iter = 0


    # Define job definition.

    if job_type == "science":
        job_definition = config_input['AWS_BATCH']['job_definition']
    elif job_type == "postproc":
        job_definition = config_input['AWS_BATCH']['postproc_job_definition']
    else:
        print(f"*** Error: job_type not recognized (job_type={job_type}); quitting...")
        exit(64)


    # Define job queue.  Use AWS Batch Console to set this up once.

    job_queue = config_input['AWS_BATCH']['job_queue']


    # Get job name base.    Example job name: rapid_postproc_pipeline_20250404_jid997

    if job_type == "science":
        job_name_base = config_input['AWS_BATCH']['job_name_base']
    elif job_type == "postproc":
        job_name_base = config_input['AWS_BATCH']['postproc_job_name_base']
    else:
        print(f"*** Error: job_name_base not recognized (job_name_base={job_name_base}); quitting...")
        exit(64)


    # Print more parameters.

    print("job_name_base =",job_name_base)
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

            if njobs_total < 3000 or n_checked % 100 == 0:
                print(f"Calling client.describe_jobs for jobs={awsbatchjobid}, n_checked={n_checked}")

            response = client.describe_jobs(jobs=[awsbatchjobid,])

            if n_checked < 5:
                print(f"response={response}")

            n_checked += 1

            job_status = response['jobs'][0]['status']

            if njobs_total < 3000 or n_checked % 100 == 0:
                print("job_status =",job_status)

            if job_status == "SUCCEEDED":
                n_succeeded += 1
            elif job_status == "FAILED":
                n_failed += 1
            elif job_status == "RUNNABLE":
                break
            elif job_status == "STARTING":
                break
            elif job_status == "RUNNING":
                break

        njobs_succeeded_failed = n_succeeded + n_failed

        print("njobs_succeeded_failed =",njobs_succeeded_failed)

        if njobs_total == njobs_succeeded_failed:
            break

        iter += 1
        print(f"From method wait_until_aws_batch_jobs_finished after iteration iter={iter}: " +\
               "Sleeping 60 seconds and then will check again...")
        time.sleep(60)

    return


#-------------------------------------------------------------------------------------------------------------
# Main program.
#-------------------------------------------------------------------------------------------------------------

if __name__ == '__main__':


    # Open loop.

    s3_client = boto3.client('s3')

    exitcode = 0

    i = 0

    while True:


        # Get current date and time.

        datetime_utc_now = datetime.utcnow()
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


        # Open database connection.

        dbh = db.RAPIDDB()

        if dbh.exit_code >= 64:
            exit(dbh.exit_code)


        # Launch science pipelines.
        #
        # Load environment variables STARTDATETIME and ENDDATETIME to specify observation datetimes.

        os.environ['STARTDATETIME'] = startdatetime
        os.environ['ENDDATETIME'] = enddatetime

        fname_out = "launch_science_pipelines_code" + "_" + proc_date + ".out"
        launch_science_pipelines_cmd = [python_cmd,
                                        launch_science_pipelines_code]

        ashexitcode_from_launch_science_pipelines_cmd = util.execute_command(launch_science_pipelines_cmd,fname_out)


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("Elapsed time in seconds after launching science pipelines =",
            end_time_benchmark - start_time_benchmark)
        start_time_benchmark = end_time_benchmark


        # Wait for all science pipelines to complete under AWS Batch.

        job_type = "science"

        print(f"Waiting until AWS Batch jobs have finished for job_type={job_type}, proc_date={proc_date}...")

        wait_until_aws_batch_jobs_finished(job_type,proc_date,config_input,dbh)

        print(f"Okay, all AWS Batch jobs have finished for job_type={job_type}, proc_date={proc_date}...")


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("Elapsed time in seconds after waiting for science-pipeline AWS Batch jobs to finish =",
            end_time_benchmark - start_time_benchmark)
        start_time_benchmark = end_time_benchmark


        # Register metadata from science pipelines into operations database.

        fname_out = "register_science_pipeline_jobs_code" + "_" + proc_date + ".out"
        register_science_pipeline_jobs_cmd = [python_cmd,
                                              register_science_pipeline_jobs_code,
                                              proc_date]

        exitcode_from_register_science_pipeline_jobs_cmd = util.execute_command(register_science_pipeline_jobs_cmd,fname_out)


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("Elapsed time in seconds after registering science-pipeline metadata into operations database =",
            end_time_benchmark - start_time_benchmark)
        start_time_benchmark = end_time_benchmark


        # Launch post-processing pipelines.
        #
        # Load environment variable JOBPROCDATE to specify processing date.

        os.environ['JOBPROCDATE'] = proc_date

        fname_out = "launch_postproc_pipelines_code" + "_" + proc_date + ".out"
        launch_postproc_pipelines_cmd = [python_cmd,
                                        launch_postproc_pipelines_code]

        exitcode_from_launch_postproc_pipelines_cmd = util.execute_command(launch_postproc_pipelines_cmd,fname_out)


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("Elapsed time in seconds after launching postproc pipelines =",
            end_time_benchmark - start_time_benchmark)
        start_time_benchmark = end_time_benchmark


        # Wait for all post-processing pipelines to complete under AWS Batch.

        job_type = "postproc"

        print(f"Waiting until AWS Batch jobs have finished for job_type={job_type}, proc_date={proc_date}...")

        wait_until_aws_batch_jobs_finished(job_type,proc_date,config_input,dbh)

        print(f"Okay, all AWS Batch jobs have finished for job_type={job_type}, proc_date={proc_date}...")


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("Elapsed time in seconds after waiting for postproc-pipeline AWS Batch jobs to finish =",
            end_time_benchmark - start_time_benchmark)
        start_time_benchmark = end_time_benchmark


        # Register metadata from post-processing pipelines into operations database.

        fname_out = "register_postproc_pipeline_jobs_code" + "_" + proc_date + ".out"
        register_postproc_pipeline_jobs_cmd = [python_cmd,
                                              register_postproc_pipeline_jobs_code,
                                              proc_date]

        exitcode_from_register_postproc_pipeline_jobs_cmd = util.execute_command(register_postproc_pipeline_jobs_cmd,fname_out)


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("Elapsed time in seconds after registering postproc-pipeline metadata into operations database =",
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


        # Test code.

        a = 1
        for n in range(10):
            a = a + 1
            print("n,a =",n,a)

        print("Sleeping 30 seconds...")
        time.sleep(30)
        print("Waking up...")

        if i == 3:
            #os.kill(os.getpid(), signal.SIGQUIT)          # Quits gracefully via signal handler upon receiving control-/
            #os.kill(os.getpid(), signal.SIGINT)           # Quits gracefully via signal handler upon receiving control-c
            os.kill(os.getpid(), signal.SIGSTOP)           # Quits unconditionally and immediately.

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
    print("Elapsed time in seconds to run VPO =",
        end_time_benchmark - start_time_benchmark_at_start)


    # Termination.

    print("Terminating: exitcode =",exitcode)

    exit(exitcode)

