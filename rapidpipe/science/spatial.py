"""Spatial derivations `register` needs to write l2-image rows.

Pure functions over numpy/healpy only -- no `rapidpipe` imports outside
`rapidpipe.science` (stage contract, dependency direction;
``tests/rapidpipe/test_dependency_direction.py``), so this module has no
opinion about manifests, the database, or any stage. Every function here
reproduces a legacy derivation from
``database/sims/db_register_socsim_files.py`` exactly, so `register`'s
`hp6`/`hp9`/`field`/`x,y,z`/`overlapfields` columns keep the values the
team's existing queries expect.

``tessellation_field`` wraps ``database.modules.utils.roman_tessellation_db.
RomanTessellationClosedForm`` -- pure arithmetic, no SQLite, no I/O -- rather
than the deprecated ``RomanTessellationNSIDE512`` the legacy script used;
the closed form's ``get_rtid`` is certified to return the identical rtid
for every tile centre (see that module's docstring).

``overlapping_fields`` and ``tan_proj2`` are ported here, rather than
imported, because their only home in this tree
(``database/modules/utils/overlapping_fields.py`` and
``modules/utils/rapid_pipeline_subs.py``) pulls in ``rapid_pipeline_subs``,
which imports boto3 and scipy at module scope -- neither a dependency of
this package (see requirements.txt) nor anything `register` needs beyond
these two pure functions. ``test_spatial.py`` guards the two copies never
drifting apart, skipping (not failing) where boto3/scipy are absent, as
they are expected to be in this repository's own test environment.
"""

from __future__ import annotations

import math

import healpy as hp
import numpy as np

from database.modules.utils.roman_tessellation_db import RomanTessellationClosedForm

#: Level-6 and level-9 HEALPix NSIDE, matching the legacy script's
#: ``nside6 = 2**6``, ``nside9 = 2**9`` (``database/sims/
#: db_register_socsim_files.py``, module-level globals).
_NSIDE6 = 2 ** 6
_NSIDE9 = 2 ** 9


def healpix_indexes(ra: float, dec: float) -> tuple[int, int]:
    """Return ``(hp6, hp9)``: the level-6 and level-9 NESTED HEALPix indexes.

    Exactly the legacy calls: ``hp.ang2pix(nside, ra, dec, nest=True,
    lonlat=True)`` at ``nside=64`` and ``nside=512``.
    """
    hp6 = int(hp.ang2pix(_NSIDE6, ra, dec, nest=True, lonlat=True))
    hp9 = int(hp.ang2pix(_NSIDE9, ra, dec, nest=True, lonlat=True))
    return hp6, hp9


def unit_vector(ra: float, dec: float) -> tuple[float, float, float]:
    """Return the ``(x, y, z)`` unit vector for ``(ra, dec)`` in degrees.

    Exactly the legacy formula (``modules/utils/rapid_pipeline_subs.py``,
    ``compute_xyz``): ``x = cos(dec) cos(ra)``, ``y = cos(dec) sin(ra)``,
    ``z = sin(dec)``.
    """
    alpha = math.radians(ra)
    delta = math.radians(dec)
    cos_delta = math.cos(delta)
    x = cos_delta * math.cos(alpha)
    y = cos_delta * math.sin(alpha)
    z = math.sin(delta)
    return x, y, z


def tessellation_field(ra: float, dec: float) -> int:
    """Return the Roman tessellation tile id ("field") containing ``(ra, dec)``.

    Wraps :class:`RomanTessellationClosedForm`, the certified closed-form
    replacement for the legacy script's SQLite-backed
    ``RomanTessellationNSIDE512`` (module docstring: "Every one of the
    6,291,458 generated rows was compared against the baked SQLite file
    column for column, and each tile's own centre resolves to its own
    rtid"). A fresh instance per call: the class is stateless arithmetic,
    with no connection or file to keep open across calls.
    """
    return int(RomanTessellationClosedForm().get_rtid(ra, dec))


