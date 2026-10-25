# ported-from: alerts/forced_phot.py @ 54fd7ea8
"""Forced PSF photometry at fixed sky positions, ported from `dev`'s ``alerts/forced_phot.py``.

`origin/dev`'s ``alerts/forced_phot.py`` (Emily Everetts, 09/2026), the
sections the packet stage needs, copied unchanged: the records
(``Position``, ``Measurement``), the footprint test
(:func:`contains_positions`), the job-directory names of the science
image, difference PSF and uncertainty, the photometry (:func:`open_image` to
:func:`forced_photometry`), and the table layout
(:data:`FORCED_TABLE_DTYPE`, :func:`forced_measurement_id`).

Not ported, because the packet stage replaces them (foundation plan v5,
"Forced photometry is a packet-set output with named inputs"): the Q3C
image search (``q3c_search``, ``find_prev_images``; the stage's window
images are its job's recorded inputs, tested here with
:func:`contains_positions`), the staging driver (``run_prev_images``; the
files are on this host), and the per-object month store
(``read_object_history`` to ``write_object_history``; a packet set's
``forced.parquet`` with its predecessors' as inputs replaces it).

Constants and their reasons are `dev`'s, below.
"""

import logging
from dataclasses import dataclass

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS
from photutils.background import (Background2D, MADStdBackgroundRMS,
                                  MedianBackground)

logger = logging.getLogger(__name__)

FIT_SHAPE = (7, 7)
STAMP_MARGIN_PX = 12


#: Background-subtracted science image in a difference-image job directory,
#: co-gridded with the difference image (the same product the cutouts use).
SCI_BASENAME = "bkg_subbed_science_image.fits"

#: Difference-image basename -> PSF basename in the same job directory.
#: (awsBatchSubmitJobs_runSingleSciencePipeline.py: sfft writes
#: sfftdiffpsf[_dconv].fits beside sfftdiffimage[_dconv]_masked.fits;
#: zogy writes diffpsf.fits.)
DIFF_PSF_BASENAMES = {
    "sfftdiffimage_masked.fits": "sfftdiffpsf.fits",
    "sfftdiffimage_dconv_masked.fits": "sfftdiffpsf_dconv.fits",
    "zogy_diffimage_masked.fits": "diffpsf.fits",
}

#: Difference-image basename -> its per-pixel uncertainty image in the same
#: job directory. The pipeline's own PSF photometry passes it as ``error``.
DIFF_UNCERT_BASENAMES = {
    "sfftdiffimage_masked.fits": "sfftdiffimage_uncert_masked.fits",
    "sfftdiffimage_dconv_masked.fits": "sfftdiffimage_uncert_masked.fits",
    "zogy_diffimage_masked.fits": "zogy_diffimage_uncert_masked.fits",
}


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Position:
    """A fixed sky position to measure, keyed by the caller's own id.

    ``pos_id`` is carried unchanged through every table the module builds;
    nothing downstream relies on array order.
    """
    pos_id: int
    ra: float
    dec: float

@dataclass
class Measurement:
    """One forced PSF measurement of one position on one image.

    A position that does not fall on an image gets no Measurement for it;
    a position that does but whose fit fails gets one with NaN flux and a
    non-zero ``flags``. ``x``, ``y`` are the 1-based pixel coordinates the
    fit was made at. ``product`` says which image of the epoch was measured
    (``"diff"`` -> psfFlux, ``"science"`` -> scienceFlux); ``mjdobs`` is
    stamped by the driver from the epoch.
    """
    pos_id: int
    rid: int
    x: float
    y: float
    flux: float
    fluxerr: float
    flags: int = 0
    product: str = "diff"
    mjdobs: float = float("nan")


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def _unit_vectors(ra, dec):
    """(..., 3) unit vectors for RA/Dec in degrees."""
    ra = np.radians(np.asarray(ra, dtype=float))
    dec = np.radians(np.asarray(dec, dtype=float))
    cos_dec = np.cos(dec)
    return np.stack([cos_dec * np.cos(ra), cos_dec * np.sin(ra), np.sin(dec)], axis=-1)


