"""
File:     forced_phot.py
Author:   Emily Everetts, Claude Fable 5.1
Date:     09/2026

Forced PSF photometry at fixed sky positions across the previous images
that contain them, one chip at a time.

The module is a chain of pure functions. The database and S3 are reached
only through two callables handed in by the caller, ``query(sql, params)``
and ``stage(url)``, so the same code runs against the live database, the
test-suite ``FakeDB`` or a directory of local files, and the caller decides
whether results are written to a table or straight into alert packets.

Layout (top to bottom, each section depends only on the ones above it)::

    records      Position, PrevImage, Measurement
    geometry     contains_positions()      -- the one geometric predicate
    search       q3c_search()              -- coarse: Q3C cone on diffimages
                 find_prev_images()        -- parent: cone + predicate
                 (science image and both PSFs are named by convention in
                  the job directory: sci_psf_basename, diff_psf_basename)
    photometry   open_image()              -- FITS -> data, header, WCS
                 error_array()             -- stopgap per-pixel uncertainty
                 project_positions()       -- sky -> pixel, on-chip mask
                 load_psf()                -- ImagePSF with x, y fixed
                 psfphot()                 -- one vectorized PSFPhotometry call
                 forced_photometry()       -- parent: one image, N positions
    driver       run_prev_images()         -- stage, measure, yield, discard
                 assemble_history()        -- rows -> per-position lists
    table        FORCED_TABLE_DTYPE, forced_measurement_id()
                 write_table() / read_table()   -- parquet, local or s3://
    entry point  main()                    -- one chip by hand: --pid, --positions CSV

Search (data flow)::

    positions (frozen object positions on the alerting chip)
      -> q3c_search:   diffimages JOIN l2files, center within CONE_RADIUS_DEG
                       of the chip center, status/vbest/band/window cuts
      -> contains_positions:  which of those images hold which positions
      -> PrevImage list, each with its position indices; an image that holds
         no position is never staged

Why a cone on image centers is complete: an image that contains a point
has its center within one SCA half-diagonal of that point, so a cone of
that radius (plus padding) around the point cannot miss it. The same
argument with two half-diagonals covers footprint-against-footprint. Both
were checked against brute force over every footprint in the database
and against synthetic rolled footprints (2026-09-23).

Positions are fixed. Each object is measured at one frozen position for
its whole history so that the (sub-percent) flux bias from a position
error is a constant per object rather than varying epoch to epoch.

Attributes
----------
SCA_HALF_DIAGONAL_DEG : float
    Largest center-to-corner distance of any SCA footprint in the database
    (0.0885 deg measured over 434,677 rows). Every point of a footprint lies
    within this radius of its center.
CONE_RADIUS_DEG : float
    Q3C cone radius around the alerting chip center when searching for
    previous images. Must exceed ``2 * SCA_HALF_DIAGONAL_DEG`` (0.177 deg,
    a tight bound: synthetic footprints put a true overlap at 0.1755 deg).
    0.2 deg gives 13% padding.
FIT_SHAPE : tuple of int
    Pixel region fitted around each position by PSFPhotometry.
STAMP_MARGIN_PX : int
    A position closer than this to a chip edge is treated as off the image.
"""

# Index note (2026-09-29): q3c_search relies on the Q3C index the schema
# declares on diffimages (q3c_ang2ipix(ra0, dec0)). Verified present as
# diffimages_radec_idxx in the deployed database (socsimsemily1) and used by
# the planner (bitmap index scans). Re-check after any schema rebuild --
# the deployed database was found (2026-09-23) to lack every index declared
# on l2filemeta -- with
#   select indexdef from pg_indexes where tablename = 'diffimages';

import logging
import os
from dataclasses import dataclass, field

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS
from photutils.background import (Background2D, MADStdBackgroundRMS,
                                  MedianBackground)

logger = logging.getLogger(__name__)

