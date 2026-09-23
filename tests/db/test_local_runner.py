"""Database-backed, end-to-end tests for rapidpipe.runs.local against a
real PostgreSQL with the run-model and l2-run-columns migrations applied.

Runs admit and register as real subprocess-free calls into their own
`main`, but through `run_stage_locally` -- so the local runner's own
plumbing (add_unit, allocate_attempt, the output-location layout,
record_attempt_result, select_attempt) is what is under test, not
admit/register's science. Following test_register_l2.py's pattern: admit
runs in-process (so this test's own tmp_path fixtures reach it), and its
manifest names a real run/attempt that must exist in the database for
register_manifest's FK lookups to succeed.

Since `run_stage_locally` launches the stage as an actual subprocess
(python -m rapidpipe.stages.<name>), unlike test_register_l2.py's
in-process `admit_main`/`register_main` calls, the child process commits
for real against the test database's PG* environment (inherited from
this process's os.environ, which the `conn` fixture also reads) -- its
writes are NOT rolled back by this test's own transaction, since they
happen in a different connection, in a different process. Tests that run
the real subprocess therefore clean up what it committed themselves, in a
`finally`, on a fresh autocommit connection.

Skips cleanly if PGHOST is unset (see conftest.py).
"""

from __future__ import annotations

import json
import os

import pytest

import rapidpipe.cli.main as cli_main
from rapidpipe.db.ids import new_ulid
from rapidpipe.products.manifest import Manifest
from rapidpipe.products.manifest import register_unit_id as derive_register_unit_id
from rapidpipe.runs import repository as repo
from rapidpipe.runs.local import run_stage_locally
from tests.unit.test_admit import _build_delivery

from .test_repository import _make_run, _make_unit


