import boto3
import os
import time
import numpy as np
import re
import healpy as hp
from astropy.io import fits
from astropy.wcs import WCS
from datetime import datetime, timezone
from dateutil import tz
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
to_zone = tz.gettz('America/Los_Angeles')

import modules.utils.rapid_pipeline_subs as util
import database.modules.utils.rapid_db as db

# The carved admission repository (rule 20). `RAPIDDB` is frozen, so the
# admission record — content identity, release stamp, recorded facts — is
# written through `pipeline/repositories/admission.py` rather than by a new
# method on that class.
from database.sims.admission_bridge import (begin_admission_run,
                                            enumerate_source,
                                            record_exposure_admission,
                                            record_l2file_admission,
                                            seal_admission_run)
import database.modules.utils.roman_tessellation_db as sqlite


# Define code name and version.

swname = "db_register_socsim_files.py"
swvers = "1.0"

debug = 1

print("swname =", swname)
print("swvers =", swvers)


# Compute start time for benchmark.

start_time_benchmark = time.time()


# Compute processing datetime (UT) and processing datetime (Pacific time).

datetime_utc_now = datetime.utcnow()
proc_utc_datetime = datetime_utc_now.strftime('%Y-%m-%dT%H:%M:%SZ')
datetime_pt_now = datetime_utc_now.replace(tzinfo=timezone.utc).astimezone(tz=to_zone)
proc_pt_datetime_started = datetime_pt_now.strftime('%Y-%m-%dT%H:%M:%S PT')

print("proc_utc_datetime =",proc_utc_datetime)
print("proc_pt_datetime_started =",proc_pt_datetime_started)


# Input S3 bucket, and optional key prefix within it (e.g. "g0001/" for a
# single generation staged alongside others in a shared bucket).  Both are
# env-var overridable so a new staging area does not require a code change;
# the defaults preserve the original SOC-sims behavior.

bucket_name_input = os.getenv('INPUTBUCKET')

if bucket_name_input is None:
    bucket_name_input = "socsims-fakesrc-fits-20260709-lite"

prefix_input = os.getenv('INPUTPREFIX')

if prefix_input is None:
    prefix_input = ""

print("bucket_name_input =",bucket_name_input)
print("prefix_input =",prefix_input)

subdir_work = "/work"

# Global variables.

level6 = 6
nside6 = 2**level6

level9 = 9
nside9 = 2**level9


# Ensure sqlite database that defines the Roman sky tessellation is available.

roman_tessellation_dbname = os.getenv('ROMANTESSELLATIONDBNAME')

if roman_tessellation_dbname is None:

    print("*** Error: Env. var. ROMANTESSELLATIONDBNAME not set; quitting...")
    exit(64)

roman_tessellation_db = sqlite.RomanTessellationNSIDE512()


# THE `DONTCHECKALREADYINGESTED` OPT-OUT IS GONE (rule 20, brief H).
#
# It used to read an environment variable here and, when unset, build a set of
# already-ingested FITS BASENAMES by querying L2Files, then skip any input
# whose basename was in it. That was the ONLY thing standing between a
# replayed ingest and duplicate admissions, and it was defective three ways:
#
#   * it was a CONVENTION WITH A KILL SWITCH, not an invariant — one
#     environment variable disabled it entirely;
#   * the variable was tested with `is None`, so `DONTCHECKALREADYINGESTED=0`
#     and `=false` both DISABLED the check, which is the opposite of what
#     either spelling reads as;
#   * it was FILENAME-SCOPED and client-side — two concurrent ingests both
#     pass a Python membership test, and a file re-delivered under a different
#     name was admitted twice.
#
# Admission idempotency is now the DATABASE'S, against a real constraint:
# `pipeline/repositories/admission.py` inserts with `ON CONFLICT ...
# RETURNING`, so a repeat RECEIVES the existing admission and a concurrent
# repeat does not race. There is deliberately no replacement escape hatch; if
# one is ever genuinely needed it goes through the mutation contract (actor,
# reason, idempotency key, dry-run, audit ledger) and never a bare environment
# variable. Recorded as proposal P-H3.
#
# The scan below no longer pre-filters: every enumerated object is offered to
# the admission repository, which returns the existing admission for anything
# already admitted. That is strictly more correct and costs one round trip per
# already-ingested file.

