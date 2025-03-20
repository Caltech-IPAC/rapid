"""
The purpose of the post-processing pipeline is to add database IDs
to products already made by the science pipeline.  This is run only
after the products have been registered in the RAPID operations database.
"""


import boto3
import os
import configparser
from datetime import datetime, timezone
from dateutil import tz
import time

to_zone = tz.gettz('America/Los_Angeles')

import database.modules.utils.rapid_db as db
import modules.utils.rapid_pipeline_subs as util

swname = "awsBatchSubmitJobs_launchSinglePostProcPipeline.py"
swvers = "1.0"

print("swname =", swname)
print("swvers =", swvers)


# Compute start time for benchmark.

start_time_benchmark = time.time()


# Compute processing datetime (UT) and processing datetime (Pacific time).

datetime_utc_now = datetime.utcnow()
proc_utc_datetime = datetime_utc_now.strftime('%Y-%m-%dT%H:%M:%SZ')
datetime_pt_now = datetime_utc_now.replace(tzinfo=timezone.utc).astimezone(tz=to_zone)
proc_pt_datetime_started = datetime_pt_now.strftime('%Y-%m-%dT%H:%M:%S PT')

print("proc_utc_datetime =",proc_utc_datetime)
print("proc_pt_datetime_started =",proc_pt_datetime_started)


# JOBPROCDATE of pipeline job.

proc_date = os.getenv('JOBPROCDATE')

if proc_date is None:

    print("*** Error: Env. var. JOBPROCDATE not set; quitting...")
    exit(64)


# RAPID_JOB_ID of pipeline job.  In this case, it is the jid under which the science pipeline already ran.

jid = os.getenv('RAPID_JOB_ID')

if jid is None:

    print("*** Error: Env. var. RAPID_JOB_ID not set; quitting...")
    exit(64)

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

job_config_ini_file_filename = job_config_filename_base + str(jid) + ".ini"
job_config_ini_file_s3_bucket_object_name = proc_date + "/" + job_config_ini_file_filename

job_prefix = proc_date + '/jid' + str(jid) + '/'


# Set up AWS Batch.

client = boto3.client('batch')


# Define job definition.  Use AWS Batch Console to set this up once.

job_definition = config_input['AWS_BATCH']['postproc_job_definition']


# Define job queue.  Use AWS Batch Console to set this up once.

job_queue = config_input['AWS_BATCH']['job_queue']


# Define job name.

job_name_base = config_input['AWS_BATCH']['postproc_job_name_base']


# Print out basic information for log file.

print("proc_date =",proc_date)
print("jid =",jid)
print("job_info_s3_bucket_base =",job_info_s3_bucket_base)
print("job_logs_s3_bucket_base =",job_logs_s3_bucket_base)
print("product_s3_bucket_base =",product_s3_bucket_base)
print("job_config_ini_file_filename =",job_config_ini_file_filename)
print("job_config_ini_file_s3_bucket_object_name =",job_config_ini_file_s3_bucket_object_name)
print("job_prefix =",job_prefix)


#-------------------------------------------------------------------------------------------------------------
# Method to submit a job to AWS Batch.
#-------------------------------------------------------------------------------------------------------------


def submit_job_to_aws_batch(proc_date,
                            jid,
                            job_info_s3_bucket,
                            job_config_ini_file_filename,
                            job_config_ini_file_s3_bucket_object_name,
                            input_images_csv_filename,
                            input_images_csv_file_s3_bucket_object_name):

    print("proc_date =",proc_date)
    print("jid =",jid)
    print("job_info_s3_bucket =",job_info_s3_bucket)
    print("job_config_ini_file_s3_bucket_object_name =",job_config_ini_file_s3_bucket_object_name)
    print("input_images_csv_file_s3_bucket_object_name =",input_images_csv_file_s3_bucket_object_name)


    # Submit single job.

    job_name = job_name_base + proc_date + "_jid" + str(jid)

    print("Submitting job to AWS Batch...")

    response = client.submit_job(
        jobName=job_name,
        jobQueue=job_queue,
        jobDefinition=job_definition,
        containerOverrides={
            'environment': [
                {
                    'name': 'JOBPROCDATE',
                    'value': proc_date
                },
                {
                    'name': 'RAPID_JOB_ID',
                    'value': str(jid)
                },
                {
                    'name': 'JOBS3BUCKET',
                    'value': job_info_s3_bucket
                },
                {
                    'name': 'JOBCONFIGFILENAME',
                    'value': job_config_ini_file_filename
                },
                {
                    'name': 'JOBCONFIGOBJNAME',
                    'value': job_config_ini_file_s3_bucket_object_name
                }
            ]
        }
    )

    print("response =",response)


