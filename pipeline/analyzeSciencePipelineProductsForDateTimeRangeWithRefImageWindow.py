import os
import ast
from datetime import datetime, timezone
from dateutil import tz
import time

to_zone = tz.gettz('America/Los_Angeles')

import database.modules.utils.rapid_db as db
import modules.utils.rapid_pipeline_subs as util

swname = "analyzeSciencePipelineProductsForDateTimeRangeWithRefImageWindow.py"
swvers = "1.0"

print("swname =", swname)
print("swvers =", swvers)

debug = True


# Compute start time for benchmark.

start_time_benchmark = time.time()


# Compute processing datetime (UT) and processing datetime (Pacific time).

datetime_utc_now = datetime.utcnow()
proc_utc_datetime = datetime_utc_now.strftime('%Y-%m-%dT%H:%M:%SZ')
datetime_pt_now = datetime_utc_now.replace(tzinfo=timezone.utc).astimezone(tz=to_zone)
proc_pt_datetime_started = datetime_pt_now.strftime('%Y-%m-%dT%H:%M:%S PT')

print("proc_utc_datetime =",proc_utc_datetime)
print("proc_pt_datetime_started =",proc_pt_datetime_started)


# JOBPROCDATE of RAPID science-pipeline jobs that already ran.
# The S3-bucket products are analyzed for that processing date.

proc_date = os.getenv('JOBPROCDATE')

if proc_date is None:

    print("*** Error: Env. var. JOBPROCDATE not set; quitting...")
    exit(64)


# Inputs are observation start and end datetimes of exposures to be processed.
# E.g., startdatetime = "2028-09-08 00:18:00", enddatetime = "2028-09-11 00:00:00"
#
# If startdatetime is set to "dynamic", a database query will be used to determine
# the startdatetime for each field and filter, assuming MINREFIMNFRAMES early in the
# observation data set are reserved/skipped for reference-image generation.  In this
# case, STARTREFIMMJDOBS and ENDREFIMMJDOBS should be set to cover the entire range
# of observations.

startdatetime = os.getenv('STARTDATETIME')

if startdatetime is None:

    print("*** Error: Env. var. STARTDATETIME not set; quitting...")
    exit(64)

enddatetime = os.getenv('ENDDATETIME')

if enddatetime is None:

    print("*** Error: Env. var. ENDDATETIME not set; quitting...")
    exit(64)


# Additional inputs are observation start and end MJD of window for generating
# reference images, and minimum number of frames in coadd stack.
# When the flag to make reference images is set to True, then only one
# representative L2 science image for the field and filter is processed
# to initially make the needed reference image for the other L2 science images
# with the same field and filter;  when it is set to False then
# all other L2 science images, except for the representative L2 science images,
# are processed.  The representative L2 science image is the first in
# a time-ordered, SCA-ordered list for a given field and filter.

start_refimage_mjdobs = os.getenv('STARTREFIMMJDOBS')

if start_refimage_mjdobs is None:

    print("*** Error: Env. var. STARTREFIMMJDOBS not set; quitting...")
    exit(64)

end_refimage_mjdobs = os.getenv('ENDREFIMMJDOBS')

if end_refimage_mjdobs is None:

    print("*** Error: Env. var. ENDREFIMMJDOBS not set; quitting...")
    exit(64)


# Assume min_refimage_nframes is either an integer or
# an list of 8 integers for fids 1 through 8, inclusive.

min_refimage_nframes = os.getenv('MINREFIMNFRAMES')

if min_refimage_nframes is None:

    print("*** Error: Env. var. MINREFIMNFRAMES not set; quitting...")
    exit(64)

first_char = min_refimage_nframes[0]
last_char = min_refimage_nframes[-1]

if first_char == "[" and last_char == "]":

    min_refimage_nframes = ast.literal_eval(min_refimage_nframes)

    if len(min_refimage_nframes) != 8:

        print("*** Error: Env. var. MINREFIMNFRAMES list does not have 8 elements; quitting...")
        exit(64)


# Set flag to determine whether pipeline instances may generate reference images.

make_refimages_flag_str = os.getenv('MAKEREFIMAGESFLAG')

if make_refimages_flag_str is None:

    print("*** Error: Env. var. MAKEREFIMAGESFLAG not set; quitting...")
    exit(64)

make_refimages_flag = eval(make_refimages_flag_str)


# If RUNFID is set, then process just the specified filter.

run_fid = os.getenv('RUNFID')

if run_fid is None:
    print("*** Message: Will process all filters...")
else:
    print(f"*** Message: Will process only fid={run_fid}...")


# Print parameters.

print("startdatetime =",startdatetime)
print("enddatetime =",enddatetime)
print("start_refimage_mjdobs =",start_refimage_mjdobs)
print("end_refimage_mjdobs =",end_refimage_mjdobs)
print("min_refimage_nframes =",min_refimage_nframes)
print("make_refimages_flag =",make_refimages_flag)
print("dry_run =",dry_run)


#################
# Main program.
#################

