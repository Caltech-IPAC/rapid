"""
This is a separate version of registerCompletedJobsInDBAfterPostProc.py that
registers database records in parallel according to number of
available cores on the job-launcher machine.

This script is to be run after the post-processing pipeline,
in order to update the checksums of the reference-image and
difference-image products in the RAPID operations database.
This pipeline is a skeleton for later additional functions,
such as copying products to other locations, such as MAST.

Input the processing date of interest on the command line
in the format YYYYMMDD, and set PROCREQ to the reqid of the
processing request.  The database is queried for the
science-pipeline jobs of that processing request that ran
normally, using a query of the following form:

select jid from jobs
where ppid = 15
and status > 0
and exitcode <= 32
and reqid = 123;

To indicate that post-processing pipeline ran normally, a done file
is written to the S3 bucket at the location of the products for the job.
For example:

$ aws s3 ls --recursive  s3://rapid-product-files/20250326/jid1/ | grep postproc
2025-03-28 08:32:07          0 20250326/jid1/postproc_job_config_jid1.done
"""


import sys
import os
import configparser
import boto3
import re
from botocore.exceptions import ClientError
from datetime import datetime, timezone
from dateutil import tz
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

to_zone = tz.gettz('America/Los_Angeles')

import modules.utils.rapid_pipeline_subs as util
import database.modules.utils.rapid_db as db


swname = "parallelRegisterCompletedJobsInDBAfterPostProc.py"
swvers = "1.1"
cfg_filename_only = "awsBatchSubmitJobs_launchSingleSciencePipeline.ini"


print("swname =", swname)
print("swvers =", swvers)
print("cfg_filename_only =", cfg_filename_only)


# Compute start time for benchmark.

start_time_benchmark = time.time()


# Compute processing datetime (UT) and processing datetime (Pacific time).

datetime_utc_now = datetime.utcnow()
proc_utc_datetime = datetime_utc_now.strftime('%Y-%m-%dT%H:%M:%SZ')
datetime_pt_now = datetime_utc_now.replace(tzinfo=timezone.utc).astimezone(tz=to_zone)
proc_pt_datetime_started = datetime_pt_now.strftime('%Y-%m-%dT%H:%M:%S PT')

print("proc_utc_datetime =",proc_utc_datetime)
print("proc_pt_datetime_started =",proc_pt_datetime_started)


# Get processing date of interest from command-line argument.

datearg = (sys.argv)[1]

print("datearg =",datearg)


# PROCREQ of the processing request.  This is the reqid of the ProcReqs database
# record that the VPO creates, and it subdivides the processing date in the S3
# buckets, so that two processing requests for the same processing date do not
# overwrite each other's files.

proc_req = os.getenv('PROCREQ')

if proc_req is None:

    print("*** Error: Env. var. PROCREQ not set; quitting...")
    exit(64)

print("proc_req =",proc_req)


# S3 key prefix that this processing request files all of its objects under.

proc_subdir = util.get_proc_subdir(datearg,proc_req)


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
postproc_job_config_filename_base = config_input['JOB_PARAMS']['postproc_job_config_filename_base']
postproc_product_config_filename_base = config_input['JOB_PARAMS']['postproc_product_config_filename_base']
awaicgen_output_mosaic_image_file = config_input['AWAICGEN']['awaicgen_output_mosaic_image_file']


# Open database connections for parallel access.

num_cores = os.cpu_count()

dbh_list = []

for i in range(num_cores):

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)

    dbh_list.append(dbh)


#-------------------------------------------------------------------------------------------------------------
# Custom methods for parallel processing, taking advantage of multiple cores on the job-launcher machine.
#-------------------------------------------------------------------------------------------------------------

