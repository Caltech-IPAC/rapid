"""Database-backed tests for rapidpipe.stages.register recording the
l2-image kind, against a real PostgreSQL with the l2 run-columns
migration (20260921-05-l2-run-columns.sql) applied.

Builds a genuine admit manifest by running admit's own `main` on the FITS
fixture from tests/rapidpipe/test_admit.py, then runs register's `main`
for real against it, with `rapidpipe.stages.register.connect` monkeypatched
to hand back the test's own `conn` (wrapped so commit()/close() are
no-ops, so the outer per-test transaction -- rolled back at teardown, per
conftest.py -- is the only thing that ever actually commits or closes).

admit itself declares no database access and never opens a connection,
but the manifest it writes still names a run and an attempt (its own
`--run`/`--attempt` argv), and `register_manifest` looks both up in the
database -- the producing run must exist in `runs` and the producing
attempt named in the manifest must exist in `attempts` (it is the FK
`product_instances.producing_attempt` references). So every test here
allocates a REAL run/unit/attempt through the repository API first for
admit's own invocation, passes those ids on admit's argv, and only then
runs admit -- unlike tests/rapidpipe/test_admit.py, which is free to use
placeholder ids ("r1"/"a1") because it never touches a database.

Skips cleanly if PGHOST is unset (see conftest.py).
"""

from __future__ import annotations

import json

import pytest

from rapidpipe.db.ids import new_ulid
from rapidpipe.products.manifest import Manifest
from rapidpipe.runs import repository as repo
from rapidpipe.science.spatial import healpix_indexes, tessellation_field
from rapidpipe.stages.contract import ExitCode
from rapidpipe.stages.register import main as register_main
from tests.rapidpipe.test_admit import (
    DELIVERED_VERSION,
    EXPOSURE_ID,
    _build_delivery,
)
from tests.rapidpipe.test_admit import main as admit_main

from .test_repository import _make_run, _make_unit


def _build_delivery_for_detector(tmp_path, sca_value):
    """Like test_admit._build_delivery, but with the delivery key's
    'detector' field matching the header's SCA-NUM -- test_admit's own
    helper always keys on the module-level DETECTOR ("7") regardless of
    any sca_value override, since none of its own tests need a second
    detector. admit's own consistency check (delivery key detector must
    equal the header SCA-NUM) would reject a mismatched pair, so this
    local helper edits the manifest's key after building the delivery."""
    inputs_dir, fits_path = _build_delivery(tmp_path, sca_value=sca_value)
    manifest_path = inputs_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["outputs"][0]["key"]["detector"] = str(sca_value)
    manifest_path.write_text(json.dumps(manifest))
    return inputs_dir, fits_path


class _NoCloseNoCommitConnProxy:
    """Hands `register` the test's own `conn`, but absorbs commit()/close().

    The database tests' isolation (conftest.py) relies on every test's
    whole body running inside one already-open, never-committed outer
    transaction that is rolled back at teardown. `register`'s stage body
    calls `conn.commit()` on success and the `connect()` context manager
    it uses calls `conn.close()` on exit; both would end or destroy the
    outer transaction early if allowed through. Everything else
    (cursor(), rollback() on a failure path) passes straight to the real
    connection, so a test exercising register's failure path still sees
    a real rollback within the outer transaction.
    """

    def __init__(self, real_conn):
        self._real_conn = real_conn

    def __getattr__(self, name):
        return getattr(self._real_conn, name)

    def commit(self):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _admit_argv(inputs_dir, outputs_dir, *, run_id, unit_id, attempt_id, extra=()):
    return [
        "--run", run_id, "--unit", unit_id, "--attempt", attempt_id,
        "--inputs", str(inputs_dir), "--outputs", str(outputs_dir),
        *extra,
    ]


