"""Tests for rapidpipe.stages.crossmatch with the fake database.

The unit id, the input manifest's entries, the settings checks, both passes
through the fake (counts, rows, manifest), the base chain (ruling R3), the
done check (R14), the legacy catalog switch, the count check and the exit
codes. PostgreSQL-backed behaviour is in tests/db/test_crossmatch.py.
"""

from __future__ import annotations

import contextlib
import json

import pytest

import rapidpipe.stages.crossmatch as crossmatch
from rapidpipe.products.manifest import Manifest
from rapidpipe.science.crossmatch.catalog import new_object_id
from rapidpipe.selftest.crossmatch import _source_rows
from rapidpipe.selftest.runner import load_expected
from rapidpipe.stages.contract import ExitCode
from rapidpipe.selftest.support.fakecrossmatchdb import (
    FakeCrossmatchDatabase,
    association_set_entry,
    build_crossmatch_input_set,
    source_set_entry,
)

RUN = "01J8Y6QZ3M0000000000XRUN0"
FIELD = 4662268
EXPECTED = load_expected("crossmatch")
SPEC = EXPECTED["inputs"]
SETS = sorted(SPEC["source_sets"])
TABLE = SPEC["table"]
COUNTS = EXPECTED["expected"]["row_counts"]


def _database() -> FakeCrossmatchDatabase:
    return FakeCrossmatchDatabase(
        source_sets={i: {"table": TABLE, "complete": True} for i in SETS},
        tables={TABLE: _source_rows(SPEC)})


@pytest.fixture()
def db(monkeypatch):
    database = _database()
    monkeypatch.setattr(crossmatch, "open_database", lambda: contextlib.nullcontext(database))
    return database


def _inputs(tmp_path, entries=None, name="inputs"):
    inputs = tmp_path / name
    if entries is None:
        entries = [source_set_entry(i, TABLE) for i in SETS]
    build_crossmatch_input_set(inputs, entries)
    return inputs


def _run(tmp_path, *, unit=str(FIELD), inputs=None, overlay=None, attempt="01J8Y6QZ3M00000000000XAT1",
         name="outputs", dry_run=False):
    inputs = inputs if inputs is not None else _inputs(tmp_path)
    argv = ["--run", RUN, "--unit", unit, "--attempt", attempt,
            "--inputs", str(inputs), "--outputs", str(tmp_path / name)]
    if overlay is not None:
        path = tmp_path / f"{name}.toml"
        path.write_text(overlay)
        argv += ["--settings", str(path)]
    if dry_run:
        argv.append("--dry-run")
    return crossmatch.main(argv), tmp_path / name


def _entry(outputs):
    manifest = Manifest.read(outputs / "manifest.json")
    (entry,) = manifest.outputs
    return manifest, entry


def test_declaration_validates():
    crossmatch.DECLARATION.validate()
    d = crossmatch.DECLARATION
    assert (d.name, d.unit, d.consumes, d.produces, d.database_access) == (
        "crossmatch", "field", ("source-set",), ("association-set",), "read-write")


def test_dry_run_touches_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(crossmatch, "open_database",
                        lambda: pytest.fail("dry run opened the database"))
    rc, outputs = _run(tmp_path, dry_run=True)
    assert rc == ExitCode.SUCCESS
    assert not outputs.exists()


@pytest.mark.parametrize("unit", ["abc", "-5", "4662268.0", " 4662268", "SCA01", ""])
def test_a_unit_that_is_not_a_field_is_a_usage_error(tmp_path, db, unit):
    rc, outputs = _run(tmp_path, unit=unit)
    assert rc == ExitCode.USAGE
    assert not (outputs / "manifest.json").exists()
    assert db.commits == 0


def test_the_happy_path_writes_both_passes_and_one_set(tmp_path, db):
    rc, outputs = _run(tmp_path)
    assert rc == ExitCode.SUCCESS
    manifest, entry = _entry(outputs)
    assert entry.kind == "association-set" and entry.is_result_set()
    assert entry.key == {"field": FIELD, "base": None, "source_sets": SETS,
                         "settings_hash": entry.key["settings_hash"]}
    assert entry.registration == {"astroobjects_table": f"astroobjects_{FIELD}",
                                  "merges_table": f"merges_{FIELD}", "row_counts": COUNTS}
    assert list(manifest.inputs.result_sets) == SETS
    assert manifest.inputs.products == {}
    assert db.commits == 1
    assert db.locks == [FIELD]
    assert db.clustered == [FIELD]
    registered = db.association_sets[entry.instance]
    assert registered["row_count"] == COUNTS["merges"]
    assert registered["result_sets_read"] == SETS
    objects = db.tables[f"astroobjects_{FIELD}"]
    assert len(objects) == COUNTS["astroobjects"]
    assert all(r["result_set"] == entry.instance and r["run"] == RUN for r in objects)
    record = json.loads((outputs / manifest.execution_record).read_text())
    assert record["notes"]["exposures"] == 2
    assert record["notes"]["neighbours"] == 8
    assert record["notes"]["cone_deg"] > 0.05


