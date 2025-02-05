from astropy.io import fits
import numpy as np

import modules.utils.rapid_pipeline_subs as util


# Subs used by the RAPID pipeline related to difference-image processing.


#-------------------------------------------------------------------
# Reformat a Troxel OpenUniverse simulated image FITS file
# so that the image data are contained in the PRIMARY header.
# Assumes the 2D image data are in the second HDU of the FITS file.
# Compute uncertainty image via simple model (photon noise only).
# We do this because the uncertainty image is not available as
# we removed the uncertainy FITS extension earlier from the
# Troxel OpenUniverse simulated images to save on disk-space costs.
# Resize images to 4089x4089 (odd number of pixels on each side).
# Normalize by exposure time.
#
# Inputs are:
# 1. A single gunzipped Troxel OpenUniverse simulated image (as
#    read directly from the S3 bucket where it is stored), and
# 2. SCA gain.
# 3. EXPTIME

def reformat_troxel_fits_file_and_compute_uncertainty_image_via_simple_model(input_filename,sca_gain,clipped_image_mean):


    # Reformat the FITS file so that the image data are contained in the PRIMARY header.
    # Also, compute via a simple model the uncertainty image from the science image,
    # assuming some value for the SCA gain (electrons/ADU), which is unavailable for Roman WFI.

    fname_output = input_filename.replace(".fits","_reformatted.fits")
    fname_output_unc = input_filename.replace(".fits","_reformatted_unc.fits")

    hdul = fits.open(input_filename)
    hdr = hdul[1].header
    data = hdul[1].data

    exptime = hdr["EXPTIME"]
    hdr["BUNIT"] = "DN/s"

    np_data = np.array(data)
    new_row = np.full(np_data.shape[1], clipped_image_mean)
    new_arr = np.append(np_data, [new_row], axis=0)                 # Append extra row of trimmed-average background.
    new_col = np.full((new_arr.shape[0], 1), clipped_image_mean)
    new_np_data = np.append(new_arr, new_col, axis=1)               # Append extra column of trimmed-average background.

    new_np_data_norm = new_np_data / exptime

    hdu = fits.PrimaryHDU(header=hdr,data=new_np_data_norm.astype(np.float32))
    hdu_list = []
    hdu_list.append(hdu)
    hdu = fits.HDUList(hdu_list)
    hdu.writeto(fname_output,overwrite=True,checksum=True)


    # Ensure data are positive for uncertainty calculations.

    pos_np_data_norm = np.abs(new_np_data_norm)
    data_unc = np.sqrt(pos_np_data_norm / sca_gain)

    hdu_unc = fits.PrimaryHDU(header=hdr,data=data_unc.astype(np.float32))
    hdu_list_unc = []
    hdu_list_unc.append(hdu_unc)
    hdu_unc = fits.HDUList(hdu_list_unc)
    hdu_unc.writeto(fname_output_unc,overwrite=True,checksum=True)

    return fname_output,fname_output_unc


#-------------------------------------------------------------------
# Mask input image with input mask.  Values in input image are reset
# to NaN if mask is less than the given threshold at a pixel location,
# which is nominally 0.5 in cov map (converted to float after swarping).
# This method is applied to ZOGY outputs because swarping the
# reference image resets NaNs to zero, which would otherwise
# give bogus positive image values in the difference image.

def mask_difference_image_with_resampled_reference_cov_map(input_filename,mask_filename,output_filename,thresh):

    hdul_input = fits.open(input_filename)
    hdr_input = hdul_input[0].header
    data_input = hdul_input[0].data

    hdul_mask = fits.open(mask_filename)
    data_mask = hdul_mask[0].data

    np_data_input = np.array(data_input)
    np_data_mask = np.array(data_mask)

    np_data_output = np.where(np_data_mask >= thresh,np_data_input,np.nan)

    hdu_list = []
    hdu = fits.PrimaryHDU(header=hdr_input,data=np_data_output)
    hdu_list.append(hdu)
    hdu = fits.HDUList(hdu_list)
    hdu.writeto(output_filename,overwrite=True,checksum=True)

    return


#-------------------------------------------------------------------------------------------------------------
# Compute diffimage_uncertainty for use as weight.fits in computing SExtractor catalog for differenced image.
# Assume science-image photon noise, reference-image photon noise, and difference-image variance contribute.
#-------------------------------------------------------------------------------------------------------------

