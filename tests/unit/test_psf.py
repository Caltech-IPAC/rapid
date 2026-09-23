"""Tests for the psf registration block (rapidpipe.products.psf) and register's refusal of a bad one."""

from __future__ import annotations

import json

import pytest

import rapidpipe.stages.register as register_module
from rapidpipe.products.psf import PsfRegistration, PsfRegistrationError, validate_psf_entry
from rapidpipe.stages.contract import ExitCode

MD5 = "9e107d9d372bb6826bd81d3542a419d6"


def psf_entry(**overrides):
    entry = {
        "kind": "psf", "format_version": "1", "instance": "01ARZ3NDEKTSV4RRFFQ69G5PSF",
        "key": {"filter": "F184", "detector": "7", "version": "1"},
        "primary": "psf/psf_f184_sca07.fits",
        "members": [{"role": "psf", "path": "psf/psf_f184_sca07.fits", "bytes": 10,
                     "sha256": "sha256:" + "2" * 64}],
        "registration": {"md5": MD5, "status": 1},
    }
    entry.update(overrides)
    return entry


def test_a_valid_entry_returns_its_block():
    assert validate_psf_entry(psf_entry()) == PsfRegistration(md5=MD5, status=1)
    assert validate_psf_entry(psf_entry(key={"filter": "F184", "detector": 18, "version": "2"}))


@pytest.mark.parametrize("overrides, message", [
    ({"key": {"filter": "F184", "detector": "7"}}, "exactly"),
    ({"key": {"filter": "F184", "detector": "19", "version": "1"}}, "detector"),
    ({"key": {"filter": "", "detector": "7", "version": "1"}}, "filter"),
    ({"registration": {"md5": MD5}}, "missing"),
    ({"registration": {"md5": MD5, "status": 1, "vbest": 1}}, "unknown"),
    ({"registration": {"md5": "x" * 32, "status": 1}}, "md5"),
    ({"registration": {"md5": MD5, "status": 2}}, "status"),
    ({"members": []}, "exactly one member"),
])
def test_invalid_entries_are_refused(overrides, message):
    with pytest.raises(PsfRegistrationError, match=message):
        validate_psf_entry(psf_entry(**overrides))


def test_register_refuses_a_bad_psf_block_before_connecting(tmp_path, monkeypatch):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "manifest.json").write_text(json.dumps({
        "schema_version": "1", "run": "r1", "unit": {"kind": "detector-image", "id": "e1/SCA07"},
        "stage": "admit", "attempt": "a1", "execution_record": "exec/a1.json",
        "inputs": {"manifest": "m", "products": {}, "result_sets": []},
        "outputs": [psf_entry(registration={"md5": MD5})],
    }))
    monkeypatch.setattr(register_module, "connect",
                        lambda *a, **k: pytest.fail("connected before validating"))
    rc = register_module.main(["--run", "r1", "--unit", "e1/SCA07", "--attempt", "r-a1",
                               "--inputs", str(inputs), "--outputs", str(tmp_path / "out")])
    assert rc == ExitCode.INPUT_REJECTED
