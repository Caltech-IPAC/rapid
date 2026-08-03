import os
import configparser
from datetime import datetime, timezone
from dateutil import tz
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
to_zone = tz.gettz('America/Los_Angeles')

import database.modules.utils.rapid_db as db
import modules.utils.rapid_pipeline_subs as util
import database.modules.utils.roman_tessellation_db as sqlite


swname = "crossMatchSources.py"
swvers = "1.0"
cfg_filename_only = "awsBatchSubmitJobs_launchSingleSciencePipeline.ini"

print("swname =", swname)
print("swvers =", swvers)
print("cfg_filename_only =", cfg_filename_only)


# Set debug = 1 here to get debug messages for creating and setting up Merges and AstroObjects tables.

debug = 1


# Compute start time for benchmark.

start_time_benchmark = time.time()
start_time_benchmark_at_start = start_time_benchmark


# Compute processing datetime (UT) and processing datetime (Pacific time).

datetime_utc_now = datetime.now(timezone.utc)
proc_utc_datetime = datetime_utc_now.strftime('%Y-%m-%dT%H:%M:%SZ')
datetime_pt_now = datetime_utc_now.astimezone(tz=to_zone)
proc_pt_datetime_started = datetime_pt_now.strftime('%Y-%m-%dT%H:%M:%S PT')

print("proc_utc_datetime =",proc_utc_datetime)
print("proc_pt_datetime_started =",proc_pt_datetime_started)


# JOBPROCDATE of RAPID science-pipeline jobs that already ran.

proc_date = os.getenv('JOBPROCDATE')

if proc_date is None:

    print("*** Error: Env. var. JOBPROCDATE not set; quitting...")
    exit(64)


# Print out basic information for log file.

print("proc_date =",proc_date)


# Ensure sqlite database that defines the Roman sky tessellation is available.

roman_tessellation_dbname = os.getenv('ROMANTESSELLATIONDBNAME')

if roman_tessellation_dbname is None:

    print("*** Error: Env. var. ROMANTESSELLATIONDBNAME not set; quitting...")
    exit(64)



# Other required environment variables.

rapid_sw = os.getenv('RAPID_SW')

if rapid_sw is None:

    print("*** Error: Env. var. RAPID_SW not set; quitting...")
    exit(64)

rapid_work = os.getenv('RAPID_WORK')

if rapid_work is None:

    print("*** Error: Env. var. RAPID_WORK not set; quitting...")
    exit(64)

cfg_path = rapid_sw + "/cdf"

print("rapid_sw =",rapid_sw)
print("cfg_path =",cfg_path)


# Read input parameters from .ini file.

config_input_filename = cfg_path + "/" + cfg_filename_only
config_input = configparser.ConfigParser()
config_input.read(config_input_filename)

job_info_s3_bucket_base = config_input['JOB_PARAMS']['job_info_s3_bucket_base']
product_s3_bucket_base = config_input['JOB_PARAMS']['product_s3_bucket_base']
job_config_filename_base = config_input['JOB_PARAMS']['job_config_filename_base']
product_config_filename_base = config_input['JOB_PARAMS']['product_config_filename_base']

match_radius = float(config_input['SOURCE_MATCHING']['match_radius'])


# Open database connections for parallel access.

num_cores = os.getenv('NUM_CORES')

if num_cores is None:
    num_cores = os.cpu_count()
else:
    num_cores = int(num_cores)

print("num_cores =",num_cores)


# Define columns to be populated in AstroObjects tables.

astroobjects_cols = []
astroobjects_cols.append("aid")
astroobjects_cols.append("ra0")
astroobjects_cols.append("dec0")
astroobjects_cols.append("flux0")

astroobjects_cols_comma_separated_string = ", ".join(astroobjects_cols)
astroobjects_columns = tuple(astroobjects_cols)

print(f"AstroObjects columns: {astroobjects_cols_comma_separated_string}")


# Define columns to be populated in Merges tables.

merges_cols = []
merges_cols.append("aid")
merges_cols.append("sid")

merges_cols_comma_separated_string = ", ".join(merges_cols)
merges_columns = tuple(merges_cols)

print(f"Merges columns: {merges_cols_comma_separated_string}")


