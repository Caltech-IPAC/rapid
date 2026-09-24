"""Database-backed tests for the `crossmatch` stage: one field's sources into astroobjects and merges.

The chain runs for real against PostgreSQL with Q3C: an l2 image is
admitted and registered, the difference stage runs with fake tools, its
Photutils catalogs are replaced by synthetic ones whose positions sit in
field 4662268 (computed with ``tessellation_field``) and its north
neighbour, `register` records the difference manifest, `load` loads it as
a source set, and `crossmatch` associates the field. Covers the child
tables, the rows and their values, both passes, ON CONFLICT on a repeated
position, the association-set instance with its `result_sets` row and
dependency edges, the CLUSTER, a second attempt over the same inputs, the
done check, a base read as catalog (ruling R3), and an unknown source set.

Skips cleanly if PGHOST is unset (see conftest.py).
"""

from __future__ import annotations

import hashlib
import json

import pytest

import rapidpipe.stages.crossmatch as crossmatch
from rapidpipe.db import objects
from rapidpipe.products.manifest import Manifest
from rapidpipe.runs import repository as repo
from rapidpipe.science.crossmatch.catalog import new_object_id
from rapidpipe.science.spatial import field_neighbours, tessellation_field
from rapidpipe.stages.contract import ExitCode
from tests.unit.fakeloaddb import finder_row, main_row, write_photutils_catalogs

from .test_load import _run_load
from .test_register_difference import (
    _admitted_l2,
    _legacy_refimage,
    _register_difference,
    _run_difference,
)
from .test_register_l2 import _NoCloseNoCommitConnProxy

FIELD = 4662268
NORTH = 4658172
# Inside the field: 1 and 2 share a position (one object, two merges rows);
# 3 lies 4e-6 deg inside the north edge (dec -28.758562); 4 is flagged.
INSIDE = (269.45, -28.77)
EDGE_IN = (269.44, -28.758566)
EDGE_OUT = (269.44, -28.758558)
POSITIVE = ([main_row(1, 10.0, 12.0, *INSIDE), main_row(2, 11.0, 12.0, *INSIDE),
             main_row(3, 20.0, 20.0, *EDGE_IN), main_row(4, 30.0, 30.0, 269.46, -28.78, flags=4)],
            [finder_row(1), finder_row(2), finder_row(3), finder_row(4)])
# In the north neighbour, 8e-6 deg from 3: matched in pass 2 only.
NEGATIVE = ([main_row(1, 40.0, 40.0, *EDGE_OUT)], [finder_row(1)])


def test_the_synthetic_positions_fall_where_the_test_needs_them():
    assert tessellation_field(*INSIDE) == FIELD
    assert tessellation_field(*EDGE_IN) == FIELD
    assert tessellation_field(*EDGE_OUT) == NORTH
    assert NORTH in field_neighbours(FIELD)
    assert len(field_neighbours(FIELD)) == 8


def _replace_photutils_catalogs(outputs):
    """Swap the fake Photutils files for this test's catalogs; keep the manifest honest."""
    path = outputs / "manifest.json"
    manifest = json.loads(path.read_text())
    for entry in manifest["outputs"]:
        if entry["kind"] != "source-catalog" or entry["key"]["catalog_type"] != "photutils":
            continue
        rows = POSITIVE if entry["key"]["sign"] == "positive" else NEGATIVE
        by_role = {m["role"]: m for m in entry["members"]}
        write_photutils_catalogs(outputs / by_role["catalog"]["path"],
                                 outputs / by_role["finder"]["path"], *rows)
        for member in entry["members"]:
            data = (outputs / member["path"]).read_bytes()
            member["bytes"] = len(data)
            member["sha256"] = "sha256:" + hashlib.sha256(data).hexdigest()
        entry["registration"]["source_count"] = len(rows[0])
    path.write_text(json.dumps(manifest))


def _loaded_source_set(conn, tmp_path, monkeypatch):
    """Run difference -> register -> load; return the run and load's outputs."""
    l2_instance = _admitted_l2(conn, tmp_path, monkeypatch)
    with conn.cursor() as cur:
        rfid = _legacy_refimage(cur)
    run_id, diff_outputs = _run_difference(conn, tmp_path, monkeypatch, l2_instance=l2_instance,
                                           rfid=rfid)
    _replace_photutils_catalogs(diff_outputs)
    assert _register_difference(conn, monkeypatch, diff_outputs, run_id, tmp_path)[0] == 0
    rc, _, load_outputs = _run_load(conn, monkeypatch, tmp_path, run_id, diff_outputs)
    assert rc == 0
    return run_id, load_outputs


