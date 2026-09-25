"""`statistics`: per-object position and flux statistics for one field's association set.

Ported from `origin/dev`'s ``pipeline/computeStatisticsForAstroObjects.py``,
one field's worth of it: `dev` scans a processing date's fields and runs
them in a process pool; the rebuild's unit is one field (a Roman
tessellation rtid), so the date scan, the field loop and the pool are the
launcher's, not the stage's. What remains runs in `dev`'s order:

make ``astroobjectsmeta_<field>`` if it is new -> one UNION ALL query of
the objects' ``merges`` rows joined to their ``sources`` rows, one SELECT
per source set -> per object, `dev`'s ``compute_radec_statistics`` and the
flux mean and standard deviation -> one CSV in `dev`'s column order ->
COPY -> the loaded row count read back and checked.

Inputs. ``--inputs`` is ``crossmatch``'s completion manifest: exactly one
``association-set`` entry, whose ``key.field`` must equal the unit. The
set's membership is base plus delta (step 1 ruling R3): its own rows plus
its base's, recursively (``rapidpipe.db.objects.association_chain``). The
sources an object's statistics are drawn from are the rows of the source
sets named in the ``source_sets`` key of every set in that chain, each
resolved to its ``sources`` child table by instance
(``rapidpipe.db.objects.source_set_table``). This replaces `dev`'s
``l2files`` overlap lookup, its ``pg_class`` existence check and its
``diffimages.vbest > 0`` filter (ruling R7): the association set names
exactly the source sets it was made from.

Not ported (ruling R7): `dev`'s three DELETEs on ``astroobjects_<field>``
(the aid self-dedupe, orphans without ``merges`` rows, objects with no
best source) -- a stage never mutates another set's rows, and the
set-scoped UNIQUE constraints make the self-dedupe moot; `dev`'s
drop-and-recreate of ``astroobjectsmeta_<field>``, which becomes one new
statistics set per attempt; and `dev`'s CLUSTER of the table on its
``meanradec`` index and its VACUUM ANALYZE (the child-table function makes
the indexes when it makes the table, and the table is appended to, not
rebuilt).

Output. One ``statistics-set`` result set (products page, "Database result
sets"): unit field, logical key ``{"membership": <association-set
instance>}``, rows in ``astroobjectsmeta_<field>``, every row carrying the
run, this attempt and the result-set instance. The instance row, its
``result_sets`` row and the rows are written in one transaction, so a set
is either complete or absent; an association set whose membership has no
``merges`` rows gives an empty set, still complete. With
``[statistics] done_check`` on, a complete statistics set with the same key
already written in this run is reused (ruling R14), only when its
producing attempt is this attempt or one that succeeded (supervisor step 9
ruling R1).

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
from rapidpipe.science.statistics import lightcurve
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

_SETTINGS_PATH = Path(__file__).resolve().parent.parent / "settings" / "statistics.toml"

#: ``[statistics] membership`` values: what the statistics describe.
#: Only the association set is built; the pruned set is designed in on the
#: products page and left unused (lead, 2026-09-22).
MEMBERSHIPS = ("association",)
DESIGNED_IN_MEMBERSHIPS = ("pruned",)

DECLARATION = StageDeclaration(
    name="statistics",
    unit="field",
    argument_schema={
        "description": (
            "rapidpipe stage statistics --run <run-id> --unit <rtid> "
            "--attempt <attempt-id> --inputs <dir> --outputs <dir> "
            "[--settings <toml>] [--dry-run]. --inputs holds crossmatch's "
            "completion manifest: one association-set entry for the unit's "
            "field, already registered."
        ),
    },
    settings_schema_path=str(_SETTINGS_PATH),
    consumes=("association-set",),
    produces=("statistics-set",),
    database_access="read-write",
    resource_defaults={"vcpus": 1, "memory_mib": 4096},
)

_UNIT_ID_RE = re.compile(r"[0-9]+")


def parse_unit_id(unit_id: str) -> int:
    """The unit id as a field (rtid): a non-negative decimal integer, else exit 64."""
    if not _UNIT_ID_RE.fullmatch(unit_id or ""):
        raise UsageError(
            f"unit id {unit_id!r} is not a field: a non-negative decimal rtid, e.g. '4711398'")
    return int(unit_id)


# ----------------------------------------------------------------------
# The database, replaceable in tests
# ----------------------------------------------------------------------


def connect(*args, **kwargs):
    """Module-level indirection to ``rapidpipe.db.connection.connect``, for tests."""
    return _connection_module.connect(*args, **kwargs)


class PostgresStatisticsDatabase:
    """The stage's database operations on one connection, in one transaction."""

    def __init__(self, conn) -> None:
        self.conn = conn

    def association_chain(self, instance: str, run_id: str) -> list[str]:
        with self.conn.cursor() as cur:
            return _objects.association_chain(cur, instance, run_id)

    def chain_source_sets(self, chain: Sequence[str]) -> list[str]:
        """The source-set instances named by every set of ``chain``, sorted, each once.

        Refuses (:class:`ValueError`) a chain member with no complete
        ``result_sets`` row, or whose key's ``source_sets`` is not a list of
        instance ids.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT pi.id, pi.logical_key->'source_sets', rs.complete
                FROM product_instances pi LEFT JOIN result_sets rs ON rs.instance = pi.id
                WHERE pi.id = ANY(%s)
                """,
                (list(chain),))
            rows = {row[0]: (row[1], row[2]) for row in cur.fetchall()}
        found: set[str] = set()
        for instance in chain:
            if instance not in rows:
                raise ValueError(f"association set {instance!r} is not registered")
            source_sets, complete = rows[instance]
            if not complete:
                raise ValueError(f"association set {instance!r} is not a complete result set")
            if source_sets is None:
                source_sets = []
            if not isinstance(source_sets, list) or not all(
                    isinstance(s, str) and s for s in source_sets):
                raise ValueError(
                    f"association set {instance!r}: key source_sets is not a list of "
                    f"instance ids: {source_sets!r}")
            found.update(source_sets)
        return sorted(found)

    def source_set_table(self, instance: str, run_id: str) -> str:
        with self.conn.cursor() as cur:
            return _objects.source_set_table(cur, instance, run_id)[0]

    def find_complete_statistics_set(self, run_id: str, key: dict[str, Any], attempt_id: str):
        with self.conn.cursor() as cur:
            return _objects.find_complete_result_set(cur, "statistics-set", run_id, key,
                                                     attempt_id)

    def ensure_astroobjectsmeta_table(self, field: int) -> bool:
        with self.conn.cursor() as cur:
            return _objects.ensure_astroobjectsmeta_table(cur, field)

    def _merges_table(self, cur, field: int) -> str:
        merges = _objects.field_table_names(field)["merges"]
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{merges}",))
        if not cur.fetchone()[0]:
            raise ValueError(f"{merges} does not exist: the association set has no rows table")
        return merges

    def member_rows(self, field: int, chain: Sequence[str],
                    source_tables: Sequence[tuple[str, str]]) -> list[tuple]:
        """`dev`'s UNION ALL query over the set's membership: ``(aid, sid, ra, dec, fluxfit)`` rows.

        One SELECT per ``(sources table, source-set instance)``: the
        ``merges_<field>`` rows of every set in ``chain`` joined on ``sid``
        to that source set's rows (ruling R7: the membership replaces
        `dev`'s ``diffimages.vbest > 0`` join).
        """
        if not source_tables:
            return []
        with self.conn.cursor() as cur:
            merges = self._merges_table(cur, field)
            clause, clause_params = _objects.set_rows_clause("a", chain)
            parts: list[str] = []
            params: list[Any] = []
            for table, source_set in source_tables:
                parts.append(
                    f"SELECT a.aid, b.sid, b.ra, b.dec, b.fluxfit FROM {merges} AS a "
                    f"JOIN {table} AS b ON a.sid = b.sid "
                    f"WHERE {clause} AND b.result_set = %s")
                params.extend([*clause_params, source_set])
            cur.execute(" UNION ALL ".join(parts), params)
            return cur.fetchall()

    def objects_in_set(self, field: int, chain: Sequence[str]) -> int:
        """Distinct ``aid`` among the ``merges_<field>`` rows of ``chain``."""
        with self.conn.cursor() as cur:
            merges = self._merges_table(cur, field)
            clause, params = _objects.set_rows_clause("m", chain)
            cur.execute(f"SELECT count(DISTINCT m.aid) FROM {merges} AS m WHERE {clause}", params)
            return int(cur.fetchone()[0])

    def register_statistics_set(self, manifest: dict[str, Any], attempt_id: str) -> None:
        register_manifest(self.conn, manifest, registering_attempt_id=attempt_id)

    def copy_astroobjectsmeta(self, field: int, csv_path: Path) -> int:
        with self.conn.cursor() as cur, csv_path.open() as fh:
            return _objects.copy_astroobjectsmeta(cur, field, fh)

    def count_result_set_rows(self, table: str, result_set: str) -> int:
        with self.conn.cursor() as cur:
            return _objects.count_result_set_rows(cur, table, result_set)

    def commit(self) -> None:
        self.conn.commit()


