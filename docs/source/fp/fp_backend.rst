RAPID Forced-Photometry Backend
####################################################

Overview
************************************

The python script ``pipeline/forcedPhotometryForField.py``
is the RAPID forced-photometry backend.
For a given set of sky positions in the same sky tile (a.k.a. field),
separate forced-photometry lightcurve output files will be generated.
Access to a RAPID operations PostgreSQL database is required, and the read-only
``DBUSER=apollo`` can be used.

For Open Univ sims, use::

    export DBNAME=fakesourcesdb

For rimtimsims, use::

    export DBNAME=rimtimsims2db

For Soc-sim images, use::

    export DBNAME=socsimsdb


Instructions
************************************

The forced-photometry backend should be executed inside a RAPID-pipeline container.

A set of one or more sky positions must be in same field (a.k.a sky tile) for a
given forced-photometry backend execution.
The PostgreSQL database table called ``Fields`` defines field centers and corners for the entire sky.
For now, the ``reqid`` is just an arbitrary unique index.

First, set up a text file with input sky positions of interest::

    vi input_sky_positions.txt

    reqid,ra,dec
    1,8.573549,-42.316955
    2,8.592243,-42.298079
    3,8.5593654,-42.272997


Here is how to execute the forced-photometry backend inside a a RAPID-pipeline container.

.. code-block::

    cd /work
    export DBPORT=5432
    #export DBNAME=fakesourcesdb
    export DBNAME=rimtimsims2db
    export DBUSER=apollo
    export DBSERVER=35.165.53.98
    export DBPASS="???"
    export AWS_DEFAULT_REGION=us-west-2
    export AWS_ACCESS_KEY_ID=???
    export AWS_SECRET_ACCESS_KEY=???
    export LD_LIBRARY_PATH=/code/c/lib
    export PATH=/code/c/bin:$PATH
    export export RAPID_SW=/code
    export export RAPID_WORK=/work
    export PYTHONPATH=/code
    export PYTHONUNBUFFERED=1
    export ROMANTESSELLATIONDBNAME=/work/roman_tessellation_nside512.db

    # The following is the database ID from the associated ``Fields`` record
    # in the PostgreSQL database.
    export FIELD=5261331
    export SKYPOSITIONSCSVFILE=input_sky_positions.txt

    aws s3 cp s3://rapid-pipeline-files/roman_tessellation_nside512.db .

    python3.11 /code/pipeline/forcedPhotometryForField.py >& forcedPhotometryForField.out &

    [1] 6367

    tail -f forcedPhotometryForField.out

The output files from the above backend-execution example are::

.. code-block::

    rapid_req1_lc.txt
    rapid_req2_lc.txt
    rapid_req3_lc.txt