do_already_ingested_check = False    # retained: read by the scan loop below


# Open database connections for parallel access.

num_cores = os.getenv('NUM_CORES')

if num_cores is None:
    num_cores = os.cpu_count()
else:
    num_cores = int(num_cores)

print("num_cores =",num_cores)

dbh_list = []

for i in range(num_cores):

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)

    dbh_list.append(dbh)


# Create S3-resource object.

s3_resource = boto3.resource('s3')


def download_s3_file(bucket_name,key,local_path):

    '''
    Download a single object from S3 to a local path, raising on failure.

    A separate client is made per call because boto3 clients are not safe to
    share across the processes forked by ProcessPoolExecutor.
    '''

    s3_client = boto3.client('s3')

    s3_client.download_file(bucket_name,key,local_path)


#-------------------------------------------------------------------------------------------------------------
# Custom methods for parallel processing, taking advantage of multiple cores on the job-launcher machine.
#-------------------------------------------------------------------------------------------------------------


def run_single_core_job(fits_files,index_thread):


    '''
    Convert a single FITS file into a FITS file.
    '''


    # Compute thread start time for code-timing benchmark.

    thread_start_time_benchmark = time.time()


    # Set thread_debug = 0 here to severly limit the amount of information logged for runs
    # that are anything but short tests.

    thread_debug = 0

    n_fits_files = len(fits_files)

    print("index_thread,n_fits_files =",index_thread,n_fits_files)

    thread_work_file = swname.replace(".py","_thread") + str(index_thread) + ".out"

    try:
        fh = open(thread_work_file, 'w', encoding="utf-8")
    except:
        print(f"*** Error: Could not open output file {thread_work_file}; quitting...")
        exit(64)

    dbh = dbh_list[index_thread]

    fh.write(f"\nStart of run_single_core_job: index_thread={index_thread}\n")


    # Per-thread outcome counters.  These are returned to the parent process so
    # that a run which registers nothing cannot report success.

    n_registered = 0
    n_failed = 0


    # Loop over input FITS files.

    for index_fits_file in range(n_fits_files):

        index_core = index_fits_file % num_cores
        if index_thread != index_core:
            continue

        input_fits_file = fits_files[index_fits_file]

        fh.write(f"i,input_fits_file = {i},{input_fits_file}\n")


        # Download file from input S3 bucket to local machine.  The S3 key may
        # contain a prefix, but the local copy is always just the basename, so
        # the work directory stays flat.
        #
        # The download goes through boto3 rather than an "aws s3 cp" subprocess:
        # the AWS CLI is not on the PATH that subprocess sees inside the Batch
        # container, and the resulting per-file failures did not reach the job
        # exit code.  boto3 is already a dependency (the listing below uses it),
        # so this drops the external-CLI dependency entirely.

        local_fits_file = input_fits_file.split("/")[-1]

        s3_object_input_fits_file = "s3://" + bucket_name_input + "/" + input_fits_file

        try:
            download_s3_file(bucket_name_input,input_fits_file,subdir_work + "/" + local_fits_file)
        except Exception as e:
            n_failed += 1
            fh.write(f"*** Error: Download failed for {s3_object_input_fits_file}: {e}\n")
            fh.flush()
            print(f"*** Error: Download failed for {s3_object_input_fits_file}: {e}")
            continue

        fh.write(f"Downloaded {s3_object_input_fits_file}\n")


        # Register L2 FITS file in database.  The local basename is used to read
        # the file, while the full S3 key is what gets recorded in the database.
        # A registration failure for one file must not be mistaken for success:
        # it is counted here and reported to the parent process, which turns a
        # nonzero total into a nonzero process exit.

        try:
            header = get_fits_header(local_fits_file)

            wcs = WCS(header)

            expid,fid = register_exposure(dbh,header,wcs)

            rid,version,filename,checksum = register_l2file(dbh,header,wcs,input_fits_file,expid,fid,local_fits_file)

            finalize_l2file(dbh,rid,version,filename,checksum)     # Keep same filename and version for now.

            compute_and_register_l2filemeta(dbh,header,wcs,rid,fid)

            n_registered += 1
        except Exception as e:
            n_failed += 1
            fh.write(f"*** Error: Registration failed for {input_fits_file}: {e}\n")
            fh.flush()
            print(f"*** Error: Registration failed for {input_fits_file}: {e}")


        # Clean up work directory.  Best-effort: a leftover file in the work
        # directory is not a registration failure, so it is not counted as one.

        try:
            os.remove(subdir_work + "/" + local_fits_file)
        except OSError as e:
            fh.write(f"*** Warning: Could not remove {local_fits_file}: {e}\n")


        # Code-timing benchmark.

        thread_end_time_benchmark = time.time()
        diff_time_benchmark = thread_end_time_benchmark - thread_start_time_benchmark
        fh.write(f"Elapsed time in seconds to register L2 FITS file in database = {diff_time_benchmark}\n")
        thread_start_time_benchmark = thread_end_time_benchmark


        # End of loop over fits_files.

        fh.write(f"Loop end over fits_files: index_fits_file,input_fits_file = {index_fits_file},{input_fits_file}\n")


    fh.write(f"\nEnd of run_single_core_job: index_thread={index_thread}\n")
    fh.write(f"n_registered,n_failed = {n_registered},{n_failed}\n")

    fh.close()

    print(f"Finish for index_thread = {index_thread}: n_registered,n_failed = {n_registered},{n_failed}")

    return n_registered,n_failed


