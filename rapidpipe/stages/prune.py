"""`prune`: a `pruned-set` result set, its base association set minus not-best pairs.

Ported from `dev`'s ``pipeline/pruneNotBestMerges.py``, one field's worth of
it: `dev` loops over every field a processing date touched, in parallel,
re-deriving the field list itself; the rebuild's unit is one field, and the
launcher's fan-out replaces `dev`'s field loop and process pool. What
remains is `dev`'s per-field exclusion, ported into the run model (step 1
ruling R6, 2026-09-24):

read the crossmatch completion manifest's one ``association-set`` entry for
this field -> follow its base chain (``base plus delta``, ruling R3) and
every chain member's named source sets -> build the not-best `sid` set over
those source sets, `dev`'s ``vbest = 0`` rule widened by the run model's own
clause (a difference image is "best" when it is promoted current, or when
it was made by this run) -> select the chain's `merges_<field>` pairs whose
`sid` is not-best -> record them as a new `pruned-set`, the base association
set minus those pairs; the base itself is never mutated (products page, "A
pruned set is its base association set minus an explicit list of excluded
pairs").

`dev` deletes the excluded rows from `merges_<field>` in place, then drops
an emptied table and VACUUMs the rest, in two further parallel passes. None
of that is ported: a pruned set records exclusions rather than mutating its
base, and a stage never drops a table (ruling R6). `dev`'s
``pruneNotBestSources`` (a global `sources` flag deleted by a separate
script) is not ported either -- it would delete rows another run's science
still depends on, breaking "science rows only by their own run".

Inputs. ``--inputs`` is a crossmatch completion manifest carrying exactly
one ``association-set`` output entry whose key names this field (products
page, "Database result sets"; step 1 ruling R2). ``result_sets_read`` names
that instance.

Output. One ``pruned-set`` result set (products page): unit field, logical
key (the base association-set instance, the settings hash). Rows land in
`prunedmerges`, one row per excluded (aid, sid) pair, through
``rapidpipe.db.objects``. The pruned set's own `result_sets` row must exist
before its `prunedmerges` rows are inserted, since those rows carry foreign
keys to it (as `load` registers before it COPYs); registration and the
insert commit in the same transaction, so a set is complete or absent, and
an empty exclusion list is still a complete set.

With ``[prune] done_check`` on (ruling R14, the default), a complete pruned
set already written for the same base and settings hash in this run is
reused and nothing is written, the rebuild's form of `dev` having no done
file for this stage at all.

This module may import ``rapidpipe.products``, ``rapidpipe.db``,
``rapidpipe.runs`` and ``rapidpipe.science``; never another stage,
``rapidpipe.launch`` or ``rapidpipe.cli``.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterator

from rapidpipe.db import connection as _connection_module
from rapidpipe.db import objects as _objects
from rapidpipe.db.connection import ConnectionUnavailable
from rapidpipe.db.ids import new_ulid
from rapidpipe.products.manifest import Manifest, OutputEntry
from rapidpipe.runs.repository import register_manifest
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

_SETTINGS_PATH = Path(__file__).resolve().parent.parent / "settings" / "prune.toml"

#: `dev`'s only pruning rule, ported (ruling R6).
KNOWN_RULES = ("not-best",)

#: The unit id's shape (ruling R2): the Roman tessellation tile (rtid) as a
#: non-negative decimal string, `dev`'s "field".
_UNIT_ID_RE = re.compile(r"^[0-9]+$")

DECLARATION = StageDeclaration(
    name="prune",
    unit="field",
    argument_schema={
        "description": (
            "rapidpipe stage prune --run <run-id> --unit <field-rtid> "
            "--attempt <attempt-id> --inputs <dir> --outputs <dir> "
            "[--settings <toml>] [--dry-run]. --inputs holds crossmatch's "
            "completion manifest, with exactly one association-set entry "
            "naming this field."
        ),
    },
    settings_schema_path=str(_SETTINGS_PATH),
    consumes=("association-set",),
    produces=("pruned-set",),
    database_access="read-write",
    resource_defaults={"vcpus": 1, "memory_mib": 4096},
)


# ----------------------------------------------------------------------
# The database, replaceable in tests
# ----------------------------------------------------------------------


def connect(*args, **kwargs):
    """Module-level indirection to ``rapidpipe.db.connection.connect``, for tests."""
    return _connection_module.connect(*args, **kwargs)


def _chain_source_sets(cur, chain: list[str]) -> list[str]:
    """The deduplicated `source-set` instances every chain member's logical key names.

    Reads ``product_instances.logical_key->>'source_sets'`` for each chain
    member (the crossmatch association-set key's own field, R10) rather
    than trusting a driver-level JSONB decode, and parses it as JSON.
    """
    if not chain:
        return []
    cur.execute(
        "SELECT logical_key ->> 'source_sets' FROM product_instances WHERE id = ANY(%s)",
        (list(chain),))
    seen: dict[str, None] = {}
    for (raw,) in cur.fetchall():
        if not raw:
            continue
        for instance in json.loads(raw):
            seen.setdefault(instance, None)
    return list(seen)


def _build_notbest_sids(cur, tables_and_sets: list[tuple[str, str]], run_id: str) -> None:
    """`dev`'s not-best `sid` set (pruneNotBestMerges.py), the run model's own-run clause added.

    One temporary table for the whole attempt, built over every source set
    named by the association chain (ruling R6): a `sid` is not-best when its
    difference image is neither promoted current (``vbest > 0``) nor made by
    this run. ``UNION``, not ``UNION ALL``, as `dev`'s own comment explains:
    a `sid` occurring in more than one source-set table would otherwise
    violate the temp table's primary key.

    ``ON COMMIT DROP`` only clears the table on a real commit; a caller
    that shares one connection and transaction across several attempts
    without ever committing (as ``tests/db``'s rolled-back-transaction
    fixture does) would otherwise see "relation already exists" on a
    second attempt, so a prior table of this name is dropped first, as
    ``rapidpipe.db.objects``'s own temp-table copy helper does.
    """
    cur.execute("DROP TABLE IF EXISTS pg_temp.notbest_sids")
    cur.execute("CREATE TEMP TABLE notbest_sids (sid bigint PRIMARY KEY) ON COMMIT DROP")
    if tables_and_sets:
        selects = []
        params: list[Any] = []
        for table, source_set in tables_and_sets:
            selects.append(
                f"SELECT a.sid FROM {table} AS a JOIN diffimages AS d ON a.pid = d.pid "
                "WHERE a.result_set = %s AND d.vbest = 0 AND d.run IS DISTINCT FROM %s")
            params.extend([source_set, run_id])
        cur.execute("INSERT INTO notbest_sids (sid) " + " UNION ".join(selects), params)
    cur.execute("ANALYZE notbest_sids")


class PostgresPruneDatabase:
    """The stage's database operations on one connection, in one transaction."""

    def __init__(self, conn) -> None:
        self.conn = conn

    def find_complete_pruned_set(self, run_id: str, key: dict[str, Any]):
        with self.conn.cursor() as cur:
            return _objects.find_complete_result_set(cur, "pruned-set", run_id, key)

    def association_chain(self, instance: str) -> list[str]:
        with self.conn.cursor() as cur:
            return _objects.association_chain(cur, instance)

    def chain_source_sets(self, chain: list[str]) -> list[str]:
        with self.conn.cursor() as cur:
            return _chain_source_sets(cur, chain)

    def source_set_table(self, instance: str) -> tuple[str, int | None]:
        with self.conn.cursor() as cur:
            return _objects.source_set_table(cur, instance)

    def not_best_pairs(self, field: int, chain: list[str],
                       tables_and_sets: list[tuple[str, str]], run_id: str) -> list[tuple[int, int]]:
        with self.conn.cursor() as cur:
            _build_notbest_sids(cur, tables_and_sets, run_id)
            table = _objects.field_table_names(field)["merges"]
            clause, params = _objects.set_rows_clause(table, chain)
            cur.execute(
                f"SELECT aid, sid FROM {table} WHERE {clause} "
                "AND sid IN (SELECT sid FROM notbest_sids)", params)
            return [(int(aid), int(sid)) for aid, sid in cur.fetchall()]

    def base_row_count(self, field: int, chain: list[str]) -> int:
        with self.conn.cursor() as cur:
            table = _objects.field_table_names(field)["merges"]
            clause, params = _objects.set_rows_clause(table, chain)
            cur.execute(f"SELECT count(*) FROM {table} WHERE {clause}", params)
            return int(cur.fetchone()[0])

    def register_pruned_set(self, manifest: dict[str, Any], attempt_id: str) -> None:
        register_manifest(self.conn, manifest, registering_attempt_id=attempt_id)

    def insert_pruned_merges(self, rows: list[tuple[int, int]], result_set: str, base_set: str,
                             run: str, attempt: str) -> int:
        with self.conn.cursor() as cur:
            return _objects.insert_pruned_merges(cur, rows, result_set, base_set, run, attempt)

    def commit(self) -> None:
        self.conn.commit()


#: Names a ``module:factory`` returning a context manager that yields an
#: object with :class:`PostgresPruneDatabase`'s methods, used instead of
#: PostgreSQL. Unset in every deployment; the stage fixture
#: (``make stage-prune``) sets it, since a subprocess cannot be
#: monkeypatched.
DATABASE_ENV = "RAPIDPIPE_PRUNE_DATABASE"


@contextlib.contextmanager
def _postgres() -> Iterator[PostgresPruneDatabase]:
    with connect() as conn:
        try:
            yield PostgresPruneDatabase(conn)
        except BaseException:
            conn.rollback()
            raise


def open_database():
    """PostgreSQL, unless ``RAPIDPIPE_PRUNE_DATABASE`` names another; tests monkeypatch this."""
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
    prune_settings = settings["prune"]
    rule = prune_settings["rule"]
    if rule not in KNOWN_RULES:
        raise UsageError(
            f"[prune] rule {rule!r} is not a known pruning rule; expected one of {KNOWN_RULES}")
    if not isinstance(prune_settings["done_check"], bool):
        raise UsageError("[prune] done_check must be true or false")


def _parse_unit(unit_id: str) -> int:
    if not _UNIT_ID_RE.fullmatch(unit_id or ""):
        raise UsageError(
            f"unit id {unit_id!r} is not a field (Roman tessellation rtid): "
            "expected a non-negative integer")
    return int(unit_id)


def _association_entry(manifest: Manifest, field: int) -> OutputEntry:
    entries = [e for e in manifest.outputs if e.kind == "association-set"
               and e.key.get("field") == field]
    if len(entries) != 1:
        raise InputRejected(
            f"input manifest has {len(entries)} association-set entries for field {field}, "
            "expected exactly one")
    return entries[0]


# ----------------------------------------------------------------------
# The body
# ----------------------------------------------------------------------


def _pruned_set_entry(instance: str, key: dict[str, Any], *, row_count: int,
                      base_row_count: int | None) -> OutputEntry:
    registration: dict[str, Any] = {
        "table": "prunedmerges", "row_count": row_count, "rule": "not-best",
    }
    if base_row_count is not None:
        registration["base_row_count"] = base_row_count
    return OutputEntry(kind="pruned-set", format_version="1", instance=instance, key=key,
                       registration=registration)


def _body(context: StageContext) -> StageResult:
    settings = context.settings
    _check_settings(settings)
    log = context.logger
    prune_settings = settings["prune"]
    manifest = context.input_manifest

    field = _parse_unit(context.unit_id)
    assoc = _association_entry(manifest, field)
    key = {"base": assoc.instance, "settings_hash": context.settings_hash}

    try:
        with open_database() as db:
            if prune_settings["done_check"]:
                existing = db.find_complete_pruned_set(context.run_id, key)
                if existing is not None:
                    instance, row_count = existing
                    log.warning(
                        "pruned set %s for base %s is already complete in this run; reusing, "
                        "as dev has no done file for this stage but the run model gives one",
                        instance, assoc.instance)
                    return StageResult(
                        outputs=[_pruned_set_entry(instance, key, row_count=int(row_count or 0),
                                                   base_row_count=None)],
                        result_sets_read=(assoc.instance,),
                        execution_notes={"done_check": {"reused": instance}})

            chain = db.association_chain(assoc.instance)
            source_sets = db.chain_source_sets(chain)
            tables_and_sets: list[tuple[str, str]] = []
            for source_set in source_sets:
                table, _ = db.source_set_table(source_set)
                tables_and_sets.append((table, source_set))

            pairs = sorted(set(db.not_best_pairs(field, chain, tables_and_sets, context.run_id)))
            base_row_count = db.base_row_count(field, chain)
            row_count = len(pairs)

            instance = new_ulid()
            entry = _pruned_set_entry(instance, key, row_count=row_count,
                                      base_row_count=base_row_count)

            # Register before inserting: prunedmerges' rows carry foreign
            # keys to this pruned set's own result_sets row, as load
            # registers before it COPYs (LEDGER-B-schema.md).
            db.register_pruned_set({
                "run": context.run_id, "stage": "prune", "attempt": context.attempt_id,
                "inputs": {"products": {}, "result_sets": [assoc.instance]},
                "outputs": [{**entry.to_dict(), "row_count": row_count}],
            }, context.attempt_id)

            inserted = db.insert_pruned_merges(
                pairs, instance, assoc.instance, context.run_id, context.attempt_id)
            if inserted != row_count:
                raise StageError(
                    f"prunedmerges: {inserted} rows inserted for pruned set {instance}, "
                    f"{row_count} written")

            db.commit()
            log.info(
                "pruned field %s: %s of %s base rows excluded, as pruned set %s (base %s)",
                field, row_count, base_row_count, instance, assoc.instance)
    except ConnectionUnavailable as exc:
        raise TransientFailure(f"could not connect to the database: {exc}") from exc
    except _connection_module.psycopg2.OperationalError as exc:
        raise TransientFailure(f"database connection lost mid-transaction: {exc}") from exc
    except ValueError as exc:
        raise InputRejected(str(exc)) from exc

    return StageResult(outputs=[entry], result_sets_read=(assoc.instance,))


def main(argv: list[str]) -> int:
    return run_stage(DECLARATION, _body, argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
