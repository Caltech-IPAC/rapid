"""Tests for rapidpipe.stages.export: the real HATS source-catalog export (ruling R12).

Settings, input-set and result-set validation map to 64/65 as the stage
contract says; the CSV dump carries dev's columns in dev's order; the
manifest entry, its key and registration block pass
:mod:`rapidpipe.products.catalogexport`; ``--dry-run`` validates and
prints the plan without opening a database. The tests that run
hats-import itself skip where it is not installed; the fixture (``make
stage-export``, ``rapidpipe selftest --stage export``) runs at the end.
"""

from __future__ import annotations

import contextlib
import csv
import importlib
import json
import tomllib
from pathlib import Path

import pytest

import rapidpipe.stages.export as export
from rapidpipe.products.catalogexport import role_for, validate_catalog_export_entry
from rapidpipe.products.manifest import Manifest
from rapidpipe.selftest.support.fakeexport import (
    ASSOCIATION_SET_INSTANCE,
    SECOND_SOURCE_SET_INSTANCE,
    SOURCE_SET_INSTANCE,
    STATISTICS_SET_INSTANCE,
    UNIT_ID,
    UNNAMED_SOURCE_SET_INSTANCE,
    FakeExportDatabase,
    build_export_input_set,
    seed_rows,
)
from rapidpipe.stages.contract import ExitCode

RUN = "01J8Y6QZ3M0000000000000RUN"
ATTEMPT = "01J8Y6QZ3M00000000000000A1"

SETTINGS = tomllib.loads(
    (Path(export.__file__).resolve().parent.parent / "settings" / "export.toml").read_text())

#: dev's [HATS_CATALOGS] sources_cols, verbatim
#: (cdf/awsBatchSubmitJobs_launchSingleSciencePipeline.ini on origin/dev).
DEV_SOURCES_COLS = ("sid,id,pid,ra,dec,xfit,yfit,fluxfit,xerr,yerr,fluxerr,npixfit,qfit,cfit,"
                    "flags,sharpness,roundness1,roundness2,npix,peak,field,hp6,hp9,expid,fid,"
                    "sca,mjdobs,isdiffpos")

_have_hats_import = importlib.util.find_spec("hats_import") is not None
needs_hats = pytest.mark.skipif(not _have_hats_import, reason="hats-import is not installed")


def _instances(**overrides):
    rows = {
        SOURCE_SET_INSTANCE: {"kind": "source-set", "complete": True,
                              "deletion_state": "retained", "row_count": 60},
        SECOND_SOURCE_SET_INSTANCE: {"kind": "source-set", "complete": True,
                                     "deletion_state": "retained", "row_count": 40},
        UNNAMED_SOURCE_SET_INSTANCE: {"kind": "source-set", "complete": True,
                                      "deletion_state": "retained", "row_count": 30},
        ASSOCIATION_SET_INSTANCE: {"kind": "association-set", "complete": True,
                                   "deletion_state": "retained", "row_count": 0},
        STATISTICS_SET_INSTANCE: {"kind": "statistics-set", "complete": True,
                                  "deletion_state": "retained", "row_count": 0},
    }
    for name, value in overrides.items():
        rows[name].update(value)
    return rows


@pytest.fixture()
def fake_db(monkeypatch):
    db = FakeExportDatabase(
        instances=_instances(),
        sources=seed_rows({SOURCE_SET_INSTANCE: 60, SECOND_SOURCE_SET_INSTANCE: 40,
                           UNNAMED_SOURCE_SET_INSTANCE: 30}))
    monkeypatch.setattr(export, "open_database", lambda: contextlib.nullcontext(db))
    return db


def _argv(inputs: Path, outputs: Path, *extra: str) -> list[str]:
    return ["--run", RUN, "--unit", UNIT_ID, "--attempt", ATTEMPT,
            "--inputs", str(inputs), "--outputs", str(outputs), *extra]


def _overlay(tmp_path: Path, text: str) -> str:
    path = tmp_path / "overlay.toml"
    path.write_text(text)
    return str(path)


def _run(tmp_path: Path, *extra: str, **build) -> tuple[int, Path]:
    inputs = tmp_path / "inputs"
    if not (inputs / "manifest.json").exists():
        build_export_input_set(inputs, **build)
    outputs = tmp_path / "outputs"
    return export.main(_argv(inputs, outputs, *extra)), outputs


# ----------------------------------------------------------------------
# Declaration and settings
# ----------------------------------------------------------------------