SCA_HALF_DIAGONAL_DEG = 0.0885
CONE_RADIUS_DEG = 0.2
FIT_SHAPE = (7, 7)
STAMP_MARGIN_PX = 12


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
class PrevImage:
    """One previous epoch of the alerting chip: a difference image and the
    science image it was made from (same ``rid``, same footprint).

    ``corners`` are the four footprint corners in database order
    ``(ra1, dec1, ra2, dec2, ra3, dec3, ra4, dec4)``, which walk the
    perimeter (pixel-space lower-left, lower-right, upper-right, upper-left
    of the raw SCA; not sky directions -- chips are rolled).

    ``diff_filename`` comes from the database; ``sci_filename``,
    ``diff_psf``, ``sci_psf``, ``diff_uncert`` and ``sci_uncert`` are the
    job-directory products named by convention (see the ``*_basename``
    helpers); ``l2_filename`` is the raw L2 file the science image was
    made from.

    ``position_index`` is filled by :func:`find_prev_images`: indices into
    the caller's position list of the positions this image contains.
    """
    rid: int
    pid: int
    diff_filename: str
    sci_filename: str
    diff_psf: str
    sci_psf: str
    diff_uncert: str
    sci_uncert: str
    l2_filename: str
    mjdobs: float
    fid: int
    band: str | None
    expid: int
    sca: int
    corners: tuple[float, ...]            # exactly 8 values
    position_index: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=int))


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
# Search
# ---------------------------------------------------------------------------

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
#: job directory (awsBatchSubmitJobs_runSingleSciencePipeline.py: the
#: masked basename with "uncert_" inserted; the sfft branch writes
#: sfftdiffimage_uncert_masked.fits whether or not cross-convolution was
#: on). Built by differenceImageSubs.compute_diffimage_uncertainty: the
#: science and reference Poisson terms plus the difference image's clipped
#: scatter. The pipeline's own PSF photometry passes it as ``error``.
DIFF_UNCERT_BASENAMES = {
    "sfftdiffimage_masked.fits": "sfftdiffimage_uncert_masked.fits",
    "sfftdiffimage_dconv_masked.fits": "sfftdiffimage_uncert_masked.fits",
    "zogy_diffimage_masked.fits": "zogy_diffimage_uncert_masked.fits",
}

#: Science-image uncertainty in the job directory: the L2 file's ERR
#: array, reformatted beside the science image as
#: <l2 basename>_reformatted_unc.fits (e.g. r0034..._f146_cal_lite_reformatted_unc.fits).
SCI_UNCERT_SUFFIX = "_reformatted_unc.fits"

#: Science-image PSF in the job directory: the psfs-table file for the
#: (filter, SCA), normalized by the science pipeline, e.g.
#: sciimage_psf_f146_sca09_normalized.fits (socsim jid145793). The filter
#: token is the Roman designation in lower case, not the RAPID name the
#: filters table uses. The WebbPSF-library family (WFI_SCA<nn>_F<nnn>_
#: PSF_DET_DIST) found in older job directories is no longer used
#: (team decision, 2026-10-06): a job directory without this file simply
#: has no science-image forced photometry for that epoch.
SCI_PSF_PATTERN = "sciimage_psf_{roman_lower}_sca{sca:02d}_normalized.fits"

#: RAPID filter name (filters.filter) -> Roman designation used in PSF
#: filenames. Same table as database/scripts/db_register_sciimg_psfs.py.
ROMAN_FILTER_TOKENS = {
    "R062": "F062", "Z087": "F087", "Y106": "F106", "J129": "F129",
    "W146": "F146", "H158": "F158", "F184": "F184", "K213": "F213",
}

# Also present in those job directories, not yet used here:
#   sfftdiffimage_uncert_masked.fits, zogy_diffimage_uncert_masked.fits,
#   <l2 basename>_reformatted_unc.fits (science),
#   awaicgen_output_mosaic_uncert_image_resampled_gainmatched.fits (ref).
# Whether these are trustworthy decides if error_array() is needed at all.

_CORNER_COLUMNS = "ra1, dec1, ra2, dec2, ra3, dec3, ra4, dec4"


def sci_psf_basename(band, sca):
    """Job-directory basename of the normalized science-image PSF for a
    RAPID filter name (e.g. ``"W146"``) and SCA number.

    Raises ``ValueError`` for a filter name the table does not know: a
    wrong guess here would stage a missing file, so fail before staging.
    """
    try:
        roman = ROMAN_FILTER_TOKENS[str(band)]
    except KeyError:
        raise ValueError(f"no Roman filter token for RAPID filter {band!r}; "
                         f"known: {sorted(ROMAN_FILTER_TOKENS)}") from None
    return SCI_PSF_PATTERN.format(sca=int(sca), roman_lower=roman.lower())


def diff_psf_basename(diff_basename):
    """Job-directory basename of the PSF paired with a difference image."""
    try:
        return DIFF_PSF_BASENAMES[diff_basename]
    except KeyError:
        raise ValueError(f"no PSF known for difference image {diff_basename!r}; "
                         f"known: {sorted(DIFF_PSF_BASENAMES)}") from None