def _run_crossmatch(conn, monkeypatch, tmp_path, run_id, inputs, *, name="crossmatch",
                    overlay=None, unit_id=str(FIELD)):
    monkeypatch.setattr(crossmatch, "connect", lambda *a, **k: _NoCloseNoCommitConnProxy(conn))
    repo.add_unit(conn, run_id, "crossmatch", "field", unit_id)
    attempt_id = repo.allocate_attempt(conn, run_id, "crossmatch", unit_id)
    outputs = tmp_path / f"{name}-outputs"
    argv = ["--run", run_id, "--unit", unit_id, "--attempt", attempt_id,
            "--inputs", str(inputs), "--outputs", str(outputs)]
    if overlay is not None:
        settings = tmp_path / f"{name}.toml"
        settings.write_text(overlay)
        argv += ["--settings", str(settings)]
    return crossmatch.main(argv), attempt_id, outputs


def _output(outputs):
    manifest = Manifest.read(outputs / "manifest.json")
    (entry,) = manifest.outputs
    return manifest, entry


def _rows(cur, sql, params=()):
    cur.execute(sql, params)
    return cur.fetchall()


def test_crossmatch_writes_both_passes_and_one_complete_association_set(
        conn, tmp_path, monkeypatch):
    run_id, load_outputs = _loaded_source_set(conn, tmp_path, monkeypatch)
    (source_set,) = Manifest.read(load_outputs / "manifest.json").outputs
    table = source_set.registration["table"]

    rc, attempt_id, outputs = _run_crossmatch(conn, monkeypatch, tmp_path, run_id, load_outputs)
    assert rc == int(ExitCode.SUCCESS)
    manifest, entry = _output(outputs)
    assert entry.kind == "association-set"
    assert entry.key == {"field": FIELD, "base": None, "source_sets": [source_set.instance],
                         "settings_hash": entry.key["settings_hash"]}
    assert entry.registration["row_counts"] == {
        "astroobjects": 2, "merges": 4, "merges_pass1": 3, "merges_pass2": 1, "new_objects": 3}
    assert list(manifest.inputs.result_sets) == [source_set.instance]

    with conn.cursor() as cur:
        loaded = _rows(cur, f"SELECT sid, ra, dec, flags, fluxfit FROM {table} "
                            f"WHERE result_set = %s ORDER BY sid", (source_set.instance,))

        def at(position):
            return [r for r in loaded if abs(r[1] - position[0]) < 1e-9
                    and abs(r[2] - position[1]) < 1e-9 and r[3] == 0]

        inside = [r[0] for r in at(INSIDE)]
        assert len(inside) == 2
        ((edge_in_sid, _, _, _, edge_in_flux),) = at(EDGE_IN)
        ((edge_out_sid, _, _, _, _),) = at(EDGE_OUT)

        objects_rows = _rows(
            cur, f"SELECT aid, ra0, dec0, flux0, run, attempt FROM astroobjects_{FIELD} "
                 f"WHERE result_set = %s ORDER BY aid", (entry.instance,))
        inside_aid, edge_aid = new_object_id(*INSIDE), new_object_id(*EDGE_IN)
        assert sorted(r[0] for r in objects_rows) == sorted([inside_aid, edge_aid])
        by_aid = {r[0]: r for r in objects_rows}
        assert by_aid[edge_aid][1:4] == (pytest.approx(EDGE_IN[0]), pytest.approx(EDGE_IN[1]),
                                         pytest.approx(edge_in_flux))
        assert all(r[4:] == (run_id, attempt_id) for r in objects_rows)

        merges_rows = _rows(cur, f"SELECT aid, sid FROM merges_{FIELD} WHERE result_set = %s",
                            (entry.instance,))
        assert sorted(merges_rows) == sorted(
            [(inside_aid, inside[0]), (inside_aid, inside[1]), (edge_aid, edge_in_sid),
             (edge_aid, edge_out_sid)])

        cur.execute(
            "SELECT pi.kind, pi.producing_stage, pi.producing_attempt, pi.custody, "
            "rs.complete, rs.row_count FROM product_instances pi "
            "JOIN result_sets rs ON rs.instance = pi.id WHERE pi.id = %s", (entry.instance,))
        assert cur.fetchone() == ("association-set", "crossmatch", attempt_id, "candidate",
                                  True, 4)
        cur.execute("SELECT producer_instance FROM dependencies WHERE consumer_instance = %s",
                    (entry.instance,))
        assert {r[0] for r in cur.fetchall()} == {source_set.instance}

        cur.execute("SELECT indisclustered FROM pg_index WHERE indexrelid = %s::regclass",
                    (f"astroobjects_{FIELD}_radec_idx",))
        assert cur.fetchone()[0] is True


