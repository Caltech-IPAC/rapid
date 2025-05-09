RAPID Pipeline Execution
####################################################

Overview
************************************

There are many steps to running the RAPID pipeline.
It is crucial to follow the listed order below, and do not omit
any steps, as a given step relies on all previous steps.

Here are the steps:

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

The to-be-run-under-AWS-Batch Docker container rapid_science_pipeline:latest
self-contains a
RAPID git-clone in the /code directory, so no volume binding to an
external filesystem containing the RAPID git repo is necessary.
The container name is arbitrary, and is set to "russ-test-jobsubmit" in the example below.
Since this Docker image contains the ENTRYPOINT instruction, you must override it  with the ``--entrypoint bash`` option
(and do not put ``bash`` at the end of the command).


Step 1
=============

Assume we process the data on April 4, 2025 (``20250404``).  This is the processing date.

Log into EC2 instance and, from root account (``sudo su``), perform Steps 1 through 4.

Launch AWS Batch jobs for the RAPID science pipeline.

The data to be processed are specified by the observation datetime range.
The environment variables STARTDATETIME and ENDDATETIME refer to the
start and end observation datetimes (an observation date is distinctly different from a processing date).

.. code-block::

   sudo su

   mkdir -p /home/ubuntu/work/test_20250404
   cd /home/ubuntu/work/test_20250404
   aws s3 cp s3://rapid-pipeline-files/roman_tessellation_nside512.db /home/ubuntu/work/test_20250404/roman_tessellation_nside512.db

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

Manually monitor the AWS Batch console to verify all jobs ran to completion.
This will be automated at some later stage of development.

RAPID products are organized in an S3 bucket according to the :doc:`processing date </prod/products>`.
The same processing date is a required input parameter in Step 2.


Step 2
============

To be executed only after all AWS Batch jobs in Step 1 have completed.

For this step, the data to be processed are specified simply by the processing date
as an argument on the command line of the Python script ``registerCompletedJobsInDB.py``.
This is executed inside a container with the same environment as defined for Step 1.

.. code-block::

   cd /work

   python3.11 /code/pipeline/parallelRegisterCompletedJobsInDB.py 20250404 >& parallelRegisterCompletedJobsInDB_20250404.out &


Step 3
============

Launch AWS Batch jobs for the RAPID post-processing pipeline.

For this step, the data to be post-processed are specified simply by the processing date
via the environment variable JOBPROCDATE (different from observation date).
This is executed inside a container with the same environment as defined for Step 1.

.. code-block::

   cd /work

   export JOBPROCDATE=20250404

   python3.11 /code/pipeline/awsBatchSubmitJobs_launchPostProcPipelinesForProcDate.py >& awsBatchSubmitJobs_launchPostProcPipelinesForProcDate_20250404.out &

Manually monitor the AWS Batch console to verify all jobs ran to completion.
This will be automated at some later stage of development.


Step 4
============

To be executed only after all AWS Batch jobs in Step 3 have completed.

For this step, the data to be processed are specified simply by the processing date
as an argument on the command line of the Python script ``registerCompletedJobsInDBAfterPostProc.py``.
This is executed inside a container with the same environment as defined for Step 1.

.. code-block::

   cd /work

   python3.11 /code/pipeline/registerCompletedJobsInDBAfterPostProc.py 20250404 >& registerCompletedJobsInDBAfterPostProc_20250404.out &


Performance
********************************************

The AWS Batch jobs are configured to each require a machine with 4 vCPUs, 16 GB of memory, and 20 GB of disk space.
AWS Batch for the RAPID pipeline is configured to have up to 1000 jobs running in parallel,
and this can be easily increased as needed; however, the number of parallel jobs is contingent
upon the AWS Batch machine availability, which can vary with load from competing AWS customers external to the RAPID project.

The addition of SFFT image differencing to the science pipeline raised the machine memory requirement from 8 GB to 16 GB,
and also increased the pipeline execution time by about 3 minutes.

Step 1
============

On an 8-core job-launcher machine (``t3.2xlarge`` EC2 instance), it takes 1183 seconds
to launch 2069 RAPID-science-pipeline jobs with 8-core multiprocessing.

The 2069 RAPID-science-pipeline jobs take 480 seconds on average to run in parallel under AWS Batch, once
the job has actually started on the AWS Batch machine.  There can, however, be significant time spent waiting
in the AWS Batch queue, as illustrated by the histogram below.  Also, the pipeline itself running on an AWS Batch machine
takes longer than the reported elapsed times last month because now the pipeline computes difference images and catalogs
for both ZOGY and SFFT.
There were 80 failed pipelines because there were no prior observations for which to generate reference images.

Here is a histogram of the AWS Batch queue wait times for an available AWS Batch machine on which to run a pipeline job:

.. image:: queue_wait_times.png

In theory, an AWS Batch machine with 4 vCPUs and 16 GB of memory is a scarcer resource than those with
only one vCPU and 8 GB of memory that were being used in pipeline testing last month.
That, along with potential competition from AWS customers external to the RAPID project, may explain
the relatively longer wait times in the AWS Batch queue for available machines.

Here is a histogram of the job execution times, measured from pipeline start to pipeline finish on an AWS Batch machine:

.. image:: pipeline_execution_times.png


Step 2
============

On an 8-core job-launcher machine, it takes 415 seconds
to register database records for 2069 RAPID-science-pipeline jobs with 8-core multiprocessing.

Records are inserted and/or updated in the Jobs, DiffImages, DiffImMeta, RefImages, RefImCatalogs,
RefImMeta, and RefImImages database tables.

For development, the RAPID operations database is deployed on a ``t2.micro`` EC2 machine,
which has only one virtual core (1 vCPU).

Step 3
============

On an 8-core job-launcher machine, it takes 1051 seconds
to launch 1989 RAPID-post-processing-pipeline jobs with 8-core multiprocessing.

The 1989 RAPID-post-processing-pipeline jobs take less than 60 seconds to run in parallel under AWS Batch.

Step 4
============

It takes 476 seconds to register database records for 1989 RAPID-post-processing-pipeline jobs running as a single process.

Records are updated in the Jobs, DiffImages, and RefImages database tables.