def compute_diffimage_uncertainty(sca_gain,
                                  science_image_filename,
                                  reference_image_filename,
                                  refiamge_cov_map_filename,
                                  diffimage_filename,
                                  diffimage_unc_filename):

    n_sigma = 3.0
    hdu_num = 0

    avg_dif_img,\
    std_dif_img,\
    cnt_dif_img,\
    gpctdif_dif_img,\
    gmin_dif_img,\
    gmax_dif_img,\
    npixnan_dif_img,\
    npixsat_dif_img = util.fits_data_statistics_with_clipping(diffimage_filename,n_sigma,hdu_num)

    hdul_sci = fits.open(science_image_filename)
    hdr_sci = hdul_sci[0].header
    data_sci = hdul_sci[0].data
    np_data_sci = np.array(data_sci)
    pos_np_data_sci = np.abs(np_data_sci)

    hdul_ref = fits.open(reference_image_filename)
    hdr_ref = hdul_ref[0].header
    data_ref = hdul_ref[0].data
    np_data_ref = np.array(data_ref)
    pos_np_data_ref = np.abs(np_data_ref)

    hdul_cov = fits.open(refiamge_cov_map_filename)
    hdr_cov = hdul_cov[0].header
    data_cov = hdul_cov[0].data
    np_data_cov = np.array(data_cov)
    pos_np_data_cov = np.where(np_data_cov >= 0.5,np_data_cov,np.nan)

    hdu_list_unc = []
    data_unc = np.sqrt(pos_np_data_sci / sca_gain + pos_np_data_ref / (sca_gain * pos_np_data_cov) + std_dif_img * std_dif_img)
    hdu_unc = fits.PrimaryHDU(header=hdr_sci,data=data_unc.astype(np.float32))
    hdu_list_unc.append(hdu_unc)
    hdu_unc = fits.HDUList(hdu_list_unc)
    hdu_unc.writeto(diffimage_unc_filename,overwrite=True,checksum=True)


############################################################################################
# Gain-match science and reference images by generating SExtractor catalogs for each.
# Assumptions:
# 1. Input reference image is resampled into distortion grid science image (or vice versa).
# 2. Distortion given by PV representation.
# 3. Both input images have been locally background-subtracted.
############################################################################################

