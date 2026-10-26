# ported-from: pipeline/referenceImageSubs.py, pipeline/awsBatchSubmitJobs_runSingleReferenceImagePipeline.py @ c740f3e3
"""The reference mosaic's quality measurements, as `dev` computes them.

`dev`: ``compute_cov5percent`` (``pipeline/referenceImageSubs.py``) and the
statistics block in ``awsBatchSubmitJobs_runSingleReferenceImagePipeline.py``:
3-sigma clipped statistics (``fits_data_statistics_with_clipping``, HDU 0)
of the coverage map (``medncov`` = its ``gmed``), of the uncertainty image
(``medpixunc`` = its ``gmed``) and of the mosaic, whose saturation level
is `dev`'s stopgap ``sextractor_SATUR_LEVEL / 60.0`` ("The following is
incorrect (60 seconds), but used as a stopgap for now"). The names of the
returned fields are ``refimmeta``'s columns (products page, "Registration
metadata").
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from astropy.io import fits

from rapidpipe.science.difference.statistics import fits_data_statistics_with_clipping

logger = logging.getLogger(__name__)


def compute_cov5percent(reference_cov_map_filename) -> float:
    """`dev` ``compute_cov5percent``.

    The percentage of the summed coverage, each pixel's capped at 5,
    relative to 5 times the pixel count.
    """
    hdu_index = 0
    hdul = fits.open(reference_cov_map_filename)
    data = hdul[hdu_index].data
    a = np.array(data)
    hdul.close()

    pixel_coverage_limit = 5
    image_size = np.shape(a)
    pixcount = image_size[0] * image_size[1]
    b = np.where(a > pixel_coverage_limit, pixel_coverage_limit, a)
    c = np.sum(b)
    cov5percent = c / (pixel_coverage_limit * pixcount) * 100

    logger.info("pixel_coverage_limit = %s, c = %s, pixcount = %s, cov5percent = %s",
                pixel_coverage_limit, c, pixcount, cov5percent)
    return float(cov5percent)


@dataclass(frozen=True)
class MosaicMeasurements:
    """refimmeta's image measurements, by column name."""

    cov5percent: float
    medncov: float
    medpixunc: float
    npixsat: int
    npixnan: int
    clmean: float
    clstddev: float
    clnoutliers: int
    gmedian: float
    datascale: float
    gmin: float
    gmax: float

    def to_dict(self) -> dict:
        return asdict(self)


def saturation_level_refimage_rate(sextractor_satur_level: float) -> float:
    """`dev`'s stopgap: the SExtractor saturation level over 60 seconds."""
    return sextractor_satur_level / 60.0


def measure_mosaic(image: Path, coverage: Path, uncertainty: Path, *,
                   sextractor_satur_level: float, seed: int | None = None) -> MosaicMeasurements:
    """`dev`'s order: cov5percent, coverage and uncertainty medians, image statistics."""
    cov5percent = compute_cov5percent(coverage)

    n_sigma = 3.0
    hdu_index = 0
    stats_covimage = fits_data_statistics_with_clipping(coverage, n_sigma, hdu_index, seed=seed)
    medncov = stats_covimage["gmed"]
    stats_uncimage = fits_data_statistics_with_clipping(uncertainty, n_sigma, hdu_index, seed=seed)
    medpixunc = stats_uncimage["gmed"]

    stats_refimage = fits_data_statistics_with_clipping(
        image, n_sigma, hdu_index, saturation_level_refimage_rate(sextractor_satur_level),
        seed=seed)

    return MosaicMeasurements(
        cov5percent=float(cov5percent),
        medncov=float(medncov),
        medpixunc=float(medpixunc),
        npixsat=int(stats_refimage["satcount"]),
        npixnan=int(stats_refimage["nancount"]),
        clmean=float(stats_refimage["clippedavg"]),
        clstddev=float(stats_refimage["clippedstd"]),
        clnoutliers=int(stats_refimage["noutliers"]),
        gmedian=float(stats_refimage["gmed"]),
        datascale=float(stats_refimage["gsigma"]),
        gmin=float(stats_refimage["gdatamin"]),
        gmax=float(stats_refimage["gdatamax"]),
    )


