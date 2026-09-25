"""Persistence for the `alerts` stage: its reads of the catalog sets, and the outbox.

The database side of `dev`'s ``alerts/providers.py`` (``iter_sources``,
``_prefetch_chip``, ``_stats_sql``), reading named result sets instead of
"the chip's rows": every query names the source set, the association set
and the statistics set by instance id (products page: "A stage reads a
result set by id, never 'whatever is current'"). ``sources`` is read through
its parent (its children INHERIT it); ``merges_<f>``, ``astroobjects_<f>``
and ``astroobjectsmeta_<f>`` are standalone per-field tables and are read by
name, the field taken from the association set's logical key. Every function runs inside the caller's transaction: none
commits or rolls back.

- :func:`result_set_kinds`: each named result set's kind, completeness and key,
  each refused unless the reading run may read it (supervisor step 9 ruling R2).
- :func:`difference_pid`: the `diffimages` row of the difference instance.
- :func:`flagged_sources`, :func:`alertable_sources`: `dev`'s ``iter_sources``
  split, ``flags <> 0`` counted and ``flags = 0`` selected, in the source set.
- :func:`association_chain`: a named association set and the bases it
  extends (crossmatch's ``logical_key.base``), newest first.
- :func:`set_field`: an association set's field, from its logical key.
- :func:`associations`: `dev`'s merges LEFT JOIN astroobjects with the
  statistics columns (``_prefetch_chip`` + ``_stats_sql``), over every named
  association set's chain (an image can span fields; a set's membership is
  its rows plus its bases') and the statistics set that describes each,
  read from step 1's standalone per-field tables ``merges_<f>``,
  ``astroobjects_<f>``, ``astroobjectsmeta_<f>`` by name, as `dev` reads them.
- :func:`history`: the objects' sources through merges anywhere in their
  chain, from any source set, `dev`'s previous-detection prefetch.
- :func:`registered_instances`: which input products have instance rows.
- :func:`attempt_outputs`, :func:`outbox_rows`, :func:`insert_outbox_rows`:
  the outbox and the recovery of an attempt whose commit was uncertain.
- :func:`set_nalertpackets`: `diffimages.nalertpackets` on the run's own row.

The instance rows themselves are written by
``rapidpipe.runs.repository.register_manifest``; this package may not import
``rapidpipe.runs``. Imports ``rapidpipe.db`` only.
"""

from __future__ import annotations

from typing import Any, Sequence

from psycopg2 import sql

from rapidpipe.db import objects as _objects

#: The `sources` columns `dev`'s ``Source`` record reads, plus the joined
#: filter name (``band``) and exposure time, as `dev`'s ``s.*`` query gives them.
SOURCE_COLUMNS: tuple[str, ...] = (
    "sid", "expid", "sca", "mjdobs", "ra", "dec", "xfit", "yfit", "xerr", "yerr",
    "fluxfit", "fluxerr", "flags", "field", "hp6", "hp9", "pid", "isdiffpos", "qfit",
    "cfit", "redchi", "npixfit", "sharpness", "roundness1", "roundness2", "peak",
)

_SOURCE_SELECT = ", ".join(f"s.{c}" for c in SOURCE_COLUMNS) + ", f.filter AS band, e.exptime"

OUTBOX_COLUMNS: tuple[str, ...] = (
    "id", "run", "attempt", "instance", "result_set", "alert_name", "candidate", "object",
    "pid", "first_seen_mjd", "ra", "dec", "record_ordinal", "block_offset", "block_length",
    "record_index", "time_processed_mjd", "schema_version",
)