def gainMatchScienceAndReferenceImages(s3_client,
                                       product_s3_bucket,
                                       jid,
                                       job_proc_date,
                                       filename_sci_image,
                                       filename_sci_uncert,
                                       filename_ref_image,
                                       filename_ref_uncert,
                                       gainmatch_dict,
                                       sextractor_gainmatch_dict):


    # Print diagnostics:

    print(" Sub gainMatchScienceAndReferenceImages: filename_sci_image =",filename_sci_image)
    print(" Sub gainMatchScienceAndReferenceImages: filename_sci_uncert =",filename_sci_uncert)
    print(" Sub gainMatchScienceAndReferenceImages: filename_ref_image =",filename_ref_image)
    print(" Sub gainMatchScienceAndReferenceImages: filename_ref_uncert =",filename_ref_uncert)


    # Initialize inputs.

    iam = "Sub gainMatchScienceAndReferenceImages"
    verbose = int(gainmatch_dict['verbose'])
    upload_intermediate_products = eval(gainmatch_dict['upload_intermediate_products'])

    params_file = "/code/cdf/rapidSexParamsGainMatch.inp"
    filter_conv_file = "/code/cdf/rapidSexGainMatchFilter.conv"
    classifier_nnw_file = "/code/cdf/rapidSexGainMatchStarGalaxyClassifier.nnw"
    params_to_get_vals_scicat = ["XWIN_IMAGE","YWIN_IMAGE","FLUX_APER_6"]
    params_to_get_vals_refcat = ["XWIN_IMAGE","YWIN_IMAGE","FLUX_APER_6","MAG_APER_6",
                                 "CLASS_STAR","ISOAREAF_IMAGE","AWIN_WORLD","BWIN_WORLD"]


    # Thresholds are used to filter input ref-image catalog to
    # support gain-matching with sci-image catalog.

    magrefthresmin = float(gainmatch_dict['magrefthresmin'])
    magrefthresmax = float(gainmatch_dict['magrefthresmax'])

    print(" magrefthresmin =",magrefthresmin)
    print(" magrefthresmax =",magrefthresmax)


    # Keep only filtered ref-catalog sources that have no
    # other ref-catalog source within a box of side length 2*refexclbox
    # pixels. Also re-filter by keeping sources with mag <= refmagkeep
    # and that fall at distance > edgebuffer pixels from any sci image edge.

    refexclbox = float(gainmatch_dict['refexclbox'])
    refmagkeep = float(gainmatch_dict['refmagkeep'])
    edgebuffer = int(gainmatch_dict['edgebuffer'])

    print(" refexclbox =",refexclbox)
    print(" refmagkeep =",refmagkeep)
    print(" edgebuffer =",edgebuffer)


    # Minimum number of filtered-ref to sci catalog matches above which to
    # proceed with flux-ratio'ing to compute relative gain-match factor.

    numsrcgmatchmin = int(gainmatch_dict['numsrcgmatchmin'])

    print(" numsrcgmatchmin =",numsrcgmatchmin)


    # Match radius (pixels) to associate filtered ref-image catalog sources to sci-image
    # catalog for purpose of gain-matching and estimating global RMS errors
    # along X and Y axes to quantify overall registration accuracy.

    radscirefmatch = float(gainmatch_dict['radscirefmatch'])

    print(" radscirefmatch =",radscirefmatch)


    # Other filtering on SExtractor parameters as described in Masci et al.'s iPTF image-differencing paper.

    min_class_star_thresh = float(gainmatch_dict['min_class_star_thresh'])
    max_isoareaf_image_thresh = int(gainmatch_dict['max_isoareaf_image_thresh'])
    max_awin_to_bwin_world_ratio_thresh = float(gainmatch_dict['max_awin_to_bwin_world_ratio_thresh'])

    print(" min_class_star_thresh =",min_class_star_thresh)
    print(" max_isoareaf_image_thresh =",max_isoareaf_image_thresh)
    print(" max_awin_to_bwin_world_ratio_thresh =",max_awin_to_bwin_world_ratio_thresh)


    # Read in keyword values from FITS header science image.

    hdul_sci = fits.open(filename_sci_image)
    hdr_sci = hdul_sci[0].header

    naxis1 = hdr_sci["NAXIS1"]
    naxis2 = hdr_sci["NAXIS2"]








    # TODO: Read MAGZP from FITS header of reference image and science image
    # (FWIW, for the science image, this is ZPTMAG in Troxel OpenUniverse sims.)
    # All the same ZPTMAG for F184 Troxel OpenUniverse sims.
    # For example: imheaders -i Roman_TDS_simple_model_F184_11864_3_lite_reformatted.fits | grep ZP
    # ZPTMAG  =   18.824125825690057

    magzpsci_keyword = "ZPTMAG"

    # Keep magzpref = 0.0 and magzpsci = 0.0 until we implement photometric calibration of reference images.
    magzpsci = 0.0
    magzpref = 0.0







    # Compute SExtractor catalog for science image.

    filename_scigainmatchsexcat_catalog = filename_sci_image.replace(".fits","_scigainmatchsexcat.txt")

    sextractor_gainmatch_dict["sextractor_detection_image".lower()] = "None"
    sextractor_gainmatch_dict["sextractor_input_image".lower()] = filename_sci_image
    sextractor_gainmatch_dict["sextractor_WEIGHT_IMAGE".lower()] = filename_sci_uncert
    sextractor_gainmatch_dict["sextractor_PARAMETERS_NAME".lower()] = params_file
    sextractor_gainmatch_dict["sextractor_FILTER_NAME".lower()] = filter_conv_file
    sextractor_gainmatch_dict["sextractor_STARNNW_NAME".lower()] = classifier_nnw_file
    sextractor_gainmatch_dict["sextractor_CATALOG_NAME".lower()] = filename_scigainmatchsexcat_catalog
    sextractor_cmd = util.build_sextractor_command_line_args(sextractor_gainmatch_dict)
    exitcode_from_sextractor = util.execute_command(sextractor_cmd)


    # Compute SExtractor catalog for reference image.

    filename_refgainmatchsexcat_catalog = filename_ref_image.replace(".fits","_refgainmatchsexcat.txt")

    sextractor_gainmatch_dict["sextractor_detection_image".lower()] = "None"
    sextractor_gainmatch_dict["sextractor_input_image".lower()] = filename_ref_image
    sextractor_gainmatch_dict["sextractor_WEIGHT_IMAGE".lower()] = filename_ref_uncert
    sextractor_gainmatch_dict["sextractor_PARAMETERS_NAME".lower()] = params_file
    sextractor_gainmatch_dict["sextractor_FILTER_NAME".lower()] = filter_conv_file
    sextractor_gainmatch_dict["sextractor_STARNNW_NAME".lower()] = classifier_nnw_file
    sextractor_gainmatch_dict["sextractor_CATALOG_NAME".lower()] = filename_refgainmatchsexcat_catalog
    sextractor_cmd = util.build_sextractor_command_line_args(sextractor_gainmatch_dict)
    exitcode_from_sextractor = util.execute_command(sextractor_cmd)


    # Upload SExtractor catalogs for science and reference images to S3 bucket.

    if upload_intermediate_products:

        scigainmatchsexcat_catalog_s3_bucket_object_name = job_proc_date + "/jid" + str(jid) + "/" +\
                                                           filename_scigainmatchsexcat_catalog

        refgainmatchsexcat_catalog_s3_bucket_object_name = job_proc_date + "/jid" + str(jid) + "/" +\
                                                           filename_refgainmatchsexcat_catalog


        filenames = [filename_scigainmatchsexcat_catalog,
                     filename_refgainmatchsexcat_catalog]

        objectnames = [scigainmatchsexcat_catalog_s3_bucket_object_name,
                       refgainmatchsexcat_catalog_s3_bucket_object_name]

        util.upload_files_to_s3_bucket(s3_client,product_s3_bucket,filenames,objectnames)


    # Parse XWIN_IMAGE,YWIN_IMAGE,FLUX_APER_6 (14-pixel diameter) from SExtractor catalog for science image.

    sci_vals = util.parse_ascii_text_sextrator_catalog(filename_scigainmatchsexcat_catalog,params_file,params_to_get_vals_scicat)


    # Parse XWIN_IMAGE,YWIN_IMAGE,FLUX_APER_6 (14-pixel diameter),CLASS_STAR,ISOAREAF_IMAGE,AWIN_WORLD,BWIN_WORLD
    # from SExtractor catalog for reference image.

    ref_vals = util.parse_ascii_text_sextrator_catalog(filename_refgainmatchsexcat_catalog,params_file,params_to_get_vals_refcat)


    # Convert returned catalog values to 1-D lists.

    num_rows_sci = len(sci_vals)

    sci_x_vals = []
    sci_y_vals = []
    sci_flux_vals = []
    for i in range(num_rows_sci):
        sci_x = float(sci_vals[i][0])
        sci_y = float(sci_vals[i][1])
        sci_flux = float(sci_vals[i][2])
        sci_x_vals.append(sci_x)
        sci_y_vals.append(sci_y)
        sci_flux_vals.append(sci_flux)

    num_rows_ref = len(ref_vals)

    ref_x_vals = []
    ref_y_vals = []
    ref_flux_vals = []
    ref_mag_vals = []
    ref_class_star_vals = []
    ref_isoareaf_image_vals = []
    ref_awin_to_bwin_world_ratio_vals = []
    for i in range(num_rows_ref):
        ref_x = float(ref_vals[i][0])
        ref_y = float(ref_vals[i][1])
        ref_flux = float(ref_vals[i][2])
        ref_mag = float(ref_vals[i][3])
        ref_class_star = float(ref_vals[i][4])
        ref_isoareaf_image = float(ref_vals[i][5])
        ref_awin_world = float(ref_vals[i][6])
        ref_bwin_world = float(ref_vals[i][7])
        ref_x_vals.append(ref_x)
        ref_y_vals.append(ref_y)
        ref_flux_vals.append(ref_flux)
        ref_mag_vals.append(ref_mag)
        ref_class_star_vals.append(ref_class_star)
        ref_isoareaf_image_vals.append(ref_isoareaf_image)
        ref_awin_to_bwin_world_ratio_vals.append(ref_awin_world / ref_bwin_world)

    nrefcat = num_rows_ref

    xsci_val = np.array(ref_x_vals)
    ysci_val = np.array(ref_y_vals)
    magref_val = np.array(ref_mag_vals)
    classstarref_val = np.array(ref_class_star_vals)
    isoareafimageref_val = np.array(ref_isoareaf_image_vals)
    awintobwinworldratioref_val = np.array(ref_awin_to_bwin_world_ratio_vals)

    magref_val += magzpref


    #-----------------------------------------------------------------
    # Filter ref-catalog sources to retain subset of point sources for
    # use in gain-matching below.

    if verbose:
        print("{}: filtering {} ref-image catalog sources to support gain-matching...".format(iam,nrefcat))

    idxref_val = np.where((magref_val >= magrefthresmin) & (magref_val <= magrefthresmax) &\
                          (classstarref_val >= min_class_star_thresh) &\
                          (isoareafimageref_val <= max_isoareaf_image_thresh) &\
                          (awintobwinworldratioref_val <= max_awin_to_bwin_world_ratio_thresh))

    xref_val = xsci_val[idxref_val]
    yref_val = ysci_val[idxref_val]
    magreffilt_val = magref_val[idxref_val]
    fluxref_val = 10**(0.4 * (magzpref - magreffilt_val))
    nrefcat = len(xref_val)

    if verbose:
        s = "{}: number of ref-image catalog sources after filtering to support gain-matching: {}"
        print(s.format(iam,nrefcat))

    if nrefcat == 0:
        s = "{}: === Warning: no ref-image catalog sources remain after filtering to support gain-matching; continuing..."
        print(s.format(iam))


    #-----------------------------------------------------------------
    # For gain-matching, keep only ref-catalog sources that have no
    # other ref-catalog source within a box of side length 2*refexclbox
    # pixels. Also re-filter using brighter mag cutoff and only keep
    # sources at distance > edgebuffer pixels from edges.

    xrefkeep = []
    yrefkeep = []
    magrefkeep = []
    fluxrefkeep = []

    idxkeep_val = np.array(range(nrefcat))

    nrefcatn = 0

    for i in range(nrefcat):

        mi = magreffilt_val[i]
        xi = xref_val[i]
        yi = yref_val[i]

        if (mi <= refmagkeep) and\
            (xi > edgebuffer) and\
            (xi < (naxis1 - edgebuffer - 1)) and\
            (yi > edgebuffer) and\
            (yi < (naxis2 - edgebuffer - 1)):

            fi = fluxref_val[i]

            idxkeepn_val = np.where(idxkeep_val != i)

            xrefn_val = xref_val[idxkeepn_val]
            yrefn_val = yref_val[idxkeepn_val]

            insidebox = np.where((np.abs(xrefn_val - xi) <= refexclbox) &
                                 (np.abs(yrefn_val - yi) <= refexclbox))

            ninsidebox = len(insidebox[0])
            if ninsidebox == 0:
                xrefkeep.append(xi)
                yrefkeep.append(yi)
                magrefkeep.append(mi)
                fluxrefkeep.append(fi)

                nrefcatn += 1


    if nrefcatn > 0:
        xrefkeep_val = np.array(xrefkeep)
        yrefkeep_val = np.array(yrefkeep)
        fluxrefkeep_val = np.array(fluxrefkeep)

    if verbose > 0:
        s = "{}: number of ref-image catalog sources retained following removal " +\
            "of those with close neighbors for gain-matching: {}"
        print(s.format(iam,nrefcatn))


    #-----------------------------------------------------------------
    # Compute gain (throughput) factor between science and reference images,
    # if possible, by cross-matching sources from ref and sci catalogs and
    # returning matched sci-catalog fluxes to compute flux ratio.
    # Default to use global image-based MAGZP values if this cannot be computed.

    scalefac = 10**(0.4*(magzpref - magzpsci))

    if verbose:
        s = "{}: default scale factor for gain-matching sci and ref images based on global image MAGZP values = {}"
        print(s.format(iam,scalefac))


    # Initialize final RMSs of ref to sci source separations along each axis
    # [pixels] in case num sci or ref catalog sources is zero.

    dxrmsfin = 0.05
    dyrmsfin = 0.05

    if num_rows_sci > 0 and nrefcatn > 0:

        dscirefnear, fluxscinearest, nmtchsciref, dxrms, dyrms = SourceMatchRefSci(sci_x_vals,
                                                                                   sci_y_vals,
                                                                                   xrefkeep_val,
                                                                                   yrefkeep_val,
                                                                                   sci_flux_vals,
                                                                                   nrefcatn,
                                                                                   radscirefmatch,
                                                                                   verbose)


        dscirefnear_val = np.array(dscirefnear)
        fluxscinearest_val = np.array(fluxscinearest)

        if nmtchsciref >= numsrcgmatchmin:


            # sci-cat fluxes != -999 are those that match ref-catalog sources.

            idxgood_val = np.where(fluxscinearest_val > 0)

            medradsep = np.median(dscirefnear_val[idxgood_val])

            flxrat_val = fluxrefkeep_val[idxgood_val] / fluxscinearest_val[idxgood_val]

            scalefac = np.median(flxrat_val)

            dxrmsfin = dxrms
            dyrmsfin = dyrms

            if verbose:
                s = "{}: median separation of {} sci to filtered ref-catalog matches = {} pixels; " +\
                     "initial match radius was {} pixels"
                print(s.format(iam,nmtchsciref,medradsep,radscirefmatch))
                print("{}: final scale factor for gain-matching sci and ref images based on flux ratios = {} "\
                    .format(iam,scalefac))
                print("{}: final RMSs along axes to use: dxrms, dyrms = {}, {} pixels"\
                    .format(iam,dxrmsfin,dyrmsfin))


        else:
            s1 = "{}: === Warning: number of matched sci and (filtered) ref catalog sources " +\
                 "(nmtchsciref) is below threshold of {}"
            print(s1.format(iam,numsrcgmatchmin))
            s2 = "{}: === Warning: gain-matching science and reference image pixels using " +\
                 "matched fluxes not possible; using their image-based MAGZP values..."
            print(s2.format(iam))
            s3 = "{}: === Warning: furthermore, assuming default RMSs for axial " +\
                 "separations (~ registration errors) of dxrms, dyrms = {}, {} pixels"
            print(s3.format(iam,dxrmsfin,dyrmsfin))


    else:
        s1 = "{}: === Warning: input number of sci or (filtered) ref catalog sources is zero"
        print(s1.format(iam))
        s2 = "{}: === Warning: gain-matching science and reference image pixels using matched " +\
             "fluxes not possible; using their image-based MAGZP values..."
        print(s2.format(iam))
        s3 = "{}: === Warning: furthermore, assuming default RMSs for axial separations " +\
             "(~ registration errors) of dxrms, dyrms = {}, {} pixels"
        print(s3.format(iam,dxrmsfin,dyrmsfin))


    return scalefac


