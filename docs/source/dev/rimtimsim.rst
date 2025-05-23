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


Look-up table all of the filter IDs versus filter names included in the entire OpenUniverse dataset, except
that fid=8 (W146) is not included:

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
