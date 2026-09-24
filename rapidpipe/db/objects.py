"""Persistence for the field result sets: `merges`, `astroobjects`, `astroobjectsmeta`, `prunedmerges`.

The database side of the `crossmatch`, `statistics` and `prune` stages,
ported from `dev`'s ``pipeline/crossMatchSources.py``,
``pipeline/computeStatisticsForAstroObjects.py``,
``pipeline/pruneNotBestMerges.py`` and ``database/modules/utils/rapid_db.py``
(``copy_data_from_file_into_database``). Every function runs inside the
caller's transaction: none commits or rolls back.

- :func:`ensure_field_object_tables`, :func:`ensure_astroobjectsmeta_table`:
  `dev`'s ``astroobjects_<field>``/``merges_<field>`` and
  ``astroobjectsmeta_<field>`` creation, indexing and grants, through
  20260924-03's ``create_field_object_tables`` and
  ``create_astroobjectsmeta_child_table``. `dev` drops and recreates
  ``astroobjectsmeta_<field>`` on every run; the rebuild never drops it.
- :func:`cluster_field_object_tables`: `dev`'s CLUSTER of
  ``astroobjects_<field>`` and ANALYZE of both tables between crossmatch's
  two passes.
- :func:`copy_astroobjects`, :func:`copy_merges`, :func:`copy_astroobjectsmeta`:
  `dev`'s bulk COPY of a CSV file (its separator and null string), through a
  temporary table and ``INSERT ... ON CONFLICT DO NOTHING`` against the
  set-scoped UNIQUE constraints, so a repeated row is dropped rather than
  duplicated. This folds `dev`'s ``pruneRedundantMerges`` into the load.
- :func:`source_set_table`: the ``sources`` child table a `source-set`
  instance's rows live in -- the rebuild's form of `dev`'s
  ``lookup_source_tables_to_crossmatch_and_distinct_fields`` date scan, by
  instance id rather than processing date.
- :func:`catalog_visibility_sql`, :func:`current_association_sets`: the rows
  a crossmatch pass may read as the existing catalog (step 1 ruling R3).
- :func:`insert_pruned_merges`: a `pruned-set`'s excluded pairs, the rows
  `dev`'s ``pruneNotBestMerges`` deletes in place (step 1 ruling R6).

The result-set row itself (`product_instances` plus `result_sets`) is
written by ``rapidpipe.runs.repository.register_manifest``, the one writer
of instance rows; this package may not import ``rapidpipe.runs``.

This module imports ``rapidpipe.db`` only, matching the package contract.
"""

from __future__ import annotations

import re
from typing import IO, Any, Iterable

from rapidpipe.db import sources

#: `dev`'s column lists, in `dev`'s order (baseline prototypes; the files
#: crossMatchSources.py and computeStatisticsForAstroObjects.py COPY).
DEV_ASTROOBJECTS_COLUMNS: tuple[str, ...] = ("aid", "ra0", "dec0", "flux0")
DEV_MERGES_COLUMNS: tuple[str, ...] = ("aid", "sid")
DEV_ASTROOBJECTSMETA_COLUMNS: tuple[str, ...] = (
    "aid", "meanra", "stdevra", "meandec", "stdevdec", "meanflux", "stdevflux", "nsources",
)

#: The run-model columns (20260924-02-objects-run-columns.sql) the rebuild
#: appends to every row it writes.
RUN_COLUMNS: tuple[str, ...] = ("run", "attempt", "result_set")

#: The CSV columns each copy function reads, in order: `dev`'s, then the run columns.
ASTROOBJECTS_COLUMNS: tuple[str, ...] = DEV_ASTROOBJECTS_COLUMNS + RUN_COLUMNS
MERGES_COLUMNS: tuple[str, ...] = DEV_MERGES_COLUMNS + RUN_COLUMNS
ASTROOBJECTSMETA_COLUMNS: tuple[str, ...] = DEV_ASTROOBJECTSMETA_COLUMNS + RUN_COLUMNS

#: `dev`'s COPY options (rapid_db.py ``copy_data_from_file_into_database``).
COPY_SEPARATOR = sources.COPY_SEPARATOR
COPY_NULL = sources.COPY_NULL

PREFIXES: tuple[str, ...] = ("astroobjects", "merges", "astroobjectsmeta")

_FIELD_TABLE_RE = re.compile(r"(astroobjects|merges|astroobjectsmeta)_([0-9]+)")
_ALIAS_RE = re.compile(r"[a-z_][a-z0-9_]*")


