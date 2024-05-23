import os
import psycopg2
import re
import hashlib

debug = 1


# Common methods.

def md5(fname):
    hash_md5 = hashlib.md5()

    try:
        with open(fname, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()
    except:
        print("*** Error: Cannot open file to compute checksum =",fname,"; quitting...")
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
        67 = Could not execute database query.
        68 = Could not open file to compute checksum.
    """

    def __init__(self):

        self.exit_code = 0
        self.conn = None


        # Get database connection parameters from environment.

        dbport = os.getenv('DBPORT')
        dbname = os.getenv('DBNAME')
        dbuser = os.getenv('DBUSER')
        dbpass = os.getenv('DBPASS')
        dbserver = os.getenv('DBSERVER')


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


    def close(self):

        '''
        Close database cursor and then connection.
        '''


        try:
            self.cur.close()
        except (Exception, psycopg2.DatabaseError) as error:
            print(error)
            self.exit_code = 2
        finally:
            if self.conn is not None:
                self.conn.close()
                print('Database connection closed.')


    def add_exposure(self,dateobs,mjdobs,field,filter,exptime,infobits,status):

        '''
        Add record in Exposures database table.
        '''


        # Define query template.

        query_template =\
            "select * from addExposure(" +\
            "cast('TEMPLATE_DATEOBS' as timestamp)," +\
            "cast(TEMPLATE_MJDOBS as double precision)," +\
            "cast(TEMPLATE_FIELD as integer)," +\
            "cast('TEMPLATE_FILTER' as character varying(16))," +\
            "cast(TEMPLATE_EXPTIME as real), " +\
            "cast(TEMPLATE_INFOBITS as integer), " +\
            "cast(TEMPLATE_STATUS as smallint)) as " +\
            "(expid integer," +\
            " fid smallint);"

        # Query database.

        print('----> dateobs = {}'.format(dateobs))
        print('----> mjdobs = {}'.format(mjdobs))
        print('----> field = {}'.format(field))
        print('----> filter = {}'.format(filter))
        print('----> exptime = {}'.format(exptime))
        print('----> infobits = {}'.format(infobits))
        print('----> status = {}'.format(status))

        mjdobs_str = str(mjdobs)
        field_str = str(field)
        exptime_str = str(exptime)
        infobits_str = str(infobits)
        status_str = str(status)

        rep = {"TEMPLATE_DATEOBS": dateobs,
               "TEMPLATE_MJDOBS": mjdobs_str,
               "TEMPLATE_FIELD": field_str,
               "TEMPLATE_FILTER": filter,
               "TEMPLATE_EXPTIME": exptime_str}

        rep["TEMPLATE_INFOBITS"] = infobits_str
        rep["TEMPLATE_STATUS"] = status_str

        rep = dict((re.escape(k), v) for k, v in rep.items())
        pattern = re.compile("|".join(rep.keys()))
        query = pattern.sub(lambda m: rep[re.escape(m.group(0))], query_template)

        print('query = {}'.format(query))

        self.cur.execute(query)
        record = self.cur.fetchone()

        if record is not None:
            self.expid = record[0]
            self.fid = record[1]
        else:
            self.expid = None
            self.fid = None
            print("*** Error: Could not insert Exposures record; quitting...")
            self.exit_code = 67
            return

        if self.exit_code == 0:
            self.conn.commit()           # Commit database transaction


    def add_l2file(self,expid,chipid,field,fid,dateobs,mjdobs,exptime,infobits,
        status,filename,checksum,crval1,crval2,crpix1,crpix2,cd11,cd12,cd21,cd22,
        ctype1,ctype2,cunit1,cunit2,a_order,a_0_2,a_0_3,a_0_4,a_1_1,a_1_2,
        a_1_3,a_2_0,a_2_1,a_2_2,a_3_0,a_3_1,a_4_0,b_order,b_0_2,b_0_3,
        b_0_4,b_1_1,b_1_2,b_1_3,b_2_0,b_2_1,b_2_2,b_3_0,b_3_1,
        b_4_0,equinox,ra,dec,paobsy,pafpa,zptmag,skymean):

        '''
        Add record in L2files database table.
        '''


        # Define query template.

        query_template =\
            "select * from addL2File(" +\
            "cast(TEMPLATE_EXPID as integer)," +\
            "cast(TEMPLATE_CHIPID as smallint)," +\
            "cast(TEMPLATE_FIELD as integer)," +\
            "cast(TEMPLATE_FID as smallint)," +\
            "cast('TEMPLATE_DATEOBS' as timestamp without time zone)," +\
            "cast(TEMPLATE_MJDOBS as double precision)," +\
            "cast(TEMPLATE_EXPTIME as real)," +\
            "cast(TEMPLATE_INFOBITS as integer)," +\
            "cast('TEMPLATE_FILENAME' as character varying(255))," +\
            "cast('TEMPLATE_CHECKSUM' as character varying(32))," +\
            "cast(TEMPLATE_STATUS as smallint)," +\
            "cast(TEMPLATE_CRVAL1 as double precision)," +\
            "cast(TEMPLATE_CRVAL2 as double precision)," +\
            "cast(TEMPLATE_CRPIX1 as real)," +\
            "cast(TEMPLATE_CRPIX2 as real)," +\
            "cast(TEMPLATE_CD11 as double precision)," +\
            "cast(TEMPLATE_CD12 as double precision)," +\
            "cast(TEMPLATE_CD21 as double precision)," +\
            "cast(TEMPLATE_CD22 as double precision)," +\
            "cast('TEMPLATE_CTYPE1' as character varying(16))," +\
            "cast('TEMPLATE_CTYPE2' as character varying(16))," +\
            "cast('TEMPLATE_CUNIT1' as character varying(16))," +\
            "cast('TEMPLATE_CUNIT2' as character varying(16))," +\
            "cast(TEMPLATE_A_ORDER as smallint)," +\
            "cast(TEMPLATE_A_0_2 as double precision)," +\
            "cast(TEMPLATE_A_0_3 as double precision)," +\
            "cast(TEMPLATE_A_0_4 as double precision)," +\
            "cast(TEMPLATE_A_1_1 as double precision)," +\
            "cast(TEMPLATE_A_1_2 as double precision)," +\
            "cast(TEMPLATE_A_1_3 as double precision)," +\
            "cast(TEMPLATE_A_2_0 as double precision)," +\
            "cast(TEMPLATE_A_2_1 as double precision)," +\
            "cast(TEMPLATE_A_2_2 as double precision)," +\
            "cast(TEMPLATE_A_3_0 as double precision)," +\
            "cast(TEMPLATE_A_3_1 as double precision)," +\
            "cast(TEMPLATE_A_4_0 as double precision)," +\
            "cast(TEMPLATE_B_ORDER as smallint)," +\
            "cast(TEMPLATE_B_0_2 as double precision)," +\
            "cast(TEMPLATE_B_0_3 as double precision)," +\
            "cast(TEMPLATE_B_0_4 as double precision)," +\
            "cast(TEMPLATE_B_1_1 as double precision)," +\
            "cast(TEMPLATE_B_1_2 as double precision)," +\
            "cast(TEMPLATE_B_1_3 as double precision)," +\
            "cast(TEMPLATE_B_2_0 as double precision)," +\
            "cast(TEMPLATE_B_2_1 as double precision)," +\
            "cast(TEMPLATE_B_2_2 as double precision)," +\
            "cast(TEMPLATE_B_3_0 as double precision)," +\
            "cast(TEMPLATE_B_3_1 as double precision)," +\
            "cast(TEMPLATE_B_4_0 as double precision)," +\
            "cast(TEMPLATE_EQUINOX as real)," +\
            "cast(TEMPLATE_RA as double precision)," +\
            "cast(TEMPLATE_DEC as double precision)," +\
            "cast(TEMPLATE_PAOBSY as real)," +\
            "cast(TEMPLATE_PAFPA as real)," +\
            "cast(TEMPLATE_ZPTMAG as real)," +\
            "cast(TEMPLATE_SKYMEAN AS real)) as " +\
            "(rid integer," +\
            " version smallint);"

        # Query database.

        print('----> expid = {}'.format(expid))
        print('----> chipid = {}'.format(chipid))
        print('----> filename = {}'.format(filename))

        rep = {"TEMPLATE_EXPID": str(expid),
               "TEMPLATE_CHIPID": str(chipid),
               "TEMPLATE_FIELD": str(field),
               "TEMPLATE_FID": str(fid),
               "TEMPLATE_DATEOBS": dateobs}

        rep["TEMPLATE_MJDOBS"] = str(mjdobs)
        rep["TEMPLATE_EXPTIM"] = str(exptime)
        rep["TEMPLATE_INFOBITS"] = str(infobits)
        rep["TEMPLATE_FILENAME"] = filename
        rep["TEMPLATE_CHECKSUM"] = checksum
        rep["TEMPLATE_STATUS"] = str(status)
        rep["TEMPLATE_CRVAL1"] = str(crval1)
        rep["TEMPLATE_CRVAL2"] = str(crval2)
        rep["TEMPLATE_CRPIX1"] = str(crpix1)
        rep["TEMPLATE_CRPIX2"] = str(crpix2)
        rep["TEMPLATE_CD11"] = str(cd11)
        rep["TEMPLATE_CD12"] = str(cd12)
        rep["TEMPLATE_CD21"] = str(cd21)
        rep["TEMPLATE_CD22"] = str(cd22)
        rep["TEMPLATE_CTYPE1"] = ctype1
        rep["TEMPLATE_CTYPE2"] = ctype2
        rep["TEMPLATE_CUNIT1"] = cunit1
        rep["TEMPLATE_CUNIT2"] = cunit2
        rep["TEMPLATE_A_ORDER"] = str(a_order)
        rep["TEMPLATE_A_0_2"] = str(a_0_2)
        rep["TEMPLATE_A_0_3"] = str(a_0_3)
        rep["TEMPLATE_A_0_4"] = str(a_0_4)
        rep["TEMPLATE_A_1_1"] = str(a_1_1)
        rep["TEMPLATE_A_1_2"] = str(a_1_2)
        rep["TEMPLATE_A_1_3"] = str(a_1_3)
        rep["TEMPLATE_A_2_0"] = str(a_2_0)
        rep["TEMPLATE_A_2_1"] = str(a_2_1)
        rep["TEMPLATE_A_2_2"] = str(a_2_2)
        rep["TEMPLATE_A_3_0"] = str(a_3_0)
        rep["TEMPLATE_A_3_1"] = str(a_3_1)
        rep["TEMPLATE_A_4_0"] = str(a_4_0)
        rep["TEMPLATE_B_ORDER"] = str(b_order)
        rep["TEMPLATE_B_0_2"] = str(b_0_2)
        rep["TEMPLATE_B_0_3"] = str(b_0_3)
        rep["TEMPLATE_B_0_4"] = str(b_0_4)
        rep["TEMPLATE_B_1_1"] = str(b_1_1)
        rep["TEMPLATE_B_1_2"] = str(b_1_2)
        rep["TEMPLATE_B_1_3"] = str(b_1_3)
        rep["TEMPLATE_B_2_0"] = str(b_2_0)
        rep["TEMPLATE_B_2_1"] = str(b_2_1)
        rep["TEMPLATE_B_2_2"] = str(b_2_2)
        rep["TEMPLATE_B_3_0"] = str(b_3_0)
        rep["TEMPLATE_B_3_1"] = str(b_3_1)
        rep["TEMPLATE_B_4_0"] = str(b_4_0)
        rep["TEMPLATE_EQUINOX"] = str(equinox)
        rep["TEMPLATE_RA"] = str(ra)
        rep["TEMPLATE_DEC"] = str(dec)
        rep["TEMPLATE_PAOBSY"] = str(paobsy)
        rep["TEMPLATE_PAFPA"] = str(pafpa)
        rep["TEMPLATE_ZPTMAG"] = str(zptmag)
        rep["TEMPLATE_SKYMEAN"] = str(skymean)

        rep = dict((re.escape(k), v) for k, v in rep.items())
        pattern = re.compile("|".join(rep.keys()))
        query = pattern.sub(lambda m: rep[re.escape(m.group(0))], query_template)

        print('query = {}'.format(query))

        self.cur.execute(query)
        record = self.cur.fetchone()

        if record is not None:
            self.rid = record[0]
            self.version = record[1]
        else:
            self.rid = None
            self.version = None
            print("*** Error: Could not insert L2Files record; quitting...")
            self.exit_code = 67
            return

        if self.exit_code == 0:
            self.conn.commit()           # Commit database transaction


    def update_l2file(self,rid,checksum,status,version):

        '''
        Update record in L2files database table.
        '''


        # Define query template.

        query_template =\
            "select * from updateL2File(" +\
            "cast(TEMPLATE_RID as integer)," +\
            "cast('TEMPLATE_FILENAME' as character varying(255))," +\
            "cast('TEMPLATE_CHECKSUM' as character varying(32))," +\
            "cast(TEMPLATE_STATUS as smallint)," +\
            "cast(TEMPLATE_VERSION AS smallint));"

        # Query database.

        print('----> rid = {}'.format(rid))
        print('----> filename = {}'.format(filename))
        print('----> checksum = {}'.format(checksum))
        print('----> status = {}'.format(status))
        print('----> version = {}'.format(version))

        rep = {"TEMPLATE_RID": str(rid),
               "TEMPLATE_FILENAME": filename,
               "TEMPLATE_CHECKSUM": checksum}

        rep["TEMPLATE_STATUS"] = str(status)
        rep["TEMPLATE_VERSION"] = str(version)

        rep = dict((re.escape(k), v) for k, v in rep.items())
        pattern = re.compile("|".join(rep.keys()))
        query = pattern.sub(lambda m: rep[re.escape(m.group(0))], query_template)

        print('query = {}'.format(query))

        # Execute query.

        try:
            self.cur.execute(query)

            try:
                for record in cur:
                    print(record)
            except:
                    print("Nothing returned from database stored function; continuing...")

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error updating L2Files record ({}); skipping...'.format(error))
            self.exit_code = 67
            return

        if self.exit_code == 0:
            self.conn.commit()           # Commit database transaction
