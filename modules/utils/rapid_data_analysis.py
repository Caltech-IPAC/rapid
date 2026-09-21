"""
RAPID Data Analysis

Methods for characterizing RAPID images, currently the computation of the point-source
limiting magnitude of a Roman WFI L2 image stored as a FITS file.

Data units
----------
Roman WFI L2 FITS images hold their data in DN (accumulated counts over EXPTIME seconds).  The
ZPTMAG header keyword is the AB magnitude zeropoint for flux in DN/s, not for flux in DN, so a
quantity measured off the pixels is divided by EXPTIME before ZPTMAG is applied to it:

    flux [DN/s] = flux [DN] / EXPTIME [s]
    mag [AB]    = -2.5 * log10(flux [DN/s]) + ZPTMAG

Images the pipeline has already normalized to a rate carry BUNIT = "DN/s" and need no division;
get_dn_per_sec_factor decides which of the two cases applies.  Every method below states the
units of its arguments and of its return values, and the units of a returned quantity are the
units of whatever was passed in unless the docstring says otherwise.
"""

import math
import numpy as np
import numpy.ma as ma
from astropy.io import fits
from astropy.stats import sigma_clip, sigma_clipped_stats


# Data units that mean accumulated counts, which have to be divided by the exposure time,
# and data units that already mean counts per second.

dn_bunits = ["DN", "ELECTRONS", "ELECTRON", "E-", "COUNTS"]

dn_per_sec_bunits = ["DN/S", "DN / S", "DN S-1", "DN/SEC", "DN / SEC", "ELECTRONS/S", "E-/S"]


#####################################################################################################
# Find the index of the first HDU that holds a two-dimensional image.
#
# RAPID FITS images come in both flavors: the OpenUniverse and RIMTIMSIM conversions put the
# image data in the primary HDU, whereas the SOC-simulation conversion writes an empty primary
# HDU followed by an image extension.
#####################################################################################################

def get_image_hdu_index(hdul):

    """
    Method get_image_hdu_index

    Inputs:
    hdul                    Open astropy HDU list.

    Returns:
    hdu_index               Index of the first HDU containing two-dimensional image data.
    """

    for i,hdu in enumerate(hdul):

        if hdu.data is not None and np.ndim(hdu.data) == 2:
            return i

    raise ValueError("Method get_image_hdu_index: no HDU with two-dimensional image data was found")


#####################################################################################################
# Read the FITS-header quantities of a Roman WFI L2 image that bear on the limiting-magnitude
# calculation, or that are useful for bookkeeping alongside it.
#####################################################################################################