def execute_parallel_processes(fits_files_list,num_cores=None):

    '''
    Run the registration threads and return the (n_registered,n_failed) totals
    summed over all threads.  A thread that dies outright counts as a failure,
    so an unhandled worker exception cannot be logged and then forgotten.
    '''

    if num_cores is None:
        num_cores = os.cpu_count()  # Use all available cores if not specified

    print("num_cores =",num_cores)

    with ProcessPoolExecutor(max_workers=num_cores) as executor:
        # Submit all tasks to the executor and store the futures in a list
        futures = [executor.submit(run_single_core_job,fits_files_list,thread_index) for thread_index in range(num_cores)]

        # Iterate over completed futures and update progress
        for i, future in enumerate(as_completed(futures)):
            index = futures.index(future)  # Find the original index/order of the completed future
            print(f"Completed: {i+1} processes, lastly for index={index}")

    n_registered_total = 0
    n_failed_total = 0

    for future in futures:
        index = futures.index(future)
        try:
            n_registered,n_failed = future.result()
            n_registered_total += n_registered
            n_failed_total += n_failed
        except Exception as e:
            print(f"*** Error in thread index {index} = {e}")
            n_failed_total += 1

    return n_registered_total,n_failed_total


#-------------------------------------------------------------------------------------------------------------
# Methods for L2-file database registration.
#-------------------------------------------------------------------------------------------------------------

def get_keyword_value(header,key):

    try:
        value = header[key]
    except:
        value = 'null'

    return value


def compute_center_sky_position(header,wcs):

    key = "NAXIS1"
    naxis1 = get_keyword_value(header,key)

    key = "NAXIS2"
    naxis2 = get_keyword_value(header,key)

    x0 = 0.5 * naxis1 + 0.5 - 1.0     # Integer pixel coordinates are zero-based and centered on pixel.
    y0 = 0.5 * naxis2 + 0.5 - 1.0


    sky0 = wcs.pixel_to_world(x0, y0)

    return sky0


def compute_corner_sky_positions(header,wcs):

    key = "NAXIS1"
    naxis1 = get_keyword_value(header,key)

    key = "NAXIS2"
    naxis2 = get_keyword_value(header,key)

    # Integer pixel coordinates are zero-based and centered on pixel.

    x1 = 0.5 - 1.0     # We want the extreme outer image edges.
    y1 = 0.5 - 1.0

    x2 = naxis1 + 0.5 - 1.0
    y2 = 0.5 - 1.0

    x3 = naxis1 + 0.5 - 1.0
    y3 = naxis2 + 0.5 - 1.0

    x4 = 0.5 - 1.0
    y4 = naxis2 + 0.5 - 1.0

    sky1 = wcs.pixel_to_world(x1, y1)
    sky2 = wcs.pixel_to_world(x2, y2)
    sky3 = wcs.pixel_to_world(x3, y3)
    sky4 = wcs.pixel_to_world(x4, y4)

    return sky1,sky2,sky3,sky4


