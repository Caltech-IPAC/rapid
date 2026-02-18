import csv
import re
import boto3
from botocore.exceptions import ClientError
from astropy.io import fits
from astropy.io import ascii
from astropy.table import QTable, join
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
                           sca_readout_noise,
                           product_s3_bucket,
                           upload_to_s3_bucket):


    infobits_refimage = 0                                                             # TODO

    print("Downloading s3://{}/{} into {}...".format(job_info_s3_bucket,input_images_csv_file_s3_bucket_object_name,input_images_csv_filename))

    response = s3_client.download_file(job_info_s3_bucket,input_images_csv_file_s3_bucket_object_name,input_images_csv_filename)

    print("response =",response)

    refimage_input_metadata = []
    refimage_input_filenames = []
    refimage_input_filenames_reformatted = []
    refimage_input_filenames_reformatted_unc = []

    n = 0
    jdstart = 999999999.0
    jdend = 0.0

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

            gunzip_cmd = ['gunzip', '-f', refimage_input_filename]
            exitcode_from_gunzip = util.execute_command(gunzip_cmd)


            # Reformat the FITS file so that the image data are contained in the PRIMARY header.
            # Also, compute via a simple model the uncertainty image from the science image,
            # assuming some value for the SCA gain (electrons/ADU), which is unavailable for Roman WFI.
            # Normalize by exposure time, and scale the input image data such that the zero point
            # of the reference image will always be a fixed value, as assigned in the input config file.
            # The data units of the reference image data will be DN/s.

            zprefimg = float(awaicgen_dict["zprefimg"])

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

            data_norm = np.array(data) / exptime

            zptmag = hdr["ZPTMAG"]
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


            # Ensure data are positive for uncertainty calculations.

            pos_data = np.abs(np.array(data) * flux_scale_factor)
            data_unc = np.sqrt(pos_data / sca_gain + sca_readout_noise ** 2) / exptime

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

    return generateReferenceImage_return_list


#####################################################################################
# Generate SExtractor reference-image catalog and upload it to S3 bucket.
#####################################################################################

def generateSExtractorReferenceImageCatalog(s3_client,
                                            product_s3_bucket,
                                            jid,
                                            job_proc_date,
                                            filename_refimage_image,
                                            filename_refimage_uncert,
                                            sextractor_refimage_dict,
                                            upload_to_s3_bucket):


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
                                      zprefimg):

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
# Generate PhotUtils reference-image catalog and upload it to S3 bucket.
#####################################################################################

def generatePhotUtilsReferenceImageCatalog(s3_client,
                                           product_s3_bucket,
                                           jid,
                                           job_proc_date,
                                           filename_refimage_image,
                                           filename_refimage_uncert,
                                           filename_refimage_psf,
                                           psfcat_refimage_dict,
                                           upload_to_s3_bucket):


    # Generate PSF-fit catalog for reference image using PhotUtils.

    n_clip_sigma = float(psfcat_refimage_dict["n_clip_sigma"])
    n_thresh_sigma = float(psfcat_refimage_dict["n_thresh_sigma"])

    fwhm = float(psfcat_refimage_dict["fwhm"])
    fit_shape_str = psfcat_refimage_dict["fit_shape"]
    fit_shape = tuple(int(x) for x in fit_shape_str.replace("(","").replace(")","").replace(" ", "").split(','))
    aperture_radius = float(psfcat_refimage_dict["aperture_radius"])

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
                                                        output_psfcat_residual_filename)

    print("psfcat_flag =",psfcat_flag)

    if not psfcat_flag:

        checksum_psfcat_filename = None
        checksum_psfcat_finder_filename = None
        output_psfcat_filename = None
        output_psfcat_finder_filename = None
        refimage_photutils_photometry_catalog_s3_bucket_object_name = None
        refimage_photutils_finder_catalog_s3_bucket_object_name = None

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

            print(phot[('id', 'x_fit', 'y_fit', 'flux_fit','x_err', 'y_err', 'flux_err', 'npixfit', 'qfit', 'cfit', 'flags')])


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

        refimage_photutils_photometry_catalog_s3_bucket_object_name = job_proc_date + "/jid" + str(jid) + "/" + output_psfcat_filename

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

        refimage_photutils_finder_catalog_s3_bucket_object_name = job_proc_date + "/jid" + str(jid) + "/" + output_psfcat_finder_filename

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

        refimage_photutils_parquet_catalog_s3_bucket_object_name = job_proc_date + "/jid" + str(jid) + "/" + output_psfcat_parquet_filename

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

    return generateReferenceImageCatalog_return_list
