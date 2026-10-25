Testing with On-Orbit HLTDS-like Data
####################################################

Overview
************************************

These tests run the RAPID pipeline on on-orbit Roman WFI L2 files whose observations are like
those of the High Latitude Time Domain Survey (HLTDS, i.e. extragalactic with a given field observed
at different position angles and on different SCAs).

The Roman program ID of the data is recorded with each test, as read from the L2 filenames.

Each test follows the same three steps:

1. Stream the L2 files for the program from MAST into the ingest S3 bucket
   with ``pipeline/stream_l2_files.py``, for example::

       python3.11 /code/pipeline/stream_l2_files.py <program> >& stream_l2_files_<date>.out

2. Convert the ASDF files to FITS and register them in the operations database
   with ``pipeline/ingestL2Files.py`` (see :doc:`/pl/l2_ingest`)::

       python3.11 /code/pipeline/ingestL2Files.py >& ingestL2Files_<date>.out

3. Run the RAPID pipeline with the Virtual Pipeline Operator (VPO) (see :doc:`/ops/vpo`)::

       python3.11 /code/pipeline/virtualPipelineOperator.py <date> >& virtualPipelineOperator_<date>.out

Adding a test entry
************************************
.. code-block::
    python docs/make_onorbit_test_entry.py virtualPipelineOperator_<date>.out \
        --stream-log stream_l2_files_<date>.out \
        --ingest-log ingestL2Files_<date>.out \
        --job-logs-dir <dir>


