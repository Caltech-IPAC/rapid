#! /bin/bash -x

#
# Script to build PostgreSQL database software (v. 15.2) from source code, start
# database server running, and then create a database (myopsdb) with $USER as superuser.
# The database server will be installed and running under the user account, not root.
# Connection parameters will be added to ~/.pgass file.
# Database files (mydb) will be in separate directory from database software (pg15.2).
# After this script is done, the superuser should "SET ROLE rapidreadrole;"
# in the psql client for non-superuser database querying.
# A non-superuser database user called apollo is created for use by pipeline operations.
#

#####################################################
# Parameters.
#####################################################

RAPID_PIPELINE_GIT_REPO=/home/ubuntu/rapid
PGPORT=5432
DB_ACTUAL_DATABASE_PATH=/data/db
DB_ACTUAL_DBNAME=rapidopsdb
DB_ACTUAL_PGPASSWORD=testpassword
DB_USER_APOLLO=apollo
DB_USER_APOLLO_PW=Arrow

DB_VERS=15.2
DB_FILE=postgresql-$DB_VERS
READLINE_FILE=readline-8.2
TERMCAP_FILE=termcap-1.3.1

DB_TAR_GZ_FILE_URL=https://ftp.postgresql.org/pub/source/v$DB_VERS/$DB_FILE.tar.gz
READLINE_TAR_GZ_FILE_URL=https://ftp.gnu.org/gnu/readline/$READLINE_FILE.tar.gz
TERMCAP_TAR_GZ_FILE_URL=https://ftp.gnu.org/gnu/termcap/$TERMCAP_FILE.tar.gz
DB_SW_ROOT=/home/ubuntu/dbsw


#####################################################
# Modify below this line only if you are an expert!
#####################################################

DB_BUILD_BASE=$DB_SW_ROOT/pg$DB_VERS
DB_BUILD_LIBS=$DB_SW_ROOT/termcap/build/lib:$DB_SW_ROOT/readline/build/lib:/usr/lib64
DB_BUILD_INCL=$DB_SW_ROOT/termcap/build/include:$DB_SW_ROOT/readline/build/include:/usr/include

echo DB_TAR_GZ_FILE_URL = $DB_TAR_GZ_FILE_URL
echo DB_BUILD_BASE = $DB_BUILD_BASE
echo DB_BUILD_LIBS = $DB_BUILD_LIBS
echo DB_BUILD_INCL = $DB_BUILD_INCL



mkdir -p $DB_SW_ROOT/readline
cd $DB_SW_ROOT/readline

wget --no-check-certificate $READLINE_TAR_GZ_FILE_URL

gunzip $READLINE_FILE.tar.gz
tar xvf $READLINE_FILE.tar

cd $READLINE_FILE
mkdir -p $DB_SW_ROOT/readline/build
./configure --prefix=$DB_SW_ROOT/readline/build
echo $?
make
echo $?
make install
echo $?



mkdir -p $DB_SW_ROOT/termcap
cd $DB_SW_ROOT/termcap

wget --no-check-certificate $TERMCAP_TAR_GZ_FILE_URL

gunzip $TERMCAP_FILE.tar.gz
tar xvf $TERMCAP_FILE.tar

cd $TERMCAP_FILE
mkdir -p $DB_SW_ROOT/termcap/build
./configure --prefix=$DB_SW_ROOT/termcap/build
echo $?
make
echo $?
make install
echo $?



mkdir -p $DB_BUILD_BASE
cd $DB_BUILD_BASE

echo PWD = $PWD

echo Building Postrgres database on $(date +\%Y\%m\%d)

printenv > buildDatabase.env



wget --no-check-certificate $DB_TAR_GZ_FILE_URL

gunzip $DB_FILE.tar.gz
tar xvf $DB_FILE.tar

cd $DB_BUILD_BASE/$DB_FILE

$DB_BUILD_BASE/$DB_FILE/configure --with-libraries=$DB_BUILD_LIBS  --with-includes=$DB_BUILD_INCL --prefix=$DB_BUILD_BASE

echo Exit code from configure = $?

pwd

make
echo Exit code from make = $?

make check
echo Exit code from make check = $?

make install
echo Exit code from make install = $?


export PATH=$DB_BUILD_BASE/bin:$PATH
export LD_LIBRARY_PATH=$DB_BUILD_BASE/lib:$LD_LIBRARY_PATH


echo PATH = $PATH
echo LD_LIBRARY_PATH = $LD_LIBRARY_PATH
echo PGPORT = $PGPORT

PGPASSFILE=$DB_ACTUAL_DATABASE_PATH/.pwfile.txt