#: Names a ``module:factory`` returning a context manager that yields an
#: object with :class:`PostgresStatisticsDatabase`'s methods, used instead of
#: PostgreSQL. Unset in every deployment; the stage fixture
#: (``make stage-statistics``) sets it, since a subprocess cannot be
#: monkeypatched.
DATABASE_ENV = "RAPIDPIPE_STATISTICS_DATABASE"


@contextlib.contextmanager
def _postgres() -> Iterator[PostgresStatisticsDatabase]:
    with connect() as conn:
        try:
            yield PostgresStatisticsDatabase(conn)
        except BaseException:
            conn.rollback()
            raise


def open_database():
    """PostgreSQL, unless ``RAPIDPIPE_STATISTICS_DATABASE`` names another; tests monkeypatch this."""
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
# Settings and inputs
# ----------------------------------------------------------------------


def _check_settings(settings: dict[str, Any]) -> None:
    table = settings["statistics"]
    if not isinstance(table["done_check"], bool):
        raise UsageError("[statistics] done_check must be true or false")
    membership = table["membership"]
    if membership in DESIGNED_IN_MEMBERSHIPS:
        raise UsageError(
            f"[statistics] membership {membership!r} is designed in and not built: delivered "
            f"statistics describe the association set, and the pruned set as a statistics "
            f"input is left unused (products page, 'Database result sets', lead, 2026-09-22)")
    if membership not in MEMBERSHIPS:
        raise UsageError(
            f"[statistics] membership {membership!r} is not one of {list(MEMBERSHIPS)}")


