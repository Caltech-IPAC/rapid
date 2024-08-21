RAPID Operations Database
####################################################

Introduction
************************************

The RAPID pipeline utilizes a PostgreSQL database.  The Q3C library
has been installed as a plug-in for fast queries on sky position.

.. note::
    The database design described below is evolving and subject to change.

The database was built from source code and deployed on the
build machine using the following script:

.. code-block::

   /source-code/location/rapid/database/scripts/buildDatabase.sh

The database-schema files are located under the following path in the RAPID
git repository:

.. code-block::

   /source-code/location/rapid/database/schema

It includes SQL files to define datatabase tables, stored functions,
roles, grants, table spaces, and some basic database-table content.  It also
includes SQL files to drop tables and stored functions as a
convenience (which are not generally needed).

Schema
************************************

A diagram of the database-table schema is given as follows:

.. image:: dbschema.png


There are multiple provisions for indexing on sky position:

* Q3C indexing
* The field column in various tables store the Roman tessellation index for the associated sky position.
  With NSIDE=10, these are relatively large rectangular buckets that are approximately 4 degrees on a side.
* Healpix level-6 index (hp6), with an approximate resolution of 0.92 degrees (almost the width of the Roman WFI or 6 SCAs plus gaps).
* Healpix level-9 indices (hp9), with an approximate resolution of 0.11 degrees (almost the width of a Roman SCA).

Record Versioning
************************************

L2 files, difference images, and reference images are versioned in their
respective database tables (L2Files, DiffImages, and RefImages), given by the version column.  The version
is also embedded in the filesystem paths of the corresponding data files.
The best version is given by vbest, a smallint table
column that stores 0 for not best, 1 for best that is usually the
latest version, or 2 if the version is locked.  It is a matter of
policy whether old versions will be kept in the filesystem and/or
database (these could be removed at will).


Sky-Position Queries Using Q3C Library Functions
************************************

The L2FileMeta and DiffImages database tables store the image centers
(ra0, dec0) and their four corners (rai, deci, i=1,...,4).  Database
queries involving Q3C functions like the following can find all images that overlap a given
image, such as the one with rid = 152336 (rid = L2File primary key), where the (ra, dec) values
below are for that image's center and four corners:

.. code-block::

    select rid, q3c_dist(ra0, dec0, 11.08126328627515, -43.824964752037445) as dist
    from l2filemeta
    where fid = 1                  -- Database ID for F184 filter from Filters table.
    and sca = 2
    and q3c_radial_query(ra0, dec0, 11.08126328627515, -43.824964752037445, 0.18)
    and (q3c_poly_query(ra1, dec1, array[11.136885386567164, -43.900893936840234, 11.185362398873613, -43.78197810436912,11.025782901132052, -43.749009077867875, 10.97701495473218, -43.86785677863402])
    or q3c_poly_query(ra2, dec2, array[11.136885386567164, -43.900893936840234, 11.185362398873613, -43.78197810436912,11.025782901132052, -43.749009077867875, 10.97701495473218, -43.86785677863402])
    or q3c_poly_query(ra3, dec3, array[11.136885386567164, -43.900893936840234, 11.185362398873613, -43.78197810436912,11.025782901132052, -43.749009077867875, 10.97701495473218, -43.86785677863402])
    or q3c_poly_query(ra4, dec4, array[11.136885386567164, -43.900893936840234, 11.185362398873613, -43.78197810436912,11.025782901132052, -43.749009077867875, 10.97701495473218, -43.86785677863402])
    or q3c_poly_query(ra0, dec0, array[11.136885386567164, -43.900893936840234, 11.185362398873613, -43.78197810436912,11.025782901132052, -43.749009077867875, 10.97701495473218, -43.86785677863402]))
    and rid != 152336
    order by dist;


Once the relevant rids are found, the filenames can be looked up as follows:

.. code-block::

    select rid,filename
    from l2files
    where rid in (152336, 232345, 172211)
    order by rid;
