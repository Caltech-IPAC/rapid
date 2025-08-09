import os
import psycopg2
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


########################################################################################################
########################################################################################################
########################################################################################################

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
    """


########################################################################################################

    def __init__(self):

        self.exit_code = 0
        self.conn = None


        # Get database connection parameters from environment.

        dbport = os.getenv('DBPORT')
        dbname = os.getenv('DBNAME')
        dbuser = os.getenv('DBUSER')
        dbpass = os.getenv('DBPASS')
        dbserver = os.getenv('DBSERVER')

        print("dbserver,dbname,dbport,dbuser =",dbserver,dbname,dbport,dbuser)


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


########################################################################################################

    def add_exposure(self,dateobs,mjdobs,field,hp6,hp9,filter,exptime,infobits,status):

        '''
        Add record in Exposures database table.
        '''

        self.exit_code = 0


        # Define query template.

        query_template =\
            "select * from addExposure(" +\
            "cast('TEMPLATE_DATEOBS' as timestamp)," +\
            "cast(TEMPLATE_MJDOBS as double precision)," +\
            "cast(TEMPLATE_FIELD as integer)," +\
            "cast(TEMPLATE_HP6 as integer)," +\
            "cast(TEMPLATE_HP9 as integer)," +\
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
        print('----> hp6 = {}'.format(hp6))
        print('----> hp9 = {}'.format(hp9))
        print('----> filter = {}'.format(filter))
        print('----> exptime = {}'.format(exptime))
        print('----> infobits = {}'.format(infobits))
        print('----> status = {}'.format(status))

        mjdobs_str = str(mjdobs)
        field_str = str(field)
        hp6_str = str(hp6)
        hp9_str = str(hp9)
        exptime_str = str(exptime)
        infobits_str = str(infobits)
        status_str = str(status)

        rep = {"TEMPLATE_DATEOBS": dateobs,
               "TEMPLATE_MJDOBS": mjdobs_str,
               "TEMPLATE_FIELD": field_str,
               "TEMPLATE_HP6": hp6_str,
               "TEMPLATE_HP9": hp9_str,
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
            print("*** Error: Could not insert or update Exposures record; returning...")
            self.exit_code = 67
            return

        if self.exit_code == 0:
            self.conn.commit()           # Commit database transaction


########################################################################################################

    def add_l2file(self,expid,sca,field,hp6,hp9,fid,dateobs,mjdobs,exptime,infobits,
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

        query_template =\
            "select * from addL2File(" +\
            "cast(TEMPLATE_EXPID as integer)," +\
            "cast(TEMPLATE_SCA as smallint)," +\
            "cast(TEMPLATE_FIELD as integer)," +\
            "cast(TEMPLATE_HP6 as integer)," +\
            "cast(TEMPLATE_HP9 as integer)," +\
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
        print('----> sca = {}'.format(sca))
        print('----> filename = {}'.format(filename))

        rep = {"TEMPLATE_EXPID": str(expid),
               "TEMPLATE_SCA": str(sca),
               "TEMPLATE_FIELD": str(field),
               "TEMPLATE_HP6": str(hp6),
               "TEMPLATE_HP9": str(hp9),
               "TEMPLATE_FID": str(fid),
               "TEMPLATE_DATEOBS": dateobs}

        rep["TEMPLATE_MJDOBS"] = str(mjdobs)
        rep["TEMPLATE_EXPTIME"] = str(exptime)
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

        query_template =\
            "select * from registerL2FileMeta(" +\
            "cast(TEMPLATE_RID as integer)," +\
            "cast(TEMPLATE_FID as smallint)," +\
            "cast(TEMPLATE_SCA as smallint)," +\
            "cast(TEMPLATE_RA0 as double precision)," +\
            "cast(TEMPLATE_DEC0 as double precision)," +\
            "cast(TEMPLATE_RA1 as double precision)," +\
            "cast(TEMPLATE_DEC1 as double precision)," +\
            "cast(TEMPLATE_RA2 as double precision)," +\
            "cast(TEMPLATE_DEC2 as double precision)," +\
            "cast(TEMPLATE_RA3 as double precision)," +\
            "cast(TEMPLATE_DEC3 as double precision)," +\
            "cast(TEMPLATE_RA4 as double precision)," +\
            "cast(TEMPLATE_DEC4 as double precision)," +\
            "cast(TEMPLATE_Z as double precision)," +\
            "cast(TEMPLATE_Y as double precision)," +\
            "cast(TEMPLATE_Z AS double precision)," +\
            "cast(TEMPLATE_HP6 AS integer)," +\
            "cast(TEMPLATE_HP9 AS integer)," +\
            "cast(TEMPLATE_MJDOBS as double precision));"


        # Query database.

        print('----> rid = {}'.format(rid))
        print('----> fid = {}'.format(fid))
        print('----> sca = {}'.format(sca))
        print('----> ra0 = {}'.format(ra0))
        print('----> dec0 = {}'.format(dec0))

        rep = {"TEMPLATE_RID": str(rid)}

        rep["TEMPLATE_FID"] = str(fid)
        rep["TEMPLATE_SCA"] = str(sca)

        rep["TEMPLATE_RA0"] = str(ra0)
        rep["TEMPLATE_DEC0"] = str(dec0)
        rep["TEMPLATE_RA1"] = str(ra1)
        rep["TEMPLATE_DEC1"] = str(dec1)
        rep["TEMPLATE_RA2"] = str(ra2)
        rep["TEMPLATE_DEC2"] = str(dec2)
        rep["TEMPLATE_RA3"] = str(ra3)
        rep["TEMPLATE_DEC3"] = str(dec3)
        rep["TEMPLATE_RA4"] = str(ra4)
        rep["TEMPLATE_DEC4"] = str(dec4)
        rep["TEMPLATE_X"] = str(x)
        rep["TEMPLATE_Y"] = str(y)
        rep["TEMPLATE_Z"] = str(z)
        rep["TEMPLATE_HP6"] = str(hp6)
        rep["TEMPLATE_HP9"] = str(hp9)
        rep["TEMPLATE_MJDOBS"] = str(mjdobs)

        rep = dict((re.escape(k), v) for k, v in rep.items())
        pattern = re.compile("|".join(rep.keys()))
        query = pattern.sub(lambda m: rep[re.escape(m.group(0))], query_template)

        print('query = {}'.format(query))


        # Execute query.

        try:
            self.cur.execute(query)

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

        query = "update L2FileMeta set hp6 = " + str(hp6) + " where rid = " + str(rid) + ";"


        # Query database.

        print('----> rid = {}'.format(rid))
        print('----> hp6 = {}'.format(hp6))

        print('query = {}'.format(query))


        # Execute query.

        try:
            self.cur.execute(query)

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

        query = "select rid,fid,sca from L2Files;"


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

        query = "update L2FileMeta set fid = " + str(fid) + ", sca = " + str(sca) + " where rid = " + str(rid) + ";"


        # Query database.

        print('----> rid = {}'.format(rid))
        print('----> fid = {}'.format(fid))
        print('----> sca = {}'.format(sca))

        print('query = {}'.format(query))


        # Execute query.

        try:
            self.cur.execute(query)

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

        query = "update L2FileMeta set hp9 = " + str(hp9) + " where rid = " + str(rid) + ";"


        # Query database.

        print('----> rid = {}'.format(rid))
        print('----> hp9 = {}'.format(hp9))

        print('query = {}'.format(query))


        # Execute query.

        try:
            self.cur.execute(query)

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

        query = "select a.rid, ra0, dec0 from L2Files a, L2FileMeta b where a.rid = b.rid;"


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

        query = "update L2Files set field = " + str(field) +\
            ", hp6 = " + str(hp6) +\
            ", hp9 = " + str(hp9) +\
            " where rid = " + str(rid) + ";"


        # Query database.

        print('----> rid = {}'.format(rid))
        print('----> field = {}'.format(field))
        print('----> hp6 = {}'.format(hp6))
        print('----> hp9 = {}'.format(hp9))

        print('query = {}'.format(query))


        # Execute query.

        try:
            self.cur.execute(query)

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

        query = "select distinct expid, ra, dec from L2Files;"


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

        query = "update Exposures set field = " + str(field) +\
            ", hp6 = " + str(hp6) +\
            ", hp9 = " + str(hp9) +\
            " where expid = " + str(expid) + ";"


        # Query database.

        print('----> expid = {}'.format(expid))
        print('----> field = {}'.format(field))
        print('----> hp6 = {}'.format(hp6))
        print('----> hp9 = {}'.format(hp9))

        print('query = {}'.format(query))


        # Execute query.

        try:
            self.cur.execute(query)

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

        query_template =\
            "select sca,fid,ra0,dec0,ra1,dec1,ra2,dec2,ra3,dec3,ra4,dec4 from L2FileMeta where rid=TEMPLATE_RID;"


        # Query database.

        print('----> rid = {}'.format(rid))

        rid_str = str(rid)

        rep = {"TEMPLATE_RID": rid_str}

        rep = dict((re.escape(k), v) for k, v in rep.items())
        pattern = re.compile("|".join(rep.keys()))
        query = pattern.sub(lambda m: rep[re.escape(m.group(0))], query_template)

        print('query = {}'.format(query))


        # Execute query.

        self.cur.execute(query)
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
                                radius_of_initial_cone_search=None):

        '''
        Query database for RIDs and distances from tile center for all science images that
        overlap the sky tile associated with the input science image and its filter and
        that were acquired before the input science image.
        Returned list is ordered by distance from tile center.
        '''

        self.exit_code = 0


        # Radius of initial cone search, in angular degrees.

        if radius_of_initial_cone_search is None:
            radius_of_initial_cone_search = 0.18


        # Define query template.

        query_template =\
            "select rid,ra0,dec0,ra1,dec1,ra2,dec2,ra3,dec3,ra4,dec4,q3c_dist(ra0, dec0, cast(TEMPLATE_RA0 as double precision), cast(TEMPLATE_DEC0 as double precision)) as dist " +\
            "from L2FileMeta " +\
            "where fid = TEMPLATE_FID " +\
            "and q3c_radial_query(ra0, dec0, cast(TEMPLATE_RA0 as double precision), cast(TEMPLATE_DEC0 as double precision), cast(TEMPLATE_RADIUS as double precision)) " +\
            "and (q3c_poly_query(ra1, dec1, array[cast(TEMPLATE_RA1 as double precision), cast(TEMPLATE_DEC1 as double precision)," +\
                                                 "cast(TEMPLATE_RA2 as double precision), cast(TEMPLATE_DEC2 as double precision)," +\
                                                 "cast(TEMPLATE_RA3 as double precision), cast(TEMPLATE_DEC3 as double precision)," +\
                                                 "cast(TEMPLATE_RA4 as double precision), cast(TEMPLATE_DEC4 as double precision)]) " +\
            "or q3c_poly_query(ra2, dec2, array[cast(TEMPLATE_RA1 as double precision), cast(TEMPLATE_DEC1 as double precision)," +\
                                               "cast(TEMPLATE_RA2 as double precision), cast(TEMPLATE_DEC2 as double precision)," +\
                                               "cast(TEMPLATE_RA3 as double precision), cast(TEMPLATE_DEC3 as double precision)," +\
                                               "cast(TEMPLATE_RA4 as double precision), cast(TEMPLATE_DEC4 as double precision)]) " +\
            "or q3c_poly_query(ra3, dec3, array[cast(TEMPLATE_RA1 as double precision), cast(TEMPLATE_DEC1 as double precision)," +\
                                               "cast(TEMPLATE_RA2 as double precision), cast(TEMPLATE_DEC2 as double precision)," +\
                                               "cast(TEMPLATE_RA3 as double precision), cast(TEMPLATE_DEC3 as double precision)," +\
                                               "cast(TEMPLATE_RA4 as double precision), cast(TEMPLATE_DEC4 as double precision)]) " +\
            "or q3c_poly_query(ra4, dec4, array[cast(TEMPLATE_RA1 as double precision), cast(TEMPLATE_DEC1 as double precision)," +\
                                               "cast(TEMPLATE_RA2 as double precision), cast(TEMPLATE_DEC2 as double precision)," +\
                                               "cast(TEMPLATE_RA3 as double precision), cast(TEMPLATE_DEC3 as double precision)," +\
                                               "cast(TEMPLATE_RA4 as double precision), cast(TEMPLATE_DEC4 as double precision)]) " +\
            "or q3c_poly_query(ra0, dec0, array[cast(TEMPLATE_RA1 as double precision), cast(TEMPLATE_DEC1 as double precision)," +\
                                               "cast(TEMPLATE_RA2 as double precision), cast(TEMPLATE_DEC2 as double precision)," +\
                                               "cast(TEMPLATE_RA3 as double precision), cast(TEMPLATE_DEC3 as double precision)," +\
                                               "cast(TEMPLATE_RA4 as double precision), cast(TEMPLATE_DEC4 as double precision)])) " +\
            "and mjdobs >= TEMPLATE_STARTMJDOBS " +\
            "and mjdobs < TEMPLATE_ENDMJDOBS " +\
            "and rid != TEMPLATE_RID " +\
            "order by dist; "


        # Special logic for generating reference image from inputs observed within a certain observation date range.
        # If STARTREFIMMJDOBS is set, then so must ENDREFIMMJDOBS.

        start_refimage_mjdobs = os.getenv('STARTREFIMMJDOBS')

        if start_refimage_mjdobs is not None:

            end_refimage_mjdobs = os.getenv('ENDREFIMMJDOBS')

            if end_refimage_mjdobs is None:

                print("*** Error: Env. var. ENDREFIMMJDOBS not set; quitting...")
                exit(64)

            start_mjdobs = start_refimage_mjdobs
            end_mjdobs = end_refimage_mjdobs
        else:
            start_mjdobs = 0.0
            end_mjdobs = mjdobs


        # Formulate query by substituting parameters into query template.

        print('----> rid = {}'.format(rid))
        print('----> fid = {}'.format(fid))
        print('----> radius_of_initial_cone_search = {}'.format(radius_of_initial_cone_search))

        rep = {"TEMPLATE_RID": str(rid)}

        rep["TEMPLATE_FID"] = str(fid)
        rep["TEMPLATE_STARTMJDOBS"] = str(start_mjdobs)
        rep["TEMPLATE_ENDMJDOBS"] = str(end_mjdobs)
        rep["TEMPLATE_RA0"] = str(field_ra0)
        rep["TEMPLATE_DEC0"] = str(field_dec0)
        rep["TEMPLATE_RA1"] = str(field_ra1)
        rep["TEMPLATE_DEC1"] = str(field_dec1)
        rep["TEMPLATE_RA2"] = str(field_ra2)
        rep["TEMPLATE_DEC2"] = str(field_dec2)
        rep["TEMPLATE_RA3"] = str(field_ra3)
        rep["TEMPLATE_DEC3"] = str(field_dec3)
        rep["TEMPLATE_RA4"] = str(field_ra4)
        rep["TEMPLATE_DEC4"] = str(field_dec4)
        rep["TEMPLATE_RADIUS"] = str(radius_of_initial_cone_search)

        rep = dict((re.escape(k), v) for k, v in rep.items())
        pattern = re.compile("|".join(rep.keys()))
        query = pattern.sub(lambda m: rep[re.escape(m.group(0))], query_template)

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
            print('*** Error from database method RAPIDDB.get_overlapping_l2files ({}); skipping...'.format(error))
            self.exit_code = 67
            return

        return records


########################################################################################################

    def get_info_for_l2file(self,rid):

        '''
        Query select columns in L2Files database table for given RID.
        '''

        self.exit_code = 0


        # Define query template.

        query_template =\
            "select filename,expid,sca,field,mjdobs,exptime,infobits,status,vbest,version " +\
            "from L2Files " +\
            "where rid = TEMPLATE_RID; "


        # Formulate query by substituting parameters into query template.

        print('----> rid = {}'.format(rid))

        rep = {"TEMPLATE_RID": str(rid)}

        rep = dict((re.escape(k), v) for k, v in rep.items())
        pattern = re.compile("|".join(rep.keys()))
        query = pattern.sub(lambda m: rep[re.escape(m.group(0))], query_template)

        print('query = {}'.format(query))


        # Execute query.

        self.cur.execute(query)
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

        query_template =\
            "select rfid,filename,infobits,version " +\
            "from RefImages " +\
            "where vbest > 0 " +\
            "and status > 0 " +\
            "and ppid = TEMPLATE_PPID " +\
            "and field = TEMPLATE_FIELD " +\
            "and fid = TEMPLATE_FID; "


        # Formulate query by substituting parameters into query template.

        print('----> ppid = {}'.format(ppid))
        print('----> field = {}'.format(field))
        print('----> fid = {}'.format(fid))

        rep = {"TEMPLATE_PPID": str(ppid)}

        rep["TEMPLATE_FIELD"] = str(field)
        rep["TEMPLATE_FID"] = str(fid)

        rep = dict((re.escape(k), v) for k, v in rep.items())
        pattern = re.compile("|".join(rep.keys()))
        query = pattern.sub(lambda m: rep[re.escape(m.group(0))], query_template)

        print('query = {}'.format(query))


        # Execute query.

        self.cur.execute(query)
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

        query_template =\
            "select jid from startJob(" +\
            "cast('TEMPLATE_PPID' as smallint)," +\
            "cast(TEMPLATE_FID as smallint)," +\
            "cast(TEMPLATE_EXPID as integer)," +\
            "cast(TEMPLATE_FIELD as integer)," +\
            "cast(TEMPLATE_SCA as smallint)," +\
            "cast(TEMPLATE_RID as integer), " +\
            "cast(TEMPLATE_MACHINE as smallint), " +\
            "cast(TEMPLATE_SLURM as integer)) as jid;"


        # Query database.

        print('----> ppid = {}'.format(ppid))
        print('----> fid = {}'.format(fid))
        print('----> expid = {}'.format(expid))
        print('----> field = {}'.format(field))
        print('----> sca = {}'.format(sca))
        print('----> rid = {}'.format(rid))

        ppid_str = str(ppid)
        fid_str = str(fid)
        expid_str = str(expid)
        field_str = str(field)
        sca_str = str(sca)
        rid_str = str(rid)

        rep = {"TEMPLATE_PPID": ppid_str,
               "TEMPLATE_FID": fid_str,
               "TEMPLATE_EXPID": expid_str,
               "TEMPLATE_FIELD": field_str}

        rep["TEMPLATE_SCA"] = sca_str
        rep["TEMPLATE_RID"] = rid_str
        rep["TEMPLATE_MACHINE"] = str(machine)
        rep["TEMPLATE_SLURM"] = str(slurm)

        rep = dict((re.escape(k), v) for k, v in rep.items())
        pattern = re.compile("|".join(rep.keys()))
        query = pattern.sub(lambda m: rep[re.escape(m.group(0))], query_template)

        print('query = {}'.format(query))

        self.cur.execute(query)
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

            query_template =\
                "select from endJob(" +\
                "cast(TEMPLATE_JID as integer)," +\
                "cast(TEMPLATE_EXITCODE as smallint)," +\
                "cast('TEMPLATE_AWSBATJOBID' as varchar(64)));"

        else:

            query_template =\
                "select from endJob(" +\
                "cast(TEMPLATE_JID as integer)," +\
                "cast(TEMPLATE_EXITCODE as smallint)," +\
                "cast('TEMPLATE_AWSBATJOBID' as varchar(64)),"+\
                "cast('TEMPLATE_STARTED' as timestamp),"+\
                "cast('TEMPLATE_ENDED' as timestamp));"


        # Query database.

        print('----> jid = {}'.format(jid))
        print('----> job_exitcode = {}'.format(job_exitcode))

        jid_str = str(jid)
        job_exitcode_str = str(job_exitcode)

        rep = {"TEMPLATE_JID": jid_str,
               "TEMPLATE_EXITCODE": job_exitcode_str}

        rep["TEMPLATE_AWSBATJOBID"] = aws_batch_job_id

        if ended is not None:
            rep["TEMPLATE_STARTED"] = started
            rep["TEMPLATE_ENDED"] = ended


        rep = dict((re.escape(k), v) for k, v in rep.items())
        pattern = re.compile("|".join(rep.keys()))
        query = pattern.sub(lambda m: rep[re.escape(m.group(0))], query_template)

        print('query = {}'.format(query))

        self.cur.execute(query)
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

        query = "update Jobs set awsbatchjobid = '" + str(aws_batch_job_id) + "' where jid = " + str(jid) + ";"


        # Query database.

        print('----> jid = {}'.format(jid))
        print('----> awsbatchjobid = {}'.format(aws_batch_job_id))

        print('query = {}'.format(query))


        # Execute query.

        try:
            self.cur.execute(query)

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

    def add_refimage(self,ppid,field,fid,hp6,hp9,infobits,status,filename,checksum):

        '''
        Add record in RefImages database table.
        '''

        self.exit_code = 0


        # Define query template.

        query_template =\
            "select * from addRefImage(" +\
            "cast(TEMPLATE_FIELD as integer)," +\
            "cast(TEMPLATE_HP6 as integer)," +\
            "cast(TEMPLATE_HP9 as integer)," +\
            "cast(TEMPLATE_FID as smallint)," +\
            "cast(TEMPLATE_PPID as smallint)," +\
            "cast(TEMPLATE_INFOBITS as integer)," +\
            "cast('TEMPLATE_FILENAME' as character varying(255))," +\
            "cast('TEMPLATE_CHECKSUM' as character varying(32))," +\
            "cast(TEMPLATE_STATUS as smallint)) as " +\
            "(rfid integer," +\
            " version smallint);"


        # Query database.

        print('----> ppid = {}'.format(ppid))
        print('----> field = {}'.format(field))
        print('----> filename = {}'.format(filename))

        rep = {"TEMPLATE_PPID": str(ppid),
               "TEMPLATE_FIELD": str(field),
               "TEMPLATE_HP6": str(hp6),
               "TEMPLATE_HP9": str(hp9),
               "TEMPLATE_FID": str(fid)}

        rep["TEMPLATE_INFOBITS"] = str(infobits)
        rep["TEMPLATE_FILENAME"] = filename
        rep["TEMPLATE_CHECKSUM"] = checksum
        rep["TEMPLATE_STATUS"] = str(status)


        rep = dict((re.escape(k), v) for k, v in rep.items())
        pattern = re.compile("|".join(rep.keys()))
        query = pattern.sub(lambda m: rep[re.escape(m.group(0))], query_template)

        print('query = {}'.format(query))

        self.cur.execute(query)
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

        query_template =\
            "select * from updateRefImage(" +\
            "cast(TEMPLATE_RFID as integer)," +\
            "cast('TEMPLATE_FILENAME' as character varying(255))," +\
            "cast('TEMPLATE_CHECKSUM' as character varying(32))," +\
            "cast(TEMPLATE_STATUS as smallint)," +\
            "cast(TEMPLATE_VERSION AS smallint));"


        # Query database.

        print('----> rfid = {}'.format(rfid))
        print('----> filename = {}'.format(filename))
        print('----> checksum = {}'.format(checksum))
        print('----> status = {}'.format(status))
        print('----> version = {}'.format(version))

        rep = {"TEMPLATE_RFID": str(rfid),
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

        query_template =\
            "select psfid,filename " +\
            "from PSFs " +\
            "where vbest > 0 " +\
            "and status > 0 " +\
            "and sca = TEMPLATE_SCA " +\
            "and fid = TEMPLATE_FID; "


        # Formulate query by substituting parameters into query template.

        print('----> sca = {}'.format(sca))
        print('----> fid = {}'.format(fid))

        rep = {"TEMPLATE_SCA": str(sca)}

        rep["TEMPLATE_FID"] = str(fid)

        rep = dict((re.escape(k), v) for k, v in rep.items())
        pattern = re.compile("|".join(rep.keys()))
        query = pattern.sub(lambda m: rep[re.escape(m.group(0))], query_template)

        print('query = {}'.format(query))


        # Execute query.

        self.cur.execute(query)
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

        query_template =\
            "select ppid,rid,expid,sca,field,fid,started,ended,status,exitcode " +\
            "from Jobs " +\
            "where jid = TEMPLATE_JID; "


        # Formulate query by substituting parameters into query template.

        print('----> jid = {}'.format(jid))

        rep = {"TEMPLATE_JID": str(jid)}

        rep = dict((re.escape(k), v) for k, v in rep.items())
        pattern = re.compile("|".join(rep.keys()))
        query = pattern.sub(lambda m: rep[re.escape(m.group(0))], query_template)

        print('query = {}'.format(query))


        # Execute query.

        self.cur.execute(query)
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
        ra0,dec0,ra1,dec1,ra2,dec2,ra3,dec3,ra4,dec4,status,filename,checksum):

        '''
        Add record in DiffImages database table.
        '''

        self.exit_code = 0


        # Define query template.

        query_template =\
            "select * from addDiffImage(" +\
            "cast(TEMPLATE_RID as integer)," +\
            "cast(TEMPLATE_PPID as smallint)," +\
            "cast(TEMPLATE_RFID as integer)," +\
            "cast(TEMPLATE_INFOBITSSCI as integer)," +\
            "cast(TEMPLATE_INFOBITSREF as integer)," +\
            "cast(TEMPLATE_RA0 as double precision)," +\
            "cast(TEMPLATE_DEC0 as double precision)," +\
            "cast(TEMPLATE_RA1 as double precision)," +\
            "cast(TEMPLATE_DEC1 as double precision)," +\
            "cast(TEMPLATE_RA2 as double precision)," +\
            "cast(TEMPLATE_DEC2 as double precision)," +\
            "cast(TEMPLATE_RA3 as double precision)," +\
            "cast(TEMPLATE_DEC3 as double precision)," +\
            "cast(TEMPLATE_RA4 as double precision)," +\
            "cast(TEMPLATE_DEC4 as double precision)," +\
            "cast('TEMPLATE_FILENAME' as character varying(255))," +\
            "cast('TEMPLATE_CHECKSUM' as character varying(32))," +\
            "cast(TEMPLATE_STATUS as smallint)) as " +\
            "(pid integer," +\
            " version smallint);"


        # Query database.

        print('----> rid = {}'.format(rid))
        print('----> ppid = {}'.format(ppid))
        print('----> rfid = {}'.format(rfid))
        print('----> filename = {}'.format(filename))

        rep = {"TEMPLATE_RID": str(rid),
               "TEMPLATE_PPID": str(ppid),
               "TEMPLATE_RFID": str(rfid),
               "TEMPLATE_INFOBITSSCI": str(infobitssci),
               "TEMPLATE_INFOBITSREF": str(infobitsref)}

        rep["TEMPLATE_RA0"] = str(ra0)
        rep["TEMPLATE_DEC0"] = str(dec0)
        rep["TEMPLATE_RA1"] = str(ra1)
        rep["TEMPLATE_DEC1"] = str(dec1)
        rep["TEMPLATE_RA2"] = str(ra2)
        rep["TEMPLATE_DEC2"] = str(dec2)
        rep["TEMPLATE_RA3"] = str(ra3)
        rep["TEMPLATE_DEC3"] = str(dec3)
        rep["TEMPLATE_RA4"] = str(ra4)
        rep["TEMPLATE_DEC4"] = str(dec4)

        rep["TEMPLATE_FILENAME"] = filename
        rep["TEMPLATE_CHECKSUM"] = checksum
        rep["TEMPLATE_STATUS"] = str(status)


        rep = dict((re.escape(k), v) for k, v in rep.items())
        pattern = re.compile("|".join(rep.keys()))
        query = pattern.sub(lambda m: rep[re.escape(m.group(0))], query_template)

        print('query = {}'.format(query))

        self.cur.execute(query)
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

        query_template =\
            "select * from updateDiffImage(" +\
            "cast(TEMPLATE_PID as integer)," +\
            "cast('TEMPLATE_FILENAME' as character varying(255))," +\
            "cast('TEMPLATE_CHECKSUM' as character varying(32))," +\
            "cast(TEMPLATE_STATUS as smallint)," +\
            "cast(TEMPLATE_VERSION AS smallint));"


        # Query database.

        print('----> pid = {}'.format(pid))
        print('----> filename = {}'.format(filename))
        print('----> checksum = {}'.format(checksum))
        print('----> status = {}'.format(status))
        print('----> version = {}'.format(version))

        rep = {"TEMPLATE_PID": str(pid),
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

        query_template =\
            "select * from registerRefImImage(" +\
            "cast(TEMPLATE_RFID as integer)," +\
            "cast(TEMPLATE_RID AS integer));"


        # Query database.

        print('----> rfid = {}'.format(rfid))
        print('----> rid = {}'.format(rid))

        rep = {"TEMPLATE_RFID": str(rfid)}

        rep["TEMPLATE_RID"] = str(rid)


        rep = dict((re.escape(k), v) for k, v in rep.items())
        pattern = re.compile("|".join(rep.keys()))
        query = pattern.sub(lambda m: rep[re.escape(m.group(0))], query_template)

        print('query = {}'.format(query))


        # Execute query.

        try:
            self.cur.execute(query)

            try:
                for record in self.cur:
                    print(record)
            except:
                    print("Nothing returned from database stored function; continuing...")

        except (Exception, psycopg2.DatabaseError) as error:
            print('*** Error inserting or updating RefImImages record ({}); skipping...'.format(error))
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

        query_template =\
            "select * from registerRefImCatalog(" +\
            "cast(TEMPLATE_RFID as integer)," +\
            "cast(TEMPLATE_PPID as smallint)," +\
            "cast(TEMPLATE_CATTYPE as smallint)," +\
            "cast(TEMPLATE_FIELD as integer)," +\
            "cast(TEMPLATE_HP6 as integer)," +\
            "cast(TEMPLATE_HP9 as integer)," +\
            "cast(TEMPLATE_FID as smallint)," +\
            "cast('TEMPLATE_FILENAME' as character varying(255))," +\
            "cast('TEMPLATE_CHECKSUM' as character varying(32))," +\
            "cast(TEMPLATE_STATUS as smallint)) as " +\
            "(rfcatid integer," +\
            " svid smallint);"


        # Query database.

        print('----> rfid = {}'.format(rfid))
        print('----> ppid = {}'.format(ppid))
        print('----> cattype = {}'.format(cattype))
        print('----> field = {}'.format(field))
        print('----> filename = {}'.format(filename))

        rep = {"TEMPLATE_RFID": str(rfid),
               "TEMPLATE_PPID": str(ppid),
               "TEMPLATE_CATTYPE": str(cattype),
               "TEMPLATE_FIELD": str(field),
               "TEMPLATE_HP6": str(hp6),
               "TEMPLATE_HP9": str(hp9)}

        rep["TEMPLATE_FID"] = str(fid)
        rep["TEMPLATE_FILENAME"] = filename
        rep["TEMPLATE_CHECKSUM"] = checksum
        rep["TEMPLATE_STATUS"] = str(status)


        rep = dict((re.escape(k), v) for k, v in rep.items())
        pattern = re.compile("|".join(rep.keys()))
        query = pattern.sub(lambda m: rep[re.escape(m.group(0))], query_template)

        print('query = {}'.format(query))

        self.cur.execute(query)
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

        query_template =\
            "select * from registerDiffImMeta(" +\
            "cast(TEMPLATE_PID as integer)," +\
            "cast(TEMPLATE_FID as smallint)," +\
            "cast(TEMPLATE_SCA as smallint)," +\
            "cast(TEMPLATE_FIELD AS integer)," +\
            "cast(TEMPLATE_HP6 AS integer)," +\
            "cast(TEMPLATE_HP9 AS integer)," +\
            "cast(TEMPLATE_NSEXCATSOURCES AS integer)," +\
            "cast(TEMPLATE_REFSCALEFAC AS real)," +\
            "cast(TEMPLATE_DXRMSFIN AS real)," +\
            "cast(TEMPLATE_DYRMSFIN AS real)," +\
            "cast(TEMPLATE_DXMEDIANFIN AS real)," +\
            "cast(TEMPLATE_DYMEDIANFIN AS real));"


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

        rep = {"TEMPLATE_PID": str(pid)}

        rep["TEMPLATE_FID"] = str(fid)
        rep["TEMPLATE_SCA"] = str(sca)
        rep["TEMPLATE_FIELD"] = str(field)
        rep["TEMPLATE_HP6"] = str(hp6)
        rep["TEMPLATE_HP9"] = str(hp9)
        rep["TEMPLATE_NSEXCATSOURCES"] = str(nsexcatsources)
        rep["TEMPLATE_REFSCALEFAC"] = str(scalefacref)
        rep["TEMPLATE_DXRMSFIN"] = str(dxrmsfin)
        rep["TEMPLATE_DYRMSFIN"] = str(dyrmsfin)
        rep["TEMPLATE_DXMEDIANFIN"] = str(dxmedianfin)
        rep["TEMPLATE_DYMEDIANFIN"] = str(dymedianfin)

        rep = dict((re.escape(k), v) for k, v in rep.items())
        pattern = re.compile("|".join(rep.keys()))
        query = pattern.sub(lambda m: rep[re.escape(m.group(0))], query_template)

        print('query = {}'.format(query))


        # Execute query.

        try:
            self.cur.execute(query)

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

        query = "select rid,sca,fid,mjdobs from L2Files where expid = " + expid + ";"


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

        query_template =\
            "select * from registerRefImMeta(" +\
            "cast(TEMPLATE_RFID as integer)," +\
            "cast(TEMPLATE_FID as smallint)," +\
            "cast(TEMPLATE_FIELD AS integer)," +\
            "cast(TEMPLATE_HP6 AS integer)," +\
            "cast(TEMPLATE_HP9 AS integer)," +\
            "cast(TEMPLATE_NFRAMES AS smallint)," +\
            "cast(TEMPLATE_MJDOBSMIN AS double precision)," +\
            "cast(TEMPLATE_MJDOBSMAX AS double precision)," +\
            "cast(TEMPLATE_NPIXSAT AS integer)," +\
            "cast(TEMPLATE_NPIXNAN AS integer)," +\
            "cast(TEMPLATE_CLMEAN AS real)," +\
            "cast(TEMPLATE_CLSTDDEV AS real)," +\
            "cast(TEMPLATE_CLNOUTLIERS AS integer)," +\
            "cast(TEMPLATE_GMEDIAN AS real)," +\
            "cast(TEMPLATE_DATASCALE AS real)," +\
            "cast(TEMPLATE_GMIN AS real)," +\
            "cast(TEMPLATE_GMAX AS real)," +\
            "cast(TEMPLATE_COV5PERCENT AS real)," +\
            "cast(TEMPLATE_MEDNCOV AS real)," +\
            "cast(TEMPLATE_MEDPIXUNC AS real)," +\
            "cast(TEMPLATE_FWHMMEDPIX AS real)," +\
            "cast(TEMPLATE_FWHMMINPIX AS real)," +\
            "cast(TEMPLATE_FWHMMAXPIX AS real)," +\
            "cast(TEMPLATE_NSEXCATSOURCES AS integer));"


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

        rep = {"TEMPLATE_RFID": str(rfid)}

        rep["TEMPLATE_FID"] = str(fid)
        rep["TEMPLATE_FIELD"] = str(field)
        rep["TEMPLATE_HP6"] = str(hp6)
        rep["TEMPLATE_HP9"] = str(hp9)
        rep["TEMPLATE_NFRAMES"] = str(nframes)
        rep["TEMPLATE_MJDOBSMIN"] = str(mjdobsmin)
        rep["TEMPLATE_MJDOBSMAX"] = str(mjdobsmax)
        rep["TEMPLATE_NPIXSAT"] = str(npixsat)
        rep["TEMPLATE_NPIXNAN"] = str(npixnan)
        rep["TEMPLATE_CLMEAN"] = str(clmean)
        rep["TEMPLATE_CLSTDDEV"] = str(clstddev)
        rep["TEMPLATE_CLNOUTLIERS"] = str(clnoutliers)
        rep["TEMPLATE_GMEDIAN"] = str(gmedian)
        rep["TEMPLATE_DATASCALE"] = str(datascale)
        rep["TEMPLATE_GMIN"] = str(gmin)
        rep["TEMPLATE_GMAX"] = str(gmax)
        rep["TEMPLATE_COV5PERCENT"] = str(cov5percent)
        rep["TEMPLATE_MEDNCOV"] = str(medncov)
        rep["TEMPLATE_MEDPIXUNC"] = str(medpixunc)
        rep["TEMPLATE_FWHMMEDPIX"] = str(fwhmmedpix)
        rep["TEMPLATE_FWHMMINPIX"] = str(fwhmminpix)
        rep["TEMPLATE_FWHMMAXPIX"] = str(fwhmmaxpix)
        rep["TEMPLATE_NSEXCATSOURCES"] = str(nsexcatsources)

        rep = dict((re.escape(k), v) for k, v in rep.items())
        pattern = re.compile("|".join(rep.keys()))
        query = pattern.sub(lambda m: rep[re.escape(m.group(0))], query_template)

        print('query = {}'.format(query))


        # Execute query.

        try:
            self.cur.execute(query)

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

        query = "select rid,sca,fid,mjdobs from L2Files where dateobs >= '" +\
                startdatetime + "' and dateobs < '" + enddatetime + "' order by mjdobs;"


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

    def get_exposure_filter(self,fid):

        '''
        Get record from Filters database table for given fid.
        '''

        self.exit_code = 0


        # Define query template.

        query_template =\
            "select filter from Filters where fid=TEMPLATE_FID;"


        # Query database.

        print('----> fid = {}'.format(fid))

        fid_str = str(fid)

        rep = {"TEMPLATE_FID": fid_str}

        rep = dict((re.escape(k), v) for k, v in rep.items())
        pattern = re.compile("|".join(rep.keys()))
        query = pattern.sub(lambda m: rep[re.escape(m.group(0))], query_template)

        print('query = {}'.format(query))


        # Execute query.

        self.cur.execute(query)
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

        query_template =\
            "select pid,rfid,filename,infobitssci,version " +\
            "from DiffImages " +\
            "where vbest > 0 " +\
            "and status > 0 " +\
            "and rid = TEMPLATE_RID " +\
            "and ppid = TEMPLATE_PPID; "


        # Formulate query by substituting parameters into query template.

        print('----> rid = {}'.format(rid))
        print('----> ppid = {}'.format(ppid))

        rep = {"TEMPLATE_RID": str(rid)}

        rep["TEMPLATE_PPID"] = str(ppid)

        rep = dict((re.escape(k), v) for k, v in rep.items())
        pattern = re.compile("|".join(rep.keys()))
        query = pattern.sub(lambda m: rep[re.escape(m.group(0))], query_template)

        print('query = {}'.format(query))


        # Execute query.

        self.cur.execute(query)
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

        query_template =\
            "select rfid,filename,infobits,version " +\
            "from RefImages " +\
            "where rfid = TEMPLATE_RFID; "


        # Formulate query by substituting parameters into query template.

        print('----> rfid = {}'.format(rfid))

        rep = {"TEMPLATE_RFID": str(rfid)}

        rep = dict((re.escape(k), v) for k, v in rep.items())
        pattern = re.compile("|".join(rep.keys()))
        query = pattern.sub(lambda m: rep[re.escape(m.group(0))], query_template)

        print('query = {}'.format(query))


        # Execute query.

        self.cur.execute(query)
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

        query = "select jid from Jobs " +\
                "where ppid = 15 " +\
                "and ended >= cast('" + proc_date + "' as timestamp) " +\
                "and ended < cast('" + proc_date + "' as timestamp) + cast('1 day' as interval) " +\
                "and status > 0 " +\
                "and exitcode <= 32;"


        # Query database.

        print('query = {}'.format(query))


        # Execute query.

        try:
            self.cur.execute(query)

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

    def get_unclosedout_jobs_for_processing_date(self,ppid,proc_date):

        '''
        Query database for Jobs records that were launched on the given processing date,
        but not yet closed out by finalizing started, ended, elapsed, exitcode and status.
        .
        '''

        self.exit_code = 0


        # Define query.

        query = "select jid,awsbatchjobid from Jobs " +\
                "where ppid = " + str(ppid) + " " +\
                "and launched >= cast('" + proc_date + "' as timestamp) " +\
                "and launched < cast('" + proc_date + "' as timestamp) + cast('1 day' as interval) " +\
                "and status = 0 " +\
                "and ended is null " +\
                "and exitcode is null;"


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
                "and b.status > 0 " +\
                "and b.vbest > 0 " +\
                "and cov5percent >= " + str(cov5percent) + " " +\
                "and nframes >= " + str(nframes) + " " +\
                "and a.dateobs >= '" + startdatetime + "' " +\
                "and a.dateobs < '" + enddatetime + "' " +\
                "order by a.mjdobs,a.sca;"


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
            print('*** Error getting all L2Files records for given dateobs range, nframes, and cov5percent ({}); skipping...'.format(error))
            self.exit_code = 67
            return

        return records


########################################################################################################

    def get_field_fid_nframes_records_for_mjdobs_range(self,start_refimage_mjdobs,end_refimage_mjdobs,min_refimage_nframes):

        '''
        Query database for all field/filter/nframes combinations in reference-image window with
        minimum number of frames in coadd stack.
        '''

        self.exit_code = 0


        # Define query.

        query =\
            f"select field,fid,count(*) from l2files where mjdobs >= {start_refimage_mjdobs} " +\
            f"and mjdobs < {end_refimage_mjdobs} group by field,fid having " +\
            f"count(*) >= {min_refimage_nframes} order by field,fid;"

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
            f"select rid,sca,field,fid,mjdobs from L2Files " +\
            f"where dateobs >= '{startdatetime}' " +\
            f"and dateobs < '{enddatetime}' " +\
            f"and field = {field} " +\
            f"and fid = {fid} " +\
            f"order by mjdobs,sca;"


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

    def get_l2file_wcs(self,rid):

        '''
        Query select WCS columns in L2Files database table for given RID.
        '''

        self.exit_code = 0


        # Define query template.

        query_template =\
            "select crval1,crval2,crpix1,crpix2,cd11,cd12,cd21,cd22 " +\
            "from L2Files " +\
            "where rid = TEMPLATE_RID; "


        # Formulate query by substituting parameters into query template.

        print('----> rid = {}'.format(rid))

        rep = {"TEMPLATE_RID": str(rid)}

        rep = dict((re.escape(k), v) for k, v in rep.items())
        pattern = re.compile("|".join(rep.keys()))
        query = pattern.sub(lambda m: rep[re.escape(m.group(0))], query_template)

        print('query = {}'.format(query))


        # Execute query.

        self.cur.execute(query)
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

        else:
            print("*** Error: Could not get select WCS columns from L2Files database record; returning...")
            self.exit_code = 67


        return record_dict
