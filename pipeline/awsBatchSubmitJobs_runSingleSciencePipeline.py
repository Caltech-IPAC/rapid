import os
import csv
import configparser
import re
import boto3
from botocore.exceptions import ClientError
from astropy.io import fits
import numpy as np
from datetime import datetime

import modules.utils.rapid_pipeline_subs as util
import database.modules.utils.rapid_db as db


swname = "awsBatchSubmitJobs_runSingleSciencePipeline.py"
swvers = "1.0"

print("swname =", swname)
print("swvers =", swvers)

aws_batch_job_id = os.getenv('AWS_BATCH_JOB_ID')
print("aws_batch_job_id =", aws_batch_job_id)


# Compute processing datetime (UT) and processing datetime (Pacific time).

datetime_utc_now = datetime.utcnow()
proc_utc_datetime = datetime_utc_now.strftime('%Y-%m-%dT%H:%M:%SZ')
datetime_now = datetime.utcnow()
proc_datetime = datetime_now.strftime('%Y-%m-%dT%H:%M:%S PT')

print("proc_datetime =",proc_datetime)


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


# JOBCONFIGFILENAME of pipeline job.

job_config_ini_file_filename = os.getenv('JOBCONFIGFILENAME')

if job_config_ini_file_filename is None:

    print("*** Error: Env. var. JOBCONFIGFILENAME not set; quitting...")
    exit(64)


# JOBCONFIGOBJNAME of pipeline job.

job_config_ini_file_s3_bucket_object_name = os.getenv('JOBCONFIGOBJNAME')

if job_config_ini_file_s3_bucket_object_name is None:

    print("*** Error: Env. var. JOBCONFIGOBJNAME not set; quitting...")
    exit(64)


# REFIMAGEINPUTSFILENAME of pipeline job.

input_images_csv_filename = os.getenv('REFIMAGEINPUTSFILENAME')

if input_images_csv_filename is None:

    print("*** Error: Env. var. REFIMAGEINPUTSFILENAME not set; quitting...")
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
print("job_config_ini_file_filename =",job_config_ini_file_filename)
print("job_config_ini_file_s3_bucket_object_name =",job_config_ini_file_s3_bucket_object_name)
print("input_images_csv_file_s3_bucket_object_name =",input_images_csv_file_s3_bucket_object_name)


def download_file_from_s3_bucket(s3_client,s3_full_name):

    '''
    Download file from S3 bucket.
    The full name is assumed to be of the following form: s3://sims-sn-f184-lite/1856/Roman_TDS_simple_model_F184_1856_2_lite.fits.gz
    and will be parsed for the s3 bucket name, object name, and filename.
    '''


    # Parse full name.

    string_match = re.match(r"s3://(.+?)/(.+)", s3_full_name)              # TODO

    try:
        s3_bucket_name = string_match.group(1)
        s3_object_name = string_match.group(2)
        print("s3_bucket_name = {}, s3_s3_object_name = {}".\
            format(s3_bucket_name,s3_object_name))

    except:
        print("*** Error: Could not parse s3_full_name; quitting...")
        exit(64)

    string_match2 = re.match(r".+?/(.+)", s3_object_name)                 # TODO

    try:
        filename = string_match2.group(1)
        print("filename = {}".format(filename))

    except:
        print("*** Error: Could not parse s3_object_name; quitting...")
        exit(64)


    # Download reference-image input from associated S3 bucket.

    print("Downloading s3://{}/{} into {}...".format(s3_bucket_name,s3_object_name,filename))

    response = s3_client.download_file(s3_bucket_name,s3_object_name,filename)

    print("response =",response)

    return filename


def upload_files_to_s3_bucket(s3_client,s3_bucket_name,filenames,s3_object_names):

    '''
    Upload list of files to S3 bucket.  Corresponding list of S3 bucket object names must be provided.
    '''

    uploaded_to_bucket = True

    for filename,s3_object_name in zip(filenames,s3_object_names):

        if not os.path.exists(filename):
            print("*** Warning: File does not exist ({}); skipping...".format(filename))
            continue

        try:
            response = s3_client.upload_file(filename,
                                             s3_bucket_name,
                                             s3_object_name)

            print("response =",response)

        except ClientError as e:
            print("*** Error: Failed to upload {} to s3://{}/{}"\
                .format(filename,s3_bucket_name,s3_object_name))
            uploaded_to_bucket = False
            break

        if uploaded_to_bucket:
            print("Successfully uploaded {} to s3://{}/{}"\
                .format(filename,s3_bucket_name,s3_object_name))

    return uploaded_to_bucket