def tan_proj2(
    x: float, y: float, crpix1: float, crpix2: float,
    crval1: float, crval2: float,
    cd1_1: float, cd1_2: float, cd2_1: float, cd2_2: float,
) -> tuple[float, float]:
    """Tangent-plane pixel-to-sky projection, ignoring geometric distortion.

    Ported verbatim from ``modules/utils/rapid_pipeline_subs.py``'s
    ``tan_proj2`` (the one helper :func:`overlapping_fields` needs), which
    this module cannot import directly -- see the module docstring.
    ``crpix1``/``crpix2`` here are zero-based, matching that function's own
    convention and :func:`overlapping_fields`'s call of it.

    Returns ``(ra_deg, dec_deg)``; ``ra_deg`` is not wrapped to [0, 360).
    """
    dtr = math.pi / 180.0
    rtd = 180.0 / math.pi

    glong = crval1
    glat = crval2

    fsamp = x - crpix1
    fline = y - crpix2

    xx = -(cd1_1 * fsamp + cd1_2 * fline) * dtr
    yy = -(cd2_1 * fsamp + cd2_2 * fline) * dtr

    delta = math.atan(math.sqrt(xx * xx + yy * yy))

    if xx == 0.0 and yy == 0.0:
        yy = 1.0
    beta = math.atan2(-xx, yy)
    glatr = glat * dtr
    glongr = glong * dtr
    lat = math.asin(
        -math.sin(delta) * math.cos(beta) * math.cos(glatr)
        + math.cos(delta) * math.sin(glatr))
    xxx = math.sin(glatr) * math.sin(delta) * math.cos(beta) + math.cos(glatr) * math.cos(delta)
    yyy = math.sin(delta) * math.sin(beta)
    lon = glongr + math.atan2(yyy, xxx)

    return lon * rtd, lat * rtd


# ======================================================================
# overlapping_fields: ported from database/modules/utils/overlapping_fields.py
# ======================================================================
#
# The functions below are a verbatim port of that module's geometry (see
# its own docstring for the method: exact great-circle edges against the
# tessellation's axis-aligned RA/Dec tile rectangles, not a bounding box
# or grid sampling), with `modules.utils.rapid_pipeline_subs.tan_proj2`
# replaced by the copy above and `database.modules.utils.roman_tessellation`
# imported the same way the legacy module does -- that module is pure
# arithmetic (the certified tessellation, not the deprecated SQLite path)
# and is not itself part of the import chain this port avoids.

import database.modules.utils.roman_tessellation as _tess  # noqa: E402


def _unit_xyz(ra_deg: float, dec_deg: float) -> np.ndarray:
    r = math.radians(ra_deg)
    d = math.radians(dec_deg)
    return np.array([math.cos(d) * math.cos(r), math.cos(d) * math.sin(r), math.sin(d)])


def _radec_of(v) -> tuple[float, float]:
    return (math.degrees(math.atan2(v[1], v[0])) % 360.0,
            math.degrees(math.asin(max(-1.0, min(1.0, v[2])))))


def _on_arc(a, b, p) -> bool:
    def ang(u, w):
        return math.acos(max(-1.0, min(1.0, float(np.dot(u, w)))))
    return ang(a, p) + ang(p, b) <= ang(a, b) + 1e-9


def _cross_parallel(a, b, dec_deg: float) -> list:
    n = np.cross(a, b)
    nn = np.linalg.norm(n)
    if nn == 0.0:
        return []
    n = n / nn
    z = math.sin(math.radians(dec_deg))
    rho2 = 1.0 - z * z
    m2 = n[0] * n[0] + n[1] * n[1]
    if rho2 <= 0.0 or m2 == 0.0:
        return []
    c = -n[2] * z
    dist2 = c * c / m2
    if dist2 > rho2:
        return []
    h = math.sqrt(max(0.0, rho2 - dist2))
    m = math.sqrt(m2)
    x0, y0 = c * n[0] / m2, c * n[1] / m2
    ux, uy = -n[1] / m, n[0] / m
    out = []
    for s in (1.0, -1.0):
        p = np.array([x0 + s * h * ux, y0 + s * h * uy, z])
        if _on_arc(a, b, p):
            out.append(p)
    return out


def _contains(vertices, p) -> bool:
    if float(np.dot(sum(vertices), p)) <= 0.0:
        return False
    signs = [
        float(np.dot(np.cross(vertices[i], vertices[(i + 1) % 4]), p))
        for i in range(4)
    ]
    return all(s > 0.0 for s in signs) or all(s < 0.0 for s in signs)


