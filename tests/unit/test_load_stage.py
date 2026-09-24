"""Tests for rapidpipe.stages.load with the fake database: every step boundary.

Selection of the difference instance and its Photutils catalogs, member
verification, `dev`'s skip on a missing catalog, the done check,
``skip_loading``, the rows handed to COPY, the source-set registration,
and the exit codes. PostgreSQL-backed behaviour is in tests/db/test_load.py.
"""

from __future__ import annotations

import json

import pytest

import rapidpipe.stages.load as load
from rapidpipe.db.ids import is_valid_ulid
from rapidpipe.products.manifest import Manifest
from rapidpipe.stages.contract import ExitCode
from tests.unit.fakeloaddb import (
    DIFFERENCE_INSTANCE,
    FakeLoadDatabase,
    build_load_input_set,
    difference_row,
    finder_row,
    main_row,
)

RUN = "01J8Y6QZ3M0000000000000RUN"
ATTEMPT = "01J8Y6QZ3M00000000000000A1"

POSITIVE = ([main_row(1, 10.0, 12.0, 269.45, -28.77), main_row(2, -3.0, 12.0, 269.45, -28.77),
             main_row(3, 20.0, 21.0, 269.46, -28.76)],
            [finder_row(1), finder_row(2), finder_row(3)])
NEGATIVE = ([main_row(1, 30.0, 30.0, 269.47, -28.75)], [finder_row(1)])


@pytest.fixture()
def db(monkeypatch):
    database = FakeLoadDatabase(differences={DIFFERENCE_INSTANCE: difference_row()})
    import contextlib
    monkeypatch.setattr(load, "open_database", lambda: contextlib.nullcontext(database))
    return database


def _run(tmp_path, catalogs=None, overlay="", **build):
    inputs = tmp_path / "inputs"
    build_load_input_set(inputs, {"positive": POSITIVE, "negative": NEGATIVE}
                         if catalogs is None else catalogs, **build)
    argv = ["--run", RUN, "--unit", "e20260821001234/SCA07", "--attempt", ATTEMPT,
            "--inputs", str(inputs), "--outputs", str(tmp_path / "outputs")]
    if overlay:
        (tmp_path / "overlay.toml").write_text(overlay)
        argv += ["--settings", str(tmp_path / "overlay.toml")]
    return load.main(argv), tmp_path / "outputs"


def _manifest(outputs):
    return Manifest.read(outputs / "manifest.json")


def test_declaration_validates():
    load.DECLARATION.validate()
    assert load.DECLARATION.database_access == "read-write"
    assert load.DECLARATION.produces == ("source-set",)


def test_loads_positive_then_negative_rows_into_the_child_table(tmp_path, db):
    rc, outputs = _run(tmp_path)
    assert rc == ExitCode.SUCCESS
    assert db.made_tables == ["sources_20260821_7"]
    rows = db.tables["sources_20260821_7"]
    assert [(r["id"], r["isdiffpos"]) for r in rows] == [("1", "true"), ("3", "true"),
                                                         ("1", "false")]
    assert {r["pid"] for r in rows} == {"4242"}
    assert {(r["expid"], r["sca"], r["fid"], r["mjdobs"]) for r in rows} == {
        ("1234", "7", "3", "61273.125")}
    assert db.commits == 1

    (entry,) = _manifest(outputs).outputs
    assert entry.kind == "source-set" and entry.is_result_set()
    assert is_valid_ulid(entry.instance)
    assert entry.key == {"difference": DIFFERENCE_INSTANCE, "catalog_type": "photutils"}
    assert entry.registration == {"row_count": 3, "table": "sources_20260821_7",
                                  "rows_by_sign": {"positive": 2, "negative": 1}}
    assert {(r["run"], r["attempt"], r["result_set"]) for r in rows} == {
        (RUN, ATTEMPT, entry.instance)}

    registered = db.source_sets[entry.instance]
    assert (registered["run"], registered["stage"], registered["attempt"],
            registered["row_count"]) == (RUN, "load", ATTEMPT, 3)
    assert registered["products"] == {
        "difference-image": DIFFERENCE_INSTANCE,
        "source-catalog/positive": "01J8Y6QZ3M00000000000PUC00",
        "source-catalog/negative": "01J8Y6QZ3M00000000000PUC11"}
    assert _manifest(outputs).inputs.products == registered["products"]
    # dev deletes its CSV after the COPY; only the manifest and record remain.
    assert sorted(p.name for p in outputs.iterdir()) == ["exec", "manifest.json"]


