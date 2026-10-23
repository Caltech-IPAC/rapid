Testing with On-Orbit HLTDS-like Data
####################################################

Overview
************************************

These tests run the RAPID pipeline on on-orbit Roman WFI L2 files whose observations are like
those of the High Latitude Time Domain Survey (HLTDS).

.. TODO: Describe which on-orbit programs/visits are HLTDS-like and why.

The Roman program ID of the data is recorded with each test, as read from the L2 filenames.

Each test follows the same three steps:

1. Stream the L2 files for the program from MAST into the ingest S3 bucket
   with ``pipeline/stream_l2_files.py``, for example::

       python3.11 /code/pipeline/stream_l2_files.py <program> --filter F158 --workers 8 >& stream_l2_files_<date>.out

2. Convert the ASDF files to FITS and register them in the operations database
   with ``pipeline/ingestL2Files.py`` (see :doc:`/pl/l2_ingest`)::

       python3.11 /code/pipeline/ingestL2Files.py >& ingestL2Files_<date>.out

3. Run the RAPID pipeline with the Virtual Pipeline Operator (VPO) (see :doc:`/ops/vpo`)::

       python3.11 /code/pipeline/virtualPipelineOperator.py <date> >& virtualPipelineOperator_<date>.out

.. note::
    Metadata about the on-orbit data are stored in the ``commissioning`` RAPID-operations PostgreSQL database.
    The ingest and product S3 buckets are ``rapid-commissioning-ingest-files`` and ``rapid-commissioning-product-files``.

Adding a test entry
************************************

Each dated section below starts from the STDOUT of these steps.  ``docs/make_onorbit_test_entry.py``
parses the VPO STDOUT, and optionally the ``stream_l2_files.py`` and ``ingestL2Files.py`` STDOUT and the
per-job pipeline logs, and appends a new dated section to this page::

    python docs/make_onorbit_test_entry.py virtualPipelineOperator_<date>.out \
        --stream-log stream_l2_files_<date>.out \
        --ingest-log ingestL2Files_<date>.out \
        --job-logs-dir <dir>

The per-job logs (the STDOUT of each reference-image and science pipeline job) are in
``s3://<job_logs_s3_bucket_base>/<procdate>/req<reqid>/``, where ``job_logs_s3_bucket_base`` is set in the
``[JOB_PARAMS]`` section of ``cdf/awsBatchSubmitJobs_launchSingleSciencePipeline.ini``.  They can be downloaded with::

    aws s3 cp s3://<job_logs_s3_bucket_base>/<procdate>/req<reqid>/ <dir> --recursive

The script reads the bucket name from that file (or from the file given with ``--ini``) and records the
location of the per-job logs in the entry.

Any file in ``<dir>`` is read, so the job STDOUT may also be saved under other names (e.g. ``jid<jid>.out``),
as long as the name contains ``jid<jid>``.  Files that are not reference-image or science job output are skipped.

Use ``--stdout`` to print the entry instead of appending it.  The generated entry leaves ``TODO``
comments where a description of the test is needed (its purpose, differences from the previous
test, and observations); fill these in before committing.

