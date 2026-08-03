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

import modules.utils.rapid_pipeline_subs as util
import database.modules.utils.rapid_db as db


swname = "virtualPipelineOperator.py"
swvers = "1.2"
cfg_filename_only = "awsBatchSubmitJobs_launchSingleSciencePipeline.ini"


# Specify python command to use for executing Python scripts.

python_cmd = '/usr/bin/python3.11'
launch_science_pipelines_code = '/code/pipeline/launchSciencePipelinesForDateTimeRangeWithRefImageWindow.py'
register_science_pipeline_jobs_code = '/code/pipeline/parallelRegisterCompletedJobsInDB.py'
launch_postproc_pipelines_code = '/code/pipeline/awsBatchSubmitJobs_launchPostProcPipelinesForProcDate.py'
register_postproc_pipeline_jobs_code = '/code/pipeline/parallelRegisterCompletedJobsInDBAfterPostProc.py'
load_psfcat_into_db_sources_code = '/code/pipeline/loadPSFCatIntoDBSourcesTable.py'
crossmatch_sources_code = '/code/pipeline/crossMatchSources.py'
compute_statistics_for_astroobjects_code = '/code/pipeline/computeStatisticsForAstroObjects.py'
prune_notbest_merges_code = '/code/pipeline/pruneNotBestMerges.py'
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
print("launch_science_pipelines_code =", launch_science_pipelines_code)
print("register_science_pipeline_jobs_code =", register_science_pipeline_jobs_code)
print("launch_postproc_pipelines_code =", launch_postproc_pipelines_code)
print("register_postproc_pipeline_jobs_code =", register_postproc_pipeline_jobs_code)


# Set signal hander.

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGQUIT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


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
# Method to wait until common set of AWS Batch jobs have finished.
#-------------------------------------------------------------------------------------------------------------

