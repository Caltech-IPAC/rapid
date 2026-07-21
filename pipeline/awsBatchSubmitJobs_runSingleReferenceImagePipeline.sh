#!/bin/bash -x

date

env
echo "AWS Batch job to run single RAPID-reference-image pipeline instance."


logfile="rapid_pipeline_job_${JOBPROCDATE}_jid${RAPID_JOB_ID}_log.txt"
echo "logfile = $logfile"

echo "Executing /usr/bin/python3.11 /code/pipeline/awsBatchSubmitJobs_runSingleReferenceImagePipeline.py >& $logfile"
/usr/bin/python3.11 /code/pipeline/awsBatchSubmitJobs_runSingleReferenceImagePipeline.py >& $logfile

exitcode=$?

echo "Exitcode = $exitcode"


if [ $exitcode -eq 0 ]
then

    echo
    echo ##################################################################
    echo "Successfully ran single RAPID-reference-image pipeline instance"
    echo ##################################################################
    echo


else

    echo
    echo ###########################################################################
    echo "*** Error: Failed running single RAPID-reference-image pipeline instance"
    echo ###########################################################################
    echo

    exitcode=64

fi


echo "Executing aws s3 cp --quiet $logfile s3://rapid-pipeline-logs/${JOBPROCDATE}/$logfile"
aws s3 cp --quiet "$logfile" s3://rapid-pipeline-logs/${JOBPROCDATE}/$logfile

awss3cpexitcode=$?
echo "awss3cpexitcode=$awss3cpexitcode"

if [ $awss3cpexitcode -eq 0 ]
then

    echo
    echo ##################################################################
    echo "Successfully copied log file to s3://rapid-pipeline-logs/${JOBPROCDATE}/$logfile"
    echo ##################################################################
    echo


else

    echo
    echo ##################################################################
    echo "*** Error: Failed copying log file to s3://rapid-pipeline-logs/${JOBPROCDATE}/$logfile"
    echo ##################################################################
    echo

    exit 66

fi


echo "jobId: $AWS_BATCH_JOB_ID"
date
echo "bye bye!!"

echo "Exitcode = $exitcode"
exit $exitcode
