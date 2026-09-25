"""`export`: a field's sources, from named source sets, as one HATS catalog.

Ported from `origin/dev`'s ``pipeline/generateSourceHATSCatalog.py``
(supervisor step 8, 2026-09-24, ruling R12): dump ``sources`` rows to CSV
files (dev's column list, ``[HATS_CATALOGS] sources_cols``, and its SELECT
``SELECT <sources_cols> FROM sources WHERE sid >= .. AND sid <= .. ORDER BY
sid``), then build a HATS (Hierarchical Adaptive Tiling Scheme) catalog from
them with ``hats_import`` -- ``ImportArguments`` (``ra_column``,
``dec_column``, ``lowest_healpix_order``, ``highest_healpix_order``,
``CsvReader()``, ``input_file_list``, ``output_artifact_name``,
``output_path``, ``tmp_dir``, ``resume=False``) run by
``pipeline_with_client`` on a local dask ``Client(n_workers=...)``. The
parameters are ``settings/export.toml``'s, each naming the dev one.

`dev`'s light-curve catalog (``pipeline/generateLightCurveHATSCatalog.py``,
AstroObjects/Merges/Sources joined into one light curve per object) is the
next port: ``[export] catalog_type = "light-curves"`` exits 64.

Departures from dev, each deliberate:

- **Which rows.** dev dumps the whole ``sources`` table. The rebuild reads
  the rows of the source sets the input manifest names, by instance
  (``WHERE result_set = ANY(<named sets>)`` through the parent table, as
  ``alerts`` reads them; products page: "A stage reads ... by id, never
  'whatever is current'"), streamed by a server-side cursor instead of
  dev's ``sid`` range chunks. ``[export] flags_zero_only`` (default false,
  as dev: no filter) restricts to ``flags = 0``.
- **Where it goes.** dev writes ``$RAPID_WORK/<catalog name>`` and runs
  ``aws s3 sync`` to ``product_s3_bucket_base``. The rebuild writes
  ``<outputs>/hats/<catalog name>/`` and publishes it with the manifest
  through ``run_stage``, like every stage's products; there is no sync.
- **The dask cluster.** dev's ``Client(n_workers=n_workers)`` (processes);
  here also ``dashboard_address=None`` (no port bound in a Batch job),
  ``local_directory`` in the attempt's scratch, and ``[hats]
  dask_processes`` (default true, dev's) to run threads instead.
- **Settings dev hard-codes or leaves to hats-import.** ``csv_rows_per_file``
  (dev: 100000), ``pixel_threshold`` (hats-import's default),
  ``sort_columns`` (none), ``hats_catalog_type`` (hats-import's "object"),
  all defaulting to dev's behaviour. ``progress_bar=False`` (no tqdm in a
  log).
- **No rows.** A named set, or all of them together, with no rows to export
  exits 65: hats-import cannot build an empty catalog, and dev never ran on
  an empty table.

Inputs. ``--inputs`` is an input-set manifest (stage ``input-set``, unit
``field``) whose ``inputs.result_sets`` names, by instance id, one or more
``source-set`` instances and optionally ``association-set`` instances. An
association set is checked (registered, complete) and recorded in
``result_sets_read`` and the execution notes, never read: the source
catalog does not use it. Every named set must be registered, complete and
retained, and this run's own or a production run's selected output
(``rapidpipe.db.objects.assert_readable_result_set``, supervisor step 9
ruling R2), else exit 65. The source sets' field is not checked against the
unit (a source set's key names its difference instance, not a field; the
rows carry ``field``).

Output. One ``catalog-export`` file product
(:mod:`rapidpipe.products.catalogexport`): key ``{field, export_type
"sources", selection <SHA-256 digest of the sorted, distinct source_sets>,
settings_hash}`` (ruling R13: the digest, not just the first named source
set, so a differently-ordered but identical selection shares a key and a
different selection does not), members every file of the catalog
directory, primary its root ``properties`` file, registration
``{row_count, export_type, hats_version, source_sets, healpix_order,
partition_count, md5}``. The
stage writes no rows (``database_access = "read"``); `register` records
the instance only.

Exit codes: 64 bad settings, or hats-import not installed; 65 an unknown,
incomplete, deleted or wrongly-kinded set, or nothing to export; 70
hats-import failed or wrote a catalog that does not check out; 75 the
database is unreachable. ``--dry-run`` validates settings and the input
manifest's shape and prints the plan, opening no connection.

This module may import ``rapidpipe.products``, ``rapidpipe.db``,
``rapidpipe.runs`` and ``rapidpipe.science``; never another stage,
``rapidpipe.launch`` or ``rapidpipe.cli``.
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import importlib
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable, Iterator

from rapidpipe.db import connection as _connection_module
from rapidpipe.db import objects as _objects
from rapidpipe.db import sources as _sources_db
from rapidpipe.db.connection import ConnectionUnavailable
from rapidpipe.db.ids import new_ulid
from rapidpipe.products.catalogexport import (
    CATALOG_EXPORT_KIND,
    role_for,
    selection_digest,
    validate_catalog_export_entry,
)
from rapidpipe.products.manifest import Manifest, OutputEntry, member_for_file
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

_SETTINGS_PATH = Path(__file__).resolve().parent.parent / "settings" / "export.toml"

#: The input manifest's stage: a composed input set, not one producer's manifest.
INPUT_SET_STAGE = "input-set"

SOURCE_SET = "source-set"
ASSOCIATION_SET = "association-set"
#: The result-set kinds the input set may name: source sets (read) and
#: association sets (recorded, unused).
RESULT_SET_KINDS = (SOURCE_SET, ASSOCIATION_SET)

CATALOG_TYPES = ("sources",)
#: Designed in, not built: dev's generateLightCurveHATSCatalog.py, the next port.
DESIGNED_IN_CATALOG_TYPES = ("light-curves",)
HATS_CATALOG_TYPES = ("object", "source")

#: hats-import's own ceiling on a healpix order (dev's `highest_healpix_order`).
MAX_HEALPIX_ORDER = 11

DECLARATION = StageDeclaration(
    name="export",
    unit="field",
    argument_schema={
        "description": (
            "rapidpipe stage export --run <run-id> --unit <rtid> "
            "--attempt <attempt-id> --inputs <dir> --outputs <dir> "
            "[--settings <toml>] [--dry-run]. --inputs holds an input-set "
            "manifest (stage input-set) naming, in inputs.result_sets, one "
            "or more source-set instances and optionally association-set "
            "instances (recorded, unused). Writes one catalog-export: a HATS "
            "catalog of the named source sets' sources rows."
        ),
    },
    settings_schema_path=str(_SETTINGS_PATH),
    consumes=RESULT_SET_KINDS,
    produces=(CATALOG_EXPORT_KIND,),
    database_access="read",
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


class PostgresExportDatabase:
    """The stage's database reads on one connection, in one read-only transaction."""

    def __init__(self, conn) -> None:
        self.conn = conn

    def result_set_states(self, instances: list[str], run_id: str) -> dict[str, dict[str, Any]]:
        """Each named set's state; ValueError unless run ``run_id`` may read every one that exists.

        The read rule is ``rapidpipe.db.objects.assert_readable_result_set``
        (supervisor step 9 ruling R2).
        """
        with self.conn.cursor() as cur:
            found = _sources_db.result_set_states(cur, instances)
            for instance in instances:
                if instance in found:
                    _objects.assert_readable_result_set(cur, instance, run_id)
            return found

    def source_rows(self, source_sets: list[str], columns: tuple[str, ...], *,
                    flags_zero_only: bool) -> Iterator[tuple]:
        return _sources_db.iter_set_rows(self.conn, source_sets, columns,
                                         flags_zero_only=flags_zero_only)


