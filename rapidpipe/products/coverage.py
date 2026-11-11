# ported-from: none
"""The reference image's coverage: which frames it is coadded from, chosen by how much of it they cover.

Holds ``make_refimage_wcs``, ``coverage_sample_grid``,
``compute_footprint_mask``, ``coverage_fraction`` and
``select_refimage_inputs``, with the tolerance a setting
(``[reference.selection] coverage_estimate_tolerance``) and the decision its
own function (:func:`coverage_decision`) so the stage judges the frames the
loop named with the same rule. Beside them, the reference's footprint as
Roman tiles (:func:`reference_tiles`, the pool ``rapidpipe loop`` widens to
under ``selection_type = "coverage"``) and the order its candidates are
walked in (:func:`candidate_order`).

Here, not in ``rapidpipe.science``, because both the loop
(``rapidpipe.foundation``, which may import ``products`` but not
``science``) and the reference stage use it. Every footprint is projected
onto the reference's tangent plane and every distance is a great-circle
one, so footprints across R.A. 0/360 are handled.
"""

from __future__ import annotations

import math

import numpy as np
from astropy.coordinates import SkyCoord
from astropy.wcs import WCS

from rapidpipe.products.spatial import RomanTessellationClosedForm, overlapping_fields

#: The sampling step of the coverage estimate, reference pixels.
COVERAGE_GRID_STEP = 25
#: The default of ``coverage_estimate_tolerance``.
COVERAGE_ESTIMATE_TOLERANCE = 0.05
#: ``[reference.selection] selection_type``'s values: the first ``max_frames`` frames, or coverage.
SELECTION_TYPES = ("nframes", "coverage")


def make_refimage_wcs(ra0: float, dec0: float, naxis1: int, naxis2: int, cdelt1: float, cdelt2: float,
                      crota2: float) -> WCS:
    """The TAN WCS of a reference image centred on ``(ra0, dec0)``, ``crpix`` at the image centre.

    Parameters
    ----------
    ra0, dec0 : float
        Sky position of the reference image's centre [deg].
    naxis1, naxis2 : int
        Size of the reference image [pixels].
    cdelt1, cdelt2 : float
        Pixel scale [deg/pixel].
    crota2 : float
        Rotation of the reference image [deg].

    Returns
    -------
    astropy.wcs.WCS
        WCS of the reference image.
    """
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.crval = [ra0, dec0]
    w.wcs.crpix = [0.5 * naxis1 + 0.5, 0.5 * naxis2 + 0.5]
    w.wcs.cdelt = [cdelt1, cdelt2]
    w.wcs.crota = [0.0, crota2]
    return w


