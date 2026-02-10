'''
Compute forced photometry for sky positions in a given RAPID field (a.k.a. sky tile).
The input sky_positions_csv_file has 3 columns: reqid,ra,dec, and these sky positions
are required to be within the input field.
'''

#########################################################################################
#
# * Exit and warning status codes (see log output for more information):
#
#      - 0   => Successful execution.
#      - 52  => 10K input-file limit reached; user must submit another request
#               with a later JD range to complete the lightcurve.
#      - 54  => Insufficient number of background pixels.
#      - 55  => Too many bad pixels.
#      - 56  => One or more epochs have photometry measurements that may
#               be impacted by bad (including NaN'd) pixels.
#      - 57  => One or more epochs had no reference image catalog source
#               falling with 5 arcsec.
#      - 58  => One or more epochs had a reference image PSF-catalog that
#               does not exist in the archive.
#      - 60  => One or more epochs had upsampled diff-image PSF dimensions
#               that were not odd integers.
#      - 61  => One or more epochs had diff-image cutouts that were off the
#               image or too close to an edge.
#      - 62  => Requested start JD was before official survey start date
#               and was reset.
#      - 63  => No records (epochs) returned by database query.
#      - 64  => Catastrophic error (see log output).
#      - 65  => Requested end JD is before official survey start date
#      - 255 => Database connection or query execution error (see log output).
#
#########################################################################################


import csv
import boto3
import os
import shutil
import numpy as np
import healpy as hp
import configparser
from datetime import datetime, timezone
from dateutil import tz
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
to_zone = tz.gettz('America/Los_Angeles')

from astropy.wcs import WCS
from astropy.io import fits
from astropy.coordinates import SkyCoord


import database.modules.utils.rapid_db as db
import modules.utils.rapid_pipeline_subs as util
import database.modules.utils.roman_tessellation_db as sqlite

level6 = 6
nside6 = 2**level6

level9 = 9
nside9 = 2**level9


swname = "forcedPhotometryForField.py"
swvers = "1.0"
cfg_filename_only = "awsBatchSubmitJobs_launchSingleSciencePipeline.ini"

print("swname =", swname)
print("swvers =", swvers)
print("cfg_filename_only =", cfg_filename_only)


# Compute start time for benchmark.

start_time_benchmark = time.time()
start_time_benchmark_at_start = start_time_benchmark


# Compute processing datetime (UT) and processing datetime (Pacific time).

datetime_utc_now = datetime.utcnow()
proc_utc_datetime = datetime_utc_now.strftime('%Y-%m-%dT%H:%M:%SZ')
datetime_pt_now = datetime_utc_now.replace(tzinfo=timezone.utc).astimezone(tz=to_zone)
proc_pt_datetime_started = datetime_pt_now.strftime('%Y-%m-%dT%H:%M:%S PT')

print("proc_utc_datetime =",proc_utc_datetime)
print("proc_pt_datetime_started =",proc_pt_datetime_started)


# Read environment variables.

field = os.getenv('FIELD')

if field is None:

    print("*** Error: Env. var. FIELD not set; quitting...")
    exit(64)

sky_positions_csv_file = os.getenv('SKYPOSITIONSCSVFILE')

if sky_positions_csv_file is None:

    print("*** Error: Env. var. sky_positions_csv_file not set; quitting...")
    exit(64)


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

print("field =",field)
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

ppid_sci = int(config_input['SCI_IMAGE']['ppid'])

sca_gain = float(config_input['INSTRUMENT']['sca_gain'])
sca_readout_noise = float(config_input['INSTRUMENT']['sca_readout_noise'])

refimage_psf_s3_bucket_dir = config_input['JOB_PARAMS']['refimage_psf_s3_bucket_dir']
refimage_psf_filename = config_input['JOB_PARAMS']['refimage_psf_filename']


# Use SExtractor reference-image catalogs for now, since PhotUtils catalogs are not made
# for reference images at this time.  TODO

cattype = 1


#=================================================================
# Parameters to control processing.

# Match radius in degrees for cone search around field center, an overestimate,
# to get all difference images that may overlap the field (a.k.a. sky tile).
match_radius_overlap_field = float(config_input['FORCED_PHOTOMETRY']['match_radius_overlap_field'])

# switch to increase verbosity to stdout.
verbose = int(config_input['FORCED_PHOTOMETRY']['verbose'])

# debug switch: set to 1 if want to save intermediate products.
debug = int(config_input['FORCED_PHOTOMETRY']['debug'])