def _register_argv(inputs_dir, outputs_dir, *, run_id, unit_id, attempt_id, extra=()):
    return [
        "--run", run_id, "--unit", unit_id, "--attempt", attempt_id,
        "--inputs", str(inputs_dir), "--outputs", str(outputs_dir),
        *extra,
    ]


def _allocate_admit_attempt(conn, *, unit_id):
    """A real run/unit/attempt for admit's own invocation. admit's manifest
    names this run and attempt; register_manifest looks both up, so they
    must exist even though admit itself never opens a connection."""
    run_id = _make_run(conn)
    _make_unit(conn, run_id, stage="admit", unit_id=unit_id)
    attempt_id = repo.allocate_attempt(conn, run_id, "admit", unit_id)
    return run_id, attempt_id


def _allocate_registering_attempt(conn, run_id, *, unit_id):
    """A real unit/attempt for register's own invocation, on the SAME run
    admit produced into -- register_manifest's custody rule reads the
    manifest's own `run` (admit's), so this attempt's run only needs to
    exist; it need not be admit's run, but sharing it keeps one run per
    test simple and matches a real pipeline where admit and register
    process the same run."""
    _make_unit(conn, run_id, stage="register", unit_id=unit_id)
    return repo.allocate_attempt(conn, run_id, "register", unit_id)


def _run_admit(conn, tmp_path, *, name="admit1", sca_value=None, **fits_kwargs):
    """Allocate a real admit run/attempt, build a delivery, run admit for
    real; return (run_id, outputs_dir)."""
    if sca_value is not None:
        inputs_dir, _ = _build_delivery_for_detector(tmp_path / name, sca_value)
    else:
        inputs_dir, _ = _build_delivery(tmp_path / name, **fits_kwargs)

    run_id, attempt_id = _allocate_admit_attempt(conn, unit_id=f"{name}-admit-unit")
    outputs_dir = tmp_path / name / "admit-outputs"
    rc = admit_main(
        _admit_argv(
            inputs_dir, outputs_dir,
            run_id=run_id, unit_id=f"{name}-admit-unit", attempt_id=attempt_id))
    assert rc == int(ExitCode.SUCCESS), f"admit failed to produce a fixture manifest (rc={rc})"
    return run_id, outputs_dir


def _run_register(conn, monkeypatch, admit_outputs_dir, *, run_id, unit_id, tmp_path, name):
    import rapidpipe.stages.register as register_module

    monkeypatch.setattr(
        register_module, "connect",
        lambda *a, **k: _NoCloseNoCommitConnProxy(conn))

    attempt_id = _allocate_registering_attempt(conn, run_id, unit_id=unit_id)
    outputs_dir = tmp_path / name / "register-outputs"
    rc = register_main(
        _register_argv(
            admit_outputs_dir, outputs_dir,
            run_id=run_id, unit_id=unit_id, attempt_id=attempt_id))
    return rc, attempt_id


def _l2files_row(cur, instance):
    cur.execute(
        "SELECT rid, run, attempt, instance, version, vbest, checksum, fid, "
        "filename, overlapfields, expid FROM l2files WHERE instance = %s",
        (instance,))
    return cur.fetchone()


def _l2filemeta_row(cur, rid):
    cur.execute(
        "SELECT ra0, dec0, x, y, z FROM l2filemeta WHERE rid = %s", (rid,))
    return cur.fetchone()


# ======================================================================
# Happy path
# ======================================================================

