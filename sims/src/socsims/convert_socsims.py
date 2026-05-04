"""
Reformat socsims.  Convert from ASDF format to FITS format, and add required FITS keywords.

% aws s3 ls s3://stpubdata/roman/nexus/soc_simulations/r00340/l2/ | grep cal.asdf | wc
   88038  352152 6778926

On Streetfighter with 8 vCPUs, this code processes a little over 2,000 ADSF files per hour.
"""

import os
import boto3
import re
import subprocess
import numpy as np
import asdf
import roman_datamodels as rdm
from astropy.io import fits
from astropy.wcs import WCS
from astropy.time import Time
from datetime import datetime, timezone
from dateutil import tz
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
to_zone = tz.gettz('America/Los_Angeles')

import modules.utils.rapid_pipeline_subs as util


# Define code name and version.

swname = "convert_socsims.py"
swvers = "1.0"

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


# Define input and output S3 buckets.

bucket_name_input = "stpubdata/roman/nexus/soc_simulations/r00340/l2"
bucket_name_output = "socsim-20260427-lite"


# Create S3-client and S3-resource objects.

s3_client = boto3.client('s3')
s3_resource = boto3.resource('s3')


# Determine number of vCPUs to use in parallel.

num_cores_str = os.getenv('NUMCORES')

if num_cores_str is None:
    num_cores = os.cpu_count()
else:
    num_cores = int(num_cores_str)


#-------------------------------------------------------------------------------------------------------------
# Custom methods for parallel processing, taking advantage of multiple cores on the job-launcher machine.
#-------------------------------------------------------------------------------------------------------------

def run_single_core_job(asdf_files,index_thread):


    '''
    Convert a single ASDF file into a FITS file.
    '''


    # Compute thread start time for code-timing benchmark.

    thread_start_time_benchmark = time.time()


    # Set thread_debug = 0 here to severly limit the amount of information logged for runs
    # that are anything but short tests.

    thread_debug = 0

    n_asdf_files = len(asdf_files)

    print("index_thread,n_asdf_files =",index_thread,n_asdf_files)

    thread_work_file = swname.replace(".py","_thread") + str(index_thread) + ".out"

    try:
        fh = open(thread_work_file, 'w', encoding="utf-8")
    except:
        print(f"*** Error: Could not open output file {thread_work_file}; quitting...")
        exit(64)

    fh.write(f"\nStart of run_single_core_job: index_thread={index_thread}\n")


    # Loop over input ASDF files.

    for index_asdf_file in range(n_asdf_files):

        index_core = index_asdf_file % num_cores
        if index_thread != index_core:
            continue

        input_asdf_file = asdf_files[index_asdf_file]

        fh.write(f"i,input_asdf_file = {i},{input_asdf_file}\n")

        if ".asdf" not in input_asdf_file:
            continue


        # Download file from input S3 bucket to local machine.

        s3_object_input_asdf_file = "s3://" + bucket_name_input + "/" + input_asdf_file
        download_cmd = ['aws','s3','cp',s3_object_input_asdf_file,input_asdf_file]
        exitcode_from_download_cmd = util.execute_command(download_cmd)


        # Create output FITS filename for working directory.

        output_fits_file = input_asdf_file.replace(".asdf","_lite.fits")


        # Convert from ASDF format to FITS format, and add required FITS keywords.
        # Define pixel grid spacing for computing SIP distortion.

        degree = 5

        fh.write(f"degree = {degree}\n")

        asdf_to_fits(
            input_asdf_file,
            output_fits_file,
            sip_degree=degree
            )


        # Gzip the output FITS file.

        gunzip_cmd = ['gzip', output_fits_file]
        exitcode_from_gunzip = util.execute_command(gunzip_cmd)


        # Upload gzipped file to output S3 bucket.

        gzipped_output_fits_file = output_fits_file + ".gz"

        s3_object_name = gzipped_output_fits_file

        filenames = [gzipped_output_fits_file]

        objectnames = [s3_object_name]

        util.upload_files_to_s3_bucket(s3_client,bucket_name_output,filenames,objectnames)


        # Clean up work directory.

        rm_cmd = ['rm','-f',input_asdf_file]
        exitcode_from_rm = util.execute_command(rm_cmd)

        rm_cmd = ['rm','-f',gzipped_output_fits_file]
        exitcode_from_rm = util.execute_command(rm_cmd)


        # Code-timing benchmark.

        thread_end_time_benchmark = time.time()
        diff_time_benchmark = thread_end_time_benchmark - thread_start_time_benchmark
        fh.write(f"Elapsed time in seconds to convert ASDF file to FITS file = {diff_time_benchmark}\n")
        thread_start_time_benchmark = thread_end_time_benchmark


        # End of loop over asdf_files.

        fh.write(f"Loop end over asdf_files: index_asdf_file,input_asdf_file = {index_asdf_file},{input_asdf_file}\n")


    fh.write(f"\nEnd of run_single_core_job: index_thread={index_thread}\n")

    fh.close()

    message = f"Finish normally for index_thread = {index_thread}"

    return message