def run_single_core_job(jids,log_fnames,index_thread):

    njobs = len(jids)

    print("index_thread,njobs =",index_thread,njobs)

    thread_work_file = swname.replace(".py","_thread") + str(index_thread) + ".out"

    try:
        fh = open(thread_work_file, 'w', encoding="utf-8")
    except:
        error_msg = f"*** Error: Could not open output file {thread_work_file}; quitting..."
        print(error_msg)
        raise Exception(error_msg)

    dbh = dbh_list[index_thread]

    fh.write(f"\nStart of run_single_core_job: index_thread={index_thread}, dbh={dbh}\n")


    #####################################################################
    # Loop over jobs for a given processing date.  In a given thread, only
    # process those jobs associated with the thread by the remainder function.
    #####################################################################

    for index_job in range(njobs):

        index_core = index_job % num_cores
        if index_thread != index_core:
            continue

        jid = jids[index_job]
        log_fname = log_fnames[index_job]

        fh.write(f"Loop start: index_job,jid,log_fname = {index_job},{jid},{log_fname}\n")

        job_exitcode = 64
        aws_batch_job_id = 'not_found'


        # Check whether science-pipeline done file exists in S3 bucket for job, and skip if it does NOT exist.
        # This is done by attempting to download the done file.  Regardless the sub
        # always returns the filename and subdirs by parsing the s3_full_name.

        s3_full_name_science_pipeline_done_file = \
            "s3://" + product_s3_bucket_base + "/" + proc_subdir + '/jid' + str(jid) + "/" + \
            job_config_filename_base +  str(jid)  + ".done"
        science_pipeline_done_filename,subdirs_done,downloaded_from_bucket = \
            util.download_file_from_s3_bucket(s3_client,
                                              s3_full_name_science_pipeline_done_file)

        if not downloaded_from_bucket:
            fh.write("*** Warning: Science-pipeline done file does NOT exist ({}); skipping...\n".format(science_pipeline_done_filename))
            continue


        # Check whether post-processing done file exists in S3 bucket for job, and skip if it exists.
        # This is done by attempting to download the done file.  Regardless the sub
        # always returns the filename and subdirs by parsing the s3_full_name.

        s3_full_name_done_file = \
            "s3://" + product_s3_bucket_base + "/" + proc_subdir + '/jid' + str(jid) + "/" + \
            postproc_job_config_filename_base +  str(jid)  + ".done"
        done_filename,subdirs_done,downloaded_from_bucket = util.download_file_from_s3_bucket(s3_client,s3_full_name_done_file)

        if downloaded_from_bucket:
            fh.write("*** Warning: Done file exists ({}); skipping...\n".format(done_filename))
            continue


        # Download log file from S3 bucket.
        # Skip to next job if the log file does not exist (e.g., the AWS Batch job never ran),
        # rather than letting the exception terminate this thread's entire loop over jobs.

        s3_bucket_object_name = proc_subdir + '/' + log_fname

        fh.write("Downloading s3://{}/{} into {}...\n".format(job_logs_s3_bucket_base,s3_bucket_object_name,log_fname))

        try:
            response = s3_client.download_file(job_logs_s3_bucket_base,s3_bucket_object_name,log_fname)

        except ClientError as e:
            fh.write("*** Warning: Failed to download {} from s3://{}/{} ({}); skipping...\n"\
                .format(log_fname,job_logs_s3_bucket_base,s3_bucket_object_name,e))
            fh.flush()
            continue

        fh.write(f"response = {response}\n")


        # Download job config file, in order to harvest some of its metadata.
        # Skip to next job if the job config file does not exist.

        job_config_ini_filename = postproc_job_config_filename_base + str(jid) + ".ini"

        s3_bucket_object_name = proc_subdir + '/' + job_config_ini_filename

        fh.write("Downloading s3://{}/{} into {}...\n".format(job_info_s3_bucket_base,s3_bucket_object_name,job_config_ini_filename))

        try:
            response = s3_client.download_file(job_info_s3_bucket_base,s3_bucket_object_name,job_config_ini_filename)

        except ClientError as e:
            fh.write("*** Warning: Failed to download {} from s3://{}/{} ({}); skipping...\n"\
                .format(job_config_ini_filename,job_info_s3_bucket_base,s3_bucket_object_name,e))
            fh.flush()
            continue

        fh.write(f"response = {response}\n")


        # Harvest job metadata from job config file

        job_config_input = configparser.ConfigParser()
        job_config_input.read(job_config_ini_filename)

        infobitssci = int(job_config_input['DIFF_IMAGE']['infobitssci'])

        infobits = int(job_config_input['REF_IMAGE']['infobits'])

        fh.write(f"infobitssci,infobits = {infobitssci},{infobits}\n")


        # Grep log file for aws_batch_job_id and terminating_exitcode.

        file = open(log_fname, "r")
        search_string1 = "aws_batch_job_id"
        search_string2 = "terminating_exitcode"

        for line in file:
            if re.search(search_string1, line):
                line = line.rstrip("\n")
                fh.write(line + "\n")
                tokens = re.split(r'\s*=\s*',line)
                aws_batch_job_id = tokens[1]
            elif re.search(search_string2, line):
                line = line.rstrip("\n")
                fh.write(line + "\n")
                tokens = re.split(r'\s*=\s*',line)
                job_exitcode = tokens[1]

        file.close()

        # Try to download product config file, in order to harvest some of its metadata.
        # This may be unsuccessful if the pipeline failed.

        product_config_ini_filename = postproc_product_config_filename_base + str(jid) + ".ini"

        s3_bucket_object_name = proc_subdir + '/' + product_config_ini_filename

        fh.write("Try downloading s3://{}/{} into {}...\n".format(product_s3_bucket_base,
                                                             s3_bucket_object_name,
                                                             product_config_ini_filename))

        try:
            response = s3_client.download_file(product_s3_bucket_base,s3_bucket_object_name,product_config_ini_filename)

            fh.write(f"response = {response}\n")
            downloaded_from_bucket = True


            # Read input parameters from product config *.ini file.

            product_config_input_filename = product_config_ini_filename
            product_config_input = configparser.ConfigParser()
            product_config_input.read(product_config_input_filename)


            # Get the timestamps of when the job started and ended on the AWS Batch machine,
            # which have already been converted to Pacific Time.

            jid_post_proc = product_config_input['JOB_PARAMS']['jid_postproc']
            job_started = product_config_input['JOB_PARAMS']['job_started']
            job_ended = product_config_input['JOB_PARAMS']['job_ended']

            fh.write(f"jid_post_proc = {jid_post_proc}\n")
            fh.write(f"job_started = {job_started}\n")
            fh.write(f"job_ended = {job_ended}\n")

            string_match = re.match(r"(.+?)T(.+?) PT", job_started)

            try:
                started_date = string_match.group(1)
                started_time = string_match.group(2)
                fh.write("started = {} {}\n".format(started_date,started_time))

            except:
                error_msg = f"*** Error: Could not parse job_started ({job_started}) for jid={jid}; quitting..."
                fh.write(error_msg + "\n")
                fh.flush()
                print(error_msg)
                raise Exception(error_msg)

            started = started_date + " " + started_time

            string_match = re.match(r"(.+?)T(.+?) PT", job_ended)

            try:
                ended_date = string_match.group(1)
                ended_time = string_match.group(2)
                fh.write("ended = {} {}\n".format(ended_date,ended_time))

            except:
                error_msg = f"*** Error: Could not parse job_ended ({job_ended}) for jid={jid}; quitting..."
                fh.write(error_msg + "\n")
                fh.flush()
                print(error_msg)
                raise Exception(error_msg)

            ended = ended_date + " " + ended_time


            # Read in reference-image metadata to update checksums.
            # Only update record if rfid is not equal to "None".

            rfid = product_config_input['REF_IMAGE']['rfid']

            fh.write(f"rfid = {rfid}\n")

            if rfid != "None":

                refimage_filename = product_config_input['REF_IMAGE']['refimage_filename']
                refimage_file_version = product_config_input['REF_IMAGE']['refimage_file_version']
                refimage_file_checksum = product_config_input['REF_IMAGE']['refimage_file_checksum']
                fh.write(f"refimage_filename = {refimage_filename}\n")
                fh.write(f"refimage_file_version = {refimage_file_version}\n")
                fh.write(f"refimage_file_checksum = {refimage_file_checksum}\n")


                # Update record in RefImages database table.

                refimage_status = 1
                dbh.update_refimage(rfid,refimage_filename,refimage_file_checksum,refimage_status,refimage_file_version)

                if dbh.exit_code >= 64:
                    error_msg = f"*** Error: dbh.update_refimage returned exit code {dbh.exit_code} for jid={jid}; quitting..."
                    fh.write(error_msg + "\n")
                    fh.flush()
                    print(error_msg)
                    raise Exception(error_msg)


            # Read in difference-image metadata to update checksums.
            # Only update record if pid is not equal to "None".

            pid = product_config_input['DIFF_IMAGE']['pid']

            fh.write(f"pid = {pid}\n")

            if pid != "None":


                diffimage_filename = product_config_input['DIFF_IMAGE']['diffimage_filename']
                diffimage_file_version = product_config_input['DIFF_IMAGE']['diffimage_file_version']
                diffimage_file_checksum = product_config_input['DIFF_IMAGE']['diffimage_file_checksum']
                fh.write(f"diffimage_filename = {diffimage_filename}\n")
                fh.write(f"diffimage_file_version = {diffimage_file_version}\n")
                fh.write(f"diffimage_file_checksum = {diffimage_file_checksum}\n")


                # Update record in DiffImages database table.

                diffimage_status = 1
                dbh.update_diffimage(pid,diffimage_filename,diffimage_file_checksum,diffimage_status,diffimage_file_version)

                if dbh.exit_code >= 64:
                    error_msg = f"*** Error: dbh.update_diffimage returned exit code {dbh.exit_code} for jid={jid}; quitting..."
                    fh.write(error_msg + "\n")
                    fh.flush()
                    print(error_msg)
                    raise Exception(error_msg)

        except ClientError as e:
            fh.write("*** Warning: Failed to download {} from s3://{}/{} ({}); skipping...\n"\
                .format(product_config_ini_filename,product_s3_bucket_base,s3_bucket_object_name,e))
            fh.flush()


            # Skip to next job, since jid_post_proc, started, and ended are only available
            # from the product config file (otherwise end_job would be called with values
            # left over from the previous job).

            continue


        # Update Jobs record.

        fh.write(f"For Jobs record: jid_post_proc,job_exitcode,aws_batch_job_id,started,ended = " +\
                 f"{jid_post_proc},{job_exitcode},{aws_batch_job_id},{started},{ended}\n")

        dbh.end_job(jid_post_proc,job_exitcode,aws_batch_job_id,started,ended)

        if dbh.exit_code >= 64:
            error_msg = f"*** Error: dbh.end_job returned exit code {dbh.exit_code} " +\
                        f"(jid={jid},jid_post_proc={jid_post_proc},job_exitcode={job_exitcode},aws_batch_job_id={aws_batch_job_id},ended={ended}); quitting..."
            fh.write(error_msg + "\n")
            fh.flush()
            print(error_msg)
            raise Exception(error_msg)


        # Touch done file.  Upload done file to S3 bucket.

        util.write_done_file_to_s3_bucket(done_filename,product_s3_bucket_base,proc_subdir,jid,s3_client)


        #####################################################################
        # End of loop over jobs for a given processing date.
        #####################################################################

        fh.write(f"Loop end: done_filename,product_s3_bucket_base,datearg,jid = {done_filename},{product_s3_bucket_base},{datearg},{jid}\n")


        # Flush write buffer.

        fh.flush()


        # End of loop over job ID.


    fh.write(f"\nEnd of run_single_core_job: index_thread={index_thread}\n")

    fh.close()


