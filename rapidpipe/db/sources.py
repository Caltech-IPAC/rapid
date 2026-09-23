"""Persistence for the `source-set` result set: rows in the `sources` child tables.

The `load` stage's database side, ported from `dev`'s
``pipeline/loadPSFCatIntoDBSourcesTable.py`` and
``database/modules/utils/rapid_db.py`` (``copy_data_from_file_into_database``,
``get_l2file_info_for_sources``, ``get_best_difference_image``). Every
function runs inside the caller's transaction: none commits or rolls back.

- :func:`difference_image_row`: the `diffimages` row a difference instance
  registered as, with the `l2files` values `dev` stamps on every source row
  (``expid``, ``sca``, ``fid``, ``mjdobs``, and ``dateobs`` for the child
  table's date). `dev` finds the row by (``rid``, ``ppid``) and ``vbest``;
  the rebuild names the instance (products page: "A stage reads ... by
  id, never 'whatever is current'").
- :func:`ensure_child_table`: `dev`'s ``sources_<yyyymmdd>_<sca>`` creation
  and indexing, through 20260923-05's ``create_sources_child_table``.
- :func:`copy_sources`: `dev`'s bulk COPY of the loader's CSV file.
- :func:`find_complete_source_set`: the rebuild's form of `dev`'s
  ``source_dbload_jid<jid>.done`` check -- a complete source set for the
  same logical key already written in this run.
- :func:`cluster_and_analyze`: `dev`'s CLUSTER and ANALYZE, through
  ``cluster_sources_child_table``.

The result-set row itself (`product_instances` plus `result_sets`) is
written by ``rapidpipe.runs.repository.register_manifest``, the one writer
of instance rows; this package may not import ``rapidpipe.runs``.

This module imports ``rapidpipe.db`` only, matching the package contract.
"""

from __future__ import annotations

import json
import re
from typing import IO, Any

#: `dev`'s column list, in `dev`'s order (loadPSFCatIntoDBSourcesTable.py
#: L176-204): the CSV file the loader writes has exactly these columns.
DEV_COLUMNS: tuple[str, ...] = (
    "id", "ra", "dec", "xfit", "yfit", "fluxfit", "xerr", "yerr", "fluxerr",
    "npixfit", "qfit", "cfit", "redchi", "flags", "sharpness", "roundness1",
    "roundness2", "npix", "peak", "pid", "isdiffpos", "field", "hp6", "hp9",
    "expid", "fid", "sca", "mjdobs",
)

#: The run-model columns (20260923-04-sources-run-columns.sql) the rebuild
#: appends to every row it loads.
RUN_COLUMNS: tuple[str, ...] = ("run", "attempt", "result_set")

COLUMNS: tuple[str, ...] = DEV_COLUMNS + RUN_COLUMNS

#: `dev`'s COPY options (rapid_db.py ``copy_data_from_file_into_database``).
COPY_SEPARATOR = ","
COPY_NULL = "\\N"

_OBS_DATE_RE = re.compile(r"^[0-9]{8}$")


def obs_date_of(dateobs: Any) -> str:
    """`dev`'s child-table date: ``str(dateobs).split()[0].replace("-", "")``.

    ``dateobs`` is the `l2files` timestamp; its UT date names the table.
    """
    obs_date = str(dateobs).split()[0].replace("-", "")
    if not _OBS_DATE_RE.match(obs_date):
        raise ValueError(f"dateobs {dateobs!r} does not give a yyyymmdd date")
    return obs_date


def child_table_name(obs_date: str, sca: int) -> str:
    """``sources_<yyyymmdd>_<sca>``, as `dev` names it."""
    if not _OBS_DATE_RE.match(obs_date):
        raise ValueError(f"observation date must be yyyymmdd, got {obs_date!r}")
    return f"sources_{obs_date}_{int(sca)}"


def difference_image_row(cur, instance: str) -> dict[str, Any]:
    """The `diffimages` row of a registered difference instance, with its `l2files` values.

    Raises :class:`ValueError` (the stage maps it to InputRejected) when the
    instance has no `diffimages` row, or its l2 image no `l2files` row.
    """
    cur.execute("SELECT pid, rid FROM diffimages WHERE instance = %s", (instance,))
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"no diffimages row for difference instance {instance!r}")
    pid, rid = row
    cur.execute(
        "SELECT expid, sca, fid, field, hp6, hp9, mjdobs, dateobs FROM l2files WHERE rid = %s",
        (rid,))
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"no l2files row with rid {rid} (difference instance {instance!r})")
    expid, sca, fid, field, hp6, hp9, mjdobs, dateobs = row
    return {
        "pid": pid, "rid": rid, "expid": expid, "sca": sca, "fid": fid,
        "field": field, "hp6": hp6, "hp9": hp9, "mjdobs": mjdobs, "dateobs": dateobs,
    }


def child_table_exists(cur, table: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{table}",))
    return bool(cur.fetchone()[0])


def ensure_child_table(cur, obs_date: str, sca: int) -> bool:
    """Make ``sources_<obs_date>_<sca>`` if it does not exist; return whether this call made it.

    An existing table is found without a lock; only a load that must make
    the table takes the function's advisory lock.
    """
    if child_table_exists(cur, child_table_name(obs_date, sca)):
        return False
    cur.execute("SELECT create_sources_child_table(%s, %s)", (obs_date, int(sca)))
    return bool(cur.fetchone()[0])


def copy_sources(cur, table: str, csv_file: IO[str]) -> None:
    """COPY the loader's CSV rows into ``table``, `dev`'s separator and null string.

    ``csv_file`` has :data:`COLUMNS` in order, one row per line.
    """
    child_table_name(*_split_table_name(table))  # refuse anything but a child table
    cur.copy_from(csv_file, table, sep=COPY_SEPARATOR, null=COPY_NULL, columns=COLUMNS)


def _split_table_name(table: str) -> tuple[str, int]:
    match = re.fullmatch(r"sources_([0-9]{8})_([0-9]+)", table)
    if match is None:
        raise ValueError(f"not a sources child table name: {table!r}")
    return match.group(1), int(match.group(2))


def count_result_set_rows(cur, table: str, result_set: str) -> int:
    """Rows of ``result_set`` in ``table`` (the loaded count, read back)."""
    _split_table_name(table)
    cur.execute(f"SELECT count(*) FROM {table} WHERE result_set = %s", (result_set,))
    return int(cur.fetchone()[0])


def find_complete_source_set(cur, run_id: str, logical_key: dict[str, Any]) -> str | None:
    """The earliest complete, retained `source-set` instance for ``logical_key`` in ``run_id``."""
    cur.execute(
        """
        SELECT pi.id FROM product_instances pi
        JOIN result_sets rs ON rs.instance = pi.id
        WHERE pi.kind = 'source-set' AND pi.run = %s AND pi.logical_key = %s::jsonb
          AND rs.complete AND pi.deletion_state = 'retained'
        ORDER BY pi.id LIMIT 1
        """,
        (run_id, json.dumps(logical_key)))
    row = cur.fetchone()
    return row[0] if row is not None else None


def cluster_and_analyze(cur, obs_date: str, sca: int) -> None:
    """`dev`'s ``CLUSTER ... USING <table>_radec_idx`` and ``ANALYZE`` on one child table."""
    cur.execute("SELECT cluster_sources_child_table(%s, %s)", (obs_date, int(sca)))