def execute_parallel_processes(asdf_files_list,num_cores=None):

    if num_cores is None:
        num_cores = os.cpu_count()  # Use all available cores if not specified

    print("num_cores =",num_cores)

    with ProcessPoolExecutor(max_workers=num_cores) as executor:
        # Submit all tasks to the executor and store the futures in a list
        futures = [executor.submit(run_single_core_job,asdf_files_list,thread_index) for thread_index in range(num_cores)]

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
# Methods for handling ASDF-to-FITS conversion.
#-------------------------------------------------------------------------------------------------------------

def execute_command_in_shell(bash_command,fname_out=None):

    '''
    Execute a batch command (a string, not a list; can be multiple bash commands connected with &&).
    '''

    print("execute_command: bash_command =",bash_command)


    # Execute code_to_execute.  Note that STDERR and STDOUT are merged into the same data stream.
    # AWS Batch runs Python 3.9.  According to https://docs.python.org/3.9/library/subprocess.html#subprocess.run,
    # if you wish to capture and combine both streams into one, use stdout=PIPE and stderr=STDOUT instead of capture_output.
    # capture_output=False is the default.

    code_to_execute_object = subprocess.run(bash_command,shell=True,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)

    returncode = code_to_execute_object.returncode
    print("returncode =",returncode)

    code_to_execute_stdout = code_to_execute_object.stdout
    #print("code_to_execute_stdout =\n",code_to_execute_stdout)

    if fname_out is not None:

        try:
            fh = open(fname_out, 'w', encoding="utf-8")
            fh.write(code_to_execute_stdout)
            fh.close()
        except:
            print(f"*** Warning from method execute_command: Could not open output file {fname_out}; quitting...")

    code_to_execute_stderr = code_to_execute_object.stderr
    #print("code_to_execute_stderr (should be empty since STDERR is combined with STDOUT) =\n",code_to_execute_stderr)

    return returncode,code_to_execute_stdout


def gwcs_to_fits_header(wcs_obj, shape):

    # Try the native SIP export first (gwcs >= 0.18)
    try:
        #fits_wcs, _ = wcs_obj.to_fits_sip(
        #    bounding_box=((0, shape[-1] - 1), (0, shape[-2] - 1)),
        #    max_pix_error=0.1,
        #)
        fits_wcs = wcs_obj.to_fits_sip(
            bounding_box=((0, shape[-1] - 1), (0, shape[-2] - 1)),
            max_pix_error=0.1,
            degree=5
        )
        print("Executed to_fits_sip method...")
        type_fits_wcs = type(fits_wcs)
        print(f"type_fits_wcs = {type_fits_wcs}")
        print(f"fits_wcs = {fits_wcs}")

        #return fits_wcs.to_header(relax=True)
        return fits_wcs

    except (AttributeError, Exception) as e:
        print("Could not execute to_fits_sip method...")
        # Handle the exception and print its message
        print("*** Exception thrown calling wcs_obj.to_fits_sip method:", e)
        exit(64)