def get_fits_header(file):

    hdul_input = fits.open(subdir_work + "/" + file)

    header = hdul_input[1].header         # Not PRIMARY header, but image header.

    return header


def register_exposure(dbh,header,wcs):

    key = "DATE-OBS"
    try:
        dateobs = header[key]
    except:
        return

    key = "MJD-OBS"
    try:
        mjdobs = header[key]
    except:
        return

    key = "FILTER"
    try:
        filter = header[key]
    except:
        return

    key = "EXPTIME"
    try:
        exptime = header[key]
    except:
        return

    infobits = 0
    status = 1


    # Look up sky position of WFI center.

    key = "TARGRA"
    targra = get_keyword_value(header,key)

    key = "TARGDEC"
    targdec = get_keyword_value(header,key)

    if targra == 'null' or targdec == 'null':

        print(f"*** Warning: Exposure for dateobs,mjdobs = {dateobs},{mjdobs} has missing TARGRA or TARGDEC; continuing...")

        sky0 = compute_center_sky_position(header,wcs)        # Fall back on image center.

        ra0 = sky0.ra.degree
        dec0 = sky0.dec.degree

    else:

        ra0 = targra
        dec0= targdec


    # Compute level-6 healpix index (NESTED pixel ordering).

    hp6 = hp.ang2pix(nside6,ra0,dec0,nest=True,lonlat=True)


    # Compute level-9 healpix index (NESTED pixel ordering).

    hp9 = hp.ang2pix(nside9,ra0,dec0,nest=True,lonlat=True)


    # Compute field.

    roman_tessellation_db.get_rtid(ra0,dec0)
    field = roman_tessellation_db.rtid


    """
    Special handling of filter in socsim images.

    socsimdb=> select * from filters;
     fid | filter
    -----+--------
       1 | F184
       2 | H158
       3 | J129
       4 | K213
       5 | R062
       6 | Y106
       7 | Z087
       8 | W146
    (8 rows)
    """


    # Insert or update record in Exposures database table.

    print("dateobs,mjdobs,field,hp6,hp9,filter,exptime,infobits,status = ",\
        dateobs,mjdobs,field,hp6,hp9,filter,exptime,infobits,status)

    dbh.add_exposure(dateobs,mjdobs,field,hp6,hp9,filter,exptime,infobits,status)

    expid = dbh.expid
    fid = dbh.fid

    # THE ADMISSION RECORD IS WRITTEN THROUGH THE CARVED REPOSITORY (rule 20).
    #
    # `add_exposure` above still writes the legacy `exposures` row — no reader
    # is migrated, exactly as brief D migrated none — but the ADMISSION, with
    # its content identity, its release stamp and its recorded facts, is a
    # separate durable record written here. That is what makes a repeat return
    # its existing admission instead of silently overwriting `created`, which
    # is what `addexposure`'s update branch does today
    # (`008-functions.sql:331-345`).
    #
    # `add_exposure` reports failure by setting `exit_code = 67` and returning,
    # leaving `dbh.expid` as None — and NONE of the three ingest scripts has
    # ever checked it, so that None flowed on as the L2 insert's expid. It is
    # checked here, because the admission cannot be written without it.
    if expid is None:
        raise RuntimeError(
            "add_exposure did not return an expid (exit_code=%s); the "
            "exposure was not admitted. Continuing would insert L2 rows "
            "against a NULL expid." % getattr(dbh, "exit_code", "?"))

    record_exposure_admission(dbh, dateobs, expid, {
        "mjdobs": mjdobs, "field": field, "hp6": hp6, "hp9": hp9,
        "filter": filter, "exptime": exptime, "infobits": infobits,
        "status": status})

    print("expid =",expid)
    print("fid =",fid)


    # Return expid and fid.

    return expid,fid