#-------------------------------------------------------------------------------------------------------------
# Custom methods for parallel processing, taking advantage of multiple cores on the job-launcher machine.
#-------------------------------------------------------------------------------------------------------------

def run_single_core_job_stage_1_crossmatching(scas,fields,index_thread):


    '''
    The current list of fields includes all fields that a science image may overlap, as found
    by querying for distinct fields all the sources child tables that are to be cross-matched.
    The cross-matching of sources in adjacent fields within field boundaries done here in stage 1
    includes populating the pertinent AstroObjects_<field> and Merges_<field> database tables for
    the relevant field.  Cross-matching sources across adjacent field boundaries is done in stage 2.

    Cross-match only sources with flags = 0.

    Cross-match one observation at a time for all SCAs in ascending time order.
    '''


    # Compute thread start time for code-timing benchmark.

    thread_start_time_benchmark = time.time()


    # Set thread_debug = 0 here to severly limit the amount of information logged for runs
    # that are anything but short tests.

    thread_debug = 0

    nfields = len(fields)

    print("index_thread,nfields =",index_thread,nfields)

    thread_work_file = swname.replace(".py","_stage_1_thread") + str(index_thread) + ".out"

    try:
        fh = open(thread_work_file, 'w', encoding="utf-8")
    except:
        print(f"*** Error: Could not open output file {thread_work_file}; quitting...")
        raise RuntimeError(f"*** Error: Could not open output file {thread_work_file}; quitting...")


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        fh.write(f"*** Error opening database connection (dbh.exit_code={dbh.exit_code}); quitting...\n")
        fh.flush()
        raise RuntimeError(f"*** Error opening database connection (dbh.exit_code={dbh.exit_code}); quitting...\n")


    fh.write(f"\nStart of run_single_core_job: index_thread={index_thread}, dbh={dbh}\n")

    my_fields = list(range(index_thread, nfields, num_cores))
    for index_field in my_fields:

        field = fields[index_field]

        astroobjects_tablename = f"astroobjects_{field}"
        merges_tablename = f"merges_{field}"

        fh.write(f"Loop start: index_field,field = {index_field},{field}\n")


        # For a given field, query for all pertinent exposures.
        # Create list of exposure IDs in ascending time order.
        # Allow for missing SCAs (cannot assume all SCAs are the same or present).

        expids_dict = {}

        for sca in scas:

            sources_tablename = f"sources_{proc_date}_{sca}"

            query = f"SELECT distinct expid,mjdobs FROM {sources_tablename} " +\
                f"WHERE field = {field} AND flags = 0;"

            sql_queries = []
            sql_queries.append(query)
            records = dbh.execute_sql_queries(sql_queries,thread_debug)

            for record in records:
                expid = record[0]
                mjdobs = record[1]
                expids_dict[expid] = mjdobs

        sorted_expids_dict = dict(sorted(expids_dict.items(), key=lambda item: item[1]))
        expids_list = list(sorted_expids_dict.keys())


        # For a given field pertinent to this parallel process,
        # loop over exposure IDs and SCAs to perform source-matching:
        # 1. Cross-match each source for field,expid,sca with the AstroObjects_<field> table.
        # 2. If there is no match, then create a new AstroObjects_<field> record.
        # 3. Register a Merges_<field> record to associate astroobject with source.
        # 4. After all SCAs are done, advance to next exposure ID in ascending time order.

        for expid in expids_list:

            astroobjects_table_file = f"astroobjects_{field}.csv"
            merges_table_file = f"merges_{field}.csv"

            with (open(astroobjects_table_file, "w") as csv_astroobjects_fh,
                 open(merges_table_file, "w") as csv_merges_fh):

                for sca in scas:

                    sources_tablename = f"sources_{proc_date}_{sca}"

                    query = f"SELECT a.sid,b.aid FROM {sources_tablename} AS a, " +\
                        f"{astroobjects_tablename} AS b " +\
                        f"WHERE q3c_join(a.ra, a.dec, b.ra0, b.dec0, {match_radius}) " +\
                        f"AND a.field = {field} AND a.expid = {expid} AND a.flags = 0;"

                    sql_queries = []
                    sql_queries.append(query)
                    records = dbh.execute_sql_queries(sql_queries,thread_debug)


                    # Code-timing benchmark.

                    thread_end_time_benchmark = time.time()
                    diff_time_benchmark = thread_end_time_benchmark - thread_start_time_benchmark
                    fh.write(f"Elapsed time in seconds to cross-match {sources_tablename} and {astroobjects_tablename} database tables = {diff_time_benchmark}\n")
                    thread_start_time_benchmark = thread_end_time_benchmark


                    # For the sources that were matched, create Merges_<field> record.

                    sid_dict = {}

                    for record in records:

                        sid = record[0]
                        aid = record[1]

                        sid_dict[sid] = 1


                        # Bulk copy is supposed to be much faster than row-by-row inserts,
                        # even for unlogged table.

                        '''
                        dbh.add_merge_to_field(merges_tablename,aid,sid)
                        '''


                        nums = ""

                        num = str(aid)
                        nums = nums + num + ","
                        num = str(sid)
                        nums = nums + num + ","

                        # Slice the string to get all but the last character, then add the newline character
                        newline_character = "\n"
                        line_to_write_to_file = nums[:-1] + newline_character

                        csv_merges_fh.write(line_to_write_to_file)


                    # Code-timing benchmark.

                    thread_end_time_benchmark = time.time()
                    diff_time_benchmark = thread_end_time_benchmark - thread_start_time_benchmark
                    fh.write(f"Elapsed time in seconds to write bulk-copy records to {merges_table_file} for matched sources = {diff_time_benchmark}\n")
                    thread_start_time_benchmark = thread_end_time_benchmark


                    # Query for all sources for the field of interest in Sources_<proc_date>_<sca> and load into memory.
                    # For the sources that were not matched for the field of interest,
                    # create AstroObjects_<field> record and then Merges_<field> record.

                    query = f"SELECT sid, ra, dec, fluxfit FROM {sources_tablename} " +\
                        f"WHERE field = {field} AND expid = {expid} AND flags = 0;"

                    sql_queries = []
                    sql_queries.append(query)
                    records = dbh.execute_sql_queries(sql_queries,thread_debug)

                    for record in records:

                        sid = record[0]

                        if sid not in sid_dict:

                            source_ra = record[1]
                            source_dec = record[2]
                            source_flux = record[3]


                            # Insert records in AstroObjects_<field> and Merges_<field> tables.
                            #
                            # Removed columns field, hp6, and hp9 from AstroObjects_<field> as an optimization.
                            #
                            # Bulk copy is supposed to be much faster than row-by-row inserts,
                            # even for unlogged table.
                            #
                            # Compute aid on job machine from deterministic method
                            # (basically creating a unique 64-bit index from (ra,dec).

                            aid = util.radec_index(source_ra, source_dec)


                            '''
                            These methods are deprecated.

                            aid = dbh.add_astro_object_to_field(astroobjects_tablename,
                                                                source_ra,
                                                                source_dec,
                                                                source_flux,
                                                                field,
                                                                source_hp6,
                                                                source_hp9,
                                                                thread_debug)

                            dbh.add_merge_to_field(merges_tablename,aid,sid,thread_debug)
                            '''


                            nums = ""

                            num = str(aid)
                            nums = nums + num + ","
                            num = str(source_ra)
                            nums = nums + num + ","
                            num = str(source_dec)
                            nums = nums + num + ","
                            num = str(source_flux)
                            nums = nums + num + ","

                            # Slice the string to get all but the last character, then add the newline character
                            newline_character = "\n"
                            line_to_write_to_file = nums[:-1] + newline_character

                            csv_astroobjects_fh.write(line_to_write_to_file)


                            nums = ""

                            num = str(aid)
                            nums = nums + num + ","
                            num = str(sid)
                            nums = nums + num + ","

                            # Slice the string to get all but the last character, then add the newline character
                            newline_character = "\n"
                            line_to_write_to_file = nums[:-1] + newline_character

                            csv_merges_fh.write(line_to_write_to_file)


                    # Code-timing benchmark.

                    thread_end_time_benchmark = time.time()
                    diff_time_benchmark = thread_end_time_benchmark - thread_start_time_benchmark
                    fh.write(f"Elapsed time in seconds to write bulk-copy records to {astroobjects_table_file} and {merges_table_file} for unmatched sources = {diff_time_benchmark}\n")
                    thread_start_time_benchmark = thread_end_time_benchmark


                    # End of loop over SCAs.

                    fh.write(f"Loop end over SCAs: index_field,field,expid,sca = {index_field},{field},{expid},{sca}\n")


            # Load records into AstroObjects_<field> database tables.

            dbh.copy_data_from_file_into_database(astroobjects_table_file,astroobjects_tablename,astroobjects_columns)

            if dbh.exit_code >= 64:
                fh.write(f"*** Error bulk-loading data from file ({astroobjects_table_file}) " +
                         f"into specified database table ({astroobjects_tablename}); quitting...\n")
                fh.flush()
                raise RuntimeError(f"*** Error bulk-loading data from file ({astroobjects_table_file}) " +
                                   f"into specified database table ({astroobjects_tablename}); quitting...")


            # Code-timing benchmark.

            thread_end_time_benchmark = time.time()
            diff_time_benchmark = thread_end_time_benchmark - thread_start_time_benchmark
            fh.write(f"Elapsed time in seconds to bulk copy records into " +
                     f"{astroobjects_tablename} database table = {diff_time_benchmark}\n")
            fh.flush()
            thread_start_time_benchmark = thread_end_time_benchmark


            # Load records into Merges_<field> database tables.

            dbh.copy_data_from_file_into_database(merges_table_file,merges_tablename,merges_columns)

            if dbh.exit_code >= 64:
                fh.write(f"*** Error bulk-loading data from file ({merges_table_file}) " +
                         f"into specified database table ({merges_tablename}); quitting...\n")
                fh.flush()
                raise RuntimeError(f"*** Error bulk-loading data from file ({merges_table_file}) " +
                                   f"into specified database table ({merges_tablename}); quitting...")


            # Code-timing benchmark.

            thread_end_time_benchmark = time.time()
            diff_time_benchmark = thread_end_time_benchmark - thread_start_time_benchmark
            fh.write(f"Elapsed time in seconds to bulk copy records into " +
                     f"{merges_tablename} database table = {diff_time_benchmark}\n")
            fh.flush()
            thread_start_time_benchmark = thread_end_time_benchmark


            # Remove no-longer-needed intermediate files.

            file_paths = [astroobjects_table_file,merges_table_file]
            for file_path in file_paths:

                if os.path.exists(file_path):
                    os.remove(file_path)
                    fh.write(f"File deleted successfully ({file_path})...\n")
                    fh.flush()
                else:
                    fh.write(f"The file does not exist({file_path})...\n")
                    fh.flush()


            # End of loop over expids.

            fh.write(f"Loop end over exposure IDs: index_field,field,expid = {index_field},{field},{expid}\n")


        # End of loop over fields.

        fh.write(f"Loop end over fields: index_field,field = {index_field},{field}\n")


        # Flush write buffer.

        fh.flush()


    # Close database connection.

    dbh.close()

    if dbh.exit_code >= 64:
        fh.write(f"*** Error closing database connection (dbh.exit_code={dbh.exit_code}); quitting...\n")
        fh.flush()
        raise RuntimeError(f"*** Error closing database connection (dbh.exit_code={dbh.exit_code}); quitting...")

    fh.write(f"\nEnd of run_single_core_job: index_thread={index_thread}\n")

    fh.close()

    message = f"Finish normally for index_thread = {index_thread}"

    return message


