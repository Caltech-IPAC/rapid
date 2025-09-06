import boto3
import os
import numpy as np
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

roman_tessellation_db = sqlite.RomanTessellationNSIDE512()


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

output_psfcat_filename = str(config_input['PSFCAT_DIFFIMAGE']['output_zogy_psfcat_filename'])
output_psfcat_finder_filename = str(config_input['PSFCAT_DIFFIMAGE']['output_zogy_psfcat_finder_filename'])

naxis1 = int(config_input['INSTRUMENT']['naxis1_sciimage'])
naxis2 = int(config_input['INSTRUMENT']['naxis2_sciimage'])

ppid = int(config_input['SCI_IMAGE']['ppid'])

match_radius = float(config_input['SOURCE_MATCHING']['match_radius'])


# Open database connections for parallel access.

#num_cores = os.cpu_count()
num_cores = 1

dbh_list = []

for i in range(num_cores):

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)

    dbh_list.append(dbh)


# Get S3 client.

s3_client = boto3.client('s3')


# Define columns to be populated in AstroObjects tables.

cols = []
cols.append("ra0")
cols.append("dec0")
cols.append("mag0")
cols.append("meanra")
cols.append("stdefra")
cols.append("meandec")
cols.append("stdevdec")
cols.append("meanmag")
cols.append("stdevmag")
cols.append("nsources")
cols.append("field")
cols.append("hp6")
cols.append("hp9")

cols_comma_separated_string = ", ".join(cols)
columns = tuple(cols)

print(f"AstroObjects columns: {cols_comma_separated_string}")


#-------------------------------------------------------------------------------------------------------------
# Custom methods for parallel processing, taking advantage of multiple cores on the job-launcher machine.
#-------------------------------------------------------------------------------------------------------------

def run_single_core_job(scas,fields,index_thread):

    '''
    The current list of fields does NOT necessarily include ALL adjacent fields, so that
    source-matching near field boundaries may not pick up all potential light-curve data points.
    This will be rectified later.  TODO
    '''

    nfields = len(fields)

    print("index_thread,nfields =",index_thread,nfields)

    thread_work_file = swname.replace(".py","_thread") + str(index_thread) + ".out"

    try:
        fh = open(thread_work_file, 'w', encoding="utf-8")
    except:
        print(f"*** Error: Could not open output file {thread_work_file}; quitting...")
        exit(64)

    dbh = dbh_list[index_thread]

    fh.write(f"\nStart of run_single_core_job: index_thread={index_thread}, dbh={dbh}\n")

    for index_field in range(nfields):

        index_core = index_field % num_cores
        if index_thread != index_core:
            continue

        field = fields[index_field]


        fh.write(f"Loop start: index_job,field = {index_job},{field}\n")


        # For a given field pertinent to this parallel process, loop over all SCAs
        # and perform source-matching:
        # 1. Cross-match each source with the AstroObjects_<field> table.
        # 2. If there is no match, then create a new AstroObjects_<field> record.
        # 3. Register a Merges_<field> record.

        astroobjects_tablename = f"astroobjects_{field}"
        merges_tablename = f"merges_{field}"

        for sca_index in range(18):

            sca = sca_index + 1
            sources_tablename = f"sources_{proc_date}_{sca}"

            query = f"SELECT a.sid,b.aid FROM {sources_tablename} AS a, " +\
                f"{astroobjects_tablename} AS b WHERE q3c_join(a.ra, a.dec, b.ra0, b.dec0, {match_radius}) " +\
                f"AND field = {field};"

            sql_queries = []
            sql_queries.append(query)
            records = dbh.execute_sql_queries(sql_queries)

            sid_dict = {}

            # For the sources that were matched, create Merges_<field> record.

            for record in records:

                sid = record[2]
                aid = record[3]

                sid_dict[sid] = 1

                dbh.add_merge_to_field(merges_tablename,aid,sid)


            # Query for all sources for the field of interest in Sources_<proc_date>_<sca> and load into memory.
            # Find those sources that were not matched.


            query = f"SELECT sid FROM {sources_tablename} WHERE field = {field};"

            sql_queries = []
            sql_queries.append(query)
            records = dbh.execute_sql_queries(sql_queries)

            sids_list = []


            # For the sources that were not matched for the field of interest,
            # create AstroObjects_<field> record and then Merges_<field> record.

            for record in records:

                sid = record[0]
                sids_list.append(sid)

            for sid in sids_list:

                try:
                    if sid_dict[sid] == 1:
                        continue
                except:

                    # Source was not matched, so create AstroObjects_<field> record and then Merges_<field> record.

                    query = f"SELECT ra,dec,field,hp6,hp9,fluxfit FROM {sources_tablename} WHERE sid = {sid};"

                    sql_queries = []
                    sql_queries.append(query)
                    records = dbh.execute_sql_queries(sql_queries)

                    for record in records:

                        source_ra = record[0]
                        source_dec = record[1]
                        source_field = record[2]
                        source_hp6 = record[3]
                        source_hp9 = record[4]
                        source_flux = record[5]

                        source_mag = -2.5 * np.log10(source_flux)


                    # For now, set the lightcurve statistics to zero.              # TODO

                    meanra = 0
                    stdevra = 0
                    meandec = 0
                    stdevdec = 0
                    meanmag = 0
                    stdevmag = 0
                    nsources = 0

                    aid = dbh.add_astro_object_to_field(astroobjects_tablename,
                                                        source_ra,
                                                        source_dec,
                                                        source_mag,
                                                        meanra,
                                                        stdevra,
                                                        meandec,
                                                        stdevdec,
                                                        meanmag,
                                                        stdevmag,
                                                        nsources,
                                                        field,
                                                        hp6,
                                                        hp9)

                    dbh.add_merge_to_field(merges_tablename,aid,sid)


        # End of loop over field.

        fh.write(f"Loop end: index_job,field = {index_job},{field}\n")


    fh.write(f"\nEnd of run_single_core_job: index_thread={index_thread}\n")

    fh.close()