def diff_uncert_basename(diff_basename):
    """Job-directory basename of the uncertainty image of a difference image."""
    try:
        return DIFF_UNCERT_BASENAMES[diff_basename]
    except KeyError:
        raise ValueError(f"no uncertainty image known for difference image {diff_basename!r}; "
                         f"known: {sorted(DIFF_UNCERT_BASENAMES)}") from None


def sci_uncert_basename(l2_filename):
    """Job-directory basename of the science image's uncertainty, from the
    L2 file name (``.fits`` or ``.fits.gz``)."""
    base = l2_filename.rsplit("/", 1)[-1]
    for ext in (".fits.gz", ".fits"):
        if base.endswith(ext):
            base = base[:-len(ext)]
            break
    return base + SCI_UNCERT_SUFFIX


def q3c_search(query, ra0, dec0, radius_deg=CONE_RADIUS_DEG, fid=None, ppid=None,
               mjd_lo=None, mjd_hi=None, diff_basename=None):
    """Coarse search: every good difference image whose center lies within
    ``radius_deg`` of (ra0, dec0), with its science image.

    Parameters
    ----------
    query : callable
        ``query(sql, params) -> list of dict`` (e.g. a provider's ``_query``).
    ra0, dec0 : float
        Search center, degrees; normally the alerting chip's center.
    radius_deg : float
        Cone radius. The default is complete for any image that overlaps the
        alerting chip's footprint (see module notes).
    fid : int, optional
        Restrict to one filter id. None keeps every band.
    ppid : int, optional
        Restrict to one pipeline id (the science-pipeline ppid).
    mjd_lo, mjd_hi : float, optional
        Half-open MJD window ``[mjd_lo, mjd_hi)`` on the science image.
    diff_basename : str, optional
        Use this file in the job directory instead of ``diffimages.filename``
        (e.g. the masked flavor the cutouts use).

    Returns
    -------
    list of PrevImage
        Ordered by ``mjdobs``, with the science image and both PSFs named
        by convention; ``position_index`` is empty (see
        :func:`find_prev_images`).

    Notes
    -----
    The cone is a superset: it returns every neighbouring SCA of the same
    pointings as well (roughly six candidates per true overlap in the socsim
    database). :func:`contains_positions` is what narrows it down, and only
    images holding at least one position are ever staged.
    """
    conditions = ["d.status > 0", "d.vbest > 0",
                  "q3c_radial_query(d.ra0, d.dec0, %s, %s, %s)"]
    params: list = [float(ra0), float(dec0), float(radius_deg)]
    if fid is not None:
        conditions.append("d.fid = %s"); params.append(int(fid))
    if ppid is not None:
        conditions.append("d.ppid = %s"); params.append(int(ppid))
    if mjd_lo is not None:
        conditions.append("l.mjdobs >= %s"); params.append(float(mjd_lo))
    if mjd_hi is not None:
        conditions.append("l.mjdobs < %s"); params.append(float(mjd_hi))
    sql = f"""
        SELECT d.pid, d.rid, d.expid, d.sca, d.fid, f.filter AS band,
               l.mjdobs, d.filename AS diff_filename, l.filename AS l2_filename,
               {', '.join('d.' + c.strip() for c in _CORNER_COLUMNS.split(','))}
        FROM diffimages d
        JOIN l2files l ON l.rid = d.rid
        JOIN filters f ON f.fid = d.fid
        WHERE {' AND '.join(conditions)}
        ORDER BY l.mjdobs, d.pid
    """
    rows = query(sql, tuple(params))
    images = [_row_to_prev_image(row, diff_basename) for row in rows]
    logger.info("q3c_search: %d candidate images within %.3f deg of (%.5f, %.5f)",
                len(images), radius_deg, ra0, dec0)
    return images


def _row_to_prev_image(row, diff_basename=None):
    job_dir, db_basename = str(row["diff_filename"]).rsplit("/", 1)
    basename = diff_basename or db_basename
    band = None if row.get("band") is None else str(row["band"])
    sca = int(row["sca"])
    return PrevImage(
        rid=int(row["rid"]), pid=int(row["pid"]),
        diff_filename=f"{job_dir}/{basename}",
        sci_filename=f"{job_dir}/{SCI_BASENAME}",
        diff_psf=f"{job_dir}/{diff_psf_basename(basename)}",
        sci_psf=f"{job_dir}/{sci_psf_basename(band, sca)}",
        diff_uncert=f"{job_dir}/{diff_uncert_basename(basename)}",
        sci_uncert=f"{job_dir}/{sci_uncert_basename(str(row['l2_filename']))}",
        l2_filename=str(row["l2_filename"]),
        mjdobs=float(row["mjdobs"]), fid=int(row["fid"]), band=band,
        expid=int(row["expid"]), sca=sca,
        corners=tuple(float(row[c.strip()]) for c in _CORNER_COLUMNS.split(",")),
    )


