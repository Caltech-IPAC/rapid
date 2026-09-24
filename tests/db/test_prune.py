"""Database-backed tests for the `prune` stage: the not-best merge exclusion into a `pruned-set`.

Runs `test_load.py`'s own chain helpers (difference -> register -> load,
for real against PostgreSQL) to get a loaded source set whose one
difference image is registered with `vbest = 0` and `run` the test's own
run -- ruling R6's own-run clause -- then fabricates an association-set
instance over that source set (`register_manifest`, as `crossmatch` would)
and a handful of `merges_<field>` pairs naming its one `sid`. The first
prune attempt, within the same run, excludes nothing (its source's
difference image was made by this run). The `diffimages` row is then
reassigned to another run (as promotion would, once its own image is
superseded) and a second attempt, with `done_check` off, excludes the
pairs and records them in `prunedmerges` under a new pruned-set instance.

Skips cleanly if PGHOST is unset (see conftest.py).
"""

from __future__ import annotations

import json

import rapidpipe.stages.prune as prune
from rapidpipe.db import objects
from rapidpipe.products.manifest import Manifest
from rapidpipe.runs import repository as repo
from rapidpipe.stages.contract import ExitCode

from .test_load import _registered_difference, _run_load
from .test_objects_child_tables import _result_set
from .test_register_l2 import _NoCloseNoCommitConnProxy
from .test_repository import _make_run

FIELD = 999999950   # a field no real per-field table has


def _write_crossmatch_manifest(inputs_dir, *, run_id, attempt_id, field, association_instance,
                               key):
    inputs_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": "1", "run": run_id,
        "unit": {"kind": "field", "id": str(field)},
        "stage": "crossmatch", "attempt": attempt_id,
        "execution_record": f"exec/{attempt_id}.json",
        "inputs": {"manifest": "inputs/manifest.json", "products": {}, "result_sets": []},
        "outputs": [{
            "kind": "association-set", "format_version": "1", "instance": association_instance,
            "key": key, "primary": None, "members": [], "registration": {},
        }],
    }
    (inputs_dir / "manifest.json").write_text(json.dumps(manifest))


def _run_prune(conn, monkeypatch, tmp_path, run_id, inputs, *, unit_id, name="prune", overlay=""):
    monkeypatch.setattr(prune, "connect", lambda *a, **k: _NoCloseNoCommitConnProxy(conn))
    repo.add_unit(conn, run_id, "prune", "field", unit_id)
    attempt_id = repo.allocate_attempt(conn, run_id, "prune", unit_id)
    outputs = tmp_path / f"{name}-outputs"
    argv = ["--run", run_id, "--unit", unit_id, "--attempt", attempt_id,
            "--inputs", str(inputs), "--outputs", str(outputs)]
    if overlay:
        settings = tmp_path / f"{name}.toml"
        settings.write_text(overlay)
        argv += ["--settings", str(settings)]
    rc = prune.main(argv)
    return rc, attempt_id, outputs


def _seeded_chain(conn, tmp_path, monkeypatch):
    """A loaded source set, an association set over it, and two merges rows on its one `sid`.

    Returns ``(run_id, field, association_instance, key, pid, sid)``. The
    `merges_<field>`/`astroobjects_<field>` tables carry no foreign keys
    (LIKE never copies one; 20260924-04's own header comment), so the two
    fabricated pairs can be inserted directly.
    """
    run_id, diff_outputs = _registered_difference(conn, tmp_path, monkeypatch)
    rc, _, load_outputs = _run_load(conn, monkeypatch, tmp_path, run_id, diff_outputs)
    assert rc == int(ExitCode.SUCCESS)
    (source_set,) = Manifest.read(load_outputs / "manifest.json").outputs

    with conn.cursor() as cur:
        cur.execute(f"SELECT sid, pid FROM {source_set.registration['table']} "
                    f"WHERE result_set = %s LIMIT 1", (source_set.instance,))
        sid, pid = cur.fetchone()
        cur.execute("SELECT vbest, run FROM diffimages WHERE pid = %s", (pid,))
        vbest, diff_run = cur.fetchone()
        assert vbest == 0 and diff_run == run_id  # dev's registration default; this run's own image

    key = {"field": FIELD, "base": None, "source_sets": [source_set.instance],
           "settings_hash": "h"}
    _, cx_attempt, association = _result_set(
        conn, "association-set", run_id=run_id, stage="crossmatch", key=key, rows=0)

    with conn.cursor() as cur:
        objects.ensure_field_object_tables(cur, FIELD)
        table = objects.field_table_names(FIELD)["merges"]
        cur.execute(
            f"INSERT INTO {table} (aid, sid, run, attempt, result_set) VALUES "
            "(%s, %s, %s, %s, %s), (%s, %s, %s, %s, %s)",
            (1, sid, run_id, cx_attempt, association, 2, sid, run_id, cx_attempt, association))

    return run_id, FIELD, association, key, pid, sid


