# Subs used by the RAPID pipeline related to reference images and catalogs.

def generateReferenceImage(s3_client,
                           job_info_s3_bucket,
                           input_images_csv_file_s3_bucket_object_name,
                           input_images_csv_filename,
                           jid,
                           job_proc_date,
                           awaicgen_dict,
                           sca_gain):


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

            hdul = fits.open(fname_input)
            hdr = hdul[1].header
            data = hdul[1].data

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

    return generateReferenceImage_return_list