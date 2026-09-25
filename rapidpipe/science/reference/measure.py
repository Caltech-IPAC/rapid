"""The reference mosaic's quality measurements, as `dev` computes them.

`dev`: ``compute_cov5percent`` (``pipeline/referenceImageSubs.py``) and the
statistics block in ``awsBatchSubmitJobs_runSingleReferenceImagePipeline.py``:
3-sigma clipped statistics (``fits_data_statistics_with_clipping``, HDU 0)
of the coverage map (``medncov`` = its ``gmed``), of the uncertainty image
(``medpixunc`` = its ``gmed``) and of the mosaic, whose saturation level
is `dev`'s stopgap ``sextractor_SATUR_LEVEL / 60.0`` ("The following is
incorrect (60 seconds), but used as a stopgap for now"). The names of the
returned fields are ``refimmeta``'s columns (R6).
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
        npixnan=int(stats_refimage["nancount"]),
        clmean=float(stats_refimage["clippedavg"]),
        clstddev=float(stats_refimage["clippedstd"]),
        clnoutliers=int(stats_refimage["noutliers"]),
        gmedian=float(stats_refimage["gmed"]),
        datascale=float(stats_refimage["gsigma"]),
        gmin=float(stats_refimage["gdatamin"]),
        gmax=float(stats_refimage["gdatamax"]),
    )