def contains_positions(corners, ra, dec):
    """Which footprints contain which sky positions.

    Parameters
    ----------
    corners : array_like, shape (n_images, 8)
        Footprint corners per image in database order
        ``ra1, dec1, ra2, dec2, ra3, dec3, ra4, dec4`` (perimeter order).
    ra, dec : array_like, shape (n_positions,)
        Positions to test, degrees.

    Returns
    -------
    numpy.ndarray of bool, shape (n_images, n_positions)
        ``[i, j]`` is True when position ``j`` is strictly inside footprint
        ``i``.

    Notes
    -----
    Exact on the sphere: a point is inside a convex spherical quadrilateral
    when it lies on the same side of all four edge great-circle planes and
    in the footprint's own hemisphere. Works in unit vectors throughout, so
    it is safe across the RA wrap and at the poles, where RA arithmetic is
    not. The hemisphere test is required: the same-side test alone also
    admits the antipode of every interior point.

    Edges are the great circles through adjacent corners. The true chip
    edge bows away from that by up to ~3 px (SIP distortion, measured on
    a real header), so a point within a few pixels of an edge may be
    misclassified here; the pixel-space margin applied after WCS
    projection makes the final call for those.

    A point exactly on an edge is not "inside".
    """
    corners = np.asarray(corners, dtype=float)
    if corners.ndim != 2 or corners.shape[1] != 8:
        raise ValueError(f"corners must have shape (n_images, 8), got {corners.shape}")
    n_images = corners.shape[0]
    points = np.atleast_1d(_unit_vectors(ra, dec)).reshape(-1, 3)
    if n_images == 0 or len(points) == 0:
        return np.zeros((n_images, len(points)), dtype=bool)

    vertices = _unit_vectors(corners[:, 0::2], corners[:, 1::2])          # (n, 4, 3)
    edge_normals = np.cross(vertices, np.roll(vertices, -1, axis=1))      # (n, 4, 3)
    # footprint "center" = mean of the corner vectors; only its hemisphere
    # matters, so it need not be normalized
    centers = vertices.sum(axis=1)                                         # (n, 3)

    sides = np.einsum("nkj,mj->nmk", edge_normals, points)                # (n, m, 4)
    same_side = np.all(sides > 0.0, axis=2) | np.all(sides < 0.0, axis=2)
    same_hemisphere = (centers @ points.T) > 0.0                           # (n, m)
    return same_side & same_hemisphere


# ---------------------------------------------------------------------------
# Photometry
# ---------------------------------------------------------------------------

#: Box size (pixels) for the background / rms map in error_array().
ERROR_BOX_PX = 64


@dataclass
class ChipImage:
    """One staged image, opened: pixels, header, WCS, and (optionally) its
    per-pixel uncertainty.

    ``mask`` is True where a pixel cannot be used (non-finite data, or
    non-finite / non-positive uncertainty when one was supplied). ``error``
    is None when no uncertainty file was given; see :func:`error_array`.
    """
    path: str
    data: np.ndarray
    header: fits.Header
    wcs: WCS
    mask: np.ndarray
    error: np.ndarray | None = None

    @property
    def shape(self):
        return self.data.shape


def _first_image_hdu(path):
    """(data, header) of the first HDU with 2-D pixel data. Primary for the
    pipeline products; Roman L2 cal files keep pixels in a SCI extension."""
    with fits.open(path, memmap=False) as hdus:
        for hdu in hdus:
            if hdu.data is not None and hdu.data.ndim == 2:
                return np.asarray(hdu.data, dtype=np.float64), hdu.header.copy()
    raise ValueError(f"no 2-D image HDU in {path}")


