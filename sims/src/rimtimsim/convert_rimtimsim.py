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
"""

from astropy.io import fits
import numpy as np
import boto3
import re

import modules.utils.rapid_pipeline_subs as util


bucket_name_input = "rimtimsim-250513"
bucket_name_output = "rimtimsim-250513-lite"
input_subdir = "simulated_image_data"
subdir_work = "/work"


# Parse input files in input S3 bucket.

s3_resource = boto3.resource('s3')

my_bucket_input = s3_resource.Bucket(bucket_name_input)

input_fits_files = []

for my_bucket_input_object in my_bucket_input.objects.all():

    #print(my_bucket_input_object.key)

    fname_input = my_bucket_input_object.key

    if input_subdir in fname_input:

        filename_match = re.match(r"(.+)/(.+\.fits)",fname_input)

        try:
            subdir_only = filename_match.group(1)
            only_fname_input = filename_match.group(2)
            print("-----0-----> subdir_only =",subdir_only)
            print("-----1-----> only_fname_input =",only_fname_input)

        except:
            print("-----2-----> No match in",fname_input)
            continue

        input_fits_files.append(only_fname_input)


for input_fits_file in input_fits_files:


    # Download file from input S3 bucket to local machine.

    s3_object_input_fits_file = "s3://" + bucket_name_input + "/" + input_subdir + "/" + input_fits_file
    download_cmd = ['aws','s3','cp',s3_object_input_fits_file,input_fits_file]
    exitcode_from_gunzip = util.execute_command(download_cmd)


    # Create output FITS filename for working directory.

    output_fits_file = input_fits_file.replace("_lvl02","").replace("rimtimsim/","rimtimsim_lite/")\
        .replace("_field03_rampfitted_exposureno","").replace("sim.fits","lite.fits")


    # Read input FITS file.

    hdul = fits.open(input_fits_file)
    hdr = hdul[0].header
    data = hdul[0].data


    # Transpose data, to correct WCS.

    transpose_data = np.transpose(data)


    # Modify CRPIX1,2 to image center.

    hdr["CRPIX1"] = 2044.5
    hdr["CRPIX2"] = 2044.5


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


    # Add EXPTIME keyword, passed from EXPOSURE keyword,

    exptime = hdr["EXPOSURE"]
    hdr["EXPTIME"] = exptime

    print("input_fits_file =",input_fits_file)
    print("output_fits_file =",output_fits_file)


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


    # Create a new ImageHDU with image data

    np_data = np.array(transpose_data)
    new_hdu = fits.ImageHDU(header=hdr,data=np_data.astype(np.float32))


    # Append the new HDU to the HDU list

    hdul.append(new_hdu)


    # Write output FITS file.

    hdul.writeto(output_fits_file,overwrite=True,checksum=True)


    # Gzip the output FITS file.

    gunzip_cmd = ['gzip', output_fits_file]
    exitcode_from_gunzip = util.execute_command(gunzip_cmd)


    # Upload gzipped file to output S3 bucket.

    s3_client = boto3.client('s3')

    gzipped_output_fits_file = output_fits_file + ".gz"

    s3_object_name = gzipped_output_fits_file

    filenames = [gzipped_output_fits_file]

    objectnames = [s3_object_name]

    util.upload_files_to_s3_bucket(s3_client,bucket_name_output,filenames,objectnames)


    # Clean up work directory.

    rm_cmd = ['rm','-f',subdir_work + "/" + input_fits_file]
    exitcode_from_rm = util.execute_command(rm_cmd)

    rm_cmd = ['rm','-f',subdir_work + "/" + output_fits_file]
    exitcode_from_rm = util.execute_command(rm_cmd)


# Termination.

exit(0)


