"""
This script is to be run after the post-processing pipeline,
in order to update the checksums of the reference-image and
difference-image products in the RAPID operations database.
This pipeline is a skeleton for later additional functions,
such as copying products to other locations, such as MAST.

Input the processing date of interest on the command line
in the format YYYYMMDD.  The database is queried for
science-pipeline jobs on that date that ran normally, using
a query of the following form:

select * from jobs
where ppid=15
and ended >= '20250326'
and ended < '20250326' + '1 day'
and status > 0
and exitcode <= 32
order by ended;

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

to_zone = tz.gettz('America/Los_Angeles')

import modules.utils.rapid_pipeline_subs as util
import database.modules.utils.rapid_db as db


swname = "registerCompletedJobInDBAfterPostProc.py"
swvers = "1.0"
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
zogy_output_diffimage_file = config_input['ZOGY']['zogy_output_diffimage_file']


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

    db_jids = dbh.get_jids_of_normal_science_pipeline_jobs_for_processing_date(datearg)

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


    #####################################################################
    # Loop over jobs for a given processing date.
    #####################################################################

    for jid,log_fname in zip(jids,log_filenames):

        print("\nStart of loop: jid, log_fname =",jid,log_fname)

        job_exitcode = 64
        aws_batch_job_id = 'not_found'


        # Check whether science-pipeline done file exists in S3 bucket for job, and skip if it does NOT exist.
        # This is done by attempting to download the done file.  Regardless the sub
        # always returns the filename and subdirs by parsing the s3_full_name.

        s3_full_name_science_pipeline_done_file = \
            "s3://" + product_s3_bucket_base + "/" + datearg + '/jid' + str(jid) + "/" + \
            job_config_filename_base +  str(jid)  + ".done"
        science_pipeline_done_filename,subdirs_done,downloaded_from_bucket = \
            util.download_file_from_s3_bucket(s3_client,
                                              s3_full_name_science_pipeline_done_file)

        if not downloaded_from_bucket:
            print("*** Warning: Science-pipeline done file does NOT exist ({}); skipping...".format(done_filename))
            continue


        # Check whether post-processing done file exists in S3 bucket for job, and skip if it exists.
        # This is done by attempting to download the done file.  Regardless the sub
        # always returns the filename and subdirs by parsing the s3_full_name.

        s3_full_name_done_file = \
            "s3://" + product_s3_bucket_base + "/" + datearg + '/jid' + str(jid) + "/" + \
            postproc_job_config_filename_base +  str(jid)  + ".done"
        done_filename,subdirs_done,downloaded_from_bucket = util.download_file_from_s3_bucket(s3_client,s3_full_name_done_file)

        if downloaded_from_bucket:
            print("*** Warning: Done file exists ({}); skipping...".format(done_filename))
            continue


        # Download log file from S3 bucket.

        s3_bucket_object_name = datearg + '/' + log_fname

        print("Downloading s3://{}/{} into {}...".format(job_logs_s3_bucket_base,s3_bucket_object_name,log_fname))

        response = s3_client.download_file(job_logs_s3_bucket_base,s3_bucket_object_name,log_fname)

        print("response =",response)


        # Download job config file, in order to harvest some of its metadata.

        job_config_ini_filename = postproc_job_config_filename_base + str(jid) + ".ini"

        s3_bucket_object_name = datearg + '/' + job_config_ini_filename

        print("Downloading s3://{}/{} into {}...".format(job_info_s3_bucket_base,s3_bucket_object_name,job_config_ini_filename))

        response = s3_client.download_file(job_info_s3_bucket_base,s3_bucket_object_name,job_config_ini_filename)

        print("response =",response)


        # Harvest job metadata from job config file

        job_config_input = configparser.ConfigParser()
        job_config_input.read(job_config_ini_filename)

        infobitssci = int(job_config_input['DIFF_IMAGE']['infobitssci'])

        infobits = int(job_config_input['REF_IMAGE']['infobits'])

        print("infobitssci,infobits =",infobitssci,infobits)


        # Grep log file for aws_batch_job_id and terminating_exitcode.

        file = open(log_fname, "r")
        search_string1 = "aws_batch_job_id"
        search_string2 = "terminating_exitcode"

        for line in file:
            if re.search(search_string1, line):
                line = line.rstrip("\n")
                print(line)
                tokens = re.split(r'\s*=\s*',line)
                aws_batch_job_id = tokens[1]
            elif re.search(search_string2, line):
                line = line.rstrip("\n")
                print(line)
                tokens = re.split(r'\s*=\s*',line)
                job_exitcode = tokens[1]


        # Try to download product config file, in order to harvest some of its metadata.
        # This may be unsuccessful if the pipeline failed.

        product_config_ini_filename = postproc_product_config_filename_base + str(jid) + ".ini"

        s3_bucket_object_name = datearg + '/' + product_config_ini_filename

        print("Try downloading s3://{}/{} into {}...".format(product_s3_bucket_base,
                                                             s3_bucket_object_name,
                                                             product_config_ini_filename))

        try:
            response = s3_client.download_file(product_s3_bucket_base,s3_bucket_object_name,product_config_ini_filename)

            print("response =",response)
            downloaded_from_bucket = True


            # Read input parameters from product config *.ini file.

            product_config_input_filename = product_config_ini_filename
            product_config_input = configparser.ConfigParser()
            product_config_input.read(product_config_input_filename)


            # Get the timestamps of when the job started and ended on the AWS Batch machine,
            # which have already been converted to Pacific Time.
            # In the Jobs record of the RAPID pipeline operations database, we will use for
            # job started the time the pipeline instance was launched (which was when the
            # Jobs record was initially inserted).

            jid_post_proc = product_config_input['JOB_PARAMS']['jid_postproc']
            job_started = product_config_input['JOB_PARAMS']['job_started']
            job_ended = product_config_input['JOB_PARAMS']['job_ended']

            print("jid_post_proc =",jid_post_proc)
            print("job_started =",job_started)
            print("job_ended =",job_ended)

            string_match = re.match(r"(.+?)T(.+?) PT", job_ended)

            try:
                ended_date = string_match.group(1)
                ended_time = string_match.group(2)
                print("ended = {} {}".format(ended_date,ended_time))

            except:
                print("*** Error: Could not parse proc_pt_datetime_ended; quitting...")
                exit(64)

            ended = ended_date + " " + ended_time


            # Read in reference-image metadata to update checksums.
            # Only update record if rfid is not equal to "None".

            rfid = product_config_input['REF_IMAGE']['rfid']

            print("rfid =",rfid)

            if rfid != "None":

                refimage_filename = product_config_input['REF_IMAGE']['refimage_filename']
                refimage_file_version = product_config_input['REF_IMAGE']['refimage_file_version']
                refimage_file_checksum = product_config_input['REF_IMAGE']['refimage_file_checksum']
                print("refimage_filename =",refimage_filename)
                print("refimage_file_version =",refimage_file_version)
                print("refimage_file_checksum =",refimage_file_checksum)


                # Update record in RefImages database table.

                refimage_status = 1
                dbh.update_refimage(rfid,refimage_filename,refimage_file_checksum,refimage_status,refimage_file_version)

                if dbh.exit_code >= 64:
                    exit(dbh.exit_code)


            # Read in difference-image metadata to update checksums.
            # Only update record if pid is not equal to "None".

            pid = product_config_input['DIFF_IMAGE']['pid']

            print("pid =",pid)

            if pid != "None":


                diffimage_filename = product_config_input['DIFF_IMAGE']['diffimage_filename']
                diffimage_file_version = product_config_input['DIFF_IMAGE']['diffimage_file_version']
                diffimage_file_checksum = product_config_input['DIFF_IMAGE']['diffimage_file_checksum']
                print("diffimage_filename =",diffimage_filename)
                print("diffimage_file_version =",diffimage_file_version)
                print("diffimage_file_checksum =",diffimage_file_checksum)


                # Update record in DiffImages database table.

                diffimage_status = 1
                dbh.update_diffimage(pid,diffimage_filename,diffimage_file_checksum,diffimage_status,diffimage_file_version)

                if dbh.exit_code >= 64:
                    exit(dbh.exit_code)

        except ClientError as e:
            print("*** Warning: Failed to download {} from s3://{}/{}"\
                .format(product_config_ini_filename,product_s3_bucket_base,s3_bucket_object_name))
            downloaded_from_bucket = False


        # Update Jobs record.

        print("For Jobs record: ended =",ended)

        dbh.end_job(jid_post_proc,job_exitcode,aws_batch_job_id,ended)

        if dbh.exit_code >= 64:
            exit(dbh.exit_code)


        # Touch done file.  Upload done file to S3 bucket.

        util.write_done_file_to_s3_bucket(done_filename,product_s3_bucket_base,datearg,jid,s3_client)


        #####################################################################
        # End of loop over jobs for a given processing date.
        #####################################################################


    # Close database connection.

    dbh.close()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to launch pipelines =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Termination.

    print("Terminating: exitcode =",exitcode)

    exit(exitcode)