def open_image(path, uncert_path=None):
    """Open a staged image for photometry.

    Parameters
    ----------
    path : str
        Local FITS file (difference or science image).
    uncert_path : str, optional
        Local FITS file holding the matching per-pixel uncertainty, same
        shape. When omitted ``error`` is None and the caller falls back on
        :func:`error_array`. This is the seam for the pipeline's own
        uncertainty products once they exist: pass the file, nothing else
        changes.

    Returns
    -------
    ChipImage
    """
    data, header = _first_image_hdu(path)
    mask = ~np.isfinite(data)
    error = None
    if uncert_path is not None:
        error, _ = _first_image_hdu(uncert_path)
        if error.shape != data.shape:
            raise ValueError(f"uncertainty {uncert_path} has shape {error.shape}, "
                             f"image {path} has {data.shape}")
        mask |= ~np.isfinite(error) | (error <= 0.0)
    wcs = WCS(header)
    logger.debug("open_image: %s %s, %d masked px, error %s",
                 path, data.shape, int(mask.sum()), "file" if error is not None else "none")
    return ChipImage(path=str(path), data=data, header=header, wcs=wcs, mask=mask, error=error)


def error_array(data, product, gain=None, mask=None, box_size=ERROR_BOX_PX):
    """Fallback per-pixel uncertainty for an image whose pipeline
    uncertainty product (PrevImage.diff_uncert / sci_uncert) is missing.

    The normal path is ``open_image(..., uncert_path=...)`` with that
    product, as the pipeline's own PSF photometry does; nothing else
    depends on how the error was obtained.

    Parameters
    ----------
    data : numpy.ndarray
        Image pixels.
    product : {"diff", "science"}
        ``"diff"``: the local background rms alone. The subtraction noise
        from both inputs is already in the residuals; what is missed is the
        extra Poisson noise under bright stars.
        ``"science"``: background rms plus the Poisson term of the
        background-subtracted signal, which needs ``gain``.
    gain : float, optional
        Electrons per data unit, for the Poisson term. When None for a
        science image the term is skipped and a warning logged.
    mask : numpy.ndarray of bool, optional
        Pixels to leave out of the background estimate (non-finite pixels
        are always left out). Masked pixels get NaN error.
    box_size : int
        Side of the boxes the background and rms are estimated in.

    Returns
    -------
    numpy.ndarray of float32, same shape as ``data``
        Positive error per pixel, NaN where masked.

    Notes
    -----
    Background and rms come from a sigma-clipped median and MAD per box
    (robust in crowded fields), median-filtered over 3x3 boxes and
    interpolated back to the pixel grid, i.e. photutils ``Background2D``.
    """
    if product not in ("diff", "science"):
        raise ValueError(f"product must be 'diff' or 'science', got {product!r}")
    data = np.asarray(data, dtype=np.float64)
    bad = ~np.isfinite(data)
    if mask is not None:
        bad |= np.asarray(mask, dtype=bool)
    try:
        # No sigma clipping: photutils counts clipped pixels towards a box's
        # exclusion, and a difference image's heavy tails clip >10% of every
        # box (16% measured on a real sfft image, 2026-10-05), which at the
        # default exclude_percentile rejects every box. The median/MAD
        # estimators are robust without the clip, and keeping the default
        # exclusion means a box is still dropped when it is mostly masked.
        bkg = Background2D(data, box_size, mask=bad, filter_size=(3, 3),
                           sigma_clip=None,
                           bkg_estimator=MedianBackground(),
                           bkg_rms_estimator=MADStdBackgroundRMS())
        background = bkg.background.astype(np.float64)
        variance = bkg.background_rms.astype(np.float64) ** 2
    except ValueError as exc:
        # e.g. an image too small or too masked for any box: one global
        # robust level and rms, which still gives sane fit weights. With no
        # good pixel there is no level, and an all-NaN error would leave
        # every forced row without a value: it raises (err-edges-sci).
        from astropy.stats import mad_std
        good = data[~bad]
        if good.size == 0:
            raise ValueError(f"error_array: no good pixel for a background in a "
                             f"{data.shape} image") from exc
        level, rms = float(np.median(good)), float(mad_std(good))
        logger.warning("error_array: Background2D failed (%s); using a global level %.3g, rms %.3g",
                       str(exc).split("(")[0].strip(), level, rms)
        background = np.full(data.shape, level)
        variance = np.full(data.shape, rms ** 2)
    if product == "science":
        if gain is None:
            logger.warning("error_array: science image without gain; Poisson term skipped")
        else:
            variance += np.clip(data - background, 0.0, None) / float(gain)
    error = np.sqrt(variance).astype(np.float32)
    error[bad] = np.nan
    return error