def test_a_second_attempt_without_a_base_makes_its_own_set_with_the_same_aids(tmp_path, db):
    rc, first = _run(tmp_path, name="first")
    assert rc == 0
    rc, second = _run(tmp_path, name="second", attempt="01J8Y6QZ3M00000000000XAT2",
                      overlay="[crossmatch]\ndone_check = false\n")
    assert rc == 0
    a, b = _entry(first)[1], _entry(second)[1]
    assert a.instance != b.instance
    assert b.registration["row_counts"] == COUNTS
    objects = db.tables[f"astroobjects_{FIELD}"]
    aids = lambda inst: sorted(r["aid"] for r in objects if r["result_set"] == inst)  # noqa: E731
    assert aids(a.instance) == aids(b.instance)


def test_done_check_reuses_a_complete_set_for_the_same_key(tmp_path, db):
    rc, first = _run(tmp_path, name="first")
    assert rc == 0
    rc, second = _run(tmp_path, name="second", attempt="01J8Y6QZ3M00000000000XAT2")
    assert rc == 0
    assert _entry(second)[1].instance == _entry(first)[1].instance
    assert db.commits == 1
    manifest = Manifest.read(second / "manifest.json")
    record = json.loads((second / manifest.execution_record).read_text())
    assert record["notes"] == {"done_check": {"reused": _entry(first)[1].instance}}


def test_a_base_is_read_as_catalog_so_nothing_is_new(tmp_path, db):
    rc, first = _run(tmp_path, name="first")
    assert rc == 0
    base = _entry(first)[1].instance
    entries = [source_set_entry(i, TABLE) for i in SETS] + [association_set_entry(base, FIELD)]
    rc, second = _run(tmp_path, name="second", attempt="01J8Y6QZ3M00000000000XAT2",
                      inputs=_inputs(tmp_path, entries, name="with-base"))
    assert rc == 0
    manifest, entry = _entry(second)
    assert entry.key["base"] == base
    assert entry.registration["row_counts"] == {
        "astroobjects": 0, "merges": 9, "merges_pass1": 8, "merges_pass2": 1, "new_objects": 0}
    assert list(manifest.inputs.result_sets) == sorted(SETS + [base])
    merges = [r for r in db.tables[f"merges_{FIELD}"] if r["result_set"] == entry.instance]
    base_aids = {r["aid"] for r in db.tables[f"astroobjects_{FIELD}"] if r["result_set"] == base}
    assert {r["aid"] for r in merges} == base_aids


def test_legacy_catalog_reads_rows_without_a_run(tmp_path, db):
    legacy_aid = new_object_id(269.45, -28.77)
    db.tables[f"astroobjects_{FIELD}"] = [{"aid": legacy_aid, "ra0": 269.45, "dec0": -28.77,
                                           "flux0": 1.0, "run": None, "attempt": None,
                                           "result_set": None}]
    db.tables[f"merges_{FIELD}"] = []
    rc, off = _run(tmp_path, name="off")
    assert rc == 0
    assert _entry(off)[1].registration["row_counts"]["astroobjects"] == 5

    rc, on = _run(tmp_path, name="on", attempt="01J8Y6QZ3M00000000000XAT2",
                  overlay="[crossmatch]\nlegacy_catalog = true\n")
    assert rc == 0
    counts = _entry(on)[1].registration["row_counts"]
    # 101 and 106 join the legacy object; the other four objects are new.
    assert counts["astroobjects"] == 4
    assert counts["merges"] == 9


def test_no_source_set_entries_exits_65(tmp_path, db):
    rc, outputs = _run(tmp_path, inputs=_inputs(tmp_path, []))
    assert rc == ExitCode.INPUT_REJECTED
    assert db.commits == 0