if __name__ == '__main__':


    #
    # Analyze science-pipeline products for exposures/SCAs in input observation datetime range.
    # This script covers same fields/fids as launchSciencePipelinesForDateTimeRangeWithRefImageWindow.py.
    #


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Query database for all field/filter/nframes combinations in reference-image window with
    # minimum number of frames in coadd stack.

    print("Querying database for all field/filter/nframes combinations in the " +
          "reference-image window with minimum number of frames in coadd stack.")

    n_filters = 8

    num = 1

    field_list = []
    fid_list = []

    for fid in range(1,n_filters + 1):

        recs = dbh.get_field_fid_nframes_records_for_mjdobs_range(start_refimage_mjdobs,end_refimage_mjdobs,min_refimage_nframes,fid)

        if dbh.exit_code >= 64:
            print("*** Error from query for field/filter/nframes combinations {}; quitting ".format(swname))
            exit(dbh.exit_code)

        for rec in recs:

            field = rec[0]
            fid = rec[1]
            nframes = rec[2]

            field_list.append(field)
            fid_list.append(fid)

            print("num,field,fid,nframes =",num,field,fid,nframes)

            num += 1


    # Loop over field/filter combinations.
    #
    # Note: In order to run an instance of the RAPID pipeline that both
    # 1. Generates a reference image; and
    # 2. Processes a science image
    # the database query for a given field and filter must return min_refimage_nframes plus one
    # (includes frames reserved for reference-image generation, plus one frame for image-differencing).
    # This is only checked explicitly if export STARTDATETIME="dynamic".

    rid_list = []

    for field,fid in zip(field_list,fid_list):

        if run_fid is not None:
            if int(run_fid) != fid:
                print(f"*** Message: Skipping fid={fid}; continuing...")
                continue

        print("field,fid =",field,fid)


        # Query database for all L2Files records associated with input observation datetime range,
        # for a given field and filter.  Return a time-ordered,SCA-ordered list.

        recs = dbh.get_l2files_records_for_datetime_range_field_fid(startdatetime,enddatetime,field,fid)

        if dbh.exit_code >= 64:
             print("*** Error from query for L2Files records {}; quitting ".format(swname))
             exit(dbh.exit_code)

        n_records = len(recs)

        if startdatetime == "dynamic":

            if first_char == "[" and last_char == "]":
                fid_index = fid - 1
                min_required_frames = min_refimage_nframes[fid_index] + 1          # min_refimage_nframes is a list.
            else:
                min_required_frames = min_refimage_nframes + 1                     # min_refimage_nframes is an integer.

            if n_records < min_required_frames:
                continue
            else:
                # Skip L2 science images reserved for reference-image generation.
                # min_required_frames includes frames reserved for reference-image generation, plus one frame for image-differencing.
                for i in range(min_required_frames - 1):
                    recs.pop(0)


        # For the remaining records (which are not reserved for reference-image generation),
        # aggregate pipeline instances to be run under AWS Batch.
        # When the flag to make reference images is set to True, then only one
        # representative L2 science image for the field and filter is processed
        # to initially make the needed reference image for the other L2 science images
        # with the same field and filter;  when it is set to False then
        # all other L2 science images, except for the representative L2 science images,
        # are processed.  The representative L2 science image is the first in
        # a time-ordered, SCA-ordered list for a given field and filter.

        if make_refimages_flag:
            # Process only first record in ordered list of records.
            rec = recs[0]
            rid = rec[0]
            sca = rec[1]
            rid_list.append(rid)
            print("rid, sca =",rid,sca)
        else:
            # Process all records except first record in ordered list of records.
            recs.pop(0)
            for rec in recs:
                rid = rec[0]
                sca = rec[1]
                rid_list.append(rid)
                print("rid, sca =",rid,sca)


    # For each rid, lookup jid and build S3-bucket URL for products.

    number_pipeline_instances = len(rid_list)
    print(f"number_pipeline_instances = {number_pipeline_instances}")


    for rid in rid_list:

        query = f"SELECT jid FROM jobs " +\
            f"WHERE rid = {rid} AND ppid = 15;"

        sql_queries = []
        sql_queries.append(query)
        records = dbh.execute_sql_queries(sql_queries,debug)

        n_records = len(records)

        if n_records > 1:
            print(f"More than one record returned when querying for jid from jobs table (n_records={n_records}); quitting...")

        for record in records:
            jid = record[0]

        s3_url = f"s3://rapid-product-files/procdate/jid{jid}/"

        print(f"=====>s3_url = {s3_url}")

        ls_cmd = f"aws s3 ls {s3_url}/ | grep awaicgen"
        exitcode_from_ls,code_to_execute_stdout = util.execute_command_in_shell(ls_cmd,print_output=False)
        lines = code_to_execute_stdout.splitlines()

        i = 0
        for line in lines:

            print(line)


    # Close database connection.

    dbh.close()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to launch all pipelines =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Termination.

    terminating_exitcode = 0

    print("terminating_exitcode =",terminating_exitcode)

    exit(terminating_exitcode)
