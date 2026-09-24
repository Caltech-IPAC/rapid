"""Tests for rapidpipe.stages.register that need no database.

Database-backed behaviour (actual writes to l2files/l2filemeta,
idempotence, constraint checks) is in tests/db/test_register_l2.py,
which runs against a real PostgreSQL. This file exercises only what does
not need a connection: --dry-run's no-connect guarantee, and an unknown
output kind's rejection.
"""

from __future__ import annotations

import json

import pytest

import rapidpipe.stages.register as register_module
from rapidpipe.stages.contract import ExitCode
from rapidpipe.stages.register import DECLARATION, main


def _write_manifest(inputs_dir, outputs):
    inputs_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": "1",
        "run": "r1",
        "unit": {"kind": "detector-image", "id": "e1/SCA07"},
        "stage": "admit",
        "attempt": "admit-attempt-1",
        "execution_record": "exec/admit-attempt-1.json",
        "inputs": {"manifest": "s3://bucket/root/manifest.json", "products": {}, "result_sets": []},
        "outputs": outputs,
    }
    (inputs_dir / "manifest.json").write_text(json.dumps(manifest))


def _l2_image_entry(instance="01ARZ3NDEKTSV4RRFFQ69G5FAV"):
    return {
        "kind": "l2-image",
        "format_version": "1",
        "instance": instance,
        "key": {"exposure": "e1", "detector": "7", "version": "1"},
        "primary": "l2/delivered.fits",
        "members": [
            {"role": "image", "path": "l2/delivered.fits", "bytes": 100,
             "sha256": "sha256:" + "0" * 64},
        ],
        "registration": {"source": "irrelevant-to-this-test"},
    }


def _argv(inputs_dir, outputs_dir, *, extra=()):
    return [
        "--run", "r1", "--unit", "e1/SCA07", "--attempt", "register-attempt-1",
        "--inputs", str(inputs_dir), "--outputs", str(outputs_dir),
        *extra,
    ]


def test_declaration_validates():
    DECLARATION.validate()
    assert DECLARATION.name == "register"
    assert DECLARATION.consumes == ("l2-image", "psf", "difference-image", "source-catalog",
                                    "alert-container", "alert-set")
    assert DECLARATION.produces == ()
    assert DECLARATION.database_access == "read-write"


def test_dry_run_exits_zero_without_connecting(tmp_path, monkeypatch):
    inputs_dir = tmp_path / "inputs"
    _write_manifest(inputs_dir, [_l2_image_entry()])
    outputs_dir = tmp_path / "outputs"

    def _raise_if_called(*args, **kwargs):
        raise AssertionError("register must not connect to the database on --dry-run")

    monkeypatch.setattr(register_module, "connect", _raise_if_called)

    rc = main(_argv(inputs_dir, outputs_dir, extra=["--dry-run"]))
    assert rc == int(ExitCode.SUCCESS)


def test_unknown_output_kind_exits_65_without_connecting(tmp_path, monkeypatch):
    inputs_dir = tmp_path / "inputs"
    _write_manifest(inputs_dir, [
        {
            "kind": "reference-image",
            "format_version": "1",
            "instance": "01ARZ3NDEKTSV4RRFFQ69G5FAX",
            "key": {"unit": "e1/SCA07"},
            "primary": "ref/foo.fits",
            "members": [
                {"role": "reference", "path": "ref/foo.fits", "bytes": 10,
                 "sha256": "sha256:" + "1" * 64},
            ],
            "registration": {},
        },
    ])
    outputs_dir = tmp_path / "outputs"

    def _raise_if_called(*args, **kwargs):
        raise AssertionError(
            "register must reject an unknown output kind before connecting")

    monkeypatch.setattr(register_module, "connect", _raise_if_called)

    rc = main(_argv(inputs_dir, outputs_dir))
    assert rc == int(ExitCode.INPUT_REJECTED)


# ----------------------------------------------------------------------
# The difference stage's manifest
# ----------------------------------------------------------------------


def _difference_manifest(tmp_path, monkeypatch):
    """Run the difference stage with fake tools; return its outputs dir."""
    import rapidpipe.stages.difference as difference

    from .fakedifftools import (
        CDF_DIR, FakePsfCatalog, FakeToolRunner, build_input_set, fake_sip_to_pv)

    monkeypatch.setattr(difference, "toolkit", lambda: difference.Toolkit(
        runner=FakeToolRunner(), sip_to_pv=fake_sip_to_pv, psf_catalog=FakePsfCatalog()))
    build_input_set(tmp_path / "diff-inputs")
    overlay = tmp_path / "overlay.toml"
    overlay.write_text(f'[paths]\ncfg_path = "{CDF_DIR}"\n')
    outputs = tmp_path / "diff-outputs"
    assert difference.main([
        "--run", "r1", "--unit", "e1/SCA07", "--attempt", "diff-attempt-1",
        "--inputs", str(tmp_path / "diff-inputs"), "--outputs", str(outputs),
        "--settings", str(overlay)]) == int(ExitCode.SUCCESS)
    return outputs


class _FakeConn:
    def __init__(self):
        self.committed = False

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def commit(self):
        self.committed = True

    def rollback(self):
        pass


def test_difference_manifest_registers_each_difference_image(tmp_path, monkeypatch):
    diff_outputs = _difference_manifest(tmp_path, monkeypatch)
    conn = _FakeConn()
    calls = {"manifest": [], "difference": []}
    monkeypatch.setattr(register_module, "connect", lambda *a, **k: conn)
    monkeypatch.setattr(register_module, "register_manifest",
                        lambda c, m, registering_attempt_id: calls["manifest"].append(m))
    monkeypatch.setattr(register_module, "register_difference_image",
                        lambda c, **kw: calls["difference"].append(kw))
    monkeypatch.setattr(register_module, "register_l2_image",
                        lambda *a, **k: pytest.fail("no l2-image entry in this manifest"))

    rc = main(_argv(diff_outputs, tmp_path / "register-outputs"))
    assert rc == int(ExitCode.SUCCESS)
    assert conn.committed
    assert len(calls["manifest"]) == 1
    kinds = [o["kind"] for o in calls["manifest"][0]["outputs"]]
    assert kinds.count("difference-image") == 1 and kinds.count("source-catalog") == 4
    (call,) = calls["difference"]
    assert call["entry"]["key"]["differencer"] == "zogy"
    assert call["output_location"] == str(diff_outputs)
    assert call["run_id"] == "r1"


def _rewrite(outputs_dir, edit):
    path = outputs_dir / "manifest.json"
    manifest = json.loads(path.read_text())
    edit(manifest)
    path.write_text(json.dumps(manifest))


@pytest.mark.parametrize("edit", [
    # A present ZOGY instance missing a declared role.
    lambda m: m["outputs"][0].update(
        members=[x for x in m["outputs"][0]["members"] if x["role"] != "significance"]),
    lambda m: m["outputs"][0]["registration"].update(catalog_outcome_bits=99),
    lambda m: m["outputs"][1]["registration"].update(source_count=-2),
    lambda m: m["outputs"][1]["key"].update(sign="both"),
])
def test_bad_difference_manifest_exits_65_without_connecting(tmp_path, monkeypatch, edit):
    diff_outputs = _difference_manifest(tmp_path, monkeypatch)
    _rewrite(diff_outputs, edit)

    def _raise_if_called(*args, **kwargs):
        raise AssertionError("register must refuse a malformed manifest before connecting")

    monkeypatch.setattr(register_module, "connect", _raise_if_called)
    assert main(_argv(diff_outputs, tmp_path / "register-outputs")) == int(ExitCode.INPUT_REJECTED)