#: Names a ``module:factory`` returning a context manager that yields an
#: object with :class:`PostgresExportDatabase`'s methods, used instead of
#: PostgreSQL. Unset in every deployment; the stage fixture sets it.
DATABASE_ENV = "RAPIDPIPE_EXPORT_DATABASE"


@contextlib.contextmanager
def _postgres() -> Iterator[PostgresExportDatabase]:
    with connect() as conn:
        try:
            yield PostgresExportDatabase(conn)
        except BaseException:
            conn.rollback()
            raise


def open_database():
    """PostgreSQL, unless ``RAPIDPIPE_EXPORT_DATABASE`` names another; tests monkeypatch this."""
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


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _nonempty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _check_settings(settings: dict) -> None:
    export = settings.get("export")
    if not isinstance(export, dict):
        raise UsageError("[export] table is required")
    catalog_type = export.get("catalog_type")
    if catalog_type in DESIGNED_IN_CATALOG_TYPES:
        raise UsageError(
            f"[export] catalog_type {catalog_type!r} is designed in and not built: the "
            f"light-curve catalog (dev's generateLightCurveHATSCatalog.py) is the next port")
    if catalog_type not in CATALOG_TYPES:
        raise UsageError(
            f"[export] catalog_type must be one of {list(CATALOG_TYPES)}, got {catalog_type!r}")
    if not isinstance(export.get("flags_zero_only"), bool):
        raise UsageError("[export] flags_zero_only must be true or false")
    if not _is_int(export.get("csv_rows_per_file")) or export["csv_rows_per_file"] <= 0:
        raise UsageError(
            f"[export] csv_rows_per_file must be a positive integer, "
            f"got {export.get('csv_rows_per_file')!r}")

    hats = settings.get("hats")
    if not isinstance(hats, dict):
        raise UsageError("[hats] table is required")
    for key in ("format_version", "catalog_name", "ra_col", "dec_col"):
        if not _nonempty_str(hats.get(key)):
            raise UsageError(f"[hats] {key} must be a non-empty string, got {hats.get(key)!r}")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", hats["catalog_name"]) or hats["catalog_name"] in (
            ".", ".."):
        raise UsageError(
            f"[hats] catalog_name must be a plain directory name, got {hats['catalog_name']!r}")
    if hats.get("hats_catalog_type") not in HATS_CATALOG_TYPES:
        raise UsageError(
            f"[hats] hats_catalog_type must be one of {list(HATS_CATALOG_TYPES)}, "
            f"got {hats.get('hats_catalog_type')!r}")
    lowest, highest = hats.get("lowest_healpix_order"), hats.get("highest_healpix_order")
    for name, value in (("lowest_healpix_order", lowest), ("highest_healpix_order", highest)):
        if not _is_int(value) or not 0 <= value <= MAX_HEALPIX_ORDER:
            raise UsageError(
                f"[hats] {name} must be an integer in [0, {MAX_HEALPIX_ORDER}], got {value!r}")
    if lowest > highest:
        raise UsageError(
            f"[hats] lowest_healpix_order ({lowest}) must not exceed "
            f"highest_healpix_order ({highest})")
    for key in ("pixel_threshold", "n_workers"):
        if not _is_int(hats.get(key)) or hats[key] <= 0:
            raise UsageError(f"[hats] {key} must be a positive integer, got {hats.get(key)!r}")
    if not isinstance(hats.get("dask_processes"), bool):
        raise UsageError("[hats] dask_processes must be true or false")

    columns = hats.get("columns")
    if not isinstance(columns, list) or not columns or not all(_nonempty_str(c) for c in columns):
        raise UsageError(f"[hats] columns must be a non-empty list of names, got {columns!r}")
    if len(set(columns)) != len(columns):
        raise UsageError("[hats] columns names a column twice")
    unknown = [c for c in columns if c not in _sources_db.READABLE_COLUMNS]
    if unknown:
        raise UsageError(
            f"[hats] columns {unknown} are not sources columns; known: "
            f"{list(_sources_db.READABLE_COLUMNS)}")
    for key in ("ra_col", "dec_col"):
        if hats[key] not in columns:
            raise UsageError(f"[hats] {key} {hats[key]!r} is not one of the dumped columns")
    sort_columns = hats.get("sort_columns")
    if not isinstance(sort_columns, list) or not all(c in columns for c in sort_columns):
        raise UsageError(
            f"[hats] sort_columns must be a list of dumped columns, got {sort_columns!r}")


