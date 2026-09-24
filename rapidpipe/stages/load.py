"""`load`: a difference image's Photutils catalogs into `sources`, as `dev` loads them.

Ported from `origin/dev`'s ``pipeline/loadPSFCatIntoDBSourcesTable.py``, one
job's worth of it: `dev` loops over a processing date's jobs in parallel;
the rebuild's unit is one detector image, so the loop, the job query, the
S3 transfers and the process pool are the launcher's, not the stage's.
What remains runs in `dev`'s order:

read and inner-join the positive PSF-fit and finder catalogs, then the
negative pair -> HEALPix level 6 and 9 per source -> make the
``sources_<yyyymmdd>_<sca>`` child table if it is new (with `dev`'s indexes
and grants) -> one CSV of `dev`'s 28 columns, positive rows then negative,
fit positions outside the image rejected -> COPY -> optionally CLUSTER and
ANALYZE (`dev` does this once per date; off by default here).

Inputs. ``--inputs`` is the finalize attempt's output location (chain
difference -> finalize -> register -> load, supervisor ruling 2026-09-24), or a difference attempt's: its completion manifest and files; ``finalize``
republishes the same entries under new instance ids. The stage reads the
``difference-image`` entry of the ``[load] differencer`` setting and that instance's two
``photutils`` ``source-catalog`` entries (members ``catalog`` and
``finder``), verifying each member's size and SHA-256. ``pid`` comes from
the `diffimages` row `register` wrote for the instance, and ``expid``,
``sca``, ``fid``, ``mjdobs`` and the child table's date from its `l2files`
row -- never re-derived (products page, "Registration metadata").

Output. One ``source-set`` result set (products page, "Database result
sets"): unit detector image, logical key (difference instance, catalog
type), rows in `sources`, every row carrying the run, this attempt and the
result-set instance. The instance row, its `result_sets` row (``complete``,
``row_count``) and the rows are written in one transaction, so a set is
either complete or absent; an empty set is complete. The stage writes the
rows itself, in the run model's custody, through ``rapidpipe.db`` and
``rapidpipe.runs.repository.register_manifest``.

As `dev` does, a job with any of its four catalog files missing is skipped:
exit 0, no rows, no source set, the reason in the execution record. With
``[load] done_check`` on (`dev`'s default), a complete source set already
loaded for the same logical key in this run is reused and nothing is
written, the rebuild's form of `dev`'s ``source_dbload_jid<jid>.done`` file.

This module may import ``rapidpipe.products``, ``rapidpipe.db``,
``rapidpipe.runs`` and ``rapidpipe.science``; never another stage,
``rapidpipe.launch`` or ``rapidpipe.cli``.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from rapidpipe.db import connection as _connection_module
from rapidpipe.db import sources as _sources
from rapidpipe.db.connection import ConnectionUnavailable
from rapidpipe.db.ids import new_ulid
from rapidpipe.products.diffimage import DIFFERENCERS
from rapidpipe.products.manifest import Manifest, Member, OutputEntry
from rapidpipe.runs.repository import register_manifest
from rapidpipe.science.load import catalogs
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

_SETTINGS_PATH = Path(__file__).resolve().parent.parent / "settings" / "load.toml"

#: `dev` loads the Photutils PSF-fit catalogs only; SExtractor's are not loaded.
CATALOG_TYPE = "photutils"

#: The two signs, in `dev`'s order, with the ``isdiffpos`` text `dev` writes.
SIGNS: tuple[tuple[str, str], ...] = (("positive", "true"), ("negative", "false"))

#: `dev` bounds fit positions by the science image's axes plus one row and
#: one column (L149-152).
EXTRA_ROWS_AND_COLUMNS = 1

DECLARATION = StageDeclaration(
    name="load",
    unit="detector-image",
    argument_schema={
        "description": (
            "rapidpipe stage load --run <run-id> --unit <unit-id> "
            "--attempt <attempt-id> --inputs <dir> --outputs <dir> "
            "[--settings <toml>] [--dry-run]. --inputs holds the difference "
            "attempt's completion manifest and files; its difference-image "
            "instance must already be registered."
        ),
    },
    settings_schema_path=str(_SETTINGS_PATH),
    consumes=("difference-image", "source-catalog"),
    produces=("source-set",),
    database_access="read-write",
    resource_defaults={"vcpus": 1, "memory_mib": 4096},
)


# ----------------------------------------------------------------------
# The database, replaceable in tests
# ----------------------------------------------------------------------


def connect(*args, **kwargs):
    """Module-level indirection to ``rapidpipe.db.connection.connect``, for tests."""
    return _connection_module.connect(*args, **kwargs)


class PostgresLoadDatabase:
    """The stage's database operations on one connection, in one transaction."""

    def __init__(self, conn) -> None:
        self.conn = conn

    def difference_image_row(self, instance: str) -> dict[str, Any]:
        with self.conn.cursor() as cur:
            return _sources.difference_image_row(cur, instance)

    def find_complete_source_set(self, run_id: str, key: dict[str, Any]):
        with self.conn.cursor() as cur:
            return _sources.find_complete_source_set(cur, run_id, key)

    def ensure_child_table(self, obs_date: str, sca: int) -> bool:
        with self.conn.cursor() as cur:
            return _sources.ensure_child_table(cur, obs_date, sca)

    def register_source_set(self, manifest: dict[str, Any], attempt_id: str) -> None:
        register_manifest(self.conn, manifest, registering_attempt_id=attempt_id)

    def copy_sources(self, table: str, csv_path: Path) -> None:
        with self.conn.cursor() as cur, csv_path.open() as fh:
            _sources.copy_sources(cur, table, fh)

    def count_result_set_rows(self, table: str, result_set: str) -> int:
        with self.conn.cursor() as cur:
            return _sources.count_result_set_rows(cur, table, result_set)

    def cluster_and_analyze(self, obs_date: str, sca: int) -> None:
        with self.conn.cursor() as cur:
            _sources.cluster_and_analyze(cur, obs_date, sca)

    def commit(self) -> None:
        self.conn.commit()


