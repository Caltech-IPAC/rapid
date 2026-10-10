RAPID Virtual Pipeline Operator
####################################################

.. important::
   The VPO is a work in progress.  It runs a whole processing request through all eighteen
   stages without further intervention, and, started without a processing date, it now keeps
   running on its own, choosing the observation range of each request from the database (see
   `Open-loop operation`_).  AWS Batch jobs that fail for infrastructure reasons, such as
   ``CannotPullContainerError``, are resubmitted automatically (see
   `Resubmitting failed AWS Batch jobs`_).  Still missing: pipelines that fail with their own
   error code are not rerun, L2Files left unprocessed within an observation range already
   covered are not revisited automatically, and a stage failure stops the open loop.

   Expect the details on this page to change as that work lands.

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

The VPO runs in one of two modes:

* **Processing-date run.**  Given the processing date as its single command-line argument,
  the VPO runs one processing request for the observation range given by ``STARTDATETIME``
  and ``ENDDATETIME``, and then exits.
* **Open loop.**  Without a command-line argument, the VPO runs processing requests one
  after another, choosing the observation range of each itself; ``STARTDATETIME`` and
  ``ENDDATETIME`` are not used.  See `Open-loop operation`_.

A processing-date run reads the rest of its inputs from the environment:

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
unset (``STARTDATETIME`` and ``ENDDATETIME`` only for a processing-date run):

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
     - Observation start datetime of the exposures to process (processing-date run only).
   * - ``ENDDATETIME``
     - Observation end datetime of the exposures to process (processing-date run only).
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
The VPO reads the S3 bucket bases, the ``[AWS_BATCH]`` job queue, job definitions and job
name bases, and the ``[JOB_PARAMS]`` parameters ``n_retry_failed_aws_batch_job``,
``min_elapsed_observation_seconds``, ``max_elapsed_observation_seconds`` and
``open_loop_sleep_seconds`` from it; the codes it launches read their own parameters from the
same file.


Open-loop operation
************************************

Started without a processing date, the VPO loops.  At the start of each iteration it opens
the database connection and chooses the observation range of the next processing request:

1. The *high-water mark* is the latest ``obsendtime`` of a ``ProcReqs`` record that finished
   normally (``status = 1``).  Requests that failed (``-1``) or never finished (``0``) do not
   count, so their range is covered again.
2. The *unprocessed* L2Files are those observed at or after the high-water mark with
   ``vbest > 0`` and ``status > 0`` and no ``DiffImages`` record with ``vbest > 0``.
3. If there are none, or they span less than ``min_elapsed_observation_seconds`` of
   observation time (earliest to latest ``dateobs``), the VPO closes the connection, sleeps
   ``open_loop_sleep_seconds`` and checks again.
4. Otherwise the range starts at the earliest unprocessed ``dateobs`` and ends just after the
   latest one, or ``max_elapsed_observation_seconds`` after the start, whichever comes first.
   As in a processing-date run, the launch scripts select ``dateobs >= STARTDATETIME`` and
   ``dateobs < ENDDATETIME``, so an L2File exactly at a capped end falls in the next range.

The request then runs exactly as a processing-date run, with the current Pacific-time date
as its processing date and the chosen range recorded in its ``ProcReqs`` record.  Several
requests may run on the same processing date; each files its S3 objects under its own
``req<reqid>``.

The ``[JOB_PARAMS]`` parameters, with their values in
``cdf/awsBatchSubmitJobs_launchSingleSciencePipeline.ini``:

.. list-table::
   :header-rows: 1
   :widths: 40 15 45

   * - Parameter
     - Value
     - Meaning
   * - ``min_elapsed_observation_seconds``
     - 900
     - Least observation time the unprocessed L2Files must span for a request to run.
   * - ``max_elapsed_observation_seconds``
     - 7200
     - Longest observation range of one request.
   * - ``open_loop_sleep_seconds``
     - 600
     - Time between checks while there is not enough unprocessed data.

The high-water mark means an observation range is never covered twice.  L2Files in a
covered range that did not get a difference image -- for example because their field did not
yet have enough frames for a reference image, so the science launcher skipped them -- are
left for a processing-date run by hand.  Unprocessed L2Files spanning less than
``min_elapsed_observation_seconds`` wait until more data arrive; when no more will, process
them with a processing-date run, or lower the parameter.

