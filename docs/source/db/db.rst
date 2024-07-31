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