def test_register_writes_instance_exposure_l2files_l2filemeta(conn, tmp_path, monkeypatch):
    run_id, admit_outputs = _run_admit(conn, tmp_path, name="happy")
    admit_manifest = Manifest.read(admit_outputs / "manifest.json")
    entry = admit_manifest.outputs[0]

    rc, registering_attempt = _run_register(
        conn, monkeypatch, admit_outputs,
        run_id=run_id, unit_id="happy-register-unit", tmp_path=tmp_path, name="happy-reg")
    assert rc == int(ExitCode.SUCCESS)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, kind FROM product_instances WHERE id = %s", (entry.instance,))
        instance_row = cur.fetchone()
        assert instance_row == (entry.instance, "l2-image")

        cur.execute(
            "SELECT external_id, field, hp6, hp9 FROM exposures WHERE external_id = %s",
            (EXPOSURE_ID,))
        exposure_row = cur.fetchone()
        assert exposure_row is not None
        assert exposure_row[0] == EXPOSURE_ID

        expected_field = tessellation_field(
            entry.registration["ra_targ"], entry.registration["dec_targ"])
        expected_hp6, expected_hp9 = healpix_indexes(
            entry.registration["ra_targ"], entry.registration["dec_targ"])
        assert exposure_row[1] == expected_field
        assert exposure_row[2] == expected_hp6
        assert exposure_row[3] == expected_hp9

        l2files_row = _l2files_row(cur, entry.instance)
        assert l2files_row is not None
        (rid, run, attempt, instance, version, vbest, checksum, fid,
         filename, overlapfields, expid) = l2files_row
        assert run == run_id
        assert attempt == registering_attempt
        assert instance == entry.instance
        assert version == int(DELIVERED_VERSION)
        assert vbest == 0
        assert checksum == entry.registration["md5"]
        assert filename.endswith(entry.primary)
        assert expected_field in overlapfields

        cur.execute("SELECT filter FROM filters WHERE fid = %s", (fid,))
        (filter_name,) = cur.fetchone()
        assert filter_name == entry.registration["filter"]

        meta_row = _l2filemeta_row(cur, rid)
        assert meta_row is not None
        ra0, dec0, x, y, z = meta_row
        assert ra0 == pytest.approx(entry.registration["centre"]["ra"], abs=1e-6)
        assert dec0 == pytest.approx(entry.registration["centre"]["dec"], abs=1e-6)
        assert (x * x + y * y + z * z) == pytest.approx(1.0, abs=1e-9)

        # No self-dependency edge: register's own products_read names the
        # entry it just registered, but register_manifest is called with
        # admit's manifest, whose own inputs.products is {} -- so no
        # dependency row is ever inserted for this consumer from that
        # call. register's own completion manifest (produces=(), so no
        # outputs) is never itself fed to register_manifest in this PR.
        cur.execute(
            "SELECT count(*) FROM dependencies WHERE consumer_instance = %s",
            (entry.instance,))
        (dep_count,) = cur.fetchone()
        assert dep_count == 0


# ======================================================================
# Idempotence
# ======================================================================

def test_register_replay_with_fresh_attempt_adds_no_rows(conn, tmp_path, monkeypatch):
    run_id, admit_outputs = _run_admit(conn, tmp_path, name="idem")
    entry = Manifest.read(admit_outputs / "manifest.json").outputs[0]

    rc1, _ = _run_register(
        conn, monkeypatch, admit_outputs,
        run_id=run_id, unit_id="idem-register-unit-1", tmp_path=tmp_path, name="idem-reg1")
    assert rc1 == int(ExitCode.SUCCESS)

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM l2files WHERE instance = %s", (entry.instance,))
        (count_before,) = cur.fetchone()
        cur.execute("SELECT count(*) FROM l2filemeta WHERE instance = %s", (entry.instance,))
        (meta_count_before,) = cur.fetchone()
        cur.execute("SELECT count(*) FROM exposures WHERE external_id = %s", (EXPOSURE_ID,))
        (exposure_count_before,) = cur.fetchone()

    # A second, fresh registering attempt (a second unit, same run) replays
    # the identical admit manifest.
    rc2, _ = _run_register(
        conn, monkeypatch, admit_outputs,
        run_id=run_id, unit_id="idem-register-unit-2", tmp_path=tmp_path, name="idem-reg2")
    assert rc2 == int(ExitCode.SUCCESS)

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM l2files WHERE instance = %s", (entry.instance,))
        (count_after,) = cur.fetchone()
        cur.execute("SELECT count(*) FROM l2filemeta WHERE instance = %s", (entry.instance,))
        (meta_count_after,) = cur.fetchone()
        cur.execute("SELECT count(*) FROM exposures WHERE external_id = %s", (EXPOSURE_ID,))
        (exposure_count_after,) = cur.fetchone()

    assert count_after == count_before == 1
    assert meta_count_after == meta_count_before == 1
    assert exposure_count_after == exposure_count_before == 1


