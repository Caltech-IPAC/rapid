"""Database-backed tests for the `load` stage: a registered difference image's catalogs into `sources`.

The chain runs for real against PostgreSQL: an l2 image is admitted and
registered, the difference stage runs with fake tools, its two Photutils
catalogs are replaced by synthetic ones in `dev`'s format (the fake
Photutils stand-in writes placeholder text) with the manifest's sizes and
SHA-256 updated to match, `register` records the difference manifest, and
`load` loads it. Covers the child table, the rows and their values, the
source-set instance and its `result_sets` row, provenance edges, the done
check and an unregistered instance.

Skips cleanly if PGHOST is unset (see conftest.py).
"""

from __future__ import annotations

import hashlib
import json

import healpy as hp
import pytest

import rapidpipe.stages.load as load
from database.modules.utils.roman_tessellation_db import RomanTessellationClosedForm
from rapidpipe.db import sources
from rapidpipe.products.manifest import Manifest
from rapidpipe.runs import repository as repo
from rapidpipe.stages.contract import ExitCode
from tests.unit.fakeloaddb import finder_row, main_row, write_photutils_catalogs

from .test_register_difference import (
    _admitted_l2,
    _legacy_refimage,
    _register_difference,
    _run_difference,
)
from .attempt_helpers import set_disposition
from .test_register_l2 import _NoCloseNoCommitConnProxy
from .test_repository import _make_unit

POSITIVE = ([main_row(1, 10.0, 12.0, 269.45, -28.77), main_row(2, -3.0, 12.0, 269.45, -28.77),
             main_row(3, 20.0, 21.0, 269.46, -28.76, flags=4)],
            [finder_row(1), finder_row(2), finder_row(3)])
NEGATIVE = ([main_row(1, 30.0, 30.0, 269.47, -28.75)], [finder_row(1)])


def _replace_photutils_catalogs(outputs):
    """Swap the fake Photutils files for loadable ones; keep the manifest honest."""
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


def _registered_difference(conn, tmp_path, monkeypatch):
    l2_instance = _admitted_l2(conn, tmp_path, monkeypatch)
    with conn.cursor() as cur:
        rfid = _legacy_refimage(cur)
    run_id, outputs = _run_difference(conn, tmp_path, monkeypatch, l2_instance=l2_instance,
                                      rfid=rfid)
    _replace_photutils_catalogs(outputs)
    assert _register_difference(conn, monkeypatch, outputs, run_id, tmp_path)[0] == 0
    return run_id, outputs


def _run_load(conn, monkeypatch, tmp_path, run_id, inputs, *, name="load", overlay=""):
    monkeypatch.setattr(load, "connect", lambda *a, **k: _NoCloseNoCommitConnProxy(conn))
    unit_id = f"{name}-unit"
    _make_unit(conn, run_id, stage="load", unit_id=unit_id)
    attempt_id = repo.allocate_attempt(conn, run_id, "load", unit_id)
    settings = tmp_path / f"{name}.toml"
    settings.write_text("[instrument]\nnaxis1_sciimage = 64\nnaxis2_sciimage = 64\n" + overlay)
    outputs = tmp_path / f"{name}-outputs"
    rc = load.main(["--run", run_id, "--unit", unit_id, "--attempt", attempt_id,
                    "--inputs", str(inputs), "--outputs", str(outputs),
                    "--settings", str(settings)])
    return rc, attempt_id, outputs