mkdir -p $DB_ACTUAL_DATABASE_PATH
cd $DB_ACTUAL_DATABASE_PATH
mkdir dbdata
mkdir dblogs


echo $DB_ACTUAL_PGPASSWORD > $PGPASSFILE
chmod 600 $PGPASSFILE

echo "localhost:$PGPORT:*:$USER:$DB_ACTUAL_PGPASSWORD" >> ~/.pgpass
chmod 600 ~/.pgpass


$DB_BUILD_BASE/bin/initdb --pwfile=$PGPASSFILE -D $DB_ACTUAL_DATABASE_PATH/dbdata -A md5 -U $USER >& initdb.out

echo Exit code from $DB_BUILD_BASE/bin/initdb --pwfile=$PGPASSFILE -D $DB_ACTUAL_DATABASE_PATH/dbdata -A md5 -U $USER = $?

$DB_BUILD_BASE/bin/pg_ctl -D $DB_ACTUAL_DATABASE_PATH/dbdata -l $DB_ACTUAL_DATABASE_PATH/dblogs/log start

echo Exit code from $DB_BUILD_BASE/bin/pg_ctl -D $DB_ACTUAL_DATABASE_PATH/dbdata -l $DB_ACTUAL_DATABASE_PATH/dblogs/log start = $?

$DB_BUILD_BASE/bin/createdb -h localhost -p $PGPORT -U $USER $DB_ACTUAL_DBNAME

echo Exit code from $DB_BUILD_BASE/bin/createdb -h localhost -p $PGPORT -U $USER $DB_ACTUAL_DBNAME = $?

$DB_BUILD_BASE/bin/psql -h localhost -d $DB_ACTUAL_DBNAME -p $PGPORT -U $USER -c "select * from pg_tables;" >& test_query.out

echo Exit code from $DB_BUILD_BASE/bin/psql -h localhost -d $DB_ACTUAL_DBNAME -p $PGPORT -U $USER -c \"select \* from pg_tables\;\" = $?


#
# CREATE TABLESPACES
#

mkdir -p $DB_ACTUAL_DATABASE_PATH/tablespacedata1
mkdir -p $DB_ACTUAL_DATABASE_PATH/tablespaceindx1

echo CREATE TABLESPACE pipeline_data_01 LOCATION \'$DB_ACTUAL_DATABASE_PATH/tablespacedata1\'\; > tablespaces.sql
echo CREATE TABLESPACE pipeline_indx_01 LOCATION \'$DB_ACTUAL_DATABASE_PATH/tablespaceindx1\'\; >> tablespaces.sql

$DB_BUILD_BASE/bin/psql -h localhost -d $DB_ACTUAL_DBNAME -p $PGPORT -U $USER -f tablespaces.sql >& tablespaces.out

echo Exit code from $DB_BUILD_BASE/bin/psql -h localhost -d $DB_ACTUAL_DBNAME -p $PGPORT -U $USER -f tablespaces.sql = $?


#
# CREATE ROLES
#

echo CREATE ROLE rapidadminrole LOGIN SUPERUSER CREATEDB CREATEROLE\; > roles.sql
echo CREATE ROLE rapidporole\; >> roles.sql
echo CREATE ROLE rapidreadrole\; >> roles.sql
echo GRANT rapidadminrole to $USER\; >> roles.sql
echo GRANT rapidporole to $USER\; >> roles.sql
echo GRANT rapidreadrole to $USER\; >> roles.sql

$DB_BUILD_BASE/bin/psql -h localhost -d $DB_ACTUAL_DBNAME -p $PGPORT -U $USER -f roles.sql >& roles.out

echo Exit code from $DB_BUILD_BASE/bin/psql -h localhost -d $DB_ACTUAL_DBNAME -p $PGPORT -U $USER -f roles.sql = $?


#
# CREATE USER apollo with psql-client login privileges and unlimited connection for use by pipeline operator, which
# inherits from ROLE rapidporole generally allowed table grants for INSERT,UPDATE,SELECT,REFERENCES (no DELETE or TRUNCATE).
#

echo CREATE USER $DB_USER_APOLLO CONNECTION LIMIT -1 ENCRYPTED PASSWORD \'$DB_USER_APOLLO_PW\'\; > users.sql
echo GRANT rapidporole to $DB_USER_APOLLO\; >> users.sql

$DB_BUILD_BASE/bin/psql -h localhost -d $DB_ACTUAL_DBNAME -p $PGPORT -U $USER -f users.sql >& users.out