# ======================================================================
# Second delivery, different detector, same exposure, second run
# ======================================================================

def test_second_detector_same_exposure_second_run_creates_second_l2files_row(
        conn, tmp_path, monkeypatch):
    run_1, admit_outputs_1 = _run_admit(conn, tmp_path, name="sca7", sca_value="7")
    entry_1 = Manifest.read(admit_outputs_1 / "manifest.json").outputs[0]
    rc1, _ = _run_register(
        conn, monkeypatch, admit_outputs_1,
        run_id=run_1, unit_id="sca7-register-unit", tmp_path=tmp_path, name="sca7-reg")
    assert rc1 == int(ExitCode.SUCCESS)

    run_2, admit_outputs_2 = _run_admit(conn, tmp_path, name="sca8", sca_value="8")
    entry_2 = Manifest.read(admit_outputs_2 / "manifest.json").outputs[0]
    rc2, _ = _run_register(
        conn, monkeypatch, admit_outputs_2,
        run_id=run_2, unit_id="sca8-register-unit", tmp_path=tmp_path, name="sca8-reg")
    assert rc2 == int(ExitCode.SUCCESS)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT expid FROM l2files WHERE instance = %s", (entry_1.instance,))
        (expid_1,) = cur.fetchone()
        cur.execute(
            "SELECT expid FROM l2files WHERE instance = %s", (entry_2.instance,))
        (expid_2,) = cur.fetchone()
    assert expid_1 == expid_2  # same exposure, reused by external_id
    assert entry_1.instance != entry_2.instance
    assert run_1 != run_2


# ======================================================================
# Unknown filter
# ======================================================================

def test_unknown_filter_exits_65_and_leaves_no_rows(conn, tmp_path, monkeypatch):
    run_id, admit_outputs = _run_admit(conn, tmp_path, name="badfilter")

    # admit itself does not validate the filter against the `filters`
    # table (that lookup is register's job), so simulate an unresolvable
    # filter by mutating admit's own completed output manifest.
    admit_manifest_path = admit_outputs / "manifest.json"
    admit_manifest = json.loads(admit_manifest_path.read_text())
    admit_manifest["outputs"][0]["registration"]["filter"] = "NOTAFILTER"
    admit_manifest_path.write_text(json.dumps(admit_manifest))
    instance = admit_manifest["outputs"][0]["instance"]

    rc, _ = _run_register(
        conn, monkeypatch, admit_outputs,
        run_id=run_id, unit_id="badfilter-register-unit", tmp_path=tmp_path,
        name="badfilter-reg")
    assert rc == int(ExitCode.INPUT_REJECTED)

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM l2files WHERE instance = %s", (instance,))
        (count,) = cur.fetchone()
    assert count == 0


# ======================================================================
# Migration constraints exist by name
# ======================================================================

def test_migration_constraints_exist_by_name(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'l2files'::regclass AND conname = 'l2filespk'")
        (l2filespk_def,) = cur.fetchone()
        for column in ("expid", "sca", "version", "run"):
            assert column in l2filespk_def
        assert "NULLS NOT DISTINCT" in l2filespk_def

        cur.execute(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'l2files'::regclass AND conname = 'l2files_instance_uq'")
        assert cur.fetchone() is not None

        cur.execute(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'exposures'::regclass AND conname = 'exposures_external_id_uq'")
        assert cur.fetchone() is not None
