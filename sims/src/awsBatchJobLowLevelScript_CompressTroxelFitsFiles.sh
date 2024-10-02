#!/bin/bash

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
echo "This is my first real AWS Batch job!."


logfile="rapid_compress_job_${FILTERSTRING}_${INPUTSUBDIR}_log.txt"
echo "logfile = $logfile"

echo "Executing /usr/bin/python3 /usr/local/bin/awsBatchJobLowLevelScript_CompressTroxelFitsFiles.py >& $logfile"
/usr/bin/python3 /usr/local/bin/awsBatchJobLowLevelScript_CompressTroxelFitsFiles.py >& $logfile
aws s3 cp --quiet $logfile s3://rapid-pipeline-logs/${FILTERSTRING}/$logfile

echo "jobId: $AWS_BATCH_JOB_ID"
date
echo "bye bye!!"