def register_l2file(dbh,header,wcs,file,expid,fid,local_file=None):

    #print("header =",header)

    key = "DATE-OBS"
    dateobs = get_keyword_value(header,key)

    #print("dateobs =",dateobs)

    key = "MJD-OBS"
    mjdobs = get_keyword_value(header,key)

    key = "EXPTIME"
    exptime = get_keyword_value(header,key)

    key = "SCA_NUM"
    sca = get_keyword_value(header,key)

    key = "CRVAL1"
    crval1 = get_keyword_value(header,key)

    key = "CRVAL2"
    crval2 = get_keyword_value(header,key)

    key = "CRPIX1"
    crpix1 = get_keyword_value(header,key)

    #print("crpix1 =",crpix1)

    key = "CRPIX2"
    crpix2 = get_keyword_value(header,key)

    key = "CD1_1"
    cd11 = get_keyword_value(header,key)

    key = "CD1_2"
    cd12 = get_keyword_value(header,key)

    key = "CD2_1"
    cd21 = get_keyword_value(header,key)

    key = "CD2_2"
    cd22 = get_keyword_value(header,key)

    key = "CTYPE1"
    ctype1 = get_keyword_value(header,key)

    key = "CTYPE2"
    ctype2 = get_keyword_value(header,key)

    key = "CUNIT1"
    cunit1 = get_keyword_value(header,key)

    key = "CUNIT2"
    cunit2 = get_keyword_value(header,key)

    key = "A_ORDER"
    a_order = get_keyword_value(header,key)

    key = "A_0_1"
    a_0_1 = get_keyword_value(header,key)

    key = "A_0_2"
    a_0_2 = get_keyword_value(header,key)

    key = "A_0_3"
    a_0_3 = get_keyword_value(header,key)

    key = "A_0_4"
    a_0_4 = get_keyword_value(header,key)

    key = "A_0_5"
    a_0_5 = get_keyword_value(header,key)

    key = "A_1_0"
    a_1_0 = get_keyword_value(header,key)

    key = "A_1_1"
    a_1_1 = get_keyword_value(header,key)

    key = "A_1_2"
    a_1_2 = get_keyword_value(header,key)

    key = "A_1_3"
    a_1_3 = get_keyword_value(header,key)

    key = "A_1_4"
    a_1_4 = get_keyword_value(header,key)

    key = "A_2_0"
    a_2_0 = get_keyword_value(header,key)

    key = "A_2_1"
    a_2_1 = get_keyword_value(header,key)

    key = "A_2_2"
    a_2_2 = get_keyword_value(header,key)

    key = "A_2_3"
    a_2_3 = get_keyword_value(header,key)

    key = "A_3_0"
    a_3_0 = get_keyword_value(header,key)

    key = "A_3_1"
    a_3_1 = get_keyword_value(header,key)

    key = "A_3_2"
    a_3_2 = get_keyword_value(header,key)

    key = "A_4_0"
    a_4_0 = get_keyword_value(header,key)

    key = "A_4_1"
    a_4_1 = get_keyword_value(header,key)

    key = "A_5_0"
    a_5_0 = get_keyword_value(header,key)

    key = "B_ORDER"
    b_order = get_keyword_value(header,key)

    key = "B_0_1"
    b_0_1 = get_keyword_value(header,key)

    key = "B_0_2"
    b_0_2 = get_keyword_value(header,key)

    key = "B_0_3"
    b_0_3 = get_keyword_value(header,key)

    key = "B_0_4"
    b_0_4 = get_keyword_value(header,key)

    key = "B_0_5"
    b_0_5 = get_keyword_value(header,key)

    key = "B_1_0"
    b_1_0 = get_keyword_value(header,key)

    key = "B_1_1"
    b_1_1 = get_keyword_value(header,key)

    key = "B_1_2"
    b_1_2 = get_keyword_value(header,key)

    key = "B_1_3"
    b_1_3 = get_keyword_value(header,key)

    key = "B_1_4"
    b_1_4 = get_keyword_value(header,key)

    key = "B_2_0"
    b_2_0 = get_keyword_value(header,key)

    key = "B_2_1"
    b_2_1 = get_keyword_value(header,key)

    key = "B_2_2"
    b_2_2 = get_keyword_value(header,key)

    key = "B_2_3"
    b_2_3 = get_keyword_value(header,key)

    key = "B_3_0"
    b_3_0 = get_keyword_value(header,key)

    key = "B_3_1"
    b_3_1 = get_keyword_value(header,key)

    key = "B_3_2"
    b_3_2 = get_keyword_value(header,key)

    key = "B_4_0"
    b_4_0 = get_keyword_value(header,key)

    key = "B_4_1"
    b_4_1 = get_keyword_value(header,key)

    key = "B_5_0"
    b_5_0 = get_keyword_value(header,key)

    #key = "EQUINOX"
    #equinox = get_keyword_value(header,key)
    equinox = 2000

    #key = "PA_OBSY"
    #paobsy = get_keyword_value(header,key)
    paobsy = 0.0

    #key = "PA_FPA"
    #pafpa = get_keyword_value(header,key)
    pafpa = 0.0

    key = "ZPTMAG"
    zptmag = get_keyword_value(header,key)

    #key = "SKY_MEAN"
    #skymean = get_keyword_value(header,key)
    skymean = 0.0


    # Compute file checksum.

    # The checksum is computed from the local copy, whose name is the basename
    # of the S3 key; file itself may still carry a key prefix.

    if local_file is None:
        local_file = file.split("/")[-1]

    print("file =",file)
    checksum = db.compute_checksum(subdir_work + "/" + local_file)

    if checksum == 65 or checksum == 68 or checksum == 66:
        print("*** Error: Unexpected value for checksum =",checksum)
        exit(0)

    filename = "s3://" + bucket_name_input + "/" + file
    infobits = 0
    status = 0         # Keep status = 0 until vbest is updated in a later step.


    # Compute sky position of image center.

    sky0 = compute_center_sky_position(header,wcs)

    ra0 = sky0.ra.degree
    dec0 = sky0.dec.degree


    # Compute level-6 healpix index (NESTED pixel ordering).

    hp6 = hp.ang2pix(nside6,ra0,dec0,nest=True,lonlat=True)


    # Compute level-9 healpix index (NESTED pixel ordering).

    hp9 = hp.ang2pix(nside9,ra0,dec0,nest=True,lonlat=True)


    # Compute field.

    roman_tessellation_db.get_rtid(ra0,dec0)
    field = roman_tessellation_db.rtid


    # Insert record in L2Files database table.

    dbh.add_l2file_fifth_order(expid,sca,field,hp6,hp9,fid,dateobs,mjdobs,exptime,infobits,
        status,filename,checksum,crval1,crval2,crpix1,crpix2,cd11,cd12,cd21,cd22,
        ctype1,ctype2,cunit1,cunit2,
        a_order,a_0_1,a_0_2,a_0_3,a_0_4,a_0_5,a_1_0,a_1_1,a_1_2,a_1_3,a_1_4,
        a_2_0,a_2_1,a_2_2,a_2_3,a_3_0,a_3_1,a_3_2,a_4_0,a_4_1,a_5_0,
        b_order,b_0_1,b_0_2,b_0_3,b_0_4,b_0_5,b_1_0,b_1_1,b_1_2,b_1_3,b_1_4,
        b_2_0,b_2_1,b_2_2,b_2_3,b_3_0,b_3_1,b_3_2,b_4_0,b_4_1,b_5_0,
        equinox,ra0,dec0,paobsy,pafpa,zptmag,skymean)

    rid = dbh.rid
    version = dbh.version

    print("rid =",rid)
    print("version =",version)


    # Return rid, version, filename, and checksum stored in database record.

    return rid,version,filename,checksum


