"""`crossmatch`: associate one field's sources with AstroObjects, as `dev` does, in two passes.

Ported from `origin/dev`'s ``pipeline/crossMatchSources.py``, one field's
worth of it: `dev` scans a processing date's jobs for their sources tables
and distinct fields and loops over the fields in parallel; the rebuild's
unit is one field (a Roman tessellation rtid), and the sources it reads are
named by ``source-set`` instances in the input manifest, so the date scan,
the field fan-out and the process pool are the launcher's, not the stage's
(supervisor step 1, rulings R2, 2026-09-24). What remains runs in `dev`'s
order, in one transaction:

advisory lock on the field -> make ``astroobjects_<field>`` and
``merges_<field>`` if new -> stage 1: the field's exposures in ascending
MJD; per exposure, per source set, match its unflagged sources in the field
against the catalog (``q3c_join`` within ``match_radius``) and write a
merges row per match; give every unmatched source a new object
(``aid = radec_index(ra, dec)``) and a merges row; COPY both files after
each exposure, so the next exposure matches against this one's new
objects -> CLUSTER ``astroobjects_<field>`` and ANALYZE both tables ->
stage 2: per neighbouring field, per source set, match that field's
unflagged sources against the catalog (inside the field's inclusion cone
when it has eight neighbours) and write merges rows only -> verify the
counts -> register the association set -> commit.

The catalog (ruling R3, "base plus delta"). An association set's rows are
its own plus its base's, recursively. The input manifest names at most one
base ``association-set`` (the field's prior catalog, frozen by whoever
composed the inputs); a pass reads ``astroobjects`` rows whose
``result_set`` is in the base's chain or is this attempt's own new set --
never "whatever is current". Rows written before the run model
(``run IS NULL``) are read only with ``[crossmatch] legacy_catalog``.

Inputs. ``--inputs`` is a manifest carrying one or more ``source-set``
entries (a ``load`` completion manifest, or an input-set manifest listing
several) and at most one ``association-set`` entry, the base. Each source
set's child table is found through its instance
(``rapidpipe.db.objects.source_set_table``), never by date arithmetic.
``inputs.result_sets`` lists the source sets and the base.

Output. One ``association-set`` result set: logical key ``{field, base,
source_sets, settings_hash}``, rows in ``astroobjects_<field>`` and
``merges_<field>`` carrying the run, this attempt and the new instance.
Rows and the instance's registration are written in one transaction, so a
set is complete or absent. The registration comes after the passes, not
before the rows as in ``load``: ``register_manifest`` writes the set's
``row_count`` when it inserts the ``result_sets`` row, and the count is
known only once both passes have run; the per-field tables carry no
foreign keys, so nothing requires the instance row first.

Idempotence. A retry is a new attempt with a new instance: rows are unique
per set (``UNIQUE (result_set, aid)`` and ``(result_set, aid, sid)``,
ruling R4), and each COPY goes through a temporary table and ``INSERT ...
ON CONFLICT DO NOTHING``, which drops repeats within one attempt (two
unmatched sources at one position in one exposure make one object, as
`dev`'s ``pruneRedundantMerges`` would leave). With ``[crossmatch]
done_check`` on, a complete set already written in this run for the same
logical key is reused and nothing is written (ruling R14). Two attempts on
one field are serialised by ``pg_advisory_xact_lock`` (ruling R13).

This module may import ``rapidpipe.products``, ``rapidpipe.db``,
``rapidpipe.runs`` and ``rapidpipe.science``; never another stage,
``rapidpipe.launch`` or ``rapidpipe.cli``.
"""

from __future__ import annotations

import contextlib
import importlib
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterator, Sequence