def _dec_extrema(a, b) -> list:
    n = np.cross(a, b)
    nn = np.linalg.norm(n)
    if nn == 0.0:
        return []
    n = n / nn
    w = np.array([0.0, 0.0, 1.0]) - n[2] * n
    ww = np.linalg.norm(w)
    if ww < 1e-15:
        return []
    w = w / ww
    return [p for p in (w, -w) if _on_arc(a, b, p)]


#: Default minimum overlap, in pixels -- copied from
#: ``overlapping_fields.DEFAULT_MIN_OVERLAP_PIXELS``, the one home for this
#: number in the legacy tree. Kept as a second constant here rather than
#: imported, for the same reason the functions are ported rather than
#: imported: importing that module drags in ``rapid_pipeline_subs``
#: (boto3, scipy).
DEFAULT_MIN_OVERLAP_PIXELS = 25.0


def overlapping_fields(
    crval1: float, crval2: float, crpix1: float, crpix2: float,
    cd11: float, cd12: float, cd21: float, cd22: float,
    naxis1: int, naxis2: int,
    field: int | None = None,
    min_overlap_pixels: float = DEFAULT_MIN_OVERLAP_PIXELS,
) -> list[int]:
    """Ascending, deduplicated tile ids the image overlaps.

    Ported from ``database.modules.utils.overlapping_fields.
    overlapping_fields``; see that module's docstring for the geometry.
    ``crpix1``/``crpix2`` are FITS one-based, as they are on the ``l2files``
    row; this converts them, exactly as the original does.
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
        ra, dec = tan_proj2(x, y, crpix1 - 1.0, crpix2 - 1.0,
                             crval1, crval2, cd11, cd12, cd21, cd22)
        cra.append(ra % 360.0)
        cdec.append(dec)

    vertices = [_unit_xyz(cra[i], cdec[i]) for i in range(4)]
    edges = [(vertices[i], vertices[(i + 1) % 4]) for i in range(4)]

    ra_c = cra[0] + sum(((r - cra[0] + 180.0) % 360.0) - 180.0 for r in cra) / 4.0

    def unwrap(r):
        return ((r - ra_c + 180.0) % 360.0) - 180.0 + ra_c

    dec_lo, dec_hi = min(cdec), max(cdec)
    for a, b in edges:
        for p in _dec_extrema(a, b):
            d = _radec_of(p)[1]
            dec_lo = min(dec_lo, d)
            dec_hi = max(dec_hi, d)

    out: set[int] = set()

    if _contains(vertices, np.array([0.0, 0.0, 1.0])):
        out.add(1)
        for i in range(1, min(_tess.ring_of(dec_lo), _tess.NRINGS) + 1):
            for k in range(_tess.nrabins(i)):
                out.add(_tess._OFFSET[i] + k)
        if field is not None:
            out.add(int(field))
        return sorted(out)
    if _contains(vertices, np.array([0.0, 0.0, -1.0])):
        out.add(_tess.NROWS)
        for i in range(max(_tess.ring_of(dec_hi), 1), _tess.NRINGS + 1):
            for k in range(_tess.nrabins(i)):
                out.add(_tess._OFFSET[i] + k)
        if field is not None:
            out.add(int(field))
        return sorted(out)

    top, bot = _tess.ring_of(dec_hi), _tess.ring_of(dec_lo)
    if top < 1:
        out.add(1)
        top = 1
    if bot > _tess.NRINGS:
        out.add(_tess.NROWS)
        bot = _tess.NRINGS

    for i in range(max(top, 1), min(bot, _tess.NRINGS) + 1):
        dmax_i, dmin_i = _tess._BOUND[i - 1], _tess._BOUND[i]
        cand = [unwrap(cra[j]) for j in range(4) if dmin_i <= cdec[j] <= dmax_i]
        for a, b in edges:
            for d in (dmin_i, dmax_i):
                for p in _cross_parallel(a, b, d):
                    cand.append(unwrap(_radec_of(p)[0]))
            for p in _dec_extrema(a, b):
                rr, dd = _radec_of(p)
                if dmin_i <= dd <= dmax_i:
                    cand.append(unwrap(rr))
        if not cand:
            continue
        n = _tess.nrabins(i)
        k_lo = int(math.floor(min(cand) * n / 360.0 + 0.5))
        k_hi = int(math.floor(max(cand) * n / 360.0 + 0.5))
        for k in range(k_lo, k_hi + 1):
            out.add(_tess._OFFSET[i] + (k % n))

    if field is not None:
        out.add(int(field))
    return sorted(out)