def _dicts(cur) -> list[dict[str, Any]]:
    columns = [d[0] for d in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def result_set_kinds(cur, instances: Sequence[str], run_id: str) -> dict[str, dict[str, Any]]:
    """``{instance: {kind, complete, key}}`` for each named result set that exists.

    An instance with no `product_instances` row is absent from the result;
    one with no `result_sets` row has ``complete`` None. Every instance that
    exists must be readable by run ``run_id``
    (``rapidpipe.db.objects.assert_readable_result_set``: complete, retained,
    and this run's or a production run's selected output), else
    :class:`ValueError` (the stage maps it to InputRejected).
    """
    cur.execute(
        """
        SELECT pi.id, pi.kind, rs.complete, pi.logical_key
        FROM product_instances pi LEFT JOIN result_sets rs ON rs.instance = pi.id
        WHERE pi.id = ANY(%s)
        """, (list(instances),))
    found = {row[0]: {"kind": row[1], "complete": row[2], "key": row[3]}
             for row in cur.fetchall()}
    for instance in instances:
        if instance in found:
            _objects.assert_readable_result_set(cur, instance, run_id)
    return found


def difference_pid(cur, instance: str) -> int:
    """The `diffimages.pid` a registered difference instance holds; ValueError if none."""
    cur.execute("SELECT pid FROM diffimages WHERE instance = %s", (instance,))
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"no diffimages row for difference instance {instance!r}")
    return int(row[0])


def flagged_sources(cur, source_set: str, pid: int) -> list[tuple[int, int]]:
    """``(sid, flags)`` of the set's ``flags <> 0`` sources on ``pid``, by sid."""
    cur.execute(
        "SELECT sid, flags FROM sources WHERE result_set = %s AND pid = %s AND flags <> 0 "
        "ORDER BY sid", (source_set, pid))
    return [(int(sid), int(flags)) for sid, flags in cur.fetchall()]


def alertable_sources(cur, source_set: str, pid: int) -> list[dict[str, Any]]:
    """The set's ``flags = 0`` sources on ``pid``, by sid, with band and exposure time."""
    cur.execute(
        f"""
        SELECT {_SOURCE_SELECT}
        FROM sources s
        JOIN filters f ON s.fid = f.fid
        JOIN exposures e ON s.expid = e.expid
        WHERE s.result_set = %s AND s.pid = %s AND s.flags = 0
        ORDER BY s.sid
        """, (source_set, pid))
    return _dicts(cur)


def association_chain(cur, instance: str, run_id: str) -> list[str]:
    """The association set and every base it extends, newest first.

    ``rapidpipe.db.objects.association_chain``: crossmatch records the set it
    extends as ``logical_key.base``; a set's membership is its own rows plus
    its bases', recursively. ValueError when a link is missing, wrong, or not
    readable by run ``run_id`` (ruling R2).
    """
    return _objects.association_chain(cur, instance, run_id)


def set_field(key: Any) -> int:
    """The tessellation field an association set's logical key names (``field``, as crossmatch writes it).

    ValueError unless it is a non-negative integer (or its decimal string):
    it becomes part of a table name.
    """
    field = (key or {}).get("field") if isinstance(key, dict) else None
    if isinstance(field, bool):
        field = None
    if isinstance(field, str) and field.isdigit():
        field = int(field)
    if not isinstance(field, int) or field < 0:
        raise ValueError(f"association set key {key!r} does not name an integer field")
    return field


