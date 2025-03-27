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

import modules.utils.rapid_pipeline_subs as plsubs
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
postproc_job_config_filename_base = config_input['JOB_PARAMS']['postproc_job_config_filename_base']
postproc_product_config_filename_base = config_input['JOB_PARAMS']['postproc_product_config_filename_base']
awaicgen_output_mosaic_image_file = config_input['AWAICGEN']['awaicgen_output_mosaic_image_file']
zogy_output_diffimage_file = config_input['ZOGY']['zogy_output_diffimage_file']


#-------------------------------------------------------------------------------------------------------------
# Main program.
#-------------------------------------------------------------------------------------------------------------

if __name__ == '__main__':


    s3_resource = boto3.resource('s3')
    s3_client = boto3.client('s3')

    exitcode = 0


    # Examine log files for given processing date.

    logs_bucket = s3_resource.Bucket(job_logs_s3_bucket_base)

    njobs = 0
    log_filenames = []
    jids = []

    for logs_bucket_object in logs_bucket.objects.all():

        input_file = logs_bucket_object.key

        if datearg not in input_file:
            continue

        if verbose > 0:
            print(logs_bucket_object.key)


        # Match proc_date/jid S3-object prefixes.

        filename_match = re.match(r"(\d\d\d\d\d\d\d\d)/(rapid_postproc_job_.+jid(\d+)_log\.txt)",input_file)

        try:
            subdir_only = filename_match.group(1)
            filename_only = filename_match.group(2)
            jid = filename_match.group(3)

            if subdir_only == datearg:

                print("-----0-----> subdir_only =",subdir_only)
                print("-----1-----> filename_only =",filename_only)
                print("-----2-----> jid =",jid)

                log_filenames.append(filename_only)
                jids.append(jid)

                njobs += 1

        except:
            if debug > 0:
                print("-----2-----> No match in",input_file)


    print("End of logs S3 bucket listing...")
    print("njobs = ",njobs)


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)



    #####################################################################
    # Loop over jobs for a given processing date.
    #####################################################################

    for jid,log_fname in zip(jids,log_filenames):

        print("\nStart of loop: jid, log_fname =",jid,log_fname)

        job_exitcode = 64
        aws_batch_job_id = 'not_found'


        # Check whether post-processing done file exists in S3 bucket for job, and skip if it exists.
        # This is done by attempting to download the done file.  Regardless the sub
        # always returns the filename and subdirs by parsing the s3_full_name.

        s3_full_name_done_file = "s3://" + product_s3_bucket_base + "/" + datearg + '/jid' + str(jid) + "/postproc_jid" +  str(jid)  + ".done"
        done_filename,subdirs_done,downloaded_from_bucket = plsubs.download_file_from_s3_bucket(s3_client,s3_full_name_done_file)

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

        infobits = int(job_config_input['SKY_TILE']['infobits'])

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

        print("Try downloading s3://{}/{} into {}...".format(product_s3_bucket_base,s3_bucket_object_name,product_config_ini_filename))

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

            jid_post_proc = product_config_input['JOB_PARAMS']['jid_post_proc']
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

        except ClientError as e:
            print("*** Warning: Failed to download {} from s3://{}/{}"\
                .format(product_config_ini_filename,product_s3_bucket_base,s3_bucket_object_name))
            downloaded_from_bucket = False



        print("For Jobs records: ended =",ended)


        # Update Jobs record.

        dbh.end_job(jid_post_proc,job_exitcode,aws_batch_job_id,ended)

        if dbh.exit_code >= 64:
            exit(dbh.exit_code)


        #####################################################################
        # Done with loop over jobs for a given processing date.
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



