import csv
import re
from botocore.exceptions import ClientError
from astropy.io import fits
from astropy.io import ascii
from astropy.wcs import WCS
from astropy.wcs.utils import proj_plane_pixel_scales
from astropy.table import join
from astropy.coordinates import SkyCoord
import numpy as np
import time

import modules.utils.rapid_pipeline_subs as util
import modules.utils.rapid_data_analysis as rda
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
                           proc_subdir,
                           awaicgen_dict,
                           min_coverage_depth,
                           min_coverage_fraction,
                           sca_gain,
                           sca_readout_noise,
                           product_s3_bucket,
                           upload_to_s3_bucket,
                           inject_fake_sources_flag,
                           fake_sources_dict,
                           rapid_sw,
                           overlapping_fields):


    start_time_benchmark = time.time()

    infobits_refimage = 0         # Placeholder; computed later by evaluate_refimage_quality.


    # Resolved per filter inside the loop over input frames, from the FILTER of the frames
    # themselves; seeded here with the scalar fallback so that it is defined for the return
    # value and the MAGZP keyword even if no frame is ever read.

    zprefimg = float(awaicgen_dict["zprefimg"])

    print("Downloading s3://{}/{} into {}...".format(job_info_s3_bucket,input_images_csv_file_s3_bucket_object_name,input_images_csv_filename))

    response = s3_client.download_file(job_info_s3_bucket,input_images_csv_file_s3_bucket_object_name,input_images_csv_filename)

    print("response =",response)


    # Optionally define injection catalog files and download injection catalogs from S3 bucket.

    if inject_fake_sources_flag:

        file_content = ""
        for overlapping_field in overlapping_fields:
            injection_catalog_filename = f"injection_catalog_rtid{overlapping_field}.json"
            s3_full_name_injection_catalog = f"s3://{job_info_s3_bucket}/injection_catalogs/{injection_catalog_filename}"
            injection_catalog_filename,subdirs,downloaded_from_bucket = util.download_file_from_s3_bucket(s3_client,s3_full_name_injection_catalog)
            print("s3_full_name_injection_catalog = ",s3_full_name_injection_catalog)
            print("injection_catalog_filename = ",injection_catalog_filename)
            if downloaded_from_bucket:
                file_content += f"{injection_catalog_filename}\n"
            else:
                print(f"*** Warning: Injection catalog is missing ({injection_catalog_filename}); omitting...")


        # Write injection-catalog-list file (contains just one row).

        injection_catalog_list_filename = f"injection_catalog_list_refimg.csv"

        with open(injection_catalog_list_filename, 'w') as f:
            f.write(file_content)


    # Loop over reference-image inputs.

    refimage_input_metadata = []
    refimage_input_filenames = []
    refimage_input_filenames_reformatted = []
    refimage_input_filenames_reformatted_unc = []
    refimage_input_filenames_injection_catalog = []

    n = 0
    jdstart = 999999999.0
    jdend = 0.0
    total_exptime = 0.0
    input_zptmags = []

    csv_records = []

    with open(input_images_csv_filename, newline='') as csvfile:

        refimage_inputs_reader = csv.reader(csvfile, delimiter=',')

        for row in refimage_inputs_reader:

            row_as_comma_separated_string = ",".join(row)
            csv_records.append(row_as_comma_separated_string)

            print(row_as_comma_separated_string)

            refimage_input_metadata.append(row)

            refimage_input_s3_full_name = row[11]
            refimage_input_field = row[14]

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

            gunzip_cmd = ['gunzip', '-f', refimage_input_filename]
            exitcode_from_gunzip = util.execute_command(gunzip_cmd)

            refimage_input_filename_gunzipped = refimage_input_filename.replace(".fits.gz",".fits")

            fname_input = refimage_input_filename_gunzipped


            # Optionally inject fake sources into reference-image input.

            if inject_fake_sources_flag:


                # Run fake-source injections code.

                print("fake_sources_dict =",fake_sources_dict)

                refimg_input_ext = fake_sources_dict['refimg_input_ext']
                num_injections = fake_sources_dict['num_injections']
                injection_mag_min = fake_sources_dict['mag_min']
                injection_mag_max = fake_sources_dict['mag_max']

                python_cmd = '/usr/bin/python3.11'
                fake_sources_code = rapid_sw + '/modules/fake_src/rapid_source_injections.py'

                fake_sources_cmd = [python_cmd,
                                    fake_sources_code,
                                    '--sci_ext',
                                    refimg_input_ext,
                                    '--num_injections',
                                    num_injections,
                                    '--mag_min',
                                    injection_mag_min,
                                    '--mag_max',
                                    injection_mag_max,
                                    '--injections_by_field_flag',
                                    '--field_catalogs_input_filename',
                                    injection_catalog_list_filename,
                                    fname_input]

                exitcode_from_fake_sources = util.execute_command(fake_sources_cmd)

                filename_image_with_fake_sources = fname_input.replace(".fits","_inject.fits")
                filename_injection_catalog = fname_input.replace(".fits","_inject.txt")
                fname_input = filename_image_with_fake_sources


            # Reformat the FITS file so that the image data are contained in the PRIMARY header.
            # Normalize by exposure time, and scale the input image data such that the zero point
            # of the reference image will be the value assigned to this frame's filter in the
            # input config file.  The data units of the reference image data will be DN/s.

            fname_output = refimage_input_filename.replace(".fits.gz","_reformatted.fits")
            fname_output_unc = refimage_input_filename.replace(".fits.gz","_reformatted_unc.fits")

            hdul = fits.open(fname_input)
            hdr = hdul[1].header
            data = hdul[1].data

            exptime = hdr["EXPTIME"]
            total_exptime += exptime

            hdr["BUNIT"] = "DN/s"

            data_norm = np.array(data) / exptime


            # The zeropoint the coadd will be built on, for the filter these frames were taken
            # in.  Read from the frame rather than passed in, because the frames are the only
            # thing here that knows the filter, and every frame of a reference image shares it.

            zprefimg = util.get_reference_image_zeropoint(awaicgen_dict,hdr.get("FILTER"))

            zptmag = hdr["ZPTMAG"]
            input_zptmags.append(float(zptmag))
            flux_scale_factor = 10 ** (0.4 * (zprefimg - zptmag))
            print(f"For zptmag = {zptmag}:")
            print(f"    flux_scale_factor = {flux_scale_factor}")
            print(f"This ensures ZPREFIMG = {zprefimg}")

            data_scaled = data_norm * flux_scale_factor

            mjdobs = float(hdr["MJD-OBS"])
            jd = util.convert_mjd_to_jd(mjdobs)
            if jd < jdstart:
                jdstart = jd
            if jd > jdend:
                jdend = jd


            # Write the reformatted FITS file for the input for reference-image stacking.

            hdu = fits.PrimaryHDU(header=hdr,data=data_scaled)
            hdu_list = []
            hdu_list.append(hdu)
            hdu = fits.HDUList(hdu_list)
            hdu.writeto(fname_output,overwrite=True,checksum=True)

            hdul.close()


            # Compute via a simple model the uncertainty image from the
            # reformatted science image, which may or may not contain injected fake sources,
            # assuming some value for the SCA gain (electrons/ADU), which is currently
            # unavailable for Roman WFI.

            hdul = fits.open(fname_output)

            hdr = hdul[0].header
            data = hdul[0].data

            pos_data = np.abs(data_norm) # Ensure data are positive
            data_unc = (np.sqrt(pos_data * exptime / sca_gain + sca_readout_noise ** 2) / exptime) * flux_scale_factor

            hdu_unc = fits.PrimaryHDU(header=hdr,data=data_unc)
            hdu_list_unc = []
            hdu_list_unc.append(hdu_unc)
            hdu_unc = fits.HDUList(hdu_list_unc)
            hdu_unc.writeto(fname_output_unc,overwrite=True,checksum=True)

            hdul.close()


            # Propagate the reference-image inputs to awaicgen and for uploading to S3 bucket.

            if inject_fake_sources_flag:
                refimage_input_filenames_injection_catalog.append(filename_injection_catalog)

            refimage_input_filenames_reformatted.append(fname_output)
            refimage_input_filenames_reformatted_unc.append(fname_output_unc)


            # Delete the original FITS file locally to save disk space.

            rm_cmd = ['rm', '-f', refimage_input_filename_gunzipped]
            exitcode_from_rm = util.execute_command(rm_cmd)

            if inject_fake_sources_flag:
                rm_cmd = ['rm', '-f', filename_image_with_fake_sources]
                exitcode_from_rm = util.execute_command(rm_cmd)


            # Increment refimage-input-file counter.

            n += 1


    n_images_to_coadd = n


    # Write reference-image inputs that were actually used to CSV file.
    # This is needed to populate the PostgreSQL RefImIMages database table.
    # Upload it to the job-info S3 bucket, alongside the original input_images_csv_file.
    # It is needed to populate the PostgreSQL RefImImages database table.

    input_images_used_csv_file = "input_images_used_for_refimage_jid"+ str(jid) + ".csv"
    input_images_used_csv_file_s3_bucket_object_name = proc_subdir + "/" + input_images_used_csv_file

    f = open(input_images_used_csv_file, "w")
    for csv_record in csv_records:
        f.write(csv_record + "\n")
    f.close()

    uploaded_to_bucket = True

    try:
        response = s3_client.upload_file(input_images_used_csv_file,
                                         job_info_s3_bucket,
                                         input_images_used_csv_file_s3_bucket_object_name)

        print("response =",response)

    except ClientError as e:
        print("*** Error: Failed to upload {} to s3://{}/{}"\
            .format(input_images_used_csv_file,job_info_s3_bucket,input_images_used_csv_file_s3_bucket_object_name))
        uploaded_to_bucket = False

    if uploaded_to_bucket:
        print("Successfully uploaded {} to s3://{}/{}"\
            .format(input_images_used_csv_file,job_info_s3_bucket,input_images_used_csv_file_s3_bucket_object_name))


    # Write list of reference-image science input filenames for awaicgen.

    awaicgen_input_images_list_file = awaicgen_dict["awaicgen_input_images_list_file"]

    f = open(awaicgen_input_images_list_file, "w")
    for fname in refimage_input_filenames_reformatted:
        f.write(fname + "\n")
    f.close()


    # Write list of reference-image uncertainty input filenames for awaicgen.

    awaicgen_input_uncert_list_file = awaicgen_dict["awaicgen_input_uncert_list_file"]

    f = open(awaicgen_input_uncert_list_file, "w")
    for fname in refimage_input_filenames_reformatted_unc:
        f.write(fname + "\n")
    f.close()


    # Optionally upload reformatted awaicgen input image and uncertainty files to S3 bucket for off-line analysis.
    # Also upload the reference-image-input fake-source injection catalogs if fake sources were indeed injected.
    # The upload_inputs flag is only to be set to True temporarily as it increases the number of uploaded files.

    upload_inputs = False

    if upload_inputs:

        files_to_upload = refimage_input_filenames_reformatted +\
                          refimage_input_filenames_reformatted_unc +\
                          [awaicgen_input_images_list_file,awaicgen_input_uncert_list_file]

        if inject_fake_sources_flag:

            files_to_upload += refimage_input_filenames_injection_catalog +\
                               [injection_catalog_list_filename]

        for fname in files_to_upload:

            s3_bucket_object_name = proc_subdir + "/jid" + str(jid) + "/refiminputs/" + fname

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
    awaicgen_output_mosaic_image_s3_bucket_object_name = proc_subdir + "/jid" + str(jid) + "/" +\
        awaicgen_dict["awaicgen_output_mosaic_image_file"]
    awaicgen_output_mosaic_cov_map_s3_bucket_object_name = proc_subdir + "/jid" + str(jid) + "/" +\
        awaicgen_dict["awaicgen_output_mosaic_cov_map_file"]
    awaicgen_output_mosaic_uncert_image_s3_bucket_object_name = proc_subdir + "/jid" + str(jid) + "/" +\
        awaicgen_dict["awaicgen_output_mosaic_uncert_image_file"]


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("=====> Elapsed time in seconds just to set up inputs for awaicgen =",
        round(end_time_benchmark - start_time_benchmark,3))
    start_time_benchmark = end_time_benchmark


    # Execute awaicgen to generate reference image.

    awaicgen_cmd = util.build_awaicgen_command_line_args(awaicgen_dict)
    exitcode_from_awaicgen = util.execute_command(awaicgen_cmd)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("=====> Elapsed time in seconds just to execute awaicgen =",
        round(end_time_benchmark - start_time_benchmark,3))
    start_time_benchmark = end_time_benchmark


    # Discard the reference image if its actual coverage is below the target.

    with fits.open(awaicgen_output_mosaic_cov_map_file) as hdul_cov:
        coverage_fraction_actual = coverage_fraction(hdul_cov[0].data,min_coverage_depth)

    print(f"coverage_fraction_actual = {coverage_fraction_actual} (min_coverage_depth = {min_coverage_depth}, " +\
          f"min_coverage_fraction = {min_coverage_fraction})")

    if coverage_fraction_actual < min_coverage_fraction:
        print("*** Warning: Actual coverage fraction is below minimum; reference image discarded; quitting...")
        exit(33)


    # Upload ancillary reference-image products to S3 bucket.  Do not upload the reference image file and
    # reference-image uncertainty file until later, after informational keywords have been added to the FITS header.

    if upload_to_s3_bucket:

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
    generateReferenceImage_return_list.append(jdstart)
    generateReferenceImage_return_list.append(jdend)
    generateReferenceImage_return_list.append(zprefimg)
    generateReferenceImage_return_list.append(total_exptime)
    generateReferenceImage_return_list.append(input_images_used_csv_file_s3_bucket_object_name)
    generateReferenceImage_return_list.append(input_zptmags)
    generateReferenceImage_return_list.append(coverage_fraction_actual)

    return generateReferenceImage_return_list