def run_single_core_job_stage_2_crossmatching(scas,fields,index_thread):


    '''
    The current list of fields includes all fields that a science image may overlap, as found
    by querying for distinct fields all the sources child tables that are to be cross-matched.
    Cross-matching of sources in adjacent fields outside of field boundaries is done here after
    stage 1 (populating the pertinent AstroObjects_<field> and Merges_<field> database tables
    within field boundaries).  Field boundaries are infinitesimally thin lines (no thickness),
    and the match radius can extend across them.

    Cross-match only sources with flags = 0.
    '''


    # Compute thread start time for code-timing benchmark.

    thread_start_time_benchmark = time.time()


    # Set thread_debug = 0 here to severly limit the amount of information logged for runs
    # that are anything but short tests.

    thread_debug = 0

    nfields = len(fields)

    print("index_thread,nfields =",index_thread,nfields)

    thread_work_file = swname.replace(".py","_stage_2_thread") + str(index_thread) + ".out"

    try:
        fh = open(thread_work_file, 'w', encoding="utf-8")
    except:
        print(f"*** Error: Could not open output file {thread_work_file}; quitting...")
        raise RuntimeError(f"*** Error: Could not open output file {thread_work_file}; quitting...")


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        fh.write(f"*** Error opening database connection (dbh.exit_code={dbh.exit_code}); quitting...\n")
        fh.flush()
        raise RuntimeError(f"*** Error opening database connection (dbh.exit_code={dbh.exit_code}); quitting...\n")


    # Open Roman tessellation database.

    roman_tessellation_db = sqlite.RomanTessellationNSIDE512()


    fh.write(f"\nStart of run_single_core_job: index_thread={index_thread}, dbh={dbh}\n")

    my_fields = list(range(index_thread, nfields, num_cores))
    for index_field in my_fields:

        field = fields[index_field]

        astroobjects_tablename = f"astroobjects_{field}"
        merges_tablename = f"merges_{field}"


        # Cross-match the current AstroObjects_<field> table with sources in all adjacent fields.
        # Field boundaries are infinitesimally thin lines, and the match radius can extend across them.

        fh.write(f"Loop start for adjacent fields (rtid is equivalent to field number): index_field,field = {index_field},{field}\n")

        rtids_list = roman_tessellation_db.get_all_neighboring_rtids(field)


        # If away from poles, a sky tile will have 8 adjacent fields,
        # and this can be exploited to speed up the cross-matching.

        n_adjacent_fields = len(rtids_list)

        if n_adjacent_fields == 8:


            # Get sky positions of center and four corners of sky tile.

            roman_tessellation_db.get_center_sky_position(field)
            ra0_field = roman_tessellation_db.ra0
            dec0_field = roman_tessellation_db.dec0
            roman_tessellation_db.get_corner_sky_positions(field)
            ra1_field = roman_tessellation_db.ra1
            dec1_field = roman_tessellation_db.dec1
            ra2_field = roman_tessellation_db.ra2
            dec2_field = roman_tessellation_db.dec2
            ra3_field = roman_tessellation_db.ra3
            dec3_field = roman_tessellation_db.dec3
            ra4_field = roman_tessellation_db.ra4
            dec4_field = roman_tessellation_db.dec4


            # Compute angular separation, in degrees, between field center and corner.
            # Use this with some margin to compute a radius of inclusion for cross-matching.
            # The tiles are not necessarily square or even rectangular, so choose maximum separation.

            ang_sep1 = util.compute_angular_separation(ra0_field, dec0_field, ra1_field, dec1_field)
            ang_sep2 = util.compute_angular_separation(ra0_field, dec0_field, ra2_field, dec2_field)
            ang_sep3 = util.compute_angular_separation(ra0_field, dec0_field, ra3_field, dec3_field)
            ang_sep4 = util.compute_angular_separation(ra0_field, dec0_field, ra4_field, dec4_field)

            ang_sep = max(ang_sep1,ang_sep2,ang_sep3,ang_sep4)


            # Augment the angular separation with the match radius.

            ang_sep += match_radius


        # Loop over adjacent fields and perform cross-matching.

        for rtid in rtids_list:
            adjacent_field = rtid
            fh.write(f"Cross-matching field = {field} with adjacent field = {adjacent_field}\n")


            # For a given field pertinent to this parallel process, loop over all SCAs
            # and perform source-matching:
            # 1. Cross-match each source in an adjacent field with the AstroObjects_<field> table.
            # 2. Speed it up by restricting cross-matching within the inclusion radius.
            # 3. Register Merges_<field> records for cross-matches.

            for sca in scas:

                sources_tablename = f"sources_{proc_date}_{sca}"

                if n_adjacent_fields == 8:

                    query = f"SELECT a.sid,b.aid " +\
                        f"FROM {sources_tablename} AS a, " +\
                        f"{astroobjects_tablename} AS b " +\
                        f"WHERE q3c_radial_query(a.ra, a.dec, {ra0_field}, {dec0_field}, {ang_sep}) " +\
                        f"AND q3c_join(a.ra, a.dec, b.ra0, b.dec0, {match_radius}) " +\
                        f"AND a.field = {adjacent_field} AND a.flags = 0;"

                else:

                    query = f"SELECT a.sid,b.aid " +\
                        f"FROM {sources_tablename} AS a, " +\
                        f"{astroobjects_tablename} AS b " +\
                        f"WHERE q3c_join(a.ra, a.dec, b.ra0, b.dec0, {match_radius}) " +\
                        f"AND a.field = {adjacent_field} AND a.flags = 0;"

                sql_queries = []
                sql_queries.append(query)
                records = dbh.execute_sql_queries(sql_queries,thread_debug)


                # Code-timing benchmark.

                thread_end_time_benchmark = time.time()
                diff_time_benchmark = thread_end_time_benchmark - thread_start_time_benchmark
                fh.write(f"Elapsed time in seconds to cross-match adjacent {sources_tablename} and {astroobjects_tablename} database tables = {diff_time_benchmark}\n")
                thread_start_time_benchmark = thread_end_time_benchmark


                # For the sources that were matched, create Merges_<field> record.

                for record in records:

                    sid = record[0]
                    aid = record[1]

                    dbh.add_merge_to_field(merges_tablename,aid,sid)


                # Code-timing benchmark.

                thread_end_time_benchmark = time.time()
                diff_time_benchmark = thread_end_time_benchmark - thread_start_time_benchmark
                fh.write(f"Elapsed time in seconds to insert {merges_tablename} database records for adjacent matched sources = {diff_time_benchmark}\n")
                thread_start_time_benchmark = thread_end_time_benchmark


                # End of loop over scas.

                fh.write(f"Loop end: index_field,field,sca = {index_field},{field},{sca}\n")


        # End of loop over fields.

        fh.write(f"Loop end: index_field,field = {index_field},{field}\n")


        # Flush write buffer.

        fh.flush()


    # Close database connections.

    dbh.close()

    if dbh.exit_code >= 64:
        fh.write(f"*** Error closing database connection (dbh.exit_code={dbh.exit_code}); quitting...\n")
        fh.flush()
        raise RuntimeError(f"*** Error closing database connection (dbh.exit_code={dbh.exit_code}); quitting...")

    roman_tessellation_db.close()

    fh.write(f"\nEnd of run_single_core_job: index_thread={index_thread}\n")

    fh.close()

    message = f"Finish normally for index_thread = {index_thread}"

    return message