def test_an_unknown_source_set_instance_exits_65(tmp_path, db):
    entries = [source_set_entry("01J8Y6QZ3M0000000000XSS99", TABLE)]
    rc, outputs = _run(tmp_path, inputs=_inputs(tmp_path, entries))
    assert rc == ExitCode.INPUT_REJECTED
    assert not (outputs / "manifest.json").exists()
    assert db.commits == 0


def test_an_incomplete_source_set_exits_65(tmp_path, db):
    db.source_sets[SETS[0]]["complete"] = False
    assert _run(tmp_path)[0] == ExitCode.INPUT_REJECTED


def test_two_bases_exit_65(tmp_path, db):
    entries = [source_set_entry(SETS[0], TABLE), association_set_entry("01J8Y6QZ3M0000000000XAS01", FIELD),
               association_set_entry("01J8Y6QZ3M0000000000XAS02", FIELD)]
    assert _run(tmp_path, inputs=_inputs(tmp_path, entries))[0] == ExitCode.INPUT_REJECTED


def test_a_base_for_another_field_exits_65(tmp_path, db):
    entries = [source_set_entry(SETS[0], TABLE),
               association_set_entry("01J8Y6QZ3M0000000000XAS01", FIELD + 1)]
    assert _run(tmp_path, inputs=_inputs(tmp_path, entries))[0] == ExitCode.INPUT_REJECTED


def test_an_unregistered_base_exits_65(tmp_path, db):
    entries = [source_set_entry(SETS[0], TABLE),
               association_set_entry("01J8Y6QZ3M0000000000XAS01", FIELD)]
    assert _run(tmp_path, inputs=_inputs(tmp_path, entries))[0] == ExitCode.INPUT_REJECTED
    assert db.commits == 0


def test_a_count_mismatch_is_a_stage_error(tmp_path, db, monkeypatch):
    monkeypatch.setattr(db, "count_result_set_rows", lambda table, result_set: 0)
    rc, outputs = _run(tmp_path)
    assert rc == ExitCode.STAGE_ERROR
    assert db.commits == 0
    assert not (outputs / "manifest.json").exists()


def test_a_tile_without_eight_neighbours_matches_without_the_cone(tmp_path, db, monkeypatch):
    real = crossmatch.field_neighbours
    monkeypatch.setattr(crossmatch, "field_neighbours", lambda f: real(f)[:7])
    cones = []
    original = db.neighbour_matches

    def spy(*args):
        cones.append(args[-1])
        return original(*args)

    monkeypatch.setattr(db, "neighbour_matches", spy)
    rc, outputs = _run(tmp_path)
    assert rc == 0
    assert cones and all(c is None for c in cones)
    manifest, _ = _entry(outputs)
    record = json.loads((outputs / manifest.execution_record).read_text())
    assert record["notes"]["cone_deg"] is None


def test_cluster_between_passes_can_be_turned_off(tmp_path, db):
    rc, _ = _run(tmp_path, overlay="[crossmatch]\ncluster_between_passes = false\n")
    assert rc == 0
    assert db.clustered == []


@pytest.mark.parametrize("overlay", [
    "[source_matching]\nmatch_radius = 'x'\n",
    "[source_matching]\nmatch_radius = 0\n",
    "[crossmatch]\nsource_flags = true\n",
    "[crossmatch]\nsource_flags = 1.5\n",
    "[crossmatch]\ndone_check = 1\n",
    "[crossmatch]\nno_such_key = 1\n",
])
def test_bad_settings_are_usage_errors(tmp_path, db, overlay):
    assert _run(tmp_path, overlay=overlay)[0] == ExitCode.USAGE


def test_a_connection_failure_is_temporary(tmp_path, monkeypatch):
    from rapidpipe.db.connection import ConnectionUnavailable

    def unavailable():
        raise ConnectionUnavailable("no database")

    monkeypatch.setattr(crossmatch, "open_database", unavailable)
    assert _run(tmp_path)[0] == ExitCode.TRANSIENT_FAILURE


def test_the_database_env_names_a_factory(tmp_path, monkeypatch):
    monkeypatch.setenv(crossmatch.DATABASE_ENV, "rapidpipe.selftest.support.fakecrossmatchdb:no_such_factory")
    assert _run(tmp_path)[0] == ExitCode.USAGE


def test_catalog_clause_is_the_set_rows_clause_or_legacy():
    assert crossmatch.catalog_clause(["A", "B"], False) == ("(b.result_set = ANY(%s))", (["A", "B"],))
    assert crossmatch.catalog_clause(["A"], True) == (
        "(b.result_set = ANY(%s) OR b.run IS NULL)", (["A"],))
