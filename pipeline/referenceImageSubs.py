import csv
import re
import boto3
from botocore.exceptions import ClientError
from astropy.io import fits
import numpy as np

import modules.utils.rapid_pipeline_subs as util
import database.modules.utils.rapid_db as db


# Subs used by the RAPID pipeline related to reference images and catalogs.


#####################################################################################
# Generate reference image and upload it to S3 bucket.
#####################################################################################

def generateReferenceImage(s3_client,
                           job_info_s3_bucket,
                           input_images_csv_file_s3_bucket_object_name,
                           input_images_csv_filename,
                           jid,
                           job_proc_date,
                           awaicgen_dict,
                           max_n_images_to_coadd,
                           sca_gain,
                           product_s3_bucket):


    infobits_refimage = 0                                                             # TODO

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

            if "/" in refimage_input_s3_object_name:

                filename_match2 = re.match(r".+?/(.+)", refimage_input_s3_object_name)                 # TODO

                try:
                    refimage_input_filename = filename_match2.group(1)
                    print("refimage_input_filename = {}".format(refimage_input_filename))

                except:
                    print("*** Error: Could not parse refimage_input_s3_object_name; quitting...")
                    exit(64)

            else:

                filename_match2 = re.match(r"(.+)", refimage_input_s3_object_name)                 # TODO

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
            # Normalize by exposure time.

            fname_input = refimage_input_filename.replace(".fits.gz",".fits")
            fname_output = refimage_input_filename.replace(".fits.gz","_reformatted.fits")
            fname_output_unc = refimage_input_filename.replace(".fits.gz","_reformatted_unc.fits")

            refimage_input_filenames_reformatted.append(fname_output)
            refimage_input_filenames_reformatted_unc.append(fname_output_unc)

            hdul = fits.open(fname_input)
            hdr = hdul[1].header
            data = hdul[1].data

            exptime = hdr["EXPTIME"]
            hdr["BUNIT"] = "DN/s"

            data_norm = data / exptime

            hdu = fits.PrimaryHDU(header=hdr,data=data_norm)
            hdu_list = []
            hdu_list.append(hdu)
            hdu = fits.HDUList(hdu_list)
            hdu.writeto(fname_output,overwrite=True,checksum=True)


            # Ensure data are positive for uncertainty calculations.

            pos_data_norm = np.abs(data_norm)
            data_unc = np.sqrt(pos_data_norm / sca_gain)

            hdu_unc = fits.PrimaryHDU(header=hdr,data=data_unc)
            hdu_list_unc = []
            hdu_list_unc.append(hdu_unc)
            hdu_unc = fits.HDUList(hdu_list_unc)
            hdu_unc.writeto(fname_output_unc,overwrite=True,checksum=True)


            # Delete the original FITS file locally to save disk space.

            rm_cmd = ['rm', '-f', fname_input]
            exitcode_from_rm = util.execute_command(rm_cmd)

            n += 1
            if n >= max_n_images_to_coadd:
               break


    n_images_to_coadd = n


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


    # Optionally upload reformatted awaicgen input image and uncertainty files to S3 bucket for off-line analysis.
    # The upload_inputs flag is only to be set to True temporarily as it increases the number of uploaded files.

    upload_inputs = False

    if upload_inputs:

        files_to_upload = refimage_input_filenames_reformatted +\
                          refimage_input_filenames_reformatted_unc +\
                          [awaicgen_input_images_list_file,awaicgen_input_uncert_list_file]

        for fname in files_to_upload:
            s3_bucket_object_name = job_proc_date + "/jid" + str(jid) + "/refiminputs/" + fname

            uploaded_to_bucket = True

            try:
                response = s3_client.upload_file(fname,
                                                 product_s3_bucket,
                                                 s3_bucket_object_name)

                print("response =",response)

            except ClientError as e:
                print("*** Error: Failed to upload {} to s3://{}/{}"\
                    .format(fname,product_s3_bucket,s3_bucket_object_name))
                uploaded_to_bucket = False

            if uploaded_to_bucket:
                print("Successfully uploaded {} to s3://{}/{}"\
                    .format(fname,product_s3_bucket,s3_bucket_object_name))


    # Set filenames and S3 object names for reference-image products.

    awaicgen_output_mosaic_image_file = awaicgen_dict["awaicgen_output_mosaic_image_file"]
    awaicgen_output_mosaic_cov_map_file = awaicgen_dict["awaicgen_output_mosaic_cov_map_file"]
    awaicgen_output_mosaic_uncert_image_file = awaicgen_dict["awaicgen_output_mosaic_uncert_image_file"]
    awaicgen_output_mosaic_image_s3_bucket_object_name = job_proc_date + "/jid" + str(jid) + "/" +\
        awaicgen_dict["awaicgen_output_mosaic_image_file"]
    awaicgen_output_mosaic_cov_map_s3_bucket_object_name = job_proc_date + "/jid" + str(jid) + "/" +\
        awaicgen_dict["awaicgen_output_mosaic_cov_map_file"]
    awaicgen_output_mosaic_uncert_image_s3_bucket_object_name = job_proc_date + "/jid" + str(jid) + "/" +\
        awaicgen_dict["awaicgen_output_mosaic_uncert_image_file"]


    # Execute awaicgen to generate reference image.

    awaicgen_cmd = util.build_awaicgen_command_line_args(awaicgen_dict)
    exitcode_from_awaicgen = util.execute_command(awaicgen_cmd)


    # Upload ancillary reference-image products to S3 bucket.  Do not upload the reference image file and
    # reference-image uncertainty file until later, after informational keywords have been added to the FITS header.

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


    # Compute MD5 checksum of reference image.

    print("Computing checksum of ",awaicgen_output_mosaic_image_file)
    checksum_refimage = db.compute_checksum(awaicgen_output_mosaic_image_file)

    if checksum_refimage == 65 or checksum_refimage == 68 or checksum_refimage == 66:
        print("*** Error: Unexpected value for checksum =",checksum_refimage)


    # Return metadata about reference image that was generated.

    generateReferenceImage_return_list = []

    generateReferenceImage_return_list.append(infobits_refimage)
    generateReferenceImage_return_list.append(checksum_refimage)
    generateReferenceImage_return_list.append(awaicgen_output_mosaic_image_file)
    generateReferenceImage_return_list.append(awaicgen_output_mosaic_cov_map_file)
    generateReferenceImage_return_list.append(awaicgen_output_mosaic_uncert_image_file)
    generateReferenceImage_return_list.append(awaicgen_output_mosaic_image_s3_bucket_object_name)
    generateReferenceImage_return_list.append(awaicgen_output_mosaic_cov_map_s3_bucket_object_name)
    generateReferenceImage_return_list.append(awaicgen_output_mosaic_uncert_image_s3_bucket_object_name)
    generateReferenceImage_return_list.append(n_images_to_coadd)
    generateReferenceImage_return_list.append(refimage_input_filenames)

    return generateReferenceImage_return_list