def test_declaration():
    export.DECLARATION.validate()
    d = export.DECLARATION
    assert (d.name, d.unit, d.database_access) == ("export", "field", "read")
    assert d.consumes == ("source-set", "association-set")
    assert d.produces == ("catalog-export",)
    assert ExitCode.NOT_IMPLEMENTED not in d.supported_exit_codes


def test_declaration_import_performs_no_io(monkeypatch):
    def _raise(*args, **kwargs):
        raise AssertionError("export module import must not read any file")
    monkeypatch.setattr(Path, "open", _raise)
    monkeypatch.setattr(Path, "read_text", _raise)
    importlib.reload(export)
    assert export.DECLARATION.name == "export"


def test_default_settings_are_devs():
    export._check_settings(SETTINGS)
    hats = SETTINGS["hats"]
    assert ",".join(hats["columns"]) == DEV_SOURCES_COLS
    assert (hats["lowest_healpix_order"], hats["highest_healpix_order"], hats["n_workers"]) == (2, 9, 1)
    assert (hats["ra_col"], hats["dec_col"], hats["catalog_name"]) == ("ra", "dec", "sources_hats_catalog")
    assert SETTINGS["export"]["flags_zero_only"] is False  # dev filters nothing
    assert SETTINGS["export"]["csv_rows_per_file"] == 100000


@pytest.mark.parametrize("overlay", [
    "[export]\nunknown_key = 1\n",
    "[export]\ncatalog_type = \"objects\"\n",
    "[export]\ncatalog_type = \"light-curves\"\n",
    "[export]\nflags_zero_only = 1\n",
    "[export]\ncsv_rows_per_file = 0\n",
    "[hats]\nlowest_healpix_order = -1\n",
    "[hats]\nhighest_healpix_order = 20\n",
    "[hats]\nlowest_healpix_order = 9\nhighest_healpix_order = 3\n",
    "[hats]\nn_workers = 0\n",
    "[hats]\npixel_threshold = 0\n",
    "[hats]\nra_col = \"\"\n",
    "[hats]\nra_col = \"meanra\"\n",
    "[hats]\ncolumns = [\"sid\", \"ra\", \"dec\", \"bobby_tables; drop\"]\n",
    "[hats]\ncolumns = [\"sid\", \"ra\", \"ra\", \"dec\"]\n",
    "[hats]\nsort_columns = [\"nope\"]\n",
    "[hats]\ncatalog_name = \"../escape\"\n",
    "[hats]\nhats_catalog_type = \"margin\"\n",
    "[hats]\ndask_processes = \"yes\"\n",
])
def test_bad_settings_exit_64(tmp_path, overlay, fake_db):
    rc, outputs = _run(tmp_path, "--settings", _overlay(tmp_path, overlay))
    assert rc == int(ExitCode.USAGE)
    assert not (outputs / "manifest.json").exists()
    assert fake_db.queries == []


def test_non_field_unit_exits_64(tmp_path, fake_db):
    inputs = tmp_path / "inputs"
    build_export_input_set(inputs)
    rc = export.main(["--run", RUN, "--unit", "W146", "--attempt", ATTEMPT,
                      "--inputs", str(inputs), "--outputs", str(tmp_path / "outputs")])
    assert rc == int(ExitCode.USAGE)


# ----------------------------------------------------------------------
# Dry run and input-set shape
# ----------------------------------------------------------------------


def test_dry_run_prints_the_plan_and_opens_no_database(tmp_path, monkeypatch, capsys):
    def _no_db():
        raise AssertionError("--dry-run must not open the database")
    monkeypatch.setattr(export, "open_database", _no_db)
    rc, outputs = _run(tmp_path, "--dry-run")
    assert rc == int(ExitCode.SUCCESS)
    assert not outputs.exists() or not any(outputs.iterdir())
    captured = capsys.readouterr()
    plan = [line for line in (captured.out + captured.err).splitlines() if "export plan" in line]
    assert plan and "sources_hats_catalog" in plan[0] and SOURCE_SET_INSTANCE in plan[0]


def test_dry_run_bad_settings_exit_64(tmp_path, monkeypatch):
    monkeypatch.setattr(export, "open_database", lambda: pytest.fail("no database"))
    rc, _ = _run(tmp_path, "--dry-run", "--settings",
                 _overlay(tmp_path, "[hats]\nn_workers = 0\n"))
    assert rc == int(ExitCode.USAGE)