def _named_result_sets(manifest: Manifest) -> tuple[str, ...]:
    if manifest.unit.kind != "field":
        raise InputRejected(
            f"input manifest unit kind is {manifest.unit.kind!r}, expected 'field'")
    if manifest.stage != INPUT_SET_STAGE:
        raise InputRejected(
            f"input manifest is stage {manifest.stage!r}'s, expected {INPUT_SET_STAGE!r}")
    named = manifest.inputs.result_sets
    if not named:
        raise InputRejected(
            "input manifest's inputs.result_sets names no result sets, expected at least "
            "one source-set")
    if len(set(named)) != len(named):
        raise InputRejected("input manifest's inputs.result_sets names a result set twice")
    for instance in named:
        if not instance:
            raise InputRejected("input manifest's inputs.result_sets names an empty instance id")
    return tuple(named)


def classify_result_sets(named: tuple[str, ...],
                         found: dict[str, dict[str, Any]]) -> tuple[list[str], list[str]]:
    """``(source sets, association sets)`` among ``named``, in the order named.

    Every named set must be registered, complete and retained, and of a
    kind in :data:`RESULT_SET_KINDS`; at least one must be a source set.
    """
    by_kind: dict[str, list[str]] = {kind: [] for kind in RESULT_SET_KINDS}
    for instance in named:
        row = found.get(instance)
        if row is None:
            raise InputRejected(f"result set {instance!r} is not registered")
        if row["kind"] not in by_kind:
            raise InputRejected(
                f"result set {instance!r} is of kind {row['kind']!r}; export reads only "
                f"{list(RESULT_SET_KINDS)}")
        if row["complete"] is not True:
            raise InputRejected(f"{row['kind']} {instance!r} is not a complete result set")
        if row.get("deletion_state", "retained") != "retained":
            raise InputRejected(f"{row['kind']} {instance!r} is {row['deletion_state']}")
        by_kind[row["kind"]].append(instance)
    if not by_kind[SOURCE_SET]:
        raise InputRejected("the input set names no source-set: there are no sources to export")
    return by_kind[SOURCE_SET], by_kind[ASSOCIATION_SET]