# simulation mode switch: set to 1 if want to simulate point source
# of fixed magnitude $simmag at each epoch using input PSFs & ZPs on
# requested position and JD range.
simflag = int(config_input['FORCED_PHOTOMETRY']['simflag'])

# magnitude of source to simulate if simulation  switch was set.
simmag = float(config_input['FORCED_PHOTOMETRY']['simmag'])

# diff-image cutout size (side length) [native pixels]
# this is also set by the PSF stamp size we plan to use.
stampsz = int(config_input['FORCED_PHOTOMETRY']['stampsz'])

# upsampling factor for stamp image pixels (on side).
stampupsamplefac = int(config_input['FORCED_PHOTOMETRY']['stampupsamplefac'])

# diameter of aperture for aperture photometry [native pixels].
apdiam = float(config_input['FORCED_PHOTOMETRY']['apdiam'])

# correction factor for PSF-fit flux uncertainty to obtain ~
# consistency with expected uncertainty in mag (~ 1.0857/snr).
corrunc = float(config_input['FORCED_PHOTOMETRY']['corrunc'])

# SNR for thresholding PSF-fit and aperture SNRs so can compute
# possible correction factor to correct for biases in PSF flux.
snrforcor = float(config_input['FORCED_PHOTOMETRY']['snrforcor'])

# minimum number of good aperture to PSF-fit flux ratios for
# computing possible flux correction to psf fluxes.
minnumrats = int(config_input['FORCED_PHOTOMETRY']['minnumrats'])

# flag to apply actual filter-dependent corrections to PSF-fit fluxes
# to obtain consistency with aperture fluxes.
applyflxcorr = int(config_input['FORCED_PHOTOMETRY']['applyflxcorr'])

# flag to correct fluxes using $simcalpsffluxcor and $simcalapfluxcor
# factors derived from comparing simulated fluxes to measured fluxes
# when $simflag = 1.
applysimcalcorr = int(config_input['FORCED_PHOTOMETRY']['applysimcalcorr'])

# flux correction factor for psf-fluxes using simulated source.
simcalpsffluxcor = float(config_input['FORCED_PHOTOMETRY']['simcalpsffluxcor'])

# flux correction factor for aperture-fluxes using simulated source.
simcalapfluxcor = float(config_input['FORCED_PHOTOMETRY']['simcalapfluxcor'])

# official start of survey (Julian Date) => use as earliest
# possible epoch for querying DB for photometry.
d_earliest = config_input['FORCED_PHOTOMETRY']['d_earliest']
jd_earliest = float(config_input['FORCED_PHOTOMETRY']['jd_earliest'])

# maximum matching radius for finding and reporting nearest
# reference-image PSF-catalog source [arcsec].
refmatchrad = float(config_input['FORCED_PHOTOMETRY']['refmatchrad'])

# maximum tolerable fraction of bad pixels in difference-image
# cutout above which photometry not possible and epoch is skipped.
maxbadpixfrac = float(config_input['FORCED_PHOTOMETRY']['maxbadpixfrac'])

# Minimum percent overlap of difference image onto sky tile (field).
minimum_percent_overlap_area = float(config_input['FORCED_PHOTOMETRY']['minimum_percent_overlap_area'])

#=================================================================

print("verbose =",verbose)
print("debug =",debug)
print("match_radius_overlap_field =",match_radius_overlap_field)
print("simflag =",simflag)
print("simmag =",simmag)
print("stampsz =",stampsz)
print("stampupsamplefac =",stampupsamplefac)
print("apdiam =",apdiam)
print("corrunc =",corrunc)
print("snrforcor =",snrforcor)
print("minnumrats =",minnumrats)
print("applyflxcorr =",applyflxcorr)
print("applysimcalcorr =",applysimcalcorr)
print("simcalpsffluxcor =",simcalpsffluxcor)
print("simcalapfluxcor =",simcalapfluxcor)
print("d_earliest =",d_earliest)
print("jd_earliest =",jd_earliest)
print("refmatchrad =",refmatchrad)
print("maxbadpixfrac =",maxbadpixfrac)
print("minimum_percent_overlap_area =",minimum_percent_overlap_area)


#################
# Main program.
#################

