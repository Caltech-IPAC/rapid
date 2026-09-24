"""Tests for rapidpipe.stages.prune with the fake database: every step boundary.

Unit id parsing, the association-set entry selection by field, settings
validation (the only pruning rule, `done_check`), the done check, and the
not-best exclusion across all three `vbest`/`run` cases (ruling R6):
promoted current, made by another run, made by this run.
PostgreSQL-backed behaviour is in tests/db/test_prune.py.
"""

from __future__ import annotations

import contextlib

import pytest

import rapidpipe.stages.prune as prune
from rapidpipe.products.manifest import Manifest
from rapidpipe.stages.contract import ExitCode
from tests.unit.fakeprunedb import (
    ASSOCIATION_INSTANCE,
    FIELD,
    SOURCE_SET_INSTANCE,
    SOURCES_TABLE,
    FakePruneDatabase,
    build_prune_input_set,
)

RUN = "01J8Y6QZ3M0000000000000RUN"
OTHER_RUN = "01J8Y6QZ3M000000000OTHRRUN"
ATTEMPT = "01J8Y6QZ3M00000000000000A1"


def _seed(**overrides):
    seed = {
        "association_sets": {
            ASSOCIATION_INSTANCE: {"base": None, "source_sets": [SOURCE_SET_INSTANCE]}},
        "source_set_tables": {SOURCE_SET_INSTANCE: SOURCES_TABLE},
        "sources": {SOURCES_TABLE: [
            {"sid": 1, "pid": 101, "result_set": SOURCE_SET_INSTANCE},
            {"sid": 2, "pid": 102, "result_set": SOURCE_SET_INSTANCE},
            {"sid": 3, "pid": 103, "result_set": SOURCE_SET_INSTANCE},
        ]},
        "diffimages": {
            101: {"vbest": 1, "run": None},       # best: promoted current
            102: {"vbest": 0, "run": OTHER_RUN},   # not best: another run
            103: {"vbest": 0, "run": RUN},         # best: this run (R6's own-run clause)
        },
        "merges": {str(FIELD): [
            {"aid": 10, "sid": 1, "result_set": ASSOCIATION_INSTANCE},
            {"aid": 11, "sid": 1, "result_set": ASSOCIATION_INSTANCE},
            {"aid": 12, "sid": 2, "result_set": ASSOCIATION_INSTANCE},
            {"aid": 13, "sid": 2, "result_set": ASSOCIATION_INSTANCE},
            {"aid": 14, "sid": 3, "result_set": ASSOCIATION_INSTANCE},
        ]},
    }
    seed.update(overrides)
    return seed


@pytest.fixture()
def db(monkeypatch):
    database = FakePruneDatabase(**_seed())
    monkeypatch.setattr(prune, "open_database", lambda: contextlib.nullcontext(database))
    return database


def _run(tmp_path, *, unit_field=FIELD, manifest_field=None, base=None, source_sets=None,
        overlay="", run=RUN, name="prune"):
    inputs = tmp_path / f"{name}-inputs"
    build_prune_input_set(
        inputs, field=FIELD if manifest_field is None else manifest_field,
        base=base, source_sets=source_sets)
    argv = ["--run", run, "--unit", str(unit_field), "--attempt", f"{ATTEMPT}-{name}",
            "--inputs", str(inputs), "--outputs", str(tmp_path / f"{name}-outputs")]
    if overlay:
        overlay_path = tmp_path / f"{name}-overlay.toml"
        overlay_path.write_text(overlay)
        argv += ["--settings", str(overlay_path)]
    return prune.main(argv), tmp_path / f"{name}-outputs"


def _manifest(outputs):
    return Manifest.read(outputs / "manifest.json")


def test_declaration_validates():
    prune.DECLARATION.validate()
    assert prune.DECLARATION.database_access == "read-write"
    assert prune.DECLARATION.unit == "field"
    assert prune.DECLARATION.consumes == ("association-set",)
    assert prune.DECLARATION.produces == ("pruned-set",)


def test_dry_run_validates_without_writing(tmp_path, db):
    inputs = tmp_path / "inputs"
    build_prune_input_set(inputs, field=FIELD)
    argv = ["--run", RUN, "--unit", str(FIELD), "--attempt", ATTEMPT,
            "--inputs", str(inputs), "--outputs", str(tmp_path / "outputs"), "--dry-run"]
    rc = prune.main(argv)
    assert rc == ExitCode.SUCCESS
    assert not (tmp_path / "outputs" / "manifest.json").exists()
    assert db.commits == 0
    assert db.prunedmerges == []


