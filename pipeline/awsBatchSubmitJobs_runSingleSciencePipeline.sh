#!/bin/bash -x

var=$1

echo "[" ${var+x} "]"


date
echo "Args: $@"

# Test if var is unset (undefined).

if [ -z ${var+x} ]; then
    echo "input var is unset; resetting to 4...";
    var=4
else
    echo "input var is set to '$var'";
fi

# Test if var is blank (empty string).

if [ -z "$var" ]; then
    echo "input var is blank; resetting to 2...";
    var=2;
fi

env
echo "AWS Batch job to run single RAPID pipeline on a science image."


logfile="rapid_pipeline_job_${JOBPROCDATE}_jid${RAPID_JOB_ID}_log.txt"
echo "logfile = $logfile"

# S3 subdirectory of the processing request, so that two processing requests for
# the same processing date do not overwrite each other's log files.

logdir="${JOBPROCDATE}/req${PROCREQ}"
echo "logdir = $logdir"

# S3 bucket for the log file, passed by the job launcher from [JOB_PARAMS] job_logs_s3_bucket_base
# of its configuration file.

if [ -z "$JOBLOGSS3BUCKET" ]; then
    echo "*** Error: Env. var. JOBLOGSS3BUCKET not set; quitting..."
    exit 66
fi
echo "JOBLOGSS3BUCKET = $JOBLOGSS3BUCKET"

echo "Executing /usr/bin/python3.11 /code/pipeline/awsBatchSubmitJobs_runSingleSciencePipeline.py >& $logfile"
/usr/bin/python3.11 /code/pipeline/awsBatchSubmitJobs_runSingleSciencePipeline.py >& $logfile

exitcode=$?

echo "Exitcode = $exitcode"


if [ $exitcode -eq 0 ]
then

    echo
    echo ##################################################################
    echo "Successfully ran single science pipeline"
    echo ##################################################################
    echo


else

    echo
    echo ##################################################################
    echo "*** Error: Failed running single science pipeline"
    echo ##################################################################
    echo

    exitcode=64

fi


echo "Executing aws s3 cp --quiet $logfile s3://${JOBLOGSS3BUCKET}/${logdir}/$logfile"
aws s3 cp --quiet "$logfile" s3://${JOBLOGSS3BUCKET}/${logdir}/$logfile

awss3cpexitcode=$?
echo "awss3cpexitcode=$awss3cpexitcode"

if [ $awss3cpexitcode -eq 0 ]
then

    echo
    echo ##################################################################
    echo "Successfully copied log file to s3://${JOBLOGSS3BUCKET}/${logdir}/$logfile"
    echo ##################################################################
    echo


else

    echo
    echo ##################################################################
    echo "*** Error: Failed copying log file to s3://${JOBLOGSS3BUCKET}/${logdir}/$logfile"
    echo ##################################################################
    echo

    exit 66

fi


echo "jobId: $AWS_BATCH_JOB_ID"
date
echo "bye bye!!"

echo "Exitcode = $exitcode"
exit $exitcode
