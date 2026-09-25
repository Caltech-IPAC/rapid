"""``rapidpipe.runs.inputs`` and its wiring into ``submit_unit`` and
``run_stage_locally`` (supervisor step 9, 2026-09-25, R4), with no
database: FakeS3 for the manifest, a fake connection for the
``product_instances`` lookup. Uses the real manifest read
(``real_input_manifest``; see tests/conftest.py).
"""

from __future__ import annotations

import json
import logging

import pytest

from rapidpipe.db.ids import new_ulid
from rapidpipe.launch import batch as launch_batch
from rapidpipe.runs import inputs as run_inputs
from rapidpipe.runs import local as local_module
from rapidpipe.runs.inputs import InputsRefused
from tests.unit.fakebatch import FakeBatch
from tests.unit.fakes3 import FakeClientError, FakeEndpointConnectionError, FakeS3

pytestmark = pytest.mark.real_input_manifest


def _manifest(instances=(), result_sets=()):
    return {
        "schema_version": "1", "run": "R", "unit": {"kind": "detector-image", "id": "U"},
        "stage": "input-set", "attempt": new_ulid(), "execution_record": "exec/x.json",
        "inputs": {"manifest": "x", "products": {"reference-image": "NOT-BOUND"},
                   "result_sets": list(result_sets)},
        "outputs": [
            {"kind": "test-product", "format_version": "1", "instance": i,
             "key": {"unit": "U"}, "primary": f"l2/{i}.fits",
             "members": [{"role": "image", "path": f"l2/{i}.fits", "bytes": 1,
                          "sha256": "sha256:" + "0" * 64}]}
            for i in instances],
    }


def _s3_with(manifest_bytes, key="set/manifest.json"):
    fake = FakeS3()
    fake.seed("b", key, manifest_bytes)
    return fake


# ----------------------------------------------------------------------
# read_input_instances
# ----------------------------------------------------------------------

def test_names_are_the_output_instances_and_result_sets_deduplicated():
    fake = _s3_with(json.dumps(_manifest(["I1", "I2"], ["S1", "I1"])).encode())
    assert run_inputs.read_input_instances("s3://b/set", s3_client=fake) == ["I1", "I2", "S1"]


def test_a_local_manifest_is_read(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps(_manifest(["I1"])))
    assert run_inputs.read_input_instances(str(tmp_path)) == ["I1"]


def test_a_missing_s3_manifest_is_refused():
    with pytest.raises(InputsRefused, match="s3://b/nothing/manifest.json"):
        run_inputs.read_input_instances("s3://b/nothing", s3_client=FakeS3())


def test_a_missing_local_manifest_is_refused(tmp_path):
    with pytest.raises(InputsRefused, match="not found"):
        run_inputs.read_input_instances(str(tmp_path / "absent"))


@pytest.mark.parametrize("body", [b"{not json", b'{"run": "R"}'])
def test_an_invalid_manifest_is_refused(body):
    with pytest.raises(InputsRefused, match="not a valid manifest"):
        run_inputs.read_input_instances("s3://b/set", s3_client=_s3_with(body))


def test_access_denied_is_refused_and_a_network_failure_propagates():
    class _Raising(FakeS3):
        def __init__(self, exc):
            super().__init__()
            self.exc = exc

        def download_file(self, *a, **k):
            raise self.exc

    with pytest.raises(InputsRefused, match="AccessDenied"):
        run_inputs.read_input_instances(
            "s3://b/set", s3_client=_Raising(FakeClientError("AccessDenied", "denied")))
    with pytest.raises(FakeEndpointConnectionError):
        run_inputs.read_input_instances(
            "s3://b/set", s3_client=_Raising(FakeEndpointConnectionError("down")))


def test_inputs_refused_carries_exit_65():
    assert InputsRefused.exit_code == 65


# ----------------------------------------------------------------------
# bind_registered_inputs
# ----------------------------------------------------------------------

class _LookupConn:
    """Answers the product_instances lookup; counts cursors opened."""

    def __init__(self, registered):
        self.registered = set(registered)
        self.cursors = 0

    def cursor(self):
        conn = self

        class _Cur:
            def __enter__(self):
                conn.cursors += 1
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params):
                assert "FROM product_instances WHERE id = ANY" in sql
                self.rows = [(i,) for i in params[0] if i in conn.registered]

            def fetchall(self):
                return self.rows

        return _Cur()