@pytest.mark.parametrize("bad_unit", ["", "-1", "1.5", "abc", " 1", "+1", "007x"])
def test_a_malformed_unit_id_exits_64(tmp_path, db, bad_unit):
    inputs = tmp_path / "inputs"
    build_prune_input_set(inputs, field=FIELD)
    argv = ["--run", RUN, "--unit", bad_unit, "--attempt", ATTEMPT,
            "--inputs", str(inputs), "--outputs", str(tmp_path / "outputs")]
    rc = prune.main(argv)
    assert rc == ExitCode.USAGE
    assert not (tmp_path / "outputs" / "manifest.json").exists()


def test_a_manifest_naming_a_different_field_exits_65(tmp_path, db):
    rc, outputs = _run(tmp_path, unit_field=FIELD, manifest_field=FIELD + 1)
    assert rc == ExitCode.INPUT_REJECTED
    assert not (outputs / "manifest.json").exists()
    assert db.commits == 0


def test_a_bad_rule_exits_64(tmp_path, db):
    rc, outputs = _run(tmp_path, overlay='[prune]\nrule = "bogus"\n')
    assert rc == ExitCode.USAGE
    assert not (outputs / "manifest.json").exists()
    assert db.commits == 0


def test_excludes_not_best_pairs_across_all_three_vbest_run_cases(tmp_path, db):
    rc, outputs = _run(tmp_path)
    assert rc == ExitCode.SUCCESS
    (entry,) = _manifest(outputs).outputs
    assert entry.kind == "pruned-set" and entry.is_result_set()
    assert entry.key["base"] == ASSOCIATION_INSTANCE
    assert "settings_hash" in entry.key
    assert entry.registration["row_count"] == 2
    assert entry.registration["base_row_count"] == 5
    assert entry.registration["table"] == "prunedmerges"
    assert entry.registration["rule"] == "not-best"
    assert _manifest(outputs).inputs.result_sets == (ASSOCIATION_INSTANCE,)

    pairs = sorted((row["aid"], row["sid"]) for row in db.prunedmerges)
    assert pairs == [(12, 2), (13, 2)]  # sid 2's pairs: pid 102, vbest=0, another run
    assert db.commits == 1
    pruned = db.pruned_sets[entry.instance]
    assert pruned["row_count"] == 2 and pruned["complete"]
    assert pruned["inputs_result_sets"] == [ASSOCIATION_INSTANCE]


def test_an_empty_exclusion_is_still_a_complete_set(tmp_path, monkeypatch):
    database = FakePruneDatabase(**_seed(diffimages={
        101: {"vbest": 1, "run": None}, 102: {"vbest": 1, "run": None},
        103: {"vbest": 1, "run": None}}))
    monkeypatch.setattr(prune, "open_database", lambda: contextlib.nullcontext(database))

    rc, outputs = _run(tmp_path)
    assert rc == ExitCode.SUCCESS
    (entry,) = _manifest(outputs).outputs
    assert entry.registration["row_count"] == 0
    assert entry.registration["base_row_count"] == 5
    assert database.prunedmerges == []
    pruned = database.pruned_sets[entry.instance]
    assert pruned["row_count"] == 0 and pruned["complete"]


def test_done_check_reuses_a_complete_pruned_set_and_off_prunes_again(tmp_path, db):
    rc, first = _run(tmp_path, name="first")
    assert rc == 0
    first_instance = _manifest(first).outputs[0].instance
    assert db.commits == 1
    assert len(db.prunedmerges) == 2

    rc, second = _run(tmp_path, name="second")
    assert rc == 0
    assert _manifest(second).outputs[0].instance == first_instance
    assert db.commits == 1  # nothing written the second time
    assert len(db.prunedmerges) == 2

    rc, third = _run(tmp_path, name="third", overlay="[prune]\ndone_check = false\n")
    assert rc == 0
    third_instance = _manifest(third).outputs[0].instance
    assert third_instance != first_instance
    assert db.commits == 2
    assert len(db.prunedmerges) == 4