To stop the open loop, send the VPO ``SIGINT`` (Ctrl-C) or ``SIGQUIT``.  It finishes the
processing request under way, if any, and exits with code 0 before starting the next one,
or within ten seconds if it is sleeping.  A stage failure still terminates the VPO, as
described under `Error handling`_.


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
     - Waits for the reference-image AWS Batch jobs to finish, and resubmits those that failed
       for infrastructure reasons.
   * - 3
     - ``parallelRegisterCompletedJobsInDB.py``
     - Registers reference-image pipeline metadata in the operations database.
   * - 4
     - ``launchSciencePipelinesForDateTimeRangeWithRefImageWindow.py``
     - Launches the science pipelines (``ppid = 15``) as AWS Batch jobs.
   * - 5
     - *wait*
     - Waits for the science AWS Batch jobs to finish, and resubmits those that failed
       for infrastructure reasons.
   * - 6
     - ``parallelRegisterCompletedJobsInDB.py``
     - Registers science-pipeline metadata in the operations database.
   * - 7
     - ``awsBatchSubmitJobs_launchPostProcPipelinesForProcDate.py``
     - Launches the post-processing pipelines (``ppid = 17``) as AWS Batch jobs.
   * - 8
     - *wait*
     - Waits for the post-processing AWS Batch jobs to finish, and resubmits those that failed
       for infrastructure reasons.
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


Resubmitting failed AWS Batch jobs
====================================

After each wait (stages 2, 5 and 8), the VPO looks for the jobs of that stage that ended
``FAILED``, logs each one with its AWS Batch status reason, container reason and exit code,
and resubmits those that failed for an *infrastructure* reason.  A failed job counts as an
infrastructure failure when either:

* AWS Batch reports one of ``CannotPullContainerError``, ``CannotStartContainerError``,
  ``CannotCreateContainerError``, ``CannotInspectContainerError``,
  ``ResourceInitializationError`` or ``DockerTimeoutError``, or that its EC2 host was
  terminated (``Host EC2 ... terminated``, e.g., a Spot instance reclaimed), or
* the container never produced an exit code.

These jobs are not resubmitted:

* Jobs that exited with an exit code of their own.  The pipelines exit with 64 or higher when
  they fail on their own errors, and a container killed for running out of memory exits with
  137.  A rerun would most likely fail the same way.
* Jobs cancelled or terminated by an operator.

A resubmitted job gets the same job name, job queue, job-definition revision and
environment variables as the failed one, so it runs the same pipeline on the same inputs.
Its ``Jobs`` record is kept, and its ``awsbatchjobid`` is updated to the new AWS Batch job,
so that the wait and the job registration that follow use the new job.  The VPO then waits
for the resubmitted jobs and checks again, resubmitting each job at most
``n_retry_failed_aws_batch_job`` times (``[JOB_PARAMS]``; 3 by default, 0 to turn
resubmission off).  Jobs still failing after that are reported with a warning and are
registered as failed, as before.

Each pass is logged, with lines such as::

    Failed AWS Batch job: job_type=science, jid=..., awsbatchjobid=..., jobName=...: statusReason=..., container reason=CannotPullContainerError: ..., exitCode=None; infrastructure failure
    Resubmitted AWS Batch job (resubmission 1 of 3): jid=..., jobName=..., old awsbatchjobid=..., new awsbatchjobid=...

and the VPO prints the elapsed time spent resubmitting after each of the three stages.


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

Resubmitting failed AWS Batch jobs (see `Resubmitting failed AWS Batch jobs`_) stops the run
only if a resubmitted job's ``Jobs`` record cannot be updated with its new AWS Batch job ID;
the ``ProcReqs`` record is then closed out with ``status = -1``.  A job that cannot be
resubmitted is reported and the run continues.


.. note::
   In the open loop, a stage failure stops the VPO, as in a processing-date run, rather than
   moving on to the next processing request, so that the failure is investigated.  The failed
   request's ``ProcReqs`` record has ``status = -1``, so its observation range is chosen again
   when the VPO is restarted.