#####################################################################################
# Generate reference-image catalog and upload it to S3 bucket.
#####################################################################################

def generateReferenceImageCatalog(s3_client,
                                  product_s3_bucket,
                                  jid,
                                  job_proc_date,
                                  filename_refimage_image,
                                  filename_refimage_uncert,
                                  sextractor_refimage_dict):


    # Compute SExtractor catalog for reference image.

    filename_refimage_catalog = filename_refimage_image.replace("image.fits","refimsexcat.txt")

    sextractor_refimage_dict["sextractor_detection_image".lower()] = "None"
    sextractor_refimage_dict["sextractor_input_image".lower()] = filename_refimage_image
    sextractor_refimage_dict["sextractor_WEIGHT_IMAGE".lower()] = filename_refimage_uncert
    sextractor_refimage_dict["sextractor_PARAMETERS_NAME".lower()] = "/code/cdf/rapidSexParamsRefImage.inp"
    sextractor_refimage_dict["sextractor_FILTER_NAME".lower()] = "/code/cdf/rapidSexRefImageFilter.conv"
    sextractor_refimage_dict["sextractor_STARNNW_NAME".lower()] = "/code/cdf/rapidSexRefImageStarGalaxyClassifier.nnw"
    sextractor_refimage_dict["sextractor_CATALOG_NAME".lower()] = filename_refimage_catalog
    sextractor_cmd = util.build_sextractor_command_line_args(sextractor_refimage_dict)
    exitcode_from_sextractor = util.execute_command(sextractor_cmd)


    # Upload reference-image catalog to S3 product bucket.

    refimage_sextractor_catalog_s3_bucket_object_name = job_proc_date + "/jid" + str(jid) + "/" + filename_refimage_catalog

    uploaded_to_bucket = True

    try:
        response = s3_client.upload_file(filename_refimage_catalog,
                                         product_s3_bucket,
                                         refimage_sextractor_catalog_s3_bucket_object_name)

        print("response =",response)

    except ClientError as e:
        print("*** Error: Failed to upload {} to s3://{}/{}"\
            .format(filename_refimage_catalog,product_s3_bucket,refimage_sextractor_catalog_s3_bucket_object_name))
        uploaded_to_bucket = False

    if uploaded_to_bucket:
        print("Successfully uploaded {} to s3://{}/{}"\
            .format(filename_refimage_catalog,product_s3_bucket,refimage_sextractor_catalog_s3_bucket_object_name))


    # Compute MD5 checksum of reference-image catalog.

    print("Computing checksum of reference-image catalog:",filename_refimage_catalog)
    checksum_refimage_catalog = db.compute_checksum(filename_refimage_catalog)

    if checksum_refimage_catalog == 65 or checksum_refimage_catalog == 68 or checksum_refimage_catalog == 66:
        print("*** Error: Unexpected value for checksum =",checksum_refimage_catalog)


    # Return metadata about reference-image catalog that was generated.

    generateReferenceImageCatalog_return_list = []
    generateReferenceImageCatalog_return_list.append(checksum_refimage_catalog)
    generateReferenceImageCatalog_return_list.append(filename_refimage_catalog)
    generateReferenceImageCatalog_return_list.append(refimage_sextractor_catalog_s3_bucket_object_name)

    return generateReferenceImageCatalog_return_list