#####################################################################################
# Generate SExtractor reference-image catalog and upload it to S3 bucket.
#####################################################################################

def generateSExtractorReferenceImageCatalog(s3_client,
                                            product_s3_bucket,
                                            jid,
                                            proc_subdir,
                                            filename_refimage_image,
                                            filename_refimage_uncert,
                                            sextractor_refimage_dict,
                                            upload_to_s3_bucket):


    start_time_benchmark = time.time()


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

    refimage_sextractor_catalog_s3_bucket_object_name = proc_subdir + "/jid" + str(jid) + "/" + filename_refimage_catalog

    if upload_to_s3_bucket:

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


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("=====> Elapsed time in seconds just to generate reference-image SExtractor catalog =",
        round(end_time_benchmark - start_time_benchmark,3))
    start_time_benchmark = end_time_benchmark


    # Return metadata about reference-image SExtractor catalog that was generated.

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
                                      refimage_input_filenames,
                                      jdstart,
                                      jdend,
                                      zprefimg,
                                      total_refimage_exptime,
                                      infobits = None,
                                      status = None,
                                      infobit_cards = None,
                                      coverage_fraction = None,
                                      min_coverage_depth = None,
                                      min_coverage_fraction = None):

    hdu_index = 0

    hdul = fits.open(reference_image_filename)

    hdr = hdul[hdu_index].header

    data = hdul[hdu_index].data

    hdul.close()


    # Add keywords to header.

    hdr["BUNIT"] = "DN/s"
    hdr["FIELD"] = (field,"Roman sky-tile number")
    hdr["FID"] = (fid,"RAPID-OPS-DB filter number")
    hdr["FILTER"] = exposure_filter
    hdr["COV5PERC"] = cov5percent
    hdr["NFRAMES"] = (nframes,"Total number of images coadded")
    hdr["JDSTART"] = (jdstart,"Obs. JD of earliest image used [days]")
    hdr["JDEND"] = (jdend,"Obs. JD of latest image used [days]")
    hdr["MAGZP"] = (zprefimg,"Zero point of reference image [mag]")
    hdr["TOTEXPTM"] = (total_refimage_exptime,"Total input-image exposure time [s]")

    if infobits is not None:
        hdr["INFOBITS"] = (int(infobits),util.format_infobits_comment(infobits))

    if status is not None:
        hdr["STATUS"] = (int(status),"Overall quality: 1 = usable, 0 = bad")

    if infobit_cards is not None:
        for keyword,bit,comment in infobit_cards:
            hdr[keyword] = (bit,comment)

    if coverage_fraction is not None:
        hdr["COVFRAC"] = (coverage_fraction,"Fraction of pixels with coverage >= COVDEPTH")
        hdr["COVDEPTH"] = (min_coverage_depth,"Min. coverage depth for COVFRAC [frames]")
        hdr["COVTARG"] = (min_coverage_fraction,"Required COVFRAC for input selection")


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