#-------------------------------------------------------------------------------------------------------------
# Main program.
#-------------------------------------------------------------------------------------------------------------


if __name__ == '__main__':


    # Initialize termination exitcode to normal termination.
    # If detected during post-processing, an abnormal termination exitcode will be set.

    terminating_exitcode = 0


    # Get S3 resource.

    s3_resource = boto3.resource('s3')


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Query database for rid.

    db_rec_dict = dbh.get_info_for_job(jid)
    ppid = db_rec_dict["ppid"]
    rid = db_rec_dict["rid"]
    rfid = db_rec_dict["rfid"]
    expid = db_rec_dict["expid"]
    sca = db_rec_dict["sca"]
    field = db_rec_dict["field"]
    fid = db_rec_dict["fid"]
    started = db_rec_dict["started"]
    ended = db_rec_dict["ended"]
    status = int(db_rec_dict["status"])
    job_exitcode = int(db_rec_dict["job_exitcode"])


    # Close database connection.

    dbh.close()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Do not launch AWS Batch job only if status != 1 and job_exitcode >= 64.

    if status != 1 or job_exitcode >= 64:

        print(f"*** Warning: Science-pipeline job did not finish normally (jid={jid}); quitting...")
        terminating_exitcode = 33


    # Populate config-file dictionary for job.

    job_config_ini_file_filename = job_config_filename_base + str(jid) + ".ini"
    job_config_ini_file = rapid_work + "/" + job_config_ini_file_filename
    job_info_s3_bucket = job_info_s3_bucket_base
    job_config_ini_file_s3_bucket_object_name = proc_date + "/" + job_config_ini_file_filename

    job_config = configparser.ConfigParser()

    job_config['JOB_PARAMS'] = {'debug': str(debug),

                         'swname': swname,

                         'swvers': swvers,
                         'jid': str(jid)}

    job_config['JOB_PARAMS']['job_info_s3_bucket_base'] = job_info_s3_bucket_base
    job_config['JOB_PARAMS']['product_s3_bucket_base'] = product_s3_bucket_base
    job_config['JOB_PARAMS']['verbose'] = str(verbose)
    job_config['JOB_PARAMS']['ppid'] = str(ppid)
    job_config['JOB_PARAMS']['rid'] = str(rid)
    job_config['JOB_PARAMS']['rfid'] = str(rfid)
    job_config['JOB_PARAMS']['expid'] = str(expid)
    job_config['JOB_PARAMS']['fid'] = str(fid)
    job_config['JOB_PARAMS']['field'] = str(field)
    job_config['JOB_PARAMS']['sca'] = str(sca)


    # Write output config file for job.

    with open(job_config_ini_file, 'w') as job_configfile:

        job_configfile.write("#" + "\n")
        job_configfile.write("# s3://" + job_info_s3_bucket + "/" + job_config_ini_file_s3_bucket_object_name + "\n")
        job_configfile.write("#" + "\n")
        job_configfile.write("# " + proc_utc_datetime + "\n")
        job_configfile.write("#" + "\n")
        job_configfile.write("# Machine-generated by " + swname + "\n")
        job_configfile.write("#" + "\n")
        job_configfile.write("\n")

        job_config.write(job_configfile)


    # Upload output config file for job, along with associated file(s) if any, to S3 bucket.

    s3_client = boto3.client('s3')

    uploaded_to_bucket = True

    try:
        response = s3_client.upload_file(job_config_ini_file,
                                         job_info_s3_bucket,
                                         job_config_ini_file_s3_bucket_object_name)
    except ClientError as e:
        print("*** Error: Failed to upload {} to s3://{}/{}"\
            .format(job_config_ini_file,job_info_s3_bucket,job_config_ini_file_s3_bucket_object_name))
        uploaded_to_bucket = False

    if uploaded_to_bucket:
        print("Successfully uploaded {} to s3://{}/{}"\
            .format(job_config_ini_file,job_info_s3_bucket,job_config_ini_file_s3_bucket_object_name))


    #
    # Launch a post-processing pipeline.
    #

"""
    submit_job_to_aws_batch(proc_date,
                            jid,
                            job_info_s3_bucket,
                            job_config_ini_file_filename,
                            job_config_ini_file_s3_bucket_object_name,
                            input_images_csv_filename,
                            input_images_csv_file_s3_bucket_object_name)
"""

    print(f"Launching AWS Batch post-processing job for jid={jid}, proc_date={proc_date}")


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to launch pipelines =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Termination.

    print("terminating_exitcode =",terminating_exitcode)

    exit(terminating_exitcode)
