# ported-from: pipeline/psfFwhmSubs.py, modules/utils/rapid_pipeline_subs.py @ d6aac515
"""An image's FWHM from its SExtractor catalog, measured on point sources.

`dev`: ``pipeline/psfFwhmSubs.py`` (``select_point_sources``,
``fwhm_mode``, ``catalog_fwhm_stats``, dev commit 1822789e) and
``ROMAN_WFI_PLATESCALE`` (``modules/utils/rapid_pipeline_subs.py``,
c479ddfc). The FWHM feeds ``SEEING_FWHM`` of the gain-match SExtractor
runs, in arcseconds since dev commit 095c45d8.

The median ``FWHM_IMAGE`` over all sources is pulled up by galaxies, so
`dev` keeps the point sources (``FLAGS == 0``, ``FLUX_AUTO /
FLUXERR_AUTO >= 50``, ``ELONGATION <= 1.3``) and takes the mode of their
FWHM, the peak of a 0.05-pixel histogram smoothed over five bins.
Fallbacks, each logged as a warning: fewer than 50 point sources, their
median; fewer than 10, the median over all sources.

`dev`'s ``catalog_fwhm_stats`` also returns a percentile of the same
sample, used only by the reference image's quality bits; the difference
stage does not read it and it is not ported here.
"""

from __future__ import annotations

import logging

import numpy as np

logger = logging.getLogger(__name__)

#: Roman WFI plate scale, arcseconds per pixel (`dev` ``util.ROMAN_WFI_PLATESCALE``).
ROMAN_WFI_PLATESCALE = 0.11

POINT_SOURCE_CATALOG_PARAMS = ["FWHM_IMAGE", "FLAGS", "FLUX_AUTO", "FLUXERR_AUTO", "ELONGATION"]

MIN_POINT_SOURCE_SNR = 50.0
MAX_POINT_SOURCE_ELONGATION = 1.3
MIN_POINT_SOURCE_COUNT = 10
MIN_MODE_COUNT = 50
MODE_BIN_WIDTH = 0.05
MODE_SMOOTHING_BINS = 5


def select_point_sources(fwhm, flags, flux, fluxerr, elongation,
                         min_snr: float = MIN_POINT_SOURCE_SNR,
                         max_elongation: float = MAX_POINT_SOURCE_ELONGATION) -> np.ndarray:
    """True for the sources that pass `dev`'s point-source cut."""
    fwhm = np.asarray(fwhm, dtype=float)
    flags = np.asarray(flags, dtype=float)
    flux = np.asarray(flux, dtype=float)
    fluxerr = np.asarray(fluxerr, dtype=float)
    elongation = np.asarray(elongation, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        snr = np.where(fluxerr > 0.0, flux / fluxerr, np.nan)
    return (np.isfinite(fwhm) & (fwhm > 0.0) & (flags == 0)
            & np.isfinite(snr) & (snr >= min_snr)
            & np.isfinite(elongation) & (elongation <= max_elongation))


def fwhm_mode(fwhm, bin_width: float = MODE_BIN_WIDTH,
              smoothing_bins: int = MODE_SMOOTHING_BINS) -> float:
    """The peak of the boxcar-smoothed histogram of the FWHM values, in pixels."""
    fwhm = np.asarray(fwhm, dtype=float)
    fwhm = fwhm[np.isfinite(fwhm)]
    edges = np.arange(np.floor(fwhm.min() / bin_width) * bin_width,
                      fwhm.max() + 2.0 * bin_width, bin_width)
    counts, edges = np.histogram(fwhm, bins=edges)
    smoothed = np.convolve(counts, np.ones(smoothing_bins) / smoothing_bins, mode="same")
    ipeak = int(np.argmax(smoothed))
    return float(0.5 * (edges[ipeak] + edges[ipeak + 1]))


def catalog_fwhm_stats(catalog_rows, label: str = "image",
                       min_count: int = MIN_POINT_SOURCE_COUNT) -> tuple[float, float, float]:
    """``(minpix, maxpix, medpix)`` of rows parsed with :data:`POINT_SOURCE_CATALOG_PARAMS`.

    ``medpix`` is the mode over the point sources, or a median fallback.
    """
    rows = np.array(catalog_rows, dtype=float).reshape(-1, len(POINT_SOURCE_CATALOG_PARAMS))
    fwhm, flags, flux, fluxerr, elongation = rows.T
    if fwhm.size == 0:
        logger.warning("%s catalog has no sources; FWHM is undefined", label)
        return np.nan, np.nan, np.nan
    with np.errstate(all="ignore"):
        minpix = float(np.nanmin(fwhm))
        maxpix = float(np.nanmax(fwhm))
        allmedpix = float(np.nanmedian(fwhm))
    point_sources = select_point_sources(fwhm, flags, flux, fluxerr, elongation)
    npoint = int(np.count_nonzero(point_sources))
    if npoint >= MIN_MODE_COUNT:
        medpix = fwhm_mode(fwhm[point_sources])
        logger.info("%s FWHM: mode over %d point sources = %s px (median over all %d "
                    "sources: %s px)", label, npoint, medpix, fwhm.size, allmedpix)
    elif npoint >= min_count:
        medpix = float(np.median(fwhm[point_sources]))
        logger.warning("only %d point sources in the %s catalog (need %d for the mode); "
                       "using their median = %s px", npoint, label, MIN_MODE_COUNT, medpix)
    else:
        medpix = allmedpix
        logger.warning("only %d point sources in the %s catalog (need %d); using the median "
                       "over all %d sources = %s px", npoint, label, min_count, fwhm.size, medpix)
    return minpix, maxpix, medpix
