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

# TODO (2026-09-29): verify the deployed database has the Q3C index the
# schema declares on diffimages (q3c_ang2ipix(ra0, dec0)); q3c_search relies
# on it. The deployed database was found (2026-09-23) to lack every index
# declared on l2filemeta, including its Q3C index, so this one may be
# missing too. Without it the cone is a sequential scan of diffimages:
# tolerable at 30k rows, not at survey scale. Check with
#   select indexdef from pg_indexes where tablename = 'diffimages';

import logging
from dataclasses import dataclass, field

import numpy as np

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
    ``diff_psf`` and ``sci_psf`` are the job-directory products named by
    convention (see :func:`sci_psf_basename`, :func:`diff_psf_basename`);
    ``l2_filename`` is the raw L2 file the science image was made from.

    ``position_index`` is filled by :func:`find_prev_images`: indices into
    the caller's position list of the positions this image contains.
    """
    rid: int
    pid: int
    diff_filename: str
    sci_filename: str
    diff_psf: str
    sci_psf: str
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
    fit was made at.
    """
    pos_id: int
    rid: int
    x: float
    y: float
    flux: float
    fluxerr: float
    flags: int = 0


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

#: Science-image PSF in the job directory: the psfs-table file for the
#: (filter, SCA), normalized by the science pipeline. Seen in real job
#: directories (socsim jid129264, rimtimsim jid143919, 2026-09-29):
#: WFI_SCA07_F146_PSF_DET_DIST_normalized.fits. The filter token is the
#: Roman designation, not the RAPID name the filters table uses.
SCI_PSF_PATTERN = "WFI_SCA{sca:02d}_{roman}_PSF_DET_DIST_normalized.fits"

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
    return SCI_PSF_PATTERN.format(sca=int(sca), roman=roman)


def diff_psf_basename(diff_basename):
    """Job-directory basename of the PSF paired with a difference image."""
    try:
        return DIFF_PSF_BASENAMES[diff_basename]
    except KeyError:
        raise ValueError(f"no PSF known for difference image {diff_basename!r}; "
                         f"known: {sorted(DIFF_PSF_BASENAMES)}") from None


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
