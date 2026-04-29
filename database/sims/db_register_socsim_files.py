import boto3
import os
import time
import numpy as np
import re
import subprocess
import healpy as hp
from astropy.io import fits
from astropy.wcs import WCS
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
to_zone = tz.gettz('America/Los_Angeles')

import modules.utils.rapid_pipeline_subs as util
import database.modules.utils.rapid_db as db
import database.modules.utils.roman_tessellation_db as sqlite


bucket_name_input = "socsim-20260427-lite"
subdir_work = "/work"

aws_access_key_id = os.getenv('AWS_ACCESS_KEY_ID')
aws_secret_access_key = os.getenv('AWS_SECRET_ACCESS_KEY')

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


    # Loop over input FITS files.

    for index_fits_file in range(n_fits_files):

        index_core = index_fits_file % num_cores
        if index_thread != index_core:
            continue

        input_fits_file = fits_files[index_fits_file]

        fh.write(f"i,input_fits_file = {i},{input_fits_file}\n")


        # Download file from input S3 bucket to local machine.

        s3_object_input_fits_file = "s3://" + bucket_name_input + "/" + input_fits_file
        download_cmd = ['aws','s3','cp',s3_object_input_fits_file,input_fits_file]
        exitcode_from_download_cmd = util.execute_command(download_cmd)

        fh.write(f"exitcode_from_download_cmd = {exitcode_from_download_cmd}\n")


        # Register L2 FITS file in database.

        header = get_fits_header(input_fits_file)
        expid,fid = register_exposure(dbh,header)

        wcs = WCS(header)

        rid,version,filename,checksum = register_l2file(dbh,header,wcs,input_fits_file,expid,fid)

        finalize_l2file(dbh,rid,version,filename,checksum)     # Keep same filename and version for now.

        compute_and_register_l2filemeta(dbh,header,wcs,rid,fid)


        # Clean up work directory.

        rm_cmd = ['rm','-f',subdir_work + "/" + input_fits_file]
        exitcode_from_rm = util.execute_command(rm_cmd)


        # Code-timing benchmark.

        thread_end_time_benchmark = time.time()
        diff_time_benchmark = thread_end_time_benchmark - thread_start_time_benchmark
        fh.write(f"Elapsed time in seconds to register L2 FITS file in database = {diff_time_benchmark}\n")
        thread_start_time_benchmark = thread_end_time_benchmark


        # End of loop over fits_files.

        fh.write(f"Loop end over fits_files: index_fits_file,input_fits_file = {index_fits_file},{input_fits_file}\n")


    fh.write(f"\nEnd of run_single_core_job: index_thread={index_thread}\n")

    fh.close()

    message = f"Finish normally for index_thread = {index_thread}"

    return message


def execute_parallel_processes(fits_files_list,num_cores=None):

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

    for future in futures:
        index = futures.index(future)
        try:
            print(future.result())
        except Exception as e:
            print(f"*** Error in thread index {index} = {e}")


#-------------------------------------------------------------------------------------------------------------
# Methods for L2-file database registration.
#-------------------------------------------------------------------------------------------------------------

def execute_command(cmd,no_check=False):

    max_ntries = 5

    ntries = 0
    while ntries < max_ntries:

        print("ntries = ",ntries)
        print("Executing cmd = ",cmd)

        p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for line in p.stdout.readlines():
            print("--->",line)
            strvalue = line.decode('utf-8').strip()
            print(strvalue)
        retval = p.wait()
        print("retval =",retval)

        if (retval == 0):
            break
        elif no_check:
            break

        print("Sleeping 30 seconds, then try again (up to {} tries)...".format(max_ntries))
        time.sleep(30)
        ntries += 1

    if not no_check:
        if (retval != 0):
            print("*** Error from execute_command; quitting...")
            exit(1)

    return retval


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


def register_exposure(dbh,header):

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


    # Compute sky position of image center.

    sky0 = compute_center_sky_position(header,wcs)

    ra0 = sky0.ra.degree
    dec0 = sky0.dec.degree


    # Compute level-6 healpix index (NESTED pixel ordering).

    hp6 = hp.ang2pix(nside6,ra0,dec-,nest=True,lonlat=True)


    # Compute level-9 healpix index (NESTED pixel ordering).

    hp9 = hp.ang2pix(nside9,ra0,dec-,nest=True,lonlat=True)


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

    print("expid =",expid)
    print("fid =",fid)


    # Return expid and fid.

    return expid,fid


def register_l2file(dbh,header,wcs,file,expid,fid):

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

    key = "EQUINOX"
    equinox = get_keyword_value(header,key)

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

    print("file =",file)
    checksum = db.compute_checksum(subdir_work + "/" + file)

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


    # Parse FITS files in input S3 bucket.

    i = 0

    my_bucket_input = s3_resource.Bucket(bucket_name_input)

    input_fits_files = []

    for my_bucket_input_object in my_bucket_input.objects.all():

        fname_input = str(my_bucket_input_object.key)

        print(f"fname_input = {fname_input}")

        input_fits_files.append(fname_input)

        i += 1

        #if i > 1:
        #    break

    print(f"Total number of socsims = {i}")


    ###############################################################################################
    # Execute database-registration tasks.  The execution is done for input FITS files in parallel,
    # with the number of parallel threads equal to the number of cores on the job-launcher machine.
    ###############################################################################################

    if num_cores > 1:
        execute_parallel_processes(input_fits_files,num_cores)
    else:
        thread_index = 0
        run_single_core_job(input_fits_files,thread_index)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to register database records =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Termination.

    exit(0)