#: Sources measured for the stars' width: SExtractor ``FLAGS`` 0, ``FLUX_AUTO`` over its error at
#: least this, no other catalog source within this, px.
STAR_SNR_MIN, STAR_ISOLATION_PX = 50.0, 14.0
#: The stars are the narrowest population: widths within this of the sources' 5th percentile, px.
#: On run 46's F146 SCA 8 mosaic the stars sit at 0.95 to 1.05 px and the galaxies at 1.3 to 1.7
#: with none between 1.1 and 1.2; SExtractor's CLASS_STAR does not separate them (median 0.69
#: for the stars, 0.71 for the galaxies), and a CLASS_STAR cut gave 1.55 px (PR 352).
STAR_LOCUS_PX = 0.15
_STAMP_HALF = 15


def quality_metrics(image: Path, coverage: Path, catalog_rows: list[list[str]],
                    measurements: MosaicMeasurements) -> dict:
    """The reference job's ``metrics``: one flat dict of named numbers.

    ``catalog_rows`` are the mosaic catalog's ``XWIN_IMAGE``, ``YWIN_IMAGE``
    (1-based), ``FLAGS``, ``FLUX_AUTO`` and ``FLUXERR_AUTO``.

    - ``star_sigma_px``: the median second-moment sigma within 4 px
      (:func:`rapidpipe.science.difference.refpsf.moments`, on the mosaic
      less each stamp's 8 to 12 px annulus median) of the stars: of the
      isolated, unflagged sources at S/N 50 or more, those within
      :data:`STAR_LOCUS_PX` of the 5th percentile of their widths; mosaic
      px; ``n_sigma_stars`` beside it; both absent without a source.
    - ``coverage_lt1``, ``coverage_lt2``, ``coverage_lt3``: the share of the
      mosaic's covered pixels (coverage above 0) whose coverage is below 1,
      2 and 3 frames.
    - ``gmin`` and ``gmax``: the mosaic's extremes (`dev`'s, above).
    """
    from rapidpipe.science.difference.refpsf import moments

    cov = np.asarray(fits.getdata(coverage, 0), dtype=float)
    covered = cov[np.isfinite(cov) & (cov > 0)]
    metrics: dict = {"gmin": measurements.gmin, "gmax": measurements.gmax}
    if covered.size:
        for n in (1, 2, 3):
            metrics[f"coverage_lt{n}"] = float(np.mean(covered < n))
    if not catalog_rows:
        return metrics
    rows = np.array(catalog_rows, dtype=float)
    x, y = rows[:, 0] - 1, rows[:, 1] - 1
    compact = (rows[:, 2] == 0) & (rows[:, 3] >= STAR_SNR_MIN * rows[:, 4])
    from scipy.spatial import cKDTree

    pairs = cKDTree(np.column_stack([x, y])).query_pairs(STAR_ISOLATION_PX, output_type="ndarray")
    compact[pairs.ravel()] = False
    data = np.asarray(fits.getdata(image, 0), dtype=float)
    ny, nx = data.shape
    h = _STAMP_HALF
    rr, cc = np.mgrid[-h:h + 1, -h:h + 1]
    annulus = (np.hypot(rr, cc) >= 8) & (np.hypot(rr, cc) < 12)
    sigmas = []
    for xi, yi in zip(np.rint(x[compact]).astype(int), np.rint(y[compact]).astype(int)):
        if not (h <= xi < nx - h and h <= yi < ny - h):
            continue
        stamp = data[yi - h:yi + h + 1, xi - h:xi + h + 1]
        if not np.isfinite(stamp).all():
            continue
        sigma = moments(stamp - np.median(stamp[annulus]))[2]
        if np.isfinite(sigma):
            sigmas.append(sigma)
    if sigmas:
        sigmas = np.asarray(sigmas)
        stars = sigmas[sigmas <= np.percentile(sigmas, 5) + STAR_LOCUS_PX]
        metrics["star_sigma_px"] = float(np.median(stars))
        metrics["n_sigma_stars"] = len(stars)
    return metrics
