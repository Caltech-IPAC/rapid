# ported-from: database/sims/db_register_socsim_files.py, database/modules/utils/overlapping_fields.py, modules/utils/rapid_pipeline_subs.py @ c740f3e3
"""Spatial helpers for `crossmatch`, over the Roman tessellation.

The pure derivations `register` needs (``healpix_indexes``,
``unit_vector``, ``tessellation_field``, ``overlapping_fields`` and its
``tan_proj2``) live in ``rapidpipe.products.spatial``, so ``rapidpipe.db``
can use them without importing this package (stage-contract.md
§The package); they are re-exported here, so
``rapidpipe.science.spatial.<name>`` keeps naming every one of them.

``radec_index``/``index_to_radec``, ``compute_angular_separation``, and the
``field_neighbours``/``field_center``/``field_corners`` tessellation
wrappers below are `crossmatch`'s science helpers (crossmatch page),
ported here rather than imported because their only home in this tree
(``modules/utils/rapid_pipeline_subs.py``) imports boto3 and scipy at
module scope. Like ``tessellation_field``, the three wrappers go through
``RomanTessellationClosedForm`` and so need no SQLite tessellation
database and no ``ROMANTESSELLATIONDBNAME`` env var -- unlike the legacy
``crossMatchSources.py`` stage 2, which still opens the deprecated
``RomanTessellationNSIDE512`` for the same three queries.
"""

from __future__ import annotations

import math

import numpy as np

from database.modules.utils.roman_tessellation_db import RomanTessellationClosedForm
from rapidpipe.products.spatial import (  # noqa: F401 -- re-exported
    DEFAULT_MIN_OVERLAP_PIXELS,
    healpix_indexes,
    overlapping_fields,
    tan_proj2,
    tessellation_field,
    unit_vector,
)


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