from rapidpipe.db import connection as _connection_module
from rapidpipe.db import objects as _objects
from rapidpipe.db.connection import ConnectionUnavailable
from rapidpipe.db.ids import new_ulid
from rapidpipe.products.manifest import Manifest, OutputEntry
from rapidpipe.runs.repository import register_manifest
from rapidpipe.science.crossmatch import catalog
from rapidpipe.science.spatial import field_neighbours
from rapidpipe.stages.contract import (
    InputRejected,
    StageContext,
    StageDeclaration,
    StageError,
    StageResult,
    TransientFailure,
    UsageError,
    run_stage,
)

_SETTINGS_PATH = Path(__file__).resolve().parent.parent / "settings" / "crossmatch.toml"

#: The first key of the field's transaction-scoped advisory lock (ruling
#: R13); the second is the field. Fixed, distinct from the child-table
#: functions' own locks.
ADVISORY_LOCK_CLASS = 20260924

#: `dev`'s stage 2 uses the inclusion cone only for a tile with this many
#: neighbours ("If away from poles, a sky tile will have 8 adjacent fields").
CONE_NEIGHBOURS = 8

DECLARATION = StageDeclaration(
    name="crossmatch",
    unit="field",
    argument_schema={
        "description": (
            "rapidpipe stage crossmatch --run <run-id> --unit <rtid> "
            "--attempt <attempt-id> --inputs <dir> --outputs <dir> "
            "[--settings <toml>] [--dry-run]. --inputs holds a manifest of one "
            "or more complete source-set entries and at most one association-set "
            "entry (the field's base catalog); the unit is the field's decimal rtid."
        ),
    },
    settings_schema_path=str(_SETTINGS_PATH),
    consumes=("source-set",),
    produces=("association-set",),
    database_access="read-write",
    resource_defaults={"vcpus": 1, "memory_mib": 4096},
)

# ----------------------------------------------------------------------
# `dev`'s four SELECTs, with the rebuild's result-set scoping
# ----------------------------------------------------------------------

#: Stage 1's exposure list (`dev` L228-229), restricted to one source set.
EXPOSURES_SQL = ("SELECT DISTINCT expid, mjdobs FROM {sources} "
                 "WHERE field = %s AND flags = %s AND result_set = %s")

#: Stage 1's Query A (`dev` L282-285): one exposure's sources in the field
#: matched against the catalog.
FIELD_MATCHES_SQL = ("SELECT a.sid, b.aid FROM {sources} AS a, {astroobjects} AS b "
                     "WHERE q3c_join(a.ra, a.dec, b.ra0, b.dec0, %s) "
                     "AND a.field = %s AND a.expid = %s AND a.flags = %s "
                     "AND a.result_set = %s AND {catalog}")

#: Stage 1's Query B (`dev` L363-364): the same exposure's sources, for the unmatched ones.
FIELD_SOURCES_SQL = ("SELECT sid, ra, dec, fluxfit FROM {sources} "
                     "WHERE field = %s AND expid = %s AND flags = %s AND result_set = %s")

#: Stage 2 (`dev` L728-733): a neighbouring field's sources inside the cone
#: matched against the catalog.
NEIGHBOUR_MATCHES_CONE_SQL = ("SELECT a.sid, b.aid FROM {sources} AS a, {astroobjects} AS b "
                              "WHERE q3c_radial_query(a.ra, a.dec, %s, %s, %s) "
                              "AND q3c_join(a.ra, a.dec, b.ra0, b.dec0, %s) "
                              "AND a.field = %s AND a.flags = %s "
                              "AND a.result_set = %s AND {catalog}")

#: Stage 2 without the cone (`dev` L737-741), for a tile without eight neighbours.
NEIGHBOUR_MATCHES_SQL = ("SELECT a.sid, b.aid FROM {sources} AS a, {astroobjects} AS b "
                         "WHERE q3c_join(a.ra, a.dec, b.ra0, b.dec0, %s) "
                         "AND a.field = %s AND a.flags = %s "
                         "AND a.result_set = %s AND {catalog}")


