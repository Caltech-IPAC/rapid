"""
Reformat rimtimsim FITS files:
1. Remove CDELT1 and CDELT2 keywords
2. Change PC1_1 to CD1_1, etc.
3. Put image data in second HDU with copy of primary header
4. Add MJD-OBS from TSTART converted to MJD
5. Copy EXPOSURE to EXPTIME
6. Rename FITS file from
rimtimsim/rimtimsim_WFI_lvl02_F087_SCA02_field03_rampfitted_exposureno_000017675_sim.fits
to
rimtimsim_lite/rimtimsim_WFI_F087_SCA02_000017675_lite.fits
7. Discard uncertainty HDU
8. Add SIP distortion keywords with zero values.
9. Modify CTYPE1 and CTYPE2 keyword values from TAN to TAN-SIP.
10. Modify CRPIX1 and CRPIX2 keyword values to the coordinates of the image center.
11. Transpose image data to correct WCS (original simulated data are incorrect).
12. Multiply by exposure time to convert e-/s into DN (assuming sca_gain = 1.0).
13. Add more FITS keywords: SCA_NUM, BUNIT = "DN", ZPTMAG for consistency with Open Universe sims.
"""

from astropy.io import fits
import numpy as np
import boto3
import re
from astropy.wcs import WCS

import modules.utils.rapid_pipeline_subs as util


bucket_name_input = "rimtimsim-260622"
bucket_name_output = "rimtimsim-260622-lite"


# Create S3 resource and client objects.

s3_resource = boto3.resource('s3')
s3_client = boto3.client('s3')


# Parse input files in input S3 bucket.

my_bucket_input = s3_resource.Bucket(bucket_name_input)

input_fits_files = []

for my_bucket_input_object in my_bucket_input.objects.all():

    #print(my_bucket_input_object.key)

    fname_input = my_bucket_input_object.key

    only_fname_input = str(fname_input)

    input_fits_files.append(only_fname_input)

n_input_fits_files = len(input_fits_files)
print(f"n_input_fits_files = {n_input_fits_files}")


# Loop over input FITS files.