def get_l2_image_metadata(input_img_filename,hdu_index = None):

    """
    Method get_l2_image_metadata

    Inputs:
    input_img_filename      FITS file containing the Roman WFI L2 image.
    hdu_index               HDU index of the image data.  If None, the first HDU holding
                            two-dimensional image data is used.

    Returns:
    Dictionary with the following entries, any of which is None when the corresponding FITS
    keyword is absent.  Keywords are looked up first in the header of the image HDU and then
    in the primary header.

    hduindex                Index of the HDU the image data were found in (dimensionless).
    zptmag                  ZPTMAG, the AB magnitude zeropoint for flux in DN/s [AB mag].
    bunit                   BUNIT, the data units, as a string; "DN" for an L2 image.
    exptime                 EXPTIME, the exposure time [s].
    filter                  FILTER, the filter name, as a string.
    sca                     SCA_NUM, the sensor-chip-assembly number (dimensionless).
    mjdobs                  MJD-OBS, the observation time [days].
    dateobs                 DATE-OBS, the observation date and time, as a string.
    skymean                 SKY_MEAN, the mean sky level, where present [DN, not DN/s].
    naxis1, naxis2          Image dimensions [pixels].
    pixscale                Pixel scale computed from the CD matrix [arcsec/pixel].
    """

    hdul = fits.open(input_img_filename)

    if hdu_index is None:
        hdu_index = get_image_hdu_index(hdul)

    hdr = hdul[hdu_index].header
    hdr_primary = hdul[0].header

    def get_keyword(key):

        if key in hdr:
            return hdr[key]

        if key in hdr_primary:
            return hdr_primary[key]

        return None

    metadata = {}

    metadata["hduindex"] = hdu_index
    metadata["zptmag"] = get_keyword("ZPTMAG")
    metadata["bunit"] = get_keyword("BUNIT")
    metadata["exptime"] = get_keyword("EXPTIME")
    metadata["filter"] = get_keyword("FILTER")
    metadata["sca"] = get_keyword("SCA_NUM")
    metadata["mjdobs"] = get_keyword("MJD-OBS")
    metadata["dateobs"] = get_keyword("DATE-OBS")
    metadata["skymean"] = get_keyword("SKY_MEAN")
    metadata["naxis1"] = get_keyword("NAXIS1")
    metadata["naxis2"] = get_keyword("NAXIS2")


    # Compute the pixel scale, in arcsec, from the determinant of the CD matrix.

    cd1_1 = get_keyword("CD1_1")
    cd1_2 = get_keyword("CD1_2")
    cd2_1 = get_keyword("CD2_1")
    cd2_2 = get_keyword("CD2_2")

    if None in (cd1_1,cd1_2,cd2_1,cd2_2):
        metadata["pixscale"] = None
    else:
        metadata["pixscale"] = 3600.0 * math.sqrt(abs(cd1_1 * cd2_2 - cd1_2 * cd2_1))

    hdul.close()

    return metadata


#####################################################################################################
# Determine the factor that converts the pixel values of an image into DN/s, which is the unit
# ZPTMAG is defined for.
#
# L2 image data are in DN, so the factor is normally 1 / EXPTIME.  The SOC-simulation conversion
# (sims/src/socsims/convert_socsims.py) makes this explicit: it derives ZPTMAG for flux in DN/s
# from meta.photometry and then multiplies the rate image by the exposure time and sets
# BUNIT = "DN".  Applying ZPTMAG to a DN background without dividing by the exposure time would
# report a limiting magnitude 2.5 * log10(EXPTIME) too bright, which is 4.6 mag for a 66-second
# exposure.
#
# Images that the pipeline has already normalized to a rate are recognized by their BUNIT and
# left alone; reformat_simdata_fits_file_and_compute_uncertainty_image_via_simple_model in
# pipeline/differenceImageSubs.py divides by EXPTIME and stamps BUNIT = "DN/s".
#####################################################################################################

def get_dn_per_sec_factor(bunit,exptime):

    """
    Method get_dn_per_sec_factor

    Inputs:
    bunit                   Value of the BUNIT FITS keyword, as a string, or None if absent.
    exptime                 Value of the EXPTIME FITS keyword [s], or None if absent.

    Returns:
    dn_per_sec_factor       Factor to multiply pixel values by to obtain DN/s.  This is
                            1 / exptime [1/s] for the DN data of an L2 image, and a dimensionless
                            1.0 only when BUNIT says the data have already been normalized to a
                            rate.
    """

    if bunit is not None and str(bunit).strip().upper() in dn_per_sec_bunits:
        return 1.0

    if bunit is None:
        print("*** Warning: BUNIT is absent; assuming the image data are in DN, as L2 images are")
    elif str(bunit).strip().upper() not in dn_bunits:
        print(f"*** Warning: unrecognized BUNIT = {bunit}; assuming the image data are in DN")

    if exptime is None or exptime <= 0.0:
        raise ValueError(f"Method get_dn_per_sec_factor: the image data are in DN, but "
                         f"EXPTIME = {exptime} is unusable, so they cannot be converted to the "
                         "DN/s that ZPTMAG applies to")

    return 1.0 / float(exptime)