#: The stages whose manifest this stage reads: ``finalize``'s in the chain,
#: ``difference``'s directly (the shape is the same).
INPUT_STAGES = ("finalize", "difference")

#: Names a ``module:factory`` returning a context manager that yields an
#: object with :class:`PostgresLoadDatabase`'s methods, used instead of
#: PostgreSQL. Unset in every deployment; the stage fixture
#: (``make stage-load``) sets it, since a subprocess cannot be monkeypatched.
DATABASE_ENV = "RAPIDPIPE_LOAD_DATABASE"


@contextlib.contextmanager
def _postgres() -> Iterator[PostgresLoadDatabase]:
    with connect() as conn:
        try:
            yield PostgresLoadDatabase(conn)
        except BaseException:
            conn.rollback()
            raise


def open_database():
    """PostgreSQL, unless ``RAPIDPIPE_LOAD_DATABASE`` names another; tests monkeypatch this."""
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
    differencer = settings["load"]["differencer"]
    if differencer not in DIFFERENCERS:
        raise UsageError(
            f"[load] differencer {differencer!r} is not a registered differencer; "
            f"known: {sorted(DIFFERENCERS)}")
    for table, key in (("load", "done_check"), ("load", "skip_loading"),
                       ("child_tables", "cluster_and_analyze")):
        if not isinstance(settings[table][key], bool):
            raise UsageError(f"[{table}] {key} must be true or false")
    for key in ("naxis1_sciimage", "naxis2_sciimage"):
        value = settings["instrument"][key]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise UsageError(f"[instrument] {key} must be a positive integer, got {value!r}")
    for key in ("xy_fit_min", "xy_fit_max_offset"):
        value = settings["psf_fit_bounds"][key]
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise UsageError(f"[psf_fit_bounds] {key} must be a number, got {value!r}")


def _sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verified_member_path(inputs_dir: Path, member: Member) -> Path:
    path = inputs_dir / member.path
    if not path.exists():
        raise InputRejected(f"input member file not found: {path}")
    if path.stat().st_size != member.bytes:
        raise InputRejected(
            f"input member {member.path!r}: manifest declares {member.bytes} bytes, "
            f"file is {path.stat().st_size} bytes")
    if _sha256_of_file(path) != member.sha256.removeprefix("sha256:"):
        raise InputRejected(f"input member {member.path!r}: SHA-256 mismatch")
    return path


def _difference_entry(manifest: Manifest, differencer: str) -> OutputEntry:
    entries = [e for e in manifest.outputs if e.kind == "difference-image"
               and e.key.get("differencer") == differencer]
    if len(entries) != 1:
        raise InputRejected(
            f"input manifest has {len(entries)} difference-image entries for differencer "
            f"{differencer!r}, expected exactly one")
    return entries[0]


