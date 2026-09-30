RAPID Virtual Pipeline Operator
####################################################

Overview
************************************

The Virtual Pipeline Operator (VPO) is ``pipeline/virtualPipelineOperator.py``.  It runs
an entire processing request end to end, in place of the operator carrying out by hand the
steps described in :doc:`bulk_run`.

For one processing date the VPO launches the reference-image, science and post-processing
pipelines as AWS Batch jobs, waits for each set of jobs to finish, registers the resulting
metadata in the operations database, and then runs the database post-processing codes that
build sources, astronomical objects, lightcurve statistics and alert packets.  It opens a
``ProcReqs`` database record when it starts and closes it out when it finishes, so every
processing request is recorded and every file the request writes is filed under that
request.

The VPO is executed inside a RAPID-pipeline Docker container running on an EC2 instance
that has access to the operations database.


Running the VPO
************************************

The VPO takes the processing date as its single command-line argument, and reads the rest
of its inputs from the environment:

.. code-block::

    export DBNAME=socsimsdb
    export STARTDATETIME="2027-10-01 07:12:00"
    export ENDDATETIME="2027-10-02 00:00:00"

    python3.11 /code/pipeline/virtualPipelineOperator.py 20260706 >& virtualPipelineOperator_20260706.out &

``STARTDATETIME`` and ``ENDDATETIME`` are the observation start and end datetimes of the
exposures to be processed.  They are not the processing date: the processing date is the
command-line argument, and it is what the S3 object keys and the ``Jobs`` records are filed
under.

These environment variables are required, and the VPO quits with exit code 64 if any is
unset:

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Variable
     - Meaning
   * - ``RAPID_SW``
     - Root of the RAPID software tree.  The input configuration file is read from
       ``$RAPID_SW/cdf``.
   * - ``RAPID_WORK``
     - Working directory for the run.
   * - ``STARTDATETIME``
     - Observation start datetime of the exposures to process.
   * - ``ENDDATETIME``
     - Observation end datetime of the exposures to process.
   * - ``AWS_ACCESS_KEY_ID``
     - AWS credentials, needed to submit and poll AWS Batch jobs.
   * - ``AWS_SECRET_ACCESS_KEY``
     - AWS credentials, needed to submit and poll AWS Batch jobs.

The database connection is taken from the usual ``DBNAME``, ``DBSERVER``, ``DBPORT``,
``DBUSER`` and ``DBPASS`` variables.

The VPO sets several environment variables itself, for the codes it launches, so they
should not be set by hand:

* ``JOBPROCDATE`` -- the processing date.
* ``PROCREQ`` -- the ``reqid`` of the ``ProcReqs`` record the VPO created for this run.
* ``PIPEID``, ``MAKEREFIMAGESFLAG``, ``DRYRUN`` -- set as each stage requires.

The input configuration file is ``cdf/awsBatchSubmitJobs_launchSingleSciencePipeline.ini``.
The VPO reads the S3 bucket bases and the ``[AWS_BATCH]`` job queue, job definitions and job
name bases from it; the codes it launches read their own parameters from the same file.


Stages
************************************

The VPO runs the following stages in order for the processing date.  Each stage is a
separate Python script, launched as a subprocess, whose stdout and stderr go to a per-stage
log file named after the stage and the processing date.

