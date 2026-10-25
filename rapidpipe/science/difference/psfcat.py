# ported-from: modules/utils/rapid_pipeline_subs.py, pipeline/awsBatchSubmitJobs_runSingleSciencePipeline.py @ d6aac515
"""The Photutils PSF-fit catalog of a difference image.

`dev`: ``compute_psf_catalog`` (``modules/utils/rapid_pipeline_subs.py``)
and the catalog-writing block repeated inline for each difference image
and sign in ``awsBatchSubmitJobs_runSingleSciencePipeline.py``. DAOStarFinder
detects above ``n_thresh_sigma`` times the image's clipped standard
deviation; PSFPhotometry fits the given PSF with the uncertainty image as
error, the image's non-finite pixels filled for the finder and masked
(not dev). When no source survives the finder the catalog is absent and
the caller sets that difference image's catalog-outcome bit; any other
failure (the constructor, the fit, a product write) raises and fails the
job (not dev, whose bare excepts logged and continued; err-edges-sci).

photutils, pandas and pyarrow are imported inside the functions that use
them: only the stage's own run needs them, and the unit-test job does not
install them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Sequence

import numpy as np
from astropy.io import fits

from rapidpipe.science.difference.statistics import fits_data_statistics_with_clipping

logger = logging.getLogger(__name__)


def compute_psf_catalog(n_clip_sigma,
                        n_thresh_sigma,
                        fwhm,
                        fit_shape,
                        aperture_radius,
                        input_img_filename,
                        input_unc_filename,
                        input_psf_filename,
                        output_psfcat_residual_filename,
                        sharplo=0.2,
                        sharphi=1.0,
                        roundlo=-1.0,
                        roundhi=1.0,
                        min_separation=0.0,
                        seed: int | None = None):
    """`dev` ``compute_psf_catalog``: returns ``(phot, psfphot)``, ``phot`` ``None`` when empty.

    `dev` returns ``(psfcat_flag, phot, psfphot)``; its flag was false only
    when its bare except caught a constructor error, which now propagates.
    """
    import photutils
    photutils.future_column_names = True
    from astropy.convolution import Gaussian2DKernel, interpolate_replace_nans
    from photutils.detection import DAOStarFinder
    from photutils.psf import ImagePSF, PSFPhotometry

    stats_image = fits_data_statistics_with_clipping(
        input_img_filename, n_clip_sigma, 0, 999999, seed=seed)
    std_image = stats_image["clippedstd"]
    logger.info("Image-data statistics: clippedavg, gmed, clippedstd = %s",
                np.array((stats_image["clippedavg"], stats_image["gmed"], std_image)))

    threshold = n_thresh_sigma * std_image
    logger.info("threshold = %s", threshold)

    with fits.open(input_img_filename) as hdul_image:
        data_image = np.array(hdul_image[0].data)
    with fits.open(input_unc_filename) as hdul_uncert:
        data_uncert = np.array(hdul_uncert[0].data)
    with fits.open(input_psf_filename) as hdul_psf:
        hdr_psf = hdul_psf[0].header
        data_psf_np = np.array(hdul_psf[0].data)

    naxis1 = hdr_psf["NAXIS1"]
    naxis2 = hdr_psf["NAXIS2"]

    # Zero-based pixel coordinates of the PSF centre.
    x_0 = (naxis1 - 1) / 2
    y_0 = (naxis2 - 1) / 2

    data_psf_sum = np.sum(data_psf_np)
    psf_model = ImagePSF(data_psf_np, flux=data_psf_sum, x_0=x_0, y_0=y_0)

    finder = DAOStarFinder(threshold=threshold,
                           fwhm=fwhm,
                           sharplo=sharplo,
                           sharphi=sharphi,
                           roundlo=roundlo,
                           roundhi=roundhi,
                           min_separation=min_separation)

    # dev wraps the constructor in a bare except whose warning was dead: its
    # next line read psfphot.__dict__ and raised AttributeError on None. The
    # constructor's own error now propagates (err-edges-sci).
    psfphot = PSFPhotometry(psf_model=psf_model, fit_shape=fit_shape, finder=finder,
                            aperture_radius=aperture_radius)
    logger.info("psfphot_attributes = %s", psfphot.__dict__.keys())

    # dev catches every exception here. photutils 3.0 returns None for every
    # empty input probed (no sources found, all NaN, all zero; err-inputs,
    # 2026-10-24) and raises only for faults, a ValueError among them, so
    # nothing is caught: a fault fails the job instead of publishing an empty
    # source set.
    # Not dev: the non-finite pixels are filled from their neighbours and
    # passed as the mask. DAOStarFinder convolves and measures with NaNs in
    # place, mask or not, and drops a star whose moments come out
    # non-finite: a star within about 4 px of one masked pixel was never
    # found (photutils 3.0.0, finder-mask, 2026-10-24). The fit ignores the
    # masked pixels; a hole wider than the kernel is zero. A zero fill alone
    # split a star with a masked peak into four rows.
    nonfinite = ~np.isfinite(data_image)
    filled = interpolate_replace_nans(data_image, Gaussian2DKernel(1))
    filled[~np.isfinite(filled)] = 0.0
    phot = psfphot(data=filled, mask=nonfinite, error=data_uncert)

    # dev's bare except here hid both the empty case (photutils raises "No
    # results available" for a residual of an empty fit) and a failed write;
    # the empty case writes no residual, a failed write raises (err-edges-sci).
    if phot is not None:
        resid = psfphot.make_residual_image(data_image)
        fits.PrimaryHDU(data=resid.astype(np.float32)).writeto(
            output_psfcat_residual_filename, overwrite=True, checksum=True)

    return phot, psfphot


def write_psf_catalog_products(phot, psfphot, sky_coords_image, output_psfcat_filename,
                               output_psfcat_finder_filename, output_psfcat_parquet_filename) -> None:
    """`dev`'s catalog-writing block: formats, RA/Dec, text catalogs, and parquet.

    ``output_psfcat_parquet_filename`` is ``None`` for the naive catalogs,
    which `dev` writes without a parquet file. `dev` logs and swallows any
    error here; an error now propagates and fails the job, so a catalog the
    stage records is a catalog on disk (err-edges-sci).
    """
    from astropy.io import ascii
    from astropy.table import join

    phot["x_init"].info.format = ".4f"
    phot["y_init"].info.format = ".4f"
    phot["flux_init"].info.format = ".6f"
    phot["flux_fit"].info.format = ".6f"
    phot["x_err"].info.format = ".4f"
    phot["y_err"].info.format = ".4f"
    phot["flux_err"].info.format = ".5f"
    phot["qfit"].info.format = ".4f"
    phot["cfit"].info.format = ".4f"

    ra, dec = compute_sky_coords_from_pixel_coords(
        sky_coords_image, list(phot["x_fit"]), list(phot["y_fit"]))

    phot["x_fit"].info.format = ".4f"
    phot["y_fit"].info.format = ".4f"
    phot.add_column(ra, name="ra")
    phot.add_column(dec, name="dec")

    ascii.write(phot, output_psfcat_filename, overwrite=True)
    ascii.write(psfphot.finder_results, output_psfcat_finder_filename, overwrite=True)

    if output_psfcat_parquet_filename is not None:
        joined_table_inner = join(phot, psfphot.finder_results, keys="id", join_type="inner")
        logger.info("nrows in PSF-fit catalog = %s", len(joined_table_inner))
        joined_table_inner.to_pandas().to_parquet(output_psfcat_parquet_filename, engine="pyarrow")


def compute_sky_coords_from_pixel_coords(filename_sciimage_image, x_list, y_list, hdu_index=0):
    """`dev` ``computeSkyCoordsFromPixelCoords``: zero-based pixels to (ra, dec) lists."""
    from astropy.wcs import WCS

    with fits.open(filename_sciimage_image) as hdul:
        w = WCS(hdul[hdu_index].header)

    ra = []
    dec = []
    for x, y in zip(x_list, y_list):
        celestial_coords = w.pixel_to_world(x, y)
        ra.append(celestial_coords.ra.deg)
        dec.append(celestial_coords.dec.deg)
    return ra, dec


@dataclass(frozen=True)
class PsfCatalogSettings:
    """The ``[psfcat_diffimage]`` settings `dev` reads for every catalog."""

    n_clip_sigma: float
    n_thresh_sigma: float
    fwhm: float
    fit_shape: tuple[int, int]
    aperture_radius: float
    sharplo: float
    sharphi: float
    roundlo: float
    roundhi: float
    min_separation: float

    @classmethod
    def from_table(cls, table: dict[str, Any]) -> "PsfCatalogSettings":
        return cls(
            n_clip_sigma=float(table["n_clip_sigma"]),
            n_thresh_sigma=float(table["n_thresh_sigma"]),
            fwhm=float(table["fwhm"]),
            fit_shape=tuple(int(x) for x in table["fit_shape"]),
            aperture_radius=float(table["aperture_radius"]),
            sharplo=float(table["sharplo"]),
            sharphi=float(table["sharphi"]),
            roundlo=float(table["roundlo"]),
            roundhi=float(table["roundhi"]),
            min_separation=float(table["min_separation"]),
        )


#: The injectable signature of :func:`psf_catalog`, for the stage and its tests.
PsfCatalogFunction = Callable[..., "PsfCatalogResult"]


@dataclass(frozen=True)
class PsfCatalogResult:
    """What one Photutils catalog call produced: the flag and the source count."""

    produced: bool
    nsources: int | None


def psf_catalog(settings: PsfCatalogSettings,
                *,
                image: str,
                uncertainty: str,
                psf: str,
                sky_coords_image: str,
                catalog: str,
                finder: str,
                residual: str,
                parquet: str | None,
                label: str,
                seed: int | None = None) -> PsfCatalogResult:
    """One `dev` catalog block: fit, then write the products when the fit was made."""
    phot, psfphot = compute_psf_catalog(
        settings.n_clip_sigma, settings.n_thresh_sigma, settings.fwhm, settings.fit_shape,
        settings.aperture_radius, image, uncertainty, psf, residual,
        sharplo=settings.sharplo, sharphi=settings.sharphi, roundlo=settings.roundlo,
        roundhi=settings.roundhi, min_separation=settings.min_separation, seed=seed)
    # photutils' PSFPhotometry.__call__ returns None, not an exception, when
    # DAOStarFinder detects candidate sources but none survive its
    # sharpness/roundness filter (a real, valid outcome on a near-empty
    # image -- confirmed live, LEDGER-fixture-real.md 2026-09-23): the
    # catalog is absent and the caller sets that difference image's
    # catalog-outcome bit.
    if phot is None:
        logger.warning("*** Warning: Could not make psf-fit catalog (no sources passed "
                       "filtering); continuing...")
        return PsfCatalogResult(produced=False, nsources=None)
    nsources = len(phot)
    logger.info("npsfcatsources (%s) = %s", label, nsources)
    write_psf_catalog_products(phot, psfphot, sky_coords_image, catalog, finder, parquet)
    return PsfCatalogResult(produced=True, nsources=nsources)


__all__: Sequence[str] = (
    "PsfCatalogResult", "PsfCatalogSettings", "compute_psf_catalog",
    "compute_sky_coords_from_pixel_coords", "psf_catalog", "write_psf_catalog_products",
)