if __name__ == '__main__':


    '''
    Compute forced photometry for given field (all available filters).
    Another name for field is sky tile.
    '''

    rtid = field

    exitcode = 0

    s3_client = boto3.client('s3')


    # Read input sky positions, which should be (ra,dec) in degrees.

    with open(sky_positions_csv_file, mode='r', newline='') as csvfile:

        sky_postions_reader = csv.reader(csvfile, delimiter=',')

        next(sky_postions_reader)                           # Skip header line.

        reqid_list = []
        ra_list = []
        dec_list = []

        c = 0
        for row in sky_postions_reader:
            reqid = int(row[0])
            ra = float(row[1])
            dec = float(row[2])
            print(f"reqid,ra,dec = {reqid},{ra},{dec}")
            reqid_list.append(reqid)
            ra_list.append(ra)
            dec_list.append(dec)
            c += 1

    numskypositions = c


    # Get sky positions of center and four corners of sky tile.

    roman_tessellation_db.get_center_sky_position(rtid)
    ra0_field = roman_tessellation_db.ra0
    dec0_field = roman_tessellation_db.dec0
    roman_tessellation_db.get_corner_sky_positions(rtid)
    ra1_field = roman_tessellation_db.ra1
    dec1_field = roman_tessellation_db.dec1
    ra2_field = roman_tessellation_db.ra2
    dec2_field = roman_tessellation_db.dec2
    ra3_field = roman_tessellation_db.ra3
    dec3_field = roman_tessellation_db.dec3
    ra4_field = roman_tessellation_db.ra4
    dec4_field = roman_tessellation_db.dec4


    # Open database connection.

    dbh = db.RAPIDDB()

    if dbh.exit_code >= 64:
        exit(dbh.exit_code)


    # Get difference images that possibly overlap the field.
    # Returns the following columns:
    # pid,expid,sca,fid,field,jd,ra0,dec0,ra1,dec1,ra2,dec2,ra3,dec3,ra4,dec4,
    # filename,checksum,infobitssci,infobitsref,rfid,refimfilename,refimchecksum,
    # dist_field_sciimg_center (degrees).

    records = dbh.get_possible_overlapping_diffimages(ppid_sci,
                                                      cattype,
                                                      jd_earliest,
                                                      ra0_field,
                                                      dec0_field,
                                                      match_radius_overlap_field)

    nrecs = len(records)

    print(f"nrecs = {nrecs}")

    if nrecs == 0:
        print("Zero DiffImages database records returned; quitting...")


    # Filter the DiffImages database records and set up forced-photometry calculations.

    i = 0
    j = 0

    pid_list = []
    expid_list = []
    sca_list = []
    fid_list = []
    field_list = []
    jd_list = []
    ra0_list = []
    dec0_list = []
    ra1_list = []
    dec1_list = []
    ra2_list = []
    dec2_list = []
    ra3_list = []
    dec3_list = []
    ra4_list = []
    dec4_list = []
    diffimg_list = []
    checksum_list = []
    infobitssci_list = []
    infobitsref_list = []
    rfid_list = []
    refimg_list = []
    refimchecksum_list = []
    ppid_ref_list = []
    dist_field_sciimg_center_list = []
    wcs_diffimg_list = []


    # Open text file to write list of valid difference-image filenames.

    diffimglistfile = 'diffimglist.txt'

    try:
        fh_diffimglist = open(diffimglistfile, 'w', encoding="utf-8")
    except:
        print(f"*** Error: Could not open {diffimglistfile}; quitting...")
        exit(64)


    # Loop over records returned from database query.

    for record in records:
        pid = record[0]
        expid = record[1]
        sca = record[2]
        fid = record[3]
        field = record[4]
        jd = record[5]
        ra0 = record[6]
        dec0 = record[7]
        ra1 = record[8]
        dec1 = record[9]
        ra2 = record[10]
        dec2 = record[11]
        ra3 = record[12]
        dec3 = record[13]
        ra4 = record[14]
        dec4 = record[15]
        filename = record[16]
        checksum = record[17]
        infobitssci = record[18]
        infobitsref = record[19]
        rfid = record[20]
        refimfilename = record[21]
        refimchecksum = record[22]
        ppid_ref = record[23]
        dist_field_sciimg_center = record[24]

        i += 1

        print(f"i,field,filename,refimfilename,ppid_ref,dist = {i},{field},{filename},{refimfilename},{ppid_ref},{dist_field_sciimg_center}")


        # Download difference image from S3 bucket.

        s3_full_name_diff_image = filename
        diffimg_filename_from_bucket,subdirs_diff_image,downloaded_from_bucket = util.download_file_from_s3_bucket(s3_client,s3_full_name_diff_image)

        print(f"diffimg_filename_from_bucket,subdirs_diff_image,downloaded_from_bucket = {diffimg_filename_from_bucket},{subdirs_diff_image},{downloaded_from_bucket}")


        # Download reference image from S3 bucket.

        s3_full_name_ref_image = refimfilename
        refimg_filename_from_bucket,subdirs_ref_image,downloaded_from_bucket = util.download_file_from_s3_bucket(s3_client,s3_full_name_ref_image)

        print(f"refimg_filename_from_bucket,subdirs_ref_image,downloaded_from_bucket = {refimg_filename_from_bucket},{subdirs_ref_image},{downloaded_from_bucket}")


        # Read FITS file


        hdu_index_diff = 0

        with fits.open(diffimg_filename_from_bucket) as hdul:

            filter_diff = hdul[hdu_index_diff].header["FILTER"].strip()

            print("filter_diff =",filter_diff)

            hdr = hdul[hdu_index_diff].header

            wcs_diffimg = WCS(hdr) # Initialize WCS object from FITS header

        print(wcs_diffimg)

        print("CTYPE = ",wcs_diffimg.wcs.crpix)

        naxis1_diffimg = hdr['NAXIS1']
        naxis1_diffimg = hdr['NAXIS2']

        print("naxis1_diffimg,naxis1_diffimg =",naxis1_diffimg,naxis1_diffimg)

        crpix1 = wcs_diffimg.wcs.crpix[0]
        crpix2 = wcs_diffimg.wcs.crpix[1]

        crval1 = hdr['CRVAL1']
        crval2 = hdr['CRVAL2']

        print(f"crval1,crval2 = {crval1},{crval2}")


        # Example of converting pixel coordinates to celestial coordinates
        # The following should reproduce CRVAL1,CRVAL2.

        pixel_x, pixel_y = crpix1 - 1, crpix2 - 1
        celestial_coords = wcs_diffimg.pixel_to_world(pixel_x, pixel_y)
        print(f"CRVAL1,CRVAL2 Pixel ({pixel_x}, {pixel_y}) corresponds to {celestial_coords.ra.deg:.12f} RA and {celestial_coords.dec.deg:.12f} Dec.")


        # Compute pixel coordinates of diff-image center and four corners.

        x0,y0,x1,y1,x2,y2,x3,y3,x4,y4 = util.compute_pix_image_center_and_four_corners(naxis1_diffimg,naxis1_diffimg)


        # Compute percent overlap area.

        percent_overlap_area = util.compute_image_overlap_area(wcs_diffimg,
                                                               naxis1_diffimg,naxis1_diffimg,
                                                               x0,y0,
                                                               x1,y1,
                                                               x2,y2,
                                                               x3,y3,
                                                               x4,y4,
                                                               ra0_field,dec0_field,
                                                               ra1_field,dec1_field,
                                                               ra2_field,dec2_field,
                                                               ra3_field,dec3_field,
                                                               ra4_field,dec4_field)

        if percent_overlap_area > minimum_percent_overlap_area:

            diffimg_filename = f"rapid_{j}_scimrefdiffimg.fits"
            refimg_filename = f"rapid_{j}_refimg.fits"

            shutil.move(diffimg_filename_from_bucket, diffimg_filename)
            print(f"Moved '{diffimg_filename_from_bucket}' to '{diffimg_filename}'")

            shutil.move(refimg_filename_from_bucket, refimg_filename)
            print(f"Moved '{refimg_filename_from_bucket}' to '{refimg_filename}'")


            # Write valid difference-image filename and corresponding SCA gain to text list file.
            # This probably varies with SCA.  TODO

            fh_diffimglist.write(f"{diffimg_filename} {sca_gain}\n")


            # Append record columns into memory.

            pid_list.append(pid)
            expid_list.append(expid)
            sca_list.append(sca)
            fid_list.append(fid)
            field_list.append(field)
            jd_list.append(jd)
            ra0_list.append(ra0)
            dec0_list.append(dec0)
            ra1_list.append(ra1)
            dec1_list.append(dec1)
            ra2_list.append(ra2)
            dec2_list.append(dec2)
            ra3_list.append(ra3)
            dec3_list.append(dec3)
            ra4_list.append(ra4)
            dec4_list.append(dec4)
            diffimg_list.append(diffimg_filename)
            checksum_list.append(checksum)
            infobitssci_list.append(infobitssci)
            infobitsref_list.append(infobitsref)
            rfid_list.append(rfid)
            refimg_list.append(refimg_filename)
            refimchecksum_list.append(refimchecksum)
            ppid_ref_list.append(ppid_ref)
            dist_field_sciimg_center_list.append(dist_field_sciimg_center)
            wcs_diffimg_list.append(wcs_diffimg)


            # Use reference-image PSF for the forced photometry since SFFT does not
            # produce a difference-image PSF.  TODO

            refimage_psf_filename_from_bucket = refimage_psf_filename.replace("FID",str(fid))
            s3_full_name_refimage_psf = "s3://" + job_info_s3_bucket_base + "/" +\
                refimage_psf_s3_bucket_dir + "/" + refimage_psf_filename_from_bucket

            if not os.path.exists(refimage_psf_filename_from_bucket):
                refimg_psf_from_bucket,subdirs_refimage_psf,downloaded_from_bucket = \
                    util.download_file_from_s3_bucket(s3_client,s3_full_name_refimage_psf,refimage_psf_filename_from_bucket)

                print("s3_full_name_refimage_psf = ",s3_full_name_refimage_psf)
                print("refimg_psf_from_bucket = ",refimg_psf_from_bucket)


            # Define PSF filename.

            refimg_psf_filename = f"rapid_{j}_diffimgpsf.fits"

            shutil.copy2(refimage_psf_filename_from_bucket, refimg_psf_filename)
            print(f"Copied {refimage_psf_filename_from_bucket} to {refimg_psf_filename}")

            rebinpsffilename = f"rapid_{j}_rebinpsf.fits"

            hdu_index = 0
            interp_order = 1      # Don't use 2 or 3 as it (cubit) introduces negative values in rebinned PSF.

            util.trim_and_upsample_refimg_psf_fits_image(refimg_psf_filename,
                                                         hdu_index,
                                                         stampupsamplefac,
                                                         stampsz,
                                                         interp_order,
                                                         rebinpsffilename)

            j += 1


        print(f"i,j = {i},{j}")

        if i >= 50:
            break


    numrecs = j

    fh_diffimglist.close()


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to determine input difference images =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Create the text file that stores sky positions and (x,y) one-based
    # image pixel coordinates for the cforcepsfaper C module.  Whether
    # a sky position falls off a difference is checked by the C module.

    xydatafile = 'xy.txt'

    try:
        fh_xydatafile = open(xydatafile, 'w', encoding="utf-8")
    except:
        print(f"*** Error: Could not open {xydatafile}; quitting...")
        exit(64)

    c = 0

    for ra,dec in zip(ra_list,dec_list):

        for i in range(numrecs):

            f = diffimg_list[i]
            w = wcs_diffimg_list[i]
            pid = pid_list[i]

            pos = SkyCoord(ra=ra, dec=dec, unit='deg')     # Returns zero-based pixel coordinates.
            x,y = w.world_to_pixel(pos)
            print(f"Center: ra={ra}, dec={dec}) corresponds to x={x}, y={y} in ")

            x += 1       # Convert to one-based pixels coordinates.
            y += 1


            # Write positions to text list file.

            fh_xydatafile.write(f"{c} {i} {pid} {ra} {dec} {x} {y}\n")

        c += 1

    fh_xydatafile.close()


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds after generating cforcepsfaper-module inputs =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Execute ulimit increase and cforcepsfaper C module in the same shell (connected by &&).
    # Execute C module cforcepsfaper with 1 thread.


    lightcurvefile = 'lightcurve_c.dat'

    bash_shebang_cmd = "#!/bin/bash -x"
    stack_size_cmd = "ulimit -s 262144"
    show_stack_size_cmd = "ulimit -a"
    cforcepsfaper_cmd = f"/code/c/bin/cforcepsfaper -i {diffimglistfile} -a {xydatafile} " +\
                        f"-o {lightcurvefile} -t 1 -r -v >& cforcepsfaper.out"


    # Create a bash script to execute the cforcepsfaper C module, and then execute it.
    # This is not the same as running it in a python shell.

    cforcepsfaper_bash_script = 'cforcepsfaper.sh'

    try:
        fh_cforcepsfaper = open(cforcepsfaper_bash_script, 'w', encoding="utf-8")
    except:
        print(f"*** Error: Could not open {cforcepsfaper_bash_script}; quitting...")
        exit(64)

    fh_cforcepsfaper.write(f"{bash_shebang_cmd}\n")
    fh_cforcepsfaper.write(f"{stack_size_cmd}\n")
    fh_cforcepsfaper.write(f"{show_stack_size_cmd}\n")
    fh_cforcepsfaper.write(f"{cforcepsfaper_cmd}\n")

    fh_cforcepsfaper.close()

    run_chmod_was_successful = True

    exitcode_from_chmod = util.execute_command_in_shell(f"chmod +x {cforcepsfaper_bash_script}")

    if int(exitcode_from_chmod) != 0:
        run_chmod_was_successful = False

    print(f"run_chmod_was_successful = {run_chmod_was_successful}")

    run_cforcepsfaper_was_successful = True

    exitcode_from_cforcepsfaper = util.execute_command_in_shell(f"./{cforcepsfaper_bash_script}","cforcepsfaper_sh.out")

    if int(exitcode_from_cforcepsfaper) != 0:
        run_cforcepsfaper_was_successful = False

    print(f"run_cforcepsfaper_was_successful = {run_cforcepsfaper_was_successful}")


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds after executing cforcerpsfaper C module =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Parse output from cforceaper C module and store in memory.
    # The key for all dictionaries is the c-index for the different sky positions.

    forcediffimflux = {}
    forcediffimfluxunc = {}
    forcediffimsnr = {}
    forcediffimchisq = {}
    forcediffimfluxap = {}
    forcediffimfluxuncap = {}
    forcediffimsnrap = {}
    aperturecorr = {}
    exitstatuseph0 = {}
    exitstatuseph2 = {}

    for c in range(numskypositions):
        forcediffimflux[c] = []
        forcediffimfluxunc[c] = []
        forcediffimsnr[c] = []
        forcediffimchisq[c] = []
        forcediffimfluxap[c] = []
        forcediffimfluxuncap[c] = []
        forcediffimsnrap[c] = []
        aperturecorr[c] = []
        exitstatuseph0[c] = []
        exitstatuseph2[c] = []


    with open(lightcurvefile, mode='r', newline='') as csvfile:

        lightcurvefile_reader = csv.reader(csvfile, delimiter=' ')

        next(lightcurvefile_reader)                           # Skip header line.

        for row in lightcurvefile_reader:

            c = row[0]
            i = row[1]

            # row[2] stores pid; skip since it is available from DB query.
            if row[2] != pid_list[i]:
                print(f"pid from row[2] does not match pid_list for i = {i}; quitting...")
                exit(64)

            forcediffimflux[c].append(row[3])
            forcediffimfluxunc[c].append(row[4])
            forcediffimsnr[c].append(row[5])
            forcediffimchisq[c].append(row[6])
            forcediffimfluxap[c].append(row[7])
            forcediffimfluxuncap[c].append(row[8])
            forcediffimsnrap[c].append(row[9])
            aperturecorr[c].append(row[10])
            exitstatuseph0[c].append(row[11])
            exitstatuseph2[c].append(row[12])


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to load output from cforcerpsfaper C module into memory =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Create final lightcurve files, one for each sky position.


    c = 0

    for ra,dec,reqid in zip(ra_list,dec_list,reqid_list):


        final_lc_file = f'rapid_req{reqid}_lc.txt'

        try:
            fh_lc = open(final_lc_file, 'w', encoding="utf-8")
        except:
            print(f"*** Error: Could not open {final_lc_file}; quitting...")
            exit(64)

        fh_lc.write(f"pid expid sca fid field psfflux exitstatuses\n")

        for i in range(numrecs):

            pid = pid_list[i]
            expid = expid_list[i]
            sca = sca_list[i]
            fid = fid_list[i]
            field = field_list[i]
            jd = jd_list[i]
            psfflux = forcediffimflux[c][i]
            exitstatuses = exitstatuseph0[c][i]

            fh_lc.write(f"{pid} {expid} {sca} {fid} {field} {psfflux} {exitstatuses}\n")

        c += 1

        fh_lc.close()


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("Elapsed time in seconds to write final lightcurve files =",
        end_time_benchmark - start_time_benchmark)
    start_time_benchmark = end_time_benchmark


    # Code-timing benchmark overall.

    end_time_benchmark = time.time()
    print(f"Elapsed time in seconds to compute forced photometry =",
        end_time_benchmark - start_time_benchmark_at_start)

    # Termination.

    print("Terminating: exitcode =",exitcode)

    exit(exitcode)