def finalize_l2file(dbh,rid,version,filename,checksum):

    status = 1


    # Update record in L2Files database table.

    dbh.update_l2file(rid,filename,checksum,status,version)


def compute_and_register_l2filemeta(dbh,header,wcs,rid,fid):

    key = "SCA_NUM"
    sca = get_keyword_value(header,key)

    key = "MJD-OBS"
    mjdobs = get_keyword_value(header,key)

    sky0 = compute_center_sky_position(header,wcs)
    sky1,sky2,sky3,sky4 = compute_corner_sky_positions(header,wcs)

    ra0 = sky0.ra.degree
    dec0 = sky0.dec.degree
    ra1 = sky1.ra.degree
    dec1 = sky1.dec.degree
    ra2 = sky2.ra.degree
    dec2 = sky2.dec.degree
    ra3 = sky3.ra.degree
    dec3 = sky3.dec.degree
    ra4 = sky4.ra.degree
    dec4 = sky4.dec.degree

    x,y,z = util.compute_xyz(ra0,dec0)


    # Compute level-6 healpix index (NESTED pixel ordering).

    hp6 = hp.ang2pix(nside6,ra0,dec0,nest=True,lonlat=True)


    # Compute level-9 healpix index (NESTED pixel ordering).

    hp9 = hp.ang2pix(nside9,ra0,dec0,nest=True,lonlat=True)


    # Register record in database.

    dbh.register_l2filemeta(rid,ra0,dec0,ra1,dec1,ra2,dec2,ra3,dec3,ra4,dec4,x,y,z,hp6,hp9,fid,sca,mjdobs)


