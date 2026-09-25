"""``run local``'s input binding (supervisor step 9, 2026-09-25, R4)
against a real PostgreSQL: the manifest at ``--inputs`` is read before
anything is written, its registered instances are bound in
``unit_inputs`` with the unit, and a missing manifest refuses with
nothing written. The stage subprocess is a script that exits 75
(transient, so the unit can be retried) and runs no stage, since what is
under test is what happens before it.
"""

from __future__ import annotations

import json

import pytest

from rapidpipe.db.ids import new_ulid
from rapidpipe.runs import repository as repo
from rapidpipe.runs.inputs import InputsRefused
from rapidpipe.runs.local import run_stage_locally
from tests.db.test_repository import _full_chain_to_current_candidate, _make_run

pytestmark = pytest.mark.real_input_manifest



def _transient_interpreter(tmp_path):
    script = tmp_path / "exit75"
    script.write_text("#!/bin/sh\nexit 75\n")
    script.chmod(0o755)
    return str(script)


def _write_manifest(path, instances, result_sets=()):
    path.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": "1", "run": "R", "unit": {"kind": "detector-image", "id": "U"},
        "stage": "input-set", "attempt": new_ulid(), "execution_record": "exec/x.json",
        "inputs": {"manifest": "x", "products": {}, "result_sets": list(result_sets)},
        "outputs": [
            {"kind": "test-product", "format_version": "1", "instance": i,
             "key": {"unit": "U"}, "primary": f"l2/{i}.fits",
             "members": [{"role": "image", "path": f"l2/{i}.fits", "bytes": 1,
                          "sha256": "sha256:" + "0" * 64}]}
            for i in instances],
    }
    (path / "manifest.json").write_text(json.dumps(manifest))
    return str(path)


def _bound(conn, run_id, unit_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ui.producer_instance FROM unit_inputs ui JOIN units u ON u.id = ui.unit "
            "WHERE u.run = %s AND u.unit_id = %s ORDER BY 1", (run_id, unit_id))
        return [row[0] for row in cur.fetchall()]


class _NoCommit:
    """The test's own connection with commit() absorbed, so everything
    run_stage_locally writes stays in the fixture's rolled-back
    transaction (the stage subprocess runs nothing and writes nothing)."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def commit(self):
        pass


def _run_local(conn, run_id, inputs, tmp_path):
    return run_stage_locally(
        _NoCommit(conn), run_id=run_id, stage="difference", unit_kind="detector-image",
        unit_id="U", inputs=inputs, outputs_root=str(tmp_path / "out"),
        python=_transient_interpreter(tmp_path))


def test_run_local_binds_registered_inputs_before_the_attempt(conn, tmp_path):
    producer = _make_run(conn, kind="scratch")
    _, _, _, instance = _full_chain_to_current_candidate(
        conn, producer, logical_key={"unit": "e001/SCA01", "v": "bind-local"})
    consumer = _make_run(conn, kind="scratch", max_attempts=2)
    inputs = _write_manifest(tmp_path / "in", [instance, new_ulid()], [new_ulid()])

    _run_local(conn, consumer, inputs, tmp_path)
    assert _bound(conn, consumer, "U") == [instance]

    # A retry rebinds nothing new.
    _run_local(conn, consumer, inputs, tmp_path)
    assert _bound(conn, consumer, "U") == [instance]

    with pytest.raises(repo.DeletionRefused, match="unit_inputs"):
        repo.mark_run_deleting(conn, producer, requested_by="brusholme")


def test_run_local_refuses_a_missing_manifest_with_nothing_written(conn, tmp_path):
    run_id = _make_run(conn, kind="scratch")
    with pytest.raises(InputsRefused, match="input manifest"):
        _run_local(conn, run_id, str(tmp_path / "nothing-here"), tmp_path)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM units WHERE run = %s", (run_id,))
        assert cur.fetchone() == (0,)