def test_an_existing_child_table_is_not_made_again(tmp_path, db):
    db.tables["sources_20260821_7"] = []
    rc, _ = _run(tmp_path)
    assert rc == ExitCode.SUCCESS
    assert db.made_tables == []
    assert len(db.tables["sources_20260821_7"]) == 3


def test_empty_catalogs_make_an_empty_complete_set(tmp_path, db):
    rc, outputs = _run(tmp_path, {"positive": ([main_row(1, 99.0, 1.0, 269.45, -28.77)],
                                               [finder_row(1)]),
                                  "negative": ([main_row(1, 1.0, 1.0, 269.45, -28.77)],
                                               [finder_row(2)])},
                       overlay="[instrument]\nnaxis1_sciimage = 64\nnaxis2_sciimage = 64\n")
    assert rc == ExitCode.SUCCESS
    (entry,) = _manifest(outputs).outputs
    assert entry.registration["row_count"] == 0
    assert db.source_sets[entry.instance]["complete"] is True


def test_naxis_setting_bounds_the_fit_positions(tmp_path, db):
    rc, outputs = _run(tmp_path, overlay="[instrument]\nnaxis1_sciimage = 15\n")
    assert rc == ExitCode.SUCCESS
    # x_fit 20 and 30 exceed 15 + 1 - 0.5; only positive id 1 (x 10) remains.
    assert [r["id"] for r in db.tables["sources_20260821_7"]] == ["1"]


@pytest.mark.parametrize("missing", ["positive", "negative"])
def test_a_missing_photutils_catalog_skips_the_job_as_dev_does(tmp_path, db, missing):
    catalogs = {"positive": POSITIVE, "negative": NEGATIVE}
    del catalogs[missing]
    rc, outputs = _run(tmp_path, catalogs)
    assert rc == ExitCode.SUCCESS
    assert _manifest(outputs).outputs == ()
    assert db.tables == {} and db.source_sets == {} and db.commits == 0
    record = json.loads((outputs / _manifest(outputs).execution_record).read_text())
    assert record["notes"]["skipped"] == [f"no photutils {missing} source-catalog entry"]


def test_done_check_reuses_a_complete_set_in_the_same_run(tmp_path, db):
    key = {"difference": DIFFERENCE_INSTANCE, "catalog_type": "photutils"}
    db.source_sets["01J8Y6QZ3MABCDEFGHJKMNPR10"] = {
        "run": RUN, "key": key, "row_count": 3, "complete": True}
    rc, outputs = _run(tmp_path)
    assert rc == ExitCode.SUCCESS
    (entry,) = _manifest(outputs).outputs
    assert entry.instance == "01J8Y6QZ3MABCDEFGHJKMNPR10"
    assert entry.registration["row_count"] == 3
    assert db.tables == {} and db.commits == 0


def test_done_check_off_loads_again(tmp_path, db):
    key = {"difference": DIFFERENCE_INSTANCE, "catalog_type": "photutils"}
    db.source_sets["01J8Y6QZ3MABCDEFGHJKMNPR10"] = {
        "run": RUN, "key": key, "row_count": 3, "complete": True}
    rc, outputs = _run(tmp_path, overlay="[load]\ndone_check = false\n")
    assert rc == ExitCode.SUCCESS
    assert _manifest(outputs).outputs[0].instance != "01J8Y6QZ3MABCDEFGHJKMNPR10"
    assert len(db.tables["sources_20260821_7"]) == 3


def test_a_set_from_another_run_is_not_reused(tmp_path, db):
    key = {"difference": DIFFERENCE_INSTANCE, "catalog_type": "photutils"}
    db.source_sets["01J8Y6QZ3MABCDEFGHJKMNPR10"] = {
        "run": "01J8Y6QZ3M00000000000THER0", "key": key, "row_count": 3, "complete": True}
    rc, _ = _run(tmp_path)
    assert rc == ExitCode.SUCCESS
    assert len(db.tables["sources_20260821_7"]) == 3


def test_skip_loading_writes_nothing(tmp_path, db):
    rc, outputs = _run(tmp_path, overlay="[load]\nskip_loading = true\n")
    assert rc == ExitCode.SUCCESS
    assert _manifest(outputs).outputs == ()
    assert db.tables == {} and db.clustered == []


def test_cluster_and_analyze_when_on(tmp_path, db):
    rc, _ = _run(tmp_path, overlay="[child_tables]\ncluster_and_analyze = true\n")
    assert rc == ExitCode.SUCCESS
    assert db.clustered == ["sources_20260821_7"]


def test_cluster_and_analyze_is_off_by_default(tmp_path, db):
    assert _run(tmp_path)[0] == ExitCode.SUCCESS
    assert db.clustered == []