def find_prev_images(query, positions, ra0, dec0, **search_kwargs):
    """Parent search: the previous images that contain each position.

    Parameters
    ----------
    query : callable
        As for :func:`q3c_search`.
    positions : sequence of Position
        Fixed positions to measure, normally the frozen positions of the
        objects on the alerting chip.
    ra0, dec0 : float
        Alerting chip center, degrees.
    **search_kwargs
        Passed to :func:`q3c_search` (``fid``, ``ppid``, ``mjd_lo``,
        ``mjd_hi``, ``radius_deg``, ``diff_basename``).

    Returns
    -------
    list of PrevImage
        Only images that contain at least one position, in time order, each
        with ``position_index`` set to the indices into ``positions`` it
        contains. This is the assignment table: the driver stages exactly
        these files and measures exactly these positions on each.
    """
    positions = list(positions)
    candidates = q3c_search(query, ra0, dec0, **search_kwargs)
    if not candidates or not positions:
        logger.info("find_prev_images: %d candidates, %d positions -> nothing to do",
                    len(candidates), len(positions))
        return []
    corners = np.array([img.corners for img in candidates], dtype=float)
    ra = np.array([p.ra for p in positions], dtype=float)
    dec = np.array([p.dec for p in positions], dtype=float)
    inside = contains_positions(corners, ra, dec)                          # (n_images, n_positions)
    kept = []
    for img, row in zip(candidates, inside):
        if row.any():
            img.position_index = np.flatnonzero(row)
            kept.append(img)
    n_pairs = int(inside.sum())
    logger.info("find_prev_images: %d of %d candidates hold positions; %d positions; "
                "%d position-image pairs (%.1f per kept image)",
                len(kept), len(candidates), len(positions), n_pairs,
                n_pairs / len(kept) if kept else 0.0)
    return kept


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
        # robust level and rms, which still gives sane fit weights
        from astropy.stats import mad_std
        good = data[~bad]
        level, rms = (float(np.median(good)), float(mad_std(good))) if good.size else (0.0, np.nan)
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
# Driver
# ---------------------------------------------------------------------------

#: Which image of each epoch to measure, and where its file, PSF and
#: uncertainty image are on the PrevImage record.
IMAGE_PRODUCTS = {
    "diff": ("diff_filename", "diff_psf", "diff_uncert"),
    "science": ("sci_filename", "sci_psf", "sci_uncert"),
}