def wait_until_aws_batch_jobs_finished(job_type,proc_date,config_input,dbh):

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
        dbh.close()
        exit(dbh.exit_code)


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
        dbh.close()
        exit(64)


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
        exit(64)


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
                    dbh.close()
                    exit(64)

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
                    dbh.close()
                    exit(64)

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

        fname_out = "launch_reference_image_pipelines_code" + "_" + proc_date + ".out"
        launch_reference_image_pipelines_cmd = [python_cmd,
                                                launch_reference_image_pipelines_code]

        exitcode_from_launch_reference_image_pipelines_cmd = util.execute_command(launch_reference_image_pipelines_cmd,fname_out)

        if exitcode_from_launch_reference_image_pipelines_cmd >= 64:
            print(f"*** Error: {launch_reference_image_pipelines_cmd} returned exit code = {exitcode_from_launch_reference_image_pipelines_cmd}; quitting...")
            dbh.close()
            exit(64)


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("VPO Elapsed time in seconds to launch reference-image pipelines =",
            end_time_benchmark - start_time_benchmark)
        start_time_benchmark = end_time_benchmark


        # Wait for all reference-image pipelines to complete under AWS Batch.

        job_type = "refimage"

        print(f"Waiting until AWS Batch jobs have finished for job_type={job_type}, proc_date={proc_date}...")

        wait_until_aws_batch_jobs_finished(job_type,proc_date,config_input,dbh)

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
            dbh.close()
            exit(64)


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


            # The pipeline launch script requires MAKEREFIMAGESFLAG set in the environment.
            # Also, set required DRYRUN to False.

            os.environ['MAKEREFIMAGESFLAG'] = make_refimages_flag
            os.environ['DRYRUN'] = "False"


            # Launch science pipelines.
            #
            # Load environment variables STARTDATETIME and ENDDATETIME to specify observation datetimes.

            os.environ['STARTDATETIME'] = startdatetime
            os.environ['ENDDATETIME'] = enddatetime

            fname_out = "launch_science_pipelines_code" + "_" + stage_label + "_" + proc_date + ".out"
            launch_science_pipelines_cmd = [python_cmd,
                                            launch_science_pipelines_code]

            exitcode_from_launch_science_pipelines_cmd = util.execute_command(launch_science_pipelines_cmd,fname_out)

            if exitcode_from_launch_science_pipelines_cmd >= 64:
                print(f"*** Error: {launch_science_pipelines_cmd} returned exit code = {exitcode_from_launch_science_pipelines_cmd}; quitting...")
                dbh.close()
                exit(64)


            # Code-timing benchmark.

            end_time_benchmark = time.time()
            print("VPO Elapsed time in seconds to launch science pipelines =",
                end_time_benchmark - start_time_benchmark)
            start_time_benchmark = end_time_benchmark


            # Wait for all science pipelines to complete under AWS Batch.

            job_type = "science"

            print(f"Waiting until AWS Batch jobs have finished for job_type={job_type}, proc_date={proc_date}, stage_label={stage_label}...")

            wait_until_aws_batch_jobs_finished(job_type,proc_date,config_input,dbh)

            print(f"Okay, all AWS Batch jobs have finished for job_type={job_type}, proc_date={proc_date}, stage_label={stage_label}...")


            # Code-timing benchmark.

            end_time_benchmark = time.time()
            print("VPO Elapsed time in seconds to wait for science-pipeline AWS Batch jobs to finish =",
                end_time_benchmark - start_time_benchmark)
            start_time_benchmark = end_time_benchmark


            # Register metadata from science pipelines into operations database.

            ppid = look_up_ppid_of_job_type(job_type)
            print("ppid =",ppid)
            os.environ['PIPEID'] = str(ppid)              # Required by register_science_pipeline_jobs_code

            fname_out = "register_science_pipeline_jobs_code" + "_" + stage_label + "_" + proc_date + ".out"
            register_science_pipeline_jobs_cmd = [python_cmd,
                                                  register_science_pipeline_jobs_code,
                                                  proc_date]

            exitcode_from_register_science_pipeline_jobs_cmd = util.execute_command(register_science_pipeline_jobs_cmd,fname_out)

            if exitcode_from_register_science_pipeline_jobs_cmd >= 64:
                print(f"*** Error: {register_science_pipeline_jobs_cmd} returned exit code = {exitcode_from_register_science_pipeline_jobs_cmd}; quitting...")
                dbh.close()
                exit(64)


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

        fname_out = "launch_postproc_pipelines_code" + "_" + proc_date + ".out"
        launch_postproc_pipelines_cmd = [python_cmd,
                                        launch_postproc_pipelines_code]

        exitcode_from_launch_postproc_pipelines_cmd = util.execute_command(launch_postproc_pipelines_cmd,fname_out)

        if exitcode_from_launch_postproc_pipelines_cmd >= 64:
            print(f"*** Error: {launch_postproc_pipelines_cmd} returned exit code = {exitcode_from_launch_postproc_pipelines_cmd}; quitting...")
            dbh.close()
            exit(64)


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("VPO Elapsed time in seconds after launching postproc pipelines =",
            end_time_benchmark - start_time_benchmark)
        start_time_benchmark = end_time_benchmark


        # Wait for all post-processing pipelines to complete under AWS Batch.

        job_type = "postproc"

        print(f"Waiting until AWS Batch jobs have finished for job_type={job_type}, proc_date={proc_date}...")

        wait_until_aws_batch_jobs_finished(job_type,proc_date,config_input,dbh)

        print(f"Okay, all AWS Batch jobs have finished for job_type={job_type}, proc_date={proc_date}...")


        # Code-timing benchmark.

        end_time_benchmark = time.time()
        print("VPO Elapsed time in seconds after waiting for postproc-pipeline AWS Batch jobs to finish =",
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
            dbh.close()
            exit(64)


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

        exitcode_from_crossmatch_sources_cmd = util.execute_command(crossmatch_sources_cmd,fname_out)

        if exitcode_from_crossmatch_sources_cmd >= 64:
            print(f"*** Error: {crossmatch_sources_cmd} returned exit code = {exitcode_from_crossmatch_sources_cmd}; quitting...")
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

        exitcode_from_compute_statistics_for_astroobjects_cmd = util.execute_command(compute_statistics_for_astroobjects_cmd,fname_out)

        if exitcode_from_compute_statistics_for_astroobjects_cmd >= 64:
            print(f"*** Error: {compute_statistics_for_astroobjects_cmd} returned exit code = {exitcode_from_compute_statistics_for_astroobjects_cmd}; quitting...")
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

        exitcode_from_prune_notbest_merges_cmd = util.execute_command(prune_notbest_merges_cmd,fname_out)

        if exitcode_from_prune_notbest_merges_cmd >= 64:
            print(f"*** Error: {prune_notbest_merges_cmd} returned exit code = {exitcode_from_prune_notbest_merges_cmd}; quitting...")
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
