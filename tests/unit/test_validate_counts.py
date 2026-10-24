"""scripts/validate_run.py's counts check scoped to a loop's SCAs.

Contract: with ``--scas``, a pending job whose scope names another SCA (its
``sca``, or its SOC stem's ``wfiNN``) is a second loop's and not counted; a
job naming no SCA (a field's) is always counted; without ``--scas`` every
pending job counts.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
_SPEC = importlib.util.spec_from_file_location("validate_run", SCRIPTS / "validate_run.py")
validate_run = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(validate_run)

PENDING = [{"stage": "psf", "state": "launched", "scope": {"file": "r0034201001001004001_0001_wfi04_f146"}},
           {"stage": "difference", "state": "running", "scope": {"expid": 4040101, "sca": 8, "fid": 8}},
           {"stage": "reference", "state": "running", "scope": {"field": 7, "fid": 8, "sca": 5}},
           {"stage": "associate", "state": "launched", "scope": {"field": 7}}]


def test_other_scas_pending_jobs_are_not_counted_and_a_fields_always_is():
    mine, others = validate_run.pending_outside(PENDING, {8})
    assert [j["stage"] for j in mine] == ["difference", "associate"]
    assert [j["stage"] for j in others] == ["psf", "reference"]


def test_without_scas_every_pending_job_counts():
    assert validate_run.pending_outside(PENDING, None) == (PENDING, [])