#: Measurement.flags bit set by this module: the fit returned a non-finite
#: flux or uncertainty. photutils 3.0 uses bits up to 1 << 11 (see the
#: bit list in param_registry.py), so ours start at 1 << 16.
FLAG_NONFINITE = 1 << 16


def project_positions(wcs, shape, positions, margin=STAMP_MARGIN_PX):
    """Sky positions to 1-based pixel coordinates, and which are usable.

    Parameters
    ----------
    wcs : astropy.wcs.WCS
    shape : tuple of int
        Image shape ``(ny, nx)``.
    positions : sequence of Position
    margin : int
        A position within ``margin`` pixels of any edge is off-chip: its
        fit region would run off the image.

    Returns
    -------
    x, y : numpy.ndarray of float
        1-based pixel coordinates (FITS convention), distortion included.
    on_chip : numpy.ndarray of bool
        Finite and inside ``[1 + margin, n - margin]`` on both axes.

    Notes
    -----
    The inverse of a distorted WCS is iterative; positions far outside
    the chip may not converge and come back non-finite, which counts as
    off-chip. Positions reaching here have passed the footprint test, so
    that is rare and only ever at the edges.
    """
    ra = np.array([p.ra for p in positions], dtype=float)
    dec = np.array([p.dec for p in positions], dtype=float)
    if len(ra) == 0:
        empty = np.zeros(0, dtype=float)
        return empty, empty.copy(), np.zeros(0, dtype=bool)
    x, y = wcs.all_world2pix(ra, dec, 1, quiet=True)
    x = np.asarray(x, dtype=float); y = np.asarray(y, dtype=float)
    ny, nx = shape
    on_chip = (np.isfinite(x) & np.isfinite(y)
               & (x >= 1 + margin) & (x <= nx - margin)
               & (y >= 1 + margin) & (y <= ny - margin))
    return x, y, on_chip


def load_psf(path, oversampling=1):
    """The photutils PSF model for forced fitting: an ImagePSF built from
    a PSF image, normalized to unit sum, with its position fixed so only
    the flux is fit.

    Parameters
    ----------
    path : str
        Local FITS file holding the PSF image (the job-directory PSF
        products are detector-sampled, so ``oversampling`` is 1).
    """
    from photutils.psf import ImagePSF

    psf_data, _ = _first_image_hdu(path)
    total = float(np.nansum(psf_data))
    if not np.isfinite(total) or total <= 0.0:
        raise ValueError(f"PSF {path} has non-positive total {total}")
    psf = ImagePSF(np.nan_to_num(psf_data) / total, flux=1.0, x_0=0.0, y_0=0.0,
                   oversampling=oversampling)
    psf.x_0.fixed = True
    psf.y_0.fixed = True
    return psf


def psfphot(data, error, mask, psf, x, y, fit_shape=FIT_SHAPE):
    """Forced PSF fluxes at fixed pixel positions: one vectorized
    PSFPhotometry call over every position.

    Parameters
    ----------
    data, error, mask : numpy.ndarray
        Image, its per-pixel uncertainty, and the unusable-pixel mask.
    psf : ImagePSF
        From :func:`load_psf`, positions fixed.
    x, y : array_like
        1-based pixel positions to fit.
    fit_shape : tuple of int
        Fit region around each position.

    Returns
    -------
    flux, fluxerr : numpy.ndarray of float
        In the order of ``x``; NaN where the fit failed.
    flags : numpy.ndarray of int
        photutils' flags for the fit, plus ``FLAG_NONFINITE`` when the
        flux or its error is not finite.
    """
    from astropy.table import QTable
    from photutils.psf import PSFPhotometry

    x = np.asarray(x, dtype=float); y = np.asarray(y, dtype=float)
    if len(x) == 0:
        empty = np.zeros(0, dtype=float)
        return empty, empty.copy(), np.zeros(0, dtype=int)
    # photutils works in 0-based array coordinates
    init = QTable({"x_init": x - 1.0, "y_init": y - 1.0})
    phot = PSFPhotometry(psf_model=psf, fit_shape=fit_shape,
                         aperture_radius=max(fit_shape) / 2.0 + 0.5)
    table = phot(data, error=error, mask=mask, init_params=init)
    flux = np.asarray(table["flux_fit"], dtype=float)
    fluxerr = np.asarray(table["flux_err"], dtype=float)
    flags = np.asarray(table["flags"], dtype=int)
    bad = ~np.isfinite(flux) | ~np.isfinite(fluxerr)
    flags[bad] |= FLAG_NONFINITE
    return flux, fluxerr, flags


