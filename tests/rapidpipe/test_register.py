"""Tests for rapidpipe.stages.register that need no database.

Database-backed behaviour (actual writes to l2files/l2filemeta,
idempotence, constraint checks) is in tests/rapidpipe_db/test_register_l2.py,
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
    assert DECLARATION.consumes == ("l2-image",)
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