def execute_parallel_processes_stage_1_crossmatching(scas_list,fields_list,num_cores):

    print("num_cores =",num_cores)

    with ProcessPoolExecutor(max_workers=num_cores) as executor:
        # Submit all tasks to the executor and store the futures in a list
        futures = [executor.submit(run_single_core_job_stage_1_crossmatching,scas_list,fields_list,thread_index) for thread_index in range(num_cores)]

        # Iterate over completed futures and update progress
        for i, future in enumerate(as_completed(futures)):
            index = futures.index(future)  # Find the original index/order of the completed future
            print(f"Completed: {i+1} processes, lastly for index={index}")

    for future in futures:
        index = futures.index(future)
        try:
            print(future.result())
        except Exception as e:
            print(f"*** Error in thread index {index} = {e}")


def execute_parallel_processes_stage_2_crossmatching(scas_list,fields_list,num_cores):

    print("num_cores =",num_cores)

    with ProcessPoolExecutor(max_workers=num_cores) as executor:
        # Submit all tasks to the executor and store the futures in a list
        futures = [executor.submit(run_single_core_job_stage_2_crossmatching,scas_list,fields_list,thread_index) for thread_index in range(num_cores)]

        # Iterate over completed futures and update progress
        for i, future in enumerate(as_completed(futures)):
            index = futures.index(future)  # Find the original index/order of the completed future
            print(f"Completed: {i+1} processes, lastly for index={index}")

    for future in futures:
        index = futures.index(future)
        try:
            print(future.result())
        except Exception as e:
            print(f"*** Error in thread index {index} = {e}")


