Testing with RimTimSim Simulated Data
####################################################

Overview
************************************

Robby Wilson's simulated data, hereby referred to as RimTimSim data.
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

5/23/2025
************************************

Processing started at a later observation time than the very beginning to reserve some prior image frames for making reference images::

    export STARTDATETIME="2027-02-27 00:00:00"
    export ENDDATETIME="2027-04-25 00:00:00"

Here are metadata about the two reference images, for fid = 4 and 7:

.. code-block::

    rimtimsimdb=> select * from refimmeta where rfid in (select rfid from refimages where vbest>0);
     rfid |  field  |  hp6  |   hp9   | fid | nframes |     mjdobsmin     |     mjdobsmax     | npixsat | npixnan  |   clmean   |  clstddev   | clnoutliers |  gmedian   | datascale  |    gmin     |   gmax    | cov5percent | medncov |  medpixunc  | fwhmmedpix | fwhmminpix | fwhmmaxpix | nsexcatsources
    ------+---------+-------+---------+-----+---------+-------------------+-------------------+---------+----------+------------+-------------+-------------+------------+------------+-------------+-----------+-------------+---------+-------------+------------+------------+------------+----------------
      175 | 4682737 | 28823 | 1844720 |   7 |      50 | 61450.25169813307 |  61514.1287204572 |       0 | 33050440 | 0.14728972 | 0.095754854 |     1516632 | 0.12298192 | 0.12606817 | 0.019130437 | 221.08353 |    32.52232 |       0 | 0.014814872 |       3.75 |       0.07 |     770.63 |          46567
      179 | 4682737 | 28823 | 1844720 |   4 |      50 | 61450.51327337697 | 61515.96017282829 |       0 | 33054741 | 0.27793813 |  0.10819535 |     1497630 |  0.2568315 | 0.14013869 |  0.09188577 | 308.40265 |   32.516655 |       0 |  0.02155227 |       4.14 |       0.18 |      382.3 |          49152
    (2 rows)


.. code-block::


    rimtimsimdb=> select * from refimages where vbest>0;
     rfid |  field  |  hp6  |   hp9   | fid | ppid | version | vbest |                                  filename                                  | status |             checksum             |          created           | svid | avid | archivestatus | infobits
    ------+---------+-------+---------+-----+------+---------+-------+----------------------------------------------------------------------------+--------+----------------------------------+----------------------------+------+------+---------------+----------
      175 | 4682737 | 28823 | 1844720 |   7 |   15 |      91 |     1 | s3://rapid-product-files/20250522/jid195/awaicgen_output_mosaic_image.fits |      1 | ce3d5d4572168a8ff766707472b88f37 | 2025-05-22 08:36:26.505672 |    1 |      |             0 |        0
      179 | 4682737 | 28823 | 1844720 |   4 |   15 |      86 |     1 | s3://rapid-product-files/20250522/jid202/awaicgen_output_mosaic_image.fits |      1 | 711f2e90e02e0f55967175be476ed270 | 2025-05-22 08:36:27.553619 |    1 |      |             0 |        0
    (2 rows)