.. list-table::
   :header-rows: 1
   :widths: 6 46 48

   * - #
     - Stage
     - What it does
   * - 1
     - ``launchBunchOfReferenceImagePipelines.py``
     - Launches the reference-image pipelines (``ppid = 12``) as AWS Batch jobs.
   * - 2
     - *wait*
     - Waits for the reference-image AWS Batch jobs to finish.
   * - 3
     - ``parallelRegisterCompletedJobsInDB.py``
     - Registers reference-image pipeline metadata in the operations database.
   * - 4
     - ``launchSciencePipelinesForDateTimeRangeWithRefImageWindow.py``
     - Launches the science pipelines (``ppid = 15``) as AWS Batch jobs.
   * - 5
     - *wait*
     - Waits for the science AWS Batch jobs to finish.
   * - 6
     - ``parallelRegisterCompletedJobsInDB.py``
     - Registers science-pipeline metadata in the operations database.
   * - 7
     - ``awsBatchSubmitJobs_launchPostProcPipelinesForProcDate.py``
     - Launches the post-processing pipelines (``ppid = 17``) as AWS Batch jobs.
   * - 8
     - *wait*
     - Waits for the post-processing AWS Batch jobs to finish.
   * - 9
     - ``parallelRegisterCompletedJobsInDBAfterPostProc.py``
     - Registers post-processing pipeline metadata in the operations database.
   * - 10
     - ``loadPSFCatIntoDBSourcesTable.py``
     - Loads the photutils PSF-fit catalogs into the ``Sources`` child tables.
   * - 11
     - ``loadSECatIntoDBSourcesTable.py``
     - Loads the positive and negative SExtractor catalogs into the ``XSources`` child
       tables.
   * - 12
     - ``crossMatchSources.py``
     - Cross-matches Sources, building ``AstroObjects_<field>`` and ``Merges_<field>``.
   * - 13
     - ``crossMatchXSources.py``
     - Cross-matches XSources, building ``XAstroObjects_<field>`` and ``XMerges_<field>``.
   * - 14
     - ``computeStatisticsForAstroObjects.py``
     - Computes lightcurve statistics into ``AstroObjectsMeta_<field>``.
   * - 15
     - ``computeStatisticsForXAstroObjects.py``
     - Computes lightcurve statistics into ``XAstroObjectsMeta_<field>``.
   * - 16
     - ``pruneNotBestMerges.py``
     - Deletes ``Merges_<field>`` records for sources from no-longer-best difference
       images.
   * - 17
     - ``pruneNotBestXMerges.py``
     - Deletes ``XMerges_<field>`` records for xsources from no-longer-best difference
       images.
   * - 18
     - ``produceAlertsForProcDate.py``
     - Produces alert packets for the difference images processed on this date.

Stages 10 through 17 pair up: each Sources stage has an XSources counterpart that does the
same thing for the SExtractor catalogs.  Alert production (stage 18) currently covers
Sources only.

The VPO prints an elapsed time in seconds after every stage, so a run's log file is also its
own timing breakdown.


Waiting for AWS Batch jobs
************************************

After launching each set of pipelines, the VPO queries the operations database for the
``Jobs`` records of that pipeline number and processing date that have not been closed out,
and counts only those with an AWS Batch job ID.  It then calls ``describe_jobs`` on each
one, sleeping 60 seconds between passes, until every job has reached ``SUCCEEDED`` or
``FAILED``.

A job that has failed does not stop the VPO; the wait ends when no job is still pending, so
failures are counted and the run moves on to registering whatever did succeed.  An
unrecognized job status, or an AWS Batch job ID that is no longer known to AWS, does stop
the run.


The ProcReqs record
************************************

The VPO inserts a ``ProcReqs`` record at the start of the run with the observation start and
end datetimes, and exports its primary key as ``PROCREQ`` for every code it launches.  All
of those codes file their S3 objects under

.. code-block::

   <processing date>/req<reqid>/

so two processing requests for the same processing date do not overwrite each other's
files.  See :doc:`../prod/products` for the product layout.

The record is closed out when the run ends, with ``status`` set to:

* ``1`` -- the request finished normally.
* ``-1`` -- the request terminated early because a stage failed.

The ``started``, ``ended`` and ``elapsed`` columns of the record bound the request in time.


Error handling
************************************

Every stage is checked for its exit code.  An exit code of 64 or higher from any stage in
1 through 17 is fatal: the VPO closes out the ``ProcReqs`` record with ``status = -1`` and
exits with code 64, leaving the remaining stages unrun.

Alert production (stage 18) is the exception.  It only reads database products populated by
the preceding stages, so a failure there is reported but does not abort the processing
request, and the ``ProcReqs`` record is still closed out with ``status = 1``.

A failure to open the database connection, or to insert the ``ProcReqs`` record, exits with
the database handler's own exit code before any stage runs.


.. note::
   The VPO also has a mode in which the processing date is omitted from the command line, in
   which case it takes the current Pacific-time date and loops.  That path still contains
   test scaffolding -- a dummy counting loop, a 30-second sleep, and a deliberate
   ``SIGINT`` to itself on the fourth iteration -- so it is not yet usable for operations.
   Always give the processing date explicitly.
