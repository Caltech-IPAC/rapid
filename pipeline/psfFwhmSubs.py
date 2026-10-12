"""
Image FWHM from a SExtractor catalog, measured on point sources.

The FWHM measured here feeds SEEING_FWHM for the gain-match SExtractor runs (through which
CLASS_STAR separates stars from galaxies) and is stored in the database as fwhmmedpix.  The
median FWHM_IMAGE over all sources is pulled up by galaxies: on an OpenUniverse reference coadd
it was 3.18 px against a stellar 1.86 px.  A crude point-source cut on quantities that do not
depend on SEEING_FWHM, i.e. not CLASS_STAR, removes most of that bias:

    FLAGS == 0                      no blends, saturation or truncation
    FLUX_AUTO / FLUXERR_AUTO >= 50  bright enough for a well-measured profile
    ELONGATION <= 1.3               round; the guided WFI PSF has ellipticity ~0.12 (1.14)

Round, bright galaxies still pass, and in a galaxy-dominated field they outnumber the stars,
so the median of the survivors is still biased (2.89 px against the stellar 1.86 px on that
coadd).  The stars, however, pile up at a single FWHM while the galaxies spread over a wide
range, so the MODE of the survivors' FWHM is the stellar locus: 1.88, 1.68 and 1.88 px against
truth-matched stellar values of 1.86, 1.72 and 1.86 px on three OpenUniverse reference coadds.

Fallbacks, each with a warning: fewer than min_mode_count survivors, their median; fewer than
min_point_source_count, the median over all sources, as before.
"""

import numpy as np


point_source_catalog_params = ["FWHM_IMAGE","FLAGS","FLUX_AUTO","FLUXERR_AUTO","ELONGATION"]

min_point_source_snr = 50.0
max_point_source_elongation = 1.3
min_point_source_count = 10
min_mode_count = 50

# Histogram of FWHM_IMAGE for the mode: bin width and the number of bins in the boxcar that
# smooths it before the peak is taken (0.05 px x 5 = 0.25 px).
mode_bin_width = 0.05
mode_smoothing_bins = 5


def select_point_sources(fwhm,flags,flux,fluxerr,elongation,
                         min_snr=min_point_source_snr,
                         max_elongation=max_point_source_elongation):

    '''
    Return a boolean array, True for the sources that pass the point-source cut.
    '''

    fwhm = np.asarray(fwhm,dtype=float)
    flags = np.asarray(flags,dtype=float)
    flux = np.asarray(flux,dtype=float)
    fluxerr = np.asarray(fluxerr,dtype=float)
    elongation = np.asarray(elongation,dtype=float)

    with np.errstate(divide="ignore",invalid="ignore"):
        snr = np.where(fluxerr > 0.0,flux / fluxerr,np.nan)

    return (np.isfinite(fwhm) & (fwhm > 0.0) &
            (flags == 0) &
            np.isfinite(snr) & (snr >= min_snr) &
            np.isfinite(elongation) & (elongation <= max_elongation))


def fwhm_mode(fwhm,bin_width=mode_bin_width,smoothing_bins=mode_smoothing_bins):

    '''
    Return the peak of the boxcar-smoothed histogram of the FWHM values [px].
    '''

    fwhm = np.asarray(fwhm,dtype=float)
    fwhm = fwhm[np.isfinite(fwhm)]

    edges = np.arange(np.floor(fwhm.min() / bin_width) * bin_width,
                      fwhm.max() + 2.0 * bin_width,bin_width)

    counts,edges = np.histogram(fwhm,bins=edges)

    smoothed = np.convolve(counts,np.ones(smoothing_bins) / smoothing_bins,mode="same")

    ipeak = int(np.argmax(smoothed))

    return float(0.5 * (edges[ipeak] + edges[ipeak + 1]))


def catalog_fwhm_stats(catalog_rows,label="image",min_count=min_point_source_count):

    '''
    Return (minpix, maxpix, medpix) for the rows of a SExtractor catalog parsed with
    point_source_catalog_params.

    medpix is the mode of FWHM_IMAGE over the point sources (see the module docstring for the
    fallbacks).  minpix and maxpix are over all sources, as before.
    '''

    rows = np.array(catalog_rows,dtype=float).reshape(-1,len(point_source_catalog_params))

    fwhm,flags,flux,fluxerr,elongation = rows.T

    if fwhm.size == 0:
        print(f"*** Warning: {label} catalog has no sources; FWHM is undefined")
        return np.nan,np.nan,np.nan

    with np.errstate(all="ignore"):
        minpix = np.nanmin(fwhm)
        maxpix = np.nanmax(fwhm)
        allmedpix = np.nanmedian(fwhm)

    point_sources = select_point_sources(fwhm,flags,flux,fluxerr,elongation)

    npoint = int(np.count_nonzero(point_sources))

    if npoint >= min_mode_count:
        medpix = fwhm_mode(fwhm[point_sources])
        print(f"{label} FWHM: mode over {npoint} point sources = {medpix} px "
              f"(median over all {fwhm.size} sources: {allmedpix} px)")
    elif npoint >= min_count:
        medpix = float(np.median(fwhm[point_sources]))
        print(f"*** Warning: only {npoint} point sources in the {label} catalog (need {min_mode_count} "
              f"for the mode); using their median = {medpix} px")
    else:
        medpix = float(allmedpix)
        print(f"*** Warning: only {npoint} point sources in the {label} catalog (need {min_count}); "
              f"using the median over all {fwhm.size} sources = {medpix} px")

    return minpix,maxpix,medpix
