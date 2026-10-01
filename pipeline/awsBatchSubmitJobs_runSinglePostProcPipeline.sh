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
echo "AWS Batch job to run single RAPID post-processing pipeline."


logfile="rapid_postproc_job_${JOBPROCDATE}_jid${RAPID_JOB_ID}_log.txt"
echo "logfile = $logfile"

# S3 subdirectory of the processing request, so that two processing requests for
# the same processing date do not overwrite each other's log files.

logdir="${JOBPROCDATE}/req${PROCREQ}"
echo "logdir = $logdir"

echo "Executing /usr/bin/python3.11 /code/pipeline/awsBatchSubmitJobs_runSinglePostProcPipeline.py >& $logfile"
/usr/bin/python3.11 /code/pipeline/awsBatchSubmitJobs_runSinglePostProcPipeline.py >& $logfile

exitcode=$?

echo "Exitcode = $exitcode"


if [ $exitcode -eq 0 ]
then

    echo
    echo ##################################################################
    echo "Successfully ran single post-processing pipeline"
    echo ##################################################################
    echo


else

    echo
    echo ##################################################################
    echo "*** Error: Failed running single post-processing pipeline"
    echo ##################################################################
    echo

    exitcode=64

fi


echo "Executing aws s3 cp --quiet $logfile s3://rapid-commissioning-pipeline-logs/${logdir}/$logfile"
aws s3 cp --quiet "$logfile" s3://rapid-commissioning-pipeline-logs/${logdir}/$logfile

awss3cpexitcode=$?
echo "awss3cpexitcode=$awss3cpexitcode"

if [ $awss3cpexitcode -eq 0 ]
then

    echo
    echo ##################################################################
    echo "Successfully copied log file to s3://rapid-commissioning-pipeline-logs/${logdir}/$logfile"
    echo ##################################################################
    echo


else

    echo
    echo ##################################################################
    echo "*** Error: Failed copying log file to s3://rapid-commissioning-pipeline-logs/${logdir}/$logfile"
    echo ##################################################################
    echo

    exit 66

fi


echo "jobId: $AWS_BATCH_JOB_ID"
date
echo "bye bye!!"

echo "Exitcode = $exitcode"
exit $exitcode