#####################################################################################
# Add informational FITS keywords to reference-image header.
#####################################################################################

def addKeywordsToReferenceImageHeader(reference_image_filename,
                                      field,
                                      fid,
                                      exposure_filter,
                                      cov5percent,
                                      nframes,
                                      refimage_input_filenames):

    hdu_index = 0

    hdul = fits.open(reference_image_filename)

    hdr = hdul[hdu_index].header

    data = hdul[hdu_index].data

    hdul.close()


    # Add keywords to header.

    hdr["BUNIT"] = "DN/s"
    hdr["FIELD"] = str(field)
    hdr["FID"] = str(fid)
    hdr["FILTER"] = exposure_filter
    hdr["COV5PERC"] = str(cov5percent)
    hdr["NFRAMES"] = str(nframes)


    # Add keywords for reference-image input filenames.

    i = 0
    for fn in refimage_input_filenames:
        i = i + 1
        zero_padding = ""
        if i < 10:
            zero_padding = "00"
        elif i < 100:
            zero_padding = "0"
        kw = "INFIL" + zero_padding + str(i)
        hdr[kw] = fn


    # Rewrite reference-image FITS file.

    np_data = np.array(data)
    new_hdu = fits.PrimaryHDU(header=hdr,data=np_data.astype(np.float32))
    new_hdu.writeto(reference_image_filename,overwrite=True,checksum=True)

    return


#####################################################################################
# Compute cov5percent from the reference-image coverage map.
# cov5percent is an absolute quality-assurance metric for reference images equal to a
# percentage of the sum of the limited coverage of all pixels in an image, where the
# limited coverage is all coverage and any coverage greater than 5 that is reset to 5
# for scoring purposes, relative to 5 times the total number of pixels in the image.
#####################################################################################

def compute_cov5percent(reference_cov_map_filename):


    # Read reference-image coverage map.

    hdu_index = 0
    hdul = fits.open(reference_cov_map_filename)
    hdr = hdul[hdu_index].header
    data = hdul[hdu_index].data
    hdul.close()


    # Compute cov5percent from reference-image coverage map.

    pixel_coverage_limit = 5
    a = np.array(data)
    image_size = np.shape(a)
    pixcount = image_size[0] * image_size[1]
    b = np.where(a > pixel_coverage_limit,pixel_coverage_limit,a)
    c = np.sum(b)
    cov5percent = c / (pixel_coverage_limit * pixcount) * 100

    print("pixel_coverage_limit =",pixel_coverage_limit)
    print("c =",c)
    print("pixcount =",pixcount)
    print("cov5percent =",cov5percent)


    # Return cov5percent to be propagated to reference-image FITS header
    # and operations database.

    return cov5percent
