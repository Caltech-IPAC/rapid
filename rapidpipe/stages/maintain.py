"""`maintain`: CLUSTER and ANALYZE one `sources` child table, once per date and detector.

`dev` runs its CLUSTER and ANALYZE once per processing date, after every
image for that date has loaded; running it per image would recluster the
table on every load (`rapid_docs` ``system/load.md``, "The child
tables"). The rebuild keeps that timing but moves it out of ``load`` into
this stage, scheduled after the date's last ``load`` unit and before
``crossmatch`` (supervisor step 1, ruling R1, 2026-09-24). It calls
``cluster_sources_child_table`` through ``rapidpipe.db.sources.cluster_and_analyze``,
the same SQL function ``load`` itself may call inline (off by default
there).

Unit: ``detector-date``, id ``<yyyymmdd>/SCA<nn>`` (ruling R2) -- the
observation date and detector a run's ``load`` units for that date and
detector share, and the child table's own name is built from. No other
declared unit kind fits: ``detector-image`` is one image/attempt,
``processing-date`` carries no detector, ``field`` is a tessellation
tile, ``exposure`` an admitted image.

Inputs. ``--inputs`` is a manifest carrying one or more ``source-set``
output entries: either a ``load`` completion manifest (one entry), or a
stage input-set manifest composed from several ``load`` attempts for the
same date and detector. Each entry's ``registration.table`` must equal
the unit's child table (``sources_<yyyymmdd>_<sca>``); a mismatch is a
rejected input, as is an entry naming a table that does not exist. The
stage does not require the input manifest's own ``stage`` or
``unit.kind`` to be any particular value -- ``load``'s completion
manifest carries unit kind ``detector-image``, an input-set manifest may
carry this stage's own ``detector-date`` -- only that every entry it
names is a ``source-set`` for the unit's table.

Outputs. None: ``maintain`` writes no result set of its own, only
clusters and analyzes rows other stages wrote. ``result_sets_read``
names every source-set instance the input manifest listed, so the run
records which loads this maintenance pass covered.

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
from typing import Any, Iterator

from rapidpipe.db import connection as _connection_module
from rapidpipe.db import sources as _sources
from rapidpipe.db.connection import ConnectionUnavailable
from rapidpipe.stages.contract import (
    InputRejected,
    StageContext,
    StageDeclaration,
    StageResult,
    TransientFailure,
    UsageError,
    run_stage,
)

DECLARATION = StageDeclaration(
    name="maintain",
    unit="detector-date",
    argument_schema={
        "description": (
            "rapidpipe stage maintain --run <run-id> --unit <yyyymmdd>/SCA<nn> "
            "--attempt <attempt-id> --inputs <dir> --outputs <dir> [--dry-run]. "
            "--inputs holds a manifest of one or more source-set entries for "
            "the unit's sources child table; no --settings overlay is accepted."
        ),
    },
    settings_schema_path=None,
    consumes=("source-set",),
    produces=(),
    database_access="read-write",
    resource_defaults={"vcpus": 1, "memory_mib": 4096},
)

#: The unit id's shape (ruling R2): the observation date and two-digit SCA
#: a run's `load` units for that date and detector share.
_UNIT_ID_RE = re.compile(r"^(?P<obs_date>[0-9]{8})/SCA(?P<sca>[0-9]{2})$")


def _parse_unit_id(unit_id: str) -> tuple[str, int]:
    match = _UNIT_ID_RE.fullmatch(unit_id)
    if match is None:
        raise UsageError(
            f"unit id {unit_id!r} is not <yyyymmdd>/SCA<nn>, e.g. '20260821/SCA01'")
    return match.group("obs_date"), int(match.group("sca"))


# ----------------------------------------------------------------------
# The database, replaceable in tests
# ----------------------------------------------------------------------


def connect(*args, **kwargs):
    """Module-level indirection to ``rapidpipe.db.connection.connect``, for tests."""
    return _connection_module.connect(*args, **kwargs)


class PostgresMaintainDatabase:
    """The stage's database operations on one connection, in one transaction."""

    def __init__(self, conn) -> None:
        self.conn = conn

    def child_table_exists(self, table: str) -> bool:
        with self.conn.cursor() as cur:
            return _sources.child_table_exists(cur, table)

    def cluster_and_analyze(self, obs_date: str, sca: int) -> None:
        with self.conn.cursor() as cur:
            _sources.cluster_and_analyze(cur, obs_date, sca)

    def commit(self) -> None:
        self.conn.commit()


#: Names a ``module:factory`` returning a context manager that yields an
#: object with :class:`PostgresMaintainDatabase`'s methods, used instead of
#: PostgreSQL. Unset in every deployment; the stage fixture
#: (``make stage-maintain``) sets it, since a subprocess cannot be
#: monkeypatched.
DATABASE_ENV = "RAPIDPIPE_MAINTAIN_DATABASE"


@contextlib.contextmanager
def _postgres() -> Iterator[PostgresMaintainDatabase]:
    with connect() as conn:
        try:
            yield PostgresMaintainDatabase(conn)
        except BaseException:
            conn.rollback()
            raise


def open_database():
    """PostgreSQL, unless ``RAPIDPIPE_MAINTAIN_DATABASE`` names another; tests monkeypatch this."""
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
# The body
# ----------------------------------------------------------------------


def _source_set_entries(manifest: Any) -> list[Any]:
    entries = [e for e in manifest.outputs if e.kind == "source-set"]
    if not entries:
        raise InputRejected("input manifest has no source-set entries")
    return entries


def _checked_table(entries: list[Any], table: str) -> None:
    for entry in entries:
        entry_table = entry.registration.get("table")
        if entry_table != table:
            raise InputRejected(
                f"source-set {entry.instance!r} registration table {entry_table!r} "
                f"does not match unit table {table!r}")


def _body(context: StageContext) -> StageResult:
    log = context.logger
    obs_date, sca = _parse_unit_id(context.unit_id)
    table = _sources.child_table_name(obs_date, sca)

    entries = _source_set_entries(context.input_manifest)
    _checked_table(entries, table)

    try:
        with open_database() as db:
            if not db.child_table_exists(table):
                raise InputRejected(f"sources child table {table!r} does not exist")
            db.cluster_and_analyze(obs_date, sca)
            db.commit()
    except ConnectionUnavailable as exc:
        raise TransientFailure(f"could not connect to the database: {exc}") from exc
    except _connection_module.psycopg2.OperationalError as exc:
        raise TransientFailure(f"database connection lost mid-transaction: {exc}") from exc

    log.info("clustered and analyzed %s (%s source-set entries)", table, len(entries))
    return StageResult(
        outputs=[],
        result_sets_read=tuple(entry.instance for entry in entries),
        execution_notes={"table": table, "clustered": True},
    )


def main(argv: list[str]) -> int:
    return run_stage(DECLARATION, _body, argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