# ----------------------------------------------------------------------
# The dump and the import
# ----------------------------------------------------------------------


def write_csv_files(rows: Iterable[tuple], columns: list[str], work: Path,
                    rows_per_file: int) -> tuple[list[Path], int]:
    """dev's dump: ``sources_<n>.csv`` files (dev's ``sources_*.csv`` glob), a header each.

    Returns the files written and the row count. No file is written for no rows.
    """
    paths: list[Path] = []
    total = 0
    fh = writer = None
    try:
        for row in rows:
            if total % rows_per_file == 0:
                if fh is not None:
                    fh.close()
                path = work / f"sources_{len(paths) + 1}.csv"
                paths.append(path)
                fh = path.open("w", newline="")
                writer = csv.writer(fh)
                writer.writerow(columns)
            writer.writerow(row)
            total += 1
    finally:
        if fh is not None:
            fh.close()
    return paths, total


def build_hats_catalog(csv_paths: list[Path], hats: dict[str, Any], output_path: Path,
                       tmp_dir: Path) -> str:
    """Run hats-import over ``csv_paths`` as dev configures it; return the hats version.

    Writes ``<output_path>/<catalog_name>/``. Module level so a test can
    replace it where hats-import is not installed.
    """
    try:
        import hats as _hats
        from dask.distributed import Client
        from hats_import.catalog.arguments import ImportArguments
        from hats_import.catalog.file_readers import CsvReader
        from hats_import.pipeline import pipeline_with_client
    except ImportError as exc:
        raise UsageError(
            f"hats-import is not installed in this environment ({exc}); the export stage "
            f"needs hats, hats-import and dask") from exc

    kwargs: dict[str, Any] = {}
    if hats["sort_columns"]:
        kwargs["sort_columns"] = ",".join(hats["sort_columns"])
    try:
        args = ImportArguments(
            ra_column=hats["ra_col"],
            dec_column=hats["dec_col"],
            lowest_healpix_order=hats["lowest_healpix_order"],
            highest_healpix_order=hats["highest_healpix_order"],
            pixel_threshold=hats["pixel_threshold"],
            catalog_type=hats["hats_catalog_type"],
            file_reader=CsvReader(),
            input_file_list=[str(p) for p in csv_paths],
            output_artifact_name=hats["catalog_name"],
            output_path=str(output_path),
            tmp_dir=str(tmp_dir),
            resume=False,
            progress_bar=False,
            **kwargs,
        )
        with Client(n_workers=hats["n_workers"], processes=hats["dask_processes"],
                    threads_per_worker=1, dashboard_address=None,
                    local_directory=str(tmp_dir / "dask")) as client:
            pipeline_with_client(args, client)
    except UsageError:
        raise
    except Exception as exc:  # noqa: BLE001 -- any hats-import failure is the tool's
        raise StageError(f"hats-import failed: {type(exc).__name__}: {exc}") from exc
    return str(_hats.__version__)