def _association_entry(manifest: Manifest, field: int) -> OutputEntry:
    entries = [e for e in manifest.outputs if e.kind == "association-set"]
    if len(entries) != 1:
        raise InputRejected(
            f"input manifest has {len(entries)} association-set entries, expected exactly one")
    entry = entries[0]
    key_field = entry.key.get("field")
    if isinstance(key_field, bool) or not (
            (isinstance(key_field, int) and key_field >= 0)
            or (isinstance(key_field, str) and _UNIT_ID_RE.fullmatch(key_field))):
        raise InputRejected(
            f"association set {entry.instance!r}: key field {key_field!r} is not a field")
    if int(key_field) != field:
        raise InputRejected(
            f"association set {entry.instance!r} is for field {key_field!r}, "
            f"not the unit's field {field}")
    return entry


# ----------------------------------------------------------------------
# The body
# ----------------------------------------------------------------------


def _statistics_set_entry(instance: str, key: dict[str, Any], *, table: str, row_count: int,
                          objects_in_set: int | None) -> OutputEntry:
    registration: dict[str, Any] = {"table": table, "row_count": row_count}
    if objects_in_set is not None:
        registration["objects_in_set"] = objects_in_set
    return OutputEntry(kind="statistics-set", format_version="1", instance=instance, key=key,
                       registration=registration)