def catalog_clause(catalog_sets: Sequence[str], legacy_catalog: bool) -> tuple[str, tuple]:
    """The WHERE clause on ``b`` naming the catalog rows a pass reads (ruling R3)."""
    clause, params = _objects.set_rows_clause("b", catalog_sets)
    if legacy_catalog:
        clause = f"({clause} OR b.run IS NULL)"
    else:
        clause = f"({clause})"
    return clause, params


# ----------------------------------------------------------------------
# The database, replaceable in tests
# ----------------------------------------------------------------------


def connect(*args, **kwargs):
    """Module-level indirection to ``rapidpipe.db.connection.connect``, for tests."""
    return _connection_module.connect(*args, **kwargs)


class PostgresCrossmatchDatabase:
    """The stage's database operations on one connection, in one transaction."""

    def __init__(self, conn) -> None:
        self.conn = conn

    def _rows(self, sql: str, params: tuple) -> list[tuple]:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())

    def lock_field(self, field: int) -> None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s, %s)", (ADVISORY_LOCK_CLASS, field))

    def source_set_table(self, instance: str) -> tuple[str, int | None]:
        with self.conn.cursor() as cur:
            return _objects.source_set_table(cur, instance)

    def association_chain(self, instance: str) -> list[str]:
        with self.conn.cursor() as cur:
            return _objects.association_chain(cur, instance)

    def find_complete_result_set(self, kind: str, run_id: str, key: dict[str, Any]):
        with self.conn.cursor() as cur:
            return _objects.find_complete_result_set(cur, kind, run_id, key)

    def ensure_field_object_tables(self, field: int) -> bool:
        with self.conn.cursor() as cur:
            return _objects.ensure_field_object_tables(cur, field)

    def exposures(self, table: str, field: int, flags: int, source_set: str) -> list[tuple]:
        return self._rows(EXPOSURES_SQL.format(sources=table), (field, flags, source_set))

    def field_matches(self, table: str, field: int, expid: int, flags: int, source_set: str,
                      match_radius: float, catalog_sets: Sequence[str],
                      legacy_catalog: bool) -> list[tuple]:
        clause, clause_params = catalog_clause(catalog_sets, legacy_catalog)
        sql = FIELD_MATCHES_SQL.format(sources=table, catalog=clause,
                                       astroobjects=_objects.field_table_names(field)["astroobjects"])
        return self._rows(sql, (match_radius, field, expid, flags, source_set, *clause_params))

    def field_sources(self, table: str, field: int, expid: int, flags: int,
                      source_set: str) -> list[tuple]:
        return self._rows(FIELD_SOURCES_SQL.format(sources=table),
                          (field, expid, flags, source_set))

    def neighbour_matches(self, table: str, field: int, neighbour: int, flags: int,
                          source_set: str, match_radius: float, catalog_sets: Sequence[str],
                          legacy_catalog: bool,
                          cone: tuple[float, float, float] | None) -> list[tuple]:
        clause, clause_params = catalog_clause(catalog_sets, legacy_catalog)
        astroobjects = _objects.field_table_names(field)["astroobjects"]
        tail = (match_radius, neighbour, flags, source_set, *clause_params)
        if cone is not None:
            sql = NEIGHBOUR_MATCHES_CONE_SQL.format(sources=table, astroobjects=astroobjects,
                                                    catalog=clause)
            return self._rows(sql, (*cone, *tail))
        sql = NEIGHBOUR_MATCHES_SQL.format(sources=table, astroobjects=astroobjects,
                                           catalog=clause)
        return self._rows(sql, tail)

    def copy_astroobjects(self, field: int, csv_path: Path) -> int:
        with self.conn.cursor() as cur, csv_path.open() as fh:
            return _objects.copy_astroobjects(cur, field, fh)

    def copy_merges(self, field: int, csv_path: Path) -> int:
        with self.conn.cursor() as cur, csv_path.open() as fh:
            return _objects.copy_merges(cur, field, fh)

    def cluster_field_object_tables(self, field: int) -> None:
        with self.conn.cursor() as cur:
            _objects.cluster_field_object_tables(cur, field)

    def count_result_set_rows(self, table: str, result_set: str) -> int:
        with self.conn.cursor() as cur:
            return _objects.count_result_set_rows(cur, table, result_set)

    def register_association_set(self, manifest: dict[str, Any], attempt_id: str) -> None:
        register_manifest(self.conn, manifest, registering_attempt_id=attempt_id)

    def commit(self) -> None:
        self.conn.commit()


