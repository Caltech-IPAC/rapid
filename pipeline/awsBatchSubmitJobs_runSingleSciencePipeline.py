import os
import csv
import configparser
import re
import boto3
from botocore.exceptions import ClientError
from astropy.io import fits
import numpy as np

import modules.utils.rapid_pipeline_subs as util


swname = "awsBatchSubmitJobs_runSingleSciencePipeline.py"
swvers = "1.0"

print("swname =", swname)
print("swvers =", swvers)

aws_batch_job_id = os.getenv('AWS_BATCH_JOB_ID')
print("aws_batch_job_id =", aws_batch_job_id)


# JOBPROCDATE of pipeline job.

job_proc_date = os.getenv('JOBPROCDATE')

if job_proc_date is None:

    print("*** Error: Env. var. JOBPROCDATE not set; quitting...")
    exit(64)


# RAPID_JOB_ID of pipeline job.

jid = os.getenv('RAPID_JOB_ID')

if jid is None:

    print("*** Error: Env. var. RAPID_JOB_ID not set; quitting...")
    exit(64)


# JOBS3BUCKET of pipeline job.

job_info_s3_bucket = os.getenv('JOBS3BUCKET')

if job_info_s3_bucket is None:

    print("*** Error: Env. var. JOBS3BUCKET not set; quitting...")
    exit(64)


# JOBCONFIGOBJNAME of pipeline job.

job_config_ini_file_s3_bucket_object_name = os.getenv('JOBCONFIGOBJNAME')

if job_config_ini_file_s3_bucket_object_name is None:

    print("*** Error: Env. var. JOBCONFIGOBJNAME not set; quitting...")
    exit(64)


# REFIMAGEINPUTSOBJNAME of pipeline job.

input_images_csv_file_s3_bucket_object_name = os.getenv('REFIMAGEINPUTSOBJNAME')

if input_images_csv_file_s3_bucket_object_name is None:

    print("*** Error: Env. var. REFIMAGEINPUTSOBJNAME not set; quitting...")
    exit(64)


# Print out basic information for log file.

print("job_proc_date =",job_proc_date)
print("jid =",jid)
print("job_info_s3_bucket =",job_info_s3_bucket)
print("job_config_ini_file_s3_bucket_object_name =",job_config_ini_file_s3_bucket_object_name)
print("input_images_csv_file_s3_bucket_object_name =",input_images_csv_file_s3_bucket_object_name)