class _NoCloseNoCommitConnProxy:
    """Hands the CLI the test's own `conn`, but absorbs commit()/close().

    Same pattern as tests/db/test_register_l2.py's proxy of the same name:
    this test's isolation relies on its whole body running inside one
    already-open, never-committed outer transaction that conftest.py rolls
    back at teardown, so a `run show`/`run list` call through `main` must
    never actually commit or close the connection it is handed.
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


def _cleanup_l2_rows(instance_id: str) -> None:
    """Delete everything the register subprocess committed for one
    instance id, on a fresh autocommit connection -- this test's own
    `conn` fixture is rolled back at teardown, but the subprocess's
    commit happened on a different connection entirely, so nothing here
    is cleaned up by that rollback."""
    import psycopg2

    conn = psycopg2.connect(
        host=os.environ["PGHOST"],
        port=os.environ.get("PGPORT", "5432"),
        dbname=os.environ["PGDATABASE"],
        user=os.environ["PGUSER"],
        password=os.environ.get("PGPASSWORD", ""),
    )
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM l2filemeta WHERE rid IN "
                        "(SELECT rid FROM l2files WHERE instance = %s)", (instance_id,))
            cur.execute("DELETE FROM l2files WHERE instance = %s", (instance_id,))
            cur.execute("DELETE FROM dependencies WHERE consumer_instance = %s "
                        "OR producer_instance = %s", (instance_id, instance_id))
            cur.execute("DELETE FROM product_members WHERE instance = %s", (instance_id,))
            cur.execute("DELETE FROM product_instances WHERE id = %s", (instance_id,))
    finally:
        conn.close()


def _cleanup_exposure(exposure_external_id: str) -> None:
    import psycopg2

    conn = psycopg2.connect(
        host=os.environ["PGHOST"],
        port=os.environ.get("PGPORT", "5432"),
        dbname=os.environ["PGDATABASE"],
        user=os.environ["PGUSER"],
        password=os.environ.get("PGPASSWORD", ""),
    )
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            # Only remove the exposure if nothing else still references it.
            cur.execute(
                "DELETE FROM exposures WHERE external_id = %s "
                "AND NOT EXISTS (SELECT 1 FROM l2files WHERE expid = exposures.expid)",
                (exposure_external_id,))
    finally:
        conn.close()


# ======================================================================
# End to end: admit then register, both through the local runner
# ======================================================================

def test_admit_then_register_end_to_end(conn, tmp_path):
    run_id = _make_run(conn, kind="scratch", max_attempts=1)
    conn.commit()

    inputs_dir, _ = _build_delivery(tmp_path)
    outputs_root = tmp_path / "outputs"

    admit_unit_id = "e2local001/SCA07"
    admit_result = run_stage_locally(
        conn,
        run_id=run_id,
        stage="admit",
        unit_kind="detector-image",
        unit_id=admit_unit_id,
        inputs=str(inputs_dir),
        outputs_root=str(outputs_root),
    )
    conn.commit()

    instance_id = None
    try:
        assert admit_result.disposition == "succeeded"
        assert admit_result.selected is True
        assert admit_result.manifest_path is not None
        assert admit_result.manifest_path.exists()

        with conn.cursor() as cur:
            cur.execute(
                "SELECT state, selected_attempt FROM units "
                "WHERE run = %s AND stage = 'admit' AND unit_id = %s",
                (run_id, admit_unit_id))
            state, selected_attempt = cur.fetchone()
            assert state == "complete"
            assert selected_attempt == admit_result.attempt_id

            cur.execute(
                "SELECT disposition, exit_code FROM attempts WHERE id = %s",
                (admit_result.attempt_id,))
            disposition, exit_code = cur.fetchone()
            assert disposition == "succeeded"
            assert exit_code == 0

        admit_manifest = Manifest.read(admit_result.manifest_path)
        entry = admit_manifest.outputs[0]
        instance_id = entry.instance

        # register's unit id is derived from the producer manifest it
        # reads, never hand-picked (Ben, 2026-09-23 ruling): the CLI does
        # this derivation itself (rapidpipe.cli.main._resolve_register_
        # unit_id) before calling run_stage_locally, exercised here
        # directly since this test drives run_stage_locally, not the CLI.
        register_unit = derive_register_unit_id(admit_manifest)
        assert register_unit == f"admit/{admit_unit_id}"
        register_result = run_stage_locally(
            conn,
            run_id=run_id,
            stage="register",
            unit_kind="detector-image",
            unit_id=register_unit,
            inputs=admit_result.output_location,
            outputs_root=str(outputs_root),
        )
        conn.commit()

        assert register_result.disposition == "succeeded"
        assert register_result.selected is True

        with conn.cursor() as cur:
            cur.execute(
                "SELECT attempt FROM l2files WHERE instance = %s", (instance_id,))
            row = cur.fetchone()
            assert row is not None
            assert row[0] == register_result.attempt_id

            cur.execute(
                "SELECT state, selected_attempt FROM units "
                "WHERE run = %s AND stage = 'register' AND unit_id = %s",
                (run_id, register_unit))
            state, selected_attempt = cur.fetchone()
            assert state == "complete"
            assert selected_attempt == register_result.attempt_id
    finally:
        if instance_id is not None:
            _cleanup_l2_rows(instance_id)
        from tests.unit.test_admit import EXPOSURE_ID
        _cleanup_exposure(EXPOSURE_ID)


# ======================================================================
# Failure: bad sha256 -> InputRejected -> failed, not selected, no manifest
# ======================================================================

def test_admit_bad_sha256_fails_and_exhausts_allowance(conn, tmp_path):
    run_id = _make_run(conn, kind="scratch", max_attempts=1)
    conn.commit()

    inputs_dir, _ = _build_delivery(tmp_path)
    manifest_path = inputs_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["outputs"][0]["members"][0]["sha256"] = "sha256:" + "0" * 64
    manifest_path.write_text(json.dumps(manifest))

    outputs_root = tmp_path / "outputs"
    unit_id = "badsha001/SCA07"

    result = run_stage_locally(
        conn,
        run_id=run_id,
        stage="admit",
        unit_kind="detector-image",
        unit_id=unit_id,
        inputs=str(inputs_dir),
        outputs_root=str(outputs_root),
    )
    conn.commit()

    assert result.exit_code == 65
    assert result.disposition == "failed"
    assert result.selected is False
    assert result.manifest_path is None

    with conn.cursor() as cur:
        cur.execute(
            "SELECT state FROM units WHERE run = %s AND stage = 'admit' AND unit_id = %s",
            (run_id, unit_id))
        (state,) = cur.fetchone()
    assert state == "failed"

    # max_attempts_per_unit=1 and one attempt already spent: a second
    # attempt on the same unit is refused by the allowance, not attempted.
    with pytest.raises(repo.UnitTerminal):
        run_stage_locally(
            conn,
            run_id=run_id,
            stage="admit",
            unit_kind="detector-image",
            unit_id=unit_id,
            inputs=str(inputs_dir),
            outputs_root=str(outputs_root),
        )
    conn.rollback()


# ======================================================================
# run show, through rapidpipe.cli.main.main
# ======================================================================

def test_run_show_prints_run_units_and_attempts(conn, monkeypatch, capsys):
    monkeypatch.setattr(
        cli_main, "connect",
        lambda *a, **k: _NoCloseNoCommitConnProxy(conn))

    run_id = _make_run(conn, kind="scratch", purpose="cli show test")
    stage, unit_id = _make_unit(conn, run_id, stage="difference", unit_id="e001/SCA01")
    attempt_id = repo.allocate_attempt(conn, run_id, stage, unit_id)
    repo.record_attempt_result(
        conn, attempt_id, exit_code=0, disposition="succeeded",
        output_location=f"runs/{run_id}/{stage}/{unit_id}/{attempt_id}",
        execution_record={
            "source_revision": "abc123",
            "schema_version": "1",
            "settings_hash": "sha256:xyz",
        },
        scheduler_job_id=None)
    repo.select_attempt(conn, attempt_id)

    rc = cli_main.main(["run", "show", run_id])
    captured = capsys.readouterr()

    assert rc == 0
    assert f"id: {run_id}" in captured.out
    assert "cli show test" in captured.out
    assert "units:" in captured.out
    assert f"  {stage}\t{unit_id}\tcomplete\t{attempt_id}" in captured.out
    assert "attempts:" in captured.out
    assert attempt_id in captured.out
    assert "succeeded" in captured.out


def test_run_show_unknown_run_exits_1(conn, monkeypatch, capsys):
    monkeypatch.setattr(
        cli_main, "connect",
        lambda *a, **k: _NoCloseNoCommitConnProxy(conn))

    rc = cli_main.main(["run", "show", new_ulid()])
    captured = capsys.readouterr()
    assert rc == 1
    assert "no such run" in captured.err