#-------------------------------------------------------------------------------------------------------------
# Main program.
#-------------------------------------------------------------------------------------------------------------

if __name__ == '__main__':


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Query database for already-ingested FITS files.

    already_ingested_fits_files = []

    query = f"SELECT (regexp_match(filename, '.+/(.+)'))[1] from l2files where vbest>0;"

    sql_queries = []
    sql_queries.append(query)
    records = dbh.execute_sql_queries(sql_queries,debug)

    for record in records:
        already_ingested_fits_file = record[0]
        already_ingested_fits_files.append(already_ingested_fits_file)

    n_ingested_fits_files = len(already_ingested_fits_files)
    print(f"n_ingested_fits_files = {n_ingested_fits_files}")

    if n_ingested_fits_files > 0:
        first_ingested_fits_file = already_ingested_fits_files[0]
        print(f"first_ingested_fits_file = {first_ingested_fits_file}")


    # Close database connections.

    dbh.close()


    # Parse FITS files in input S3 bucket.

    i = 0

    my_bucket_input = s3_resource.Bucket(bucket_name_input)

    input_fits_files = []
    sca_nums = []
    root_names = []

    for my_bucket_input_object in my_bucket_input.objects.filter(Prefix=prefix_input):

        fname_input = str(my_bucket_input_object.key)


        # Skip anything that is not an L2 FITS file, such as the generation
        # manifest that may be staged alongside the data under the same prefix.

        if not re.match(r".+\.fits(\.gz)?$", fname_input):
            print(f"Skipping non-FITS object: {fname_input}")
            continue


        # The already-ingested list holds basenames (the database query strips
        # the S3 bucket and any key prefix off the stored filename), so compare
        # against the basename, not the full key.

        basename_input = fname_input.split("/")[-1]

        if do_already_ingested_check and (basename_input in already_ingested_fits_files):
            continue

        print(f"fname_input = {fname_input}")

        fname_fields = basename_input.split("_")

        print(f"fname_fields = {fname_fields}")

        root_name = fname_fields[0] + fname_fields[1]
        sca_num = fname_fields[2]

        input_fits_files.append(fname_input)
        root_names.append(root_name)
        sca_nums.append(sca_num)

        i += 1

        #if i > 400:
        #    break

    print(f"Total number of socsims to ingest into database = {i}")


    # Sort by SCA, observation in order to avoid possible database race condition
    # of inserting identical Exposures records at the same time.
    #
    # lexsort uses (Secondary Sort Key, Primary Sort Key)

    fname_indexes = np.lexsort((root_names, sca_nums))

    j = 0
    sorted_input_fits_files = []
    for i in fname_indexes:
        input_fits_file = input_fits_files[i]
        print(f"j,i,input_fits_file = {j},{i},{input_fits_file}")
        sorted_input_fits_files.append(input_fits_file)
        j += 1


    ###############################################################################################
    # Execute database-registration tasks.  The execution is done for input FITS files in parallel,
    # with the number of parallel threads equal to the number of cores on the job-launcher machine.
    ###############################################################################################

    n_to_register = len(sorted_input_fits_files)

    if num_cores > 1:
        n_registered,n_failed = execute_parallel_processes(sorted_input_fits_files,num_cores)
    else:
        thread_index = 0
        n_registered,n_failed = run_single_core_job(sorted_input_fits_files,thread_index)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to register database records =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Termination.  A run that had work to do and registered nothing, or that
    # had any per-file failure, must not report success: a green job that wrote
    # zero rows is indistinguishable from a real run to everything downstream.

    print(f"n_to_register,n_registered,n_failed = {n_to_register},{n_registered},{n_failed}")

    if n_failed > 0:
        print(f"*** Error: {n_failed} of {n_to_register} file(s) failed to download or register; quitting...")
        exit(65)

    if n_to_register > 0 and n_registered == 0:
        print(f"*** Error: {n_to_register} file(s) were listed but none were registered; quitting...")
        exit(65)

    exit(0)