#####################################################################################################
# Compute the number of noise pixels of a PSF model.
#
# The number of noise pixels is the effective area, in pixels, over which the background
# noise is integrated when a point source is measured with optimal (PSF-weighted) photometry:
#
#     N_p = (sum_i p_i)^2 / sum_i p_i^2
#
# which reduces to 1 / sum_i p_i^2 for a unit-normalized PSF.  The corresponding uncertainty
# in the fitted flux of a background-limited point source is sigma_bkg * sqrt(N_p).
#
# The PSF model must be sampled at the pixel scale of the image.  The RAPID PSF library
# (WFI_SCAnn_Fnnn_PSF_DET_DIST.fits) satisfies this, as its headers record OVERSAMP = 1.
#####################################################################################################

def compute_psf_noise_pixels(psf_data):

    """
    Method compute_psf_noise_pixels

    Inputs:
    psf_data                Two-dimensional PSF model image (numpy array), in arbitrary units.
                            Need not be normalized; the calculation is invariant to the overall
                            scaling of the PSF.

    Returns:
    noise_pixels            Number of noise pixels, N_p [pixels].
    psf_sum                 Sum of the PSF-model pixel values, as given, in the units of psf_data
                            (useful for checking that a PSF that is supposed to be unit-normalized
                            actually is).
    """

    p = np.array(psf_data, dtype=np.float64)
    p = np.where(np.isfinite(p), p, 0.0)

    psf_sum = np.sum(p)
    psf_sum_sq = np.sum(p * p)

    if psf_sum <= 0.0 or psf_sum_sq <= 0.0:
        raise ValueError("Method compute_psf_noise_pixels: PSF model has non-positive sum "
                         f"(psf_sum = {psf_sum}, psf_sum_sq = {psf_sum_sq})")

    noise_pixels = (psf_sum * psf_sum) / psf_sum_sq

    return noise_pixels,psf_sum


#####################################################################################################
# Compute the factor by which iterative n-sigma clipping under-estimates the standard deviation
# of Gaussian noise, so that the clipped sigma can be reinflated to the true sigma.
#
# Iterative clipping converges to a fixed point at which the surviving pixels are the standard
# normal truncated at +/- n_sigma * c, where c is the ratio of the clipped sigma to the true
# sigma.  That ratio therefore satisfies
#
#     c^2 = 1 - 2 t phi(t) / (2 Phi(t) - 1),    t = n_sigma * c
#
# which is solved here by fixed-point iteration.  The correction is 1.0154 at 3 sigma and is
# numerically inert beyond about 5 sigma.
#
# This is the analytic counterpart, for iterative clipping, of the Monte Carlo factor computed
# by rapid_pipeline_subs.compute_clip_corr for the single-pass percentile-based clipping done in
# rapid_pipeline_subs.fits_data_statistics_with_clipping.  Note that compute_clip_corr returns a
# variance correction, whereas this method returns a sigma correction.
#####################################################################################################

def compute_iterative_clip_sigma_corr(n_clip_sigma):

    """
    Method compute_iterative_clip_sigma_corr

    Inputs:
    n_clip_sigma            Number of sigmas for the data clipping (dimensionless).

    Returns:
    corr_fact               Dimensionless factor to multiply the clipped sigma by to recover the
                            true sigma of the underlying Gaussian noise (always >= 1.0).
    """

    c = 1.0

    for i in range(100):

        t = n_clip_sigma * c

        phi = math.exp(-0.5 * t * t) / math.sqrt(2.0 * math.pi)
        cdf = 0.5 * (1.0 + math.erf(t / math.sqrt(2.0)))

        var_trunc = 1.0 - 2.0 * t * phi / (2.0 * cdf - 1.0)

        c_new = math.sqrt(var_trunc)

        if abs(c_new - c) < 1.0e-12:
            c = c_new
            break

        c = c_new

    return 1.0 / c


#####################################################################################################
# Compute the per-pixel noise a sky level would produce if the image were sky-limited.
#
# This is the same noise model the pipeline uses to build its uncertainty images in
# referenceImageSubs.py and differenceImageSubs.py:
#
#     sigma [DN] = sqrt(sky [DN] / gain + (readout noise [e-] / gain)^2)
#
# A background sigma measured off the pixels that is much larger than this is a sign that the
# clipping never reached blank sky, which happens in crowded fields, where what gets measured is
# source confusion rather than background.
#####################################################################################################

