import os
import json
import psycopg2
import psycopg2.sql as sql
import re
import hashlib

debug = 1

########################################################################################################
# Common methods.
########################################################################################################


def md5(fname):
    hash_md5 = hashlib.md5()

    try:
        with open(fname, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()
    except:
        print("*** Error: Cannot open file to compute checksum =",fname,"; returning...")
        return(68)


def compute_checksum(fname,dbcksum=None):


    # See if file exists.

    isExist = os.path.exists(fname)

    if isExist == False:
        print('*** Error: File does not exist ({}); returning...'.format(fname))
        return 65


    # Compute checksum and optionally compare with that stored in database.

    cksum = md5(fname)

    if cksum == 68:
        return 68

    if debug == 1:
        print('cksum = {}'.format(cksum))

    if dbcksum is not None:
        if cksum == dbcksum:
            if debug == 1:
                print("File checksum is correct...")
            else:
                print('*** Error: File checksum is incorrect ({}); returning...'.format(fname))
                return 66

    return cksum


def get_db_credentials():

    """
    Resolve DB username/password the same way RAPIDDB.__init__ does: if
    RAPID_DB_SECRET_ID is set, fetch from AWS Secrets Manager (boto3 default
    credential chain); otherwise fall back to DBUSER/DBPASS env vars.

    Returns (dbuser,dbpass), or (None,None) if credentials could not be
    resolved.
    """

    dbuser = None
    dbpass = None

    db_secret_id = os.getenv('RAPID_DB_SECRET_ID')

    if db_secret_id is not None:

        try:
            import boto3
            secrets_client = boto3.client('secretsmanager')
            secret_value = secrets_client.get_secret_value(SecretId=db_secret_id)
            secret_dict = json.loads(secret_value['SecretString'])
            dbuser = secret_dict['username']
            dbpass = secret_dict['password']
        except Exception:
            print("*** Error: Could not fetch DB credentials from Secrets Manager secret {}; quitting...".format(db_secret_id))
            return None,None

    else:
        dbuser = os.getenv('DBUSER')
        dbpass = os.getenv('DBPASS')

    return dbuser,dbpass


########################################################################################################
########################################################################################################
########################################################################################################

class BorrowedConnection:

    """
    Wrapper around a psycopg2 connection that this class does NOT own.

    Every method in RAPIDDB below ends with the same two lines --

        if self.exit_code == 0:
            self.conn.commit()           # Commit database transaction

    -- and there are thirty-two of them.  That per-call commit is the whole
    contract of this module: each call is its own transaction, and when the
    call returns the row is durable.  It is a perfectly reasonable contract
    for a script that adds one exposure and exits.

    It is the WRONG contract when a caller needs several of these calls to
    land together or not at all.  Product registration is exactly that
    caller.  Registering a difference image is add_diffimage, then
    update_diffimage to set vBest, then register_diffimmeta -- and then, on
    a DIFFERENT connection held by the registration consumer, the watermark
    UPDATE that says this attempt has been registered.  With the per-call
    commit the product rows were committed the instant add_diffimage
    returned, so a crash before the watermark left the rows written and the
    attempt still a candidate.  The next pass registered them all over
    again, and minted a fresh version each time.  Two connections cannot be
    one transaction by construction, so the fix has two halves: the caller
    hands its own connection in (below), and this wrapper stops the commits
    that would break its unit of work apart.

    commit() and rollback() are therefore swallowed and counted, not
    forwarded.  The count is not decoration -- a test asserting that a
    registration wrote nothing outside its transaction needs to see that the
    calls were made and refused.  close() is swallowed for the same reason:
    the borrower does not get to close a connection it was lent.

    Why a wrapper rather than a flag the thirty-two commit sites consult:
    the sites are identical and uninteresting, and rewriting all of them
    would be thirty-two chances to miss one and thirty-two lines of diff
    over code that is not otherwise being touched.  More to the point, a
    flag only protects the sites that remember to check it -- the
    thirty-third, added later by someone who never read this comment, would
    silently reopen the defect.  Interposing at the connection means there
    is no commit path that bypasses the guard, because there is no other way
    to reach the driver.  Everything else (cursor(), isolation_level, the
    autocommit dance in vacuum_analyze_table) passes straight through by
    __getattr__, so the borrowed handle behaves like the real one in every
    respect except the one that matters.
    """

    def __init__(self,conn):

        self._conn = conn
        self.commits_suppressed = 0
        self.rollbacks_suppressed = 0

    def commit(self):

        # The borrower owns the transaction boundary.  Committing here would
        # end its unit of work in the middle, which is the defect.

        self.commits_suppressed += 1

    def rollback(self):

        # Equally not ours to call: rolling back here would discard work the
        # borrower did before it handed the connection over, and would do it
        # silently, since these methods report through self.exit_code rather
        # than by raising.  The borrower's own error path rolls back.

        self.rollbacks_suppressed += 1

    def close(self):

        # A borrowed connection is closed by whoever opened it.

        pass

    def __getattr__(self,name):

        return getattr(self._conn,name)


class RAPIDDB:

    """
    Class to facilitate execution of queries in the RAPID operations database.
    For each query a different method is defined.

    Returns exitcode:
         0 = Normal
         2 = Exception raised closing database connection
        64 = Cannot connect to database
        65 = Input file does not exist
        66 = File checksum does not match database checksum
        67 = Could not execute database query or no record(s) returned.
        68 = Could not open file to compute checksum.

    A caller that already holds a connection passes it as conn=; see
    BorrowedConnection above and the borrowing() classmethod below.  In that
    mode nothing here commits, nothing here rolls back, and nothing here
    calls exit() -- the caller owns the transaction and the process.

    **THIS CLASS IS FROZEN (conformance rule 17; implementation brief G).**

    No new capability lands here.  New database access goes through
    `rapid_db_connect.connect()` and a narrow repository over it -- see
    `pipeline/repositories/` for the first two, which is the shape the rest
    of this class's 5,000 lines migrate into over time.  Adding a
    thirty-third query method here would deepen exactly the surface that
    migration has to unwind, and would inherit this class's three
    incompatible error contracts (the exit_code flag, the `return None` on
    failure, and the raw driver exceptions from the bookkeeping queries in
    __init__) rather than the one contract the replacement has.

    Frozen is not deprecated: the existing methods work, their callers are
    not broken, and nothing here is scheduled for deletion.  What is
    forbidden is GROWTH.

    Changes that are still welcome: bug fixes, parameterization of
    interpolated SQL, and conversions of the kind __init__ has just had --
    the five `exit(64)` calls that terminated the interpreter from library
    code are now a raised `DBCredentialError` (which carries `exit_code`,
    so an entrypoint that owns exiting can still honour the codes above).
    """


########################################################################################################

    def __init__(self,conn=None):

        self.exit_code = 0
        self.conn = None


        # THE BORROWED-CONNECTION PATH.  Taken before any of the environment
        # reading below, and deliberately so: the env-var checks further down
        # call exit(64) straight out of library code, which takes the whole
        # process with them.  A caller that already has a working connection
        # has already proved the configuration is fine, and must never be at
        # risk of a hard exit for a variable it does not need.  This is also
        # why the credential lookup is skipped -- there is nothing to
        # authenticate, the connection is already open and authenticated.
        #
        # self.owns_connection records which mode we are in so close() can
        # tell "close the connection I opened" from "leave alone the
        # connection I was lent".

        if conn is not None:

            self.owns_connection = False
            self.conn = BorrowedConnection(conn)
            self.cur = self.conn.cursor()
            return

        self.owns_connection = True



        # Get database connection parameters from environment. DBSERVER/
        # DBPORT/DBNAME are always config, not secret, and stay plain env
        # reads with no hardcoded value: at scale, connections go through
        # a pgbouncer pooler on the DB host (port 6432) rather than
        # PostgreSQL directly (port 5432), so DBPORT must keep pointing at
        # whichever is correct for the deployment — set it in the caller's
        # environment, not defaulted here.
        #
        # Credentials: if RAPID_DB_SECRET_ID is set, fetch username/password
        # from AWS Secrets Manager (boto3 default credential chain — under
        # Batch this is the job role reading rapid/db/service/pipeline);
        # otherwise fall back to DBUSER/DBPASS env vars so local/dev usage
        # is unchanged.

        dbserver = os.getenv('DBSERVER')
        dbport = os.getenv('DBPORT')
        dbname = os.getenv('DBNAME')

        dbuser,dbpass = get_db_credentials()

        if dbuser is None and dbpass is None and os.getenv('RAPID_DB_SECRET_ID') is not None:
            self.exit_code = 64
            return

        print("dbserver,dbname,dbport,dbuser =",dbserver,dbname,dbport,dbuser)


        # LIBRARY CODE DOES NOT TERMINATE THE PROCESS (conformance rule 17).
        #
        # These five checks called exit(64) -- straight out of a constructor,
        # in a module 25 call sites import.  The exit-code CONTRACT is not
        # the defect and is unchanged: 64 still means "cannot connect to
        # database", and the class docstring above still documents the
        # family.  What was wrong was WHERE the contract was enforced.  A
        # library that exits cannot be caught, cannot be cleaned up after,
        # cannot report which of five variables was missing to a caller that
        # might know how to supply it, and cannot be tested without a
        # subprocess.  Worse, it took the process with it in contexts that
        # had no business dying: `pipeline/stages/alert_production.py:184-187`
        # records every alert job on the mock's first wave exiting 64 at a
        # RAPIDDB() line, because a Batch payload carries no DBSERVER.
        #
        # The exception carries the code it would have exited with, so the
        # entrypoints that legitimately own exiting can still honour the
        # contract -- `exit_code` on the exception, `sys.exit(exc.exit_code)`
        # at the process boundary.  That is the move rule 17 asks for: the
        # exit-code contract migrates to the entrypoints that own exiting,
        # rather than being abolished.
        #
        # WHY DBCredentialError AND NOT A NEW TYPE.  `rapid_db_connect.py`
        # already has the vocabulary -- DBError / DBUnavailable /
        # DBCredentialError, with `error_category` the runtime's taxonomy
        # serializer reads -- and a missing DBSERVER is precisely its
        # "config_invalid" case.  A second family here would be the third
        # error vocabulary in one package.  See that module's docstring for
        # which family applies where.

        # IMPORTED HERE, NOT AT MODULE SCOPE, and the direction is why:
        # `rapid_db_connect` imports `get_db_credentials` FROM THIS MODULE
        # (rapid_db_connect.py:64), so a module-level import back would be a
        # cycle -- and one that fails at interpreter start rather than at a
        # call site, taking down every importer of either module.  A
        # function-local import runs after both modules are initialized.
        from database.modules.utils.rapid_db_connect import DBCredentialError

        missing = [name for name,value in (('DBSERVER',dbserver),
                                           ('DBPORT',dbport),
                                           ('DBNAME',dbname),
                                           ('DBUSER',dbuser),
                                           ('DBPASS',dbpass))
                   if value is None]

        if missing:

            # Reported ALL AT ONCE rather than one exit per variable.  The
            # old form told an operator about DBSERVER, and only after they
            # fixed it did it mention DBPORT -- five round trips to learn
            # what one message can say.
            self.exit_code = 64
            raise DBCredentialError(
                "cannot connect to database: environment variable(s) not set: "
                + ", ".join(missing)
                + " (DBUSER/DBPASS may instead come from the "
                  "RAPID_DB_SECRET_ID secret's 'username'/'password')")


        # Connect to database

        try:
            self.conn = psycopg2.connect(host=dbserver,database=dbname,port=dbport,user=dbuser,password=dbpass)
        except:
            print("Could not connect to database...")
            self.exit_code = 64
            return


        # Open database cursor.

        self.cur = self.conn.cursor()


        # Select database version.

        q1 = 'SELECT version();'
        print('q1 = {}'.format(q1))
        self.cur.execute(q1)
        db_version = self.cur.fetchone()
        print('PostgreSQL database version = {}'.format(db_version))


        # Check database current_user.

        q2 = 'SELECT current_user;'
        print('q2 = {}'.format(q2))
        self.cur.execute(q2)
        for record in self.cur:
            print('record = {}'.format(record))


########################################################################################################

    @classmethod
    def borrowing(cls,conn):

        '''
        Build a handle over a connection the caller already owns.

        The named form of RAPIDDB(conn=...), for call sites where "borrowing"
        says what is happening more plainly than a keyword does -- notably the
        registrar factory in pipeline/registration, which hands the
        registration consumer's own connection to the ported product bodies so
        the product rows and the registered-watermark write land in one
        transaction instead of two.
        '''

        return cls(conn=conn)


########################################################################################################

    def close(self):

        '''
        Close database cursor and then connection.

        A BORROWED connection is not closed: its close() is a no-op (see
        BorrowedConnection), because whoever opened it is still using it and
        will close it when its own block ends.  The cursor IS closed either
        way -- that one we opened.
        '''

        try:
            self.cur.close()
        except (Exception, psycopg2.DatabaseError) as error:
            print(error)
            self.exit_code = 2
        finally:
            if self.conn is not None:
                self.conn.close()
                if getattr(self,'owns_connection',True):
                    print('Database connection closed.')
                else:
                    print('Borrowed database connection left open for its owner.')

########################################################################################################

    def is_connection_alive(self):

        try:
            # Open a temporary cursor and run a minimal query
            with self.conn.cursor() as cur:
                cur.execute("SELECT 1;")
            return True
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            return False

########################################################################################################

    def _doQuery(self, query, params=None):
        # This is a protected, internal method.
        # It handles the low-level details of executing the query.

        print('query = {}, params = {}'.format(query, params))

        self.cur.execute(query, params)

        try:
            records = self.cur.fetchall()
        except:
            records = None

        return records


########################################################################################################

    def vacuum_analyze_table(self,tablename):
        old_isolation_level = self.conn.isolation_level
        self.conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
        query = sql.SQL("VACUUM ANALYZE {tbl};").format(tbl=sql.Identifier(tablename))

        try:

            self._doQuery(query)
            self.conn.set_isolation_level(old_isolation_level)

        except Exception as e:

            print(f"*** Error in method vacuum_analyze_table: {e}")


        return

########################################################################################################

    def add_exposure(self,dateobs,mjdobs,field,hp6,hp9,filter,exptime,infobits,status):

        '''
        Add record in Exposures database table.
        '''

        self.exit_code = 0


        # Define query template.

        query =\
            "select * from addExposure(" +\
            "cast(%s as timestamp)," +\
            "cast(%s as double precision)," +\
            "cast(%s as integer)," +\
            "cast(%s as integer)," +\
            "cast(%s as integer)," +\
            "cast(%s as character varying(16))," +\
            "cast(%s as real), " +\
            "cast(%s as integer), " +\
            "cast(%s as smallint)) as " +\
            "(expid integer," +\
            " fid smallint);"


        # Query database.

        print('----> dateobs = {}'.format(dateobs))
        print('----> mjdobs = {}'.format(mjdobs))
        print('----> field = {}'.format(field))
        print('----> hp6 = {}'.format(hp6))
        print('----> hp9 = {}'.format(hp9))
        print('----> filter = {}'.format(filter))
        print('----> exptime = {}'.format(exptime))
        print('----> infobits = {}'.format(infobits))
        print('----> status = {}'.format(status))

        params = (dateobs, mjdobs, field, hp6, hp9, filter, exptime, infobits, status)

        print('query = {}, params = {}'.format(query, params))

        self.cur.execute(query, params)
        record = self.cur.fetchone()

        if record is not None:
            self.expid = record[0]
            self.fid = record[1]
        else:
            self.expid = None
            self.fid = None
            print("*** Error: Could not insert or update Exposures record; returning...")
            self.exit_code = 67
            return

        if self.exit_code == 0:
            self.conn.commit()           # Commit database transaction



########################################################################################################

    def add_l2file_fourth_order(self,expid,sca,field,hp6,hp9,fid,dateobs,mjdobs,exptime,infobits,
        status,filename,checksum,crval1,crval2,crpix1,crpix2,cd11,cd12,cd21,cd22,
        ctype1,ctype2,cunit1,cunit2,a_order,a_0_2,a_0_3,a_0_4,a_1_1,a_1_2,
        a_1_3,a_2_0,a_2_1,a_2_2,a_3_0,a_3_1,a_4_0,b_order,b_0_2,b_0_3,
        b_0_4,b_1_1,b_1_2,b_1_3,b_2_0,b_2_1,b_2_2,b_3_0,b_3_1,
        b_4_0,equinox,ra,dec,paobsy,pafpa,zptmag,skymean):

        '''
        Add record in L2files database table.
        '''

        self.exit_code = 0


        # Define query template.

        query =\
            "select * from addL2File(" +\
            "cast(%s as integer)," +\
            "cast(%s as smallint)," +\
            "cast(%s as integer)," +\
            "cast(%s as integer)," +\
            "cast(%s as integer)," +\
            "cast(%s as smallint)," +\
            "cast(%s as timestamp without time zone)," +\
            "cast(%s as double precision)," +\
            "cast(%s as real)," +\
            "cast(%s as integer)," +\
            "cast(%s as character varying(255))," +\
            "cast(%s as character varying(32))," +\
            "cast(%s as smallint)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as real)," +\
            "cast(%s as real)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as character varying(16))," +\
            "cast(%s as character varying(16))," +\
            "cast(%s as character varying(16))," +\
            "cast(%s as character varying(16))," +\
            "cast(%s as smallint)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as smallint)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as real)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as real)," +\
            "cast(%s as real)," +\
            "cast(%s as real)," +\
            "cast(%s AS real)) as " +\
            "(rid integer," +\
            " version smallint);"


        # Query database.

        print('----> expid = {}'.format(expid))
        print('----> sca = {}'.format(sca))
        print('----> filename = {}'.format(filename))

        params = (expid, sca, field, hp6, hp9, fid, dateobs, mjdobs, exptime, infobits,
                  filename, checksum, status, crval1, crval2, crpix1, crpix2, cd11, cd12, cd21, cd22,
                  ctype1, ctype2, cunit1, cunit2, a_order, a_0_2, a_0_3, a_0_4, a_1_1,
                  a_1_2, a_1_3, a_2_0, a_2_1, a_2_2, a_3_0, a_3_1, a_4_0, b_order, b_0_2, b_0_3,
                  b_0_4, b_1_1, b_1_2, b_1_3, b_2_0, b_2_1, b_2_2, b_3_0, b_3_1,
                  b_4_0, equinox, ra, dec, paobsy, pafpa, zptmag, skymean)

        print('query = {}, params = {}'.format(query, params))

        self.cur.execute(query, params)
        record = self.cur.fetchone()

        if record is not None:
            self.rid = record[0]
            self.version = record[1]
        else:
            self.rid = None
            self.version = None
            print("*** Error: Could not insert L2Files record; returning...")
            self.exit_code = 67
            return

        if self.exit_code == 0:
            self.conn.commit()           # Commit database transaction



########################################################################################################

    def add_l2file_fifth_order(self,expid,sca,field,hp6,hp9,fid,dateobs,mjdobs,exptime,infobits,
        status,filename,checksum,crval1,crval2,crpix1,crpix2,cd11,cd12,cd21,cd22,
        ctype1,ctype2,cunit1,cunit2,
        a_order,a_0_1,a_0_2,a_0_3,a_0_4,a_0_5,a_1_0,a_1_1,a_1_2,a_1_3,a_1_4,
        a_2_0,a_2_1,a_2_2,a_2_3,a_3_0,a_3_1,a_3_2,a_4_0,a_4_1,a_5_0,
        b_order,b_0_1,b_0_2,b_0_3,b_0_4,b_0_5,b_1_0,b_1_1,b_1_2,b_1_3,b_1_4,
        b_2_0,b_2_1,b_2_2,b_2_3,b_3_0,b_3_1,b_3_2,b_4_0,b_4_1,b_5_0,
        equinox,ra,dec,paobsy,pafpa,zptmag,skymean):

        '''
        Add record in L2files database table.
        '''

        self.exit_code = 0


        # Define query template.

        query =\
            "select * from addL2File(" +\
            "cast(%s as integer)," +\
            "cast(%s as smallint)," +\
            "cast(%s as integer)," +\
            "cast(%s as integer)," +\
            "cast(%s as integer)," +\
            "cast(%s as smallint)," +\
            "cast(%s as timestamp without time zone)," +\
            "cast(%s as double precision)," +\
            "cast(%s as real)," +\
            "cast(%s as integer)," +\
            "cast(%s as character varying(255))," +\
            "cast(%s as character varying(32))," +\
            "cast(%s as smallint)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as real)," +\
            "cast(%s as real)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as character varying(16))," +\
            "cast(%s as character varying(16))," +\
            "cast(%s as character varying(16))," +\
            "cast(%s as character varying(16))," +\
            "cast(%s as smallint)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as smallint)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as real)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as real)," +\
            "cast(%s as real)," +\
            "cast(%s as real)," +\
            "cast(%s AS real)) as " +\
            "(rid integer," +\
            " version smallint);"


        # Query database.

        print('----> expid = {}'.format(expid))
        print('----> sca = {}'.format(sca))
        print('----> filename = {}'.format(filename))

        params = (expid, sca, field, hp6, hp9, fid, dateobs, mjdobs, exptime, infobits,
                  filename, checksum, status, crval1, crval2, crpix1, crpix2, cd11, cd12, cd21, cd22,
                  ctype1, ctype2, cunit1, cunit2,
                  a_order, a_0_1, a_0_2, a_0_3, a_0_4, a_0_5, a_1_0, a_1_1, a_1_2, a_1_3, a_1_4,
                  a_2_0, a_2_1, a_2_2, a_2_3, a_3_0, a_3_1, a_3_2, a_4_0, a_4_1, a_5_0,
                  b_order, b_0_1, b_0_2, b_0_3, b_0_4, b_0_5, b_1_0, b_1_1, b_1_2, b_1_3, b_1_4,
                  b_2_0, b_2_1, b_2_2, b_2_3, b_3_0, b_3_1, b_3_2, b_4_0, b_4_1, b_5_0,
                  equinox, ra, dec, paobsy, pafpa, zptmag, skymean)

        print('query = {}, params = {}'.format(query, params))

        self.cur.execute(query, params)
        record = self.cur.fetchone()

        if record is not None:
            self.rid = record[0]
            self.version = record[1]
        else:
            self.rid = None
            self.version = None
            print("*** Error: Could not insert L2Files record; returning...")
            self.exit_code = 67
            return

        if self.exit_code == 0:
            self.conn.commit()           # Commit database transaction


########################################################################################################

    def update_l2file(self,rid,filename,checksum,status,version):

        '''
        Update record in L2files database table.
        '''

        self.exit_code = 0


        # Define query template.

        query =\
            "select * from updateL2File(" +\
            "cast(%s as integer)," +\
            "cast(%s as character varying(255))," +\
            "cast(%s as character varying(32))," +\
            "cast(%s as smallint)," +\
            "cast(%s AS smallint));"


        # Query database.

        print('----> rid = {}'.format(rid))
        print('----> filename = {}'.format(filename))
        print('----> checksum = {}'.format(checksum))
        print('----> status = {}'.format(status))
        print('----> version = {}'.format(version))

        params = (rid, filename, checksum, status, version)

        print('query = {}, params = {}'.format(query, params))


        # Execute query.

        try:
            self.cur.execute(query, params)

            try:
                for record in self.cur:
                    print(record)
            except:
                print("Nothing returned from database stored function; continuing...")

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error updating L2Files record ({}); skipping...'.format(error))
            self.exit_code = 67
            return

        if self.exit_code == 0:
            self.conn.commit()           # Commit database transaction


########################################################################################################

    def register_l2filemeta(self,rid,ra0,dec0,ra1,dec1,ra2,dec2,ra3,dec3,ra4,dec4,x,y,z,hp6,hp9,fid,sca,mjdobs):

        '''
        Insert or update record in L2FileMeta database table.
        '''

        self.exit_code = 0


        # Define query template.

        query =\
            "select * from registerL2FileMeta(" +\
            "cast(%s as integer)," +\
            "cast(%s as smallint)," +\
            "cast(%s as smallint)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s AS double precision)," +\
            "cast(%s AS integer)," +\
            "cast(%s AS integer)," +\
            "cast(%s as double precision));"


        # Query database.

        print('----> rid = {}'.format(rid))
        print('----> fid = {}'.format(fid))
        print('----> sca = {}'.format(sca))
        print('----> ra0 = {}'.format(ra0))
        print('----> dec0 = {}'.format(dec0))


        params = (rid, fid, sca, ra0, dec0, ra1, dec1, ra2, dec2, ra3, dec3, ra4, dec4, x, y, z, hp6, hp9, mjdobs)

        print('query = {}, params = {}'.format(query, params))


        # Execute query.

        try:
            self.cur.execute(query, params)

            try:
                for record in self.cur:
                    print(record)
            except:
                print("Nothing returned from database stored function; continuing...")

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error inserting or updating L2FileMeta record ({}); skipping...'.format(error))
            self.exit_code = 67
            return

        if self.exit_code == 0:
            self.conn.commit()           # Commit database transaction


########################################################################################################

    def get_all_l2filemeta(self):

        '''
        Get all records in L2FileMeta database table.
        '''

        self.exit_code = 0


        # Define query.

        query = "select rid,ra0,dec0 from L2FileMeta;"


        # Query database.

        print('query = {}'.format(query))


        # Execute query.

        try:
            self.cur.execute(query)

            try:
                records = []
                nrecs = 0
                for record in self.cur:
                    records.append(record)
                    nrecs += 1

                print("nrecs =",nrecs)

            except:
                print("Nothing returned from database query; continuing...")

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error getting all L2FileMeta records ({}); skipping...'.format(error))
            self.exit_code = 67
            return

        return records


########################################################################################################

    def update_l2filemeta_hp6(self,rid,hp6):

        '''
        Update hp6 index in L2FileMeta database record.
        '''

        self.exit_code = 0


        # Define query.

        query = "update L2FileMeta set hp6 = %s where rid = %s;"
        params = (hp6, rid)


        # Query database.

        print('----> rid = {}'.format(rid))
        print('----> hp6 = {}'.format(hp6))

        print('query = {}, params = {}'.format(query, params))


        # Execute query.

        try:
            self.cur.execute(query, params)

            try:
                records = []
                for record in self.cur:
                    records.append(record)
            except:
                print("Nothing returned from database query; continuing...")

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error updating L2FileMeta record ({}); skipping...'.format(error))
            self.exit_code = 67
            return

        if self.exit_code == 0:
            self.conn.commit()           # Commit database transaction


########################################################################################################

    def get_all_l2files_assoc_rid_with_fid_and_sca(self):

        '''
        Get all records in L2Files database table.
        '''

        self.exit_code = 0


        # Define query.

        query = "select rid,fid,sca from L2Files and vbest > 0;"


        # Query database.

        print('query = {}'.format(query))


        # Execute query.

        try:
            self.cur.execute(query)

            try:
                records = []
                nrecs = 0
                for record in self.cur:
                    records.append(record)
                    nrecs += 1

                print("nrecs =",nrecs)

            except:
                print("Nothing returned from database query; continuing...")

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error getting all L2Files records ({}); skipping...'.format(error))
            self.exit_code = 67
            return

        return records


########################################################################################################

    def update_l2filemeta_fid_sca(self,rid,fid,sca):

        '''
        Update fid and sca columns in L2FileMeta database record.
        '''

        self.exit_code = 0


        # Define query.

        query = "update L2FileMeta set fid = %s, sca = %s where rid = %s;"
        params = (fid, sca, rid)


        # Query database.

        print('----> rid = {}'.format(rid))
        print('----> fid = {}'.format(fid))
        print('----> sca = {}'.format(sca))

        print('query = {}, params = {}'.format(query, params))


        # Execute query.

        try:
            self.cur.execute(query, params)

            try:
                records = []
                for record in self.cur:
                    records.append(record)
            except:
                print("Nothing returned from database query; continuing...")

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error updating L2FileMeta record ({}); skipping...'.format(error))
            self.exit_code = 67
            return

        if self.exit_code == 0:
            self.conn.commit()           # Commit database transaction


########################################################################################################

    def update_l2filemeta_hp9(self,rid,hp9):

        '''
        Update hp9 index in L2FileMeta database record.
        '''


        # Define query.

        query = "update L2FileMeta set hp9 = %s where rid = %s;"
        params = (hp9, rid)


        # Query database.

        print('----> rid = {}'.format(rid))
        print('----> hp9 = {}'.format(hp9))

        print('query = {}, params = {}'.format(query, params))


        # Execute query.

        try:
            self.cur.execute(query, params)

            try:
                records = []
                for record in self.cur:
                    records.append(record)
            except:
                print("Nothing returned from database query; continuing...")

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error updating L2FileMeta record ({}); skipping...'.format(error))
            self.exit_code = 67
            return

        if self.exit_code == 0:
            self.conn.commit()           # Commit database transaction


########################################################################################################

    def get_all_l2files(self):

        '''
        Get all records in L2Files database table.
        '''

        self.exit_code = 0


        # Define query.

        query = "select a.rid, ra0, dec0 from L2Files a, L2FileMeta b where a.rid = b.rid and vbest > 0;"


        # Query database.

        print('query = {}'.format(query))


        # Execute query.

        try:
            self.cur.execute(query)

            try:
                records = []
                nrecs = 0
                for record in self.cur:
                    records.append(record)
                    nrecs += 1

                print("nrecs =",nrecs)

            except:
                print("Nothing returned from database query; continuing...")

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error getting all L2Files records ({}); skipping...'.format(error))
            self.exit_code = 67
            return

        return records


########################################################################################################

    def update_l2files_field_hp6_hp9(self,rid,field,hp6,hp9):

        '''
        Update field,hp6,hp9 indices in L2Files database record.
        '''

        self.exit_code = 0


        # Define query.

        query = "update L2Files set field = %s, hp6 = %s, hp9 = %s where rid = %s;"
        params = (field, hp6, hp9, rid)


        # Query database.

        print('----> rid = {}'.format(rid))
        print('----> field = {}'.format(field))
        print('----> hp6 = {}'.format(hp6))
        print('----> hp9 = {}'.format(hp9))

        print('query = {}, params = {}'.format(query, params))


        # Execute query.

        try:
            self.cur.execute(query, params)

            try:
                records = []
                for record in self.cur:
                    records.append(record)
            except:
                print("Nothing returned from database query; continuing...")

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error updating L2Files record ({}); skipping...'.format(error))
            self.exit_code = 67
            return

        if self.exit_code == 0:
            self.conn.commit()           # Commit database transaction


########################################################################################################

    def get_all_exposures(self):

        '''
        Get all records in Exposures database table.
        '''

        self.exit_code = 0


        # Define query.
        # Here we query the L2Files table for all exposures, since
        # RA_TARG, DEC_TARG are currently stored here.

        query = "select distinct expid, ra, dec from L2Files and vbest > 0;"


        # Query database.

        print('query = {}'.format(query))


        # Execute query.

        try:
            self.cur.execute(query)

            try:
                records = []
                nrecs = 0
                for record in self.cur:
                    records.append(record)
                    nrecs += 1

                print("nrecs =",nrecs)

            except:
                print("Nothing returned from database query; continuing...")

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error getting all Exposures records ({}); skipping...'.format(error))
            self.exit_code = 67
            return

        return records


########################################################################################################

    def update_exposures_field_hp6_hp9(self,expid,field,hp6,hp9):

        '''
        Update field,hp6,hp9 indices in Exposures database record.
        '''

        self.exit_code = 0


        # Define query.

        query = "update Exposures set field = %s, hp6 = %s, hp9 = %s where expid = %s;"
        params = (field, hp6, hp9, expid)


        # Query database.

        print('----> expid = {}'.format(expid))
        print('----> field = {}'.format(field))
        print('----> hp6 = {}'.format(hp6))
        print('----> hp9 = {}'.format(hp9))

        print('query = {}, params = {}'.format(query, params))


        # Execute query.

        try:
            self.cur.execute(query, params)

            try:
                records = []
                for record in self.cur:
                    records.append(record)
            except:
                print("Nothing returned from database query; continuing...")

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error updating Exposures record ({}); skipping...'.format(error))
            self.exit_code = 67
            return

        if self.exit_code == 0:
            self.conn.commit()           # Commit database transaction


########################################################################################################

    def get_l2filemeta_record(self,rid):

        '''
        Get record from L2FileMeta database table for given rid.
        '''

        self.exit_code = 0


        # Define query template.

        query =\
            "select sca,fid,ra0,dec0,ra1,dec1,ra2,dec2,ra3,dec3,ra4,dec4 from L2FileMeta where rid=%s;"


        # Query database.

        print('----> rid = {}'.format(rid))

        params = (rid,)

        print('query = {}, params = {}'.format(query, params))


        # Execute query.

        self.cur.execute(query, params)
        record = self.cur.fetchone()

        if record is not None:
            sca = record[0]
            fid = record[1]
            ra0 = record[2]
            dec0 = record[3]
            ra1 = record[4]
            dec1 = record[5]
            ra2 = record[6]
            dec2 = record[7]
            ra3 = record[8]
            dec3 = record[9]
            ra4 = record[10]
            dec4 = record[11]
        else:
            sca = None
            fid = None
            ra0 = None
            dec0 = None
            ra1 = None
            dec1 = None
            ra2 = None
            dec2 = None
            ra3 = None
            dec3 = None
            ra4 = None
            dec4 = None
            print("*** Error: Could not get L2FileMeta database record; returning...")
            self.exit_code = 67


        return sca,fid,ra0,dec0,ra1,dec1,ra2,dec2,ra3,dec3,ra4,dec4


########################################################################################################

    def get_overlapping_l2files(self,
                                rid,
                                fid,
                                mjdobs,
                                field_ra0,field_dec0,
                                field_ra1,field_dec1,
                                field_ra2,field_dec2,
                                field_ra3,field_dec3,
                                field_ra4,field_dec4,
                                radius_of_initial_cone_search=None,
                                start_mjdobs=None,
                                end_mjdobs=None):

        '''
        Query database for RIDs and distances from tile center for all science images that
        overlap the sky tile associated with the input science image and its filter
        that were acquired before the input science image.
        Returned list is ordered by distance from tile center.

        `rid` is the representative to EXCLUDE from the result, or None to
        exclude nothing. It is a query control, not a description of the
        image being built around — see `submission.gathering.
        _overlapping_l2files`, whose docstring explains why the reference
        stage wants no exclusion at all.

        NONE, NOT THE STRING 'null' (round-4 finding #3). The open case used
        to be asked for by passing the string, which selected a branch
        emitting `a.rid is not %s`; once this query was parameterized, that
        bound the string through the placeholder and PostgreSQL received
        `a.rid IS NOT 'null'`, which is a syntax error — the whole overlap
        query failed with exit_code 67 and the reference stage gathered
        nothing. It parsed historically only because the value was
        substituted literally, giving `IS NOT null`; the parameterization
        that closed the injection seam changed that silently.

        An open query now emits NO exclusion clause at all, which is what
        "exclude nothing" means and what `IS NOT null` was standing in for:
        rid is an integer column, so `IS NOT NULL` was true for every row it
        was ever asked about. Expressing it as the absence of a predicate
        rather than as a predicate that happens to be universally true also
        makes the two branches say what they mean.

        `start_mjdobs`/`end_mjdobs` are the half-open observation window the
        candidate frames must fall in, `[start, end)`. Passed in, not read
        from the environment: the window selects which frames enter a
        science product, so its home is release content with a submission-
        manifest override, and STARTREFIMMJDOBS/ENDREFIMMJDOBS are gone.
        Omitting them keeps this query's own historical meaning — everything
        observed before the representative — for the standalone callers.
        '''

        self.exit_code = 0


        # Radius of initial cone search, in angular degrees.

        if radius_of_initial_cone_search is None:
            radius_of_initial_cone_search = 0.18


        # Define query template.

        # TODO: This query will not actually give all overlapping images (however small a chance this may be).
        #       For example, an image corner may overlap on a sky tile that does not cover a tile center or corner.

        query =\
            "select a.rid,ra0,dec0,ra1,dec1,ra2,dec2,ra3,dec3,ra4,dec4,field, " +\
            "q3c_dist(ra0, dec0, cast(%s as double precision), cast(%s as double precision)) as dist " +\
            "from L2FileMeta a, L2Files b " +\
            "where a.rid = b.rid " +\
            "and a.fid = %s " +\
            "and status > 0 " +\
            "and vbest > 0 " +\
            "and q3c_radial_query(ra0, dec0, cast(%s as double precision), cast(%s as double precision), cast(%s as double precision)) " +\
            "and (q3c_poly_query(ra1, dec1, array[cast(%s as double precision), cast(%s as double precision)," +\
                                                 "cast(%s as double precision), cast(%s as double precision)," +\
                                                 "cast(%s as double precision), cast(%s as double precision)," +\
                                                 "cast(%s as double precision), cast(%s as double precision)]) " +\
            "or q3c_poly_query(ra2, dec2, array[cast(%s as double precision), cast(%s as double precision)," +\
                                               "cast(%s as double precision), cast(%s as double precision)," +\
                                               "cast(%s as double precision), cast(%s as double precision)," +\
                                               "cast(%s as double precision), cast(%s as double precision)]) " +\
            "or q3c_poly_query(ra3, dec3, array[cast(%s as double precision), cast(%s as double precision)," +\
                                               "cast(%s as double precision), cast(%s as double precision)," +\
                                               "cast(%s as double precision), cast(%s as double precision)," +\
                                               "cast(%s as double precision), cast(%s as double precision)]) " +\
            "or q3c_poly_query(ra4, dec4, array[cast(%s as double precision), cast(%s as double precision)," +\
                                               "cast(%s as double precision), cast(%s as double precision)," +\
                                               "cast(%s as double precision), cast(%s as double precision)," +\
                                               "cast(%s as double precision), cast(%s as double precision)]) " +\
            "or q3c_poly_query(ra0, dec0, array[cast(%s as double precision), cast(%s as double precision)," +\
                                               "cast(%s as double precision), cast(%s as double precision)," +\
                                               "cast(%s as double precision), cast(%s as double precision)," +\
                                               "cast(%s as double precision), cast(%s as double precision)])) " +\
            "and a.mjdobs >= %s " +\
            "and a.mjdobs < %s "

        # The exclusion is a clause or it is nothing. `exclude_rid` decides
        # BOTH the SQL and whether a value is bound for it, so the query text
        # and the parameter tuple cannot disagree about how many placeholders
        # there are.
        exclude_rid = rid is not None

        if exclude_rid:
            query += "and a.rid != %s " +\
                              "order by dist; "
        else:
            query += "order by dist; "


        # The observation window the reference image's inputs are drawn from.
        #
        # It used to be read here from STARTREFIMMJDOBS/ENDREFIMMJDOBS, with
        # `[0.0, mjdobs)` when neither was set.  That environment path is
        # deleted: the window selects which frames enter a science product,
        # and "nothing that can alter a science product is reachable from the
        # environment" (design/code-standards.md).  Its authoritative value is
        # release content, per-run overridable only through the submission
        # manifest's enumerated override field, and the caller — which is the
        # side that has both — passes the resolved pair in.
        #
        # `[0.0, mjdobs)` remains the default for a caller that passes
        # neither, which is what the standalone scripts still do: the window
        # ending at the representative's own observation is this query's
        # historical meaning, not a policy default substituted for an absent
        # variable.

        if start_mjdobs is None:
            start_mjdobs = 0.0
        if end_mjdobs is None:
            end_mjdobs = mjdobs


        # Formulate query params.

        print('----> rid = {}'.format(rid))
        print('----> fid = {}'.format(fid))
        print('----> radius_of_initial_cone_search = {}'.format(radius_of_initial_cone_search))

        params = (field_ra0, field_dec0, fid,
                  field_ra0, field_dec0, radius_of_initial_cone_search,
                  field_ra1, field_dec1, field_ra2, field_dec2, field_ra3, field_dec3, field_ra4, field_dec4,
                  field_ra1, field_dec1, field_ra2, field_dec2, field_ra3, field_dec3, field_ra4, field_dec4,
                  field_ra1, field_dec1, field_ra2, field_dec2, field_ra3, field_dec3, field_ra4, field_dec4,
                  field_ra1, field_dec1, field_ra2, field_dec2, field_ra3, field_dec3, field_ra4, field_dec4,
                  field_ra1, field_dec1, field_ra2, field_dec2, field_ra3, field_dec3, field_ra4, field_dec4,
                  start_mjdobs, end_mjdobs)

        # Bound only when the clause that reads it was emitted.
        if exclude_rid:
            params = params + (rid,)

        print('query = {}, params = {}'.format(query, params))


        # Execute query.

        try:
            self.cur.execute(query, params)

            try:
                records = []
                nrecs = 0
                for record in self.cur:
                    records.append(record)
                    nrecs += 1

                print("nrecs =",nrecs)

            except:
                print("Nothing returned from database query; continuing...")

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error from database method RAPIDDB.get_overlapping_l2files ({}); skipping...'.format(error))
            self.exit_code = 67
            return

        return records


########################################################################################################

    def get_l2file_created(self,rid):

        '''
        The L2Files row's own `created` timestamp for the given rid.

        THE `l2_available` MILESTONE'S SOURCE (integration ruling 6;
        design/observability.md: "l2_available carries the authoritative
        source-availability timestamp"). No SOC-side ingest/publication
        timestamp exists anywhere in this schema — L2FileMeta carries none,
        and `l2files.created timestamptz DEFAULT now()` (006) is the only
        timestamp column anywhere near "when RAPID first knew about this
        file". It is the row's OWN insert time, not a timestamp the SOC
        supplies — a proxy, not a direct measurement — but it is the best
        one the live schema names, and RAPID cannot know about an L2 file
        before its row exists, so it is a reasonable upper bound on true SOC
        availability.

        Returns the `created` value, or None if no row exists for this rid.
        '''

        self.exit_code = 0

        query = "select created from L2Files where rid = %s;"
        params = (rid,)

        print('query = {}, params = {}'.format(query, params))

        try:
            self.cur.execute(query, params)
            record = self.cur.fetchone()

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error getting L2Files.created for rid {}: {}'.format(rid,error))
            self.exit_code = 67
            return

        return record[0] if record is not None else None


########################################################################################################

    def get_info_for_l2file(self,rid):

        '''
        Query select columns in L2Files database table for given RID.
        '''

        self.exit_code = 0


        # Define query template.

        query =\
            "select filename,expid,sca,field,mjdobs,exptime,infobits,status,vbest,version " +\
            "from L2Files " +\
            "where rid = %s; "


        # Formulate query by substituting parameters into query template.

        print('----> rid = {}'.format(rid))


        params = (rid,)

        print('query = {}, params = {}'.format(query, params))


        # Execute query.

        self.cur.execute(query, params)
        record = self.cur.fetchone()

        if record is not None:
            filename = record[0]
            expid = record[1]
            sca = record[2]
            field = record[3]
            mjdobs = record[4]
            exptime = record[5]
            infobits = record[6]
            status = record[7]
            vbest = record[8]
            version = record[9]

        else:
            filename = None
            expid = None
            sca = None
            field = None
            mjdobs = None
            exptime = None
            infobits = None
            status = None
            vbest = None
            version = None
            print("*** Error: Could not get select columns from L2Files database record; returning...")
            self.exit_code = 67


        return record


########################################################################################################

    def get_best_reference_image(self,ppid,field,fid):

        '''
        Query RefImages database table for the best (latest unless version is locked) version of reference image.
        '''

        self.exit_code = 0


        # Define query template.

        query =\
            "select rfid,filename,infobits,version " +\
            "from RefImages " +\
            "where vbest > 0 " +\
            "and status > 0 " +\
            "and ppid = %s " +\
            "and field = %s " +\
            "and fid = %s; "


        # Formulate query by substituting parameters into query template.

        print('----> ppid = {}'.format(ppid))
        print('----> field = {}'.format(field))
        print('----> fid = {}'.format(fid))


        params = (ppid, field, fid)

        print('query = {}, params = {}'.format(query, params))


        # Execute query.

        self.cur.execute(query, params)
        record = self.cur.fetchone()

        record_dict = {}

        if record is not None:
            record_dict["rfid"] = record[0]
            record_dict["filename"] = record[1]
            record_dict["infobits"] = record[2]
            record_dict["version"] = record[3]

        else:
            print("*** Message: No best RefImages database record found; continuing...")
            self.exit_code = 7


        return record_dict


########################################################################################################

    def start_job(self,ppid,fid,expid,field,sca,rid,machine='null',slurm='null'):

        '''
        Insert or update record in Jobs database table.  Return job ID.
        '''

        self.exit_code = 0


        # Define query template.

        query =\
            "select jid from startJob(" +\
            "cast(%s as smallint)," +\
            "cast(%s as smallint)," +\
            "cast(%s as integer)," +\
            "cast(%s as integer)," +\
            "cast(%s as smallint)," +\
            "cast(%s as integer), " +\
            "cast(%s as smallint), " +\
            "cast(%s as integer)) as jid;"


        # Query database.

        print('----> ppid = {}'.format(ppid))
        print('----> fid = {}'.format(fid))
        print('----> expid = {}'.format(expid))
        print('----> field = {}'.format(field))
        print('----> sca = {}'.format(sca))
        print('----> rid = {}'.format(rid))

        params = (ppid, fid, expid, field, sca, rid, machine, slurm)

        print('query = {}, params = {}'.format(query, params))

        self.cur.execute(query, params)
        record = self.cur.fetchone()

        if record is not None:
            jid = record[0]
        else:
            jid = None
            print("*** Error: Could not insert or update Jobs record; returning...")
            self.exit_code = 67
            return

        if self.exit_code == 0:
            self.conn.commit()           # Commit database transaction

        return jid


########################################################################################################

    def end_job(self,jid,job_exitcode,aws_batch_job_id,started,ended=None):

        '''
        Register exitcode and end timestamp in Jobs database table.  Return void.
        '''

        self.exit_code = 0


        # Define query template.

        if ended is None:

            query =\
                "select from endJob(" +\
                "cast(%s as integer)," +\
                "cast(%s as smallint)," +\
                "cast(%s as varchar(64)));"

            params = (jid, job_exitcode, aws_batch_job_id)

        else:

            query =\
                "select from endJob(" +\
                "cast(%s as integer)," +\
                "cast(%s as smallint)," +\
                "cast(%s as varchar(64)),"+\
                "cast(%s as timestamp),"+\
                "cast(%s as timestamp));"

            params = (jid, job_exitcode, aws_batch_job_id, started, ended)


        # Query database.

        print('----> jid = {}'.format(jid))
        print('----> job_exitcode = {}'.format(job_exitcode))

        print('query = {}, params = {}'.format(query, params))

        self.cur.execute(query, params)
        record = self.cur.fetchone()

        if record is not None:
            print("*** Message: Successfully executed stored funtion endJob; returning...")
        else:
            jid = None
            print("*** Error: Could not execute stored funtion endJob; returning...")
            self.exit_code = 67
            return

        if self.exit_code == 0:
            self.conn.commit()           # Commit database transaction

        return jid


########################################################################################################

    def update_job_with_aws_batch_job_id(self,jid,aws_batch_job_id):

        '''
        Update awsbatchjobid in Jobs database record.
        '''

        self.exit_code = 0


        # Define query.

        query = "update Jobs set awsbatchjobid = %s where jid = %s;"
        params = (aws_batch_job_id, jid)


        # Query database.

        print('----> jid = {}'.format(jid))
        print('----> awsbatchjobid = {}'.format(aws_batch_job_id))

        print('query = {}, params = {}'.format(query, params))


        # Execute query.

        try:
            self.cur.execute(query, params)

            try:
                records = []
                for record in self.cur:
                    records.append(record)
            except:
                print("Nothing returned from database query; continuing...")

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error updating Jobs record ({}); skipping...'.format(error))
            self.exit_code = 67
            return

        if self.exit_code == 0:
            self.conn.commit()           # Commit database transaction


########################################################################################################

    def add_refimage(self,ppid,field,fid,hp6,hp9,infobits,status,filename,checksum,
        attempt_id=None,registered_record_sequence=None):

        '''
        Add record in RefImages database table.

        attempt_id and registered_record_sequence are the ATTEMPT IDENTITY of
        the registration that is inserting this row, and they are what makes a
        replayed registration idempotent (migration 018).  Passed through, the
        stored function looks for an existing RefImages row carrying that same
        pair BEFORE it mints max(version)+1: the same attempt registered again
        at the same record sequence gets the row it already has, and only a
        HIGHER sequence -- a supersession, which is a genuinely new
        registration -- mints a new version.  Without them the second pass over
        the same attempt inserted a duplicate reference image at version+1 and
        pointed vBest at it.

        Both default to None so every existing caller is unaffected: the stored
        function defaults them too, and a call that omits them behaves exactly
        as it did before -- mint a version, insert, return.  That is the right
        default for callers that are not registering an attempt's products,
        because there is no attempt for the row to be idempotent with respect
        to.
        '''

        self.exit_code = 0


        # Define query template.

        query =\
            "select * from addRefImage(" +\
            "cast(%s as integer)," +\
            "cast(%s as integer)," +\
            "cast(%s as integer)," +\
            "cast(%s as smallint)," +\
            "cast(%s as smallint)," +\
            "cast(%s as integer)," +\
            "cast(%s as character varying(255))," +\
            "cast(%s as character varying(32))," +\
            "cast(%s as smallint)," +\
            "cast(%s as bigint)," +\
            "cast(%s as integer)) as " +\
            "(rfid integer," +\
            " version smallint);"


        # Query database.

        print('----> ppid = {}'.format(ppid))
        print('----> field = {}'.format(field))
        print('----> filename = {}'.format(filename))
        print('----> attempt_id = {}'.format(attempt_id))
        print('----> registered_record_sequence = {}'.format(registered_record_sequence))


        params = (field, hp6, hp9, fid, ppid, infobits, filename, checksum, status,
                  attempt_id, registered_record_sequence)

        print('query = {}, params = {}'.format(query, params))

        self.cur.execute(query, params)
        record = self.cur.fetchone()

        if record is not None:
            self.rfid = record[0]
            self.version = record[1]
        else:
            self.rfid = None
            self.version = None
            print("*** Error: Could not insert RefImages record; returning...")
            self.exit_code = 67
            return

        if self.exit_code == 0:
            self.conn.commit()           # Commit database transaction


########################################################################################################

    def update_refimage(self,rfid,filename,checksum,status,version):

        '''
        Update record in RefImages database table.
        '''

        self.exit_code = 0


        # Define query template.

        query =\
            "select * from updateRefImage(" +\
            "cast(%s as integer)," +\
            "cast(%s as character varying(255))," +\
            "cast(%s as character varying(32))," +\
            "cast(%s as smallint)," +\
            "cast(%s AS smallint));"


        # Query database.

        print('----> rfid = {}'.format(rfid))
        print('----> filename = {}'.format(filename))
        print('----> checksum = {}'.format(checksum))
        print('----> status = {}'.format(status))
        print('----> version = {}'.format(version))


        params = (rfid, filename, checksum, status, version)

        print('query = {}, params = {}'.format(query, params))


        # Execute query.

        try:
            self.cur.execute(query, params)

            try:
                for record in self.cur:
                    print(record)
            except:
                print("Nothing returned from database stored function; continuing...")

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error updating RefImages record ({}); skipping...'.format(error))
            self.exit_code = 67
            return

        if self.exit_code == 0:
            self.conn.commit()           # Commit database transaction


########################################################################################################

    def get_best_psf(self,sca,fid):

        '''
        Query PSFs database table for the best (latest unless version is locked) version of PSF.
        '''

        self.exit_code = 0


        # Define query template.

        query =\
            "select psfid,filename " +\
            "from PSFs " +\
            "where vbest > 0 " +\
            "and status > 0 " +\
            "and sca = %s " +\
            "and fid = %s; "


        # Formulate query by substituting parameters into query template.

        print('----> sca = {}'.format(sca))
        print('----> fid = {}'.format(fid))


        params = (sca, fid)

        print('query = {}, params = {}'.format(query, params))


        # Execute query.

        self.cur.execute(query, params)
        record = self.cur.fetchone()

        if record is not None:
            psfid = record[0]
            filename = record[1]

        else:
            psfid = None
            filename = None

            print("*** Error: Could not get best PSFs database record; continuing...")
            self.exit_code = 67


        return psfid,filename


########################################################################################################

    def get_info_for_job(self,jid):

        '''
        Query select columns in Jobs database table for given JID.
        '''

        self.exit_code = 0


        # Define query template.

        query =\
            "select ppid,rid,expid,sca,field,fid,started,ended,status,exitcode " +\
            "from Jobs " +\
            "where jid = %s; "


        # Formulate query by substituting parameters into query template.

        print('----> jid = {}'.format(jid))


        params = (jid,)

        print('query = {}, params = {}'.format(query, params))


        # Execute query.

        self.cur.execute(query, params)
        record = self.cur.fetchone()

        record_dict = {}

        if record is not None:
            record_dict["ppid"] = record[0]
            record_dict["rid"] = record[1]
            record_dict["expid"] = record[2]
            record_dict["sca"] = record[3]
            record_dict["field"] = record[4]
            record_dict["fid"] = record[5]
            record_dict["started"] = record[6]
            record_dict["ended"] = record[7]
            record_dict["status"] = record[8]
            record_dict["exitcode"] = record[9]

        else:
            print("*** Error: Could not get select columns from Jobs database record; returning...")
            self.exit_code = 67


        return record_dict


########################################################################################################

    def add_diffimage(self,rid,ppid,rfid,infobitssci,infobitsref,
        ra0,dec0,ra1,dec1,ra2,dec2,ra3,dec3,ra4,dec4,status,filename,checksum,
        attempt_id=None,registered_record_sequence=None):

        '''
        Add record in DiffImages database table.

        attempt_id and registered_record_sequence carry the same meaning here
        as in add_refimage: the identity of the registration inserting the row,
        used by the stored function to find-or-insert on that pair before
        minting max(version)+1, so a replayed registration returns the row it
        already wrote instead of a duplicate at a new version.  A higher
        sequence still mints a new version, which is how supersession keeps
        working.  Both optional, so callers that are not registering an
        attempt's products are unchanged.
        '''

        self.exit_code = 0


        # Define query template.

        query =\
            "select * from addDiffImage(" +\
            "cast(%s as integer)," +\
            "cast(%s as smallint)," +\
            "cast(%s as integer)," +\
            "cast(%s as integer)," +\
            "cast(%s as integer)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as double precision)," +\
            "cast(%s as character varying(255))," +\
            "cast(%s as character varying(32))," +\
            "cast(%s as smallint)," +\
            "cast(%s as bigint)," +\
            "cast(%s as integer)) as " +\
            "(pid integer," +\
            " version smallint);"


        # Query database.

        print('----> rid = {}'.format(rid))
        print('----> ppid = {}'.format(ppid))
        print('----> rfid = {}'.format(rfid))
        print('----> filename = {}'.format(filename))
        print('----> attempt_id = {}'.format(attempt_id))
        print('----> registered_record_sequence = {}'.format(registered_record_sequence))


        params = (rid, ppid, rfid, infobitssci, infobitsref, ra0, dec0, ra1, dec1, ra2, dec2, ra3, dec3, ra4, dec4, filename, checksum, status,
                  attempt_id, registered_record_sequence)

        print('query = {}, params = {}'.format(query, params))

        self.cur.execute(query, params)
        record = self.cur.fetchone()

        if record is not None:
            self.pid = record[0]
            self.version = record[1]
        else:
            self.pid = None
            self.version = None
            print("*** Error: Could not insert DiffImages record; returning...")
            self.exit_code = 67
            return

        if self.exit_code == 0:
            self.conn.commit()           # Commit database transaction


########################################################################################################

    def update_diffimage(self,pid,filename,checksum,status,version):

        '''
        Update record in DiffImages database table.
        '''

        self.exit_code = 0


        # Define query template.

        query =\
            "select * from updateDiffImage(" +\
            "cast(%s as integer)," +\
            "cast(%s as character varying(255))," +\
            "cast(%s as character varying(32))," +\
            "cast(%s as smallint)," +\
            "cast(%s AS smallint));"


        # Query database.

        print('----> pid = {}'.format(pid))
        print('----> filename = {}'.format(filename))
        print('----> checksum = {}'.format(checksum))
        print('----> status = {}'.format(status))
        print('----> version = {}'.format(version))


        params = (pid, filename, checksum, status, version)

        print('query = {}, params = {}'.format(query, params))


        # Execute query.

        try:
            self.cur.execute(query, params)

            try:
                for record in self.cur:
                    print(record)
            except:
                print("Nothing returned from database stored function; continuing...")

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error updating DiffImages record ({}); skipping...'.format(error))
            self.exit_code = 67
            return

        if self.exit_code == 0:
            self.conn.commit()           # Commit database transaction


########################################################################################################

    def register_refimimage(self,rfid,rid):

        '''
        Insert record in RefImImages database table.
        '''

        self.exit_code = 0


        # Define query template.

        query =\
            "select * from registerRefImImage(" +\
            "cast(%s as integer)," +\
            "cast(%s AS integer));"


        # Query database.

        print('----> rfid = {}'.format(rfid))
        print('----> rid = {}'.format(rid))


        params = (rfid, rid)

        print('query = {}, params = {}'.format(query, params))


        # Execute query.

        try:
            self.cur.execute(query, params)

            try:
                for record in self.cur:
                    print(record)
            except:
                print("Nothing returned from database stored function; continuing...")

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error inserting RefImImages record ({}); skipping...'.format(error))
            self.exit_code = 67
            return

        if self.exit_code == 0:
            self.conn.commit()           # Commit database transaction


########################################################################################################

    def get_distinct_fid_sca_from_psfs(self):

        '''
        Select all distinct fid, sca pairs in PSFs database table.
        '''

        self.exit_code = 0


        # Define query.

        query = "select distinct fid, sca from PSFs order by fid, sca;"


        # Query database.

        print('query = {}'.format(query))


        # Execute query.

        try:
            self.cur.execute(query)

            try:
                records = []
                nrecs = 0
                for record in self.cur:
                    records.append(record)
                    nrecs += 1

                print("nrecs =",nrecs)

            except:
                print("Nothing returned from database query; continuing...")

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error getting all distinct fid, sca pairs in PSFs database table ({}); skipping...'.format(error))
            self.exit_code = 67
            return

        return records


########################################################################################################

    def register_refimcatalog(self,
                              rfid,
                              ppid,
                              cattype,
                              field,
                              hp6,
                              hp9,
                              fid,
                              status,
                              filename,
                              checksum):

        '''
        Add or update record in RefImCatalogs database table.
        '''

        self.exit_code = 0


        # Define query template.

        query =\
            "select * from registerRefImCatalog(" +\
            "cast(%s as integer)," +\
            "cast(%s as smallint)," +\
            "cast(%s as smallint)," +\
            "cast(%s as integer)," +\
            "cast(%s as integer)," +\
            "cast(%s as integer)," +\
            "cast(%s as smallint)," +\
            "cast(%s as character varying(255))," +\
            "cast(%s as character varying(32))," +\
            "cast(%s as smallint)) as " +\
            "(rfcatid integer," +\
            " svid smallint);"


        # Query database.

        print('----> rfid = {}'.format(rfid))
        print('----> ppid = {}'.format(ppid))
        print('----> cattype = {}'.format(cattype))
        print('----> field = {}'.format(field))
        print('----> filename = {}'.format(filename))


        params = (rfid, ppid, cattype, field, hp6, hp9, fid, filename, checksum, status)

        print('query = {}, params = {}'.format(query, params))

        self.cur.execute(query, params)
        record = self.cur.fetchone()

        if record is not None:
            self.rfcatid = record[0]
            self.svid = record[1]
        else:
            self.rfcatid = None
            self.svid = None
            print("*** Error: Could not register RefImCatalogs record; returning...")
            self.exit_code = 67
            return

        if self.exit_code == 0:
            self.conn.commit()           # Commit database transaction


########################################################################################################

    def register_diffimmeta(self,
                            pid,
                            fid,
                            sca,
                            field,
                            hp6,
                            hp9,
                            nsexcatsources,
                            scalefacref,
                            dxrmsfin,
                            dyrmsfin,
                            dxmedianfin,
                            dymedianfin):

        '''
        Insert or update record in DiffImMeta database table.
        '''

        self.exit_code = 0


        # Define query template.

        query =\
            "select * from registerDiffImMeta(" +\
            "cast(%s as integer)," +\
            "cast(%s as smallint)," +\
            "cast(%s as smallint)," +\
            "cast(%s AS integer)," +\
            "cast(%s AS integer)," +\
            "cast(%s AS integer)," +\
            "cast(%s AS integer)," +\
            "cast(%s AS real)," +\
            "cast(%s AS real)," +\
            "cast(%s AS real)," +\
            "cast(%s AS real)," +\
            "cast(%s AS real));"


        # Query database.

        print('----> pid = {}'.format(pid))
        print('----> fid = {}'.format(fid))
        print('----> sca = {}'.format(sca))
        print('----> field = {}'.format(field))
        print('----> hp6 = {}'.format(hp6))
        print('----> hp9 = {}'.format(hp9))
        print('----> nsexcatsources = {}'.format(nsexcatsources))
        print('----> scalefacref = {}'.format(scalefacref))
        print('----> dxrmsfin = {}'.format(dxrmsfin))
        print('----> dyrmsfin = {}'.format(dyrmsfin))
        print('----> dxmedianfin = {}'.format(dxmedianfin))
        print('----> dymedianfin = {}'.format(dymedianfin))


        params = (pid, fid, sca, field, hp6, hp9, nsexcatsources, scalefacref, dxrmsfin, dyrmsfin, dxmedianfin, dymedianfin)

        print('query = {}, params = {}'.format(query, params))


        # Execute query.

        try:
            self.cur.execute(query, params)

            try:
                for record in self.cur:
                    print(record)
            except:
                print("Nothing returned from database stored function; continuing...")

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error inserting or updating DiffImMeta record ({}); skipping...'.format(error))
            self.exit_code = 67
            return

        if self.exit_code == 0:
            self.conn.commit()           # Commit database transaction


########################################################################################################

    def get_l2files_records_for_expid(self,expid):

        '''
        Query database for all L2Files records associated with the given exposure ID.
        '''

        self.exit_code = 0


        # Define query.

        query = "select rid,sca,fid,mjdobs from L2Files where expid = %s and vbest > 0;"
        params = (expid,)


        # Query database.

        print('query = {}, params = {}'.format(query, params))


        # Execute query.

        try:
            self.cur.execute(query, params)

            try:
                records = []
                nrecs = 0
                for record in self.cur:
                    records.append(record)
                    nrecs += 1

                print("nrecs =",nrecs)

            except:
                print("Nothing returned from database query; continuing...")

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error getting all L2Files records for given exposure ID ({}); skipping...'.format(error))
            self.exit_code = 67
            return

        return records


########################################################################################################

    def register_refimmeta(self,
                           rfid,
                           fid,
                           field,
                           hp6,
                           hp9,
                           nframes,
                           mjdobsmin,
                           mjdobsmax,
                           npixsat,
                           npixnan,
                           clmean,
                           clstddev,
                           clnoutliers,
                           gmedian,
                           datascale,
                           gmin,
                           gmax,
                           cov5percent,
                           medncov,
                           medpixunc,
                           fwhmmedpix,
                           fwhmminpix,
                           fwhmmaxpix,
                           nsexcatsources):

        '''
        Insert or update record in RefImMeta database table.
        '''

        self.exit_code = 0


        # Define query template.

        query =\
            "select * from registerRefImMeta(" +\
            "cast(%s as integer)," +\
            "cast(%s as smallint)," +\
            "cast(%s AS integer)," +\
            "cast(%s AS integer)," +\
            "cast(%s AS integer)," +\
            "cast(%s AS smallint)," +\
            "cast(%s AS double precision)," +\
            "cast(%s AS double precision)," +\
            "cast(%s AS integer)," +\
            "cast(%s AS integer)," +\
            "cast(%s AS real)," +\
            "cast(%s AS real)," +\
            "cast(%s AS integer)," +\
            "cast(%s AS real)," +\
            "cast(%s AS real)," +\
            "cast(%s AS real)," +\
            "cast(%s AS real)," +\
            "cast(%s AS real)," +\
            "cast(%s AS real)," +\
            "cast(%s AS real)," +\
            "cast(%s AS real)," +\
            "cast(%s AS real)," +\
            "cast(%s AS real)," +\
            "cast(%s AS integer));"


        # Query database.

        print('----> rfid = {}'.format(rfid))
        print('----> fid = {}'.format(fid))
        print('----> field = {}'.format(field))
        print('----> hp6 = {}'.format(hp6))
        print('----> hp9 = {}'.format(hp9))
        print('----> nframes = {}'.format(nframes))
        print('----> mjdobsmin = {}'.format(mjdobsmin))
        print('----> mjdobsmax = {}'.format(mjdobsmax))
        print('----> npixsat = {}'.format(npixsat))
        print('----> npixnan = {}'.format(npixnan))
        print('----> clmean = {}'.format(clmean))
        print('----> clstddev = {}'.format(clstddev))
        print('----> clnoutliers = {}'.format(clnoutliers))
        print('----> gmedian = {}'.format(gmedian))
        print('----> datascale = {}'.format(datascale))
        print('----> gmin = {}'.format(gmin))
        print('----> gmax = {}'.format(gmax))
        print('----> cov5percent = {}'.format(cov5percent))
        print('----> medncov = {}'.format(medncov))
        print('----> medpixunc = {}'.format(medpixunc))
        print('----> fwhmmedpix = {}'.format(fwhmmedpix))
        print('----> fwhmminpix = {}'.format(fwhmminpix))
        print('----> fwhmmaxpix = {}'.format(fwhmmaxpix))
        print('----> nsexcatsources = {}'.format(nsexcatsources))


        params = (rfid, fid, field, hp6, hp9, nframes, mjdobsmin, mjdobsmax, npixsat, npixnan, clmean, clstddev, clnoutliers, gmedian, datascale, gmin, gmax, cov5percent, medncov, medpixunc, fwhmmedpix, fwhmminpix, fwhmmaxpix, nsexcatsources)

        print('query = {}, params = {}'.format(query, params))


        # Execute query.

        try:
            self.cur.execute(query, params)

            try:
                for record in self.cur:
                    print(record)
            except:
                print("Nothing returned from database stored function; continuing...")

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error inserting or updating RefImMeta record ({}); skipping...'.format(error))
            self.exit_code = 67
            return

        if self.exit_code == 0:
            self.conn.commit()           # Commit database transaction


########################################################################################################

    def get_l2files_records_for_datetime_range(self,startdatetime,enddatetime):

        '''
        Query database for all L2Files records associated with the given observation datetime range.
        '''

        self.exit_code = 0


        # Define query.

        query = "select rid,sca,fid,mjdobs from L2Files where dateobs >= %s and dateobs < %s and vbest > 0 order by mjdobs;"
        params = (startdatetime, enddatetime)


        # Query database.

        print('query = {}, params = {}'.format(query, params))


        # Execute query.

        try:
            self.cur.execute(query, params)

            try:
                records = []
                nrecs = 0
                for record in self.cur:
                    records.append(record)
                    nrecs += 1

                print("nrecs =",nrecs)

            except:
                print("Nothing returned from database query; continuing...")

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error getting all L2Files records for given dateobs range ({}); skipping...'.format(error))
            self.exit_code = 67
            return

        return records


########################################################################################################

    def get_exposure_filter(self,fid):

        '''
        Get record from Filters database table for given fid.
        '''

        self.exit_code = 0


        # Define query template.

        query =\
            "select filter from Filters where fid=%s;"


        # Query database.

        print('----> fid = {}'.format(fid))

        params = (fid,)

        print('query = {}, params = {}'.format(query, params))


        # Execute query.

        self.cur.execute(query, params)
        record = self.cur.fetchone()

        if record is not None:
            exposure_filter = record[0]

        else:
            exposure_filter = None

            print("*** Error: Could not get Filters database record; returning...")
            self.exit_code = 67


        return exposure_filter




########################################################################################################

    def get_best_difference_image(self,rid,ppid):

        '''
        Query DiffImages database table for the best (latest unless version is locked) version of difference image.
        '''

        self.exit_code = 0


        # Define query template.

        query =\
            "select pid,rfid,filename,infobitssci,version " +\
            "from DiffImages " +\
            "where vbest > 0 " +\
            "and status > 0 " +\
            "and rid = %s " +\
            "and ppid = %s; "


        # Formulate query by substituting parameters into query template.

        print('----> rid = {}'.format(rid))
        print('----> ppid = {}'.format(ppid))


        params = (rid, ppid)

        print('query = {}, params = {}'.format(query, params))


        # Execute query.

        self.cur.execute(query, params)
        record = self.cur.fetchone()

        record_dict = {}

        if record is not None:
            record_dict["pid"] = record[0]
            record_dict["rfid"] = record[1]
            record_dict["filename"] = record[2]
            record_dict["infobitssci"] = record[3]
            record_dict["version"] = record[4]

        else:
            print("*** Message: No best DiffImages database record found; continuing...")
            self.exit_code = 7


        return record_dict


########################################################################################################

    def get_reference_image(self,rfid):

        '''
        Query RefImages database table for the reference image specified by the given rfid,
        which may not necessarily be the best version.
        '''

        self.exit_code = 0


        # Define query template.

        query =\
            "select rfid,filename,infobits,version " +\
            "from RefImages " +\
            "where rfid = %s; "


        # Formulate query by substituting parameters into query template.

        print('----> rfid = {}'.format(rfid))


        params = (rfid,)

        print('query = {}, params = {}'.format(query, params))


        # Execute query.

        self.cur.execute(query, params)
        record = self.cur.fetchone()

        record_dict = {}

        if record is not None:
            record_dict["rfid"] = record[0]
            record_dict["filename"] = record[1]
            record_dict["infobits"] = record[2]
            record_dict["version"] = record[3]

        else:
            print(f"*** Message: No RefImages database record found for rfid={rfid}; continuing...")
            self.exit_code = 7


        return record_dict


########################################################################################################

    def get_jids_of_normal_science_pipeline_jobs_for_processing_date(self,proc_date):

        '''
        Query database for science-pipeline Jobs records that both
        ended on the given processing date and ran normally.
        '''

        self.exit_code = 0


        # Define query.

        # The science pipeline's ppid is read from the route matrix, not
        # written as a literal here (W4 single-homing sweep): it was 15
        # in this string, 15 in the master .ini, and 15 in an if/elif in
        # virtualPipelineOperator, with nothing keeping the three equal.
        from submission.routes import JOB_TYPE_SCIENCE, ppid_for

        query = "select jid from Jobs " +\
                "where ppid = %s " +\
                "and ended >= cast(%s as timestamp) " +\
                "and ended < cast(%s as timestamp) + cast('1 day' as interval) " +\
                "and status > 0 " +\
                "and exitcode <= 32;"
        params = (ppid_for(JOB_TYPE_SCIENCE), proc_date, proc_date)


        # Query database.

        print('query = {}, params = {}'.format(query, params))


        # Execute query.

        try:
            self.cur.execute(query, params)

            try:
                records = []
                nrecs = 0
                for record in self.cur:
                    jid = record[0]
                    records.append(jid)
                    nrecs += 1

                print("nrecs =",nrecs)

            except:
                print("Nothing returned from database query; continuing...")

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error getting Jobs records for given processing date {}: {}; skipping...'.format(proc_date,error))
            self.exit_code = 67
            return

        return records


########################################################################################################

    def get_job_record(self,jid):

        '''
        Query select columns in Jobs database table for given JID.

        Post-process gathering needs the job's own identity — which exposure,
        SCA and science image it processed — because a post-process unit is
        "close out what this science job produced" and is keyed by exposure/SCA
        like every other unit.

        This method did not exist. `gather_post_process_units` called it behind
        a `hasattr` guard, so against the REAL handle the guard was always
        false: every post-process unit fell back to the jid-as-exposure
        degenerate case and carried no rid, expid, fid or field at all — and
        `post_process.stamp_difference_image` requires all four.

        Column order is (expid, sca, field, fid, rid, ppid, status, exitcode),
        verified against the deployed table 2026-08-06.
        '''

        self.exit_code = 0


        # Define query template.

        query =\
            "select expid,sca,field,fid,rid,ppid,status,exitcode " +\
            "from Jobs " +\
            "where jid = %s;"

        params = (jid,)

        print('query = {}, params = {}'.format(query, params))


        # Execute query.

        try:
            self.cur.execute(query, params)
            record = self.cur.fetchone()

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error getting Jobs record for jid {}: {}; skipping...'.format(jid,error))
            self.exit_code = 67
            return None

        if record is None:
            print("No Jobs record for jid =",jid)
            return None

        return record


########################################################################################################

    def get_scas_with_science_jobs_for_processing_date(self,proc_date):

        '''
        Distinct SCAs that ran normally on the given processing date.

        THE POST-DB CHAIN'S WORK IS ENUMERATED AT SUBMISSION, NOT DISCOVERED
        AT RUNTIME (post-DB co-design ruling 1). The catalog-load job type's
        unit is (processing date, SCA), and this is where that list comes
        from.

        **RE-SOURCED OFF `Jobs` ONTO THE CURRENT SHAPE.** This read `Jobs`
        until the alert-trigger step, and `Jobs` HOLDS ZERO ROWS: the
        submission restructure records work in `attempts`, and nothing has
        written a Jobs row since. The gatherer therefore yielded no units,
        which is why the catalog-load probe and the load-rate measurement it
        carries were both blocked (vocab-role-closure ledger, § Named fork).
        The question is unchanged — what work did the science pipeline
        actually do on this date — and only the table that answers it moved.

        `diffimages` is that table, joined to the attempt that produced it:
        a current (`vbest = 1`) science difference image IS the evidence
        that this SCA produced a loadable catalogue, and its `attempt_id`
        carries the attempt whose outcome says the work succeeded. The date
        is `diffimages.created`, the row's own timestamp, rather than a Jobs
        `ended` that no longer exists.

        What this still deliberately does NOT do is probe `to_regclass` for
        `sources_<date>_<sca>` tables, the way `crossMatchSources.py:882-890`
        did. That asked the catalog what tables happen to exist — so the work
        list depended on what a previous run had already created, and a unit
        whose table was missing simply vanished from the list rather than
        being reported as work not done. Reading the registered products asks
        what the pipeline DID, which is the question the manifest needs
        answered.
        '''

        self.exit_code = 0

        from submission.routes import JOB_TYPE_SCIENCE, ppid_for

        query = "select distinct d.sca from DiffImages d " +\
                "join Attempts a on a.attempt_id = d.attempt_id " +\
                "where d.ppid = %s " +\
                "and d.vbest = 1 " +\
                "and a.rapid_outcome = 'success' " +\
                "and d.created >= cast(%s as timestamp) " +\
                "and d.created < cast(%s as timestamp) + cast('1 day' as interval) " +\
                "and d.sca is not null " +\
                "order by d.sca;"
        params = (ppid_for(JOB_TYPE_SCIENCE), proc_date, proc_date)

        print('query = {}, params = {}'.format(query, params))

        try:
            self.cur.execute(query, params)
            records = [record[0] for record in self.cur]
            print("nrecs =",len(records))

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error getting SCAs for processing date {}: {}; skipping...'.format(proc_date,error))
            self.exit_code = 67
            return

        return records


########################################################################################################

    def get_fields_with_science_jobs_for_processing_date(self,proc_date):

        '''
        Distinct sky fields that ran normally on the given processing date.

        The crossmatch job type's unit is (processing date, field), and the
        co-design has it "gathered after catalog load completes" — which is
        about ORDERING, not about where the field list comes from. The list
        comes from here, the Jobs rows, for the same reason the SCA list
        does; what waiting for catalog load buys is that the source rows
        those fields name are loaded by the time the crossmatch unit runs.

        `crossMatchSources.py:899` derived this by selecting distinct field
        from each `sources_<date>_<sca>` child table it had just found by
        catalog probe — a query that cannot run until the previous step has
        written its tables, and that is exactly why it could not live in a
        manifest. The registered products answer the same question at
        submission time.

        **RE-SOURCED OFF `Jobs`**, for the reason its SCA sibling above
        states at length and on exactly the same join: `Jobs` holds zero
        rows, so this returned an empty field list and the crossmatch unit
        was blocked by the same single root cause as the catalog load.
        '''

        self.exit_code = 0

        from submission.routes import JOB_TYPE_SCIENCE, ppid_for

        query = "select distinct d.field from DiffImages d " +\
                "join Attempts a on a.attempt_id = d.attempt_id " +\
                "where d.ppid = %s " +\
                "and d.vbest = 1 " +\
                "and a.rapid_outcome = 'success' " +\
                "and d.created >= cast(%s as timestamp) " +\
                "and d.created < cast(%s as timestamp) + cast('1 day' as interval) " +\
                "and d.field is not null " +\
                "order by d.field;"
        params = (ppid_for(JOB_TYPE_SCIENCE), proc_date, proc_date)

        print('query = {}, params = {}'.format(query, params))

        try:
            self.cur.execute(query, params)
            records = [record[0] for record in self.cur]
            print("nrecs =",len(records))

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error getting fields for processing date {}: {}; skipping...'.format(proc_date,error))
            self.exit_code = 67
            return

        return records


########################################################################################################

    def get_registered_diffimages_for_processing_date_sca(self,proc_date,sca):

        '''
        The current difference images this (date, SCA) unit's load reads.

        THE LOADER'S RE-SOURCE. `download_psf_catalogs` built its keys as
        `<proc_date>/jid<N>/<name>` from a `jids` list the gatherer took from
        `Jobs`. Neither half survives: `Jobs` is empty, and the product bucket
        holds no such prefix — everything since the submission restructure is
        attempt-scoped (vocab-role-closure ledger, § Named fork, item 2).

        The replacement does not RECONSTRUCT a key at all, which is the point.
        `diffimages.filename` is the registered product's own S3 URI, written
        by the registration this row came from, and the PSF catalogue is its
        SIBLING under the same attempt prefix. So the loader resolves the
        catalogue against a URI the catalog already holds rather than
        assembling one from parts — and a key-grammar change (the zero-padded
        C-core form is already live alongside the older unpadded one) cannot
        silently produce a key that points nowhere.

        Returns (pid, expid, sca, attempt_id, filename) per current row.
        '''

        self.exit_code = 0

        from submission.routes import JOB_TYPE_SCIENCE, ppid_for

        query = "select d.pid, d.expid, d.sca, d.attempt_id, d.filename, " +\
                "d.field, d.fid, l.mjdobs " +\
                "from DiffImages d " +\
                "join Attempts a on a.attempt_id = d.attempt_id " +\
                "join L2Files l on l.rid = d.rid " +\
                "where d.ppid = %s " +\
                "and d.vbest = 1 " +\
                "and a.rapid_outcome = 'success' " +\
                "and d.sca = %s " +\
                "and d.created >= cast(%s as timestamp) " +\
                "and d.created < cast(%s as timestamp) + cast('1 day' as interval) " +\
                "and d.filename is not null " +\
                "order by d.pid;"
        params = (ppid_for(JOB_TYPE_SCIENCE), sca, proc_date, proc_date)

        print('query = {}, params = {}'.format(query, params))

        try:
            self.cur.execute(query, params)
            records = [record for record in self.cur]
            print("nrecs =",len(records))

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error getting registered diffimages for {} sca {}: {}; skipping...'.format(proc_date,sca,error))
            self.exit_code = 67
            return

        return records


########################################################################################################

    def get_scas_with_incomplete_catalog_load_for_processing_date(self,proc_date):

        '''
        SCAs that ran science on this date but have NO successful catalog-load
        attempt for it — the durable-state coverage gap crossmatch and alert
        production both gate on.

        THE DURABLE-STATE ORDERING PREDICATE (integration review 2026-08,
        composite ruling 1's "every ordering fact is a durable-state gathering
        predicate"; design/operations.md: "Crossmatch readiness is durable
        state, not operator sequencing: its gathering predicate checks
        recorded catalog-load completion facts directly ... never an ordering
        convention among gatherer invocations"). Nothing about SUBMISSION
        ORDER is read here — an operator that happened to submit crossmatch
        before catalog load finished must still see the gap, and an operator
        restarted mid-chain must see the same gap it would have seen before
        the restart. The only fact this reads is whether the row exists.

        **COVERAGE SEMANTICS: PER PROCESSING DATE, NOT PER FIELD.**
        `crossMatchSources.py` cross-matches one field against
        `sources_<proc_date>_<sca>` for EVERY sca in the date's science SCA
        list (`run_single_core_job_stage_1_crossmatching`'s `for sca in
        scas:` loop, guarded only by "allow for missing SCAs" — a
        table-existence tolerance, not a per-field SCA subset). A crossmatch
        unit for field F does not read a narrower slice of SCAs than a
        crossmatch unit for field G on the same date; both want the WHOLE
        date's catalog load done. So this is scoped to `proc_date` alone —
        the same population `get_scas_with_science_jobs_for_processing_date`
        already enumerates — and the caller applies the SAME answer to every
        field of that date rather than a per-field re-query.

        "A successful catalog-load attempt" is a `logical_jobs.job_type =
        'catalog-load'` row (migration 039) whose owning attempt reached
        `lifecycle_state = 'terminal_after_start'` with `rapid_outcome =
        'success'`, scoped to this SCA and processing date via the
        applicable-identifier columns migration 039 adds
        (`attempts.sca`, `attempts.processing_date`) — never the
        exposure/SCA sentinel a catalog-load unit's synthetic `exposure_id`
        would otherwise look like.

        Returns the SCAs with NO such attempt: an empty list is the
        all-clear both callers gate on.
        '''

        self.exit_code = 0

        query = "select sc.sca from (" +\
                "  select distinct d.sca from DiffImages d " +\
                "  join Attempts a on a.attempt_id = d.attempt_id " +\
                "  where d.ppid = %s and d.vbest = 1 " +\
                "  and a.rapid_outcome = 'success' " +\
                "  and d.created >= cast(%s as timestamp) " +\
                "  and d.created < cast(%s as timestamp) + cast('1 day' as interval) " +\
                "  and d.sca is not null" +\
                ") sc " +\
                "where not exists (" +\
                "  select 1 from Attempts la " +\
                "  join logical_jobs lj on lj.logical_job_id = la.logical_job_id " +\
                "  where lj.job_type = %s " +\
                "  and la.sca = sc.sca " +\
                "  and la.processing_date = cast(%s as date) " +\
                "  and la.lifecycle_state = 'terminal_after_start' " +\
                "  and la.rapid_outcome = 'success'" +\
                ") " +\
                "order by sc.sca;"

        from submission.routes import JOB_TYPE_CATALOG_LOAD, JOB_TYPE_SCIENCE, ppid_for

        params = (ppid_for(JOB_TYPE_SCIENCE), proc_date, proc_date,
                  JOB_TYPE_CATALOG_LOAD, proc_date)

        print('query = {}, params = {}'.format(query, params))

        try:
            self.cur.execute(query, params)
            records = [record[0] for record in self.cur]
            print("nrecs =",len(records))

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error getting incomplete catalog-load SCAs for {}: {}; skipping...'.format(proc_date,error))
            self.exit_code = 67
            return

        return records


########################################################################################################

    def get_scas_with_gatherable_catalog_load_for_processing_date(self,proc_date):

        '''
        SCAs that ran science on this date and have no catalog-load attempt
        that is pending or succeeded — the catalog-load GATHER set.

        THE RESUBMISSION GATE (mission mock, live finding 2026-08-09). The
        catalog-load enumeration used to return every science SCA of the
        date unconditionally, so a 15-second poll cadence resubmitted the
        same (date, SCA) unit every accumulator cut for the whole flight of
        the first attempt and forever after its success. Gathering is a
        durable-state predicate here exactly as it is for ordering
        (composite ruling 1): the state read is "does a BLOCKING attempt
        exist" — one in flight (lifecycle 'submitted'/'started') or one
        that succeeded. A subject whose attempts all FAILED is returned
        again: retry by re-gathering is the intended recovery path, and a
        blocked-on-failure gate would need scoped_retry for every transient.

        The in-flight test deliberately does NOT count
        'application_closed'/'terminal_after_start' with a NULL outcome as
        blocking — those are reconciliation states whose outcome resolves
        within the reconciler's grace horizon, and counting them would
        block retries of attempts the reconciler later marks failed.
        '''

        self.exit_code = 0

        query = "select sc.sca from (" +\
                "  select distinct d.sca from DiffImages d " +\
                "  join Attempts a on a.attempt_id = d.attempt_id " +\
                "  where d.ppid = %s and d.vbest = 1 " +\
                "  and a.rapid_outcome = 'success' " +\
                "  and d.created >= cast(%s as timestamp) " +\
                "  and d.created < cast(%s as timestamp) + cast('1 day' as interval) " +\
                "  and d.sca is not null" +\
                ") sc " +\
                "where not exists (" +\
                "  select 1 from Attempts la " +\
                "  join logical_jobs lj on lj.logical_job_id = la.logical_job_id " +\
                "  where lj.job_type = %s " +\
                "  and la.sca = sc.sca " +\
                "  and la.processing_date = cast(%s as date) " +\
                "  and (la.lifecycle_state in ('submitted','started') " +\
                "       or la.rapid_outcome = 'success')" +\
                ") " +\
                "order by sc.sca;"

        from submission.routes import JOB_TYPE_CATALOG_LOAD, JOB_TYPE_SCIENCE, ppid_for

        params = (ppid_for(JOB_TYPE_SCIENCE), proc_date, proc_date,
                  JOB_TYPE_CATALOG_LOAD, proc_date)

        print('query = {}, params = {}'.format(query, params))

        try:
            self.cur.execute(query, params)
            records = [record[0] for record in self.cur]
            print("nrecs =",len(records))

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error getting gatherable catalog-load SCAs for {}: {}; skipping...'.format(proc_date,error))
            self.exit_code = 67
            return

        return records


########################################################################################################

    def get_fields_with_blocking_crossmatch_attempt_for_processing_date(self,proc_date):

        '''
        Fields with a crossmatch attempt for this processing date that is
        pending or succeeded — the crossmatch resubmission-gate EXCLUSION
        set (mission mock, live finding 2026-08-09; same blocking predicate
        as the catalog-load gather set, see that method's docstring).
        Failed attempts do not block: re-gathering is the retry path.
        '''

        self.exit_code = 0

        query = "select distinct la.field from Attempts la " +\
                "join logical_jobs lj on lj.logical_job_id = la.logical_job_id " +\
                "where lj.job_type = %s " +\
                "and la.processing_date = cast(%s as date) " +\
                "and la.field is not null " +\
                "and (la.lifecycle_state in ('submitted','started') " +\
                "     or la.rapid_outcome = 'success') " +\
                "order by la.field;"

        from submission.routes import JOB_TYPE_CROSSMATCH

        params = (JOB_TYPE_CROSSMATCH, proc_date)

        print('query = {}, params = {}'.format(query, params))

        try:
            self.cur.execute(query, params)
            records = [record[0] for record in self.cur]
            print("nrecs =",len(records))

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error getting blocking crossmatch fields for {}: {}; skipping...'.format(proc_date,error))
            self.exit_code = 67
            return

        return records


########################################################################################################

    def get_blocking_exposure_scas_for_job_type(self,job_type,expids):

        '''
        (exposure_id, sca) pairs among the given exposures with an attempt
        of the given job type that is pending or succeeded — the
        EXPOSURE_SCA-grain resubmission-gate exclusion set (final Codex
        convergence round, 2026-08-09: science and reference-image
        gathering were the last state-blind enumerations; same blocking
        predicate as the other gates, failed attempts free the subject).
        Scoped to the caller's enumerated exposures so the query never
        scans the whole attempts corpus.
        '''

        self.exit_code = 0

        query = "select distinct la.exposure_id, la.sca from Attempts la " +\
                "join logical_jobs lj on lj.logical_job_id = la.logical_job_id " +\
                "where lj.job_type = %s " +\
                "and la.exposure_id = any(%s) " +\
                "and la.sca is not null " +\
                "and (la.lifecycle_state in ('submitted','started') " +\
                "     or la.rapid_outcome = 'success') " +\
                "order by la.exposure_id, la.sca;"

        params = (job_type, list(expids))

        print('query = {}, params = {}'.format(query, params))

        try:
            self.cur.execute(query, params)
            records = [record for record in self.cur]
            print("nrecs =",len(records))

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error getting blocking {} exposure/SCAs: {}; skipping...'.format(job_type,error))
            self.exit_code = 67
            return

        return records


########################################################################################################

    def get_fields_with_blocking_attempt_for_job_type_since(self,job_type,since):

        '''
        Fields with an attempt of the given job type submitted at or after
        `since` that is pending or succeeded — the per-field
        resubmission-gate EXCLUSION set for the FIELD-grain job types
        (statistics and the three sweeps), whose identity carries no
        processing date (composite ruling 2: only applicable identifiers).

        `since` is the UTC midnight of the pass's processing date, giving
        the v1 cadence "at most one successful or in-flight run per field
        per UTC day" (mission mock, live finding 2026-08-09: the sweeps'
        state-blind enumeration resubmitted every accumulator cut). The
        day-cadence is a recorded, revisitable judgment call — a real
        sweep cadence policy is an open design item; this gate exists so
        enablement is bounded, not to decide that policy.
        '''

        self.exit_code = 0

        query = "select distinct la.field from Attempts la " +\
                "join logical_jobs lj on lj.logical_job_id = la.logical_job_id " +\
                "where lj.job_type = %s " +\
                "and la.submitted_at >= %s " +\
                "and la.field is not null " +\
                "and (la.lifecycle_state in ('submitted','started') " +\
                "     or la.rapid_outcome = 'success') " +\
                "order by la.field;"

        params = (job_type, since)

        print('query = {}, params = {}'.format(query, params))

        try:
            self.cur.execute(query, params)
            records = [record[0] for record in self.cur]
            print("nrecs =",len(records))

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error getting blocking {} fields since {}: {}; skipping...'.format(job_type,since,error))
            self.exit_code = 67
            return

        return records


########################################################################################################

    def get_attempts_awaiting_alert_emission(self,release_identity,limit=None):

        '''
        Units whose difference image became current and that have not emitted.

        THE RULED EMISSION PREDICATE (step-4 co-design gates 1, 4, 5;
        design/operations.md § Alert production). Each clause below is one
        sentence of the design, and nothing else is in here:

          * "the trigger's unit is the registered attempt, read through its
            registration_outcome record" -> the driving table is `attempts`,
            and the promotion is read from that document, not re-derived by
            querying `diffimages` for what looks current now. The document
            records what the registration COMMITTED; a later supersession
            changes `vbest` but must not change whether this attempt emitted.

          * "the gathering predicate is the first outcome in which the unit's
            difference image became current" -> a promotion event naming the
            difference-image role's product. `role_resolved_from` is carried
            through so a record registered before role bindings existed is
            visible as such rather than silently equivalent.

          * "No promotion, no alert" -> a promotions array with no entry
            yields no row. A pin-suppressed registration promoted nothing and
            so is absent here; a later pin-release promotion appears then,
            which is exactly the ruled behaviour.

          * "enumerates attempts whose registration committed with the
            unit's difference image promoted to current, past the alert
            watermark and with the unit's catalog load complete" -> the
            catalog-load clause, ADDED by integration review 2026-08
            (composite ruling 1: "the ruled catalog-load clause is missing
            from the implemented alert predicate" — this was the exact gap
            named). The unit's processing date is the promoted difference
            image's own `created` date (the same convention
            `get_scas_with_science_jobs_for_processing_date` and
            `get_scas_with_incomplete_catalog_load_for_processing_date`
            use), and "complete" means a `logical_jobs.job_type =
            'catalog-load'` attempt reached `terminal_after_start` /
            `success` for that (date, sca) — read through the SAME fact
            class `get_scas_with_incomplete_catalog_load_for_processing_date`
            reads, expressed inline here because this query is driven by
            promo/diffimages rows a separate per-date call cannot join
            against without re-deriving the date twice.

          * "Emission is once per logical unit per release" -> gathering
            excludes units carrying ANY `alert_emissions` row in
            (watermark_seed, claimed-and-fresh, emitted) — migration 037's
            state model, replacing 033's single-state anti-join (co-design
            ruling 3). A STALE claim (age past the 1-hour threshold
            migration 037's `derived.alert_emission_status` view names, AND
            the claiming attempt terminal — THE OWED OWNER-TERMINAL CONJUNCT,
            integration ruling 3) is gatherable again: a crashed claimant
            must not permanently suppress its unit's alert, but a claimant
            that is merely SLOW (still running, not yet terminal) must not be
            raced by a second gatherer just because its claim aged past the
            threshold — age alone says nothing about whether the claimant is
            dead. The owner lookup joins `attempts` on `claim_token` cast to
            an attempt id (guarded: a token that is not a bare integer counts
            as NOT terminal, conservatively — see below). The primary key
            still makes a double CONFIRMED emission impossible even if two
            gatherers raced; this clause is what stops a second one being
            GATHERED, which is cheaper than relying only on the CAS claim to
            refuse it.

          * "a reference-image-only attempt is a natural no-op" -> such an
            attempt records no difference-image promotion, so it never
            appears. No job-type filter is needed to exclude it.

        THE WATERMARK IS INITIALIZED AT DEPLOYMENT, NOT HERE. `alert_emissions`
        starting empty would make every historical promotion eligible; the
        deployment seeds it instead (see submission/gathering.py
        `initialize_alert_watermark`). This query has no date floor of its own
        precisely so that the watermark is the ONE thing deciding what is
        eligible — a second, implicit cutoff here would be a place for the two
        to disagree.

        Returns (attempt_id, expid, sca, pid, product, role_resolved_from,
        registered_at, sequence) per eligible unit, oldest registration first
        so a bounded run emits the earliest outstanding work.
        '''

        self.exit_code = 0

        from submission.routes import JOB_TYPE_CATALOG_LOAD

        # The staleness threshold, matching migration 037's
        # `derived.alert_emission_status` view exactly (that view's own
        # comment: "keep the two in sync by inspection until a shared
        # parameter home exists" — this is the pipeline half of that pair).
        claim_staleness = "interval '1 hour'"

        query = (
                "select a.attempt_id, a.exposure_id, a.sca, "
                "       (promo->>'pid')::int as pid, "
                "       promo->>'product' as product, "
                "       promo->>'role_resolved_from' as role_resolved_from, "
                "       a.registered_at, (promo->>'sequence')::int as sequence "
                "from Attempts a "
                "cross join lateral jsonb_array_elements("
                "     coalesce(a.registration_outcome->'promotions', '[]'::jsonb)) as promo "
                "join DiffImages d on d.pid = (promo->>'pid')::int "
                "where a.registration_outcome is not null "
                "and promo->>'type' = 'promotion' "
                "and promo->>'pid' is not null "
                "and a.exposure_id is not null and a.sca is not null "
                "and exists ("
                "  select 1 from Attempts la "
                "  join logical_jobs lj on lj.logical_job_id = la.logical_job_id "
                "  where lj.job_type = %s "
                "  and la.sca = a.sca "
                "  and la.processing_date = d.created::date "
                "  and la.lifecycle_state = 'terminal_after_start' "
                "  and la.rapid_outcome = 'success'"
                ") "
                "and not exists (select 1 from Alert_Emissions e "
                "                where e.exposure_id = a.exposure_id "
                "                and e.sca = a.sca "
                "                and e.release_identity = %s "
                "                and ("
                "                  e.state in ('watermark_seed', 'emitted') "
                "                  or (e.state = 'claimed' "
                "                      and ("
                "                        e.claimed_at >= now() - " + claim_staleness + " "
                # THE OWED OWNER-TERMINAL CONJUNCT (integration ruling 3), in
                # the "or not exists(...)" clause below: a stale claim (age
                # past threshold) is excluded from gathering (kept NOT
                # gatherable) unless the claiming attempt is ALSO terminal.
                # Expressed as the negation inside this OR, matching the
                # surrounding "exclude when fresh OR <not-yet-safe-to-
                # retake>" shape: the claim stays excluded while fresh, OR
                # while stale-but-the-claimant-might-still-be-running. A
                # claim_token that does not parse to a bare integer (guarded
                # by the regexp_replace/NULLIF idiom the CAS claim itself
                # uses) cannot be resolved to an owning attempt at all, so it
                # is treated conservatively as NOT terminal — excluded from
                # gathering until the CAS claim's own age-based takeover can
                # retake it directly, rather than gathering guessing an
                # owner it cannot verify.
                "                        or not exists ("
                "                          select 1 from attempts owner "
                "                          where owner.attempt_id = "
                "                                nullif(regexp_replace(e.claim_token, "
                "                                                      '[^0-9]', '', 'g'), '')::bigint "
                "                          and owner.lifecycle_state in "
                "                              ('terminal_after_start', 'terminal_without_start')"
                "                        )"
                "                      )"
                "                  )"
                "                )) "
                # THE RESUBMISSION GATE (mission mock, live 2026-08-09 —
                # the same pending-attempt gate the rest of the post-DB
                # chain gained): a subject with an alert-production attempt
                # already in flight is not re-gathered. Only PENDING blocks
                # — an emitted subject is already excluded by the watermark
                # anti-join above, and a failed attempt frees the subject
                # (retry by re-gathering). Without this, every accumulator
                # cut re-submitted all not-yet-claimed subjects for the
                # whole flight of their first attempts (57 children for 36
                # subjects, observed live after the DB outage orphaned a
                # wave).
                "and not exists ("
                "  select 1 from Attempts ap "
                "  join logical_jobs ljp on ljp.logical_job_id = ap.logical_job_id "
                "  where ljp.job_type = %s "
                "  and ap.exposure_id = a.exposure_id "
                "  and ap.sca = a.sca "
                "  and ap.lifecycle_state in ('submitted', 'started')"
                ") "
                "order by a.registered_at, a.attempt_id"
        )
        from submission.routes import JOB_TYPE_ALERT_PRODUCTION

        params = [JOB_TYPE_CATALOG_LOAD, release_identity,
                  JOB_TYPE_ALERT_PRODUCTION]

        if limit is not None:
            query += " limit %s"
            params.append(int(limit))
        query += ";"

        print('query = {}, params = {}'.format(query, tuple(params)))

        try:
            self.cur.execute(query, tuple(params))
            records = [record for record in self.cur]
            print("nrecs =",len(records))

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error getting attempts awaiting alert emission for release {}: {}; skipping...'.format(release_identity,error))
            self.exit_code = 67
            return

        return records


########################################################################################################

    def seed_alert_emission_watermark(self,exposure_id,sca,release_identity,
                                      attempt_id,pid=None):

        '''
        Seed the emission watermark for one (unit, release) as `watermark_seed`.

        WATERMARK SEEDING ONLY (migration 037 / integration ruling 3). This
        used to be `record_alert_emission`, an `ON CONFLICT DO NOTHING` insert
        shared by both the deployment-time watermark seed and the live claim
        path. The two are now different writes with different target states —
        a seed row is TERMINAL-suppress on write (`state = 'watermark_seed'`,
        never published), a claim row is TRANSIENT (`state = 'claimed'`,
        carries a claim_token and claimed_at, and is confirmed or superseded
        later) — so one method can no longer serve both. The live claim/CAS
        path is `claim_alert_emission`; this method is `initialize_alert_
        watermark`'s writer only.

        `ON CONFLICT DO NOTHING` and a reported row count, same as before: a
        second seeding pass over a unit already seeded (or already claimed —
        seeding never overwrites) must not raise, it must say it did not win.

        Returns True if this call seeded the row, False if a row already
        existed for this (unit, release) under any state.
        '''

        self.exit_code = 0

        query = "insert into Alert_Emissions " +\
                "(exposure_id, sca, release_identity, attempt_id, pid, " +\
                " alerts_published, state) " +\
                "values (%s, %s, %s, %s, %s, %s, 'watermark_seed') " +\
                "on conflict (exposure_id, sca, release_identity) do nothing;"
        params = (exposure_id, sca, release_identity, attempt_id, pid, 0)

        print('query = {}, params = {}'.format(query, params))

        try:
            self.cur.execute(query, params)
            claimed = (self.cur.rowcount == 1)
            self.conn.commit()

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error seeding alert emission watermark for unit {}/{} release {}: {}'.format(exposure_id,sca,release_identity,error))
            self.exit_code = 67
            return

        return claimed


########################################################################################################

    def claim_alert_emission(self,exposure_id,sca,release_identity,
                             attempt_id,claiming_attempt_id,claim_token,
                             pid=None):

        '''
        CAS-claim the right to emit one (unit, release) (migration 037 /
        integration ruling 3).

        Replaces the old claim-before-publish `record_alert_emission`
        (renamed `seed_alert_emission_watermark`, watermark-seeding only).
        This is the LIVE claim: `INSERT ... ON CONFLICT DO UPDATE ... WHERE`
        CAS against the primary key (exposure_id, sca, release_identity) —
        the ON CONFLICT arm fires when a row already exists in ANY state, and
        the WHERE clause is what makes it a claim rather than a last-writer-
        wins upsert: it only overwrites a row that is itself 'claimed' AND
        (stale by age, OR the same claimant re-claiming idempotently, OR a
        retry of the same logical unit whose prior claimant attempt is now
        terminal). A 'watermark_seed' or 'emitted' row never matches the
        WHERE clause and so is never touched — those are the terminal-
        suppress states and this statement cannot un-suppress one.

        `attempt_id` is the REGISTERED SOURCE attempt (the promotion that made
        this unit eligible — `unit.fields["attempt_id"]`, unchanged across
        retries of the emission step). `claiming_attempt_id` is the alert-
        production attempt actually doing this claim (`context.attempt_id`);
        `claim_token` is that same identity as text, carried as a separate
        parameter because the CAS's third disjunct needs it as a plain
        integer for the `prior.attempt_id = ...::bigint` join as well as a
        string for the column itself.

        Returns the claim_token of the row that came back from RETURNING
        (there is at most one), or None if no row matched — either the row is
        terminal (already seeded/emitted) or 'claimed'-fresh by a different,
        non-terminal claimant. The CALLER decides what a None means for it;
        this method only reports the CAS outcome.

        Must run inside the caller's own transaction (the borrowed connection
        held for the attempt's lifetime) — this call itself does not commit,
        unlike every autocommitting method elsewhere in this class. The CLAIM
        is its own transaction, committed once by the caller immediately
        after this call returns, deliberately BEFORE publishing begins (a
        crash after a committed claim leaves a stale-recoverable row, never a
        suppression).
        '''

        self.exit_code = 0

        # The staleness threshold, matching migration 037's
        # `derived.alert_emission_status` view (that view's own comment:
        # "keep the two in sync by inspection until a shared parameter home
        # exists" — this is the pipeline half of that pair, and the same
        # literal `get_attempts_awaiting_alert_emission` uses).
        query = \
            "insert into Alert_Emissions " +\
            "(exposure_id, sca, release_identity, attempt_id, pid, " +\
            " alerts_published, state, claim_token, claimed_at) " +\
            "values (%s, %s, %s, %s, %s, 0, 'claimed', %s, now()) " +\
            "on conflict (exposure_id, sca, release_identity) do update " +\
            "  set claim_token = excluded.claim_token, " +\
            "      claimed_at = now() " +\
            "  where alert_emissions.state = 'claimed' " +\
            "    and (alert_emissions.claimed_at < now() - interval '1 hour' " +\
            "         or alert_emissions.claim_token = excluded.claim_token " +\
            "         or exists ( " +\
            "              select 1 from attempts prior, attempts me " +\
            "              where me.attempt_id = %s " +\
            "                and prior.attempt_id = " +\
            "                    nullif(regexp_replace(alert_emissions.claim_token, " +\
            "                                          '[^0-9]', '', 'g'), '')::bigint " +\
            "                and prior.logical_job_id = me.logical_job_id " +\
            "                and prior.lifecycle_state in " +\
            "                    ('terminal_after_start', 'terminal_without_start'))) " +\
            "returning claim_token;"
        params = (exposure_id, sca, release_identity, attempt_id, pid,
                  str(claim_token), int(claiming_attempt_id))

        print('query = {}, params = {}'.format(query, params))

        try:
            self.cur.execute(query, params)
            row = self.cur.fetchone()

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error claiming alert emission for unit {}/{} release {}: {}'.format(exposure_id,sca,release_identity,error))
            self.exit_code = 67
            return

        return row[0] if row is not None else None


########################################################################################################

    def confirm_alert_emission(self,exposure_id,sca,release_identity,
                               claim_token,alerts_published):

        '''
        CONFIRM a claimed emission, once publishing has flushed successfully
        (migration 037 / integration ruling 3).

        `UPDATE ... WHERE claim_token = <mine> AND state = 'claimed' RETURNING
        claim_token` — succeeds only when this claim is still owned by the
        caller. NULLs claim_token/claimed_at in the same statement: migration
        037's `alert_emissions_claim_shape_ck` CHECK constraint requires a
        non-'claimed' row to carry NEITHER field, so a confirm that left them
        set would violate the constraint outright.

        Returns the claim_token of the row that was confirmed (there is at
        most one), or None if no row matched — the claim was taken over by
        another attempt (or already confirmed) between this attempt's publish
        and this confirm call. The CALLER records that as a recorded no-op
        (the takeover republishes; consumers dedup), never as a failure.

        Must run inside the SAME transaction as the alert_published milestone
        write (integration ruling 3 / 6: "Emission confirmation and the
        alert-published milestone commit in one transaction"). This call does
        not commit — the caller's transaction envelope does, after both
        statements.
        '''

        self.exit_code = 0

        # claim_token is NULLed by this very statement, so RETURNING the
        # post-update column would always read NULL. RETURNING a bound
        # parameter instead (`%s as confirmed_token`) reports which token was
        # confirmed without a second round trip or a subquery.
        query = \
            "update Alert_Emissions " +\
            "   set state = 'emitted', alerts_published = %s, " +\
            "       emitted_at = now(), claim_token = null, claimed_at = null " +\
            " where exposure_id = %s and sca = %s and release_identity = %s " +\
            "   and claim_token = %s and state = 'claimed' " +\
            "returning %s as confirmed_token;"
        params = (int(alerts_published), exposure_id, sca, release_identity,
                  str(claim_token), str(claim_token))

        print('query = {}, params = {}'.format(query, params))

        try:
            self.cur.execute(query, params)
            row = self.cur.fetchone()

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error confirming alert emission for unit {}/{} release {}: {}'.format(exposure_id,sca,release_identity,error))
            self.exit_code = 67
            return

        return row[0] if row is not None else None


########################################################################################################

    def insert_alert_outbox_packet(self,alert_id,identity_basis,payload,
                                   payload_checksum,schema_version_id,topic,
                                   release_identity,exposure_id,sca,
                                   producing_attempt_id,corrects_alert_id=None):

        '''
        Write one alert packet to the transactional outbox (rule 14; DRAFT
        migration 050's `insert_alert_outbox_packet` PL/pgSQL function).

        Must run inside the SAME transaction as the `alert_emissions` confirm
        CAS and the `alert_published` milestone write (migration 050's own
        header: "the SAME TRANSACTION as the database effect that produced
        them", which in this topology is `alert_production.py`'s confirmation
        transaction). This call does NOT commit — exactly the same posture as
        `confirm_alert_emission` above, and for the same reason: the caller's
        transaction envelope commits once, after the confirm CAS, the outbox
        inserts and the milestone all agree, so a crash between any two of
        them rolls all of them back rather than leaving a packet committed
        for an emission that was never actually confirmed.

        `payload` is the framed-ready Avro bytes as a Python `bytes` object,
        bound to the `bytea` parameter via `psycopg2.Binary()`. Nothing else
        in this file has ever bound a bytea column — every existing query
        here is text/numeric/timestamp — so there is no prior idiom to match;
        `psycopg2.Binary()` is psycopg2's own documented adapter for exactly
        this (a plain `bytes` object passed unwrapped is escaped as text, not
        binary, and will not round-trip through a bytea column intact).

        THE FUNCTION RAISES ON A SAME-alert_id COLLISION WHOSE ENVELOPE
        DIFFERS (different `payload_checksum`, different pinned
        `schema_version_id`, or a different basis/topic/release_identity) —
        see 050's own comment on `insert_alert_outbox_packet`: "one identity,
        two different packets ... both are defects that a silent no-op would
        hide". That is a HARD INVARIANT VIOLATION, not a retryable outcome —
        it means either the alert_id digest inputs are incomplete or two
        genuinely different packets were minted under one identity — so
        unlike every other method in this class this one does NOT catch the
        exception, set exit_code and return. It lets the raise propagate
        uncaught, exactly like a CHECK or FK violation would from a bare
        `self.cur.execute()` elsewhere: swallowing it here would convert a
        corruption signal into a False/None the caller could mistake for an
        ordinary "no row" answer, and it would leave the caller's transaction
        in an aborted-but-uncaught state with no exception to explain why the
        confirm CAS and milestone never landed either.

        A same-alert_id collision with an IDENTICAL envelope is NOT an error:
        the function absorbs it (the ordinary idempotent re-run after a lost
        response) and returns 'idempotent' rather than raising.

        Returns 'inserted' (first write of this alert_id) or 'idempotent'
        (identical re-write, absorbed).
        '''

        self.exit_code = 0

        query = "select insert_alert_outbox_packet" +\
                "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);"
        params = (alert_id, identity_basis, psycopg2.Binary(payload),
                  payload_checksum, schema_version_id, topic,
                  release_identity, exposure_id, sca, producing_attempt_id,
                  corrects_alert_id)

        # THE PAYLOAD IS NOT PRINTED, unlike every other method's params in
        # this file. The convention is a debugging aid built for text and
        # numeric parameters; here one parameter is a multi-megabyte Avro
        # packet carrying image cutouts, and echoing it would put hundreds of
        # megabytes of binary into a Batch job's CloudWatch stream per chip —
        # obscuring the log it was meant to serve, and costing real money to
        # store. The identity and envelope are what a reader of this line
        # actually needs; the bytes are recorded in the row itself, with a
        # checksum beside them.
        print('query = {}, params = (alert_id={}, basis={}, payload=<{} '
              'bytes>, checksum={}, schema_version_id={}, topic={}, '
              'release={}, exposure={}, sca={}, attempt={}, corrects={})'
              .format(query, alert_id, identity_basis, len(payload),
                      payload_checksum, schema_version_id, topic,
                      release_identity, exposure_id, sca,
                      producing_attempt_id, corrects_alert_id))

        # No try/except around this execute — see the docstring. A same-id,
        # different-envelope collision must reach the caller as a raised
        # exception, not as an exit_code the caller has to remember to check.
        self.cur.execute(query, params)
        row = self.cur.fetchone()

        return row[0]


########################################################################################################

    def get_difference_image_product_key(self,pid):

        '''
        The `products.product_key` bound to a difference image, or None.

        DRAFT 048 adds `diffimages.product_id` (nullable FK to `products`) as
        the binding between a legacy difference-image row and its rule-10
        product identity. This reads that binding for the alert-production
        identity basis (`alerts/identity.py`'s 'product-key' basis prefers
        the product key when one exists; 'legacy-pid' is the fallback for a
        difference image with no binding yet — see migration 050's comment
        on `alert_outbox.identity_basis`).

        TOLERATES 048 BEING UNAPPLIED. 048 is still a DRAFT migration
        (`migrations-draft/`, not yet adopted by `rapid_systems` into
        `apply-db-migrations.sh`), so CI and any database built from the
        authoritative stream alone has neither the `products` table nor the
        `diffimages.product_id` column this query joins through. Catching
        `psycopg2.errors.UndefinedTable` / `UndefinedColumn` specifically
        (rather than the blanket `(Exception, psycopg2.DatabaseError)` every
        other method here uses) is the file's own established convention for
        exactly this situation — `pipeline/stages/catalog_db.py`'s
        `require_table` docstring and several other call sites in this repo
        name the same two exception classes as "not in the runtime taxonomy"
        precisely because they mean "the schema object is absent", a
        different fact from "the query failed". Narrowing the catch to these
        two, rather than reusing the file's blanket clause, is what keeps
        "the join target does not exist" from being conflated with "the join
        ran and found no product" or with a genuine query failure (a typo, a
        permissions error, a connection drop) — the latter two still fall
        through to the blanket handler below and still set exit_code = 67.

        A schema-absent result and a present-but-NULL result are BOTH
        reported as None to the caller, and that is deliberate: this method
        answers "does the caller have a product key to use", and the answer
        is no in both cases. What is NOT collapsed is exit_code — the
        schema-absent path leaves exit_code at 0 (an ordinary, expected
        answer given a DRAFT migration, not a query failure) while the
        blanket handler below still sets 67, so a caller inspecting exit_code
        after the call can still tell "no key" from "the query broke".
        '''

        self.exit_code = 0

        query = "select p.product_key from DiffImages d " +\
                "join Products p on p.product_id = d.product_id " +\
                "where d.pid = %s;"
        params = (pid,)

        print('query = {}, params = {}'.format(query, params))

        try:
            self.cur.execute(query, params)
            record = self.cur.fetchone()

        except (psycopg2.errors.UndefinedTable,
               psycopg2.errors.UndefinedColumn) as error:
            # 048 unapplied: no products table, or no diffimages.product_id
            # column to join through. Not a failure — see docstring — so
            # exit_code stays 0. The aborted transaction from the failed
            # statement still needs clearing before this connection can be
            # used again; every other read in this class is autocommitting
            # (no explicit BEGIN of its own), so a rollback here undoes
            # nothing this call itself wanted committed.
            self.conn.rollback()
            print('*** DRAFT 048 not applied (products/diffimages.product_id '
                 'absent); treating pid {} as having no product binding: {}'.format(pid,error))
            return None

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error getting product key for diffimage pid {}: {}'.format(pid,error))
            self.exit_code = 67
            return

        return record[0] if record is not None else None


########################################################################################################

    def get_fields_with_per_field_table(self,prototype):

        '''
        Fields that currently have a per-field clone of the named prototype.

        The corpus-wide sweeps (statistics, the two currency sweeps, the dedup
        check) act on every field that HAS a table, which is a different
        question from "which fields ran on a date" — a field loaded last month
        still needs its currency maintained when a difference image is demoted
        today, and no processing-date query would name it.

        SO THIS IS A CATALOG QUERY, AND IT IS DELIBERATELY IN THE SUBMISSION
        LAYER. Ruling 1 bans runtime catalog introspection INSIDE a job type —
        "a job type never discovers its work by catalog introspection at
        runtime; every unit is individually retryable and individually
        reconcilable". Enumerating at submission is what that ruling asks
        for: the submitter runs this once, the manifest names each field as a
        declared unit, and each unit is then retryable on its own. The same
        `pg_tables` question asked from inside the sweep (as
        `pruneNotBestMerges.py:358` asked it) produced one unbounded job whose
        work list nothing could reconstruct or retry piecewise.

        `prototype` is a table NAME, not caller SQL: it is matched against the
        known prototypes and rejected otherwise, so this cannot become a
        string-substituted query surface. The LIKE pattern is a bound
        parameter for the same reason.
        '''

        self.exit_code = 0

        # The prototypes whose clones the post-DB chain sweeps. An allow-list,
        # not an escaping routine: the set is small, closed, and known here,
        # and a caller asking for anything else is a bug rather than a table
        # this method should try to serve.
        known = ("merges", "astroobjects", "astroobjectsmeta", "sources")
        if prototype not in known:
            print('*** Error: {} is not a per-field prototype; known: {}'.format(
                prototype, ", ".join(known)))
            self.exit_code = 65
            return

        query = "select tablename from pg_tables " +\
                "where schemaname = 'public' " +\
                "and tablename like %s " +\
                "order by tablename;"
        params = (prototype + "\\_%",)

        print('query = {}, params = {}'.format(query, params))

        try:
            self.cur.execute(query, params)
            rows = [record[0] for record in self.cur]

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error listing per-field tables for {}: {}; skipping...'.format(prototype,error))
            self.exit_code = 67
            return

        # Only the suffixes that are actually field identifiers. A table named
        # `merges_backup_20260101` matches the LIKE and is NOT a field clone;
        # returning it would put a bogus unit in a manifest and the job would
        # fail on a table whose name it built from a non-numeric field.
        fields = []
        prefix = prototype + "_"
        for tablename in rows:
            suffix = tablename[len(prefix):]
            if suffix.isdigit():
                fields.append(int(suffix))
            else:
                print("Skipping non-field table:", tablename)

        print("nrecs =",len(fields))
        return fields


########################################################################################################

    def get_unclosedout_jobs_for_processing_date(self,ppid,proc_date):

        '''
        Query database for Jobs records that were launched on the given processing date,
        but not yet closed out by finalizing started, ended, elapsed, exitcode and status.
        .
        '''

        self.exit_code = 0


        # Define query.

        query = "select jid,awsbatchjobid from Jobs " +\
                "where ppid = %s " +\
                "and launched >= cast(%s as timestamp) " +\
                "and launched < cast(%s as timestamp) + cast('1 day' as interval) " +\
                "and status = 0 " +\
                "and ended is null " +\
                "and exitcode is null;"
        params = (ppid, proc_date, proc_date)


        # Query database.

        print('query = {}, params = {}'.format(query, params))


        # Execute query.

        try:
            self.cur.execute(query, params)

            try:
                records = []
                nrecs = 0
                for record in self.cur:
                    records.append(record)
                    nrecs += 1

                print("nrecs =",nrecs)

            except:
                print("Nothing returned from database query; continuing...")

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error getting unclosedout Jobs records for given ppid={} and processing date {}: {}; skipping...'.format(ppid,proc_date,error))
            self.exit_code = 67
            return

        return records


########################################################################################################

    def get_l2files_records_for_datetime_range_and_superior_reference_images(self,
                                                                             startdatetime,
                                                                             enddatetime,
                                                                             nframes,
                                                                             cov5percent):

        '''
        Query database for all L2Files records associated with the given observation datetime range
        and superior reference images as defined by the input criteria nframes and cov5percent.
        '''

        self.exit_code = 0


        # Define query.

        query = "select a.rid,a.sca,a.fid,a.mjdobs " +\
                "from L2Files a, RefImages b, RefImMeta c " +\
                "where a.field = b.field " +\
                "and b.rfid = c.rfid " +\
                "and a.fid = b.fid " +\
                "and a.vbest > 0 " +\
                "and b.status > 0 " +\
                "and b.vbest > 0 " +\
                "and cov5percent >= %s " +\
                "and nframes >= %s " +\
                "and a.dateobs >= %s " +\
                "and a.dateobs < %s " +\
                "order by a.mjdobs,a.sca;"
        params = (cov5percent, nframes, startdatetime, enddatetime)


        # Query database.

        print('query = {}, params = {}'.format(query, params))


        # Execute query.

        try:
            self.cur.execute(query, params)

            try:
                records = []
                nrecs = 0
                for record in self.cur:
                    records.append(record)
                    nrecs += 1

                print("nrecs =",nrecs)

            except:
                print("Nothing returned from database query; continuing...")

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error getting all L2Files records for given dateobs range, nframes, and cov5percent ({}); skipping...'.format(error))
            self.exit_code = 67
            return

        return records


########################################################################################################

    def get_field_fid_nframes_records_for_mjdobs_range(self,start_refimage_mjdobs,end_refimage_mjdobs,min_refimage_nframes,fid=None):

        '''
        Query database for all field/filter/nframes combinations in reference-image window with
        minimum number of frames in coadd stack.
        '''

        self.exit_code = 0


        # Define query.

        if fid is None:

            query =\
                "select field,fid,count(*) from l2files " +\
                "where mjdobs >= %s " +\
                "and mjdobs < %s " +\
                "and vbest > 0 " +\
                "group by field,fid " +\
                "having count(*) >= %s " +\
                "order by field,fid;"
            params = (start_refimage_mjdobs, end_refimage_mjdobs, min_refimage_nframes)

        else:

            query =\
                "select field,fid,count(*) from l2files " +\
                "where mjdobs >= %s " +\
                "and mjdobs < %s " +\
                "and vbest > 0 " +\
                "and fid = %s " +\
                "group by field,fid " +\
                "having count(*) >= %s " +\
                "order by field,fid;"
            params = (start_refimage_mjdobs, end_refimage_mjdobs, fid, min_refimage_nframes)


        print('query = {}, params = {}'.format(query, params))


        # Execute query.

        try:
            self.cur.execute(query, params)

            try:
                records = []
                nrecs = 0
                for record in self.cur:
                    records.append(record)
                    nrecs += 1

                print("nrecs =",nrecs)

            except:
                print("Nothing returned from database query; continuing...")

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error executing get_field_fid_nframes_records_for_mjdobs_range ({}); skipping...'.format(error))
            self.exit_code = 67
            return

        return records


########################################################################################################

    def get_l2files_records_for_datetime_range_field_fid(self,startdatetime,enddatetime,field,fid):

        '''
        Query database for all L2Files records associated with the given observation datetime range.
        '''

        self.exit_code = 0


        # Define query.

        query =\
            "select rid,sca,field,fid,mjdobs from L2Files " +\
            "where dateobs >= %s " +\
            "and dateobs < %s " +\
            "and field = %s " +\
            "and fid = %s " +\
            "and vbest > 0 " +\
            "order by mjdobs,sca;"
        params = (startdatetime, enddatetime, field, fid)


        # Query database.

        print('query = {}, params = {}'.format(query, params))


        # Execute query.

        try:
            self.cur.execute(query, params)

            try:
                records = []
                nrecs = 0
                for record in self.cur:
                    records.append(record)
                    nrecs += 1

                print("nrecs =",nrecs)

            except:
                print("Nothing returned from database query; continuing...")

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error getting all L2Files records for given dateobs range ({}); skipping...'.format(error))
            self.exit_code = 67
            return

        return records


########################################################################################################

    def get_l2file_info_for_sources(self,rid):

        '''
        Query select columns in L2Files database table for given RID.
        '''

        self.exit_code = 0


        # Define query template.

        query =\
            "select crval1,crval2,crpix1,crpix2,cd11,cd12,cd21,cd22, " +\
            "expid,sca,fid,field,hp6,hp9,mjdobs,dateobs " +\
            "from L2Files " +\
            "where rid = %s; "


        # Formulate query by substituting parameters into query template.

        print('----> rid = {}'.format(rid))


        params = (rid,)

        print('query = {}, params = {}'.format(query, params))


        # Execute query.

        self.cur.execute(query, params)
        record = self.cur.fetchone()

        record_dict = {}

        if record is not None:
            record_dict["crval1"] = record[0]
            record_dict["crval2"] = record[1]
            record_dict["crpix1"] = record[2]
            record_dict["crpix2"] = record[3]
            record_dict["cd11"] = record[4]
            record_dict["cd12"] = record[5]
            record_dict["cd21"] = record[6]
            record_dict["cd22"] = record[7]
            record_dict["expid"] = record[8]
            record_dict["sca"] = record[9]
            record_dict["fid"] = record[10]
            record_dict["field"] = record[11]
            record_dict["hp6"] = record[12]
            record_dict["hp9"] = record[13]
            record_dict["mjdobs"] = record[14]
            record_dict["dateobs"] = record[14]

        else:
            print("*** Error from get_l2file_recs_for_sources: " +
                  "Could not get select columns from L2Files database record; returning...")
            self.exit_code = 67


        return record_dict


########################################################################################################

    def execute_sql_queries(self,sql_queries,params_list=None,debug=1):

        '''
        Execute list of SQL queries and commit transaction.  If params_list
        is given, params_list[i] are the params for sql_queries[i]; if None
        (default), queries are executed with no params (backward-compatible
        with callers that build complete query text themselves).
        '''

        self.exit_code = 0


        for i,query in enumerate(sql_queries):

            params = params_list[i] if params_list is not None else None

            if debug == 1:
                print('query = {}, params = {}'.format(query, params))


            # Execute query.

            try:
                self.cur.execute(query, params)

                try:
                    records = []
                    nrecs = 0
                    for record in self.cur:
                        if nrecs == 0:            # Print first record returned as a sanity check.
                            if debug == 1:
                                print("From execute_sql_queries: record =",record)
                        records.append(record)
                        nrecs += 1

                    if debug == 1:
                        print("nrecs =",nrecs)

                except:
                    print("Nothing returned from database query; continuing...")

            except (Exception, psycopg2.DatabaseError) as error:
                print(f"*** Error executing query ({query}): {error}; quitting...")
                self.exit_code = 67
                self.conn.rollback()           # Rollback database transaction
                return

        if self.exit_code == 0:
            self.conn.commit()           # Commit database transaction


        # Return records for the last query executed.  This is done for convenience and code is not generalized.

        return records


########################################################################################################

    def copy_data_from_file_into_database(self,csv_file_path,table_name,columns):

        '''
        Copy data from file into specified database table.
        '''

        self.exit_code = 0

        separator = ","
        null_string = "\\N" # Default for PostgreSQL COPY

        print('csv_file_path = {}'.format(csv_file_path))
        print('table_name = {}'.format(table_name))


        # Open the CSV file in read mode and bulk-load specified database table.

        try:

            with open(csv_file_path, 'r') as f:

                self.cur.copy_from(f, table_name, sep=separator, null=null_string, columns=columns)

            self.conn.commit()           # Commit database transaction

        except (Exception, psycopg2.DatabaseError) as error:
            print(f'*** Error bulk-loading data from file ({csv_file_path}) into specified database table ({table_name}); skipping...')
            print(f'*** Exception: {error}')
            self.exit_code = 67
            raise

        return None


########################################################################################################

    def add_astro_object_to_field(self,tablename,ra0,dec0,flux0,field,hp6,hp9,debug=0):

        self.exit_code = 0


        # Define query.

        query = sql.SQL(
            "insert into {tbl}"
            "            (ra0,"
            "             dec0,"
            "             flux0,"
            "             field,"
            "             hp6,"
            "             hp9"
            "            )"
            "            values"
            "            (%s,"
            "             %s,"
            "             %s,"
            "             %s,"
            "             %s,"
            "             %s)"
            "             RETURNING aid;"
        ).format(tbl=sql.Identifier(tablename))
        params = (ra0, dec0, flux0, field, hp6, hp9)

        if debug == 1:
            print('query = {}, params = {}'.format(query, params))


        # Execute query.

        self.cur.execute(query, params)
        record = self.cur.fetchone()

        if record is not None:
            aid = record[0]
        else:
            print('*** Error inserting record into {}; skipping...'.format(tablename))
            self.exit_code = 67
            return

        if self.exit_code == 0:
            self.conn.commit()           # Commit database transaction

        return aid


########################################################################################################

    def add_merge_to_field(self,tablename,aid,sid,debug=0):

        self.exit_code = 0


        # Define query.

        query = sql.SQL(
            "insert into {tbl}"
            "            (aid,"
            "             sid"
            "            )"
            "            values"
            "            (%s,"
            "             %s);"
        ).format(tbl=sql.Identifier(tablename))
        params = (aid, sid)

        if debug == 1:
            print('query = {}, params = {}'.format(query, params))


        # Execute query.

        self.cur.execute(query, params)

        try:
            record = self.cur.fetchone()
        except:
            if debug == 1:
                print(f"Nothing returned from database query ({query}); continuing...")

        if self.exit_code == 0:
            self.conn.commit()           # Commit database transaction


########################################################################################################

    def delete_merge_from_field(self,tablename,sid,debug=0):

        self.exit_code = 0


        # Define query.

        query = sql.SQL("DELETE FROM {tbl} WHERE sid = %s;").format(tbl=sql.Identifier(tablename))
        params = (sid,)

        if debug == 1:
            print('query = {}, params = {}'.format(query, params))


        # Execute query.

        try:
            self.cur.execute(query, params)

            if debug == 1:
                rows_affected = self.cur.rowcount
                print(f"Deleted: {rows_affected} row")

        except (Exception, psycopg2.DatabaseError) as error:
            print(f'*** Error deleting {tablename} record (error={error}); skipping...')
            self.exit_code = 67
            return

        if self.exit_code == 0:
            self.conn.commit()           # Commit database transaction


########################################################################################################

    def delete_source(self,child_tablename,sid,debug=0):

        self.exit_code = 0


        # Define query.

        query = sql.SQL("DELETE FROM {tbl} WHERE sid = %s;").format(tbl=sql.Identifier(child_tablename))
        params = (sid,)

        if debug == 1:
            print('query = {}, params = {}'.format(query, params))


        # Execute query.

        try:
            self.cur.execute(query, params)

            if debug == 1:
                rows_affected = self.cur.rowcount
                print(f"Deleted: {rows_affected} row")

        except (Exception, psycopg2.DatabaseError) as error:
            print(f'*** Error deleting {child_tablename} record (error={error}); skipping...')
            self.exit_code = 67
            return

        if self.exit_code == 0:
            self.conn.commit()           # Commit database transaction


########################################################################################################

    def get_possible_overlapping_diffimages(self,
                                            ppid,
                                            jd_earliest,
                                            field_ra0,
                                            field_dec0,
                                            radius_of_initial_cone_search=None):

        '''
        Query database for PIDs and distances from tile center for all difference images that
        possibly overlap the specified field (a.k.a. sky tile).
        Returned list is ordered by JD.
        '''

        self.exit_code = 0


        # Radius of initial cone search, in angular degrees.

        if radius_of_initial_cone_search is None:
            radius_of_initial_cone_search = 1.0


        # Define query template.

        query =\
            "select pid,expid,sca,a.fid,a.field,jd,ra0,dec0,ra1,dec1,ra2,dec2,ra3,dec3,ra4,dec4, " +\
            "a.filename,a.checksum,infobitssci,infobitsref,a.rfid,b.filename,b.checksum,b.ppid, " +\
            "q3c_dist(ra0, dec0, cast(%s as double precision), cast(%s as double precision)) as dist " +\
            "from DiffImages a, RefImages b " +\
            "where a.rfid = b.rfid " +\
            "and a.ppid = %s " +\
            "and jd >= %s " +\
            "and a.status > 0 " +\
            "and b.status > 0 " +\
            "and a.vbest > 0 " +\
            "and b.vbest > 0 " +\
            "and q3c_radial_query(ra0, dec0, " +\
            "cast(%s as double precision), " +\
            "cast(%s as double precision), " +\
            "cast(%s as double precision)) " +\
            "order by jd; "


        # Formulate query by substituting parameters into query template.

        print(f'----> field_ra0 = {field_ra0}')
        print(f'----> field_dec0 = {field_dec0}')
        print(f'----> radius_of_initial_cone_search = {radius_of_initial_cone_search}')
        print(f'----> jd_earliest = {jd_earliest}')
        print(f'----> radius_of_initial_cone_search = {radius_of_initial_cone_search}')
        print(f'----> ppid = {ppid}')


        params = (field_ra0, field_dec0, ppid, jd_earliest, field_ra0, field_dec0, radius_of_initial_cone_search)

        print('query = {}, params = {}'.format(query, params))


        # Execute query.

        try:
            self.cur.execute(query, params)

            try:
                records = []
                nrecs = 0
                for record in self.cur:
                    records.append(record)
                    nrecs += 1

                print("nrecs =",nrecs)

            except:
                print("Nothing returned from database query; continuing...")

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error from database method RAPIDDB.get_possible_overlapping_diffimages ({}); skipping...'.format(error))
            self.exit_code = 67
            return

        return records


########################################################################################################

    def get_filters(self):

        '''
        Query database for all Filters records.
        '''

        self.exit_code = 0


        # Define query.

        query = f"select fid,filter from Filters order by fid;"


        # Query database.

        print('query = {}'.format(query))


        # Execute query.

        try:
            self.cur.execute(query)

            try:
                records = []
                nrecs = 0
                for record in self.cur:
                    records.append(record)
                    nrecs += 1

                print("nrecs =",nrecs)

            except:
                print("Nothing returned from database query; continuing...")

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error getting all L2Files records for given dateobs range ({}); skipping...'.format(error))
            self.exit_code = 67
            return

        return records


########################################################################################################

    def delete_astroobject_from_field(self,tablename,aid,debug=0):

        self.exit_code = 0


        # Define query.

        query = sql.SQL("DELETE FROM {tbl} WHERE aid = %s;").format(tbl=sql.Identifier(tablename))
        params = (aid,)

        if debug == 1:
            print('query = {}, params = {}'.format(query, params))


        # Execute query.

        try:
            self.cur.execute(query, params)

            if debug == 1:
                rows_affected = self.cur.rowcount
                print(f"Deleted: {rows_affected} row")

        except (Exception, psycopg2.DatabaseError) as error:
            print(f'*** Error deleting {tablename} record (error={error}); skipping...')
            self.exit_code = 67
            return

        if self.exit_code == 0:
            self.conn.commit()           # Commit database transaction


########################################################################################################

    def update_astroobject_mean_sky_position(self,
                                             astroobjects_tablename,
                                             aid,
                                             meanra,
                                             meandec,
                                             nsources,
                                             debug=0):

        '''
        Update select statistics in AstroObjects database record.
        '''

        self.exit_code = 0


        # Define query.

        query = sql.SQL(
            "update {tbl} "
            "set meanra = %s, "
            "meandec = %s, "
            "nsources = %s "
            " where aid = %s;"
        ).format(tbl=sql.Identifier(astroobjects_tablename))
        params = (meanra, meandec, nsources, aid)


        # Query database.

        if debug == 1:
            print('query = {}, params = {}'.format(query, params))


        # Execute query.

        try:
            self.cur.execute(query, params)

            try:
                records = []
                for record in self.cur:
                    records.append(record)
            except:
                if debug == 1:
                    print("Nothing returned from database query; continuing...")

        except (Exception, psycopg2.DatabaseError) as error:
            print(f'*** Error updating mean sky position in astroobjects_tablename record (aid={aid},error={error}); skipping...')
            self.exit_code = 67
            return

        if self.exit_code == 0:
            self.conn.commit()           # Commit database transaction


########################################################################################################

    def delete_redundant_merges_for_astroobject(self,tablename,aid,debug=0):

        self.exit_code = 0


        # Define query.

        query = sql.SQL(
            "DELETE FROM {tbl} "
            "WHERE aid = %s "
            "AND ctid NOT IN "
            "(SELECT MIN(ctid) "
            "FROM {tbl} "
            "GROUP BY aid,sid);"
        ).format(tbl=sql.Identifier(tablename))
        params = (aid,)

        if debug == 1:
            print('query = {}, params = {}'.format(query, params))


        # Execute query.

        try:
            self.cur.execute(query, params)

            n_rows_deleted = self.cur.rowcount

            if debug == 1:
                print(f"Deleted: {n_rows_deleted} rows")

        except (Exception, psycopg2.DatabaseError) as error:
            print(f'*** Error deleting {tablename} record (error={error}); skipping...')
            self.exit_code = 67
            return

        if self.exit_code == 0:
            self.conn.commit()           # Commit database transaction

        return n_rows_deleted


########################################################################################################

    def delete_redundant_merges_for_field(self,tablename,debug=0):

        self.exit_code = 0


        # Define query.

        query = sql.SQL(
            "DELETE FROM {tbl} "
            "WHERE ctid NOT IN "
            "(SELECT MIN(ctid) "
            "FROM {tbl} "
            "GROUP BY aid,sid);"
        ).format(tbl=sql.Identifier(tablename))

        if debug == 1:
            print('query = {}'.format(query))


        # Execute query.

        try:
            self.cur.execute(query)

            n_rows_deleted = self.cur.rowcount

            if debug == 1:
                print(f"Deleted: {n_rows_deleted} rows")

        except (Exception, psycopg2.DatabaseError) as error:
            print(f'*** Error deleting {tablename} record (error={error}); skipping...')
            self.exit_code = 67
            return

        if self.exit_code == 0:
            self.conn.commit()           # Commit database transaction

        return n_rows_deleted


########################################################################################################

    def get_field_fid_nframes_records(self,min_refimage_nframes,fid=None):

        '''
        Query database for all field/filter/nframes combinations with
        minimum number of frames in coadd stack.
        '''

        self.exit_code = 0


        # Define query.

        if fid is None:

            query =\
                "select field,fid,count(*) from l2files " +\
                "where vbest > 0 " +\
                "group by field,fid " +\
                "having count(*) >= %s " +\
                "order by field,fid;"
            params = (min_refimage_nframes,)

        else:

            query =\
                "select field,fid,count(*) from l2files " +\
                "where vbest > 0 " +\
                "and fid = %s " +\
                "group by field,fid " +\
                "having count(*) >= %s " +\
                "order by field,fid;"
            params = (fid, min_refimage_nframes)


        print('query = {}, params = {}'.format(query, params))


        # Execute query.

        try:
            self.cur.execute(query, params)

            try:
                records = []
                nrecs = 0
                for record in self.cur:
                    records.append(record)
                    nrecs += 1

                print(f"fid,min_refimage_nframes,nrecs = {fid},{min_refimage_nframes},{nrecs}")

            except:
                print("Nothing returned from database query; continuing...")

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error executing get_field_fid_nframes_records ({}); skipping...'.format(error))
            self.exit_code = 67
            return

        return records


########################################################################################################

    def insert_astroobjectsmeta_statistics(self,
                                           astroobjectsmeta_tablename,
                                           aid,
                                           meanra,
                                           stdevra,
                                           meandec,
                                           stdevdec,
                                           meanflux,
                                           stdevflux,
                                           nsources,
                                           debug=0):

        '''
        Insert statistics in AstroObjectsMeta_<field> database record.
        '''

        self.exit_code = 0


        # Define query.

        query = sql.SQL(
            "INSERT INTO {tbl} "
            "(aid,meanra,stdevra,meandec,stdevdec,meanflux,stdevflux,nsources) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s);"
        ).format(tbl=sql.Identifier(astroobjectsmeta_tablename))
        params = (aid, meanra, stdevra, meandec, stdevdec, meanflux, stdevflux, nsources)


        # Query database.

        if debug == 1:
            print('query = {}, params = {}'.format(query, params))


        # Execute query.

        try:
            self.cur.execute(query, params)

            try:
                records = []
                for record in self.cur:
                    records.append(record)
            except:
                if debug == 1:
                    print("Nothing returned from database query; continuing...")

        except (Exception, psycopg2.DatabaseError) as error:
            print(f'*** Error inserting astroobjectsmeta_tablename record (aid={aid},error={error}); skipping...')
            self.exit_code = 67
            return

        if self.exit_code == 0:
            self.conn.commit()           # Commit database transaction


########################################################################################################

    def get_ready_test_campaign_units(self):

        '''
        Every READY work_unit of every ACTIVE test-operational-class
        campaign — the campaign gatherer's one enumeration (integration
        review, IR-13-a).

        "a campaign-unit gatherer: a registry row under the test
        operational class whose gathering enumerates ready test-class
        work_units of ACTIVE campaigns" (run ledger, quoted verbatim in
        submission.gathering.gather_campaign_units, this query's one
        caller). One joined query rather than "list active campaigns" then
        "list ready units per campaign": migration 036 makes campaign->
        unit a plain FK (work_units.campaign_id), so the join is a single
        SELECT, and a mission-mock campaign has no scale problem this v1
        needs two round trips to guard against (create_mock_campaign_from_
        staged's own max_units guard bounds it at creation time already).

        Returns (work_unit_id, campaign_id, campaign_name, job_type,
        input_scope) — the exact column order
        submission.gathering.gather_campaign_units unpacks positionally.
        `job_type` is work_units.job_type, read here (not assumed science)
        because gather_campaign_units re-asserts the v1 route restriction
        itself rather than trusting this query to have filtered it —
        "assert at campaign creation and at gathering", the ruling's own
        double-guard.
        '''

        self.exit_code = 0

        query = "select u.work_unit_id, u.campaign_id, c.campaign_name, " +\
                "u.job_type, u.input_scope " +\
                "from work_units u " +\
                "join campaigns c on c.campaign_id = u.campaign_id " +\
                "where c.state = 'active' " +\
                "and c.operational_class = 'test' " +\
                "and u.state = 'ready' " +\
                "and u.superseded_by_unit_id is null " +\
                "order by u.work_unit_id;"

        print('query = {}'.format(query))

        try:
            self.cur.execute(query)

            try:
                records = []
                nrecs = 0
                for record in self.cur:
                    records.append(record)
                    nrecs += 1

                print("nrecs =",nrecs)

            except:
                print("Nothing returned from database query; continuing...")

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error getting ready test-campaign work units ({}); skipping...'.format(error))
            self.exit_code = 67
            return

        return records


########################################################################################################

    def get_campaign_unit_source_l2_identity(self,work_unit_id):

        '''
        One campaign work unit's source L2 identity: (rid, field, fid),
        recorded in that unit's CREATION unit_events row detail by
        pipeline.mock.transformer.create_mock_campaign_from_staged.

        WHY A DETAIL-KEYED READ, NOT A REVERSE EXPOSURE/SCA -> RID QUERY.
        A campaign work unit's identity (work_units.input_scope) names
        (exposure, sca) — the same identity gather_science_units resolves
        through the (field, filter) -> L2Files loop, which always has
        `rid` in hand before it ever needs exposure/SCA. Nothing before
        the campaign gatherer needed to go the OTHER way (exposure/SCA
        back to rid), so no such query previously existed; rather than add
        one against L2Files (which is not itself unique on exposure/SCA
        without also disambiguating on vbest/version — a second place to
        get that logic right), the source rid is carried explicitly in the
        creation event's jsonb detail, exactly as create_mock_campaign
        already carries generation_id/manifest_key there for the schedule-
        staging path (pipeline/mock/transformer.py).

        Reads the unit's CREATION event specifically (from_state is null —
        migration 036: "from_state NULL on the unit's first event
        (creation)") rather than the most recent event, because detail is
        per-event and only the creation event is where
        create_mock_campaign_from_staged writes this fact; a later
        transition's detail (if any) carries a different, unrelated
        payload and reading "the latest row" would silently pick it up
        instead once a unit has been transitioned.

        Returns (rid, field, fid) as a single row, or a row of (None, None,
        None) if no creation event or no such keys in its detail — the
        caller (submission.gathering._campaign_unit_l2_identity) treats
        either as "not recorded" and raises, since a campaign unit must
        always have been created with this detail.
        '''

        self.exit_code = 0

        query = "select (detail->>'source_rid')::int, " +\
                "(detail->>'source_field')::int, " +\
                "(detail->>'source_fid')::int " +\
                "from unit_events " +\
                "where work_unit_id = %s " +\
                "and from_state is null " +\
                "order by occurred_at " +\
                "limit 1;"
        params = (work_unit_id,)

        print('query = {}, params = {}'.format(query, params))

        try:
            self.cur.execute(query, params)
            record = self.cur.fetchone()

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error getting source L2 identity for work unit {} ({}); skipping...'.format(work_unit_id,error))
            self.exit_code = 67
            return

        if record is None:
            return (None, None, None)

        return record
