import healpy as hp
import database.modules.utils.rapid_db as db
import modules.utils.rapid_pipeline_subs as util

def add_hp9_indexes_to_l2filemeta():


    # Open database connection.

    dbh = db.RAPIDDB()


    # For all records, get rid, ra0, dec0.

    recs = dbh.get_all_l2filemeta()


    # Loop over all records and compute hp9 index for each.

    level = 9
    nside = 2**level

    for rec in recs:

        rid = rec[0]
        ra = rec[1]
        dec = rec[2]


        # Compute level-9 healpix index (NESTED pixel ordering).

        hp9 = hp.ang2pix(nside,ra,dec,nest=True,lonlat=True)


        # Update record with commit.

        dbh.update_l2filemeta_hp9(rid,hp9)


    # Close database connection.

    dbh.close()


def add_field_hp6_hp9_indexes_to_l2files(roman_tessellation_dict):


    # Open database connection.

    dbh = db.RAPIDDB()


    # For all records, get rid, ra0, dec0.

    recs = dbh.get_all_l2files()


    # Loop over all records and compute hp9 index for each.

    level6 = 6
    nside6 = 2**level6

    level9 = 9
    nside9 = 2**level9

    for rec in recs:

        rid = rec[0]
        ra = rec[1]
        dec = rec[2]


        # Compute level-6 healpix index (NESTED pixel ordering).

        hp6 = hp.ang2pix(nside6,ra,dec,nest=True,lonlat=True)


        # Compute level-9 healpix index (NESTED pixel ordering).

        hp9 = hp.ang2pix(nside9,ra,dec,nest=True,lonlat=True)


        # Compute field.

        field = util.get_roman_tessellation_index(roman_tessellation_dict,ra,dec)


        # Update record with commit.

        dbh.update_l2files_field_hp6_hp9(rid,field,hp6,hp9)


    # Close database connection.

    dbh.close()


def add_field_hp6_hp9_indexes_to_exposures(roman_tessellation_dict):


    # Open database connection.

    dbh = db.RAPIDDB()


    # For all records, get rid, ra0, dec0.

    recs = dbh.get_all_exposures()


    # Loop over all records and compute hp9 index for each.

    level6 = 6
    nside6 = 2**level6

    level9 = 9
    nside9 = 2**level9

    for rec in recs:

        rid = rec[0]
        ra = rec[1]
        dec = rec[2]


        # Compute level-6 healpix index (NESTED pixel ordering).

        hp6 = hp.ang2pix(nside6,ra,dec,nest=True,lonlat=True)


        # Compute level-9 healpix index (NESTED pixel ordering).

        hp9 = hp.ang2pix(nside9,ra,dec,nest=True,lonlat=True)


        # Compute field.

        field = util.get_roman_tessellation_index(roman_tessellation_dict,ra,dec)


        # Update record with commit.

        dbh.update_exposures_field_hp6_hp9(rid,field,hp6,hp9)


    # Close database connection.

    dbh.close()


# Main program.

if __name__ == '__main__':
    rtd = util.read_roman_tessellation_nside10()
    add_hp9_indexes_to_l2filemeta()
    add_field_hp6_hp9_indexes_to_l2files(rtd)
    add_field_hp6_hp9_indexes_to_exposures(rtd)

    exit(0)