echo Exit code from $DB_BUILD_BASE/bin/psql -h localhost -d $DB_ACTUAL_DBNAME -p $PGPORT -U $USER -f users.sql = $?

echo "localhost:$PGPORT:$DB_ACTUAL_DBNAME:$DB_USER_APOLLO:$DB_USER_APOLLO_PW" >> ~/.pgpass
chmod 600 ~/.pgpass


#
# Install Q3C-library extension.
#

cd $DB_BUILD_BASE

git clone https://github.com/segasai/q3c

cd $DB_BUILD_BASE/q3c
make
echo $?

make install
echo $?

$DB_BUILD_BASE/bin/psql -h localhost -d $DB_ACTUAL_DBNAME -p $PGPORT -U $USER -c "CREATE EXTENSION q3c;"
echo $?

$DB_BUILD_BASE/bin/psql -h localhost -d $DB_ACTUAL_DBNAME -p $PGPORT -U $USER -c "SELECT * FROM pg_extension;"

exitcode=$?

echo $exitcode


#
# CREATE RAPID TABLES AND PROCEDURES
#

$DB_BUILD_BASE/bin/psql -h localhost -d $DB_ACTUAL_DBNAME -p $PGPORT -U $USER -f $RAPID_PIPELINE_GIT_REPO/database/schema/rapidOpsTables.sql >& rapidOpsTables.out

exitcode1=$?

echo Exit code from $DB_BUILD_BASE/bin/psql -h localhost -d $DB_ACTUAL_DBNAME -p $PGPORT -U $USER -f $RAPID_PIPELINE_GIT_REPO/database/schema/rapidOpsTables.sql = $exitcode1

$DB_BUILD_BASE/bin/psql -h localhost -d $DB_ACTUAL_DBNAME -p $PGPORT -U $USER -f $RAPID_PIPELINE_GIT_REPO/database/schema/rapidOpsTableGrants.sql >& rapidOpsTableGrants.out

exitcode2=$?

echo Exit code from $DB_BUILD_BASE/bin/psql -h localhost -d $DB_ACTUAL_DBNAME -p $PGPORT -U $USER -f $RAPID_PIPELINE_GIT_REPO/database/schema/rapidOpsTableGrants.sql = $exitcode2

$DB_BUILD_BASE/bin/psql -h localhost -d $DB_ACTUAL_DBNAME -p $PGPORT -U $USER -f $RAPID_PIPELINE_GIT_REPO/database/schema/rapidOpsProcs.sql >& rapidOpsProcs.out

exitcode3=$?

echo Exit code from $DB_BUILD_BASE/bin/psql -h localhost -d $DB_ACTUAL_DBNAME -p $PGPORT -U $USER -f $RAPID_PIPELINE_GIT_REPO/database/schema/rapidOpsProcs.sql = $exitcode3

$DB_BUILD_BASE/bin/psql -h localhost -d $DB_ACTUAL_DBNAME -p $PGPORT -U $USER -f $RAPID_PIPELINE_GIT_REPO/database/schema/rapidOpsProcGrants.sql >& rapidOpsProcGrants.out

exitcode4=$?

echo Exit code from $DB_BUILD_BASE/bin/psql -h localhost -d $DB_ACTUAL_DBNAME -p $PGPORT -U $USER -f $RAPID_PIPELINE_GIT_REPO/database/schema/rapidOpsProcGrants.sql = $exitcode4


#
# TERMINATE
#

if [ $exitcode -eq 0 ]
then

    echo
    echo ##################################################################
    echo "Congratulations! Database server is running and database $DB_ACTUAL_DBNAME created."
    echo
    echo Stop the server with this command:
    echo $DB_BUILD_BASE/bin/pg_ctl -D $DB_ACTUAL_DATABASE_PATH/dbdata -l $DB_ACTUAL_DATABASE_PATH/dblogs/log stop
    echo
    echo Your ~/.pgpass file has been augmented with database connection parameters.
    echo
    echo Put these three lines in your environment to run the psql client:
    echo "export PATH=$DB_BUILD_BASE/bin:\$PATH"
    echo "export LD_LIBRARY_PATH=$DB_BUILD_BASE/lib:\$LD_LIBRARY_PATH"
    echo "export PGPORT=$PGPORT"
    echo
    echo Here is how to connect to your new database:
    echo $DB_BUILD_BASE/bin/psql -h localhost -d $DB_ACTUAL_DBNAME -p $PGPORT -U $USER
    echo ##################################################################
    echo

    exit 0

else

    echo ##################################################################
    echo There was an error...
    echo ##################################################################

    exit 1

fi
