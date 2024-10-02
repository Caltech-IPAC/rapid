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

if [ -z $var ]; then
    echo "input var is blank; resetting to 2...";
    var=2;
fi

env
echo "AWS Batch job to compress OpenUniverse simulated FITS file."


logfile="rapid_compress_job_${FILTERSTRING}_${INPUTSUBDIR}_log.txt"
echo "logfile = $logfile"

echo "Executing /usr/bin/python3 /usr/local/bin/awsBatchJobLowLevelScript_CompressTroxelFitsFiles.py >& $logfile"
/usr/bin/python3 /usr/local/bin/awsBatchJobLowLevelScript_CompressTroxelFitsFiles.py >& $logfile

exitcode=$?

echo "Exitcode = $exitcode"


if [ $exitcode -eq 0 ]
then

    echo
    echo ##################################################################
    echo "Successfully compressed FITS files"
    echo ##################################################################
    echo


else

    echo
    echo ##################################################################
    echo "*** Error: Failed compressing FITS files"
    echo ##################################################################
    echo

    exit 64

fi


echo "Executing aws s3 cp --quiet $logfile s3://rapid-pipeline-logs/${FILTERSTRING}/$logfile"
aws s3 cp --quiet $logfile s3://rapid-pipeline-logs/${FILTERSTRING}/$logfile

exitcode=$?


if [ $exitcode -eq 0 ]
then

    echo
    echo ##################################################################
    echo "Successfully copied log file to s3://rapid-pipeline-logs/${FILTERSTRING}/$logfile"
    echo ##################################################################
    echo


else

    echo
    echo ##################################################################
    echo "*** Error: Failed copying log file to s3://rapid-pipeline-logs/${FILTERSTRING}/$logfile"
    echo ##################################################################
    echo

    exit 64

fi


echo "jobId: $AWS_BATCH_JOB_ID"
date
echo "bye bye!!"

echo "Exitcode = $exitcode"
exit $exitcode
