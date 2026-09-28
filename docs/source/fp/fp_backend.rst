RAPID Forced-Photometry Backend
####################################################

Overview
************************************

The Python script ``pipeline/forcedPhotometryForField.py`` is the RAPID
forced-photometry backend. Run it inside a RAPID-pipeline container with
one or more sky positions in the same sky tile (a.k.a. field). It generates
one forced-photometry lightcurve file per sky position.

The backend requires access to a RAPID operations PostgreSQL database;
the read-only ``DBUSER=apollo`` can be used. The ``Fields`` table defines
field centers and corners for the entire sky.

For Open Univ sims, use::

    export DBNAME=fakesourcesdb

For rimtimsims, use::

    export DBNAME=rimtimsims2db

For Soc-sim images, use::

    export DBNAME=socsimsdb


Instructions
************************************

Create a text file of input sky positions. For now, ``reqid`` is an
arbitrary unique index::

    vi input_sky_positions.txt

    reqid,ra,dec
    1,8.573549,-42.316955
    2,8.592243,-42.298079
    3,8.5593654,-42.272997


Inside the RAPID-pipeline container, configure the environment and run
the backend:

.. code-block::

    cd /work
    export DBPORT=5432
    #export DBNAME=fakesourcesdb
    export DBNAME=rimtimsims2db
    export DBUSER=apollo
    export DBSERVER=???
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

    # The following is the database ID from the associated Fields-table record
    # in the PostgreSQL database.
    export FIELD=5261331
    export SKYPOSITIONSCSVFILE=input_sky_positions.txt

    aws s3 cp s3://rapid-pipeline-files/roman_tessellation_nside512.db .

    python3.11 /code/pipeline/forcedPhotometryForField.py >& forcedPhotometryForField.out &

    [1] 6367

    tail -f forcedPhotometryForField.out

Output
************************************

The example produces these forced-photometry lightcurve files:

.. code-block::

    rapid_req1_lc.txt
    rapid_req2_lc.txt
    rapid_req3_lc.txt

Each file contains a table with metadata columns and one lightcurve data
point per row, covering all available Roman WFI bandpass filters.