def coverage_sample_grid(naxis1: int, naxis2: int) -> tuple[np.ndarray, np.ndarray]:
    """Zero-based pixel coordinates of the grid that samples a reference image every :data:`COVERAGE_GRID_STEP` pixels.

    Parameters
    ----------
    naxis1, naxis2 : int
        Size of the reference image [pixels].

    Returns
    -------
    x_grid, y_grid : numpy.ndarray
        2-D arrays of the grid points' x and y pixel coordinates.
    """
    x = np.arange(COVERAGE_GRID_STEP // 2, naxis1, COVERAGE_GRID_STEP)
    y = np.arange(COVERAGE_GRID_STEP // 2, naxis2, COVERAGE_GRID_STEP)
    return np.meshgrid(x, y)


def compute_footprint_mask(w: WCS, x_grid: np.ndarray, y_grid: np.ndarray, ras, decs) -> np.ndarray:
    """Flag the grid points inside an input image's footprint, given its four corners in order around it.

    The corners are projected onto the reference's tangent plane, so a
    footprint across R.A. 0/360 is handled.

    Parameters
    ----------
    w : astropy.wcs.WCS
        WCS of the reference image.
    x_grid, y_grid : numpy.ndarray
        Zero-based pixel coordinates of the grid points.
    ras, decs : sequence of float
        R.A. and Dec. of the input image's four corners [deg].

    Returns
    -------
    numpy.ndarray of bool
        True for the grid points inside the footprint.
    """
    x, y = w.world_to_pixel(SkyCoord(ra=ras, dec=decs, unit="deg"))
    inside_pos = np.ones(x_grid.shape, dtype=bool)
    inside_neg = np.ones(x_grid.shape, dtype=bool)
    for i in range(4):
        j = (i + 1) % 4
        cross = (x[j] - x[i]) * (y_grid - y[i]) - (y[j] - y[i]) * (x_grid - x[i])
        inside_pos &= cross >= 0.0
        inside_neg &= cross <= 0.0
    return inside_pos | inside_neg


def coverage_fraction(cov_map: np.ndarray, min_coverage_depth: float) -> float:
    """The fraction of a coverage map's pixels with a depth of at least ``min_coverage_depth``.

    Parameters
    ----------
    cov_map : numpy.ndarray
        Coverage map (input images per pixel).
    min_coverage_depth : float
        Depth for a pixel to count as covered [frames].

    Returns
    -------
    float
        Fraction of covered pixels.
    """
    return float(np.count_nonzero(cov_map >= min_coverage_depth)) / cov_map.size


def coverage_decision(fraction: float, min_coverage_fraction: float,
                      tolerance: float = COVERAGE_ESTIMATE_TOLERANCE) -> str:
    """Judge an estimated coverage fraction against the minimum, with the estimate's tolerance.

    Parameters
    ----------
    fraction : float
        Estimated fraction of pixels at ``min_coverage_depth`` or deeper.
    min_coverage_fraction : float
        The fraction a reference needs.
    tolerance : float
        How far the estimate may be from the truth.

    Returns
    -------
    str
        ``"build"`` at or above the minimum plus the tolerance, ``"verify"`` within it (the coadd's
        coverage map decides), ``"reject"`` below the minimum less the tolerance.
    """
    if fraction >= min_coverage_fraction + tolerance:
        return "build"
    if fraction >= min_coverage_fraction - tolerance:
        return "verify"
    return "reject"


def select_refimage_inputs(w: WCS, naxis1: int, naxis2: int, candidate_corners: list,
                           min_coverage_depth: float, min_coverage_fraction: float,
                           max_coverage_depth: float, max_coverage_fraction: float, max_n_images_to_coadd: int,
                           tolerance: float = COVERAGE_ESTIMATE_TOLERANCE) -> tuple[list[int], float, float, str]:
    """Add candidates in order, skipping any that miss the reference, until enough of it is deep enough; then judge.

    Selection stops once ``max_coverage_fraction`` of the reference's
    pixels reach ``max_coverage_depth``, or at ``max_n_images_to_coadd``
    frames; a candidate whose footprint misses the reference (its tile is
    under the reference's edge) is skipped and does not count
    toward the cap.

    Parameters
    ----------
    w : astropy.wcs.WCS
        WCS of the reference image.
    naxis1, naxis2 : int
        Size of the reference image [pixels].
    candidate_corners : list of (ras, decs)
        Each candidate's four corners' R.A. and Dec. [deg], in the order they are walked.
    min_coverage_depth, min_coverage_fraction : float
        The depth [frames] and the fraction of pixels at it a reference needs.
    max_coverage_depth, max_coverage_fraction : float
        The depth [frames] and the fraction of pixels at it where selection stops.
    max_n_images_to_coadd : int
        The most candidates selected.
    tolerance : float
        The estimate's tolerance (:func:`coverage_decision`).

    Returns
    -------
    selected_indices : list of int
        Indices into ``candidate_corners`` of the selected candidates, in candidate order.
    estimated_fraction : float
        Estimated fraction of pixels at ``min_coverage_depth`` or deeper.
    estimated_fraction_max : float
        Estimated fraction of pixels at ``max_coverage_depth`` or deeper.
    decision : str
        :func:`coverage_decision`'s.
    """
    x_grid, y_grid = coverage_sample_grid(naxis1, naxis2)
    depth = np.zeros(x_grid.shape, dtype=np.int32)
    selected_indices: list[int] = []
    estimated_fraction_max = 0.0
    for i, (ras, decs) in enumerate(candidate_corners):
        if len(selected_indices) >= max_n_images_to_coadd:
            break
        mask = compute_footprint_mask(w, x_grid, y_grid, ras, decs)
        if not mask.any():
            continue
        depth += mask
        selected_indices.append(i)
        estimated_fraction_max = coverage_fraction(depth, max_coverage_depth)
        if estimated_fraction_max >= max_coverage_fraction:
            break
    estimated_fraction = coverage_fraction(depth, min_coverage_depth)
    return (selected_indices, estimated_fraction, estimated_fraction_max,
            coverage_decision(estimated_fraction, min_coverage_fraction, tolerance))


def footprint_depth(w: WCS, naxis1: int, naxis2: int, candidate_corners: list) -> tuple[list[int], np.ndarray]:
    """The sampled depth of every given frame together, and which of them touch the reference at all.

    The reference stage's estimate of the frames its job names, all of
    them (the loop has already chosen them, :func:`select_refimage_inputs`).

    Parameters
    ----------
    w : astropy.wcs.WCS
        WCS of the reference image.
    naxis1, naxis2 : int
        Size of the reference image [pixels].
    candidate_corners : list of (ras, decs)
        Each frame's four corners' R.A. and Dec. [deg].

    Returns
    -------
    covering : list of int
        Indices of the frames whose footprint meets the reference.
    depth : numpy.ndarray
        Frames over each grid point of :func:`coverage_sample_grid`.
    """
    x_grid, y_grid = coverage_sample_grid(naxis1, naxis2)
    depth = np.zeros(x_grid.shape, dtype=np.int32)
    covering = []
    for i, (ras, decs) in enumerate(candidate_corners):
        mask = compute_footprint_mask(w, x_grid, y_grid, ras, decs)
        if mask.any():
            depth += mask
            covering.append(i)
    return covering, depth


def tile_center(rtid: int) -> tuple[float, float]:
    """The centre of Roman tile ``rtid``, as ``rapidpipe.science.spatial.field_center`` gives it.

    Parameters
    ----------
    rtid : int
        The tile (field).

    Returns
    -------
    tuple of float
        ``(ra, dec)`` [deg].
    """
    tessellation = RomanTessellationClosedForm()
    tessellation.get_center_sky_position(int(rtid))
    return tessellation.ra0, tessellation.dec0


def reference_tiles(w: WCS, naxis1: int, naxis2: int) -> list[int]:
    """The Roman tiles under a reference image's whole footprint, the loop's ``"coverage"`` pool.

    They are :func:`rapidpipe.products.spatial.overlapping_fields` of the reference's WCS, which
    works on the tangent plane, with no inset.

    Parameters
    ----------
    w : astropy.wcs.WCS
        WCS of the reference image (:func:`make_refimage_wcs`).
    naxis1, naxis2 : int
        Size of the reference image [pixels].

    Returns
    -------
    list of int
        Ascending tile ids.
    """
    (cd11, cd12), (cd21, cd22) = w.pixel_scale_matrix
    crpix1, crpix2 = w.wcs.crpix
    crval1, crval2 = w.wcs.crval
    return overlapping_fields(crval1, crval2, crpix1, crpix2, cd11, cd12, cd21, cd22, naxis1, naxis2,
                              min_overlap_pixels=0.0)


def angular_distance(ra1: float, dec1: float, ra2: float, dec2: float) -> float:
    """The great-circle distance between two sky positions, degrees (haversine, so R.A. wraps).

    Parameters
    ----------
    ra1, dec1, ra2, dec2 : float
        The two positions [deg].

    Returns
    -------
    float
        Their separation [deg].
    """
    p1, p2 = math.radians(dec1), math.radians(dec2)
    h = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(ra2 - ra1) / 2) ** 2)
    return math.degrees(2 * math.asin(min(1.0, math.sqrt(h))))


def candidate_order(mjdobs: float, ra0: float, dec0: float, key: tuple, center: tuple[float, float]) -> tuple:
    """A candidate's sort key: ``mjdobs``, then its centre's distance from the reference's, then its L2 key.

    Parameters
    ----------
    mjdobs : float
        The candidate's MJD-OBS.
    ra0, dec0 : float
        The candidate's centre [deg].
    key : tuple
        Its L2 key, ``(expid, sca, fid)``: the backstop for an exact tie.
    center : tuple of float
        The reference's centre [deg].

    Returns
    -------
    tuple
        The key, lower first.
    """
    return mjdobs, angular_distance(ra0, dec0, *center), tuple(key)