def asdf_to_fits(asdf_path, fits_path, sip_degree=5):

    print(f"Reading {asdf_path}...")
    dm = rdm.open(asdf_path)

    # ------------------------------------------------------------------ #
    # Science array                                                        #
    # ------------------------------------------------------------------ #
    sci_data = np.array(dm.data)          # shape (ny, nx) or (nints, ny, nx)
    image_data_64 = sci_data.astype(np.float64)
    shape = sci_data.shape

    # ------------------------------------------------------------------ #
    # WCS                                                                  #
    # ------------------------------------------------------------------ #
    wcs_obj   = dm.meta.wcs              # gwcs.WCS instance
    wcs_header = gwcs_to_fits_header(wcs_obj, shape)


    # Build FITS file.

    hdr = fits.Header()

    hdr["EXTNAME"] = "SCI"
    hdr["NAXIS"]  = 2
    hdr["NAXIS1"] = shape[1]
    hdr["NAXIS2"] = shape[0]


    '''
    Example with required keywords:

    CRPIX1  =               2044.5 / Pixel coordinate of reference point
    CRPIX2  =               2044.5 / Pixel coordinate of reference point
    CD1_1   =  2.6187949489571E-05 / Coordinate transformation matrix element
    CD1_2   =  1.4574831453605E-05 / Coordinate transformation matrix element
    CD2_1   = -1.5281148010609E-05 / Coordinate transformation matrix element
    CD2_2   =  2.5443010447297E-05 / Coordinate transformation matrix element
    CUNIT1  = 'deg'                / Units of coordinate increment and value
    CUNIT2  = 'deg'                / Units of coordinate increment and value
    CTYPE1  = 'RA---TAN-SIP'       / TAN (gnomonic) projection + SIP distortions
    CTYPE2  = 'DEC--TAN-SIP'       / TAN (gnomonic) projection + SIP distortions
    CRVAL1  =      268.49713037465 / [deg] Coordinate value at reference point
    CRVAL2  =      -29.20479624611 / [deg] Coordinate value at reference point
    LONPOLE =                180.0 / [deg] Native longitude of celestial pole
    LATPOLE =      -29.20479624611 / [deg] Native latitude of celestial pole
    WCSNAME = 'wfiwcs_20210204_d2' / Coordinate system title
    MJDREF  =                  0.0 / [d] MJD of fiducial time
    RADESYS = 'FK5'                / Equatorial coordinate system
    EQUINOX =               2000.0 / [yr] Equinox of equatorial coordinates
    FILTER  = 'K213    '           / filter used
    NCOL    =                 4088 / number of columns in image
    NROW    =                 4088 / number of rows in image
    DETECTOR= 'SCA02   '           / detector assembly
    TSTART  =    2461486.094234365 / observation start in Julian Date
    TEND    =   2461486.0948676984 / observation end in Julian Date
    DATE-OBS= '2027-03-21T14:15:41.849' / observation start in UTC Calendar Date
    DATE-END= '2027-03-21T14:16:36.569' / observation end in UTC Calendar Date
    EXPOSURE=                54.72 / time on source in s
    SOFTWARE= 'rimtimsim_v2.0'
    CREATED = '2026-03-05 01:45:23'
    MJD-OBS =    61485.59423436504
    EXPTIME =                54.72
    ZPTMAG  =    25.85726796291789
    SCA_NUM =                    2
    '''


    # Add EXPTIME,DATE-OBS,DATE-END,MJD-OBS keywords.

    exptime = dm.meta.exposure.exposure_time
    dateobs = dm.meta.exposure.start_time
    dateend = dm.meta.exposure.end_time

    print(f"exptime = {exptime}")
    print(f"dateobs = {dateobs}")

    t_dateobs = type(dateobs)
    print(f"t_dateobs = {t_dateobs}")


    # %Y = Year, %m = Month, %d = Day, %H = Hour, %M = Minute, %S = Second
    date_object = datetime.strptime(f"{dateobs}", "%Y-%m-%dT%H:%M:%S.%f")

    t = Time(date_object)
    mjd = t.mjd

    hdr["EXPTIME"] = exptime
    hdr["DATE-OBS"] = str(dateobs)
    hdr["DATE-END"] = str(dateend)
    hdr["MJD-OBS"] = mjd


    # Add SCA_NUM keyword.

    detector = dm.meta.instrument.detector
    sca_num = int(detector.replace("WFI",""))
    hdr["SCA_NUM"] = sca_num


    # Translater filter names to be similar to Open Universe sims:
    #
    # rimtimsims2db=> select * from filters;
    # fid | filter
    # -----+--------
    #    1 | F184
    #    2 | H158
    #    3 | J129
    #    4 | K213
    #    5 | R062
    #    6 | Y106
    #    7 | Z087
    #    8 | W146
    # (8 rows)

    filter = dm.meta.instrument.optical_element
    if "213" in filter:
        translated_filter = filter.replace("F213","K213").strip()
    elif "184" in filter:
        translated_filter = filter.replace("F184","F184").strip()
    elif "158" in filter:
        translated_filter = filter.replace("F158","H158").strip()
    elif "129" in filter:
        translated_filter = filter.replace("F129","J129").strip()
    elif "062" in filter:
        translated_filter = filter.replace("F062","R062").strip()
    elif "106" in filter:
        translated_filter = filter.replace("F106","Y106").strip()
    elif "087" in filter:
        translated_filter = filter.replace("F087","Z087").strip()
    elif "146" in filter:
        translated_filter = filter.replace("F146","W146").strip()
    else:
        print(f"*** Error: Unexpected filter = {filter}")
        exit(64)

    hdr["FILTER"] = translated_filter


    # Add ZPTMAG keyword.

    '''
    Nominal WFI AB Zero Points
    Filter  Wavelength (m)    AB Zero Point (mag)
    F062    0.48 – 0.76       26.4
    F087    0.76 – 0.98       26.3
    F106    0.93 – 1.19       26.4
    F129    1.13 – 1.45       26.3
    F158    1.38 – 1.77       26.4
    F184    1.68 – 2.00       25.9
    F213    1.95 – 2.30       25.4
    F146    0.93 – 2.00       27.5
    '''

    if "213" in filter:
        zptmag = 25.4
    elif "184" in filter:
        zptmag = 25.9
    elif "158" in filter:
        zptmag = 26.4
    elif "129" in filter:
        zptmag = 26.3
    elif "062" in filter:
        zptmag = 26.4
    elif "106" in filter:
         zptmag = 26.4
    elif "087" in filter:
        zptmag = 26.3
    elif "146" in filter:
        zptmag = 27.5
    else:
        print(f"*** Error: Unexpected filter = {filter}")
        exit(64)

    hdr["ZPTMAG"] = zptmag


    # Multiply by exposure time to convert e-/s into DN (assuming sca_gain = 1.0).

    hdr["BUNIT"] = "DN"

    print(f"exptime = {exptime}")

    np_data = image_data_64 * float(exptime)


    # Create primary and image HDUs, and then output FITS file.

    new_hdu = fits.ImageHDU(header=hdr,data=np_data.astype(np.float32))

    primary_hdu = fits.PrimaryHDU(header=hdr)

    hdul = fits.HDUList([primary_hdu, new_hdu])
    hdul.writeto(fits_path,overwrite=True,checksum=True)
    print(f"Wrote       : {fits_path}")


