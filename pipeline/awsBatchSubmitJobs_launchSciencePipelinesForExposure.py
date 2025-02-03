import os

import database.modules.utils.rapid_db as db
import modules.utils.rapid_pipeline_subs as util

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

    if dbh.exit_code >= 64:
        print("*** Error from {}; quitting ".format(swname))
        exit(dbh.exit_code)


    # Launch pipeline instances via AWS Batch.

    for rec in recs:
        rid = rec[0]
        sca = rec[1]
        print("rid, sca =",rid,sca)


        # Load RID into the environment.
        # Database query record has numbers converted to values.

        os.environ['RID'] = str(rid)


        # Launch single pipeline instance.


        python_cmd = 'python3'
        launch_single_pipeline_instance_code = '/code/pipeline/awsBatchSubmitJobs_launchSingleSciencePipeline.py'

        launch_cmd = [python_cmd,
                      launch_single_pipeline_instance_code]

        exitcode_from_launch_cmd = util.execute_command(launch_cmd)


    # Close database connection.

    dbh.close()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Termination.

    terminating_exitcode = 0

    print("terminating_exitcode =",terminating_exitcode)

    exit(terminating_exitcode)