def test_dry_run_missing_result_sets_exits_65(tmp_path):
    rc, outputs = _run(tmp_path, "--dry-run", result_sets=())
    assert rc == int(ExitCode.INPUT_REJECTED)
    assert not outputs.exists() or not any(outputs.iterdir())


def test_missing_inputs_manifest_exits_65(tmp_path):
    inputs = tmp_path / "inputs"
    inputs.mkdir(parents=True)
    rc = export.main(_argv(inputs, tmp_path / "outputs"))
    assert rc == int(ExitCode.INPUT_REJECTED)


@pytest.mark.parametrize("field,value", [("unit", {"kind": "detector-image", "id": UNIT_ID}),
                                         ("stage", "crossmatch")])
def test_wrong_input_manifest_shape_exits_65(tmp_path, field, value):
    path = build_export_input_set(tmp_path / "inputs")
    manifest = json.loads(path.read_text())
    manifest[field] = value
    path.write_text(json.dumps(manifest))
    rc, _ = _run(tmp_path)
    assert rc == int(ExitCode.INPUT_REJECTED)


@pytest.mark.parametrize("result_sets", [
    (), (SOURCE_SET_INSTANCE, SOURCE_SET_INSTANCE), (SOURCE_SET_INSTANCE, ""),
])
def test_bad_named_result_sets_exit_65(tmp_path, result_sets):
    rc, _ = _run(tmp_path, result_sets=result_sets)
    assert rc == int(ExitCode.INPUT_REJECTED)


# ----------------------------------------------------------------------
# Result-set classification (exit 65)
# ----------------------------------------------------------------------


@pytest.mark.parametrize("named,instances,message", [
    (("01J8Y6QZ3M00000000000NOPE0",), _instances(), "not registered"),
    ((SOURCE_SET_INSTANCE, STATISTICS_SET_INSTANCE), _instances(), "statistics-set"),
    ((ASSOCIATION_SET_INSTANCE,), _instances(), "no source-set"),
    ((SOURCE_SET_INSTANCE,), _instances(**{SOURCE_SET_INSTANCE: {"complete": False}}),
     "not a complete"),
    ((SOURCE_SET_INSTANCE,), _instances(**{SOURCE_SET_INSTANCE: {"complete": None}}),
     "not a complete"),
    ((SOURCE_SET_INSTANCE,), _instances(**{SOURCE_SET_INSTANCE: {"deletion_state": "deleted"}}),
     "deleted"),
])
def test_classify_refuses(named, instances, message):
    with pytest.raises(export.InputRejected, match=message):
        export.classify_result_sets(named, instances)


def test_classify_splits_by_kind_in_named_order():
    named = (SECOND_SOURCE_SET_INSTANCE, ASSOCIATION_SET_INSTANCE, SOURCE_SET_INSTANCE)
    assert export.classify_result_sets(named, _instances()) == (
        [SECOND_SOURCE_SET_INSTANCE, SOURCE_SET_INSTANCE], [ASSOCIATION_SET_INSTANCE])


def test_unregistered_set_exits_65_before_any_row_is_read(tmp_path, fake_db):
    rc, outputs = _run(tmp_path, result_sets=(SOURCE_SET_INSTANCE, "01J8Y6QZ3M00000000000NOPE0"))
    assert rc == int(ExitCode.INPUT_REJECTED)
    assert fake_db.queries == [] and not (outputs / "manifest.json").exists()


def test_no_rows_exits_65(tmp_path, fake_db):
    fake_db.sources = [r for r in fake_db.sources if r["result_set"] != SOURCE_SET_INSTANCE]
    rc, outputs = _run(tmp_path, result_sets=(SOURCE_SET_INSTANCE,))
    assert rc == int(ExitCode.INPUT_REJECTED)
    assert not (outputs / "manifest.json").exists()


def test_database_unreachable_exits_75(tmp_path, monkeypatch):
    def _down():
        raise export.ConnectionUnavailable("no route to host")
    monkeypatch.setattr(export, "open_database", _down)
    rc, _ = _run(tmp_path)
    assert rc == int(ExitCode.TRANSIENT_FAILURE)


# ----------------------------------------------------------------------
# The CSV dump
# ----------------------------------------------------------------------