def compute_poisson_sigma_expectation(sky_level,sca_gain = 1.0,sca_readout_noise = 0.0):

    """
    Method compute_poisson_sigma_expectation

    Inputs:
    sky_level               Sky level [DN], as accumulated counts, not a rate.
    sca_gain                Detector gain [electrons/DN].  The SOC-simulation conversion assumes
                            1.0, while the pipeline configuration carries 2.0 for socsims and 1.0
                            for the OpenUniverse and RIMTIMSIM sims.
    sca_readout_noise       Detector readout noise [electrons].  Zero by default, which makes the
                            expectation purely photon noise.

    Returns:
    poisson_sigma           Expected per-pixel noise [DN] for a sky-limited image at that sky level.
    """

    if sca_gain <= 0.0:
        raise ValueError(f"Method compute_poisson_sigma_expectation: sca_gain = {sca_gain} "
                         "must be positive")

    if sky_level < 0.0:
        raise ValueError(f"Method compute_poisson_sigma_expectation: sky_level = {sky_level} "
                         "must not be negative")

    variance = sky_level / sca_gain + (sca_readout_noise / sca_gain) ** 2

    return math.sqrt(variance)


#####################################################################################################
# Estimate the background noise sigma of an image via iterative sigma clipping.
#####################################################################################################

def estimate_background_sigma(data,n_clip_sigma = 3.0,maxiters = 10,mask = None,apply_clip_corr = True):

    """
    Method estimate_background_sigma

    Iteratively sigma-clips the image pixels and returns the standard deviation of the surviving
    pixels as the background-noise estimate.  Sources are rejected by the clipping, so the result
    is an estimate of the pixel-to-pixel background noise, not of the total image variance.

    Inputs:
    data                    Two-dimensional image data (numpy array), in any units; for an L2
                            image these are DN.  NaNs are ignored.
    n_clip_sigma            Number of sigmas for the data clipping (dimensionless).
    maxiters                Maximum number of clipping iterations.  Set to 1 for a single pass.
    mask                    Optional boolean array, of the same shape as data, that is True for
                            pixels to be excluded (e.g., a source mask or a bad-pixel mask).
    apply_clip_corr         Reinflate the clipped standard deviation with
                            compute_iterative_clip_sigma_corr, which removes the few-percent
                            low bias that the clipping itself introduces.  This assumes the
                            clipping has converged on Gaussian background noise, and is inert
                            beyond about 5 sigma.

    Returns:
    bkgsig                  Clipped standard deviation of the background pixels, in the units of
                            data; DN for an L2 image, which is not yet the DN/s that ZPTMAG
                            applies to.
    npixused                Number of pixels that survived the masking and the clipping [pixels].
    """

    a = np.array(data, dtype=np.float64)

    bad = ~np.isfinite(a)

    if mask is not None:
        bad = bad | np.array(mask, dtype=bool)

    clipped = sigma_clip(ma.masked_array(a, bad), sigma=n_clip_sigma, maxiters=maxiters, masked=True)

    bkgsig = float(ma.getdata(clipped.std()))
    npixused = int(clipped.count())

    if apply_clip_corr:
        bkgsig *= compute_iterative_clip_sigma_corr(n_clip_sigma)

    if not np.isfinite(bkgsig) or bkgsig <= 0.0:
        raise ValueError(f"Method estimate_background_sigma: invalid background sigma = {bkgsig}")

    return bkgsig,npixused


#####################################################################################################
# Compute the n-sigma point-source limiting magnitude of an image.
#
# The limiting flux is
#
#     f_lim = n_sigma * sqrt(N_p) * sigma_bkg / psf_flux_fraction
#
# and the limiting magnitude is
#
#     m_lim = zptmag - 2.5 * log10(f_lim)
#
# consistent with the flux-to-magnitude convention used elsewhere in the pipeline
# (mag = -2.5 * log10(flux) + zeropoint).
#####################################################################################################

