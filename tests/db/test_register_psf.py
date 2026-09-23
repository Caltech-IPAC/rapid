"""Database-backed tests for `register` recording a `psf` entry (20260923-07, rapidpipe.db.psfs).

Nothing on `rebuild` emits a psf entry yet, so the manifest is written by
hand in the shape the products page fixes (kind `psf`, key filter,
detector, version) under a real admit attempt. Covers `dev`'s ``addPSF``
version allocation across the table, ``vbest`` 0 at registration, the run
columns, and replay.

Skips cleanly if PGHOST is unset (see conftest.py).
"""

from __future__ import annotations

import json

from rapidpipe.db.ids import new_ulid
from rapidpipe.runs import repository as repo
from rapidpipe.stages.contract import ExitCode

from .test_register_l2 import _run_register
from .test_repository import _make_run, _make_unit

MD5 = "9e107d9d372bb6826bd81d3542a419d6"


def _psf_manifest(conn, tmp_path, name, *, filter_="F184", detector="7"):
    run_id = _make_run(conn, selected_stages=["admit", "register"])
    _make_unit(conn, run_id, stage="admit", unit_id=f"{name}-admit")
    attempt_id = repo.allocate_attempt(conn, run_id, "admit", f"{name}-admit")
    outputs = tmp_path / name
    (outputs / "psf").mkdir(parents=True)
    (outputs / "psf" / "psf.fits").write_bytes(b"psf")
    instance = new_ulid()
    (outputs / "manifest.json").write_text(json.dumps({
        "schema_version": "1", "run": run_id,
        "unit": {"kind": "detector-image", "id": f"{name}-admit"},
        "stage": "admit", "attempt": attempt_id, "execution_record": f"exec/{attempt_id}.json",
        "inputs": {"manifest": "delivery/manifest.json", "products": {}, "result_sets": []},
        "outputs": [{
            "kind": "psf", "format_version": "1", "instance": instance,
            "key": {"filter": filter_, "detector": detector, "version": "1"},
            "primary": "psf/psf.fits",
            "members": [{"role": "psf", "path": "psf/psf.fits", "bytes": 3,
                         "sha256": "sha256:" + "3" * 64}],
            "registration": {"md5": MD5, "status": 1},
        }],
    }))
    return run_id, outputs, instance


def _psfs_row(cur, instance):
    cur.execute(
        "SELECT psfid, fid, sca, version, vbest, filename, checksum, status, run, attempt, instance "
        "FROM psfs WHERE instance = %s", (instance,))
    columns = [d[0] for d in cur.description]
    row = cur.fetchone()
    return dict(zip(columns, row)) if row is not None else None


def test_register_writes_a_psfs_row_through_addpsf(conn, tmp_path, monkeypatch):
    with conn.cursor() as cur:
        cur.execute("SELECT coalesce(max(version), 0) FROM psfs WHERE fid = 1 AND sca = 7")
        (before,) = cur.fetchone()
    run_id, outputs, instance = _psf_manifest(conn, tmp_path, "one")
    rc, registering_attempt = _run_register(conn, monkeypatch, outputs, run_id=run_id,
                                            unit_id="one-register", tmp_path=tmp_path, name="one")
    assert rc == int(ExitCode.SUCCESS)
    with conn.cursor() as cur:
        row = _psfs_row(cur, instance)
    assert row is not None
    assert (row["fid"], row["sca"], row["version"], row["vbest"], row["status"]) == (
        1, 7, before + 1, 0, 1)
    assert row["checksum"] == MD5
    assert row["filename"] == f"{outputs}/psf/psf.fits"
    assert (row["run"], row["attempt"], row["instance"]) == (run_id, registering_attempt, instance)


def test_a_second_run_takes_the_next_version(conn, tmp_path, monkeypatch):
    first_run, first_outputs, first = _psf_manifest(conn, tmp_path, "a")
    assert _run_register(conn, monkeypatch, first_outputs, run_id=first_run,
                         unit_id="a-register", tmp_path=tmp_path, name="a")[0] == 0
    second_run, second_outputs, second = _psf_manifest(conn, tmp_path, "b")
    assert _run_register(conn, monkeypatch, second_outputs, run_id=second_run,
                         unit_id="b-register", tmp_path=tmp_path, name="b")[0] == 0
    with conn.cursor() as cur:
        assert _psfs_row(cur, second)["version"] == _psfs_row(cur, first)["version"] + 1


def test_replay_writes_nothing_new(conn, tmp_path, monkeypatch):
    run_id, outputs, instance = _psf_manifest(conn, tmp_path, "r")
    assert _run_register(conn, monkeypatch, outputs, run_id=run_id,
                         unit_id="r-register-1", tmp_path=tmp_path, name="r1")[0] == 0
    assert _run_register(conn, monkeypatch, outputs, run_id=run_id,
                         unit_id="r-register-2", tmp_path=tmp_path, name="r2")[0] == 0
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM psfs WHERE run = %s", (run_id,))
        assert cur.fetchone()[0] == 1


def test_an_unknown_filter_exits_65(conn, tmp_path, monkeypatch):
    run_id, outputs, instance = _psf_manifest(conn, tmp_path, "u", filter_="F999")
    rc, _ = _run_register(conn, monkeypatch, outputs, run_id=run_id,
                          unit_id="u-register", tmp_path=tmp_path, name="u")
    assert rc == int(ExitCode.INPUT_REJECTED)