@dataclass
class _Catalog:
    sign: str
    isdiffpos: str
    entry: OutputEntry | None
    catalog: Member | None
    finder: Member | None

    @property
    def missing(self) -> str | None:
        if self.entry is None:
            return f"no {CATALOG_TYPE} {self.sign} source-catalog entry"
        if self.catalog is None:
            return f"{CATALOG_TYPE} {self.sign} source-catalog has no 'catalog' member"
        if self.finder is None:
            return f"{CATALOG_TYPE} {self.sign} source-catalog has no 'finder' member"
        return None


def _one_role(entry: OutputEntry, role: str) -> Member | None:
    members = [m for m in entry.members if m.role == role]
    if len(members) > 1:
        raise InputRejected(
            f"source-catalog {entry.instance!r} has {len(members)} members with role {role!r}")
    return members[0] if members else None


def _catalogs(manifest: Manifest, difference_instance: str) -> list[_Catalog]:
    found = []
    for sign, isdiffpos in SIGNS:
        entries = [e for e in manifest.outputs if e.kind == "source-catalog"
                   and e.key.get("difference") == difference_instance
                   and e.key.get("catalog_type") == CATALOG_TYPE
                   and e.key.get("sign") == sign]
        if len(entries) > 1:
            raise InputRejected(
                f"input manifest has {len(entries)} {CATALOG_TYPE} {sign} source-catalog "
                f"entries for difference instance {difference_instance!r}")
        entry = entries[0] if entries else None
        found.append(_Catalog(
            sign=sign, isdiffpos=isdiffpos, entry=entry,
            catalog=_one_role(entry, "catalog") if entry else None,
            finder=_one_role(entry, "finder") if entry else None))
    return found


def _products_read(difference: OutputEntry, found: list[_Catalog]) -> dict[str, str]:
    """The instances this attempt read: the difference image and each sign's catalog.

    ``inputs.products`` maps a name to one instance; the two catalogs share
    a kind, so each is named ``source-catalog/<sign>``.
    """
    products = {"difference-image": difference.instance}
    for c in found:
        if c.entry is not None:
            products[f"source-catalog/{c.sign}"] = c.entry.instance
    return products


# ----------------------------------------------------------------------
# The body
# ----------------------------------------------------------------------


def _source_set_entry(instance: str, key: dict[str, Any], *, table: str | None,
                      row_count: int, rows_by_sign: dict[str, int] | None) -> OutputEntry:
    registration: dict[str, Any] = {"row_count": row_count}
    if table is not None:
        registration["table"] = table
    if rows_by_sign is not None:
        registration["rows_by_sign"] = rows_by_sign
    return OutputEntry(kind="source-set", format_version="1", instance=instance, key=key,
                       registration=registration)