if __name__ == '__main__':


    # Download job configuration data file from S3 bucket.

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
    product_config_filename_base = config_input['DEFAULT']['product_config_filename_base']

    sca_gain = float(config_input['INSTRUMENT']['sca_gain'])

    ppid_sciimage = int(config_input['SCI_IMAGE']['ppid'])

    rid_sciimage = int(config_input['SCI_IMAGE']['rid'])
    sca_sciimage = int(config_input['SCI_IMAGE']['sca'])
    fid_sciimage = int(config_input['SCI_IMAGE']['fid'])
    s3_full_name_science_image = config_input['SCI_IMAGE']['s3_full_name_science_image']
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


    # Download gzipped science image from S3 bucket.

    science_image_filename_gz = download_file_from_s3_bucket(s3_client,s3_full_name_science_image)


    # Upload science image to product S3 bucket (in order to test upload method).

    product_s3_bucket = product_s3_bucket_base
    s3_object_name_science_image = job_proc_date + "/jid" + str(jid) + "/" + science_image_filename_gz

    upload_files_to_s3_bucket(s3_client,product_s3_bucket,[science_image_filename_gz],[s3_object_name_science_image])


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
                # assuming some value for the SCA gain (electrons/ADU), which is unavailable for Roman WFI.

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

                hdu_list_unc = []
                data_unc = np.sqrt(np.array(data) / sca_gain)
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

        f = open(awaicgen_input_uncert_list_file, "w")
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


        # Compute MD5 checksum of reference image.

        print("Computing checksum of ",awaicgen_output_mosaic_image_file)
        checksum = db.compute_checksum(awaicgen_output_mosaic_image_file)

        if checksum == 65 or checksum == 68 or checksum == 66:
            print("*** Error: Unexpected value for checksum =",checksum)


    # Populate config-file dictionary for products.

    product_config_ini_filename = product_config_filename_base + str(jid) + ".ini"
    product_config_ini_file_s3_bucket_object_name = job_proc_date + "/" + product_config_ini_filename

    product_config = configparser.ConfigParser()

    product_config['DEFAULT'] = {'debug': str(debug),
                                 'swname': swname,
                                 'swvers': swvers}

    product_config['DEFAULT']['product_s3_bucket_base'] = product_s3_bucket_base
    product_config['DEFAULT']['jid'] = str(jid)
    product_config['DEFAULT']['job_proc_date'] = job_proc_date
    product_config['DEFAULT']['verbose'] = str(verbose)

    if rfid is None:

        product_config['REF_IMAGE'] = {}

        product_config['REF_IMAGE']['rfid'] = str(rfid)
        product_config['REF_IMAGE']['ppid'] = str(ppid_sciimage)
        product_config['REF_IMAGE']['awaicgen_output_mosaic_image_file_checksum'] = checksum

        mosaic_image_name_for_db_record = "s3://{}/{}".format(product_s3_bucket,awaicgen_output_mosaic_image_s3_bucket_object_name)
        mosaic_cov_map_name_for_db_record = "s3://{}/{}".format(product_s3_bucket,awaicgen_output_mosaic_cov_map_s3_bucket_object_name)
        mosaic_uncert_image_name_for_db_record = "s3://{}/{}".format(product_s3_bucket,awaicgen_output_mosaic_uncert_image_s3_bucket_object_name)

        product_config['REF_IMAGE']['awaicgen_output_mosaic_image_file'] = mosaic_image_name_for_db_record
        product_config['REF_IMAGE']['awaicgen_output_mosaic_cov_map_file'] = mosaic_cov_map_name_for_db_record
        product_config['REF_IMAGE']['awaicgen_output_mosaic_uncert_image_file'] = mosaic_uncert_image_name_for_db_record
        product_config['REF_IMAGE']['awaicgen_output_mosaic_image_status'] = str(1)
        product_config['REF_IMAGE']['awaicgen_output_mosaic_image_infobits'] = str(0)


    # Write product config file for job.

    with open(product_config_ini_filename, 'w') as product_configfile:

        product_configfile.write("#" + "\n")
        product_configfile.write("# s3://" + product_s3_bucket + "/" + product_config_ini_file_s3_bucket_object_name + "\n")
        product_configfile.write("#" + "\n")
        product_configfile.write("# " + proc_utc_datetime + "\n")
        product_configfile.write("#" + "\n")
        product_configfile.write("# Machine-generated by " + swname + "\n")
        product_configfile.write("#" + "\n")
        product_configfile.write("\n")

        product_config.write(product_configfile)


    # Upload producy config file for job, along with associated file(s) if any, to S3 bucket.

    uploaded_to_bucket = True

    try:
        response = s3_client.upload_file(product_config_ini_filename,
                                         product_s3_bucket,
                                         product_config_ini_file_s3_bucket_object_name)
    except ClientError as e:
        print("*** Error: Failed to upload {} to s3://{}/{}"\
            .format(product_config_ini_filename,product_s3_bucket,product_config_ini_file_s3_bucket_object_name))
        uploaded_to_bucket = False

    if uploaded_to_bucket:
        print("Successfully uploaded {} to s3://{}/{}"\
            .format(product_config_ini_filename,product_s3_bucket,product_config_ini_file_s3_bucket_object_name))