def _body(context: StageContext) -> StageResult:
    settings = context.settings
    _check_settings(settings)
    log = context.logger
    field = parse_unit_id(context.unit_id)

    association = _association_entry(context.input_manifest, field)
    key = {"membership": association.instance}
    result_sets_read = (association.instance,)
    table = _objects.field_table_names(field)["astroobjectsmeta"]

    try:
        with open_database() as db:
            chain = db.association_chain(association.instance, context.run_id)
            source_sets = db.chain_source_sets(chain)
            if not source_sets:
                # dev: "no source tables" -> exit 7.
                raise InputRejected(
                    f"association set {association.instance!r} and its bases name no source "
                    f"sets: there are no sources to compute statistics from")
            source_tables = [(db.source_set_table(s, context.run_id), s) for s in source_sets]

            if settings["statistics"]["done_check"]:
                existing = db.find_complete_statistics_set(context.run_id, key, context.attempt_id)
                if existing is not None:
                    instance, row_count = existing
                    log.warning("statistics set %s for association set %s is already written "
                                "in this run; reusing it", instance, association.instance)
                    return StageResult(
                        outputs=[_statistics_set_entry(instance, key, table=table,
                                                       row_count=int(row_count or 0),
                                                       objects_in_set=None)],
                        result_sets_read=result_sets_read,
                        execution_notes={"done_check": {"reused": instance}})

            made = db.ensure_astroobjectsmeta_table(field)
            if made:
                log.info("made %s", table)

            records = db.member_rows(field, chain, source_tables)
            log.info("%s records from %s source sets over association chain %s",
                     len(records), len(source_tables), chain)
            grouped = lightcurve.accumulate(records)
            if grouped.repeated_pairs:
                log.warning("%s repeated (aid, sid) pairs across the chain counted once",
                            grouped.repeated_pairs)
            objects_in_set = db.objects_in_set(field, chain)

            instance = new_ulid()
            run_columns = (context.run_id, context.attempt_id, instance)
            context.outputs_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=context.outputs_dir) as work:
                csv_path = Path(work) / f"astroobjectsmeta_{field}_{context.attempt_id}.csv"
                row_count = 0
                with csv_path.open("w") as csv_fh:
                    for aid in grouped.aids():
                        stats = lightcurve.object_statistics(
                            aid, grouped.ras[aid], grouped.decs[aid], grouped.fluxes[aid])
                        csv_fh.write(lightcurve.csv_line(stats, run_columns))
                        row_count += 1
                entry = _statistics_set_entry(instance, key, table=table, row_count=row_count,
                                              objects_in_set=objects_in_set)
                db.register_statistics_set({
                    "run": context.run_id, "stage": "statistics", "attempt": context.attempt_id,
                    "inputs": {"products": {}, "result_sets": list(result_sets_read)},
                    "outputs": [{**entry.to_dict(), "row_count": row_count}],
                }, context.attempt_id)
                db.copy_astroobjectsmeta(field, csv_path)

            loaded = db.count_result_set_rows(table, instance)
            if loaded != row_count:
                raise StageError(
                    f"{table}: {loaded} rows loaded for statistics set {instance}, "
                    f"{row_count} written")
            db.commit()
            log.info("wrote statistics for %s objects (%s in the set) into %s as statistics "
                     "set %s", row_count, objects_in_set, table, instance)
    except ConnectionUnavailable as exc:
        raise TransientFailure(f"could not connect to the database: {exc}") from exc
    except _connection_module.psycopg2.OperationalError as exc:
        raise TransientFailure(f"database connection lost mid-transaction: {exc}") from exc
    except ValueError as exc:
        raise InputRejected(str(exc)) from exc

    notes: dict[str, Any] = {"association_chain": chain, "source_sets": source_sets}
    if grouped.repeated_pairs:
        notes["repeated_pairs"] = grouped.repeated_pairs
    return StageResult(outputs=[entry], result_sets_read=result_sets_read,
                       execution_notes=notes)


def main(argv: list[str]) -> int:
    return run_stage(DECLARATION, _body, argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
