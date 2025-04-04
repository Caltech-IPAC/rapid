RAPID-Pipeline Bulk Run
####################################################

Overview
************************************

There are many steps to running the RAPID pipeline.
It is crucial to follow the listed order below, and do not omit
any steps, as a given step relies on all previous steps.

1. Run science pipelines (``ppid = 15``).

2. Register pipeline metadata in operations database.

3. Run post-processing pipelines (``ppid = 17``).
   Relies on updated operations database from step 2.

4. Register additional pipeline metadata in operations database.

All steps are executed within the environment of a Docker container
that is running on an EC2 instance with access to the operations database.
Any scripts to automate the execution of pipelines must
necessarily involve the ``docker run`` command.

Launch scripts are used to run pipelines.
These query the operations database and launch pipeline-instance jobs under AWS Batch.
Individual AWS Batch jobs do not themselves interact with the operations database.


Instructions
********************************************

The following shows commands to launch instances of the RAPID science pipeline as AWS Batch jobs
for a given range of observation dates.  It is assumed that all AWS Batch jobs will finish under
the same processing date.  In the example below, it is assumed the processing date is April 4, 2025 (``20250404``).

The to-be-run-under-AWS-Batch Docker container rapid_science_pipeline:1.0 has /code built in,
so there is no need to mount an external volume for /code.
The container name is arbitrary, and is set to "russ-test-jobsubmit" in the example below.
Since this Docker image contains the ENTRYPOINT instruction, you must override it  with the ``--entrypoint bash`` option
(and do not put ``bash`` at the end of the command).


Step 1
=============

The data to be processed are specified by the observation datetime range.

The environment variables STARTDATETIME and ENDDATETIME refer to the start and end observation datetimes (different from processing date).

.. code-block::

   mkdir -p /home/ubuntu/work/test_20250404
   cd /home/ubuntu/work/test_20250404
   aws s3 cp s3://rapid-pipeline-files/roman_tessellation_nside512.db /home/ubuntu/work/test_20250404/roman_tessellation_nside512.db

   sudo su

   docker run -it --entrypoint bash --name russ-test-jobsubmit -v /home/ubuntu/work/test_20250404:/work public.ecr.aws/y9b1s7h8/rapid_science_pipeline:latest

   export DBPORT=5432
   export DBNAME=rapidopsdb
   export DBUSER=rapidporuss
   export DBSERVER=35.165.53.98
   export DBPASS="????"
   export AWS_DEFAULT_REGION=us-west-2
   export AWS_SECRET_ACCESS_KEY=????
   export AWS_ACCESS_KEY_ID=????
   export LD_LIBRARY_PATH=/code/c/lib
   export PATH=/code/c/bin:$PATH
   export export RAPID_SW=/code
   export export RAPID_WORK=/work
   export PYTHONPATH=/code
   export PYTHONUNBUFFERED=1
   export ROMANTESSELLATIONDBNAME=/work/roman_tessellation_nside512.db

   cd /work

   export STARTDATETIME="2028-09-07 00:00:00"
   export ENDDATETIME="2028-09-08 08:30:00"
   python3.11 /code/pipeline/awsBatchSubmitJobs_launchSciencePipelinesForDateTimeRange.py >& awsBatchSubmitJobs_launchSciencePipelinesForDateTimeRange.out &


Manually monitor the AWS Batch console to verify all jobs ran to completion.  This will be automated at some later stage of development.


Step 2
============

For this step, the data to be processed are specified simply by the processing date as an argument on the command line of the Python script ``registerCompletedJobsInDB.py``.

.. code-block::

   cd /home/ubuntu/work/test_20250404

   sudo su

   docker run -it --entrypoint bash --name russ-test-jobsubmit -v /home/ubuntu/work/test_20250404:/work public.ecr.aws/y9b1s7h8/rapid_science_pipeline:latest

   export DBPORT=5432
   export DBNAME=rapidopsdb
   export DBUSER=rapidporuss
   export DBSERVER=35.165.53.98
   export DBPASS="????"
   export AWS_DEFAULT_REGION=us-west-2
   export AWS_SECRET_ACCESS_KEY=????
   export AWS_ACCESS_KEY_ID=????
   export LD_LIBRARY_PATH=/code/c/lib
   export PATH=/code/c/bin:$PATH
   export export RAPID_SW=/code
   export export RAPID_WORK=/work
   export PYTHONPATH=/code
   export PYTHONUNBUFFERED=1

   cd /work

   python3.11 $RAPID_SW/pipeline/registerCompletedJobsInDB.py 20250404 >& registerCompletedJobsInDB_20250404.out &