def test_a_second_attempt_makes_a_second_set_with_the_same_aids(conn, tmp_path, monkeypatch):
    run_id, load_outputs = _loaded_source_set(conn, tmp_path, monkeypatch)
    rc, _, first = _run_crossmatch(conn, monkeypatch, tmp_path, run_id, load_outputs,
                                   name="first")
    assert rc == 0
    rc, _, second = _run_crossmatch(conn, monkeypatch, tmp_path, run_id, load_outputs,
                                    name="second", overlay="[crossmatch]\ndone_check = false\n")
    assert rc == 0
    a, b = _output(first)[1], _output(second)[1]
    assert a.instance != b.instance
    assert b.registration["row_counts"] == a.registration["row_counts"]
    with conn.cursor() as cur:
        def aids(instance):
            return sorted(r[0] for r in _rows(
                cur, f"SELECT aid FROM astroobjects_{FIELD} WHERE result_set = %s", (instance,)))
        assert aids(a.instance) == aids(b.instance)
        assert objects.count_result_set_rows(cur, f"merges_{FIELD}", b.instance) == 4


def test_done_check_reuses_the_set(conn, tmp_path, monkeypatch):
    run_id, load_outputs = _loaded_source_set(conn, tmp_path, monkeypatch)
    rc, _, first = _run_crossmatch(conn, monkeypatch, tmp_path, run_id, load_outputs,
                                   name="first")
    assert rc == 0
    rc, _, second = _run_crossmatch(conn, monkeypatch, tmp_path, run_id, load_outputs,
                                    name="second")
    assert rc == 0
    assert _output(second)[1].instance == _output(first)[1].instance
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(DISTINCT result_set) FROM merges_{FIELD} WHERE run = %s",
                    (run_id,))
        assert cur.fetchone()[0] == 1


def test_a_base_is_read_as_catalog(conn, tmp_path, monkeypatch):
    run_id, load_outputs = _loaded_source_set(conn, tmp_path, monkeypatch)
    rc, _, first = _run_crossmatch(conn, monkeypatch, tmp_path, run_id, load_outputs,
                                   name="first")
    assert rc == 0
    base = json.loads((first / "manifest.json").read_text())["outputs"][0]
    composed = json.loads((load_outputs / "manifest.json").read_text())
    composed["outputs"].append(base)
    inputs = tmp_path / "with-base"
    inputs.mkdir()
    (inputs / "manifest.json").write_text(json.dumps(composed))

    rc, _, second = _run_crossmatch(conn, monkeypatch, tmp_path, run_id, inputs, name="second")
    assert rc == 0
    manifest, entry = _output(second)
    assert entry.key["base"] == base["instance"]
    assert entry.registration["row_counts"] == {
        "astroobjects": 0, "merges": 4, "merges_pass1": 3, "merges_pass2": 1, "new_objects": 0}
    assert base["instance"] in manifest.inputs.result_sets
    with conn.cursor() as cur:
        assert objects.association_chain(cur, entry.instance) == [entry.instance, base["instance"]]
        cur.execute("SELECT producer_instance FROM dependencies WHERE consumer_instance = %s",
                    (entry.instance,))
        assert base["instance"] in {r[0] for r in cur.fetchall()}


def test_an_unknown_source_set_exits_65(conn, tmp_path, monkeypatch):
    run_id, load_outputs = _loaded_source_set(conn, tmp_path, monkeypatch)
    composed = json.loads((load_outputs / "manifest.json").read_text())
    composed["outputs"][0]["instance"] = "01J8Y6QZ3M0000000000XSS99"
    inputs = tmp_path / "unknown"
    inputs.mkdir()
    (inputs / "manifest.json").write_text(json.dumps(composed))
    rc, _, outputs = _run_crossmatch(conn, monkeypatch, tmp_path, run_id, inputs)
    assert rc == int(ExitCode.INPUT_REJECTED)
    assert not (outputs / "manifest.json").exists()
