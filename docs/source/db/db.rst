RAPID Operations Database
####################################################

The RAPID pipeline utilizes a PostgreSQL database.

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
convenience (which is not generally needed).

A diagram of the database-table schema is given as follows:

.. image:: dbschema.png

Difference images and reference images are versioned in their
respective database tables, given by the version column, which is also
embedded in the filesystem path.  The best version is given by vbest, a smallint table
column that stores 0 for not best, 1 for best that is usually the
latest version, or 2 if the version is locked.  It is a matter of
policy whether old versions will be kept in the filesystem and/or
database (these could be removed at will).
