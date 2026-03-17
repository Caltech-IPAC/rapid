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

    def __init__(self,debug=0):

        self.exit_code = 0
        self.conn = None
        self.debug = debug


        # Get database connection parameters from environment.

        sqlite_dbname = "roman_tessellation_nside512.db"

        dbname = os.getenv('ROMANTESSELLATIONDBNAME')

        if dbname is None:

            dbname = "/Users/laher/Documents/rapid/" + sqlite_dbname

        print("dbname =",dbname)


        # Connect to database

        try:
            self.conn = sqlite3.connect(database=dbname)
        except:
            print("*** Error: Could not connect to database in sub roman_tessellation_db.__init__...")
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
                print('Database connection closed in sub roman_tessellation_db.close.')


    def get_rtid(self,ra,dec):

        '''
        Query SQLite database for rtid associated with given (RA, Dec).
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

        if self.debug > 0:
            print('query = {}'.format(query))


        # Execute query.

        self.rtid = None

        try:
            self.cur.execute(query)

            record = self.cur.fetchone()

            if record is not None:
                self.rtid = record[0]
            else:


                # Near the 360-0 boundary crossing, special handling is needed.

                bc_ra = str(ra - 360.0)

                query = f"select rtid from vskytiles " +\
                        f"where decmin <= {dec} and decmax > {dec} " +\
                        f"and ramin <= {bc_ra} and ramax > {bc_ra};"

                if self.debug > 0:
                    print('query = {}'.format(query))


                # Execute query.

                self.rtid = None

                try:
                    self.cur.execute(query)

                    record = self.cur.fetchone()

                    if record is not None:
                        self.rtid = record[0]
                    else:
                        print("*** Error: Unexpected query return value in sub roman_tessellation_db.get_rtid; returning None...")
                        self.exit_code = 69
                        return

                except (Exception, sqlite3.DatabaseError) as error:
                    print("*** Error executing sub roman_tessellation_db.get_rtid; returning None...")
                    self.exit_code = 67

        except (Exception, sqlite3.DatabaseError) as error:
            print("*** Error executing sub roman_tessellation_db.get_rtid; returning None...")
            self.exit_code = 67
            return


    def get_center_sky_position(self,rtid):

        '''
        Query SQLite database for center sky position (RA, Dec) associated with given rtid.
        '''


        # Define query template.

        query_template = "select cra,cdec from skytiles " +\
                         "where rtid = QUERY_RTID;"


        # Substitute parameters into query template.

        rep = {}
        rep["QUERY_RTID"] = str(rtid)


        rep = dict((re.escape(k), v) for k, v in rep.items())
        pattern = re.compile("|".join(rep.keys()))
        query = pattern.sub(lambda m: rep[re.escape(m.group(0))], query_template)

        if self.debug > 0:
            print('query = {}'.format(query))


        # Execute query.

        self.ra0 = None
        self.dec0 = None

        try:
            self.cur.execute(query)

            record = self.cur.fetchone()

            if record is not None:
                self.ra0 = record[0]
                self.dec0 = record[1]
            else:
                print("*** Error: Unexpected query return value in sub roman_tessellation_db.get_center_sky_position; returning None...")
                self.exit_code = 69
                return

        except (Exception, sqlite3.DatabaseError) as error:
            print("*** Error executing sub roman_tessellation_db.get_center_sky_position; returning None...")
            self.exit_code = 67
            return


    def get_corner_sky_positions(self,rtid):

        '''
        Query SQLite database for ramin, ramax, decmin, decmmax sky positions (RA, Dec) associated with given rtid.
        Then formulate the sky positions of the four corners.
        '''


        # Define query template.
        # Query the vskytiles table instead of the skytiles table because apparently it has more precision.

        query_template = "select ramin,ramax,decmin,decmax from vskytiles " +\
                         "where rtid = QUERY_RTID;"


        # Substitute parameters into query template.

        rep = {}
        rep["QUERY_RTID"] = str(rtid)


        rep = dict((re.escape(k), v) for k, v in rep.items())
        pattern = re.compile("|".join(rep.keys()))
        query = pattern.sub(lambda m: rep[re.escape(m.group(0))], query_template)

        if self.debug > 0:
            print('query = {}'.format(query))


        # Execute query.

        self.ramin = None
        self.ramax = None
        self.decmin = None
        self.decmax = None

        self.ra1 = None
        self.dec1 = None
        self.ra2 = None
        self.dec2 = None
        self.ra3 = None
        self.dec3 = None
        self.ra4 = None
        self.dec4 = None

        try:
            self.cur.execute(query)

            record = self.cur.fetchone()

            if record is not None:
                self.ramin = record[0]
                self.ramax = record[1]
                self.decmin = record[2]
                self.decmax = record[3]
            else:
                print("*** Error: Unexpected query return value in sub roman_tessellation_db.get_corner_sky_positions; returning None...")
                self.exit_code = 69
                return

        except (Exception, sqlite3.DatabaseError) as error:
            print("*** Error executing sub roman_tessellation_db.get_corner_sky_positions; returning None...")
            self.exit_code = 67
            return

        self.ra1 = self.ramin
        self.dec1 = self.decmin
        self.ra2 = self.ramax
        self.dec2 = self.decmin
        self.ra3 = self.ramax
        self.dec3 = self.decmax
        self.ra4 = self.ramin
        self.dec4 = self.decmax


    def get_sky_tiles_in_dec_bin(self,decmin,decmax):

        '''
        Query SQLite database for sky tiles in the same declination bin given by decmin and decmax,
        which are returned by the class method get_corner_sky_positions.
        Return records ordered by ramin.
        '''


        # Define query template.

        query_template = "select rtid,ramin,ramax,decmin,decmax from vskytiles " +\
                         "where decmin >= QUERY_DECMIN_LOWER and decmax >= QUERY_DECMAX_LOWER " +\
                         "and decmin <= QUERY_DECMIN_UPPER and decmax <= QUERY_DECMAX_UPPER " +\
                         "order by ramin;"


        # Substitute parameters into query template.

        tol = 1.0e-4
        decmin_lower = decmin - tol
        decmin_upper = decmin + tol
        decmax_lower = decmax - tol
        decmax_upper = decmax + tol

        rep = {}
        rep["QUERY_DECMIN_LOWER"] = str(decmin_lower)
        rep["QUERY_DECMIN_UPPER"] = str(decmin_upper)
        rep["QUERY_DECMAX_LOWER"] = str(decmax_lower)
        rep["QUERY_DECMAX_UPPER"] = str(decmax_upper)

        rep = dict((re.escape(k), v) for k, v in rep.items())
        pattern = re.compile("|".join(rep.keys()))
        query = pattern.sub(lambda m: rep[re.escape(m.group(0))], query_template)

        if self.debug > 0:
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

                if self.debug > 0:
                    print("nrecs =",nrecs)

            except:
                print("*** Error: Unexpected query return value in sub roman_tessellation_db.get_sky_tiles_in_dec_bin; returning None...")
                self.exit_code = 69
                return

        except (Exception, sqlite3.DatabaseError) as error:
            print("*** Error executing sub roman_tessellation_db.get_sky_tiles_in_dec_bin; returning None...")
            self.exit_code = 67
            return

        return records


    def get_all_neighboring_rtids(self,rtid):

        '''
        Query SQLite database for all sky tiles surrounding a given sky tile.
        Field number (rtid) starts at 1 for the north pole and ends at 6291458
        for the south pole.  There is only one round sky tile at each pole.
        Suffixes _above and _below are used in relation to the declination bin
        of interest, where north is defined as up.
        '''

        rtids_list = []

        if rtid == 1:

            an_rtid_below = 2

            self.get_corner_sky_positions(an_rtid_below)

            ramin_below = self.ramin
            ramax_below = self.ramax
            decmin_below = self.decmin
            decmax_below = self.decmax

            records = self.get_sky_tiles_in_dec_bin(decmin_below,decmax_below)

            nrecords = len(records)

            for i in range(nrecords):
                rtid_below = records[i - 1][0]
                rtids_list.append(rtid_below)

        elif rtid == 6291458:

            an_rtid_above = 6291457

            self.get_corner_sky_positions(an_rtid_above)

            ramin_above = self.ramin
            ramax_above = self.ramax
            decmin_above = self.decmin
            decmax_above = self.decmax

            records = self.get_sky_tiles_in_dec_bin(decmin_above,decmax_above)

            nrecords = len(records)

            for i in range(nrecords):
                rtid_above = records[i - 1][0]
                rtids_list.append(rtid_above)

        else:

            self.get_corner_sky_positions(rtid)

            ramin = self.ramin
            ramax = self.ramax
            decmin = self.decmin
            decmax = self.decmax

            records = self.get_sky_tiles_in_dec_bin(decmin,decmax)

            nrecords = len(records)

            if self.debug > 0:
                print("nrecords =",nrecords)
                print("records =",records)
                print("rtid =",rtid)

            for i in range(nrecords):
                if self.debug > 0:
                    print("records[i][0] =",records[i][0])
                if rtid == records[i][0]:
                    if self.debug > 0:
                        print(f"Found record at i = {i}")
                    if i == 0:
                        rtid_left = records[nrecords - 1][0]
                        rtid_right = records[i + 1][0]
                    elif i == nrecords - 1:
                        rtid_left = records[i - 1][0]
                        rtid_right = records[0][0]
                    else:
                        rtid_left = records[i - 1][0]
                        rtid_right = records[i + 1][0]

                    break

            rtids_list.append(rtid_left)
            rtids_list.append(rtid_right)


            # Row of sky tiles above that of the sky tile of interest.

            an_rtid_above = records[0][0] - 1

            if an_rtid_above == 1:
                rtids_list.append(an_rtid_above)
            else:

                if self.debug > 0:
                    print("Above: ramin,ramax =",ramin,ramax)

                self.get_corner_sky_positions(an_rtid_above)

                ramin_above = self.ramin
                ramax_above = self.ramax
                decmin_above = self.decmin
                decmax_above = self.decmax

                records_above = self.get_sky_tiles_in_dec_bin(decmin_above,decmax_above)

                nrecords_above = len(records_above)

                for i in range(nrecords_above):
                    rtid_above = records_above[i][0]
                    ramin_above = records_above[i][1]
                    ramax_above = records_above[i][2]

                    if self.debug > 0:
                           print("Above: i,rtid_above,ramin_above,ramax_above =",i,rtid_above,ramin_above,ramax_above)

                    if ramin_above >= ramin and ramin_above <= ramax:
                        pass
                    elif ramax_above >= ramin and ramax_above <= ramax:
                        pass
                    elif ramin_above <= ramin and ramax_above >= ramax:
                        pass
                    elif ramin < 0.0:

                        ra1 = ramin_above - 360.0
                        ra2 = ramax_above - 360.0

                        if  ra1 >= ramin and ra1 <= ramax:
                            pass
                        elif ra2 >= ramin and ra2 <= ramax:
                            pass
                        elif ra1 <= ramin and ra2 >= ramax:
                            pass
                        else:
                            continue
                    else:
                        continue

                    rtids_list.append(rtid_above)


            # Row of sky tiles below that of the sky tile of interest.

            an_rtid_below = records[nrecords - 1][0] + 1

            if an_rtid_below == 6291458:
                rtids_list.append(an_rtid_below)
            else:

                if self.debug > 0:
                    print("Below: ramin,ramax =",ramin,ramax)

                self.get_corner_sky_positions(an_rtid_below)

                ramin_below = self.ramin
                ramax_below = self.ramax
                decmin_below = self.decmin
                decmax_below = self.decmax

                records_below = self.get_sky_tiles_in_dec_bin(decmin_below,decmax_below)

                nrecords_below = len(records_below)

                for i in range(nrecords_below):
                    rtid_below = records_below[i][0]
                    ramin_below = records_below[i][1]
                    ramax_below = records_below[i][2]

                    if self.debug > 0:
                        print("Below: i,rtid_below,ramin_below,ramax_below =",i,rtid_below,ramin_below,ramax_below)

                    if ramin_below >= ramin and ramin_below <= ramax:
                        pass
                    elif ramax_below >= ramin and ramax_below <= ramax:
                        pass
                    elif ramin_below <= ramin and ramax_below >= ramax:
                        pass
                    elif ramin < 0.0:

                        ra1 = ramin_below - 360.0
                        ra2 = ramax_below - 360.0

                        if  ra1 >= ramin and ra1 <= ramax:
                            pass
                        elif ra2 >= ramin and ra2 <= ramax:
                            pass
                        elif ra1 <= ramin and ra2 >= ramax:
                            pass
                        else:
                            continue
                    else:
                        continue

                    rtids_list.append(rtid_below)


        return rtids_list


    def get_overlapping_rtids(self, ra0, dec0, ra1, dec1, ra2, dec2, ra3, dec3, ra4, dec4):

        '''
        Query SQLite database for all sky tiles that overlap the sky area defined
        by the given center (ra0, dec0) and four corner positions (ra1,dec1) through
        (ra4,dec4).  Assumes the image is square with N up (aligned to the RA/Dec grid),
        so the bounding box of the five input points exactly defines the image footprint.
        Returns a list of (rtid, ramin, ramax, decmin, decmax) records.

        The south pole tile (rtid=6291458) is stored with ramin=ramax=0 in vskytiles
        and is therefore invisible to the bounding-box RA overlap check.  It is handled
        as a special case and appended explicitly when the query region reaches its
        declination range.
        '''

        self.exit_code = 0


        # Compute bounding box from center and all four corners.

        ras  = [ra0,  ra1,  ra2,  ra3,  ra4]
        decs = [dec0, dec1, dec2, dec3, dec4]

        decmin_query = min(decs)
        decmax_query = max(decs)


        # Detect RA wrap-around (image straddles the RA=0/360 boundary).
        # If the spread of RA values exceeds 180 degrees, normalize RAs > 180
        # to their negative equivalents before computing ramin/ramax.

        ra_spread = max(ras) - min(ras)

        if ra_spread > 180.0:
            ras_norm = [ra - 360.0 if ra > 180.0 else ra for ra in ras]
            ramin_query = min(ras_norm)
            ramax_query = max(ras_norm)
            wrap = True
        else:
            ramin_query = min(ras)
            ramax_query = max(ras)
            wrap = False


        # Define query template.
        # Two rectangles overlap when: A.ramin < B.ramax AND A.ramax > B.ramin
        #                          AND A.decmin < B.decmax AND A.decmax > B.decmin.
        # For the wrap-around case the RA condition becomes an OR: the image covers
        # [ramin_query+360, 360] union [0, ramax_query], so a tile overlaps if
        # its ramin < ramax_query OR its ramax > ramin_query+360.

        if wrap:
            query_template = (
                "select rtid,ramin,ramax,decmin,decmax from vskytiles "
                "where decmax > QUERY_DECMIN and decmin < QUERY_DECMAX "
                "and (ramin < QUERY_RAMAX or ramax > QUERY_RAMIN_PLUS360);"
            )
            rep = {
                "QUERY_DECMIN":        str(decmin_query),
                "QUERY_DECMAX":        str(decmax_query),
                "QUERY_RAMAX":         str(ramax_query),
                "QUERY_RAMIN_PLUS360": str(ramin_query + 360.0),
            }
        else:
            query_template = (
                "select rtid,ramin,ramax,decmin,decmax from vskytiles "
                "where decmax > QUERY_DECMIN and decmin < QUERY_DECMAX "
                "and ramax > QUERY_RAMIN and ramin < QUERY_RAMAX;"
            )
            rep = {
                "QUERY_DECMIN": str(decmin_query),
                "QUERY_DECMAX": str(decmax_query),
                "QUERY_RAMIN":  str(ramin_query),
                "QUERY_RAMAX":  str(ramax_query),
            }

        rep = dict((re.escape(k), v) for k, v in rep.items())
        pattern = re.compile("|".join(rep.keys()))
        query = pattern.sub(lambda m: rep[re.escape(m.group(0))], query_template)

        if self.debug > 0:
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

                if self.debug > 0:
                    print("nrecs =", nrecs)

            except:
                print("*** Error: Unexpected query return value in sub roman_tessellation_db.get_overlapping_sky_tiles; returning None...")
                self.exit_code = 69
                return

        except (Exception, sqlite3.DatabaseError) as error:
            print("*** Error executing sub roman_tessellation_db.get_overlapping_sky_tiles; returning None...")
            self.exit_code = 67
            return


        # Special case: the south pole tile (rtid=6291458) is stored with ramin=ramax=0
        # in vskytiles, making it invisible to the bounding-box RA overlap check.
        # Explicitly add it if the query dec range overlaps its declination band.

        south_pole_rtid = 6291458
        south_pole_tile_decmax = -89.9543075561523

        if decmin_query < south_pole_tile_decmax:
            rtids_returned = [r[0] for r in records]
            if south_pole_rtid not in rtids_returned:
                try:
                    self.cur.execute(
                        "select rtid,ramin,ramax,decmin,decmax from vskytiles where rtid = {};".format(south_pole_rtid)
                    )
                    pole_record = self.cur.fetchone()
                    if pole_record is not None:
                        records.append(pole_record)
                except (Exception, sqlite3.DatabaseError) as error:
                    print("*** Error fetching south pole tile in sub roman_tessellation_db.get_overlapping_sky_tiles; skipping...")

        return records