def forced_photometry(image_path, psf_path, positions, rid, product, uncert_path=None,
                      gain=None, fit_shape=FIT_SHAPE, margin=STAMP_MARGIN_PX):
    """Forced PSF photometry of one staged image at a set of sky positions.

    Parameters
    ----------
    image_path, psf_path : str
        Local FITS files: the image and its PSF.
    positions : sequence of Position
        Sky positions to measure; only those landing on the chip get a row.
    rid : int
        Identifier stamped on every row (the image's ``rid``).
    product : {"diff", "science"}
        Passed to :func:`error_array` when no uncertainty file is given.
    uncert_path : str, optional
        Per-pixel uncertainty file; see :func:`open_image`.
    gain : float, optional
        For the science-image Poisson term when the error is estimated.
        Defaults to the header's ``GAIN`` when present.
    fit_shape, margin
        See :func:`psfphot` and :func:`project_positions`.

    Returns
    -------
    list of Measurement
        One per position that lands on the chip, in input order. A
        position off the chip has no row; a position on the chip whose
        fit fails has a row with NaN flux and non-zero flags.
    """
    positions = list(positions)
    image = open_image(image_path, uncert_path=uncert_path)
    error = image.error
    if error is None:
        if gain is None:
            gain = image.header.get("GAIN")
        error = error_array(image.data, product, gain=gain, mask=image.mask)
    x, y, on_chip = project_positions(image.wcs, image.shape, positions, margin=margin)
    idx = np.flatnonzero(on_chip)
    if len(idx) == 0:
        logger.info("forced_photometry: rid=%s none of %d positions on %s", rid, len(positions), image_path)
        return []
    psf = load_psf(psf_path)
    flux, fluxerr, flags = psfphot(image.data, error, image.mask, psf, x[idx], y[idx], fit_shape=fit_shape)
    rows = [Measurement(pos_id=positions[i].pos_id, rid=int(rid), x=float(x[i]), y=float(y[i]),
                        flux=float(f), fluxerr=float(e), flags=int(g))
            for i, f, e, g in zip(idx, flux, fluxerr, flags)]
    logger.info("forced_photometry: rid=%s %d of %d positions on chip, %d flagged",
                rid, len(rows), len(positions), int((flags != 0).sum()))
    return rows


# ---------------------------------------------------------------------------
# Table: one row per object and epoch, both products merged
# ---------------------------------------------------------------------------

#: Columns of the forced-photometry table.
FORCED_TABLE_DTYPE = np.dtype([
    ("forced_id", "i8"), ("aid", "i8"), ("rid", "i8"), ("pid", "i8"),
    ("expid", "i8"), ("sca", "i2"), ("fid", "i2"), ("band", "U8"),
    ("mjdobs", "f8"), ("ra", "f8"), ("dec", "f8"), ("x", "f4"), ("y", "f4"),
    ("psf_flux", "f8"), ("psf_fluxerr", "f8"),
    ("science_flux", "f8"), ("science_fluxerr", "f8"),
    ("flags", "i4"), ("time_proc", "f8"),
])


def forced_measurement_id(rid, aid, ra, dec):
    """63-bit id of one forced measurement: a hash of the epoch, the object
    and the position actually used (to 0.1 mas).

    Deterministic, so re-reading a parquet file or loading it into a table
    is idempotent; position-dependent, so a re-run at an updated object
    position is a new measurement with a new id.
    """
    import hashlib

    key = f"{int(rid)}:{int(aid)}:{float(ra):.7f}:{float(dec):.7f}".encode()
    return int.from_bytes(hashlib.blake2b(key, digest_size=8).digest(), "big") & (2**63 - 1)