#: Names a ``module:factory`` returning a context manager that yields an
#: object with :class:`PostgresCrossmatchDatabase`'s methods, used instead of
#: PostgreSQL. Unset in every deployment; the stage fixture
#: (``make stage-crossmatch``) sets it, since a subprocess cannot be monkeypatched.
DATABASE_ENV = "RAPIDPIPE_CROSSMATCH_DATABASE"


@contextlib.contextmanager
def _postgres() -> Iterator[PostgresCrossmatchDatabase]:
    with connect() as conn:
        try:
            yield PostgresCrossmatchDatabase(conn)
        except BaseException:
            conn.rollback()
            raise


def open_database():
    """PostgreSQL, unless ``RAPIDPIPE_CROSSMATCH_DATABASE`` names another; tests monkeypatch this."""
    override = os.environ.get(DATABASE_ENV)
    if not override:
        return _postgres()
    module_name, _, factory_name = override.partition(":")
    try:
        factory = getattr(importlib.import_module(module_name), factory_name)
    except (ImportError, AttributeError) as exc:
        raise UsageError(f"{DATABASE_ENV}={override!r} does not name a factory: {exc}") from exc
    return factory()


# ----------------------------------------------------------------------
# Unit, settings and inputs
# ----------------------------------------------------------------------

_UNIT_ID_RE = re.compile(r"[0-9]+")


def parse_unit_id(unit_id: str) -> int:
    """The field (tessellation rtid) a unit id names; a decimal non-negative integer."""
    if not _UNIT_ID_RE.fullmatch(unit_id or ""):
        raise UsageError(f"unit id {unit_id!r} is not a field (a decimal tessellation rtid)")
    return int(unit_id)


def _check_settings(settings: dict[str, Any]) -> None:
    radius = settings["source_matching"]["match_radius"]
    if not isinstance(radius, (int, float)) or isinstance(radius, bool) or radius <= 0:
        raise UsageError(f"[source_matching] match_radius must be a positive number, got {radius!r}")
    flags = settings["crossmatch"]["source_flags"]
    if not isinstance(flags, int) or isinstance(flags, bool):
        raise UsageError(f"[crossmatch] source_flags must be an integer, got {flags!r}")
    for key in ("cluster_between_passes", "legacy_catalog", "done_check"):
        if not isinstance(settings["crossmatch"][key], bool):
            raise UsageError(f"[crossmatch] {key} must be true or false")


def _source_set_entries(manifest: Manifest) -> list[OutputEntry]:
    entries = [e for e in manifest.outputs if e.kind == "source-set"]
    if not entries:
        raise InputRejected("input manifest has no source-set entries")
    return entries


def _base_entry(manifest: Manifest, field: int) -> OutputEntry | None:
    entries = [e for e in manifest.outputs if e.kind == "association-set"]
    if len(entries) > 1:
        raise InputRejected(
            f"input manifest has {len(entries)} association-set entries; at most one base")
    if not entries:
        return None
    base = entries[0]
    base_field = base.key.get("field")
    if isinstance(base_field, bool) or not isinstance(base_field, (int, str)) \
            or str(base_field) != str(field):
        raise InputRejected(
            f"base association-set {base.instance!r} is for field {base_field!r}, "
            f"not the unit's field {field}")
    return base


# ----------------------------------------------------------------------
# The body
# ----------------------------------------------------------------------


def _entry(instance: str, key: dict[str, Any], field: int,
           row_counts: dict[str, int]) -> OutputEntry:
    tables = _objects.field_table_names(field)
    return OutputEntry(
        kind="association-set", format_version="1", instance=instance, key=key,
        registration={"astroobjects_table": tables["astroobjects"],
                      "merges_table": tables["merges"], "row_counts": row_counts})


