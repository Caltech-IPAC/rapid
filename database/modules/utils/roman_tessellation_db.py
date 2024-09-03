import os
import re
import sqlite3

class RomanTessellationNSIDE512:

    """
    Class to facilitate execution of queries in the SQLite database
    that stores Roman sky tessallation data for NSIDE=512.
    For each query a different method is defined.

    Returns exitcode:
         0 = Normal
         2 = Exception raised closing database connection
        64 = Cannot connect to database
        65 = Input file does not exist
        66 = File checksum does not match database checksum
        67 = Could not execute database query.
        68 = Could not open file to compute checksum.
        69 = Query returned unexpected results (e.g., None)
    """

    def __init__(self):

        self.exit_code = 0
        self.conn = None


        # Get database connection parameters from environment.

        sqlite_dbname = "roman_tessellation_nside512.db"

        dbname = os.getenv('DBNAME')

        if dbname is None:

            dbname = "/Users/laher/Documents/rapid/" + sqlite_dbname

        print("dbname =",dbname)


        # Connect to database

        try:
            self.conn = sqlite3.connect(database=dbname)
        except:
            print("Could not connect to database...")
            self.exit_code = 64
            return


        # Open database cursor.

        self.cur = self.conn.cursor()


        # Select database version.

        q1 = 'select count(*) from decbins;'
        print('q1 = {}'.format(q1))
        self.cur.execute(q1)
        n_decbins = self.cur.fetchone()
        print('Number of records in decbins table = {}'.format(n_decbins))


    def close(self):

        '''
        Close database cursor and then connection.
        '''

        try:
            self.cur.close()
        except (Exception, sqlite3.DatabaseError) as error:
            print(error)
            self.exit_code = 2
        finally:
            if self.conn is not None:
                self.conn.close()
                print('Database connection closed.')


    def get_rtid(self,ra,dec):

        '''
        Get rtid for given (RA, Dec).
        '''


        # Define query template.

        query_template = "select rtid from vskytiles " +\
                         "where decmin <= QUERY_DEC and decmax > QUERY_DEC " +\
                         "and ramin <= QUERY_RA and ramax > QUERY_RA;"


        # Substitute parameters into query template.

        rep = {}
        rep["QUERY_RA"] = str(ra)
        rep["QUERY_DEC"] = str(dec)


        rep = dict((re.escape(k), v) for k, v in rep.items())
        pattern = re.compile("|".join(rep.keys()))
        query = pattern.sub(lambda m: rep[re.escape(m.group(0))], query_template)

        print('query = {}'.format(query))


        # Execute query.

        self.rtid = None

        try:
            self.cur.execute(query)

            record = self.cur.fetchone()

            if record is not None:
                self.rtid = record[0]
            else:
                print("*** Error unexpected query return value; returning None...")
                self.exit_code = 69
                return

        except (Exception, sqlite3.DatabaseError) as error:
            print("*** Error executing get_rtid; returning None...")
            self.exit_code = 67
            return