def test_csv_files_carry_devs_columns_and_split_by_rows_per_file(tmp_path, fake_db):
    columns = SETTINGS["hats"]["columns"]
    rows = fake_db.source_rows([SOURCE_SET_INSTANCE], tuple(columns), flags_zero_only=False)
    paths, total = export.write_csv_files(rows, columns, tmp_path, 25)
    assert total == 60
    assert [p.name for p in paths] == ["sources_1.csv", "sources_2.csv", "sources_3.csv"]
    counts = []
    for path in paths:
        with path.open(newline="") as fh:
            read = list(csv.reader(fh))
        assert ",".join(read[0]) == DEV_SOURCES_COLS
        counts.append(len(read) - 1)
    assert counts == [25, 25, 10]


def test_csv_rows_are_the_named_sets_by_sid_and_flags_filter(fake_db, tmp_path):
    columns = ("sid", "flags", "result_set")
    rows = list(fake_db.source_rows([SECOND_SOURCE_SET_INSTANCE, SOURCE_SET_INSTANCE], columns,
                                    flags_zero_only=False))
    assert len(rows) == 100 and [r[0] for r in rows] == sorted(r[0] for r in rows)
    assert {r[2] for r in rows} == {SOURCE_SET_INSTANCE, SECOND_SOURCE_SET_INSTANCE}
    unflagged = list(fake_db.source_rows([SOURCE_SET_INSTANCE], columns, flags_zero_only=True))
    assert unflagged and all(r[1] == 0 for r in unflagged) and len(unflagged) == 54


def test_no_rows_write_no_file(tmp_path):
    assert export.write_csv_files(iter(()), ["sid", "ra", "dec"], tmp_path, 10) == ([], 0)
    assert list(tmp_path.iterdir()) == []


# ----------------------------------------------------------------------
# The product
# ----------------------------------------------------------------------


@pytest.mark.parametrize("path,role", [
    ("properties", "hats"), ("hats.properties", "hats"),
    ("dataset/Norder=3/Dir=0/Npix=450.parquet", "partition"),
    ("dataset/_metadata", "metadata"), ("partition_info.csv", "metadata"),
    ("skymap.fits", "metadata"), ("dataset/properties", "metadata"),
])
def test_member_roles(path, role):
    assert role_for(path) == role


def _fake_catalog(outputs: Path) -> Path:
    catalog = outputs / "hats" / "sources_hats_catalog"
    (catalog / "dataset" / "Norder=3" / "Dir=0").mkdir(parents=True)
    (catalog / "dataset" / "Norder=4" / "Dir=0").mkdir(parents=True)
    (catalog / "properties").write_text("catalog_name=sources_hats_catalog\nhats_nrows=100\n")
    (catalog / "hats.properties").write_text("catalog_name=sources_hats_catalog\nhats_nrows=100\n")
    (catalog / "partition_info.csv").write_text("Norder,Npix\n3,450\n4,1801\n")
    (catalog / "dataset" / "_metadata").write_bytes(b"PAR1")
    (catalog / "dataset" / "Norder=3" / "Dir=0" / "Npix=450.parquet").write_bytes(b"PAR1a")
    (catalog / "dataset" / "Norder=4" / "Dir=0" / "Npix=1801.parquet").write_bytes(b"PAR1b")
    return catalog


def _key():
    return {"field": int(UNIT_ID), "export_type": "sources", "result_set": SOURCE_SET_INSTANCE,
            "settings_hash": "a" * 64}


def test_catalog_entry_shape(tmp_path):
    catalog = _fake_catalog(tmp_path)
    entry = export.catalog_entry(catalog, tmp_path, key=_key(), format_version="1",
                                 row_count=100, hats_version="0.11.0",
                                 source_sets=[SOURCE_SET_INSTANCE, SECOND_SOURCE_SET_INSTANCE])
    assert entry.kind == "catalog-export" and entry.primary == "hats/sources_hats_catalog/properties"
    reg = entry.registration
    assert set(reg) == {"row_count", "export_type", "hats_version", "source_sets",
                        "healpix_order", "partition_count", "md5"}
    assert (reg["row_count"], reg["partition_count"], reg["healpix_order"]) == (100, 2, 4)
    assert reg["source_sets"] == [SOURCE_SET_INSTANCE, SECOND_SOURCE_SET_INSTANCE]
    assert len(entry.members) == 6
    assert sorted(m.role for m in entry.members).count("hats") == 2
    validate_catalog_export_entry(entry.to_dict())


def test_catalog_entry_refuses_a_row_count_mismatch(tmp_path):
    catalog = _fake_catalog(tmp_path)
    with pytest.raises(export.StageError, match="100 rows"):
        export.catalog_entry(catalog, tmp_path, key=_key(), format_version="1", row_count=99,
                             hats_version="0.11.0", source_sets=[SOURCE_SET_INSTANCE])