# TODO: Need to handle case where rfid is not None (reference image
# already exists and was queried from the database by the launch script).










# Unzip the science image gzipped file.

gunzip_cmd = ['gunzip', science_image_filename_gz]
exitcode_from_gunzip = util.execute_command(gunzip_cmd)

science_image_filename = science_image_filename_gz.replace(".fits.gz",".fits")

hdu_index_for_science_image_data = 1
hdu_index_for_reference_image_data = 0


# Since the reference image was made by awaicgen, there is no geometric image distortion,
# and, hence, no need to convert from sip to pv distortion, so the following flag is set to False.
# Set the following flag to True only for the case where the reference image is a single Roman SCA image.

pv_convert_flag_for_reference_image_data = False                   # TODO


# Swarp the reference image and associated uncertainty image into the distortion frame of the science image.

sci_fits_file_with_pv,\
    ref_fits_file_with_pv,\
    ref_uncert_fits_file_with_pv,\
    output_resampled_reference_image,\
    output_resampled_reference_uncert_image =\
    util.resample_reference_image_to_science_image_with_pv_distortion(science_image_filename,\
                                                                      hdu_index_for_science_image_data,\
                                                                      awaicgen_output_mosaic_image_file,\
                                                                      awaicgen_output_mosaic_uncert_image_file,\
                                                                      hdu_index_for_reference_image_data,\
                                                                      pv_convert_flag_for_reference_image_data,\
                                                                      swarp_dict)


# Upload intermediate FITS files to product S3 bucket for diagnostic purposes.

product_s3_bucket = product_s3_bucket_base
s3_object_name_sci_fits_file_with_pv = job_proc_date + "/jid" + str(jid) + "/" + sci_fits_file_with_pv
s3_object_name_ref_fits_file_with_pv = job_proc_date + "/jid" + str(jid) + "/" + ref_fits_file_with_pv
s3_object_name_output_resampled_reference_image = job_proc_date + "/jid" + str(jid) + "/" + output_resampled_reference_image
s3_object_name_output_resampled_reference_uncert_image = job_proc_date + "/jid" + str(jid) + "/" + output_resampled_reference_uncert_image

filenames = [sci_fits_file_with_pv,
             output_resampled_reference_image,
             output_resampled_reference_uncert_image]

objectnames = [s3_object_name_sci_fits_file_with_pv,
               s3_object_name_output_resampled_reference_image,
               s3_object_name_output_resampled_reference_uncert_image]

if pv_convert_flag_for_reference_image_data:
    filenames.append(ref_fits_file_with_pv)
    objectnames.append(s3_object_name_ref_fits_file_with_pv)

upload_files_to_s3_bucket(s3_client,product_s3_bucket,filenames,objectnames)


# Get listing of working directory as a diagnostic.

ls_cmd = ['ls','-ltr']
exitcode_from_ls = util.execute_command(ls_cmd)


terminating_exitcode = 0

print("terminating_exitcode =",terminating_exitcode)

exit(terminating_exitcode)