def test_only_registered_instances_bind_and_the_rest_are_logged(monkeypatch, caplog):
    bound_calls = []
    monkeypatch.setattr(run_inputs, "bind_unit_inputs",
                        lambda conn, run, stage, unit, ids: bound_calls.append(list(ids)))
    conn = _LookupConn({"I1", "S1"})
    with caplog.at_level(logging.INFO, logger="rapidpipe.runs.inputs"):
        bound, skipped = run_inputs.bind_registered_inputs(
            conn, "R", "difference", "U", ["I1", "DELIVERY", "S1", "I1"])
    assert (bound, skipped) == (["I1", "S1"], ["DELIVERY"])
    assert bound_calls == [["I1", "S1"]]
    assert "DELIVERY" in caplog.text


def test_nothing_registered_binds_nothing_and_touches_no_row(monkeypatch):
    monkeypatch.setattr(run_inputs, "bind_unit_inputs",
                        lambda *a, **k: pytest.fail("bind_unit_inputs called"))
    conn = _LookupConn(set())
    assert run_inputs.bind_registered_inputs(conn, "R", "admit", "U", ["X"]) == ([], ["X"])
    assert run_inputs.bind_registered_inputs(conn, "R", "admit", "U", []) == ([], [])
    assert conn.cursors == 1  # the one lookup for ["X"]; none for []


# ----------------------------------------------------------------------
# submit_unit / run_stage_locally wiring
# ----------------------------------------------------------------------

class _Conn:
    def commit(self):
        pass

    def rollback(self):
        pass


def _patch_submit(monkeypatch, order):
    monkeypatch.setenv("RAPIDPIPE_BATCH_JOB_QUEUE", "q")
    monkeypatch.setattr(launch_batch, "_release_job_definition", lambda conn, run_id: None)
    monkeypatch.setattr(launch_batch, "add_unit", lambda *a: order.append("add_unit"))
    monkeypatch.setattr(run_inputs, "bind_registered_inputs",
                        lambda conn, run, stage, unit, names: order.append(("bind", list(names))))
    monkeypatch.setattr(launch_batch, "allocate_attempt",
                        lambda *a, **k: order.append("allocate") or "A1")
    monkeypatch.setattr(launch_batch, "record_attempt_locations", lambda *a: None)
    monkeypatch.setattr(launch_batch, "record_scheduler_job", lambda *a, **k: None)


def test_submit_unit_binds_after_add_unit_and_before_allocation(monkeypatch):
    order: list = []
    _patch_submit(monkeypatch, order)
    fake = _s3_with(json.dumps(_manifest(["I1"], ["S1"])).encode())
    launch_batch.submit_unit(
        _Conn(), run_id="R", stage="difference", unit_kind="detector-image", unit_id="U",
        inputs_location="s3://b/set", outputs_root="s3://o/root", job_definition="d",
        client=FakeBatch(), s3_client=fake)
    assert order == ["add_unit", ("bind", ["I1", "S1"]), "allocate"]


def test_submit_unit_refuses_a_missing_manifest_before_writing_anything(monkeypatch):
    order: list = []
    _patch_submit(monkeypatch, order)
    batch = FakeBatch()
    with pytest.raises(InputsRefused):
        launch_batch.submit_unit(
            _Conn(), run_id="R", stage="difference", unit_kind="detector-image",
            unit_id="U", inputs_location="s3://b/absent", outputs_root="s3://o/root",
            job_definition="d", client=batch, s3_client=FakeS3())
    assert order == []
    assert batch.submitted == []


def test_run_stage_locally_refuses_a_missing_manifest_before_writing_anything(
        monkeypatch, tmp_path):
    monkeypatch.setattr(local_module, "add_unit", lambda *a: pytest.fail("add_unit called"))
    with pytest.raises(InputsRefused):
        local_module.run_stage_locally(
            _Conn(), run_id="R", stage="difference", unit_kind="detector-image",
            unit_id="U", inputs=str(tmp_path / "absent"), outputs_root=str(tmp_path / "o"))