def _field(field: Any) -> int:
    """A field (Roman tessellation rtid) as a non-negative int; refuses anything else."""
    if isinstance(field, bool):
        raise ValueError(f"field must be a non-negative integer, got {field!r}")
    try:
        value = int(field)
    except (TypeError, ValueError):
        raise ValueError(f"field must be a non-negative integer, got {field!r}") from None
    if value < 0 or (isinstance(field, str) and not field.isdigit()) or (
            isinstance(field, float) and field != value):
        raise ValueError(f"field must be a non-negative integer, got {field!r}")
    return value


def field_table_names(field: int) -> dict[str, str]:
    """``{"astroobjects": "astroobjects_<field>", "merges": ..., "astroobjectsmeta": ...}``."""
    value = _field(field)
    return {prefix: f"{prefix}_{value}" for prefix in PREFIXES}


def _split_field_table(table: str) -> tuple[str, int]:
    match = _FIELD_TABLE_RE.fullmatch(table)
    if match is None:
        raise ValueError(f"not a per-field object table name: {table!r}")
    return match.group(1), int(match.group(2))


def ensure_field_object_tables(cur, field: int) -> bool:
    """Make ``astroobjects_<field>`` and ``merges_<field>`` if absent; return whether this call made either.

    Existing tables are found without a lock; only an attempt that must
    make a table takes the function's advisory lock.
    """
    names = field_table_names(field)
    if (sources.child_table_exists(cur, names["astroobjects"])
            and sources.child_table_exists(cur, names["merges"])):
        return False
    cur.execute("SELECT create_field_object_tables(%s)", (_field(field),))
    return bool(cur.fetchone()[0])


def ensure_astroobjectsmeta_table(cur, field: int) -> bool:
    """Make ``astroobjectsmeta_<field>`` if absent; return whether this call made it."""
    if sources.child_table_exists(cur, field_table_names(field)["astroobjectsmeta"]):
        return False
    cur.execute("SELECT create_astroobjectsmeta_child_table(%s)", (_field(field),))
    return bool(cur.fetchone()[0])


def cluster_field_object_tables(cur, field: int) -> None:
    """`dev`'s ``CLUSTER astroobjects_<field> USING astroobjects_<field>_radec_idx`` and ANALYZE of both."""
    cur.execute("SELECT cluster_field_object_tables(%s)", (_field(field),))


def _copy_dedupe(cur, table: str, csv_file: IO[str], columns: tuple[str, ...]) -> int:
    """COPY ``csv_file`` into a temporary copy of ``table``, then insert what is new.

    The temporary table is made ``LIKE`` the target (same column order, so
    ``SELECT *`` lines up) and dropped after the insert, so two copies into
    the same table in one transaction do not collide; ``ON COMMIT DROP``
    covers an exception between the two. Returns the rows inserted: rows
    already present under the same set-scoped key, and repeats within the
    file, are not counted.
    """
    _split_field_table(table)
    temp = f"copy_{table}"
    cur.execute(f"DROP TABLE IF EXISTS pg_temp.{temp}")
    cur.execute(f"CREATE TEMP TABLE {temp} (LIKE {table} INCLUDING DEFAULTS) ON COMMIT DROP")
    cur.copy_from(csv_file, temp, sep=COPY_SEPARATOR, null=COPY_NULL, columns=columns)
    cur.execute(f"INSERT INTO {table} SELECT * FROM {temp} ON CONFLICT DO NOTHING")
    inserted = int(cur.rowcount)
    cur.execute(f"DROP TABLE {temp}")
    return inserted


def copy_astroobjects(cur, field: int, csv_file: IO[str]) -> int:
    """COPY :data:`ASTROOBJECTS_COLUMNS` rows into ``astroobjects_<field>``; return the count inserted."""
    return _copy_dedupe(cur, field_table_names(field)["astroobjects"], csv_file,
                        ASTROOBJECTS_COLUMNS)


def copy_merges(cur, field: int, csv_file: IO[str]) -> int:
    """COPY :data:`MERGES_COLUMNS` rows into ``merges_<field>``; return the count inserted."""
    return _copy_dedupe(cur, field_table_names(field)["merges"], csv_file, MERGES_COLUMNS)


def copy_astroobjectsmeta(cur, field: int, csv_file: IO[str]) -> int:
    """COPY :data:`ASTROOBJECTSMETA_COLUMNS` rows into ``astroobjectsmeta_<field>``; return the count inserted."""
    return _copy_dedupe(cur, field_table_names(field)["astroobjectsmeta"], csv_file,
                        ASTROOBJECTSMETA_COLUMNS)


