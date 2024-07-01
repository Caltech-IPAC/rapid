import healpy as hp
import database.modules.utils.rapid_db as db



def backfill_fid_and_chipid_in_l2filemeta():


    # Open database connection.

    dbh = db.RAPIDDB()


    # For all records, get rid, fid, chipid.

    recs = dbh.get_all_l2files_assoc_rid_with_fid_and_chipid()


    # Loop over all records and update L2FileMeta table.

    for rec in recs:

        rid = rec[0]
        fid = rec[1]
        chipid = rec[2]


        # Update record.

        dbh.update_l2filemeta_fid_chipid(rid,fid,chipid)


    # Close database connection.

    dbh.close()



# Main program.

if __name__ == '__main__':
    backfill_fid_and_chipid_in_l2filemeta()