def test_sfft_selects_the_sfft_instance(tmp_path, db):
    rc, _ = _run(tmp_path, overlay='[load]\ndifferencer = "sfft"\n')
    # The input set holds only a ZOGY instance.
    assert rc == ExitCode.INPUT_REJECTED
    db.differences = {DIFFERENCE_INSTANCE: difference_row()}
    rc, _ = _run(tmp_path / "second", overlay='[load]\ndifferencer = "sfft"\n',
                 differencer="sfft")
    assert rc == ExitCode.SUCCESS


def test_an_unknown_differencer_is_a_usage_error(tmp_path, db):
    assert _run(tmp_path, overlay='[load]\ndifferencer = "naive"\n')[0] == ExitCode.USAGE


def test_an_unknown_settings_key_is_a_usage_error(tmp_path, db):
    assert _run(tmp_path, overlay="[load]\njobprocdate = 1\n")[0] == ExitCode.USAGE


def test_an_unregistered_difference_instance_exits_65(tmp_path, db):
    db.differences = {}
    rc, outputs = _run(tmp_path)
    assert rc == ExitCode.INPUT_REJECTED
    assert not (outputs / "manifest.json").exists()


def test_a_tampered_catalog_exits_65(tmp_path, db):
    inputs = tmp_path / "inputs"
    build_load_input_set(inputs, {"positive": POSITIVE, "negative": NEGATIVE})
    catalog = inputs / "work" / "zogy_diffimage_masked_psfcat.txt"
    catalog.write_text(catalog.read_text().replace("269.45", "269.44"))
    rc = load.main(["--run", RUN, "--unit", "u", "--attempt", ATTEMPT,
                    "--inputs", str(inputs), "--outputs", str(tmp_path / "out")])
    assert rc == ExitCode.INPUT_REJECTED
    assert db.tables == {}


def test_a_manifest_from_another_stage_exits_65(tmp_path, db):
    inputs = tmp_path / "inputs"
    path = build_load_input_set(inputs, {"positive": POSITIVE, "negative": NEGATIVE})
    manifest = json.loads(path.read_text())
    manifest["stage"] = "admit"
    path.write_text(json.dumps(manifest))
    rc = load.main(["--run", RUN, "--unit", "u", "--attempt", ATTEMPT,
                    "--inputs", str(inputs), "--outputs", str(tmp_path / "out")])
    assert rc == ExitCode.INPUT_REJECTED


def test_a_finalize_manifest_is_read_like_a_difference_one(tmp_path, db):
    # Chain difference -> finalize -> register -> load (ruling 2026-09-24):
    # finalize republishes the same entries, so load reads its manifest too.
    inputs = tmp_path / "inputs"
    path = build_load_input_set(inputs, {"positive": POSITIVE, "negative": NEGATIVE})
    manifest = json.loads(path.read_text())
    manifest["stage"] = "finalize"
    path.write_text(json.dumps(manifest))
    rc = load.main(["--run", RUN, "--unit", "u", "--attempt", ATTEMPT,
                    "--inputs", str(inputs), "--outputs", str(tmp_path / "out")])
    assert rc == ExitCode.SUCCESS
    assert db.tables["sources_20260821_7"]


def test_a_connection_failure_is_temporary(tmp_path, monkeypatch):
    from rapidpipe.db.connection import ConnectionUnavailable

    def unavailable():
        raise ConnectionUnavailable("no database")

    monkeypatch.setattr(load, "open_database", unavailable)
    assert _run(tmp_path)[0] == ExitCode.TRANSIENT_FAILURE


def test_a_row_count_mismatch_is_a_stage_error(tmp_path, db, monkeypatch):
    monkeypatch.setattr(db, "count_result_set_rows", lambda table, result_set: 0)
    rc, outputs = _run(tmp_path)
    assert rc == ExitCode.STAGE_ERROR
    assert db.commits == 0
    assert not (outputs / "manifest.json").exists()


def test_dry_run_touches_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(load, "open_database", lambda: pytest.fail("dry run opened the database"))
    inputs = tmp_path / "inputs"
    build_load_input_set(inputs, {"positive": POSITIVE, "negative": NEGATIVE})
    rc = load.main(["--run", RUN, "--unit", "u", "--attempt", ATTEMPT, "--inputs", str(inputs),
                    "--outputs", str(tmp_path / "out"), "--dry-run"])
    assert rc == ExitCode.SUCCESS
    assert not (tmp_path / "out").exists()


def test_the_database_env_names_a_factory(tmp_path, monkeypatch):
    monkeypatch.setenv(load.DATABASE_ENV, "tests.unit.fakeloaddb:no_such_factory")
    assert _run(tmp_path)[0] == ExitCode.USAGE