def compute_limiting_magnitude(bkgsig,noise_pixels,zptmag,n_sigma_limit = 5.0,psf_flux_fraction = 1.0):

    """
    Method compute_limiting_magnitude

    Inputs:
    bkgsig                  Background-noise sigma [DN/s], the units ZPTMAG applies to.  Divide a
                            sigma measured off L2 pixels by EXPTIME before passing it here.
    noise_pixels            Number of noise pixels of the PSF model [pixels]
                            (see compute_psf_noise_pixels).
    zptmag                  AB magnitude zeropoint of the image [AB mag], defined by
                            mag = -2.5 * log10(flux) + zptmag for flux in DN/s.
    n_sigma_limit           Signal-to-noise ratio that defines the limit (dimensionless; 5.0 for a
                            5-sigma limit).
    psf_flux_fraction       Dimensionless fraction of the total point-source flux contained in the
                            PSF model stamp.  Leave at 1.0 for a PSF stamp large enough to hold essentially
                            all of the flux; set to the enclosed-energy fraction for a truncated
                            stamp, which faintens the limiting magnitude accordingly.

    Returns:
    maglimit                Limiting magnitude [AB mag], in the same magnitude system as zptmag.
    fluxlimit               Limiting flux [DN/s].
    """

    if bkgsig <= 0.0:
        raise ValueError(f"Method compute_limiting_magnitude: bkgsig = {bkgsig} must be positive")

    if noise_pixels <= 0.0:
        raise ValueError(f"Method compute_limiting_magnitude: noise_pixels = {noise_pixels} must be positive")

    if psf_flux_fraction <= 0.0 or psf_flux_fraction > 1.0:
        raise ValueError(f"Method compute_limiting_magnitude: psf_flux_fraction = {psf_flux_fraction} "
                         "must be in the interval (0,1]")

    fluxlimit = n_sigma_limit * math.sqrt(noise_pixels) * bkgsig / psf_flux_fraction

    maglimit = zptmag - 2.5 * math.log10(fluxlimit)

    return maglimit,fluxlimit


#####################################################################################################
# Compute the n-sigma point-source limiting magnitude of a Roman WFI L2 image stored in a FITS file.
#####################################################################################################

