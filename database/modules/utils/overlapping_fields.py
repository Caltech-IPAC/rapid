"""
overlapping_fields.py — the exact sky-tile footprint of a science image.

WHAT THIS REPLACES.  Three approximations were in use, each wrong in a
different direction:

  * `RomanTessellationClosedForm.get_overlapping_rtids` takes the RA/Dec
    BOUNDING BOX of the image's corners.  Its own docstring says so:
    "Assumes the image is axis-aligned, as the SQLite version did."  A
    rotated SCA's sky-aligned bounding box is up to 2x its area, so this
    over-reports — measured at +20% of tiles on average over 1,200 random
    pointings, worst case +100%.
  * `get_all_neighboring_rtids` returns the centre tile plus its ring of
    neighbours.  Rotation-blind and shape-blind by construction.
  * Grid SAMPLING (method 3 of `scripts/compare_methods_overlapping_fields.py`)
    projects a coarse pixel grid and collects the tiles hit.  It can only
    return tiles containing a sample point, so it UNDER-reports: measured
    to miss at least one genuinely overlapped tile in 221 of the same
    1,200 pointings (18%), 246 tiles missed in total.

Under-reporting is the dangerous one — a missing field is an image
silently absent from that field's stack — and it is the one sampling does.

HOW THIS IS EXACT.  Two structural facts do all the work:

  1. TILES ARE AXIS-ALIGNED RA/DEC RECTANGLES.  The tessellation is banded
     in declination (rings) and binned in RA within a ring, so
     `corners_of(rtid)` is exactly `(ramin, ramax, decmin, decmax)`.  There
     is no general polygon-polygon intersection to do.
  2. THE IMAGE'S EDGES ARE GREAT-CIRCLE ARCS.  A straight line in a TAN
     tangent plane is a great circle — the gnomonic property — and the SCA
     is a rectangle in pixel space, so its four sky edges are great-circle
     arcs (for the CD-matrix WCS; see SIP below).

So: for each ring the image touches, compute the EXACT interval of RA the
image occupies INSIDE that ring's declination band, and take the bins
covering it.  No sampling, no bounding box, no projection approximation,
and rotation is handled exactly because the quadrilateral is used as a
quadrilateral.

Within a band, the image's extreme RAs are attained at one of exactly
three kinds of point, and this enumerates all three:
  (a) a corner whose declination lies in the band;
  (b) where an edge crosses one of the band's two bounding parallels;
  (c) an edge's own maximum-|declination| point, which is where RA turns
      around along a great circle.
That set is complete, which is why the interval is exact rather than
merely dense.

MEASURED (2026-09-08).  Against pixel-grid ground truth over 1,200
pointings drawn uniformly on the sphere at every rotation: ZERO tiles
missed.  That is the assertion the sweep makes, and it is the direction
that matters — a missed tile is an image silently absent from a field.

It also reports 12 tiles BEYOND ground truth.  Those are not errors:
ground truth is itself sampled, so a tile clipped by a sliver containing
no sample point is invisible to it.  Ten such cases from an earlier run
were each re-tested at EVERY pixel; nine were confirmed real, and the
tenth was measured directly at 0.038 arcsec of overlap — 0.35 of a pixel
width, so no pixel centre can fall in it at any sampling resolution.
All ten were real.  The 12 here are not individually verified and are
reported, never asserted.

None of this is folklore: the sweep is
`database/modules/utils/test/test_overlapping_fields.py`, opt-in behind
`RAPID_FOOTPRINT_SWEEP=1`, and re-running it reproduces every figure
above.

SIP.  The edges are great circles for the CD-matrix WCS this evaluates.
Real SIP coefficients bow an edge by up to a few pixels, which cannot
change a whole-tile verdict (tiles are ~250 arcsec) but can flip a
sub-arcsec sliver either way.  If that matters, see `min_overlap_pixels`.

MINIMUM OVERLAP.  A tile overlapped by a third of a pixel is real
geometry and useless science.  `min_overlap_pixels` shrinks the image
rectangle by that many pixels before the test, so the threshold is a
STATED number rather than the accidental, rotation-dependent one that
falls out of a sampling step.

The default is `DEFAULT_MIN_OVERLAP_PIXELS` = 25 px (laher, 2026-09-08),
which at the 0.11 arcsec/px plate scale is 2.75 arcsec against a tile
~250 arcsec across — roughly 1% of a tile edge.  It is defined ONCE here
and read from here by every caller (`database/sims/
db_backfill_l2files_overlapfields.py` takes its argparse default from
this constant rather than restating the number), because a backfill and
a registration path that disagreed about the threshold would put two
incompatible definitions in one column.

`min_overlap_pixels=0.0` is still available and is pure geometric
exactness — it is what the test suite asserts the geometry against, and
what any comparison with pixel-sampled ground truth has to use.  Note
what a non-zero inset does to that comparison: pixels in the outer
margin are DELIBERATELY outside the tested rectangle, so a sampled tile
is no longer necessarily in the result.  That is the threshold working,
not a defect, but it means "sampled tiles are a subset" is an invariant
of the exact geometry only.
"""

import math

import numpy as np

import database.modules.utils.roman_tessellation as tess
import modules.utils.rapid_pipeline_subs as util


def _unit(ra_deg, dec_deg):
    r = math.radians(ra_deg); d = math.radians(dec_deg)
    return np.array([math.cos(d)*math.cos(r), math.cos(d)*math.sin(r), math.sin(d)])


def _radec(v):
    return (math.degrees(math.atan2(v[1], v[0])) % 360.0,
            math.degrees(math.asin(max(-1.0, min(1.0, v[2])))))


