import healpy as hp
import database.modules.utils.rapid_db as db



def add_hp6_indexes():


    # Open database connection.

    dbh = db.RAPIDDB()


    # For all records, get rid, ra0, dec0.

    recs = dbh.get_all_l2filemeta()


    # Loop over all records and compute hp6 index for each.

    level = 6
    nside = 2**6

    for rec in recs:

        rid = rec[0]
        ra = rec[1]
        dec = rec[2]


        # Compute level-6 healpix index (NESTED pixel ordering).

        hp6 = hp.ang2pix(nside,ra,dec,nest=True,lonlat=True)


        # Update record.

        dbh.update_l2filemeta_hp6(rid,hp6)


    # Close database connection.

    dbh.close()



# Main program.

if __name__ == '__main__':
    add_hp6_indexes()