def execute_parallel_processes(jids,log_filenames,num_cores=None):

    if num_cores is None:
        num_cores = os.cpu_count()  # Use all available cores if not specified

    print("num_cores =",num_cores)

    with ProcessPoolExecutor(max_workers=num_cores) as executor:
        # Submit all tasks to the executor and store the futures in a list
        futures = [executor.submit(run_single_core_job,jids,log_filenames,thread_index) for thread_index in range(num_cores)]

        # Iterate over completed futures and update progress
        for i, future in enumerate(as_completed(futures)):
            index = futures.index(future)  # Find the original index/order of the completed future
            print(f"Completed: {i+1} processes, lastly for index={index}")

    # Return the number of threads that terminated with an exception.

    nfailed = 0

    for future in futures:
        index = futures.index(future)
        try:
            print(future.result())
        except Exception as e:
            print(f"*** Error in thread index {index} = {e}")
            nfailed += 1

    return nfailed


#-------------------------------------------------------------------------------------------------------------
# Main program.
#-------------------------------------------------------------------------------------------------------------

if __name__ == '__main__':


    s3_client = boto3.client('s3')

    exitcode = 0


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Query database for Jobs records that ended on the given processing date and ran normally.

    db_jids = dbh.get_jids_of_normal_science_pipeline_jobs_for_processing_date(datearg,proc_req)

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    njobs = 0
    log_filenames = []
    jids = []

    for db_jid in db_jids:

        print("db_jid =",db_jid)

        log_filename_only = "rapid_postproc_job_" + datearg + "_jid" + str(db_jid) + "_log.txt"

        print("log_filename_only =",log_filename_only)

        log_filenames.append(log_filename_only)
        jids.append(db_jid)

        njobs += 1


    print("njobs = ",njobs)


    ############################################################################
    # Execute job-closeout tasks for all science-pipeline jobs with jids on a
    # given processing date.  The execution for each job is done in parallel,
    # taking advantage of multiple cores on the job-launcher machine.
    ############################################################################

    nfailed = execute_parallel_processes(jids,log_filenames)

    if nfailed > 0:
        print(f"*** Error: {nfailed} of {num_cores} threads failed; setting exitcode = 64")
        exitcode = 64


    # Close database connection.

    dbh.close()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to register database records =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Termination.

    print("Terminating: exitcode =",exitcode)

    exit(exitcode)