if __name__ == '__main__':


    # Download job configuration data file from S3 bucket.

    job_config_ini_file_filename = "job_config_jid" + str(jid) + ".ini"
    input_images_csv_filename = "input_images_for_refimage_jid"+ str(jid) + ".csv"

    s3_client = boto3.client('s3')

    print("Downloading s3://{}/{} into {}...".format(job_info_s3_bucket,job_config_ini_file_s3_bucket_object_name,job_config_ini_file_filename))

    response = s3_client.download_file(job_info_s3_bucket,job_config_ini_file_s3_bucket_object_name,job_config_ini_file_filename)

    print("response =",response)


    # Read in job configuration parameters from .ini file.

    config_input = configparser.ConfigParser()
    config_input.read(job_config_ini_file_filename)

    verbose = int(config_input['DEFAULT']['verbose'])
    debug = int(config_input['DEFAULT']['debug'])
    job_info_s3_bucket_base = config_input['DEFAULT']['job_info_s3_bucket_base']
    product_s3_bucket_base = config_input['DEFAULT']['product_s3_bucket_base']

    ppid_sciimage = int(config_input['SCI_IMAGE']['ppid'])

    rid_sciimage = int(config_input['SCI_IMAGE']['rid'])
    sca_sciimage = int(config_input['SCI_IMAGE']['sca'])
    fid_sciimage = int(config_input['SCI_IMAGE']['fid'])
    s3_bucket_object_name_sciimage = config_input['SCI_IMAGE']['objectname_sciimage']
    expid_sciimage = int(config_input['SCI_IMAGE']['expid'])
    field_sciimage = int(config_input['SCI_IMAGE']['field'])
    mjdobs_sciimage = float(config_input['SCI_IMAGE']['mjdobs'])
    exptime_sciimage = float(config_input['SCI_IMAGE']['exptime'])
    infobits_sciimage = int(config_input['SCI_IMAGE']['infobits'])
    status_sciimage = int(config_input['SCI_IMAGE']['status'])

    ra0_sciimage = float(config_input['SCI_IMAGE']['ra0'])
    dec0_sciimage = float(config_input['SCI_IMAGE']['dec0'])
    ra1_sciimage = float(config_input['SCI_IMAGE']['ra1'])
    dec1_sciimage = float(config_input['SCI_IMAGE']['dec1'])
    ra2_sciimage = float(config_input['SCI_IMAGE']['ra2'])
    dec2_sciimage = float(config_input['SCI_IMAGE']['dec2'])
    ra3_sciimage = float(config_input['SCI_IMAGE']['ra3'])
    dec3_sciimage = float(config_input['SCI_IMAGE']['dec3'])
    ra4_sciimage = float(config_input['SCI_IMAGE']['ra4'])
    dec4_sciimage = float(config_input['SCI_IMAGE']['dec4'])

    sky_tile_rtid = int(config_input['SKY_TILE']['rtid'])
    sky_tile_ra0 = float(config_input['SKY_TILE']['ra0'])
    sky_tile_dec0 = float(config_input['SKY_TILE']['dec0'])
    sky_tile_ra1 = float(config_input['SKY_TILE']['ra1'])
    sky_tile_dec1 = float(config_input['SKY_TILE']['dec1'])
    sky_tile_ra2 = float(config_input['SKY_TILE']['ra2'])
    sky_tile_dec2 = float(config_input['SKY_TILE']['dec2'])
    sky_tile_ra3 = float(config_input['SKY_TILE']['ra3'])
    sky_tile_dec3 = float(config_input['SKY_TILE']['dec3'])
    sky_tile_ra4 = float(config_input['SKY_TILE']['ra4'])
    sky_tile_dec4 = float(config_input['SKY_TILE']['dec4'])

    rfid_str = config_input['REF_IMAGE']['rfid']

    if rfid_str == 'None':
        rfid = None
    else:
        rfid = int(rfid_str)

    ppid_refimage = int(config_input['REF_IMAGE']['ppid'])
    max_n_images_to_coadd = int(config_input['REF_IMAGE']['max_n_images_to_coadd'])
    naxis1_refimage = int(config_input['REF_IMAGE']['naxis1'])
    naxis2_refimage = int(config_input['REF_IMAGE']['naxis2'])
    cdelt1_refimage = float(config_input['REF_IMAGE']['cdelt1'])
    cdelt2_refimage = float(config_input['REF_IMAGE']['cdelt2'])
    crota2_refimage = float(config_input['REF_IMAGE']['crota2'])
    ra0_refimage = float(config_input['REF_IMAGE']['ra0'])
    dec0_refimage = float(config_input['REF_IMAGE']['dec0'])
    ra1_refimage = float(config_input['REF_IMAGE']['ra1'])
    dec1_refimage = float(config_input['REF_IMAGE']['dec1'])
    ra2_refimage = float(config_input['REF_IMAGE']['ra2'])
    dec2_refimage = float(config_input['REF_IMAGE']['dec2'])
    ra3_refimage = float(config_input['REF_IMAGE']['ra3'])
    dec3_refimage = float(config_input['REF_IMAGE']['dec3'])
    ra4_refimage = float(config_input['REF_IMAGE']['ra4'])
    dec4_refimage = float(config_input['REF_IMAGE']['dec4'])

    awaicgen_dict = config_input['AWAICGEN']

    swarp_dict = config_input['SWARP']

    sextractor_dict = config_input['SEXTRACTOR']

    print("max_n_images_to_coadd =", max_n_images_to_coadd)


    # Optionally read in CVS file containing inputs for generating reference image.

    if rfid is None:

        print("Downloading s3://{}/{} into {}...".format(job_info_s3_bucket,input_images_csv_file_s3_bucket_object_name,input_images_csv_filename))

        response = s3_client.download_file(job_info_s3_bucket,input_images_csv_file_s3_bucket_object_name,input_images_csv_filename)

        print("response =",response)

        refimage_input_metadata = []
        refimage_input_filenames = []
        refimage_input_filenames_reformatted = []
        refimage_input_filenames_reformatted_unc = []

        n = 0

        with open(input_images_csv_filename, newline='') as csvfile:

            refimage_inputs_reader = csv.reader(csvfile, delimiter=',')

            for row in refimage_inputs_reader:

                print(', '.join(row))

                refimage_input_metadata.append(row)

                refimage_input_s3_full_name = row[11]                                                   # TODO

                filename_match = re.match(r"s3://(.+?)/(.+)", refimage_input_s3_full_name)              # TODO

                try:
                    refimage_input_s3_bucket_name = filename_match.group(1)
                    refimage_input_s3_object_name = filename_match.group(2)
                    print("s3_bucket_name = {}, s3_object_name = {}".\
                        format(refimage_input_s3_bucket_name,refimage_input_s3_object_name))

                except:
                    print("*** Error: Could not parse refimage_input_s3_full_name; quitting...")
                    exit(64)

                filename_match2 = re.match(r".+?/(.+)", refimage_input_s3_object_name)                 # TODO

                try:
                    refimage_input_filename = filename_match2.group(1)
                    print("refimage_input_filename = {}".format(refimage_input_filename))

                except:
                    print("*** Error: Could not parse refimage_input_s3_object_name; quitting...")
                    exit(64)

                refimage_input_filenames.append(refimage_input_filename)


                # Download reference-image input from associated S3 bucket.

                print("Downloading s3://{}/{} into {}...".format(refimage_input_s3_bucket_name,refimage_input_s3_object_name,refimage_input_filename))

                response = s3_client.download_file(refimage_input_s3_bucket_name,refimage_input_s3_object_name,refimage_input_filename)

                print("response =",response)


                # Unzip the gz file.

                gunzip_cmd = ['gunzip', refimage_input_filename]
                exitcode_from_gunzip = util.execute_command(gunzip_cmd)

                # Reformat the FITS file so that the image data are contained in the PRIMARY header.
                # Also, compute via a simple model the uncertainty image from the science image,
                # assuming some value for the electronics gain (electrons/ADU), which is unavailable for Roman WFI.

                fname_input = refimage_input_filename.replace(".fits.gz",".fits")
                fname_output = refimage_input_filename.replace(".fits.gz","_reformatted.fits")
                fname_output_unc = refimage_input_filename.replace(".fits.gz","_reformatted_unc.fits")

                refimage_input_filenames_reformatted.append(fname_output)
                refimage_input_filenames_reformatted_unc.append(fname_output_unc)

                hdul_1 = fits.open(fname_input)
                hdr = hdul_1[1].header
                data = hdul_1[1].data

                hdu_list = []
                hdu = fits.PrimaryHDU(header=hdr,data=data)
                hdu_list.append(hdu)
                hdu = fits.HDUList(hdu_list)
                hdu.writeto(fname_output,overwrite=True,checksum=True)

                gain = 5.0
                hdu_list_unc = []
                data_unc = np.sqrt(np.array(data) / gain)
                hdu_unc = fits.PrimaryHDU(header=hdr,data=data_unc)
                hdu_list_unc.append(hdu_unc)
                hdu_unc = fits.HDUList(hdu_list_unc)
                hdu_unc.writeto(fname_output_unc,overwrite=True,checksum=True)


                # Delete the original FITS file locally to save disk space.

                rm_cmd = ['rm', '-f', fname_input]
                exitcode_from_rm = util.execute_command(rm_cmd)

                n += 1
                if n >= max_n_images_to_coadd:
                   break


        # Write list of reference-image science input filenames for awaicgen.

        awaicgen_input_images_list_file = awaicgen_dict["awaicgen_input_images_list_file"]

        f = open(awaicgen_input_images_list_file, "w")
        n = 0
        for fname in refimage_input_filenames_reformatted:
            f.write(fname + "\n")
            n += 1
            if n >= max_n_images_to_coadd:
                break
        f.close()


        # Write list of reference-image uncertainty input filenames for awaicgen.

        awaicgen_input_uncert_list_file = awaicgen_dict["awaicgen_input_uncert_list_file"]

        f = open(awaicgen_input_images_list_file, "w")
        n = 0
        for fname in refimage_input_filenames_reformatted_unc:
            f.write(fname + "\n")
            n += 1
            if n >= max_n_images_to_coadd:
                break
        f.close()


        # Set filenames and S3 object names for reference-image products.

        awaicgen_output_mosaic_image_file = awaicgen_dict["awaicgen_output_mosaic_image_file"]
        awaicgen_output_mosaic_cov_map_file = awaicgen_dict["awaicgen_output_mosaic_cov_map_file"]
        awaicgen_output_mosaic_uncert_image_file = awaicgen_dict["awaicgen_output_mosaic_uncert_image_file"]
        product_s3_bucket = product_s3_bucket_base
        awaicgen_output_mosaic_image_s3_bucket_object_name = job_proc_date + "/jid" + str(jid) + "/" +\
            awaicgen_dict["awaicgen_output_mosaic_image_file"]
        awaicgen_output_mosaic_cov_map_s3_bucket_object_name = job_proc_date + "/jid" + str(jid) + "/" +\
            awaicgen_dict["awaicgen_output_mosaic_cov_map_file"]
        awaicgen_output_mosaic_uncert_image_s3_bucket_object_name = job_proc_date + "/jid" + str(jid) + "/" +\
            awaicgen_dict["awaicgen_output_mosaic_uncert_image_file"]

        awaicgen_dict["awaicgen_output_mosaic_image_file"] = awaicgen_output_mosaic_image_file


        # Execute awaicgen to generate reference image.

        awaicgen_cmd = util.build_awaicgen_command_line_args(awaicgen_dict)
        exitcode_from_awaicgen = util.execute_command(awaicgen_cmd)


        # Upload reference-image products to S3 bucket.

        uploaded_to_bucket = True

        try:
            response = s3_client.upload_file(awaicgen_output_mosaic_image_file,
                                             product_s3_bucket,
                                             awaicgen_output_mosaic_image_s3_bucket_object_name)

            print("response =",response)

        except ClientError as e:
            print("*** Error: Failed to upload {} to s3://{}/{}"\
                .format(awaicgen_output_mosaic_image_file,product_s3_bucket,awaicgen_output_mosaic_image_s3_bucket_object_name))
            uploaded_to_bucket = False

        if uploaded_to_bucket:
            print("Successfully uploaded {} to s3://{}/{}"\
                .format(awaicgen_output_mosaic_image_file,product_s3_bucket,awaicgen_output_mosaic_image_s3_bucket_object_name))

        uploaded_to_bucket = True

        try:
            response = s3_client.upload_file(awaicgen_output_mosaic_cov_map_file,
                                             product_s3_bucket,
                                             awaicgen_output_mosaic_cov_map_s3_bucket_object_name)

            print("response =",response)

        except ClientError as e:
            print("*** Error: Failed to upload {} to s3://{}/{}"\
                .format(awaicgen_output_mosaic_cov_map_file,product_s3_bucket,awaicgen_output_mosaic_cov_map_s3_bucket_object_name))
            uploaded_to_bucket = False

        if uploaded_to_bucket:
            print("Successfully uploaded {} to s3://{}/{}"\
                .format(awaicgen_output_mosaic_cov_map_file,product_s3_bucket,awaicgen_output_mosaic_cov_map_s3_bucket_object_name))

        try:
            response = s3_client.upload_file(awaicgen_output_mosaic_uncert_image_file,
                                             product_s3_bucket,
                                             awaicgen_output_mosaic_uncert_image_s3_bucket_object_name)

            print("response =",response)

        except ClientError as e:
            print("*** Error: Failed to upload {} to s3://{}/{}"\
                .format(awaicgen_output_mosaic_uncert_image_file,product_s3_bucket,awaicgen_output_mosaic_uncert_image_s3_bucket_object_name))
            uploaded_to_bucket = False

        if uploaded_to_bucket:
            print("Successfully uploaded {} to s3://{}/{}"\
                .format(awaicgen_output_mosaic_uncert_image_file,product_s3_bucket,awaicgen_output_mosaic_uncert_image_s3_bucket_object_name))


terminating_exitcode = 0

print("terminating_exitcode =",terminating_exitcode)

exit(terminating_exitcode)