def _pass1(db, field: int, sets: list[tuple[str, str]], flags: int, match_radius: float,
           catalog_sets: list[str], legacy_catalog: bool, run_columns: tuple[str, str, str],
           work: Path, log) -> dict[str, int]:
    """`dev`'s stage 1 for one field; returns the counts it wrote."""
    pairs: list[tuple[Any, Any]] = []
    sets_by_expid: dict[Any, list[tuple[str, str]]] = {}
    for table, instance in sets:
        for expid, mjdobs in db.exposures(table, field, flags, instance):
            pairs.append((expid, mjdobs))
            sets_by_expid.setdefault(expid, []).append((table, instance))
    expids = catalog.expid_order(pairs)
    log.info("field %s: %s exposures to match, in ascending MJD", field, len(expids))

    counts = {"expids": len(expids), "new_objects": 0, "astroobjects": 0, "merges": 0}
    astro_path = work / f"astroobjects_{field}.csv"
    merges_path = work / f"merges_{field}.csv"
    for expid in expids:
        with astro_path.open("w") as astro_fh, merges_path.open("w") as merges_fh:
            # dev queries every sources table for every exposure; only the
            # sets that returned this exposure can hold its rows.
            for table, instance in sets_by_expid[expid]:
                matched: set[Any] = set()
                for sid, aid in db.field_matches(table, field, expid, flags, instance,
                                                 match_radius, catalog_sets, legacy_catalog):
                    matched.add(sid)
                    merges_fh.write(catalog.merges_line(aid, sid, run_columns))
                for sid, ra, dec, fluxfit in db.field_sources(table, field, expid, flags,
                                                              instance):
                    if sid in matched:
                        continue
                    aid = catalog.new_object_id(ra, dec)
                    astro_fh.write(catalog.astroobjects_line(aid, ra, dec, fluxfit, run_columns))
                    merges_fh.write(catalog.merges_line(aid, sid, run_columns))
                    counts["new_objects"] += 1
        counts["astroobjects"] += db.copy_astroobjects(field, astro_path)
        counts["merges"] += db.copy_merges(field, merges_path)
    return counts


def _pass2(db, field: int, sets: list[tuple[str, str]], flags: int, match_radius: float,
           catalog_sets: list[str], legacy_catalog: bool, run_columns: tuple[str, str, str],
           work: Path, log) -> tuple[int, dict[str, Any]]:
    """`dev`'s stage 2 for one field; returns the merges rows inserted and notes."""
    neighbours = field_neighbours(field)
    cone = None
    if len(neighbours) == CONE_NEIGHBOURS:
        cone = catalog.field_inclusion_cone(field, match_radius)
    merges_path = work / f"merges_{field}_neighbours.csv"
    with merges_path.open("w") as merges_fh:
        for neighbour in neighbours:
            for table, instance in sets:
                for sid, aid in db.neighbour_matches(table, field, neighbour, flags, instance,
                                                     match_radius, catalog_sets, legacy_catalog,
                                                     cone):
                    merges_fh.write(catalog.merges_line(aid, sid, run_columns))
    inserted = db.copy_merges(field, merges_path)
    log.info("field %s: %s neighbouring fields, cone %s; %s merges rows across the boundaries",
             field, len(neighbours), "on" if cone else "off", inserted)
    notes = {"neighbours": len(neighbours), "cone_deg": cone[2] if cone else None}
    return inserted, notes


