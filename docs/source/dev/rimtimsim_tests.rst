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

Only one field is covered by the RimTimSim simulated-image dataset::

    rimtimsimdb=> select distinct field from l2files;
      field
    ---------
     4682737
    (1 row)

Also, only one SCA and two filters are included in the RimTimSim simulated-image dataset::

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


5/30/2025
************************************

The following improvements have been implemented:

1. Feed ZOGY computed astrometric uncertainties computed from gain-matching, instead of fixed value 0.01 pixels.
2. Shift the gain-matched reference image that is fed to ZOGY by subpixel x and y offsets computed from gain-matching.
3. Transpose the science-image PSF prior to feeding it to ZOGY (this required due to a feature of the rimtimsim dataset).

Only ZOGY difference-image products were made in this test.
NaNs were removed from the ZOGY inputs prior to ZOGY execution, and then restored in the ZOGY outputs (this is a new
requirement due to the presence of NaNs in the rimtimsim dataset).
Initially, only one image was processed for each of the two filters, in order to make the two required reference images
that are needed to process the remaining images (jids 1 and 3).  The reference images were made on the day before the
date of this test.
For the jid=1 instance, PSF-fit catalog generation took 488 seconds (nsources=27420), and reference-image generation took 426 seconds (nframes=23),
comprising the majority of the run time.  The total run time of the jid=1 instance was 1038 seconds.

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


Of the 215 jobs executed on 5/30/2025 that did not have to generate a reference image on the fly (because the two
required reference images were generated on the previous day),
the minimum elapsed job run time was 387 seconds and the maximum 849 seconds.

Here are metadata about the two reference images, for fid = 4 and 7:

.. code-block::

    rimtimsimdb=> select * from refimages where vbest>0;
     rfid |  field  |  hp6  |   hp9   | fid | ppid | version | vbest |                                 filename                                 | status |             checksum             |          created           | svid | avid | archivestatus | infobits
    ------+---------+-------+---------+-----+------+---------+-------+--------------------------------------------------------------------------+--------+----------------------------------+----------------------------+------+------+---------------+----------
      219 | 4682737 | 28823 | 1844720 |   4 |   15 |     110 |     1 | s3://rapid-product-files/20250529/jid1/awaicgen_output_mosaic_image.fits |      1 | 8c234333894d25bb4a4a1305d143d618 | 2025-05-29 07:58:33.624864 |    1 |      |             0 |        0
      220 | 4682737 | 28823 | 1844720 |   7 |   15 |     109 |     1 | s3://rapid-product-files/20250529/jid3/awaicgen_output_mosaic_image.fits |      1 | 5bba26bc6ac244c5ebc8d9ab3cb0dccc | 2025-05-29 07:58:35.057414 |    1 |      |             0 |        0
    (2 rows)

.. code-block::

    rimtimsimdb=> select * from refimmeta where rfid in (select rfid from refimages where vbest>0);
     rfid |  field  |  hp6  |   hp9   | fid | nframes |     mjdobsmin     |     mjdobsmax     | npixsat | npixnan  |   clmean   |  clstddev   | clnoutliers |  gmedian   |  datascale  |    gmin    |   gmax    | cov5percent | medncov |  medpixunc  | fwhmmedpix | fwhmminpix | fwhmmaxpix | nsexcatsources
    ------+---------+-------+---------+-----+---------+-------------------+-------------------+---------+----------+------------+-------------+-------------+------------+-------------+------------+-----------+-------------+---------+-------------+------------+------------+------------+----------------
      219 | 4682737 | 28823 | 1844720 |   4 |      23 | 61450.51327337697 | 61462.55675993627 |       0 | 33052859 |   0.273195 | 0.107858755 |     1496560 |  0.2515229 |  0.13983491 | 0.09267347 | 315.44882 |    32.51499 |       0 |  0.03142484 |       3.44 |      -0.02 |      209.4 |          61980
      220 | 4682737 | 28823 | 1844720 |   7 |      25 | 61450.25169813307 | 61462.81866137544 |       0 | 33043712 | 0.13141742 |  0.08563688 |     1585559 | 0.10927002 | 0.115269825 | 0.01366262 | 307.28656 |    32.52147 |       0 | 0.019715047 |       2.46 |      -1.21 |     180.55 |         104036
    (2 rows)