#---------------------------------------------------------------------
# cross-match sources from ref and sci catalogs and return matched
# sci-catalog fluxes for use in gain-matching. Also return array storing
# radial separations of all matches satisfying radscirefmatch and RMSs
# of separations along each axis.

def SourceMatchRefSci(xf_val,
                      yf_val,
                      xp_val,
                      yp_val,
                      fluxsci_val,
                      nrefcat,
                      radscirefmatch,
                      verbose):

    radsq = radscirefmatch * radscirefmatch
    radaxis = radscirefmatch / np.sqrt(2.0)

    mdnear = []
    mdxnear = []
    mdynear = []
    mfluxsci = []

    nmtch = 0

    x_sci = np.array(xf_val)
    y_sci = np.array(yf_val)
    x_ref = np.array(xp_val)
    y_ref = np.array(yp_val)
    flux_sci = np.array(fluxsci_val)


    # Loop over each input reference-catalog source.

    for i in range(nrefcat):


        # initialize since not guaranteed a sci-cat match at d <= radmatch.

        mdnear.append(-999)
        mfluxsci.append(-999)

        dxi_val = np.where(np.abs(x_ref[i] - x_sci) <= radaxis)

        if len(dxi_val[0]) != 0:

            xfsub_val = x_sci[tuple(dxi_val)]
            yfsub_val = y_sci[tuple(dxi_val)]
            fluxscisub_val = flux_sci[tuple(dxi_val)]

            dx_val = x_ref[i] - xfsub_val
            dy_val = y_ref[i] - yfsub_val
            radsq_val = (dx_val * dx_val) + (dy_val * dy_val)

            idxmin = np.argmin(radsq_val)
            minradsq = radsq_val[idxmin]

            if minradsq <= radsq:

                mdnear[i] = np.sqrt(minradsq)
                mdxnear.append(dx_val[idxmin])
                mdynear.append(dy_val[idxmin])
                mfluxsci[i] = fluxscisub_val[idxmin]

                nmtch += 1


    # compute RMSs of separations along each axis to return below.
    # Note effect of possible bias from radscirefmatch constraint.

    dxrms = 0.0
    dyrms = 0.0

    if nmtch >= 3:
        mdxnear_val = np.array(mdxnear)
        dxrms = np.sqrt(np.mean(mdxnear_val * mdxnear_val))

        mdynear_val = np.array(mdynear)
        dyrms = np.sqrt(np.mean(mdynear_val * mdynear_val))

    if verbose > 0:
        print("iam: SourceMatchRefSci: number of matches = {}".format(nmtch))
        print("iam: SourceMatchRefSci: DxRMS = {} pixels".format(dxrms))
        print("iam: SourceMatchRefSci: DyRMS = {} pixels".format(dyrms))


    return mdnear,mfluxsci,nmtch,dxrms,dyrms
