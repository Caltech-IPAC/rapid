"""Persistence for the `alerts` stage: its reads of the catalog sets, and the outbox.

The database side of `dev`'s ``alerts/providers.py`` (``iter_sources``,
``_prefetch_chip``, ``_stats_sql``), reading named result sets instead of
"the chip's rows": every query names the source set, the association set
and the statistics set by instance id (products page: "A stage reads a
result set by id, never 'whatever is current'"), and reads the PARENT
tables ``sources``, ``merges``, ``astroobjects`` and ``astroobjectsmeta``,
whose rows live in inheriting children (`dev`'s ``merges_<field>`` and
siblings). Every function runs inside the caller's transaction: none
commits or rolls back.

- :func:`result_set_kinds`: each named result set's kind, completeness and key.
- :func:`difference_pid`: the `diffimages` row of the difference instance.
- :func:`flagged_sources`, :func:`alertable_sources`: `dev`'s ``iter_sources``
  split, ``flags <> 0`` counted and ``flags = 0`` selected, in the source set.
- :func:`associations`: `dev`'s merges LEFT JOIN astroobjects with the
  statistics columns (``_prefetch_chip`` + ``_stats_sql``), over every named
  association set (an image can span fields) and the statistics set that
  describes each.
- :func:`history`: the objects' sources through merges of their association
  set, from any source set, `dev`'s previous-detection prefetch.
- :func:`attempt_outputs`, :func:`outbox_rows`, :func:`insert_outbox_rows`:
  the outbox and the recovery of an attempt whose commit was uncertain.
- :func:`set_nalertpackets`: `diffimages.nalertpackets` on the run's own row.

The instance rows themselves are written by
``rapidpipe.runs.repository.register_manifest``; this package may not import
``rapidpipe.runs``. Imports ``rapidpipe.db`` only.
"""

from __future__ import annotations

from typing import Any, Sequence

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


def result_set_kinds(cur, instances: Sequence[str]) -> dict[str, dict[str, Any]]:
    """``{instance: {kind, complete, key}}`` for each named result set that exists.

    An instance with no `product_instances` row is absent from the result;
    one with no `result_sets` row has ``complete`` None.
    """
    cur.execute(
        """
        SELECT pi.id, pi.kind, rs.complete, pi.logical_key
        FROM product_instances pi LEFT JOIN result_sets rs ON rs.instance = pi.id
        WHERE pi.id = ANY(%s)
        """, (list(instances),))
    return {row[0]: {"kind": row[1], "complete": row[2], "key": row[3]}
            for row in cur.fetchall()}


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


def associations(cur, statistics_by_association: dict[str, str | None],
                 sids: Sequence[int]) -> list[dict[str, Any]]:
    """`dev`'s association prefetch over one or more association sets.

    ``statistics_by_association`` maps each named association set to the
    statistics set that describes it, or None. One row per (sid, merges aid,
    association set), in sid, aid, set order. ``aid`` is None where the
    merges row has no astroobjects row in its set (an orphan).
    ``stdevra``/``stdevdec`` come from the describing statistics set's
    astroobjectsmeta row; ``nsources`` from it, else the aid's merges count in
    its association set (`dev`'s ``_stats_sql``).
    """
    if not sids or not statistics_by_association:
        return []
    assocs = list(statistics_by_association)
    stats = [statistics_by_association[a] for a in assocs]
    cur.execute(
        """
        SELECT m.sid, m.aid AS merges_aid, m.result_set AS association_set,
               a.aid, a.ra0, a.dec0, am.stdevra, am.stdevdec,
               COALESCE(am.nsources::int,
                        (SELECT count(*) FROM merges m2
                         WHERE m2.aid = a.aid AND m2.result_set = m.result_set)::int) AS nsources
        FROM merges m
        LEFT JOIN astroobjects a ON a.aid = m.aid AND a.result_set = m.result_set
        LEFT JOIN unnest(%(assocs)s::text[], %(stats)s::text[]) AS sm(assoc, stats)
               ON sm.assoc = m.result_set
        LEFT JOIN astroobjectsmeta am ON am.aid = a.aid AND am.result_set = sm.stats
        WHERE m.result_set = ANY(%(assocs)s::text[]) AND m.sid = ANY(%(sids)s)
        ORDER BY m.sid, m.aid, m.result_set
        """, {"assocs": assocs, "stats": stats, "sids": list(sids)})
    return _dicts(cur)


def history(cur, objects: Sequence[tuple[str, int]], min_mjd: float) -> list[dict[str, Any]]:
    """Every source of the objects through their association set's merges, oldest first.

    ``objects`` are (association set, aid) pairs. The sources may belong to
    any source set: they are the frozen inputs the association set names.
    ``object_set``/``object_aid`` say which object each row belongs to.
    """
    if not objects:
        return []
    cur.execute(
        f"""
        SELECT m.result_set AS object_set, m.aid AS object_aid, {_SOURCE_SELECT}
        FROM unnest(%s::text[], %s::bigint[]) AS o(result_set, aid)
        JOIN merges m ON m.result_set = o.result_set AND m.aid = o.aid
        JOIN sources s ON s.sid = m.sid
        JOIN filters f ON s.fid = f.fid
        JOIN exposures e ON s.expid = e.expid
        WHERE s.mjdobs >= %s
        ORDER BY s.mjdobs, s.sid
        """, ([o[0] for o in objects], [int(o[1]) for o in objects], min_mjd))
    return _dicts(cur)


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