def test_load_writes_devs_rows_and_one_complete_source_set(conn, tmp_path, monkeypatch):
    run_id, diff_outputs = _registered_difference(conn, tmp_path, monkeypatch)
    diff_manifest = Manifest.read(diff_outputs / "manifest.json")
    diff_entry = next(e for e in diff_manifest.outputs if e.kind == "difference-image")

    rc, attempt_id, outputs = _run_load(conn, monkeypatch, tmp_path, run_id, diff_outputs)
    assert rc == int(ExitCode.SUCCESS)
    (entry,) = Manifest.read(outputs / "manifest.json").outputs
    assert entry.kind == "source-set"
    assert entry.key == {"difference": diff_entry.instance, "catalog_type": "photutils"}
    assert entry.registration["row_count"] == 3

    with conn.cursor() as cur:
        cur.execute("SELECT pid, rid FROM diffimages WHERE instance = %s", (diff_entry.instance,))
        pid, rid = cur.fetchone()
        cur.execute("SELECT expid, sca, fid, mjdobs, dateobs FROM l2files WHERE rid = %s", (rid,))
        expid, sca, fid, mjdobs, dateobs = cur.fetchone()
        table = sources.child_table_name(sources.obs_date_of(dateobs), sca)
        assert entry.registration["table"] == table

        cur.execute(
            f"SELECT id, isdiffpos, pid, expid, sca, fid, mjdobs, xfit, yfit, flags, field, "
            f"hp6, hp9, sharpness, peak, redchi, run, attempt, result_set FROM {table} "
            f"WHERE result_set = %s ORDER BY isdiffpos DESC, id", (entry.instance,))
        columns = [d[0] for d in cur.description]
        rows = [dict(zip(columns, r)) for r in cur.fetchall()]
        assert [(r["id"], r["isdiffpos"]) for r in rows] == [(1, True), (3, True), (1, False)]
        for r in rows:
            assert (r["pid"], r["expid"], r["sca"], r["fid"]) == (pid, expid, sca, fid)
            assert r["mjdobs"] == pytest.approx(mjdobs)
            assert (r["run"], r["attempt"], r["result_set"]) == (run_id, attempt_id, entry.instance)
        first = rows[0]
        assert (first["xfit"], first["yfit"], first["redchi"], first["peak"]) == (
            pytest.approx(10.0), pytest.approx(12.0), pytest.approx(1.25), pytest.approx(13.5))
        assert rows[1]["flags"] == 4
        assert first["field"] == RomanTessellationClosedForm().get_rtid(269.45, -28.77)
        assert first["hp6"] == hp.ang2pix(64, 269.45, -28.77, nest=True, lonlat=True)
        assert first["hp9"] == hp.ang2pix(512, 269.45, -28.77, nest=True, lonlat=True)

        cur.execute(
            "SELECT pi.kind, pi.producing_stage, pi.producing_attempt, pi.custody, "
            "rs.complete, rs.row_count FROM product_instances pi "
            "JOIN result_sets rs ON rs.instance = pi.id WHERE pi.id = %s", (entry.instance,))
        assert cur.fetchone() == ("source-set", "load", attempt_id, "candidate", True, 3)

        cur.execute("SELECT producer_instance FROM dependencies WHERE consumer_instance = %s",
                    (entry.instance,))
        producers = {r[0] for r in cur.fetchall()}
        photutils = {e.instance for e in diff_manifest.outputs if e.kind == "source-catalog"
                     and e.key["catalog_type"] == "photutils"}
        assert producers == {diff_entry.instance} | photutils


def test_done_check_reuses_the_set_and_off_loads_again(conn, tmp_path, monkeypatch):
    run_id, diff_outputs = _registered_difference(conn, tmp_path, monkeypatch)
    rc, first_attempt, first = _run_load(conn, monkeypatch, tmp_path, run_id, diff_outputs,
                                         name="first")
    assert rc == 0
    first_instance = Manifest.read(first / "manifest.json").outputs[0].instance
    set_disposition(conn, first_attempt, "succeeded")  # ruling R1: only a succeeded set is reused

    rc, _, second = _run_load(conn, monkeypatch, tmp_path, run_id, diff_outputs, name="second")
    assert rc == 0
    assert Manifest.read(second / "manifest.json").outputs[0].instance == first_instance
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM sources WHERE run = %s", (run_id,))
        assert cur.fetchone()[0] == 3

    rc, _, third = _run_load(conn, monkeypatch, tmp_path, run_id, diff_outputs, name="third",
                             overlay="[load]\ndone_check = false\n")
    assert rc == 0
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM sources WHERE run = %s", (run_id,))
        assert cur.fetchone()[0] == 6


def test_an_unregistered_difference_instance_exits_65(conn, tmp_path, monkeypatch):
    l2_instance = _admitted_l2(conn, tmp_path, monkeypatch)
    with conn.cursor() as cur:
        rfid = _legacy_refimage(cur)
    run_id, diff_outputs = _run_difference(conn, tmp_path, monkeypatch, l2_instance=l2_instance,
                                           rfid=rfid)
    _replace_photutils_catalogs(diff_outputs)
    rc, _, outputs = _run_load(conn, monkeypatch, tmp_path, run_id, diff_outputs)
    assert rc == int(ExitCode.INPUT_REJECTED)
    assert not (outputs / "manifest.json").exists()