#-------------------------------------------------------------------------------------------------------------
# Main program.
#-------------------------------------------------------------------------------------------------------------

if __name__ == '__main__':

    do_not_overwrite = False

    # Parse FITS files in output S3 bucket.

    my_bucket_output = s3_resource.Bucket(bucket_name_output)

    output_fits_files = []

    for my_bucket_output_object in my_bucket_output.objects.all():

        fname_output = str(my_bucket_output_object.key)

        #print(f"fname_output = {fname_output}")

        output_fits_files.append(fname_output)


    # Parse desired ASDF files in input S3 bucket.

    input_asdf_files = []

    cp_cmd = f"aws s3 ls s3://{bucket_name_input}/ | grep cal.asdf"
    exitcode_from_cp,code_to_execute_stdout = execute_command_in_shell(cp_cmd)
    lines = code_to_execute_stdout.splitlines()

    i = 0
    for line in lines:

        #print(line)

        input_file_metadata = line.strip().split()

        if "cal.asdf" in input_file_metadata[3]:

            input_asdf_file = input_file_metadata[3]


            # Special logic.
            if "r0034001001001001001_" not in input_asdf_file:
            #if "r0034001001001001001_0003_wfi06_f062_cal" not in input_asdf_file:
                continue


            print(f"input_asdf_file = {input_asdf_file}")

            output_fits_file = input_asdf_file.replace(".asdf","_lite.fits.gz")

            if do_not_overwrite and output_fits_file in output_fits_files:

                print(f"{output_fits_file} exists in output S3 bucket; skipping...")
                continue



            input_asdf_files.append(input_asdf_file)

        i += 1

        #if i > 1:
        #    break

    print(f"Total number of socsims = {i}")


    #########################################################################################
    # Execute asdf-to-fits tasks.  The execution is done for input ASDF files in parallel,
    # with the number of parallel threads equal to the number of cores on the job-launcher machine.
    #########################################################################################

    if num_cores > 1:
        execute_parallel_processes(input_asdf_files,num_cores)
    else:
        thread_index = 0
        run_single_core_job(input_asdf_files,thread_index)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to convert ASDF files to FITS files =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Termination.

    exit(0)