def _on_arc(a, b, p):
    """Is `p` on the minor arc a->b?  Angles, so it is scale-free."""
    ang = lambda u, w: math.acos(max(-1.0, min(1.0, float(np.dot(u, w)))))
    return ang(a, p) + ang(p, b) <= ang(a, b) + 1e-9


def _cross_parallel(a, b, dec_deg):
    """Where the great-circle arc a->b crosses declination `dec_deg`.

    The great circle is the plane `n . r = 0`; the parallel is the plane
    `z = sin(dec)`.  Two planes meet in a line, and the line meets the
    unit sphere in 0, 1 or 2 points — closed form, no iteration.
    """
    n = np.cross(a, b)
    nn = np.linalg.norm(n)
    if nn == 0.0:
        return []
    n = n/nn
    z = math.sin(math.radians(dec_deg))
    rho2 = 1.0 - z*z
    m2 = n[0]*n[0] + n[1]*n[1]
    if rho2 <= 0.0 or m2 == 0.0:
        return []
    c = -n[2]*z
    dist2 = c*c/m2
    if dist2 > rho2:
        return []
    h = math.sqrt(max(0.0, rho2 - dist2))
    m = math.sqrt(m2)
    x0, y0 = c*n[0]/m2, c*n[1]/m2
    ux, uy = -n[1]/m, n[0]/m
    out = []
    for s in (1.0, -1.0):
        p = np.array([x0 + s*h*ux, y0 + s*h*uy, z])
        if _on_arc(a, b, p):
            out.append(p)
    return out


def _dec_extrema(a, b):
    """The arc's own max/min declination points — where RA turns around."""
    n = np.cross(a, b)
    nn = np.linalg.norm(n)
    if nn == 0.0:
        return []
    n = n/nn
    w = np.array([0.0, 0.0, 1.0]) - n[2]*n
    ww = np.linalg.norm(w)
    if ww < 1e-15:
        return []
    w = w/ww
    return [p for p in (w, -w) if _on_arc(a, b, p)]


#: Default minimum overlap, in pixels (laher, 2026-09-08).  One home for
#: the number: every caller takes its default from here.  See the module
#: docstring's "MINIMUM OVERLAP" for what it buys and what it costs.
DEFAULT_MIN_OVERLAP_PIXELS = 25.0


def overlapping_fields(crval1, crval2, crpix1, crpix2,
                       cd11, cd12, cd21, cd22,
                       naxis1, naxis2,
                       field=None,
                       min_overlap_pixels=DEFAULT_MIN_OVERLAP_PIXELS):
    """Ascending, deduplicated rtids of every sky tile the image overlaps.

    `crpix1`/`crpix2` are FITS one-based, as they are on the `l2files`
    row; this converts them.  `field`, if given, is unioned in AFTER the
    inset — it costs nothing, makes `field = ANY(overlapfields)` true by
    construction whatever the threshold, and is why raising
    `min_overlap_pixels` can never empty the result.

    Returns a sorted list of ints.
    """
    inset = float(min_overlap_pixels)
    lo1, hi1 = inset, naxis1 - 1.0 - inset
    lo2, hi2 = inset, naxis2 - 1.0 - inset
    if hi1 <= lo1 or hi2 <= lo2:
        raise ValueError("min_overlap_pixels exceeds half the detector")

    xs = (lo1, hi1, hi1, lo1)
    ys = (lo2, lo2, hi2, hi2)
    cra, cdec = [], []
    for x, y in zip(xs, ys):
        ra, dec = util.tan_proj2(x, y, crpix1 - 1.0, crpix2 - 1.0,
                                 crval1, crval2, cd11, cd12, cd21, cd22)
        cra.append(ra % 360.0)
        cdec.append(dec)

    V = [_unit(cra[i], cdec[i]) for i in range(4)]
    edges = [(V[i], V[(i + 1) % 4]) for i in range(4)]

    # Unwrap every RA about the image's own centre so an RA=0/360 straddle
    # is arithmetic rather than a special case.
    ra_c = cra[0] + sum(((r - cra[0] + 180.0) % 360.0) - 180.0 for r in cra)/4.0
    unwrap = lambda r: ((r - ra_c + 180.0) % 360.0) - 180.0 + ra_c

    # Declination span, including any bulge of an edge past its corners.
    dec_lo, dec_hi = min(cdec), max(cdec)
    for a, b in edges:
        for p in _dec_extrema(a, b):
            d = _radec(p)[1]
            dec_lo = min(dec_lo, d); dec_hi = max(dec_hi, d)

    out = set()
    top, bot = tess.ring_of(dec_hi), tess.ring_of(dec_lo)
    if top < 1:
        out.add(1); top = 1
    if bot > tess.NRINGS:
        out.add(tess.NROWS); bot = tess.NRINGS

    for i in range(max(top, 1), min(bot, tess.NRINGS) + 1):
        dmax_i, dmin_i = tess._BOUND[i-1], tess._BOUND[i]
        cand = [unwrap(cra[j]) for j in range(4) if dmin_i <= cdec[j] <= dmax_i]
        for a, b in edges:
            for d in (dmin_i, dmax_i):
                for p in _cross_parallel(a, b, d):
                    cand.append(unwrap(_radec(p)[0]))
            for p in _dec_extrema(a, b):
                rr, dd = _radec(p)
                if dmin_i <= dd <= dmax_i:
                    cand.append(unwrap(rr))
        if not cand:
            continue
        n = tess.nrabins(i)
        k_lo = int(math.floor(min(cand)*n/360.0 + 0.5))
        k_hi = int(math.floor(max(cand)*n/360.0 + 0.5))
        for k in range(k_lo, k_hi + 1):
            out.add(tess._OFFSET[i] + (k % n))

    if field is not None:
        out.add(int(field))
    return sorted(out)