#################
# Main program.
#################

if __name__ == '__main__':


    '''
    Launch parallel tasks to load AstroObjects and Merges database tables
    for all RAPID science pipelines that already ran on a given processing date,
    which have Sources database tables already loaded.
    '''


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Query for unique SCAs that have sources tables associated with processing date
    # (cannot always assume there will be 18).
    # Query the sources tables for list of unique fields.

    scas_dict = {}
    fields_dict = {}

    for i in range(18):

        sca = i + 1
        tablename = f"sources_{proc_date}_{sca}"

        sql_queries = []
        sql_queries.append(f"SELECT to_regclass('public.{tablename}') IS NOT NULL;")
        records = dbh.execute_sql_queries(sql_queries,debug)

        table_exists_flag = records[0][0]

        if table_exists_flag is not True:
            continue

        scas_dict[sca] = 1

        sql_queries = []
        sql_queries.append(f"select distinct field from {tablename} WHERE flags = 0;")
        records = dbh.execute_sql_queries(sql_queries,debug)

        for record in records:
            field = record[0]
            fields_dict[field] = 1

    scas_list = list(scas_dict.keys())
    fields_list = list(fields_dict.keys())

    nscas = len(scas_list)
    nfields = len(fields_list)

    print("scas_list =",scas_list)
    print("fields_list =",fields_list)
    print("nscas,nfields =",nscas,nfields)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to collect inputs =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Assume astoobjects_<field> and merges_<field> database tables are created in tandem,
    # so we only need to test for the existence of the former table.

    already_made_dict = {}

    for field in fields_list:

        tablename1 = f"astroobjects_{field}"

        sql_queries = []
        sql_queries.append(f"SELECT to_regclass('public.{tablename1}') IS NOT NULL;")
        records = dbh.execute_sql_queries(sql_queries,debug)

        table_exists_flag = records[0][0]

        already_made_dict[field] = table_exists_flag


    # Create astroobjects and merges database tables for all fields associated with processing date.

    print("Creating astroobjects and merges database tables for all fields associated with processing date...")

    sql_queries = []
    sql_queries.append("SET default_tablespace = pipeline_data_01;")


    for field in fields_list:

        table_exists_flag = already_made_dict[field]

        if table_exists_flag is True:
            continue

        tablename1 = f"astroobjects_{field}"

        tablename2 = f"merges_{field}"

        sql_queries.append(f"CREATE TABLE {tablename1} (LIKE astroobjects INCLUDING DEFAULTS INCLUDING CONSTRAINTS);")
        sql_queries.append(f"CREATE TABLE {tablename2} (LIKE merges INCLUDING DEFAULTS INCLUDING CONSTRAINTS);")

    dbh.execute_sql_queries(sql_queries,debug)


    # Create indexes and grants on astroobjects and merges database tables for all fields associated with processing date.

    print("Creating indexes and grants on astroobjects and merges database tables for all fields associated with processing date...")

    sql_queries = []
    sql_queries.append("SET default_tablespace = pipeline_indx_01;")

    for field in fields_list:

        table_exists_flag = already_made_dict[field]

        tablename1 = f"astroobjects_{field}"
        tablename2 = f"merges_{field}"

        if table_exists_flag is False:        # The following is done once, when the tables are created.

            sql_queries.append(f"CREATE INDEX {tablename1}_aid_idx ON {tablename1} (aid);")
            sql_queries.append(f"CREATE INDEX {tablename1}_radec_idx ON {tablename1} (q3c_ang2ipix(ra0, dec0));")
            sql_queries.append(f"CREATE INDEX {tablename2}_aid_idx ON {tablename2} USING btree (aid);")
            sql_queries.append(f"CREATE INDEX {tablename2}_sid_idx ON {tablename2} USING btree (sid);")
            sql_queries.append(f"REVOKE ALL ON TABLE {tablename1} FROM rapidreadrole;")
            sql_queries.append(f"GRANT SELECT ON TABLE {tablename1} TO GROUP rapidreadrole;")
            sql_queries.append(f"REVOKE ALL ON TABLE {tablename2} FROM rapidreadrole;")
            sql_queries.append(f"GRANT SELECT ON TABLE {tablename2} TO GROUP rapidreadrole;")
            sql_queries.append(f"REVOKE ALL ON TABLE {tablename1} FROM rapidadminrole;")
            sql_queries.append(f"GRANT ALL ON TABLE {tablename1} TO GROUP rapidadminrole;")
            sql_queries.append(f"REVOKE ALL ON TABLE {tablename2} FROM rapidadminrole;")
            sql_queries.append(f"GRANT ALL ON TABLE {tablename2} TO GROUP rapidadminrole;")
            sql_queries.append(f"REVOKE ALL ON TABLE {tablename1} FROM rapidporole;")
            sql_queries.append(f"GRANT INSERT,UPDATE,SELECT,DELETE,TRUNCATE,TRIGGER,REFERENCES ON TABLE {tablename1} TO rapidporole;")
            sql_queries.append(f"REVOKE ALL ON TABLE {tablename2} FROM rapidporole;")
            sql_queries.append(f"GRANT INSERT,UPDATE,SELECT,DELETE,TRUNCATE,TRIGGER,REFERENCES ON TABLE {tablename2} TO rapidporole;")

        sql_queries.append(f"ALTER TABLE {tablename1} SET UNLOGGED;")
        sql_queries.append(f"ALTER TABLE {tablename2} SET UNLOGGED;")

    dbh.execute_sql_queries(sql_queries,debug)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to create astroobjects and merges database tables for all fields associated with processing date =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Close database connection.

    dbh.close()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    #########################################################################################
    # Execute stage-1 source-matching tasks, which includes cross-matching sources and
    # astroobjects within field boundaries, making new merges records for sources that
    # matched and making new astroobjects and merges records for sources that did not match.
    # It is assumed that the sources child database tables have already been loaded for
    # all science-pipeline jobs on the specified processing date.
    # The execution is done for fields in parallel, with the number of parallel threads
    # equal to the number of cores on the job-launcher machine.
    #########################################################################################

    if num_cores > 1:
        execute_parallel_processes_stage_1_crossmatching(scas_list,fields_list,num_cores)
    else:
        thread_index = 0
        run_single_core_job_stage_1_crossmatching(scas_list,fields_list,thread_index)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to load astroobjects and merges database tables within field boundaries =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Cluster and analyze astroobjects database tables for all fields associated with processing date.

    print("Clustering and analyzing astroobjects database tables for all fields associated with processing date...")

    sql_queries = []
    sql_queries.append("SET default_tablespace = pipeline_indx_01;")

    for field in fields_list:

        tablename1 = f"astroobjects_{field}"

        tablename2 = f"merges_{field}"

        sql_queries.append(f"CLUSTER {tablename1} USING {tablename1}_radec_idx;")
        sql_queries.append(f"ANALYZE {tablename1};")
        sql_queries.append(f"ANALYZE {tablename2};")
        #sql_queries.append(f"ALTER TABLE {tablename1} SET LOGGED;")                # For speed, do not log.
        #sql_queries.append(f"ALTER TABLE {tablename2} SET LOGGED;")

    dbh.execute_sql_queries(sql_queries,debug)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to recluster and reanalyze astroobjects database tables =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Close database connection.

    dbh.close()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    #####################################################################################
    # Execute stage-2 source-matching tasks, which includes cross-matching sources
    # and astroobjects across adjacent field boundaries and making new merges records
    # for sources that matched.
    # It is assumed that the sources child database tables have already been loaded for
    # all science-pipeline jobs on the specified processing date.
    # The execution is done for fields in parallel, with the number of parallel threads
    # equal to the number of cores on the job-launcher machine.
    #####################################################################################

    if num_cores > 1:
        execute_parallel_processes_stage_2_crossmatching(scas_list,fields_list,num_cores)
    else:
        thread_index = 0
        run_single_core_job_stage_2_crossmatching(scas_list,fields_list,thread_index)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to load merges database tables across field boundaries =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Code-timing benchmark overall.

    end_time_benchmark = time.time()
    print(f"Elapsed time in seconds to cross-match all sources for {proc_date} =",
        end_time_benchmark - start_time_benchmark_at_start)


    # Termination.

    terminating_exitcode = 0

    print("terminating_exitcode =",terminating_exitcode)

    exit(terminating_exitcode)
