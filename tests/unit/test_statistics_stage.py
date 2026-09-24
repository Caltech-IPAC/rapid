"""Tests for rapidpipe.stages.statistics with the fake database: every step boundary.

Dry run, the unit id, the input manifest's association-set entry, the
membership setting, the happy path through the fake database (a
base-plus-delta chain), the done check, an empty association set, and the
exit codes. PostgreSQL-backed behaviour is in tests/db/test_statistics.py.
"""

from __future__ import annotations

import contextlib
import json

import pytest

import rapidpipe.stages.statistics as statistics
from rapidpipe.db.connection import ConnectionUnavailable
from rapidpipe.products.manifest import Manifest
from rapidpipe.selftest.runner import load_expected
from rapidpipe.selftest.support.fakestatisticsdb import (
    ASSOCIATION_SET,
    BASE_SET,
    FIELD,
    FakeStatisticsDatabase,
    build_statistics_input_set,
    seed_from_fixture,
)
from rapidpipe.stages.contract import ExitCode

RUN = "01J8Y6QZ3M0000000000XRUN01"
ATTEMPT = "01J8Y6QZ3M0000000000XATT01"
TABLE = f"astroobjectsmeta_{FIELD}"
SPEC = load_expected("statistics")["inputs"]
DELTA_SOURCE_SET = SPEC["source_sets"]["delta"]["instance"]


@pytest.fixture()
def db(monkeypatch):
    database = FakeStatisticsDatabase(**seed_from_fixture(SPEC))
    monkeypatch.setattr(statistics, "open_database", lambda: contextlib.nullcontext(database))
    return database


def _run(tmp_path, *, unit=str(FIELD), overlay=None, dry_run=False, **manifest_kwargs):
    inputs = tmp_path / "inputs"
    manifest_kwargs.setdefault("source_sets", [DELTA_SOURCE_SET])
    build_statistics_input_set(inputs, **manifest_kwargs)
    argv = ["--run", RUN, "--unit", unit, "--attempt", ATTEMPT,
            "--inputs", str(inputs), "--outputs", str(tmp_path / "outputs")]
    if overlay is not None:
        settings = tmp_path / "overlay.toml"
        settings.write_text(overlay)
        argv += ["--settings", str(settings)]
    if dry_run:
        argv.append("--dry-run")
    return statistics.main(argv), tmp_path / "outputs"


def _manifest(outputs):
    return Manifest.read(outputs / "manifest.json")


def test_declaration_validates():
    statistics.DECLARATION.validate()
    assert statistics.DECLARATION.unit == "field"
    assert statistics.DECLARATION.consumes == ("association-set",)
    assert statistics.DECLARATION.produces == ("statistics-set",)
    assert statistics.DECLARATION.database_access == "read-write"


def test_dry_run_touches_no_database(tmp_path, monkeypatch):
    def refuse():
        raise AssertionError("dry run opened the database")
    monkeypatch.setattr(statistics, "open_database", refuse)
    rc, outputs = _run(tmp_path, dry_run=True)
    assert rc == ExitCode.SUCCESS
    assert not (outputs / "manifest.json").exists()


@pytest.mark.parametrize("unit", ["abc", "-1", "4711398.0", "", " 4711398", "e001/SCA01"])
def test_a_unit_that_is_not_a_field_exits_64(tmp_path, db, unit):
    rc, outputs = _run(tmp_path, unit=unit)
    assert rc == ExitCode.USAGE
    assert db.commits == 0


def test_an_association_set_for_another_field_exits_65(tmp_path, db):
    rc, outputs = _run(tmp_path, unit=str(FIELD + 1))
    assert rc == ExitCode.INPUT_REJECTED
    assert not (outputs / "manifest.json").exists()
    assert db.commits == 0


def test_a_field_given_as_a_string_in_the_key_is_accepted(tmp_path, db):
    inputs = tmp_path / "inputs"
    build_statistics_input_set(inputs, source_sets=[DELTA_SOURCE_SET])
    manifest = json.loads((inputs / "manifest.json").read_text())
    manifest["outputs"][0]["key"]["field"] = str(FIELD)
    (inputs / "manifest.json").write_text(json.dumps(manifest))
    rc = statistics.main(["--run", RUN, "--unit", str(FIELD), "--attempt", ATTEMPT,
                          "--inputs", str(inputs), "--outputs", str(tmp_path / "outputs")])
    assert rc == ExitCode.SUCCESS


@pytest.mark.parametrize("extra", [0, 1])
def test_anything_but_exactly_one_association_set_exits_65(tmp_path, db, extra):
    inputs = tmp_path / "inputs"
    build_statistics_input_set(inputs, source_sets=[DELTA_SOURCE_SET])
    manifest = json.loads((inputs / "manifest.json").read_text())
    if extra:
        manifest["outputs"].append({**manifest["outputs"][0],
                                    "instance": "01J8Y6QZ3M00000000000ASC03"})
    else:
        manifest["outputs"] = []
    (inputs / "manifest.json").write_text(json.dumps(manifest))
    rc = statistics.main(["--run", RUN, "--unit", str(FIELD), "--attempt", ATTEMPT,
                          "--inputs", str(inputs), "--outputs", str(tmp_path / "outputs")])
    assert rc == ExitCode.INPUT_REJECTED


def test_membership_pruned_is_designed_in_and_refused_with_64(tmp_path, db, capsys):
    rc, _ = _run(tmp_path, overlay='[statistics]\nmembership = "pruned"\n')
    assert rc == ExitCode.USAGE
    captured = capsys.readouterr()
    assert "2026-09-22" in captured.out + captured.err
    assert db.commits == 0


