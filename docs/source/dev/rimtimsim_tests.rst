Testing with RimTimSim Simulated Data
####################################################

Overview
************************************

Robby Wilson's simulated data, hereby referred to as RimTimSim data.
These are images of dense stellar fields.
It is a small dataset with just 263 images total (details below).

The tests described below are organized by processing date.

RimTimSim simulated data are used, which cover the following observation range::

    rimtimsimdb=> select min(dateobs),max(dateobs) from l2files;
               min           |          max
    -------------------------+------------------------
     2027-02-14 06:02:26.719 | 2027-04-24 21:17:51.141
    (1 row)

Only one SCA and two filters are included in the RimTimSim simulated-image dataset::

    rimtimsimdb=> select sca,fid,count(*) from l2files group by sca,fid order by sca,fid;
     sca | fid | count
    -----+-----+-------
       2 |   4 |   131
       2 |   7 |   132
    (2 rows)


Look-up table all of the filter IDs versus filter names included in the database:

.. code-block::

    rimtimsimdb=> select * from filters order by fid;
     fid | filter
    -----+--------
       1 | F184
       2 | H158
       3 | J129
       4 | K213
       5 | R062
       6 | Y106
       7 | Z087
       8 | W146
    (8 rows)


5/28/2025
************************************

Only ZOGY difference-image products were made in this test.
NaNs were removed from the ZOGY inputs prior to ZOGY execution, and then restored in the ZOGY outputs.
Initially only one image was processed for each of the two filters, in order to make the two required reference images
that are needed to process the remaining images.

The following Jobs records gives the run times for the two pipeline instances for bootstrapping the reference images.
For the jid=1 instance, PSF-fit catalog generation took 1043 seconds, and reference-image generation took 446 seconds,
comprising the majority of the run time.

.. code-block::

    select * from jobs where jid in (1,3);
     jid | ppid | expid | sca |  field  | fid | rid | machine |          launched          |     qwaited     |       started       |        ended        | elapsed  | exitcode | status | slurm |            awsbatchjobid
    -----+------+-------+-----+---------+-----+-----+---------+----------------------------+-----------------+---------------------+---------------------+----------+----------+--------+-------+--------------------------------------
       1 |   15 |   422 |   2 | 4682737 |   4 | 423 |         | 2025-05-28 08:22:47.154981 | 00:01:42.845019 | 2025-05-28 08:24:30 | 2025-05-28 08:51:24 | 00:26:54 |        0 |      1 |       | e2b58b65-84c9-4beb-8a8c-1c429f7fa37b
       3 |   15 |   292 |   2 | 4682737 |   7 | 293 |         | 2025-05-28 08:23:01.312995 | 00:01:39.687005 | 2025-05-28 08:24:41 | 2025-05-28 08:46:41 | 00:22:00 |        0 |      1 |       | 5f983f49-803e-44e7-80fb-dff097d68a41
    (2 rows)


Processing started at a later observation time than the very beginning to reserve some prior image frames for making reference images::

    export STARTDATETIME="2027-02-27 00:00:00"
    export ENDDATETIME="2027-04-25 00:00:00"


Numbers of exposure-SCA images processed for each available filter (fid = 4 and 7 only):

.. code-block::

    rimtimsimdb=> select count(*) from l2files where dateobs >= '20270227' group by fid order by fid;
     count
    -------
       108
       107
    (2 rows)

Numbers of exposure-SCA images for use in constructing the reference images:

.. code-block::

    rimtimsimdb=> select fid,count(*) from l2files where dateobs < '20270227' group by fid order by fid;
      fid | count
     -----+-------
        4 |    23
        7 |    25
    (2 rows)


Here are metadata about the two reference images, for fid = 4 and 7:

.. code-block::

    rimtimsimdb=> select * from refimages where vbest>0;

     rfid |  field  |  hp6  |   hp9   | fid | ppid | version | vbest |                                 filename                                 | status |             checksum             |          created           | svid | avid | archivestatus | infobits
    ------+---------+-------+---------+-----+------+---------+-------+--------------------------------------------------------------------------+--------+----------------------------------+----------------------------+------+------+---------------+----------
      217 | 4682737 | 28823 | 1844720 |   4 |   15 |     109 |     1 | s3://rapid-product-files/20250528/jid1/awaicgen_output_mosaic_image.fits |      1 | e5609ad6307f7c6ac35729501d8aff6e | 2025-05-28 09:03:48.450075 |    1 |      |             0 |        0
      218 | 4682737 | 28823 | 1844720 |   7 |   15 |     108 |     1 | s3://rapid-product-files/20250528/jid3/awaicgen_output_mosaic_image.fits |      1 | f48bbdfe9314e0756e83eb97a1cd9c7d | 2025-05-28 09:03:49.889292 |    1 |      |             0 |        0
    (2 rows)

.. code-block::

    rimtimsimdb=> select * from refimmeta where rfid in (select rfid from refimages where vbest>0);
     rfid |  field  |  hp6  |   hp9   | fid | nframes |     mjdobsmin     |     mjdobsmax     | npixsat | npixnan  |   clmean   |  clstddev   | clnoutliers |  gmedian   | datascale  |    gmin     |   gmax    | cov5percent | medncov |  medpixunc  | fwhmmedpix | fwhmminpix | fwhmmaxpix | nsexcatsources
    ------+---------+-------+---------+-----+---------+-------------------+-------------------+---------+----------+------------+-------------+-------------+------------+------------+-------------+-----------+-------------+---------+-------------+------------+------------+------------+----------------
      217 | 4682737 | 28823 | 1844720 |   4 |      23 | 61450.51327337697 | 61462.55675993627 |       0 | 33052571 | 0.28307146 |  0.10786036 |     1520878 | 0.26280764 | 0.14043844 |  0.09176843 |  313.1495 |    32.51655 |       0 | 0.032161918 |       6.22 |       2.45 |     159.01 |          40276
      218 | 4682737 | 28823 | 1844720 |   7 |      25 | 61450.25169813307 | 61462.81866137544 |       0 | 33043704 | 0.15944998 | 0.102196425 |     1438074 | 0.13417932 | 0.13140243 | 0.020625079 | 305.82645 |   32.521763 |       0 | 0.021889236 |       6.09 |       2.75 |     253.22 |          56478
    (2 rows)