for input_fits_file in input_fits_files:

    print(f"input_fits_file = {input_fits_file}")

    if ".fits" not in input_fits_file:
        continue


    # Download file from input S3 bucket to local machine.

    s3_object_input_fits_file = "s3://" + bucket_name_input + "/" + input_fits_file
    download_cmd = ['aws','s3','cp',s3_object_input_fits_file,input_fits_file]
    exitcode_from_download_cmd = util.execute_command(download_cmd)


    # Create output FITS filename for working directory.

    output_fits_file = input_fits_file.replace("_lvl02_RAPIDSIMS","")\
        .replace("_field03_rampfitted_exposureno","").replace("sim.fits","_lite.fits")

    print("output_fits_file =",output_fits_file)


    # Read input FITS file.

    hdul = fits.open(input_fits_file)
    hdr = hdul[0].header
    data = hdul[0].data


    # Transpose data, to correct WCS.

    transpose_data = np.transpose(data)


    # Print original values in FITS header.

    crpix1_orig = hdr["CRPIX1"]
    crpix2_orig = hdr["CRPIX2"]
    crval1_orig = hdr['CRVAL1']
    crval2_orig = hdr['CRVAL2']

    print(f"crpix1_orig,crpix2_orig,crval1_orig,crval2_orig = " +
          f"{crpix1_orig}, {crpix2_orig}, {crval1_orig}, {crval2_orig}")


    # Awaicgen requires CRPIX keywords at image center, so we must recompute the CRVAL keywords.

    crpix1 = 2044.5
    crpix2 = 2044.5

    wcs = WCS(hdr) # Initialize WCS object from FITS header

    print(wcs)

    pixel_x, pixel_y = crpix1 - 1, crpix2 - 1
    celestial_coords = wcs.pixel_to_world(pixel_x, pixel_y)
    print(f"CRVAL1,CRVAL2 Pixel ({pixel_x}, {pixel_y}) corresponds to " +
          f"{celestial_coords.ra.deg:.12f} RA and {celestial_coords.dec.deg:.12f} Dec.")

    crval1 = celestial_coords.ra.deg
    crval2 = celestial_coords.dec.deg

    hdr["CRPIX1"] = crpix1
    hdr["CRPIX2"] = crpix2

    hdr["CRVAL1"] = crval1
    hdr["CRVAL2"] = crval2

    print(f"crpix1,crpix2,crval1,crval2 = {crpix1}, {crpix2}, {crval1}, {crval2}")


    # Remove CDELT1 and CDELT2 keywords.

    hdr.remove('CDELT1', remove_all=True)
    hdr.remove('CDELT2', remove_all=True)


    # Add MJD-OBS keyword, derived from existing TSTART keyword.

    obsjd = hdr["TSTART"]
    obsmjd = obsjd - 2400000.5
    hdr["MJD-OBS"] = obsmjd


    # Rename PCi_j keywords to CDi_j keywords.

    hdr.rename_keyword('PC1_1', 'CD1_1', force=False)
    hdr.rename_keyword('PC1_2', 'CD1_2', force=False)
    hdr.rename_keyword('PC2_1', 'CD2_1', force=False)
    hdr.rename_keyword('PC2_2', 'CD2_2', force=False)


    # Add EXPTIME keyword, passed from EXPOSURE keyword.

    exptime = hdr["EXPOSURE"]
    hdr["EXPTIME"] = exptime


    # Add SCA_NUM keyword.

    detector = hdr["DETECTOR"]
    sca_num = int(detector.replace("SCA",""))
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

    filter = hdr["FILTER"]

    zptmag = 0.0                                                     # Placeholder.

    if "213" in filter:
        translated_filter = filter.replace("F213","K213").strip()
        zptmag = 25.85726796291789                                   # From Ryan for F213.
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
        zptmag = 26.29818407774948                                   # From Ryan for F087.
    elif "146" in filter:
        translated_filter = filter.replace("F146","W146").strip()
    else:
        print(f"*** Error: Unexpected filter = {filter}")
        exit(64)

    hdr["FILTER"] = translated_filter


    # Add ZPTMAG keyword.

    hdr["ZPTMAG"] = zptmag


    # Modify CTYPE1 and CTYPE2 keyword values from TAN to TAN-SIP.

    hdr["CTYPE1"] = "RA---TAN-SIP"
    hdr["CTYPE2"] = "DEC--TAN-SIP"


    # Add SIP distortion keywords with zero values.

    hdr["A_ORDER"] = 4
    hdr["A_0_2"] = 0.0
    hdr["A_0_3"] = 0.0
    hdr["A_0_4"] = 0.0
    hdr["A_1_1"] = 0.0
    hdr["A_1_2"] = 0.0
    hdr["A_1_3"] = 0.0
    hdr["A_2_0"] = 0.0
    hdr["A_2_1"] = 0.0
    hdr["A_2_2"] = 0.0
    hdr["A_3_0"] = 0.0
    hdr["A_3_1"] = 0.0
    hdr["A_4_0"] = 0.0
    hdr["B_ORDER"] = 4
    hdr["B_0_2"] = 0.0
    hdr["B_0_3"] = 0.0
    hdr["B_0_4"] = 0.0
    hdr["B_1_1"] = 0.0
    hdr["B_1_2"] = 0.0
    hdr["B_1_3"] = 0.0
    hdr["B_2_0"] = 0.0
    hdr["B_2_1"] = 0.0
    hdr["B_2_2"] = 0.0
    hdr["B_3_0"] = 0.0
    hdr["B_3_1"] = 0.0
    hdr["B_4_0"] = 0.0


    # Replace primary HDU with empty image data

    hdul[0] = fits.PrimaryHDU(header=hdr,data=None)


    # Discard uncertainty-image HDU.

    del hdul[1]


    # Create a new ImageHDU with image data.
    # Multiply by exposure time to convert e-/s into DN (assuming sca_gain = 1.0).

    hdr["BUNIT"] = "DN"

    np_data = np.array(transpose_data) * exptime
    new_hdu = fits.ImageHDU(header=hdr,data=np_data.astype(np.float32))


    # Append the new HDU to the HDU list

    hdul.append(new_hdu)


    # Write output FITS file.

    hdul.writeto(output_fits_file,overwrite=True,checksum=True)


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

    rm_cmd = ['rm','-f',input_fits_file]
    exitcode_from_rm = util.execute_command(rm_cmd)

    rm_cmd = ['rm','-f',output_fits_file]
    exitcode_from_rm = util.execute_command(rm_cmd)

    rm_cmd = ['rm','-f',gzipped_output_fits_file]
    exitcode_from_rm = util.execute_command(rm_cmd)


# Termination.

exit(0)