def count_result_set_rows(cur, table: str, result_set: str) -> int:
    """Rows of ``result_set`` in ``table`` (a per-field object table, or ``prunedmerges``)."""
    if table != "prunedmerges":
        _split_field_table(table)
    cur.execute(f"SELECT count(*) FROM {table} WHERE result_set = %s", (result_set,))
    return int(cur.fetchone()[0])


def source_set_table(cur, instance: str) -> tuple[str, int | None]:
    """The ``sources`` child table a `source-set` instance's rows live in, and its row count.

    Follows the instance's logical key to its difference instance, that
    instance's ``diffimages`` row, and the ``l2files`` row's ``dateobs`` and
    ``sca`` (`dev`'s ``sources_<yyyymmdd>_<sca>``). Raises
    :class:`ValueError` (the stage maps it to InputRejected) when the
    instance is not a complete, retained `source-set`, or any link is missing.
    """
    cur.execute(
        """
        SELECT pi.logical_key->>'difference', rs.complete, pi.deletion_state, rs.row_count
        FROM product_instances pi JOIN result_sets rs ON rs.instance = pi.id
        WHERE pi.id = %s AND pi.kind = 'source-set'
        """,
        (instance,))
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"no source-set result set with instance {instance!r}")
    difference, complete, deletion_state, row_count = row
    if not complete or deletion_state != "retained":
        raise ValueError(
            f"source-set {instance!r} is not complete and retained "
            f"(complete={complete}, deletion_state={deletion_state})")
    if not difference:
        raise ValueError(f"source-set {instance!r} names no difference instance in its key")
    cur.execute(
        """
        SELECT l.dateobs, l.sca FROM diffimages d JOIN l2files l ON l.rid = d.rid
        WHERE d.instance = %s
        """,
        (difference,))
    row = cur.fetchone()
    if row is None:
        raise ValueError(
            f"no diffimages/l2files row for difference instance {difference!r} "
            f"(source-set {instance!r})")
    dateobs, sca = row
    return sources.child_table_name(sources.obs_date_of(dateobs), sca), row_count


def catalog_visibility_sql(alias: str, run_id: str | None = None) -> tuple[str, tuple]:
    """The rows of ``<alias>`` a crossmatch pass reads as the existing catalog (ruling R3).

    Visible: pre-run-model rows (``run IS NULL``, always current), this
    run's own rows, and rows of any current, retained `association-set`.
    Returns ``(sql, params)``: ``sql`` has one ``%s`` placeholder, the run
    id. With ``run_id`` the params are ``(run_id,)``; without, they are
    ``()`` and the caller supplies the run id in that position.
    """
    if not _ALIAS_RE.fullmatch(alias):
        raise ValueError(f"not a table alias: {alias!r}")
    sql = (
        f"({alias}.run IS NULL OR {alias}.run = %s OR {alias}.result_set IN "
        "(SELECT id FROM product_instances WHERE kind = 'association-set' "
        "AND custody = 'current' AND deletion_state = 'retained'))"
    )
    return sql, ((run_id,) if run_id is not None else ())


def current_association_sets(cur, field: int) -> list[str]:
    """The current, retained `association-set` instances whose logical key names ``field``."""
    cur.execute(
        """
        SELECT id FROM product_instances
        WHERE kind = 'association-set' AND custody = 'current'
          AND deletion_state = 'retained' AND logical_key->>'field' = %s
        ORDER BY id
        """,
        (str(_field(field)),))
    return [row[0] for row in cur.fetchall()]


def insert_pruned_merges(
    cur, rows: Iterable[tuple[int, int]], result_set: str, base_set: str,
    run: str, attempt: str,
) -> int:
    """Insert a `pruned-set`'s excluded (aid, sid) pairs into ``prunedmerges``; return the count inserted.

    One statement over two arrays; a pair already recorded for
    ``result_set`` (a retry, or a repeat in ``rows``) is not inserted again.
    """
    pairs = [(int(aid), int(sid)) for aid, sid in rows]
    if not pairs:
        return 0
    cur.execute(
        """
        INSERT INTO prunedmerges (result_set, base_set, aid, sid, run, attempt)
        SELECT %s, %s, p.aid, p.sid, %s, %s
        FROM unnest(%s::bigint[], %s::bigint[]) AS p (aid, sid)
        ON CONFLICT DO NOTHING
        """,
        (result_set, base_set, run, attempt,
         [aid for aid, _ in pairs], [sid for _, sid in pairs]))
    return int(cur.rowcount)