def _body(context: StageContext) -> StageResult:
    settings = context.settings
    _check_settings(settings)
    log = context.logger
    field = parse_unit_id(context.unit_id)
    xm = settings["crossmatch"]
    flags = int(xm["source_flags"])
    legacy_catalog = bool(xm["legacy_catalog"])
    match_radius = float(settings["source_matching"]["match_radius"])

    manifest = context.input_manifest
    source_set_ids = sorted({e.instance for e in _source_set_entries(manifest)})
    base = _base_entry(manifest, field)
    base_id = base.instance if base is not None else None
    result_sets_read = sorted(source_set_ids + ([base_id] if base_id else []))
    key = {"field": field, "base": base_id, "source_sets": source_set_ids,
           "settings_hash": context.settings_hash}
    tables = _objects.field_table_names(field)

    try:
        with open_database() as db:
            db.lock_field(field)
            sets = [(db.source_set_table(instance)[0], instance) for instance in source_set_ids]
            chain = db.association_chain(base_id) if base_id else []

            if xm["done_check"]:
                existing = db.find_complete_result_set("association-set", context.run_id, key)
                if existing is not None:
                    instance, row_count = existing
                    log.warning("association set %s for this key is already complete in this "
                                "run; reusing it", instance)
                    row_counts = {
                        "astroobjects": db.count_result_set_rows(tables["astroobjects"], instance),
                        "merges": int(row_count or 0)}
                    return StageResult(outputs=[_entry(instance, key, field, row_counts)],
                                       result_sets_read=result_sets_read,
                                       execution_notes={"done_check": {"reused": instance}})

            made = db.ensure_field_object_tables(field)
            if made:
                log.info("made %s and %s", tables["astroobjects"], tables["merges"])

            instance = new_ulid()
            run_columns = (context.run_id, context.attempt_id, instance)
            catalog_sets = chain + [instance]
            context.outputs_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=context.outputs_dir) as work_dir:
                work = Path(work_dir)
                pass1 = _pass1(db, field, sets, flags, match_radius, catalog_sets,
                               legacy_catalog, run_columns, work, log)
                log.info("field %s pass 1: %s new objects (%s inserted), %s merges rows",
                         field, pass1["new_objects"], pass1["astroobjects"], pass1["merges"])
                if xm["cluster_between_passes"]:
                    db.cluster_field_object_tables(field)
                merges_pass2, pass2_notes = _pass2(db, field, sets, flags, match_radius,
                                                   catalog_sets, legacy_catalog, run_columns,
                                                   work, log)

            row_counts = {
                "astroobjects": pass1["astroobjects"],
                "merges": pass1["merges"] + merges_pass2,
                "merges_pass1": pass1["merges"],
                "merges_pass2": merges_pass2,
                "new_objects": pass1["new_objects"],
            }
            for prefix in ("astroobjects", "merges"):
                found = db.count_result_set_rows(tables[prefix], instance)
                if found != row_counts[prefix]:
                    raise StageError(
                        f"{tables[prefix]}: {found} rows for association set {instance}, "
                        f"{row_counts[prefix]} inserted")

            entry = _entry(instance, key, field, row_counts)
            db.register_association_set({
                "run": context.run_id, "stage": "crossmatch", "attempt": context.attempt_id,
                "inputs": {"products": {}, "result_sets": result_sets_read},
                "outputs": [{**entry.to_dict(), "row_count": row_counts["merges"]}],
            }, context.attempt_id)
            db.commit()
            log.info("field %s: association set %s, %s objects, %s merges rows (%s + %s)",
                     field, instance, row_counts["astroobjects"], row_counts["merges"],
                     row_counts["merges_pass1"], row_counts["merges_pass2"])
    except ConnectionUnavailable as exc:
        raise TransientFailure(f"could not connect to the database: {exc}") from exc
    except _connection_module.psycopg2.OperationalError as exc:
        raise TransientFailure(f"database connection lost mid-transaction: {exc}") from exc
    except ValueError as exc:
        raise InputRejected(str(exc)) from exc

    notes = {"exposures": pass1["expids"], "base_chain": chain,
             "clustered": bool(xm["cluster_between_passes"]), "made_tables": made, **pass2_notes}
    return StageResult(outputs=[entry], result_sets_read=result_sets_read,
                       execution_notes=notes)


def main(argv: list[str]) -> int:
    return run_stage(DECLARATION, _body, argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
