"""Pure helpers for `crossmatch`, ported from `dev`'s ``pipeline/crossMatchSources.py``.

No database access: the stage module runs the SQL and hands rows here.

- :func:`expid_order`: stage 1's exposure order. `dev` collects
  ``(expid, mjdobs)`` pairs from every sources table into one dict (a later
  pair for the same ``expid`` overwrites an earlier one), then sorts the
  dict's items by ``mjdobs`` ascending (``crossMatchSources.py`` lines
  219-257). Python's sort is stable, so exposures with equal ``mjdobs`` keep
  the order they were first seen in.
- :func:`merges_line`, :func:`astroobjects_line`: `dev`'s CSV lines, the
  ``str()`` of each value joined by commas, no quoting, newline-terminated
  (lines 337-347 and 425-452), with the rebuild's run columns (``run``,
  ``attempt``, ``result_set``) appended in
  ``rapidpipe.db.objects.MERGES_COLUMNS``/``ASTROOBJECTS_COLUMNS`` order.
- :func:`new_object_id`: `dev`'s ``aid = util.radec_index(ra, dec)``
  (line 407), as a Python ``int``.
- :func:`inclusion_cone_radius`, :func:`field_inclusion_cone`: stage 2's
  cone for a field with eight neighbours -- the largest centre-to-corner
  separation plus the match radius (lines 673-703).
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from rapidpipe.science.spatial import (
    compute_angular_separation,
    field_center,
    field_corners,
    radec_index,
)

NEWLINE = "\n"


def expid_order(pairs: Iterable[tuple[Any, Any]]) -> list[Any]:
    """Exposure ids in ascending ``mjdobs`` order, exactly as `dev` builds ``expids_list``.

    ``pairs`` is every ``(expid, mjdobs)`` row of the per-table exposure
    queries, in the order the tables were queried.
    """
    expids_dict: dict[Any, Any] = {}
    for expid, mjdobs in pairs:
        expids_dict[expid] = mjdobs
    sorted_expids_dict = dict(sorted(expids_dict.items(), key=lambda item: item[1]))
    return list(sorted_expids_dict.keys())


def _line(values: Sequence[Any]) -> str:
    return ",".join(str(v) for v in values) + NEWLINE


def merges_line(aid: Any, sid: Any, run_columns: Sequence[str]) -> str:
    """``aid,sid,run,attempt,result_set\\n``: `dev`'s merges line plus the run columns."""
    return _line((aid, sid, *run_columns))


def astroobjects_line(aid: Any, ra: Any, dec: Any, flux: Any, run_columns: Sequence[str]) -> str:
    """``aid,ra0,dec0,flux0,run,attempt,result_set\\n``.

    `dev` writes the unmatched source's own ``ra``, ``dec`` and ``fluxfit``
    as the new object's ``ra0``, ``dec0`` and ``flux0``.
    """
    return _line((aid, ra, dec, flux, *run_columns))


def new_object_id(ra: float, dec: float) -> int:
    """`dev`'s ``aid`` for a new AstroObjects row: ``radec_index(ra, dec)``."""
    return int(radec_index(ra, dec))


def inclusion_cone_radius(center: tuple[float, float],
                          corners: Sequence[tuple[float, float]],
                          match_radius: float) -> float:
    """The largest centre-to-corner separation, plus ``match_radius``, in degrees.

    `dev`: ``ang_sep = max(ang_sep1, ang_sep2, ang_sep3, ang_sep4)``, then
    ``ang_sep += match_radius`` ("The tiles are not necessarily square or
    even rectangular, so choose maximum separation").
    """
    ra0, dec0 = center
    ang_sep = max(compute_angular_separation(ra0, dec0, ra, dec) for ra, dec in corners)
    ang_sep += match_radius
    return ang_sep


def field_inclusion_cone(field: int, match_radius: float) -> tuple[float, float, float]:
    """``(ra0, dec0, radius)``: the stage-2 cone of tile ``field``, from the closed-form tessellation."""
    center = field_center(field)
    return center[0], center[1], inclusion_cone_radius(center, field_corners(field), match_radius)
