"""Tests for rapidpipe.stages.maintain with the fake database: every step boundary.

Unit-id parsing, the source-set table check, the missing-table check, the
happy path through the fake database, and the exit codes. PostgreSQL-backed
behaviour is in tests/db/test_maintain.py.
"""

from __future__ import annotations

import contextlib
import json

import pytest

import rapidpipe.stages.maintain as maintain
from rapidpipe.products.manifest import Manifest
from rapidpipe.stages.contract import ExitCode
from tests.unit.fakemaintaindb import (
    FakeMaintainDatabase,
    build_maintain_input_set,
)

RUN = "01J8Y6QZ3M0000000000MRUN0"
ATTEMPT = "01J8Y6QZ3M00000000000MAT1"
TABLE = "sources_20260821_7"
SOURCE_SET = "01J8Y6QZ3M0000000000MSS01"


@pytest.fixture()
def db(monkeypatch):
    database = FakeMaintainDatabase(existing_tables=[TABLE])
    monkeypatch.setattr(maintain, "open_database", lambda: contextlib.nullcontext(database))
    return database


def _run(tmp_path, *, unit="20260821/SCA07", table=TABLE, instance=SOURCE_SET):
    inputs = tmp_path / "inputs"
    build_maintain_input_set(inputs, table=table, instance=instance)
    argv = ["--run", RUN, "--unit", unit, "--attempt", ATTEMPT,
            "--inputs", str(inputs), "--outputs", str(tmp_path / "outputs")]
    return maintain.main(argv), tmp_path / "outputs"


def _manifest(outputs):
    return Manifest.read(outputs / "manifest.json")


def test_declaration_validates():
    maintain.DECLARATION.validate()
    assert maintain.DECLARATION.unit == "detector-date"
    assert maintain.DECLARATION.database_access == "read-write"
    assert maintain.DECLARATION.produces == ()
    assert maintain.DECLARATION.settings_schema_path is None


def test_clusters_and_analyzes_the_units_table(tmp_path, db):
    rc, outputs = _run(tmp_path)
    assert rc == ExitCode.SUCCESS
    assert db.clustered == [TABLE]
    assert db.commits == 1

    manifest = _manifest(outputs)
    assert manifest.outputs == ()
    assert manifest.inputs.result_sets == (SOURCE_SET,)
    record = json.loads((outputs / manifest.execution_record).read_text())
    assert record["notes"] == {"table": TABLE, "clustered": True}


def test_multiple_source_set_entries_are_all_read(tmp_path, db, monkeypatch):
    inputs = tmp_path / "inputs"
    build_maintain_input_set(inputs, table=TABLE, instance=SOURCE_SET)
    path = inputs / "manifest.json"
    manifest = json.loads(path.read_text())
    second = dict(manifest["outputs"][0])
    second["instance"] = "01J8Y6QZ3M0000000000MSS02"
    manifest["outputs"].append(second)
    path.write_text(json.dumps(manifest))

    rc = maintain.main(["--run", RUN, "--unit", "20260821/SCA07", "--attempt", ATTEMPT,
                        "--inputs", str(inputs), "--outputs", str(tmp_path / "outputs")])
    assert rc == ExitCode.SUCCESS
    result = _manifest(tmp_path / "outputs")
    assert set(result.inputs.result_sets) == {SOURCE_SET, "01J8Y6QZ3M0000000000MSS02"}
    assert db.clustered == [TABLE]
    assert db.commits == 1


@pytest.mark.parametrize("unit", ["20260821", "2026-08-21/SCA07", "20260821/SCA7",
                                  "20260821/sca07", "20260821/SCA007", "notadate/SCA07", ""])
def test_a_malformed_unit_id_is_a_usage_error(tmp_path, db, unit):
    rc, outputs = _run(tmp_path, unit=unit)
    assert rc == ExitCode.USAGE
    assert not (outputs / "manifest.json").exists()
    assert db.commits == 0


def test_a_source_set_naming_a_different_table_exits_65(tmp_path, db):
    rc, outputs = _run(tmp_path, table="sources_20260821_9")
    assert rc == ExitCode.INPUT_REJECTED
    assert not (outputs / "manifest.json").exists()
    assert db.commits == 0


def test_a_manifest_with_no_source_set_entries_exits_65(tmp_path, db):
    inputs = tmp_path / "inputs"
    build_maintain_input_set(inputs, table=TABLE, instance=SOURCE_SET)
    path = inputs / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["outputs"] = []
    path.write_text(json.dumps(manifest))

    rc = maintain.main(["--run", RUN, "--unit", "20260821/SCA07", "--attempt", ATTEMPT,
                        "--inputs", str(inputs), "--outputs", str(tmp_path / "outputs")])
    assert rc == ExitCode.INPUT_REJECTED
    assert db.commits == 0


def test_a_missing_child_table_exits_65(tmp_path):
    database = FakeMaintainDatabase(existing_tables=[])
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(maintain, "open_database", lambda: contextlib.nullcontext(database))
        rc, outputs = _run(tmp_path)
    assert rc == ExitCode.INPUT_REJECTED
    assert not (outputs / "manifest.json").exists()
    assert database.clustered == []
    assert database.commits == 0


def test_a_connection_failure_is_temporary(tmp_path, monkeypatch):
    from rapidpipe.db.connection import ConnectionUnavailable

    def unavailable():
        raise ConnectionUnavailable("no database")

    monkeypatch.setattr(maintain, "open_database", unavailable)
    assert _run(tmp_path)[0] == ExitCode.TRANSIENT_FAILURE


def test_dry_run_touches_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(maintain, "open_database",
                        lambda: pytest.fail("dry run opened the database"))
    inputs = tmp_path / "inputs"
    build_maintain_input_set(inputs, table=TABLE, instance=SOURCE_SET)
    rc = maintain.main(["--run", RUN, "--unit", "20260821/SCA07", "--attempt", ATTEMPT,
                        "--inputs", str(inputs), "--outputs", str(tmp_path / "out"),
                        "--dry-run"])
    assert rc == ExitCode.SUCCESS
    assert not (tmp_path / "out").exists()


def test_the_database_env_names_a_factory(tmp_path, monkeypatch):
    monkeypatch.setenv(maintain.DATABASE_ENV, "tests.unit.fakemaintaindb:no_such_factory")
    assert _run(tmp_path)[0] == ExitCode.USAGE


def test_an_unknown_settings_overlay_key_is_a_usage_error(tmp_path, db):
    inputs = tmp_path / "inputs"
    build_maintain_input_set(inputs, table=TABLE, instance=SOURCE_SET)
    overlay = tmp_path / "overlay.toml"
    overlay.write_text("[maintain]\nsomething = 1\n")
    rc = maintain.main(["--run", RUN, "--unit", "20260821/SCA07", "--attempt", ATTEMPT,
                        "--inputs", str(inputs), "--outputs", str(tmp_path / "out"),
                        "--settings", str(overlay)])
    assert rc == ExitCode.USAGE