def run_prev_images(images, positions, stage, products=("diff", "science"), uncert_url=None,
                    gain=None, fit_shape=FIT_SHAPE, margin=STAMP_MARGIN_PX, strict=False,
                    delete_staged=True):
    """Measure every position on every previous image, one file at a time.

    A generator: for each image and each product it stages the image (and
    its PSF), runs :func:`forced_photometry` on the positions that image
    holds, yields the rows, and deletes the staged copy before moving on,
    so memory and disk hold one image at a time.

    Parameters
    ----------
    images : sequence of PrevImage
        From :func:`find_prev_images`, ``position_index`` filled.
    positions : sequence of Position
        The list ``position_index`` indexes into.
    stage : callable
        ``stage(url) -> local path``; a plain path comes back unchanged
        (e.g. ``AlertDataProvider._stage``). Must raise on failure.
    products : sequence of {"diff", "science"}
        Which image of each epoch to measure.
    uncert_url : callable, optional
        ``uncert_url(image, product) -> url or None`` overriding which
        per-pixel uncertainty image is staged alongside. By default the
        record's own (``diff_uncert`` / ``sci_uncert``, the pipeline's
        products) is used; a return of None, or a file that cannot be
        staged, falls back to :func:`error_array` with a warning.
    gain, fit_shape, margin
        Passed to :func:`forced_photometry`.
    strict : bool
        False (default): a failure on one image-product is logged and
        skipped, the run continues. True: it propagates.
    delete_staged : bool
        Remove staged copies after use (never the science PSF, which is
        cached for the run, and never a file the caller passed as a plain
        path).

    Yields
    ------
    (PrevImage, str, list of Measurement)
        The image, the product measured, and its rows (possibly empty),
        with ``product`` and ``mjdobs`` stamped on every row.

    Notes
    -----
    Every epoch's job directory holds a copy of the same science PSF for
    its (band, SCA), under the same basename, so that file is staged once
    per basename and reused. The difference PSF also repeats its basename
    but is matched to its own epoch, so it is staged every time.
    """
    positions = list(positions)
    sci_psf_cache: dict[str, str] = {}
    n_failed = 0
    for image in images:
        subset = [positions[i] for i in np.asarray(image.position_index, dtype=int)]
        if not subset:
            continue
        for product in products:
            file_attr, psf_attr, unc_attr = IMAGE_PRODUCTS[product]
            url, psf_url = getattr(image, file_attr), getattr(image, psf_attr)
            staged = []
            try:
                local = stage(url); staged.append((url, local))
                if product == "science":
                    key = psf_url.rsplit("/", 1)[-1]
                    if key not in sci_psf_cache or not os.path.exists(sci_psf_cache[key]):
                        sci_psf_cache[key] = stage(psf_url)
                    psf_local = sci_psf_cache[key]
                else:
                    psf_local = stage(psf_url); staged.append((psf_url, psf_local))
                unc_local = None
                unc = (uncert_url(image, product) if uncert_url is not None
                       else getattr(image, unc_attr))
                if unc is not None:
                    try:
                        unc_local = stage(unc); staged.append((unc, unc_local))
                        if unc_local is None or not os.path.exists(unc_local):
                            raise FileNotFoundError(unc_local or unc)   # plain paths pass stagers untouched
                    except Exception as exc:
                        unc_local = None
                        logger.warning("run_prev_images: rid=%s %s: uncertainty image %s not "
                                       "staged (%s); estimating the error instead",
                                       image.rid, product, unc, exc)
                rows = forced_photometry(local, psf_local, subset, rid=image.rid, product=product,
                                         uncert_path=unc_local, gain=gain,
                                         fit_shape=fit_shape, margin=margin)
                for row in rows:
                    row.product = product
                    row.mjdobs = image.mjdobs
            except Exception:
                n_failed += 1
                if strict:
                    raise
                logger.exception("run_prev_images: rid=%s pid=%s product=%s failed; skipping",
                                 image.rid, image.pid, product)
                rows = None
            finally:
                if delete_staged:
                    # stagers key local files by basename, so a staged copy
                    # can share its path with a cached PSF: never delete those
                    keep = set(sci_psf_cache.values())
                    for src, path in staged:
                        if path != src and path is not None and path not in keep and os.path.exists(path):
                            os.remove(path)
            if rows is not None:
                yield image, product, rows
    if n_failed:
        logger.warning("run_prev_images: %d image-product measurements failed and were skipped", n_failed)


def assemble_history(rows):
    """Group measurements by position, in time order.

    Parameters
    ----------
    rows : iterable of Measurement

    Returns
    -------
    dict
        ``pos_id -> list of Measurement`` sorted by ``mjdobs`` then
        ``product``, so an epoch's difference and science measurements sit
        together. Positions with no rows are absent.
    """
    history: dict[int, list[Measurement]] = {}
    for row in rows:
        history.setdefault(row.pos_id, []).append(row)
    for rows_for_pos in history.values():
        rows_for_pos.sort(key=lambda r: (r.mjdobs, r.product))
    return history


# ---------------------------------------------------------------------------
# Per-chip table: one row per object and epoch, both products merged
# ---------------------------------------------------------------------------
# The layout the alert provider caches per chip and persists as parquet
# (one file per chip), and the layout a future database table would take.

#: Columns of the per-chip forced-photometry table.
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


def _split_s3(path):
    bucket, _, key = path[len("s3://"):].partition("/")
    return bucket, key


def write_table(table, path):
    """Write a FORCED_TABLE_DTYPE table as parquet, to a local path (parent
    directories created) or an ``s3://bucket/key`` object."""
    import tempfile

    import pyarrow as pa
    import pyarrow.parquet as pq

    arrow = pa.table({name: pa.array(table[name].tolist()) if table.dtype[name].kind == "U"
                      else pa.array(table[name]) for name in FORCED_TABLE_DTYPE.names})
    if path.startswith("s3://"):
        import boto3

        bucket, key = _split_s3(path)
        with tempfile.TemporaryDirectory() as tmp:
            local = os.path.join(tmp, os.path.basename(key))
            pq.write_table(arrow, local)
            boto3.client("s3").upload_file(local, bucket, key)
    else:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        pq.write_table(arrow, path)
    logger.info("write_table: %d rows -> %s", len(table), path)