@pytest.mark.parametrize("mutate,message", [
    (lambda e: e["key"].pop("settings_hash"), "key must name exactly"),
    (lambda e: e["key"].update(export_type="light-curves"), "export_type"),
    (lambda e: e.update(primary=next(m["path"] for m in e["members"] if m["role"] == "metadata")), "primary"),
    (lambda e: e["registration"].update(partition_count=5), "partition_count"),
    (lambda e: e["registration"].update(source_sets=[SECOND_SOURCE_SET_INSTANCE]), "first of"),
    (lambda e: e["registration"].update(row_count=0), "row_count"),
    (lambda e: e["registration"].pop("md5"), "registration must name exactly"),
    (lambda e: e["members"][0].update(role="bundle"), "roles"),
])
def test_validator_refuses(tmp_path, mutate, message):
    catalog = _fake_catalog(tmp_path)
    entry = export.catalog_entry(catalog, tmp_path, key=_key(), format_version="1",
                                 row_count=100, hats_version="0.11.0",
                                 source_sets=[SOURCE_SET_INSTANCE]).to_dict()
    entry = json.loads(json.dumps(entry))
    mutate(entry)
    with pytest.raises(ValueError, match=message):
        validate_catalog_export_entry(entry)


def test_hats_import_failure_exits_70(tmp_path, fake_db, monkeypatch):
    def _boom(*args, **kwargs):
        raise export.StageError("hats-import failed: RuntimeError: boom")
    monkeypatch.setattr(export, "build_hats_catalog", _boom)
    rc, outputs = _run(tmp_path)
    assert rc == int(ExitCode.STAGE_ERROR)
    assert not (outputs / "manifest.json").exists()


def test_stage_with_a_stub_import_writes_the_manifest(tmp_path, fake_db, monkeypatch):
    """The body end to end with hats-import replaced: key, registration, notes, reads."""
    seen = {}

    def _stub(csv_paths, hats, output_path, tmp_dir):
        seen["csv"] = [p.read_text().splitlines() for p in csv_paths]
        _fake_catalog(output_path.parent)
        return "0.0.stub"
    monkeypatch.setattr(export, "build_hats_catalog", _stub)
    # The stub's catalog says 100 rows: the two named sets, 60 + 40.
    rc, outputs = _run(tmp_path)
    assert rc == int(ExitCode.SUCCESS)
    manifest = Manifest.read(outputs / "manifest.json")
    (entry,) = manifest.outputs
    assert entry.key["field"] == int(UNIT_ID) and entry.key["result_set"] == SOURCE_SET_INSTANCE
    assert entry.key["export_type"] == "sources" and len(entry.key["settings_hash"]) == 64
    assert entry.registration["source_sets"] == [SOURCE_SET_INSTANCE, SECOND_SOURCE_SET_INSTANCE]
    assert entry.registration["hats_version"] == "0.0.stub"
    assert list(manifest.inputs.result_sets) == [SOURCE_SET_INSTANCE, SECOND_SOURCE_SET_INSTANCE,
                                                 ASSOCIATION_SET_INSTANCE]
    assert fake_db.queries[0]["source_sets"] == [SOURCE_SET_INSTANCE, SECOND_SOURCE_SET_INSTANCE]
    assert len(seen["csv"][0]) == 101 and seen["csv"][0][0] == DEV_SOURCES_COLS
    assert not [p for p in outputs.iterdir() if p.name.startswith(".export-")]


@needs_hats
def test_stage_runs_hats_import_for_real(tmp_path, fake_db):
    rc, outputs = _run(tmp_path, "--settings", _overlay(tmp_path, "[hats]\ndask_processes = false\n"))
    assert rc == int(ExitCode.SUCCESS)
    (entry,) = Manifest.read(outputs / "manifest.json").outputs
    assert entry.registration["row_count"] == 100
    assert entry.registration["partition_count"] >= 1
    validate_catalog_export_entry(entry.to_dict())


@needs_hats
def test_fixture_passes():
    from rapidpipe.selftest.export import FIXTURE
    from rapidpipe.selftest.runner import run_fixture
    import sys
    repo_root = Path(export.__file__).resolve().parents[2]
    result = run_fixture(FIXTURE, tools="fake", python=sys.executable, repo_root=repo_root,
                         work_dir=None, output_location=None)
    assert result.exit_code == 0, result.checks.failures
    assert not result.checks.failures
