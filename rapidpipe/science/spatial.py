"""Spatial derivations `register` needs to write l2-image rows.

Pure functions over numpy/healpy only -- no `rapidpipe` imports outside
`rapidpipe.science` (stage contract, dependency direction;
``tests/unit/test_dependency_direction.py``), so this module has no
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

``radec_index``/``index_to_radec``, ``compute_angular_separation``, and the
``field_neighbours``/``field_center``/``field_corners`` tessellation
wrappers below are `crossmatch`'s science helpers (step-1 ruling R8), also
ported here rather than imported for the same ``rapid_pipeline_subs``
reason. Like ``tessellation_field``, the three wrappers go through
``RomanTessellationClosedForm`` and so need no SQLite tessellation
database and no ``ROMANTESSELLATIONDBNAME`` env var -- unlike the legacy
``crossMatchSources.py`` stage 2, which still opens the deprecated
``RomanTessellationNSIDE512`` for the same three queries.
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


_closed_form_tessellation: RomanTessellationClosedForm | None = None


def _closed_form() -> RomanTessellationClosedForm:
    """The module's lazily-created, shared `RomanTessellationClosedForm`.

    Created on first use and reused after: the class holds no connection
    or open file (module docstring), and the two methods below that write
    their answer onto instance attributes (`get_center_sky_position`,
    `get_corner_sky_positions`) are read back before this module makes
    another call through the same instance, so sharing it across calls to
    :func:`field_neighbours`/:func:`field_center`/:func:`field_corners`
    costs nothing.
    """
    global _closed_form_tessellation
    if _closed_form_tessellation is None:
        _closed_form_tessellation = RomanTessellationClosedForm()
    return _closed_form_tessellation


def field_neighbours(rtid: int) -> list[int]:
    """rtids of every tile sharing an edge or corner with ``rtid``.

    Thin wrapper over ``RomanTessellationClosedForm.get_all_neighboring_rtids``
    -- see that method's docstring for what its certification does and does
    not cover (tile-for-tile verified against the deprecated SQLite class,
    poles included; see also this module's own docstring on certification
    depth). Crossmatch stage 2 uses this to build its per-field neighbour
    list (dev: ``roman_tessellation_db.get_all_neighboring_rtids``, called
    on the deprecated SQLite class).
    """
    return [int(n) for n in _closed_form().get_all_neighboring_rtids(int(rtid))]


def field_center(rtid: int) -> tuple[float, float]:
    """``(ra, dec)`` in degrees of tile ``rtid``'s centre.

    Thin wrapper over ``RomanTessellationClosedForm.get_center_sky_position``,
    which sets ``self.ra0``/``self.dec0`` rather than returning them; this
    reads them back into a plain tuple. Crossmatch stage 2 uses the centre
    as the origin of its per-field inclusion cone (dev:
    ``roman_tessellation_db.get_center_sky_position``, then
    ``util.compute_angular_separation`` from this point to each corner).
    """
    tessellation = _closed_form()
    tessellation.get_center_sky_position(int(rtid))
    return tessellation.ra0, tessellation.dec0


def field_corners(rtid: int) -> list[tuple[float, float]]:
    """Tile ``rtid``'s four corners as ``[(ra, dec), ...]`` in degrees.

    Thin wrapper over ``RomanTessellationClosedForm.get_corner_sky_positions``,
    which sets ``self.ra1``/``self.dec1`` .. ``self.ra4``/``self.dec4``
    rather than returning them; this reads them back in the same order,
    starting from the low corner going round the box (that method's own
    docstring). Crossmatch stage 2 measures the angular separation from
    the field centre to each of these four corners to size its inclusion
    cone (dev: ``roman_tessellation_db.get_corner_sky_positions``).
    """
    tessellation = _closed_form()
    tessellation.get_corner_sky_positions(int(rtid))
    return [
        (tessellation.ra1, tessellation.dec1),
        (tessellation.ra2, tessellation.dec2),
        (tessellation.ra3, tessellation.dec3),
        (tessellation.ra4, tessellation.dec4),
    ]


#: Packing constants for `radec_index`/`index_to_radec` (legacy
#: `modules/utils/rapid_pipeline_subs.py:2799-2808`). `_RADEC_UNITS_PER_DEG
#: = 11_880_000 = 3300 * 3600` packs each axis at 1/3300 arcsecond
#: (~0.33 mas) precision. `_DEC_MODULUS = 2_138_400_001 = 180 *
#: _RADEC_UNITS_PER_DEG + 1` is the dec-axis modulus used to pack the two
#: axes into one int64: `dec_units` ranges over `[0, 180 *
#: _RADEC_UNITS_PER_DEG]` (dec spans 180 degrees, -90 to +90), so this
#: modulus is exactly one more than its largest possible value, and
#: `radec_index` round-trips through `index_to_radec` losslessly for any
#: (ra in [0, 360), dec in [-90, 90]) pair at that precision.
_RADEC_UNITS_PER_DEG = 11_880_000
_DEC_MODULUS = 180 * _RADEC_UNITS_PER_DEG + 1


def radec_index(ra_deg, dec_deg):
    """Pack ``(ra_deg, dec_deg)`` into a single AstroObjects id ("aid").

    Ported verbatim from ``modules/utils/rapid_pipeline_subs.py:2799-2805``
    (``util.radec_index``). Works on scalars, lists, and numpy arrays --
    ``np.asarray`` is a no-op on an existing array -- since crossmatch
    calls this both per-source (a scalar `aid` for one unmatched source)
    and, potentially, over whole catalogs.
    """
    ra_units = np.rint(np.asarray(ra_deg) * _RADEC_UNITS_PER_DEG).astype(np.int64)
    dec_units = np.rint((np.asarray(dec_deg) + 90.0) * _RADEC_UNITS_PER_DEG).astype(np.int64)
    return ra_units * _DEC_MODULUS + dec_units


def index_to_radec(idx):
    """Inverse of :func:`radec_index`: return ``(ra_deg, dec_deg)``.

    Ported verbatim from ``modules/utils/rapid_pipeline_subs.py:2806-2808``
    (``util.index_to_radec``). Works on scalars, lists, and numpy arrays.
    """
    idx = np.asarray(idx, dtype=np.int64)
    dec_units = idx % _DEC_MODULUS
    ra_units = idx // _DEC_MODULUS
    return ra_units / _RADEC_UNITS_PER_DEG, dec_units / _RADEC_UNITS_PER_DEG - 90.0


def compute_angular_separation(ra1: float, dec1: float, ra2: float, dec2: float) -> float:
    """Great-circle separation, in degrees, between two sky positions.

    Ported verbatim (in effect) from
    ``modules/utils/rapid_pipeline_subs.py:766-801``
    (``util.compute_angular_separation``/``compute_xyz``): the chord
    length between the two positions' unit vectors, converted to an angle
    via ``2 * asin(chord / 2)`` (a haversine-style formula). Reuses
    :func:`unit_vector`, already in this module, in place of dev's
    separate ``compute_xyz`` helper -- the same formula (see
    :func:`unit_vector`'s docstring) -- rather than duplicating it; dev's
    module-level ``rtd = 180.0 / math.pi`` scale factor is exactly
    ``math.degrees``.
    """
    ax, ay, az = unit_vector(ra1, dec1)
    bx, by, bz = unit_vector(ra2, dec2)
    dx, dy, dz = bx - ax, by - ay, bz - az
    return math.degrees(2.0 * math.asin(0.5 * math.sqrt(dx * dx + dy * dy + dz * dz)))


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
