import os
import time
import boto3
import configparser

import modules.utils.rapid_pipeline_subs as util


swname = "list_aws_batch_jobs.py"
swvers = "1.0"
cfg_filename_only = "awsBatchSubmitJobs_launchSingleSciencePipeline.ini"


# Read AWS environment

aws_access_key_id = os.getenv('AWS_ACCESS_KEY_ID')
aws_secret_access_key = os.getenv('AWS_SECRET_ACCESS_KEY')

if aws_access_key_id is None:

    print("*** Error: Env. var. AWS_ACCESS_KEY_ID not set; quitting...")
    exit(64)

if aws_secret_access_key is None:

    print("*** Error: Env. var. AWS_SECRET_ACCESS_KEY not set; quitting...")
    exit(64)


# JOBTYPE of pipeline job.  Either science or postproc.

job_type = os.getenv('JOBTYPE')

if job_type is None:

    print("*** Error: Env. var. JOBTYPE not set (either science or postproc); quitting...")
    exit(64)

if not (job_type == "science" or job_type == "postproc"):

    print("*** Error: Env. var. JOBTYPE not either science or postproc; quitting...")
    exit(64)


# JOBSTATUS of pipeline job.  Possible values are
# 'SUBMITTED','PENDING','RUNNABLE','STARTING','RUNNING','SUCCEEDED','FAILED'

job_status_choices = ['SUBMITTED','PENDING','RUNNABLE','STARTING','RUNNING','SUCCEEDED','FAILED']

job_status_to_list = os.getenv('JOBSTATUS')

flag = False

for choice in job_status_choices:

    if job_status_to_list == choice:
        flag = True
        break

if not flag:
    job_status_to_list = "ALL"
    print(f"*** Message: Env. var. JOBSTATUS not one of the singular choices ({job_status_to_list}), so list all possibilities {job_status_choices}; continuing...")

# JOBPROCDATE of pipeline job.

proc_date = os.getenv('JOBPROCDATE')

if proc_date is None:
    print("*** Message: Env. var. JOBPROCDATE not set, so will list jobs for processing dates; continuing...")
else:
    print(f"*** Message: Env. var. JOBPROCDATE is set, so will list jobs for {proc_date}; continuing...")


# RAPID_SW is /code inside container, but full path outside.

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


# Print parameters.

print("job_name_base =",job_name_base)
print("job_type =",job_type)
print("job_queue =",job_queue)
print("job_definition =",job_definition)
print("job_name_base =",job_name_base)


# Get Batch.Client object.

client = boto3.client('batch')


# Get list of jobs.
# The client.list_jobs method must be called the first time without the nextToken parameter.

"""
Response Syntax:
{
    'jobSummaryList': [
        {
            'jobArn': 'string',
            'jobId': 'string',
            'jobName': 'string',
            'createdAt': 123,
            'status': 'SUBMITTED'|'PENDING'|'RUNNABLE'|'STARTING'|'RUNNING'|'SUCCEEDED'|'FAILED',
            'statusReason': 'string',
            'startedAt': 123,
            'stoppedAt': 123,
            'container': {
                'exitCode': 123,
                'reason': 'string'
            },
            'arrayProperties': {
                'size': 123,
                'index': 123
            },
            'nodeProperties': {
                'isMainNode': True|False,
                'numNodes': 123,
                'nodeIndex': 123
            },
            'jobDefinition': 'string'
        },
    ],
    'nextToken': 'string'
}
"""

page = 1
njobs_total = 0
if job_status_to_list != "ALL":
    njobs_with_specified_status = 0
else:
    njobs_vs_status = {}
    for job_status in job_status_choices:
        njobs_vs_status[job_status] = 0

job_name_wildcard = job_name_base + '*'


# Set next_token = None the first time util.list_aws_batch_jobs
# is called, so that proper behavior is handled by the method.

next_token = None
response = util.list_aws_batch_jobs(client,next_token,job_queue,job_name_wildcard)

for job in response['jobSummaryList']:
    job_name = job['jobName']
    if proc_date is None or proc_date in job_name:
        job_status = job['status']
        print("job_name,job_status =",job_name,job_status)
        if job_status_to_list != "ALL" and job_status == job_status_to_list:
            njobs_with_specified_status += 1
        else:
            njobs_vs_status[job_status] += 1

        njobs_total += 1

next_token = response['nextToken']

print("page = ",page)

page += 1

while True:

    response = util.list_aws_batch_jobs(client,next_token,job_queue,job_name_wildcard)

    for job in response['jobSummaryList']:
        job_name = job['jobName']
        if proc_date is None or proc_date in job_name:
            job_status = job['status']
            print("job_name,job_status =",job_name,job_status)
            if job_status_to_list != "ALL" and job_status == job_status_to_list:
                njobs_with_specified_status += 1
            else:
                njobs_vs_status[job_status] += 1
            njobs_total += 1

    # print("response = ",response)
    # print("next_token = ",next_token)

    print("page = ",page)

    page += 1

    try:
        next_token = response['nextToken']
    except:
        break

print("njobs_total =",njobs_total)
print("job_status_to_list =",job_status_to_list)
if job_status_to_list != "ALL":
    print(f"njobs_{job_status} = {njobs_with_specified_status}")
else:
    for job_status in job_status_choices:
        print(f"njobs_{job_status} = {njobs_vs_status[job_status]}")

# Terminate.

print("Terminating normally...")

exit(0)