def test_own_run_pairs_are_not_excluded(conn, tmp_path, monkeypatch):
    run_id, field, association, key, pid, sid = _seeded_chain(conn, tmp_path, monkeypatch)
    inputs = tmp_path / "cx-inputs"
    _write_crossmatch_manifest(inputs, run_id=run_id, attempt_id="cx-attempt", field=field,
                               association_instance=association, key=key)

    rc, attempt_id, outputs = _run_prune(conn, monkeypatch, tmp_path, run_id, inputs,
                                        unit_id=str(field))
    assert rc == int(ExitCode.SUCCESS)
    (entry,) = Manifest.read(outputs / "manifest.json").outputs
    assert entry.kind == "pruned-set" and entry.is_result_set()
    assert entry.key == {"base": association, "settings_hash": entry.key["settings_hash"]}
    assert entry.registration["row_count"] == 0
    assert entry.registration["base_row_count"] == 2
    assert entry.registration["table"] == "prunedmerges"
    assert entry.registration["rule"] == "not-best"

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM prunedmerges WHERE result_set = %s", (entry.instance,))
        assert cur.fetchone()[0] == 0

        cur.execute(
            "SELECT pi.kind, pi.producing_stage, pi.producing_attempt, rs.complete, rs.row_count "
            "FROM product_instances pi JOIN result_sets rs ON rs.instance = pi.id "
            "WHERE pi.id = %s", (entry.instance,))
        assert cur.fetchone() == ("pruned-set", "prune", attempt_id, True, 0)

        cur.execute("SELECT producer_instance FROM dependencies WHERE consumer_instance = %s",
                    (entry.instance,))
        assert {r[0] for r in cur.fetchall()} == {association}


def test_pairs_made_by_another_run_are_excluded_on_a_fresh_attempt(conn, tmp_path, monkeypatch):
    run_id, field, association, key, pid, sid = _seeded_chain(conn, tmp_path, monkeypatch)
    inputs = tmp_path / "cx-inputs"
    _write_crossmatch_manifest(inputs, run_id=run_id, attempt_id="cx-attempt", field=field,
                               association_instance=association, key=key)

    rc, _, first_outputs = _run_prune(conn, monkeypatch, tmp_path, run_id, inputs,
                                      unit_id=str(field), name="first")
    assert rc == 0
    first_instance = Manifest.read(first_outputs / "manifest.json").outputs[0].instance

    # As promotion would once this image's replacement is registered: the
    # difference image now belongs to another run, so it is no longer
    # "made by this run" -- with vbest still 0, it is no longer best.
    other_run = _make_run(conn, kind="scratch", selected_stages=["difference"])
    with conn.cursor() as cur:
        cur.execute("UPDATE diffimages SET run = %s WHERE pid = %s", (other_run, pid))

    rc, second_attempt, second_outputs = _run_prune(
        conn, monkeypatch, tmp_path, run_id, inputs, unit_id=str(field), name="second",
        overlay="[prune]\ndone_check = false\n")
    assert rc == int(ExitCode.SUCCESS)
    (entry,) = Manifest.read(second_outputs / "manifest.json").outputs
    assert entry.instance != first_instance
    assert entry.registration["row_count"] == 2
    assert entry.registration["base_row_count"] == 2

    with conn.cursor() as cur:
        cur.execute("SELECT aid, sid, base_set, run, attempt FROM prunedmerges "
                    "WHERE result_set = %s ORDER BY aid", (entry.instance,))
        rows = cur.fetchall()
        assert rows == [(1, sid, association, run_id, second_attempt),
                        (2, sid, association, run_id, second_attempt)]

        cur.execute(
            "SELECT rs.complete, rs.row_count FROM result_sets rs WHERE rs.instance = %s",
            (entry.instance,))
        assert cur.fetchone() == (True, 2)

        cur.execute("SELECT producer_instance FROM dependencies WHERE consumer_instance = %s",
                    (entry.instance,))
        assert {r[0] for r in cur.fetchall()} == {association}


def test_done_check_reuses_the_pruned_set_within_the_run(conn, tmp_path, monkeypatch):
    run_id, field, association, key, pid, sid = _seeded_chain(conn, tmp_path, monkeypatch)
    inputs = tmp_path / "cx-inputs"
    _write_crossmatch_manifest(inputs, run_id=run_id, attempt_id="cx-attempt", field=field,
                               association_instance=association, key=key)

    rc, _, first = _run_prune(conn, monkeypatch, tmp_path, run_id, inputs, unit_id=str(field),
                              name="first")
    assert rc == 0
    first_instance = Manifest.read(first / "manifest.json").outputs[0].instance

    rc, _, second = _run_prune(conn, monkeypatch, tmp_path, run_id, inputs, unit_id=str(field),
                               name="second")
    assert rc == 0
    assert Manifest.read(second / "manifest.json").outputs[0].instance == first_instance

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM product_instances WHERE kind = 'pruned-set' "
                    "AND run = %s", (run_id,))
        assert cur.fetchone()[0] == 1


def test_a_manifest_naming_a_different_field_exits_65(conn, tmp_path, monkeypatch):
    run_id, field, association, key, pid, sid = _seeded_chain(conn, tmp_path, monkeypatch)
    inputs = tmp_path / "cx-inputs"
    _write_crossmatch_manifest(inputs, run_id=run_id, attempt_id="cx-attempt", field=field,
                               association_instance=association, key=key)

    rc, _, outputs = _run_prune(conn, monkeypatch, tmp_path, run_id, inputs,
                                unit_id=str(field + 1), name="wrong-field")
    assert rc == int(ExitCode.INPUT_REJECTED)
    assert not (outputs / "manifest.json").exists()
