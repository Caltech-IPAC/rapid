import os
import database.modules.utils.rapid_db as db

swname = "awsBatchSubmitJobs_launchSciencePipelinesForExposure.py"
swvers = "1.0"

print("swname =", swname)
print("swvers =", swvers)


# EXPID of input file, read from environment variable EXPID.

expid = os.getenv('EXPID')

if expid is None:

    print("*** Error: Env. var. EXPID not set; quitting...")
    exit(64)


if __name__ == '__main__':


    #
    # Launch a science pipelines for input exposure ID.
    #


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Query database for all L2Files records associated with the given exposure ID.

    recs = dbh.get_l2files_records_for_expid(expid)

    for rec in recs:
        rid = rec[0]
        sca = rec[1]
        print("rid, sca =",rid,sca)

    if dbh.exit_code >= 64:
        print("*** Error from {}; quitting ".format(swname))
        exit(dbh.exit_code)


    # Close database connection.

    dbh.close()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)