def test_an_unknown_membership_exits_64(tmp_path, db):
    rc, _ = _run(tmp_path, overlay='[statistics]\nmembership = "whatever"\n')
    assert rc == ExitCode.USAGE


def test_happy_path_writes_one_row_per_object_of_the_chain(tmp_path, db):
    rc, outputs = _run(tmp_path)
    assert rc == ExitCode.SUCCESS
    manifest = _manifest(outputs)
    assert manifest.inputs.result_sets == (ASSOCIATION_SET,)
    assert manifest.inputs.products == {}
    (entry,) = manifest.outputs
    assert entry.kind == "statistics-set" and entry.is_result_set()
    assert entry.key == {"membership": ASSOCIATION_SET}
    assert entry.registration == {"table": TABLE, "row_count": 4, "objects_in_set": 4}

    assert db.commits == 1
    assert db.made_tables == [TABLE]
    assert db.queries[0]["chain"] == [ASSOCIATION_SET, BASE_SET]
    rows = db.tables[TABLE]
    assert [int(r["aid"]) for r in rows] == [101, 102, 103, 104]
    assert {int(r["aid"]): int(r["nsources"]) for r in rows} == {101: 3, 102: 1, 103: 2, 104: 2}
    assert all((r["run"], r["attempt"], r["result_set"]) == (RUN, ATTEMPT, entry.instance)
               for r in rows)
    registered = db.statistics_sets[entry.instance]
    assert registered["result_sets"] == [ASSOCIATION_SET]
    assert (registered["row_count"], registered["stage"]) == (4, "statistics")

    record = json.loads((outputs / manifest.execution_record).read_text())
    assert record["notes"]["association_chain"] == [ASSOCIATION_SET, BASE_SET]
    assert record["notes"]["source_sets"] == sorted(
        s["instance"] for s in SPEC["source_sets"].values())


def test_the_base_alone_reads_only_its_own_rows(tmp_path, db):
    rc, outputs = _run(tmp_path, instance=BASE_SET, base=None,
                       source_sets=[SPEC["source_sets"]["base"]["instance"]])
    assert rc == ExitCode.SUCCESS
    (entry,) = _manifest(outputs).outputs
    assert entry.registration["row_count"] == 3
    rows = {int(r["aid"]): int(r["nsources"]) for r in db.tables[TABLE]}
    assert rows == {101: 1, 102: 1, 103: 1}


def test_done_check_reuses_a_complete_set_and_off_writes_again(tmp_path, db):
    db.statistics_sets["01J8Y6QZ3M00000000000STS01"] = {
        "run": RUN, "key": {"membership": ASSOCIATION_SET}, "row_count": 4, "complete": True}
    rc, outputs = _run(tmp_path)
    assert rc == ExitCode.SUCCESS
    (entry,) = _manifest(outputs).outputs
    assert entry.instance == "01J8Y6QZ3M00000000000STS01"
    assert db.commits == 0 and TABLE not in db.tables

    rc, outputs = _run(tmp_path / "again", overlay="[statistics]\ndone_check = false\n")
    assert rc == ExitCode.SUCCESS
    assert _manifest(outputs).outputs[0].instance != "01J8Y6QZ3M00000000000STS01"
    assert db.commits == 1 and len(db.tables[TABLE]) == 4


def test_an_association_set_with_no_merges_rows_gives_an_empty_complete_set(tmp_path, db):
    db.merges[str(FIELD)] = []
    rc, outputs = _run(tmp_path)
    assert rc == ExitCode.SUCCESS
    (entry,) = _manifest(outputs).outputs
    assert entry.registration == {"table": TABLE, "row_count": 0, "objects_in_set": 0}
    assert db.statistics_sets[entry.instance]["complete"] is True
    assert db.commits == 1


def test_a_chain_naming_no_source_sets_exits_65(tmp_path, db):
    for row in db.instances.values():
        row["source_sets"] = []
    rc, outputs = _run(tmp_path)
    assert rc == ExitCode.INPUT_REJECTED
    assert db.commits == 0


def test_an_unregistered_association_set_exits_65(tmp_path, db):
    rc, _ = _run(tmp_path, instance="01J8Y6QZ3M00000000000ASC77")
    assert rc == ExitCode.INPUT_REJECTED


def test_an_incomplete_base_exits_65(tmp_path, db):
    db.instances[BASE_SET]["complete"] = False
    rc, _ = _run(tmp_path)
    assert rc == ExitCode.INPUT_REJECTED
    assert db.commits == 0


def test_a_missing_merges_table_exits_65(tmp_path, db):
    del db.merges[str(FIELD)]
    rc, _ = _run(tmp_path)
    assert rc == ExitCode.INPUT_REJECTED
    assert db.commits == 0


def test_a_row_count_mismatch_exits_70(tmp_path, db, monkeypatch):
    monkeypatch.setattr(db, "count_result_set_rows", lambda table, result_set: 3)
    rc, outputs = _run(tmp_path)
    assert rc == ExitCode.STAGE_ERROR
    assert db.commits == 0
    assert not (outputs / "manifest.json").exists()


def test_an_unreachable_database_exits_75(tmp_path, monkeypatch):
    @contextlib.contextmanager
    def unreachable():
        raise ConnectionUnavailable("no route")
        yield
    monkeypatch.setattr(statistics, "open_database", unreachable)
    rc, _ = _run(tmp_path)
    assert rc == ExitCode.TRANSIENT_FAILURE


def test_a_bad_database_override_exits_64(tmp_path, monkeypatch):
    monkeypatch.setenv(statistics.DATABASE_ENV, "no.such.module:factory")
    rc, _ = _run(tmp_path)
    assert rc == ExitCode.USAGE