def execute_parallel_processes(scas_list,fields_list,num_cores=None):

    if num_cores is None:
        num_cores = os.cpu_count()  # Use all available cores if not specified

    print("num_cores =",num_cores)

    with ProcessPoolExecutor(max_workers=num_cores) as executor:
        # Submit all tasks to the executor and store the futures in a list
        futures = [executor.submit(run_single_core_job,scas_list,fields_list,thread_index) for thread_index in range(num_cores)]

        # Iterate over completed futures and update progress
        for i, future in enumerate(as_completed(futures)):
            index = futures.index(future)  # Find the original index/order of the completed future
            print(f"Completed: {i+1} processes, lastly for index={index}")


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
        records = dbh.execute_sql_queries(sql_queries)

        table_exists_flag = records[0][0]

        if table_exists_flag is not True:
            continue

        scas_dict[sca] = 1

        sql_queries = []
        sql_queries.append(f"select distinct field from {tablename};")
        records = dbh.execute_sql_queries(sql_queries)

        for record in records:
            field = record[0]
            fields_dict[field] = 1

    scas_list = scas_dict.keys()
    fields_list = fields_dict.keys()

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


    # Assume astoobjects_<field> and merges_field database tables are created in tandem,
    # so we only need to test for the existence of the former table.

    already_made_dict = {}

    for field in fields_list:

        tablename1 = f"astroobjects_{field}"

        sql_queries = []
        sql_queries.append(f"SELECT to_regclass('public.{tablename1}') IS NOT NULL;")
        records = dbh.execute_sql_queries(sql_queries)

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

    dbh.execute_sql_queries(sql_queries)


    # Create indexes and grants on astroobjects and merges database tables for all fields associated with processing date.

    print("Creating indexes and grants on astroobjects and merges database tables for all fields associated with processing date...")

    sql_queries = []
    sql_queries.append("SET default_tablespace = pipeline_indx_01;")

    for field in fields_list:

        table_exists_flag = already_made_dict[field]

        if table_exists_flag is True:
            continue

        tablename1 = f"astroobjects_{field}"

        tablename2 = f"merges_{field}"

        sql_queries.append(f"CREATE INDEX {tablename1}_field_idx ON {tablename1} (field);")
        sql_queries.append(f"CREATE INDEX {tablename1}_nsources_idx ON {tablename1} (nsources);")
        sql_queries.append(f"CREATE INDEX {tablename1}_aid_idx ON {tablename1} (aid);")
        sql_queries.append(f"ALTER TABLE ONLY {tablename1} ADD CONSTRAINT astroobjectspk_1 UNIQUE (ra0, dec0);")
        sql_queries.append(f"CREATE INDEX {tablename1}_radec_idx ON {tablename1} (q3c_ang2ipix(ra0, dec0));")
        sql_queries.append(f"CLUSTER {tablename1}_radec_idx ON {tablename1};")
        sql_queries.append(f"ANALYZE {tablename1};")
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

    dbh.execute_sql_queries(sql_queries)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to create astroobjects and merges database tables for all fields associated with processing date =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    ################################################################################
    # Execute source-matching tasks for all science-pipeline jobs with jids on
    # a given processing date.  The execution is done for fields in parallel, with
    # the number of parallel threads equal to the number of cores on the
    # job-launcher machine.
    ################################################################################

    if num_cores > 1:
        execute_parallel_processes(scas_list,fields_list,num_cores)
    else:
        thread_index = 0
        run_single_core_job(scas_list,fields_list,thread_index)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to load astroobjects and merges database tables for all fields associated with processing date =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Recluster and reanalyze astroobjects database tables for all fields associated with processing date.

    print("Reclustering and reanalyzing astroobjects database tables for all fields associated with processing date...")

    sql_queries = []
    sql_queries.append("SET default_tablespace = pipeline_indx_01;")

    for field in fields_list:

        tablename = f"astroobjects_{field}"

        sql_queries.append(f"CLUSTER {tablename}_radec_idx ON {tablename};")
        sql_queries.append(f"ANALYZE {tablename};")

    dbh.execute_sql_queries(sql_queries)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to recluster and reanalyze astroobjects database tables =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Close database connections.

    dbh.close()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)

    for tdbh in dbh_list:
        tdbh.close()

        if tdbh.exit_code >= 64:
            exit(tdbh.exit_code)


    # Termination.

    terminating_exitcode = 0

    print("terminating_exitcode =",terminating_exitcode)

    exit(terminating_exitcode)
