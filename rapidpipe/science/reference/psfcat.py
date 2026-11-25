# ported-from: none
"""The reference mosaic's Photutils PSF-fit catalog and the two limiting magnitudes its grade reads.

Written from ``generatePhotUtilsReferenceImageCatalog`` and
``compute_refimage_maglimits`` (``pipeline/referenceImageSubs.py``). The
catalog is the difference stage's Photutils catalog
(:func:`rapidpipe.science.difference.psfcat.psf_catalog`) run on the
mosaic with its uncertainty map and the reference PSF, under
``[reference.psfcat]``, whose values are the ini's strings: a text
catalog, its finder catalog, the joined Parquet beside them and the fit's
residual image. No source surviving the finder is a catalog not made,
which sets InfoBit 16; any other failure raises.

The limiting magnitudes, both at ``[reference.infobits] maglim_snr`` on
the mosaic's zero point: ``maglimcat`` from the median ``flux_err`` of the
catalog's sources, and ``maglimit`` from the mosaic's clipped background
noise and the PSF's noise pixels (:mod:`rapidpipe.science.limmag`'s
formula, the mosaic already in DN/s). Either one that cannot be computed
is NaN and sets no bit.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import numpy as np
from astropy.io import fits

from rapidpipe.science.difference.psfcat import PsfCatalogSettings, psf_catalog
from rapidpipe.science.limmag import background_sigma, psf_noise_pixels

logger = logging.getLogger(__name__)


def settings_from_table(table: Mapping[str, Any]) -> PsfCatalogSettings:
    """``[reference.psfcat]``'s catalog values, parsed from the ini's strings.

    Parameters
    ----------
    table : mapping
        The run's ``[reference.psfcat]`` table.

    Returns
    -------
    PsfCatalogSettings
        The values :func:`rapidpipe.science.difference.psfcat.psf_catalog` reads.

    Raises
    ------
    ValueError
        A value that is not a number, or a ``fit_shape`` that is not two integers.
    """
    shape = tuple(int(x) for x in str(table["fit_shape"]).strip("()[] ").replace(" ", "").split(","))
    if len(shape) != 2:
        raise ValueError(f"[reference.psfcat] fit_shape must be two integers, got {table['fit_shape']!r}")
    return PsfCatalogSettings(
        n_clip_sigma=float(table["n_clip_sigma"]), n_thresh_sigma=float(table["n_thresh_sigma"]),
        fwhm=float(table["fwhm"]), fit_shape=shape, aperture_radius=float(table["aperture_radius"]),
        sharplo=float(table["sharplo"]), sharphi=float(table["sharphi"]),
        roundlo=float(table["roundlo"]), roundhi=float(table["roundhi"]),
        min_separation=float(table["min_separation"]))


@dataclass(frozen=True)
class ReferenceCatalog:
    """What the reference's PSF-fit catalog produced: its flag, source count and files (``None`` when not made)."""

    produced: bool
    nsources: int
    catalog: Path | None
    finder: Path | None
    parquet: Path | None


def reference_psf_catalog(table: Mapping[str, Any], out_dir: Path, image: Path, uncertainty: Path,
                          psf: Path, *, seed: int | None = None,
                          make: Callable = psf_catalog) -> ReferenceCatalog:
    """Make the mosaic's PSF-fit catalog under ``out_dir`` with the names ``[reference.psfcat]`` gives.

    Parameters
    ----------
    table : mapping
        The run's ``[reference.psfcat]`` table.
    out_dir : pathlib.Path
        Where the catalog, finder, Parquet and residual files are written.
    image, uncertainty : pathlib.Path
        The mosaic and its uncertainty map.
    psf : pathlib.Path
        The reference PSF, a normalised odd square stamp.
    seed : int or None
        The clipped statistics' seed (``[reference.statistics] clip_correction_seed``).
    make : callable
        :func:`rapidpipe.science.difference.psfcat.psf_catalog`, or a test's stand-in.

    Returns
    -------
    ReferenceCatalog
        ``produced`` false, no files and no sources when nothing survived the finder.
    """
    catalog = out_dir / table["output_psfcat_filename"]
    parquet = catalog.with_suffix(".parquet")
    result = make(settings_from_table(table), image=str(image), uncertainty=str(uncertainty),
                  psf=str(psf), sky_coords_image=str(image), catalog=str(catalog),
                  finder=str(out_dir / table["output_psfcat_finder_filename"]),
                  residual=str(out_dir / table["output_psfcat_residual_filename"]),
                  parquet=str(parquet), label="reference", seed=seed)
    if not result.produced:
        return ReferenceCatalog(produced=False, nsources=0, catalog=None, finder=None, parquet=None)
    return ReferenceCatalog(produced=True, nsources=int(result.nsources), catalog=catalog,
                            finder=out_dir / table["output_psfcat_finder_filename"], parquet=parquet)


def limiting_magnitudes(image: Path, psf: Path | None, catalog: Path | None, zprefimg: float,
                        snr: float = 5.0) -> tuple[float, float]:
    """The reference's two limiting magnitudes, ``(maglimcat, maglimit)``, each NaN when not computable.

    Parameters
    ----------
    image : pathlib.Path
        The mosaic, in DN/s.
    psf : pathlib.Path or None
        The reference PSF; none leaves ``maglimit`` NaN.
    catalog : pathlib.Path or None
        The PSF-fit text catalog with its ``flux_err`` column; none leaves ``maglimcat`` NaN.
    zprefimg : float
        The mosaic's zero point [AB mag, for flux in DN/s].
    snr : float
        The signal-to-noise ratio of the limit.

    Returns
    -------
    tuple of float
        ``zprefimg - 2.5 log10(snr * median flux_err)`` over the catalog's finite positive
        uncertainties, and ``zprefimg - 2.5 log10(snr * sqrt(noise pixels) * background sigma)``.
    """
    maglimcat = math.nan
    if catalog is not None:
        try:
            from astropy.io import ascii
            flux_err = np.asarray(ascii.read(catalog)["flux_err"], dtype=np.float64)
            flux_err = flux_err[np.isfinite(flux_err) & (flux_err > 0.0)]
            if len(flux_err) > 0:
                maglimcat = float(zprefimg - 2.5 * np.log10(snr * np.median(flux_err)))
        except Exception as exc:  # noqa: BLE001 -- an unreadable catalog leaves the metric unset
            logger.warning("reference: no catalog limiting magnitude from %s (%s)", catalog, exc)
    maglimit = math.nan
    if psf is not None:
        try:
            noise_pixels = psf_noise_pixels(fits.getdata(psf, 0))
            sigma = background_sigma(fits.getdata(image, 0))
            maglimit = float(zprefimg - 2.5 * math.log10(snr * math.sqrt(noise_pixels) * sigma))
        except Exception as exc:  # noqa: BLE001 -- an unmeasurable image leaves the metric unset
            logger.warning("reference: no formula limiting magnitude of %s (%s)", image, exc)
    return maglimcat, maglimit
