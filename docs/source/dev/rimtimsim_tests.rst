Testing with RimTimSim Simulated Data
####################################################

Overview
************************************

Robby Wilson's simulated-image data, hereby referred to as RimTimSim data, are describe here.
These are images of dense stellar fields.
It is a small dataset with just 263 images total (details below).
These images have small dithers and small image-angle variations.

The tests described below are organized by processing date.

The RimTimSim simulated-image dataset covers the following observation range::

    rimtimsimdb=> select min(dateobs),max(dateobs) from l2files;
               min           |          max
    -------------------------+------------------------
     2027-02-14 06:02:26.719 | 2027-04-24 21:17:51.141
    (1 row)

Only one field is covered by the RimTimSim dataset::

    rimtimsimdb=> select distinct field from l2files;
      field
    ---------
     4682737
    (1 row)

Also, only one SCA and two filters are included in the RimTimSim dataset::

    rimtimsimdb=> select sca,fid,count(*) from l2files group by sca,fid order by sca,fid;
     sca | fid | count
    -----+-----+-------
       2 |   4 |   131
       2 |   7 |   132
    (2 rows)

Look-up table all of the filter IDs versus Roman Space Telescope filter names included in the database:

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

The following pipeline-software improvements have been implemented:

1. Feed ZOGY computed astrometric uncertainties computed from gain-matching, instead of fixed value 0.01 pixels.
2. Shift the gain-matched reference image that is fed to ZOGY by subpixel x and y offsets computed from gain-matching.
3. Transpose the science-image PSF prior to feeding it to ZOGY (this required due to a feature of the RimTimSim dataset).

Only ZOGY difference-image products were made in this test.
NaNs were removed from the ZOGY inputs prior to ZOGY execution, and then restored in the ZOGY outputs (this is a new
requirement due to the presence of NaNs in the RimTimSim dataset).

Initially, only one image was processed for each of the two filters (jids 1 and 3),
in order to make the two required reference images for the single field associated with two filters in the RimTimSim dataset.
The reference images were made on the day before the date of this test.
These reference images were made beforehand to avoid needlessly having redundant reference images made when
processing the remaining images en masse in parallel.
For the jid=1 instance, PSF-fit catalog generation took 488 seconds (nsources=27420), and reference-image generation took 426 seconds (nframes=23),
comprising the majority of the run time.  The total run time of the jid=1 instance was 1038 seconds.

Processing was started for a later observation time than the very beginning,
in order to reserve some prior image frames for making reference images::

    export STARTDATETIME="2027-02-27 00:00:00"
    export ENDDATETIME="2027-04-25 00:00:00"

Numbers of exposure-SCA images processed for each available filter (fid = 4 and 7 only):

.. code-block::

    rimtimsimdb=> select fid,count(*) from l2files where dateobs >= '20270227' group by fid order by fid;
      fid | count
     -----+-------
        4 |    108
        7 |    107
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

The ``cov5percent`` QA metric for these two reference images is about 32.5 percent, but
because the entire dataset has small dithers and small image-angle variations, the footprint
of the image difference between science and reference images is almost 100 percent.


4/10/2026
************************************

A new set of rimtimsims, consisting of 131 FITS images, covering SCA number 2,
bandpass filter K213, and a single sky footprint/orientation (with dithers of no more
than a few pixels) that is associated with field number 4682737.  These rimtimsims
came already with fake-source injections, and therefore no additional fake sources
were injected by the RAPID pipeline.

SFFT was run without the ``--crossconv`` flag

Here are details about how the test was executed:

.. code-block::

    export DBNAME=rimtimsims2db
    export STARTDATETIME="2027-02-19 17:00:00"
    export ENDDATETIME="2027-04-24 23:00:00"
    export STARTREFIMMJDOBS=61450
    export ENDREFIMMJDOBS=61455.3
    export MINREFIMNFRAMES=6

    python3.11 /code/pipeline/virtualPipelineOperator.py 20260410 >& virtualPipelineOperator_20260410.out &

The ``STARTDATETIME`` and ``ENDDATETIME`` date/times exclude the first 10 images,
which are reserved for reference-image generation.

The following database query shows the RAPID pipelines ran normally for the portion that
generates the file products in parallel via the AWS Batch service.

.. code-block::

    rimtimsims2db=> select ppid,exitcode,count(*) from jobs where cast(launched as date) = '20260410' group by ppid, exitcode order by ppid, exitcode;

     ppid | exitcode | count
    ------+----------+-------
       15 |        0 |   121
       17 |        0 |   121
    (2 rows)


As shown in the table below for the longest running pipeline instance (jid = 91915),
executing AWAICGEN for reference-image generation
(depends on the number of input images; NFRAMES=10 for this case),
executing SFFT, (both science image and reference-image inputs),
and generating PhotUtils catalogs are the dominant factors
affecting pipeline performance.

Generating a PSF-fit catalog for the positive naive-difference image took an
anomalously long time (19.4 minutes!).

=================================================================  =====================
Pipeline step                                                      Execution time (sec)
=================================================================  =====================
Downloading science image                                             0.643
Uploading science image to product S3 bucket                          0.450
Downloading or generating reference image                           625.096
Uploading reference image to S3 product bucket                        2.158
Generating science-image catalog                                     23.402
Swarping images                                                       8.927
Uploading intermediate FITS files to product S3 bucket                2.653
Running bkgest on science image                                       7.801
Running gainMatchScienceAndReferenceImages                           30.194
Replacing NaNs, applying image offsets, etc.                          0.573
Running ZOGY                                                         39.462
Masking ZOGY difference image                                         0.952
Running SExtractor on positive ZOGY difference image                  4.315
Running SExtractor on negative ZOGY difference image                 21.474
Generating PSF-fit catalog on positive ZOGY difference image         82.193
Generating PSF-fit catalog on negative ZOGY difference image          6.587
Uploading main products to S3 bucket                                  5.502
Running SFFT                                                        149.377
Uploading SFFT difference image to S3 product bucket                  4.796
Running SExtractor on positive SFFT difference images                43.833
Running SExtractor on negative SFFT difference images                26.593
Uploading SFFT-diffimage SExtractor catalogs to S3 product bucket     1.241
Generating PSF-fit catalog on positive SFFT difference image         25.353
Generating PSF-fit catalog on negative SFFT difference image         12.718
Uploading SFFT-diffimage PSF-fit catalogs to S3 product bucket        0.131
Computing naive difference images                                     0.560
Uploading naive difference images to S3 product bucket                1.079
Running SExtractor on positive naive difference image                 4.027
Running SExtractor on negative naive difference image                18.012
Uploading SExtractor catalogs for naive difference images             0.892
Generating PSF-fit catalog on positive naive difference image      1164.406
Generating PSF-fit catalog on negative naive difference image       192.792
Uploading PSF-fit catalogs for naive difference images                1.837
Uploading products at pipeline end to S3 product bucket               0.037
Total elapsed time to run one instance of science pipeline         2510.065
=================================================================  =====================