def compute_limiting_magnitude_for_l2_image(input_img_filename,
                                            input_psf_filename,
                                            zptmag = None,
                                            n_sigma_limit = 5.0,
                                            n_clip_sigma = 3.0,
                                            maxiters = 10,
                                            hdu_index = None,
                                            psf_hdu_index = 0,
                                            input_unc_filename = None,
                                            noise_method = "image",
                                            mask = None,
                                            psf_flux_fraction = 1.0,
                                            dn_per_sec_factor = None,
                                            sca_gain = 1.0,
                                            sca_readout_noise = 0.0,
                                            poisson_ratio_warn = 2.0,
                                            zptmag_fits_keyword = "ZPTMAG"):

    """
    Method compute_limiting_magnitude_for_l2_image

    Inputs:
    input_img_filename      FITS file containing the Roman WFI L2 image, whose data are in DN.
                            Data already normalized to DN/s are detected from BUNIT and left
                            unscaled.
    input_psf_filename      FITS file containing the PSF model for the image, sampled at the pixel
                            scale of the image.  The PSF is used only through its number of noise
                            pixels, so its normalization does not matter.
    zptmag                  AB magnitude zeropoint of the image [AB mag], for flux in DN/s.  If
                            None, it is read from FITS keyword zptmag_fits_keyword.
    n_sigma_limit           Signal-to-noise ratio that defines the limit (dimensionless; 5.0 for a
                            5-sigma limit).
    n_clip_sigma            Number of sigmas for the data clipping of the background estimate
                            (dimensionless).
    maxiters                Maximum number of clipping iterations.  The single-pass 6-sigma clipping
                            used for the difference-image FOM metric corresponds to
                            n_clip_sigma = 6.0 and maxiters = 1, which is appropriate for an image
                            whose sky is free of sources but not for an L2 science image.
    hdu_index               HDU index of the image data.  If None, the first HDU holding
                            two-dimensional image data is used.
    psf_hdu_index           HDU index of the PSF-model data.
    input_unc_filename      FITS file containing the per-pixel uncertainty image, in the same units
                            as the image (DN for an L2 image), so that the same conversion to DN/s
                            applies to it.  Required for noise_method = "uncertainty".
    noise_method            "image" estimates the background noise by sigma-clipping the image
                            pixels themselves.  "uncertainty" instead takes the clipped median of
                            the uncertainty image, which avoids source contamination but relies on
                            the uncertainty map being correct.  Comparing the two is a useful check
                            that the uncertainty map is reasonable.
    mask                    Optional boolean array, of the same shape as the image, that is True for
                            pixels to be excluded from the background estimate (e.g., a source mask
                            or a bad-pixel mask).
    psf_flux_fraction       Dimensionless fraction of the total point-source flux contained in the
                            PSF stamp.
    dn_per_sec_factor       Factor that converts the pixel values into the DN/s that ZPTMAG applies
                            to, normally 1 / EXPTIME [1/s].  If None, it is determined from BUNIT
                            and EXPTIME by get_dn_per_sec_factor.
    sca_gain                Detector gain [electrons/DN], used only for the sky-limited check
                            described under poisson_ratio_warn.
    sca_readout_noise       Detector readout noise [electrons], likewise.
    poisson_ratio_warn      Warn when the measured background sigma exceeds the sky-limited
                            expectation by more than this dimensionless factor, which means the
                            clipping never reached blank sky and the limiting magnitude describes
                            source confusion rather than background.  Set to None to skip the
                            check.  It is skipped automatically for an image that is not sky
                            dominated, such as a background-subtracted or difference image, whose
                            sky level carries no Poisson information.
    zptmag_fits_keyword     FITS keyword from which the zeropoint is read when zptmag is None.

    Returns:
    Dictionary with the limiting magnitude and the quantities that went into it:

    maglimit                Limiting magnitude [AB mag].
    fluxlimit               Limiting flux [DN/s].
    bkgsig                  Background-noise sigma [DN/s], after conversion from the DN of the
                            image by dnpersecfactor.
    noisepixels             Number of noise pixels of the PSF model [pixels].
    psfsum                  Sum of the PSF-model pixel values, in the units of the PSF file.
    zptmag                  AB magnitude zeropoint used [AB mag], for flux in DN/s.
    nsigmalimit             Signal-to-noise ratio that defined the limit (dimensionless).
    noisemethod             "image" or "uncertainty", as a string.
    npixused                Number of pixels the background estimate was made from [pixels].
    skylevel                Median sky level [DN], reported whenever EXPTIME is known, including
                            when the check below was skipped; None otherwise.
    poissonsigma            Sky-limited expectation for the background sigma [DN/s], or None when
                            the check was skipped because the image is not sky dominated.
    poissonratio            bkgsig divided by poissonsigma (dimensionless), or None as above.  A
                            value near 1 means the image is sky-limited; a large value means the
                            background estimate is contaminated, usually by source confusion.
    dnpersecfactor          Factor applied to convert the measured sigma to DN/s, normally
                            1 / EXPTIME [1/s].  For noise_method = "uncertainty" this comes from
                            the uncertainty file's own header.
    hduindex                Index of the HDU the image data were read from (dimensionless).

    Notes:
    Only background noise is counted, which is the relevant noise for a faint point source; the
    Poisson noise of the source itself is neglected.  The background sigma is measured in the
    native units of the image, normally DN, and then converted to DN/s, since ZPTMAG is defined
    for flux in DN/s.  The magnitudes are AB magnitudes as long as ZPTMAG is an AB zeropoint.
    """

    print("Computing limiting magnitude for input FITS image =",input_img_filename)

    metadata = get_l2_image_metadata(input_img_filename,hdu_index = hdu_index)

    hdu_index = metadata["hduindex"]

    hdul_image = fits.open(input_img_filename)
    data_image = np.array(hdul_image[hdu_index].data)
    hdul_image.close()

    hdul_psf = fits.open(input_psf_filename)
    data_psf = np.array(hdul_psf[psf_hdu_index].data)
    hdr_psf = hdul_psf[psf_hdu_index].header
    hdul_psf.close()

    if "OVERSAMP" in hdr_psf and hdr_psf["OVERSAMP"] != 1:
        print(f"*** Warning: PSF model {input_psf_filename} has OVERSAMP = {hdr_psf['OVERSAMP']}; "
              "the number of noise pixels assumes the PSF is sampled at the image pixel scale")

    if zptmag is None:

        zptmag = metadata["zptmag"]

        if zptmag is None:
            raise KeyError(f"Method compute_limiting_magnitude_for_l2_image: FITS keyword "
                           f"{zptmag_fits_keyword} not found in {input_img_filename}; "
                           "pass zptmag explicitly")

        zptmag = float(zptmag)

    if mask is not None and np.shape(mask) != np.shape(data_image):
        raise ValueError(f"Method compute_limiting_magnitude_for_l2_image: mask shape "
                         f"{np.shape(mask)} does not match image shape {np.shape(data_image)}")

    dn_per_sec_factor_is_auto = dn_per_sec_factor is None

    if dn_per_sec_factor_is_auto:
        dn_per_sec_factor = get_dn_per_sec_factor(metadata["bunit"],metadata["exptime"])


    # The factor that converts the measured sigma to DN/s, which for the uncertainty map is
    # determined from that file's own header, since it need not share the units of the image.

    bkgsig_factor = dn_per_sec_factor

    if noise_method == "image":

        bkgsig,npixused = estimate_background_sigma(data_image,
                                                    n_clip_sigma = n_clip_sigma,
                                                    maxiters = maxiters,
                                                    mask = mask)

    elif noise_method == "uncertainty":

        if input_unc_filename is None:
            raise ValueError("Method compute_limiting_magnitude_for_l2_image: "
                             "input_unc_filename is required for noise_method = \"uncertainty\"")

        hdul_uncert = fits.open(input_unc_filename)
        unc_hdu_index = get_image_hdu_index(hdul_uncert)
        data_uncert = np.array(hdul_uncert[unc_hdu_index].data, dtype=np.float64)
        hdul_uncert.close()

        if data_uncert.shape != data_image.shape:
            raise ValueError(f"Method compute_limiting_magnitude_for_l2_image: uncertainty image "
                             f"{input_unc_filename} has shape {data_uncert.shape}, which does not "
                             f"match the shape {data_image.shape} of {input_img_filename}")

        if dn_per_sec_factor_is_auto:

            metadata_uncert = get_l2_image_metadata(input_unc_filename)

            bkgsig_factor = get_dn_per_sec_factor(metadata_uncert["bunit"],metadata_uncert["exptime"])

            if bkgsig_factor != dn_per_sec_factor:
                print(f"*** Warning: the uncertainty image is in BUNIT = {metadata_uncert['bunit']} "
                      f"while the science image is in BUNIT = {metadata['bunit']}; converting each "
                      "to DN/s with its own exposure time")

        bad = ~np.isfinite(data_uncert) | (data_uncert <= 0.0)

        if mask is not None:
            bad = bad | np.array(mask, dtype=bool)

        avg,med,std = sigma_clipped_stats(data_uncert,
                                          mask = bad,
                                          sigma = n_clip_sigma,
                                          maxiters = maxiters)
        bkgsig = float(med)
        npixused = int(np.sum(~bad))

        if not np.isfinite(bkgsig) or bkgsig <= 0.0:
            raise ValueError(f"Method compute_limiting_magnitude_for_l2_image: "
                             f"invalid background sigma = {bkgsig} from uncertainty image")

    else:
        raise ValueError("Method compute_limiting_magnitude_for_l2_image: noise_method = "
                         f"{noise_method} is not one of \"image\" or \"uncertainty\"")


    # Convert the background sigma from the DN it was measured in into the DN/s that ZPTMAG
    # applies to.  For an L2 image this divides by EXPTIME; for an image the pipeline has
    # already normalized to a rate, the factor is 1.0 and this is a no-op.

    bkgsig = bkgsig * bkgsig_factor


    # Compare the measured background sigma with what a sky-limited image would give, working in
    # accumulated DN, which is what the Poisson expectation is defined for.  The image data are
    # already in DN unless BUNIT says otherwise, in which case dn_per_sec_factor is 1.0 and
    # multiplying by EXPTIME recovers the DN.

    sky_level = None
    poisson_sigma = None
    poisson_ratio = None

    exptime = metadata["exptime"]

    good = np.isfinite(data_image)

    if mask is not None:
        good = good & ~np.array(mask, dtype=bool)

    if exptime is not None and exptime > 0.0 and np.any(good):

        sky_level = float(np.median(data_image[good])) * exptime * dn_per_sec_factor


        # The comparison only means something for an image whose sky is still in it.  A
        # sky-dominated image has more sky counts than per-pixel noise, since sqrt(N) < N for any
        # N above 1, whereas a background-subtracted or difference image has a sky level near
        # zero and noise that says nothing about it.

        sigma_dn = bkgsig * exptime

        if sky_level > sigma_dn:

            poisson_sigma = compute_poisson_sigma_expectation(sky_level,
                                                              sca_gain = sca_gain,
                                                              sca_readout_noise = sca_readout_noise)

            # Both sigmas in DN/s, so that the ratio can be reported alongside bkgsig.

            poisson_sigma = poisson_sigma / float(exptime)

            poisson_ratio = bkgsig / poisson_sigma

            if poisson_ratio_warn is not None and poisson_ratio > poisson_ratio_warn:

                print(f"*** Warning: the measured background sigma of {bkgsig} DN/s is "
                      f"{poisson_ratio} times the {poisson_sigma} DN/s expected for a sky-limited "
                      f"image at a sky level of {sky_level} DN.  The sigma clipping is not "
                      "reaching blank sky, most often because the field is crowded, so the "
                      "limiting magnitude below describes source confusion rather than "
                      "background.  Pass a source mask, or use noise_method = \"uncertainty\", "
                      "for the background-limited depth.")

    noise_pixels,psf_sum = compute_psf_noise_pixels(data_psf)

    maglimit,fluxlimit = compute_limiting_magnitude(bkgsig,
                                                    noise_pixels,
                                                    zptmag,
                                                    n_sigma_limit = n_sigma_limit,
                                                    psf_flux_fraction = psf_flux_fraction)

    print("hdu_index =",hdu_index)
    print("zptmag [AB mag, for flux in DN/s] =",zptmag)
    print("dn_per_sec_factor [1/s] =",bkgsig_factor)
    print("bkgsig [DN/s] =",bkgsig)
    print("noise_pixels [pixels] =",noise_pixels)
    print(f"{n_sigma_limit}-sigma fluxlimit [DN/s] =",fluxlimit)
    print("sky_level [DN] =",sky_level)
    print("poisson_sigma [DN/s] =",poisson_sigma)
    print("poisson_ratio (dimensionless) =",poisson_ratio)
    print(f"{n_sigma_limit}-sigma maglimit [AB mag] =",maglimit)

    limmag = {}

    limmag["maglimit"] = maglimit
    limmag["fluxlimit"] = fluxlimit
    limmag["bkgsig"] = bkgsig
    limmag["noisepixels"] = noise_pixels
    limmag["psfsum"] = psf_sum
    limmag["zptmag"] = zptmag
    limmag["nsigmalimit"] = n_sigma_limit
    limmag["noisemethod"] = noise_method
    limmag["npixused"] = npixused
    limmag["dnpersecfactor"] = bkgsig_factor
    limmag["hduindex"] = hdu_index
    limmag["skylevel"] = sky_level
    limmag["poissonsigma"] = poisson_sigma
    limmag["poissonratio"] = poisson_ratio

    return limmag