def read_table(path):
    """A FORCED_TABLE_DTYPE table from a parquet file written by
    :func:`write_table`, local or ``s3://``; None when there is no such
    file (or no access to it)."""
    import tempfile

    import pyarrow.parquet as pq

    def to_table(local):
        arrow = pq.read_table(local)
        table = np.zeros(arrow.num_rows, dtype=FORCED_TABLE_DTYPE)
        for name in FORCED_TABLE_DTYPE.names:
            table[name] = arrow.column(name).to_numpy(zero_copy_only=False)
        return table

    if path.startswith("s3://"):
        import boto3
        import botocore.exceptions

        bucket, key = _split_s3(path)
        with tempfile.TemporaryDirectory() as tmp:
            local = os.path.join(tmp, os.path.basename(key))
            try:
                boto3.client("s3").download_file(bucket, key, local)
            except botocore.exceptions.ClientError as exc:
                if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "403", "AccessDenied"):
                    return None
                raise
            return to_table(local)
    if not os.path.exists(path):
        return None
    return to_table(path)


# ---------------------------------------------------------------------------
# Entry point: one chip by hand
# ---------------------------------------------------------------------------

MEASUREMENT_FIELDS = ("pos_id", "rid", "product", "mjdobs", "x", "y", "flux", "fluxerr", "flags")


def read_positions_csv(path):
    """Positions from a CSV with columns ``pos_id, ra, dec`` (header row)."""
    import csv

    with open(path, newline="") as fh:
        return [Position(int(r["pos_id"]), float(r["ra"]), float(r["dec"]))
                for r in csv.DictReader(fh)]


def write_measurements_csv(path, rows):
    """Measurements to a CSV with MEASUREMENT_FIELDS as columns."""
    import csv

    with open(path, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(MEASUREMENT_FIELDS)
        for r in rows:
            writer.writerow([getattr(r, f) for f in MEASUREMENT_FIELDS])


def main(argv=None):
    """Forced photometry for one chip: ``--pid`` names the alerting
    difference image, ``--positions`` the CSV of positions to measure.

    Uses the live database (DB* environment variables, as RAPIDDB does)
    and S3 through the alert provider's staging. Writes one CSV row per
    measurement.
    """
    import argparse

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("--pid", type=int, required=True, help="diffimages.pid of the alerting chip")
    parser.add_argument("--positions", required=True, help="CSV with pos_id, ra, dec")
    parser.add_argument("--out", required=True, help="output CSV of measurements")
    parser.add_argument("--window-days", type=float, default=30.0,
                        help="look back this many days before the chip's mjdobs (default 30)")
    parser.add_argument("--products", default="diff,science", help="comma-separated: diff, science")
    parser.add_argument("--same-band", action="store_true", help="restrict to the chip's filter")
    parser.add_argument("--strict", action="store_true", help="stop at the first failed image")
    args = parser.parse_args(argv)

    from database.modules.utils.rapid_db import RAPIDDB
    from alerts.providers import AlertDataProvider

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    db = RAPIDDB()
    if db.conn is None:
        raise SystemExit("could not connect to the database (exit_code %s)" % db.exit_code)
    provider = AlertDataProvider(db)
    (chip,) = provider._query("""
        SELECT d.ra0, d.dec0, d.fid, d.ppid, l.mjdobs
        FROM diffimages d JOIN l2files l ON l.rid = d.rid WHERE d.pid = %s""", (args.pid,))
    positions = read_positions_csv(args.positions)
    images = find_prev_images(provider._query, positions, chip["ra0"], chip["dec0"],
                              fid=chip["fid"] if args.same_band else None, ppid=chip["ppid"],
                              mjd_lo=chip["mjdobs"] - args.window_days, mjd_hi=chip["mjdobs"] + 1e-6)
    all_rows = []
    for _, _, rows in run_prev_images(images, positions, provider._stage,
                                      products=tuple(p.strip() for p in args.products.split(",")),
                                      strict=args.strict):
        all_rows.extend(rows)
    write_measurements_csv(args.out, all_rows)
    history = assemble_history(all_rows)
    logger.info("main: pid=%d, %d positions, %d images, %d measurements for %d positions -> %s",
                args.pid, len(positions), len(images), len(all_rows), len(history), args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