def _table_exists(cur, table: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{table}",))
    return bool(cur.fetchone()[0])


def _field_tables(cur, field: int, *, meta: bool) -> dict[str, sql.Identifier]:
    """The field's ``merges_<f>``/``astroobjects_<f>`` (and ``astroobjectsmeta_<f>``) as identifiers.

    step 1's per-field tables are standalone tables, not children of the
    ``merges``/``astroobjects``/``astroobjectsmeta`` prototypes, so they are
    read by name, as `dev` reads ``merges_<field>``. ValueError when one is
    missing.
    """
    names = _objects.field_table_names(field)
    wanted = ("merges", "astroobjects") + (("astroobjectsmeta",) if meta else ())
    for prefix in wanted:
        if not _table_exists(cur, names[prefix]):
            raise ValueError(f"{names[prefix]} does not exist: no crossmatch/statistics "
                             f"tables for field {field}")
    return {prefix: sql.Identifier(names[prefix]) for prefix in wanted}


def associations(cur, lineages: dict[str, list[str]], fields: dict[str, int],
                 statistics_by_association: dict[str, str | None],
                 sids: Sequence[int]) -> list[dict[str, Any]]:
    """`dev`'s association prefetch over the named association sets' lineages.

    ``lineages`` maps each named association set to its chain (itself, then
    the bases it extends, :func:`association_chain`), ``fields`` to its
    tessellation field (the chain shares it: a base is always the same
    field's), and ``statistics_by_association`` to the statistics set
    describing its membership, or None. Each set is read from its field's
    ``merges_<f>``/``astroobjects_<f>``/``astroobjectsmeta_<f>``, rows of the
    chain only (``result_set``). A trigger's merges row may be in any set of
    the chain (a new detection of a known object lands in the newest set), its
    object in any set of the chain (it stays in the set that made it); where
    several hold the aid, the newest wins. One row per (sid, merges aid, named
    set), in sid, aid, set order; ``association_set`` is the named set.
    ``aid`` is None where no set of the chain holds the object (an orphan).
    ``nsources`` is the statistics row's, else the number of distinct sources
    the aid has in merges across the chain (`dev`'s ``_stats_sql``); an
    (aid, sid) pair present in a base and its extension counts once.
    """
    if not sids or not lineages:
        return []
    rows: list[dict[str, Any]] = []
    for named, chain in lineages.items():
        stats = statistics_by_association.get(named)
        t = _field_tables(cur, fields[named], meta=stats is not None)
        if stats is not None:
            stats_select = sql.SQL("am.stdevra, am.stdevdec, am.nsources::int AS meta_nsources")
            stats_join = sql.SQL("LEFT JOIN {meta} am ON am.aid = a.aid "
                                 "AND am.result_set = %(stats)s").format(meta=t["astroobjectsmeta"])
        else:
            stats_select = sql.SQL("NULL::real AS stdevra, NULL::real AS stdevdec, "
                                   "NULL::int AS meta_nsources")
            stats_join = sql.SQL("")
        cur.execute(sql.SQL(
            """
            SELECT DISTINCT ON (m.sid, m.aid)
                   m.sid, m.aid AS merges_aid, a.aid, a.ra0, a.dec0, {stats_select},
                   (SELECT count(DISTINCT m2.sid) FROM {merges} m2
                    WHERE m2.aid = a.aid AND m2.result_set = ANY(%(chain)s::text[]))::int
                       AS merges_count
            FROM {merges} m
            JOIN unnest(%(chain)s::text[]) WITH ORDINALITY AS lm(member, depth)
                 ON lm.member = m.result_set
            LEFT JOIN LATERAL (
                SELECT o.aid, o.ra0, o.dec0 FROM {astroobjects} o
                JOIN unnest(%(chain)s::text[]) WITH ORDINALITY AS lo(member, depth)
                     ON lo.member = o.result_set
                WHERE o.aid = m.aid ORDER BY lo.depth LIMIT 1) a ON true
            {stats_join}
            WHERE m.sid = ANY(%(sids)s)
            ORDER BY m.sid, m.aid, lm.depth
            """).format(stats_select=stats_select, stats_join=stats_join,
                        merges=t["merges"], astroobjects=t["astroobjects"]),
            {"chain": list(chain), "stats": stats, "sids": list(sids)})
        for row in _dicts(cur):
            meta_nsources = row.pop("meta_nsources")
            merges_count = row.pop("merges_count")
            row["association_set"] = named
            row["nsources"] = (None if row["aid"] is None
                               else meta_nsources if meta_nsources is not None else merges_count)
            rows.append(row)
    return sorted(rows, key=lambda r: (r["sid"], r["merges_aid"], r["association_set"]))


def history(cur, lineages: dict[str, list[str]], fields: dict[str, int],
            objects: Sequence[tuple[str, int]], min_mjd: float) -> list[dict[str, Any]]:
    """Every source of the objects through merges anywhere in their set's chain, oldest first.

    ``objects`` are (named association set, aid) pairs, read from that set's
    field's ``merges_<f>``. The sources may belong to any source set: they are
    the frozen inputs the chain names. ``object_set``/``object_aid`` say which
    object each row belongs to.
    """
    rows: list[dict[str, Any]] = []
    for named, chain in lineages.items():
        aids = sorted({int(aid) for set_, aid in objects if set_ == named})
        if not aids:
            continue
        merges = _field_tables(cur, fields[named], meta=False)["merges"]
        cur.execute(sql.SQL(
            """
            SELECT DISTINCT ON (s.mjdobs, s.sid, m.aid)
                   m.aid AS object_aid, {source_select}
            FROM {merges} m
            JOIN sources s ON s.sid = m.sid
            JOIN filters f ON s.fid = f.fid
            JOIN exposures e ON s.expid = e.expid
            WHERE m.result_set = ANY(%s::text[]) AND m.aid = ANY(%s::bigint[])
              AND s.mjdobs >= %s
            ORDER BY s.mjdobs, s.sid, m.aid
            """).format(source_select=sql.SQL(_SOURCE_SELECT), merges=merges),
            (list(chain), aids, min_mjd))
        for row in _dicts(cur):
            row["object_set"] = named
            rows.append(row)
    return sorted(rows, key=lambda r: (r["mjdobs"], r["sid"]))


def registered_instances(cur, instances: Sequence[str]) -> set[str]:
    """Which of ``instances`` have a ``product_instances`` row (a dev product has none)."""
    if not instances:
        return set()
    cur.execute("SELECT id FROM product_instances WHERE id = ANY(%s)", (list(instances),))
    return {row[0] for row in cur.fetchall()}


def attempt_outputs(cur, attempt: str) -> dict[str, str]:
    """``{kind: instance}`` of the alert-container and alert-set this attempt produced."""
    cur.execute(
        "SELECT kind, id FROM product_instances WHERE producing_attempt = %s "
        "AND kind IN ('alert-container', 'alert-set') ORDER BY id", (attempt,))
    return {kind: instance for kind, instance in cur.fetchall()}


def product_members(cur, instance: str) -> list[dict[str, Any]]:
    cur.execute("SELECT role, path, bytes, sha256 FROM product_members WHERE instance = %s "
                "ORDER BY role", (instance,))
    return _dicts(cur)


def outbox_rows(cur, instance: str) -> list[dict[str, Any]]:
    """The outbox rows of one container, in record order."""
    cur.execute(
        f"SELECT {', '.join(OUTBOX_COLUMNS)} FROM alert_outbox WHERE instance = %s "
        "ORDER BY record_ordinal", (instance,))
    return _dicts(cur)


def insert_outbox_rows(cur, rows: Sequence[dict[str, Any]]) -> None:
    """Insert outbox rows (dicts keyed by :data:`OUTBOX_COLUMNS`)."""
    placeholders = ", ".join(["%s"] * len(OUTBOX_COLUMNS))
    for row in rows:
        cur.execute(
            f"INSERT INTO alert_outbox ({', '.join(OUTBOX_COLUMNS)}) VALUES ({placeholders})",
            tuple(row[c] for c in OUTBOX_COLUMNS))


def set_nalertpackets(cur, instance: str, run: str, packets: int = 1) -> int:
    """Set ``diffimages.nalertpackets`` on the run's own row; return the rows updated."""
    cur.execute("UPDATE diffimages SET nalertpackets = %s WHERE instance = %s AND run = %s",
                (packets, instance, run))
    return cur.rowcount