def _properties(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            name, _, value = line.partition("=")
            values[name.strip()] = value.strip()
    return values


_NORDER_RE = re.compile(r"Norder=([0-9]+)")


def catalog_entry(catalog_dir: Path, outputs_dir: Path, *, key: dict[str, Any],
                  format_version: str, row_count: int, hats_version: str,
                  source_sets: list[str]) -> OutputEntry:
    """The ``catalog-export`` entry for a written catalog directory, checked."""
    files = sorted(p for p in catalog_dir.rglob("*") if p.is_file())
    members = tuple(member_for_file(role_for(p.relative_to(catalog_dir).as_posix()), p,
                                    relative_to=outputs_dir) for p in files)
    primary_file = next((catalog_dir / n for n in ("properties", "hats.properties")
                         if (catalog_dir / n).is_file()), None)
    if primary_file is None:
        raise StageError(f"hats-import wrote no properties file in {catalog_dir}")
    properties = _properties(primary_file)
    written = properties.get("hats_nrows")
    if written is not None and int(written) != row_count:
        raise StageError(
            f"hats catalog properties say {written} rows; {row_count} were dumped")
    partitions = [m for m in members if m.role == "partition"]
    orders = [int(m) for m in _NORDER_RE.findall(" ".join(m.path for m in partitions))]
    md5 = hashlib.md5(primary_file.read_bytes()).hexdigest()
    entry = OutputEntry(
        kind=CATALOG_EXPORT_KIND, format_version=format_version, instance=new_ulid(), key=key,
        members=members, primary=primary_file.relative_to(outputs_dir).as_posix(),
        registration={
            "row_count": row_count, "export_type": key["export_type"],
            "hats_version": hats_version, "source_sets": list(source_sets),
            "healpix_order": max(orders) if orders else 0,
            "partition_count": len(partitions), "md5": md5,
        })
    try:
        validate_catalog_export_entry(entry.to_dict())
    except ValueError as exc:
        raise StageError(f"the catalog-export entry does not check out: {exc}") from exc
    return entry


# ----------------------------------------------------------------------
# The body
# ----------------------------------------------------------------------


def _validate_inputs(context: StageContext) -> None:
    """``run_stage``'s pre-dry-run hook: settings, the unit and the input set's shape,
    then (under ``--dry-run``) the plan."""
    _check_settings(context.settings)
    field = parse_unit_id(context.unit_id)
    named = _named_result_sets(context.input_manifest)
    if context.dry_run:
        hats = context.settings["hats"]
        context.logger.info(
            "export plan: field %s; read sources rows of the named source sets among %s "
            "(flags_zero_only=%s); dump %d columns to CSV; hats-import (orders %d-%d, "
            "pixel_threshold %d, %d dask workers) -> %s/hats/%s/; one catalog-export",
            field, list(named), context.settings["export"]["flags_zero_only"],
            len(hats["columns"]), hats["lowest_healpix_order"], hats["highest_healpix_order"],
            hats["pixel_threshold"], hats["n_workers"], context.outputs_location,
            hats["catalog_name"])


def _body(context: StageContext) -> StageResult:
    settings = context.settings
    _check_settings(settings)
    log = context.logger
    field = parse_unit_id(context.unit_id)
    named = _named_result_sets(context.input_manifest)
    export, hats = settings["export"], settings["hats"]
    columns = list(hats["columns"])

    context.outputs_dir.mkdir(parents=True, exist_ok=True)
    catalog_root = context.outputs_dir / "hats"
    catalog_dir = catalog_root / hats["catalog_name"]
    if catalog_dir.exists():
        raise StageError(f"{catalog_dir} already exists: outputs must be the attempt's own")

    with tempfile.TemporaryDirectory(dir=context.outputs_dir, prefix=".export-") as work_name:
        work = Path(work_name)
        try:
            with open_database() as db:
                found = db.result_set_states(list(named), context.run_id)
                source_sets, association_sets = classify_result_sets(named, found)
                rows = db.source_rows(source_sets, tuple(columns),
                                      flags_zero_only=export["flags_zero_only"])
                csv_paths, row_count = write_csv_files(rows, columns, work,
                                                       export["csv_rows_per_file"])
        except ConnectionUnavailable as exc:
            raise TransientFailure(f"could not connect to the database: {exc}") from exc
        except _connection_module.psycopg2.OperationalError as exc:
            raise TransientFailure(f"database connection lost mid-read: {exc}") from exc
        except ValueError as exc:
            raise InputRejected(str(exc)) from exc
        log.info("dumped %d sources rows of source sets %s into %d CSV files",
                 row_count, source_sets, len(csv_paths))
        if row_count == 0:
            raise InputRejected(
                f"source sets {source_sets} have no rows to export"
                + (" with flags = 0" if export["flags_zero_only"] else ""))

        catalog_root.mkdir(parents=True, exist_ok=True)
        hats_version = build_hats_catalog(csv_paths, hats, catalog_root, work / "hats-tmp")

    if not catalog_dir.is_dir():
        raise StageError(f"hats-import wrote no catalog directory {catalog_dir}")
    key = {"field": field, "export_type": export["catalog_type"],
           "selection": selection_digest(source_sets),
           "settings_hash": context.settings_hash}
    entry = catalog_entry(catalog_dir, context.outputs_dir, key=key,
                          format_version=hats["format_version"], row_count=row_count,
                          hats_version=hats_version, source_sets=source_sets)
    log.info("wrote HATS catalog %s: %d rows, %d partitions, hats %s", catalog_dir, row_count,
             entry.registration["partition_count"], hats_version)
    return StageResult(
        outputs=[entry], result_sets_read=tuple(named),
        execution_notes={"source_sets": source_sets, "association_sets": association_sets,
                         "csv_files": len(csv_paths)})


def main(argv: list[str]) -> int:
    return run_stage(DECLARATION, _body, argv, validate_inputs=_validate_inputs)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