def _body(context: StageContext) -> StageResult:
    settings = context.settings
    _check_settings(settings)
    log = context.logger
    load_settings = settings["load"]
    manifest = context.input_manifest

    if manifest.unit.kind != "detector-image":
        raise InputRejected(
            f"input manifest unit kind is {manifest.unit.kind!r}, expected 'detector-image'")
    if manifest.stage not in INPUT_STAGES:
        raise InputRejected(
            f"input manifest is stage {manifest.stage!r}'s, expected one of {INPUT_STAGES}")

    difference = _difference_entry(manifest, load_settings["differencer"])
    found = _catalogs(manifest, difference.instance)
    products_read = _products_read(difference, found)
    key = {"difference": difference.instance, "catalog_type": CATALOG_TYPE}

    missing = [c.missing for c in found if c.missing is not None]
    if missing:
        # dev: "Warning: ... catalog file does not exist; skipping...", no done file.
        for reason in missing:
            log.warning("%s; skipping this difference image, as dev does", reason)
        return StageResult(outputs=[], products_read=products_read,
                           execution_notes={"skipped": missing})

    paths = {c.sign: (_verified_member_path(context.inputs_dir, c.catalog),
                      _verified_member_path(context.inputs_dir, c.finder)) for c in found}

    naxis1 = int(settings["instrument"]["naxis1_sciimage"]) + EXTRA_ROWS_AND_COLUMNS
    naxis2 = int(settings["instrument"]["naxis2_sciimage"]) + EXTRA_ROWS_AND_COLUMNS
    bounds = settings["psf_fit_bounds"]

    joined = {}
    for c in found:
        catalog_path, finder_path = paths[c.sign]
        try:
            table = catalogs.read_joined_catalog(catalog_path, finder_path)
            hp6_arr, hp9_arr = catalogs.healpix_arrays(table)
        except Exception as exc:  # noqa: BLE001 - an unreadable catalog is a corrupt input
            raise InputRejected(f"{c.sign} {CATALOG_TYPE} catalog: {exc}") from exc
        log.info("nrows in %s difference-image PSF-fit catalog = %s", c.sign, len(table))
        joined[c.sign] = (table, hp6_arr, hp9_arr)

    try:
        with open_database() as db:
            row = db.difference_image_row(difference.instance)
            obs_date = _sources.obs_date_of(row["dateobs"])
            table_name = _sources.child_table_name(obs_date, row["sca"])

            if load_settings["skip_loading"]:
                if settings["child_tables"]["cluster_and_analyze"]:
                    db.cluster_and_analyze(obs_date, row["sca"])
                    db.commit()
                return StageResult(outputs=[], products_read=products_read,
                                   execution_notes={"skip_loading": True})

            if load_settings["done_check"]:
                existing = db.find_complete_source_set(context.run_id, key)
                if existing is not None:
                    instance, row_count = existing
                    log.warning("source set %s for this difference instance is already "
                                "loaded in this run; skipping, as dev does on its done file",
                                instance)
                    return StageResult(
                        outputs=[_source_set_entry(instance, key, table=table_name,
                                                   row_count=int(row_count or 0),
                                                   rows_by_sign=None)],
                        products_read=products_read,
                        execution_notes={"done_check": {"reused": instance}})

            instance = new_ulid()
            run_columns = (context.run_id, context.attempt_id, instance)
            rows_by_sign: dict[str, int] = {}
            # dev writes the CSV beside its catalogs and deletes it after the COPY.
            context.outputs_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=context.outputs_dir) as work:
                csv_path = Path(work) / f"sources_{obs_date}_sca{row['sca']}_{context.attempt_id}.csv"
                with csv_path.open("w") as csv_fh:
                    for c in found:
                        table, hp6_arr, hp9_arr = joined[c.sign]
                        rows_by_sign[c.sign] = catalogs.write_joined_table_inner_to_csv_file(
                            c.isdiffpos, row["expid"], row["sca"], row["fid"], row["mjdobs"],
                            row["pid"], csv_fh, table, hp6_arr, hp9_arr,
                            naxis1=naxis1, naxis2=naxis2,
                            xy_fit_min=float(bounds["xy_fit_min"]),
                            xy_fit_max_offset=float(bounds["xy_fit_max_offset"]),
                            run_columns=run_columns, log=log)
                row_count = sum(rows_by_sign.values())
                entry = _source_set_entry(instance, key, table=table_name, row_count=row_count,
                                          rows_by_sign=rows_by_sign)

                made = db.ensure_child_table(obs_date, row["sca"])
                if made:
                    log.info("made sources child table %s", table_name)
                db.register_source_set({
                    "run": context.run_id, "stage": "load", "attempt": context.attempt_id,
                    "inputs": {"products": products_read, "result_sets": []},
                    "outputs": [{**entry.to_dict(), "row_count": row_count}],
                }, context.attempt_id)
                db.copy_sources(table_name, csv_path)

            loaded = db.count_result_set_rows(table_name, instance)
            if loaded != row_count:
                raise StageError(
                    f"{table_name}: {loaded} rows loaded for source set {instance}, "
                    f"{row_count} written")
            if settings["child_tables"]["cluster_and_analyze"]:
                db.cluster_and_analyze(obs_date, row["sca"])
            db.commit()
            log.info("loaded %s rows (%s) into %s as source set %s",
                     row_count, rows_by_sign, table_name, instance)
    except ConnectionUnavailable as exc:
        raise TransientFailure(f"could not connect to the database: {exc}") from exc
    except _connection_module.psycopg2.OperationalError as exc:
        raise TransientFailure(f"database connection lost mid-transaction: {exc}") from exc
    except ValueError as exc:
        raise InputRejected(str(exc)) from exc

    return StageResult(outputs=[entry], products_read=products_read)


def main(argv: list[str]) -> int:
    return run_stage(DECLARATION, _body, argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