#####################################################################################
# Select reference-image inputs by estimated coverage of the full reference-image area.
#####################################################################################

COVERAGE_GRID_STEP = 25
COVERAGE_ESTIMATE_TOLERANCE = 0.05


def make_refimage_wcs(ra0,dec0,naxis1,naxis2,cdelt1,cdelt2,crota2):

    '''
    Make the TAN WCS of a reference image centered on (ra0,dec0), with crpix at the
    image center as in the reference-image launcher.

    Parameters
    ----------
    ra0, dec0 : float
        Sky position of the reference-image center [deg].
    naxis1, naxis2 : int
        Size of the reference image [pixels].
    cdelt1, cdelt2 : float
        Pixel scale [deg/pixel].
    crota2 : float
        Rotation of the reference image [deg].

    Returns
    -------
    w : astropy.wcs.WCS
        WCS of the reference image.
    '''

    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN","DEC--TAN"]
    w.wcs.crval = [ra0,dec0]
    w.wcs.crpix = [0.5 * naxis1 + 0.5,0.5 * naxis2 + 0.5]
    w.wcs.cdelt = [cdelt1,cdelt2]
    w.wcs.crota = [0.0,crota2]

    return w


def coverage_sample_grid(naxis1,naxis2):

    '''
    Zero-based pixel coordinates of the grid that samples a reference image every
    COVERAGE_GRID_STEP pixels.

    Parameters
    ----------
    naxis1, naxis2 : int
        Size of the reference image [pixels].

    Returns
    -------
    x_grid, y_grid : numpy.ndarray
        2-D arrays of x and y pixel coordinates of the grid points.
    '''

    x = np.arange(COVERAGE_GRID_STEP // 2,naxis1,COVERAGE_GRID_STEP)
    y = np.arange(COVERAGE_GRID_STEP // 2,naxis2,COVERAGE_GRID_STEP)
    x_grid,y_grid = np.meshgrid(x,y)

    return x_grid,y_grid


def compute_footprint_mask(w,x_grid,y_grid,ras,decs):

    '''
    Flag the grid points inside the footprint of an input image, given its four corners
    in order around the image.  The corners are projected onto the reference-image
    tangent plane, so footprints that straddle R.A. = 0/360 deg are handled.

    Parameters
    ----------
    w : astropy.wcs.WCS
        WCS of the reference image.
    x_grid, y_grid : numpy.ndarray
        Zero-based pixel coordinates of the grid points.
    ras, decs : sequence of float
        R.A. and Dec. of the four corners of the input image [deg].

    Returns
    -------
    mask : numpy.ndarray of bool
        True for grid points inside the footprint.
    '''

    x,y = w.world_to_pixel(SkyCoord(ra=ras,dec=decs,unit='deg'))

    inside_pos = np.ones(x_grid.shape,dtype=bool)
    inside_neg = np.ones(x_grid.shape,dtype=bool)

    for i in range(4):
        j = (i + 1) % 4
        cross = (x[j] - x[i]) * (y_grid - y[i]) - (y[j] - y[i]) * (x_grid - x[i])
        inside_pos &= cross >= 0.0
        inside_neg &= cross <= 0.0

    return inside_pos | inside_neg


def coverage_fraction(cov_map,min_coverage_depth):

    '''
    Fraction of pixels in a coverage map with depth of coverage at least min_coverage_depth.

    Parameters
    ----------
    cov_map : numpy.ndarray
        Coverage map (number of input images per pixel).
    min_coverage_depth : float
        Minimum depth of coverage for a pixel to count as covered.

    Returns
    -------
    fraction : float
        Fraction of covered pixels.
    '''

    return float(np.count_nonzero(cov_map >= min_coverage_depth)) / cov_map.size


def select_refimage_inputs(w,naxis1,naxis2,candidate_corners,
                           min_coverage_depth,min_coverage_fraction,
                           max_coverage_depth,max_coverage_fraction,max_n_images_to_coadd):

    '''
    Add candidate input images in the given order, skipping any whose footprint misses the
    reference image, until the estimated fraction of the reference image with coverage >=
    max_coverage_depth is at least max_coverage_fraction, or max_n_images_to_coadd are selected;
    then judge the selection against the minimum coverage.

    Parameters
    ----------
    w : astropy.wcs.WCS
        WCS of the reference image.
    naxis1, naxis2 : int
        Size of the reference image [pixels].
    candidate_corners : list of (ras, decs)
        For each candidate input image in order, R.A. and Dec. of its four corners [deg].
    min_coverage_depth : float
        Depth of coverage for a pixel to count toward the minimum [frames].
    min_coverage_fraction : float
        Required fraction of pixels with coverage >= min_coverage_depth.
    max_coverage_depth : float
        Depth of coverage for a pixel to count toward the stopping point [frames].
    max_coverage_fraction : float
        Fraction of pixels with coverage >= max_coverage_depth at which selection stops.
    max_n_images_to_coadd : int
        Maximum number of candidates selected.

    Returns
    -------
    selected_indices : list of int
        Indices into candidate_corners of the selected candidates, in candidate order.
    estimated_fraction : float
        Estimated fraction of pixels with coverage >= min_coverage_depth.
    estimated_fraction_max : float
        Estimated fraction of pixels with coverage >= max_coverage_depth.
    decision : str
        "build" if the estimate clears the minimum by the tolerance, "verify" if it is within
        the tolerance of the minimum (the actual coverage map decides), or "reject" if it is
        below the minimum by more than the tolerance.
    '''

    x_grid,y_grid = coverage_sample_grid(naxis1,naxis2)
    depth = np.zeros(x_grid.shape,dtype=np.int32)

    selected_indices = []
    estimated_fraction_max = 0.0

    for i,(ras,decs) in enumerate(candidate_corners):
        if len(selected_indices) >= max_n_images_to_coadd:
            break

        # Candidates are matched to the reference by sky tile, and edge tiles extend past the
        # reference image, so some candidates miss it entirely; skip them rather than spend the cap.

        mask = compute_footprint_mask(w,x_grid,y_grid,ras,decs)
        if not mask.any():
            continue

        depth += mask
        selected_indices.append(i)
        estimated_fraction_max = coverage_fraction(depth,max_coverage_depth)
        if estimated_fraction_max >= max_coverage_fraction:
            break

    estimated_fraction = coverage_fraction(depth,min_coverage_depth)

    if estimated_fraction >= min_coverage_fraction + COVERAGE_ESTIMATE_TOLERANCE:
        decision = "build"
    elif estimated_fraction >= min_coverage_fraction - COVERAGE_ESTIMATE_TOLERANCE:
        decision = "verify"
    else:
        decision = "reject"

    return selected_indices,estimated_fraction,estimated_fraction_max,decision


#####################################################################################
# Generate PhotUtils reference-image catalog and upload it to S3 bucket.
#####################################################################################

def generatePhotUtilsReferenceImageCatalog(s3_client,
                                           product_s3_bucket,
                                           jid,
                                           proc_subdir,
                                           filename_refimage_image,
                                           filename_refimage_uncert,
                                           filename_refimage_psf,
                                           psfcat_refimage_dict,
                                           upload_to_s3_bucket):


    start_time_benchmark = time.time()


    # Generate PSF-fit catalog for reference image using PhotUtils.

    n_clip_sigma = float(psfcat_refimage_dict["n_clip_sigma"])
    n_thresh_sigma = float(psfcat_refimage_dict["n_thresh_sigma"])

    fwhm = float(psfcat_refimage_dict["fwhm"])
    fit_shape_str = psfcat_refimage_dict["fit_shape"]
    fit_shape = tuple(int(x) for x in fit_shape_str.replace("(","").replace(")","").replace(" ", "").split(','))
    aperture_radius = float(psfcat_refimage_dict["aperture_radius"])

    sharplo = float(psfcat_refimage_dict["sharplo"])
    sharphi = float(psfcat_refimage_dict["sharphi"])
    roundlo = float(psfcat_refimage_dict["roundlo"])
    roundhi = float(psfcat_refimage_dict["roundhi"])
    min_separation = float(psfcat_refimage_dict["min_separation"])

    input_img_filename = filename_refimage_image
    input_unc_filename = filename_refimage_uncert
    input_psf_filename = filename_refimage_psf
    output_psfcat_filename = psfcat_refimage_dict["output_psfcat_filename"]
    output_psfcat_finder_filename = psfcat_refimage_dict["output_psfcat_finder_filename"]
    output_psfcat_residual_filename = psfcat_refimage_dict["output_psfcat_residual_filename"]

    psfcat_flag,phot,psfphot = util.compute_psf_catalog(n_clip_sigma,
                                                        n_thresh_sigma,
                                                        fwhm,
                                                        fit_shape,
                                                        aperture_radius,
                                                        input_img_filename,
                                                        input_unc_filename,
                                                        input_psf_filename,
                                                        output_psfcat_residual_filename,
                                                        sharplo=sharplo,
                                                        sharphi=sharphi,
                                                        roundlo=roundlo,
                                                        roundhi=roundhi,
                                                        min_separation=min_separation)

    print("psfcat_flag =",psfcat_flag)

    checksum_psfcat_filename = None
    checksum_psfcat_finder_filename = None
    refimage_photutils_photometry_catalog_s3_bucket_object_name = None
    refimage_photutils_finder_catalog_s3_bucket_object_name = None
    refimage_photutils_photometry_catalog_uploaded_to_bucket = False
    refimage_photutils_finder_catalog_uploaded_to_bucket = False
    output_psfcat_parquet_filename = None
    nrows = 0

    if not psfcat_flag:

        output_psfcat_filename = None
        output_psfcat_finder_filename = None

    else:


        # Output psf-fit catalog is an PSFPhotometry astropy table with the PSF-fitting results
        # merged with the DAOStarFinder astropy table.
        # Output columns are documentated at
        # https://photutils.readthedocs.io/en/latest/api/photutils.psf.PSFPhotometry.html
        # https://photutils.readthedocs.io/en/stable/api/photutils.detection.DAOStarFinder.html

        try:
            phot['x_init'].info.format = '.4f'
            phot['y_init'].info.format = '.4f'
            phot['flux_init'].info.format = '.6f'
            phot['flux_fit'].info.format = '.6f'
            phot['x_err'].info.format = '.4f'
            phot['y_err'].info.format = '.4f'
            phot['flux_err'].info.format = '.5f'
            phot['qfit'].info.format = '.4f'
            phot['cfit'].info.format = '.4f'

            print(phot[('id', 'x_fit', 'y_fit', 'flux_fit','x_err', 'y_err', 'flux_err', 'n_pixels_fit', 'qfit', 'cfit', 'flags')])


            # Compute sky coordinates for given pixel coordinates.

            ra,dec = util.computeSkyCoordsFromPixelCoords(filename_refimage_image,
                                                          list(phot['x_fit']),
                                                          list(phot['y_fit']))

            phot['x_fit'].info.format = '.4f'
            phot['y_fit'].info.format = '.4f'
            phot.add_column(ra, name='ra')
            phot.add_column(dec, name='dec')


            # Write PSF-fit photometry catalog in astropy table to text file.

            print("output_psfcat_filename = ", output_psfcat_filename)

            ascii.write(phot, output_psfcat_filename, overwrite=True)


            # Write PSF-fit finder catalog in astropy table to text file.

            print("output_psfcat_finder_filename = ", output_psfcat_finder_filename)

            ascii.write(psfphot.finder_results, output_psfcat_finder_filename, overwrite=True)


            # Join photometry and finder objects and output parquet file.

            joined_table_inner = join(phot, psfphot.finder_results, keys='id', join_type='inner')

            nrows = len(joined_table_inner)
            print(f"nrows in PSF-fit catalog = {nrows}\n")

            output_psfcat_parquet_filename = output_psfcat_filename.replace(".txt",".parquet")

            # Convert the QTable to a pandas DataFrame
            df = joined_table_inner.to_pandas()

            # Write the DataFrame to a Parquet file.
            df.to_parquet(output_psfcat_parquet_filename, engine='pyarrow')

        except Exception as e:
            print(f"PSF-fit PSFPhotometry and DAOStarFinder catalogs: An unexpected error occurred: {e}")


        # Upload reference-image photometry catalog to S3 product bucket.

        refimage_photutils_photometry_catalog_s3_bucket_object_name = proc_subdir + "/jid" + str(jid) + "/" + output_psfcat_filename

        if upload_to_s3_bucket:

            uploaded_to_bucket = True

            try:
                response = s3_client.upload_file(output_psfcat_filename,
                                                 product_s3_bucket,
                                                 refimage_photutils_photometry_catalog_s3_bucket_object_name)

                print("response =",response)

            except ClientError as e:
                print("*** Error: Failed to upload {} to s3://{}/{}"\
                    .format(output_psfcat_filename,product_s3_bucket,refimage_photutils_photometry_catalog_s3_bucket_object_name))
                uploaded_to_bucket = False

            if uploaded_to_bucket:
                print("Successfully uploaded {} to s3://{}/{}"\
                    .format(output_psfcat_filename,product_s3_bucket,refimage_photutils_photometry_catalog_s3_bucket_object_name))

            refimage_photutils_photometry_catalog_uploaded_to_bucket = uploaded_to_bucket


        # Upload reference-image finder catalog to S3 product bucket.

        refimage_photutils_finder_catalog_s3_bucket_object_name = proc_subdir + "/jid" + str(jid) + "/" + output_psfcat_finder_filename

        if upload_to_s3_bucket:

            uploaded_to_bucket = True

            try:
                response = s3_client.upload_file(output_psfcat_finder_filename,
                                                 product_s3_bucket,
                                                 refimage_photutils_finder_catalog_s3_bucket_object_name)

                print("response =",response)

            except ClientError as e:
                print("*** Error: Failed to upload {} to s3://{}/{}"\
                    .format(output_psfcat_finder_filename,product_s3_bucket,refimage_photutils_finder_catalog_s3_bucket_object_name))
                uploaded_to_bucket = False

            if uploaded_to_bucket:
                print("Successfully uploaded {} to s3://{}/{}"\
                    .format(output_psfcat_finder_filename,product_s3_bucket,refimage_photutils_finder_catalog_s3_bucket_object_name))

            refimage_photutils_finder_catalog_uploaded_to_bucket = uploaded_to_bucket


        # Upload reference-image-catalog parquet file to S3 product bucket.

        if output_psfcat_parquet_filename is not None:

            refimage_photutils_parquet_catalog_s3_bucket_object_name = proc_subdir + "/jid" + str(jid) + "/" + output_psfcat_parquet_filename

            if upload_to_s3_bucket:

                uploaded_to_bucket = True

                try:
                    response = s3_client.upload_file(output_psfcat_parquet_filename,
                                                     product_s3_bucket,
                                                     refimage_photutils_parquet_catalog_s3_bucket_object_name)

                    print("response =",response)

                except ClientError as e:
                    print("*** Error: Failed to upload {} to s3://{}/{}"\
                        .format(output_psfcat_parquet_filename,product_s3_bucket,refimage_photutils_parquet_catalog_s3_bucket_object_name))
                    uploaded_to_bucket = False

                if uploaded_to_bucket:
                    print("Successfully uploaded {} to s3://{}/{}"\
                        .format(output_psfcat_parquet_filename,product_s3_bucket,refimage_photutils_parquet_catalog_s3_bucket_object_name))


        # Compute MD5 checksum of reference-image PSF-fit photometry catalog.

        print("Computing checksum of reference-image PSF-fit photometry catalog:",output_psfcat_filename)
        checksum_psfcat_filename = db.compute_checksum(output_psfcat_filename)

        if checksum_psfcat_filename == 65 or checksum_psfcat_filename == 68 or checksum_psfcat_filename == 66:
            print("*** Error: Unexpected value for checksum =",checksum_psfcat_filename)


        # Compute MD5 checksum of reference-image PSF-fit finder catalog.

        print("Computing checksum of reference-image PSF-fit finder catalog:",output_psfcat_finder_filename)
        checksum_psfcat_finder_filename = db.compute_checksum(output_psfcat_finder_filename)

        if checksum_psfcat_finder_filename == 65 or checksum_psfcat_finder_filename == 68 or checksum_psfcat_finder_filename == 66:
            print("*** Error: Unexpected value for checksum =",checksum_psfcat_finder_filename)


    # Code-timing benchmark.

    end_time_benchmark = time.time()
    print("=====> Elapsed time in seconds just to generate reference-image PhotUtils catalog =",
        round(end_time_benchmark - start_time_benchmark,3))
    start_time_benchmark = end_time_benchmark


    # Return metadata about reference-image PhotUtils catalog that was generated.

    generateReferenceImageCatalog_return_list = []
    generateReferenceImageCatalog_return_list.append(psfcat_flag)
    generateReferenceImageCatalog_return_list.append(checksum_psfcat_filename)
    generateReferenceImageCatalog_return_list.append(checksum_psfcat_finder_filename)
    generateReferenceImageCatalog_return_list.append(output_psfcat_filename)
    generateReferenceImageCatalog_return_list.append(output_psfcat_finder_filename)
    generateReferenceImageCatalog_return_list.append(refimage_photutils_photometry_catalog_s3_bucket_object_name)
    generateReferenceImageCatalog_return_list.append(refimage_photutils_finder_catalog_s3_bucket_object_name)
    generateReferenceImageCatalog_return_list.append(refimage_photutils_photometry_catalog_uploaded_to_bucket)
    generateReferenceImageCatalog_return_list.append(refimage_photutils_finder_catalog_uploaded_to_bucket)
    generateReferenceImageCatalog_return_list.append(nrows)

    return generateReferenceImageCatalog_return_list


#####################################################################################
# Reference-image InfoBits: processing-summary/QA bit flags, as for ZTF reference
# images.  The bits are defined in cdf/infobitsref.txt, their thresholds and the
# bits that make a reference image bad ("fatal" bits) in the [REFIMAGE_INFOBITS]
# section of the configuration file.  A bit is set when its condition is met; a
# metric that could not be computed sets no bit.
#####################################################################################

# Defaults, used when the configuration file has no [REFIMAGE_INFOBITS] section
# (e.g., a job config written before it existed).  Same values as the ini file.

refimage_infobits_defaults = {"t00": 1.0,
                              "t01": 0.1,
                              "t02": 65.0,
                              "t03": 1.0,
                              "t04": 2.0,
                              "t05": 15.0,
                              "t06": 15.0,
                              "t07": 10,
                              "t08": 10,
                              "t10": 3.5,
                              "fatalinfobits": "2,5,7,8,16",
                              "fwhm_percentile": 90.0,
                              "maglim_snr": 5.0}


def get_refimage_infobits_params(config_input):

    """
    Read the reference-image InfoBits thresholds and fatal bits from the [REFIMAGE_INFOBITS]
    section of the configuration file, with defaults for any that are missing.

    Parameters
    ----------
    config_input : configparser.ConfigParser
        Job configuration.

    Returns
    -------
    params : dict
        Thresholds t00..t08 and t10, "fatalinfobits" (list of int), "fwhm_percentile", and
        "maglim_snr".
    """

    if not config_input.has_section("REFIMAGE_INFOBITS"):
        print("*** Warning: REFIMAGE_INFOBITS section missing from config; using default "
              "reference-image InfoBits thresholds")

    params = {}

    for key,default in refimage_infobits_defaults.items():
        value = config_input.get("REFIMAGE_INFOBITS",key,fallback=str(default))
        if key == "fatalinfobits":
            params[key] = [int(b) for b in value.split(",") if b.strip()]
        elif key in ("t07","t08"):
            params[key] = int(float(value))
        else:
            params[key] = float(value)

    return params


def compute_robust_spread(values):

    """
    Compute the robust spread 0.5 * (p84 - p16) of a set of values, the same statistic as
    the datascale image metric.

    Parameters
    ----------
    values : sequence of float
        Values; non-finite ones are ignored.

    Returns
    -------
    spread : float
        The robust spread, or NaN for fewer than two finite values.
    """

    v = np.asarray(values, dtype=np.float64)
    v = v[np.isfinite(v)]

    if len(v) < 2:
        return np.nan

    p16,p84 = np.percentile(v,[16.0,84.0])

    return 0.5 * (p84 - p16)


def compute_refimage_maglimits(refimage_filename,
                               psf_filename,
                               psfcat_filename,
                               zprefimg,
                               snr = 5.0):

    """
    Compute the two limiting magnitudes of a reference image used by its InfoBits: one from
    the photometric uncertainties of its PSF-fit catalog, and one from the image background
    noise and the PSF (rapid_data_analysis.compute_limiting_magnitude_for_l2_image).

    Parameters
    ----------
    refimage_filename : str
        FITS file of the reference image, in DN/s.
    psf_filename : str
        FITS file of the reference-image PSF.
    psfcat_filename : str or None
        PhotUtils PSF-fit catalog of the reference image (with a flux_err column), or None.
    zprefimg : float
        Zero point of the reference image [AB mag, for flux in DN/s].
    snr : float, optional
        Signal-to-noise ratio of the limit.

    Returns
    -------
    maglimcat : float
        zprefimg - 2.5 log10(snr * median flux_err) over the catalog sources [mag], or NaN.
    maglimit : float
        snr-sigma point-source limiting magnitude from the background noise [mag], or NaN.
    """

    maglimcat = np.nan

    if psfcat_filename is not None:
        try:
            cat = ascii.read(psfcat_filename)
            flux_err = np.asarray(cat["flux_err"], dtype=np.float64)
            flux_err = flux_err[np.isfinite(flux_err) & (flux_err > 0.0)]
            if len(flux_err) > 0:
                maglimcat = float(zprefimg - 2.5 * np.log10(snr * np.median(flux_err)))
        except Exception as e:
            print(f"*** Warning: Could not compute catalog-based limiting magnitude from {psfcat_filename} ({e})")

    maglimit = np.nan

    try:
        limmag = rda.compute_limiting_magnitude_for_l2_image(refimage_filename,
                                                             psf_filename,
                                                             zptmag = zprefimg,
                                                             n_sigma_limit = snr,
                                                             dn_per_sec_factor = 1.0,
                                                             poisson_ratio_warn = None)
        maglimit = float(limmag["maglimit"])
    except Exception as e:
        print(f"*** Warning: Could not compute formula-based limiting magnitude of {refimage_filename} ({e})")

    return maglimcat,maglimit


def compute_refimage_infobits(metrics,params):

    """
    Set the reference-image InfoBits from its QA metrics, and the overall status from the
    fatal bits.

    Parameters
    ----------
    metrics : dict
        "gmedian" and "datascale" [DN/s], "pctbad" [% of covered pixels], "fwhm_med_arcsec" and
        "fwhm_pctl_arcsec" [arcsec], "maglimcat" and "maglimit" [mag], "npsfcat",
        "nsexcat", "zptmag_rms" [mag], and "psfcat_flag" (bool).  A NaN metric sets no bit.
    params : dict
        As returned by get_refimage_infobits_params.

    Returns
    -------
    infobits : int
        Bit flags.
    status : int
        1 if no fatal bit is set (usable), 0 otherwise (bad).
    """

    def above(name,threshold):
        v = metrics.get(name)
        return v is not None and np.isfinite(v) and v > threshold

    def below(name,threshold):
        v = metrics.get(name)
        return v is not None and np.isfinite(v) and v < threshold

    conditions = {0: above("gmedian",params["t00"]),
                  1: above("datascale",params["t01"]),
                  2: above("pctbad",params["t02"]),
                  3: above("fwhm_med_arcsec",params["t03"]),
                  4: above("fwhm_pctl_arcsec",params["t04"]),
                  5: below("maglimcat",params["t05"]),
                  6: below("maglimit",params["t06"]),
                  7: below("npsfcat",params["t07"]),
                  8: below("nsexcat",params["t08"]),
                  10: above("zptmag_rms",params["t10"]),
                  16: not bool(metrics.get("psfcat_flag",True))}

    infobits = 0

    for bit,condition in conditions.items():
        if condition:
            infobits |= 2**bit

    fatal = sum(2**b for b in params["fatalinfobits"])

    status = 0 if (infobits & fatal) else 1

    return infobits,status


def get_refimage_infobit_cards(infobit_definitions_filename,params):

    """
    Read the InfoBit definitions (cdf/infobitsref.txt), substitute the thresholds, and return
    the definitions in use as FITS header cards.

    Parameters
    ----------
    infobit_definitions_filename : str
        cdf/infobitsref.txt, with lines "BITnn   = nn / definition with tnn" (and pXX for
        the FWHM percentile).
    params : dict
        As returned by get_refimage_infobits_params.

    Returns
    -------
    cards : list of (str, int, str)
        (keyword, bit, definition) for each bit not RESERVED.
    """

    formats = {"t00": "{:.1f}", "t01": "{:.2f}", "t02": "{:.1f}", "t03": "{:.2f}",
               "t04": "{:.2f}", "t05": "{:.1f}", "t06": "{:.1f}", "t07": "{:d}",
               "t08": "{:d}", "t10": "{:.2f}"}

    cards = []

    with open(infobit_definitions_filename) as f:
        for line in f:
            if "RESERVED" in line or "=" not in line:
                continue
            keyword,rest = line.split("=",1)
            value,comment = rest.split("/",1)
            comment = comment.strip()
            for key,fmt in formats.items():
                comment = comment.replace(key,fmt.format(params[key]))
            comment = comment.replace("pXX",f"{params['fwhm_percentile']:g}TH")
            cards.append((keyword.strip(),int(value),comment))

    return cards


def evaluate_refimage_quality(config_input,
                              cfg_path,
                              refimage_filename,
                              cov_map_filename,
                              psf_filename,
                              psfcat_filename,
                              psfcat_flag,
                              zprefimg,
                              input_zptmags,
                              stats_refimage,
                              fwhm_mode_pix,
                              fwhm_pctl_pix,
                              nsexcat,
                              npsfcat):

    """
    Compute the QA metrics, InfoBits, and status of a newly generated reference image, and
    the InfoBit definitions for its header.

    Parameters
    ----------
    config_input : configparser.ConfigParser
        Job configuration, with the [REFIMAGE_INFOBITS] section.
    cfg_path : str
        Directory of cdf/infobitsref.txt.
    refimage_filename : str
        FITS file of the reference image, in DN/s.
    cov_map_filename : str
        FITS file of its depth-of-coverage map.  The percentage of bad pixels is taken over the
        pixels with coverage > 0 only, since the mosaic grid is larger than the area its input
        images cover and is NaN outside that area.
    psf_filename : str
        FITS file of the reference-image PSF.
    psfcat_filename : str or None
        PhotUtils PSF-fit catalog of the reference image.
    psfcat_flag : bool
        Whether the PSF-fit catalog could be made.
    zprefimg : float
        Zero point of the reference image [AB mag, for flux in DN/s].
    input_zptmags : sequence of float
        ZPTMAG of the input images.
    stats_refimage : dict
        As returned by util.fits_data_statistics_with_clipping for the reference image.
    fwhm_mode_pix : float
        Mode of FWHM_IMAGE over the point sources in the reference-image SExtractor catalog,
        or its median fallback, as returned by psfFwhmSubs.catalog_fwhm_stats [pixels].
    fwhm_pctl_pix : float
        The fwhm_percentile percentile of FWHM_IMAGE over the same sources [pixels].
    nsexcat, npsfcat : int
        Numbers of sources in the SExtractor and PSF-fit catalogs.

    Returns
    -------
    infobits : int
        Bit flags.
    status : int
        1 if usable, 0 if bad.
    infobit_cards : list of (str, int, str)
        Header cards for the InfoBit definitions in use.
    metrics : dict
        The QA metrics the bits were set from.
    """

    params = get_refimage_infobits_params(config_input)

    with fits.open(refimage_filename) as hdul:
        hdr = hdul[0].header
        image = np.asarray(hdul[0].data)
        pixscale_arcsec = float(np.mean(proj_plane_pixel_scales(WCS(hdr)))) * 3600.0


    # Bad pixels: non-finite pixels inside the covered area, plus saturated pixels.

    with fits.open(cov_map_filename) as hdul:
        covered = np.asarray(hdul[0].data) > 0

    ncovered = int(np.count_nonzero(covered))
    nbad = int(np.count_nonzero(covered & ~np.isfinite(image))) + int(stats_refimage["satcount"])
    del image

    def fwhm_pix_to_arcsec(fwhm_pix):
        fwhm_pix = float(fwhm_pix)
        return fwhm_pix * pixscale_arcsec if np.isfinite(fwhm_pix) and fwhm_pix > 0.0 else np.nan

    maglimcat,maglimit = compute_refimage_maglimits(refimage_filename,
                                                    psf_filename,
                                                    psfcat_filename if psfcat_flag else None,
                                                    zprefimg,
                                                    snr = params["maglim_snr"])

    metrics = {"gmedian": float(stats_refimage["gmed"]),
               "datascale": float(stats_refimage["gsigma"]),
               "pctbad": 100.0 * nbad / ncovered if ncovered > 0 else np.nan,
               "fwhm_med_arcsec": fwhm_pix_to_arcsec(fwhm_mode_pix),
               "fwhm_pctl_arcsec": fwhm_pix_to_arcsec(fwhm_pctl_pix),
               "maglimcat": maglimcat,
               "maglimit": maglimit,
               "npsfcat": int(npsfcat) if psfcat_flag else 0,
               "nsexcat": int(nsexcat),
               "zptmag_rms": compute_robust_spread(input_zptmags),
               "psfcat_flag": bool(psfcat_flag)}

    infobits,status = compute_refimage_infobits(metrics,params)

    infobit_cards = get_refimage_infobit_cards(cfg_path + "/infobitsref.txt",params)

    print("Reference-image QA metrics:",metrics)
    print(f"Reference-image InfoBits = {infobits} (bits {[b for b in range(32) if infobits & 2**b]}), "
          f"status = {status} (fatal bits {params['fatalinfobits']})")

    return infobits,status,infobit_cards,metrics
