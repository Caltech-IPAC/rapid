# ported-from: pipeline/crossMatchSources.py @ c740f3e3
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
  ``attempt``, ``result_set``) appended, in the column order of the run
  model's per-field tables (retired with that schema; the associate port
  writes Parquet instead).
- :func:`new_object_id`: `dev`'s ``aid = util.radec_index(ra, dec)``
  (line 407), as a Python ``int``.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from rapidpipe.science.spatial import radec_index

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
