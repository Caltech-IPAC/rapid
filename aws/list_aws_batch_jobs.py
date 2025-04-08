import os
import time
import boto3
import configparser

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

if job_status_to_list is None:

    print("*** Error: Env. var. JOBSTATUS not set (either science or postproc); quitting...")
    exit(64)

flag = False

for choice in job_status_choices:

    if job_status_to_list == choice:
        flag = True
        break

if not flag:

    print(f"*** Error: Env. var. JOBSTATUS not one of the choices ({job_status_to_list}); quitting...")
    exit(64)


# JOBPROCDATE of pipeline job.

proc_date = os.getenv('JOBPROCDATE')

if proc_date is None:

    print("*** Error: Env. var. JOBPROCDATE not set; quitting...")
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
njobs = 0
nsucceeded = 0

job_name_wildcard = job_name_base + '*'

response = client.list_jobs(jobQueue=job_queue,
                            maxResults=100,
                            filters=[
                                        {
                                            'name': 'JOB_NAME',
                                            'values': [
                                                          job_name_wildcard,
                                                      ]
                                        },
                                   ],
                               )

for job in response['jobSummaryList']:
    job_name = job['jobName']
    if proc_date in job_name:
        job_status = job['status']
        print("job_name,job_status =",job_name,job_status)
        if job_status == job_status_to_list:
            nsucceeded += 1
        njobs += 1

next_token = response['nextToken']

print("page = ",page)

page += 1

while True:

    response = client.list_jobs(jobQueue=job_queue,
                                maxResults=100,
                                nextToken=next_token,
                                filters=[
                                             {
                                                 'name': 'JOB_NAME',
                                                 'values': [
                                                               job_name_wildcard,
                                                           ]
                                             },
                                        ],
                               )

    for job in response['jobSummaryList']:
        job_name = job['jobName']
        if proc_date in job_name:
            job_status = job['status']
            print("job_name,job_status =",job_name,job_status)
            if job_status == job_status_to_list:
                nsucceeded += 1
            njobs += 1

    # print("response = ",response)
    # print("next_token = ",next_token)

    print("page = ",page)

    page += 1

    try:
        next_token = response['nextToken']
    except:
        break

print("njobs =",njobs)
print("nsucceeded =",nsucceeded)


# Terminate.

print("Terminating normally...")

exit(0)
