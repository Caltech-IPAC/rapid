"""scripts/validate_run.py's counts and tables checks: what makes a run not clean.

Contracts: a run with a failed job, or with no jobs, fails ``counts``; a run
with no published job fails ``tables``; with ``--scas``, a pending job whose scope names another SCA (its
``sca``, or its SOC stem's ``wfiNN``) is a second loop's and not counted; a
job naming no SCA (a field's) is always counted; without ``--scas`` every
pending job counts.
"""

from __future__ import annotations

import importlib.util
from collections import Counter
import sys
from pathlib import Path

import pytest

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


def fake_q(jobs: list[dict]):
    """``q`` answering the counts and tables checks' reads from ``jobs`` (``stage``, ``state``, ``scope``)."""
    def q(sql, params=()):
        if "GROUP BY 1, 2" in sql:
            counts = Counter((j["stage"], j["state"]) for j in jobs)
            return [{"stage": st, "state": s, "n": n} for (st, s), n in counts.items()]
        if "state IN (" in sql:
            return [j for j in jobs if j["state"] in ("launched", "running", "failed")]
        if "state = 'published'" in sql:
            return []                                    # no manifests to read: the job count is the contract
        return [{"n": 0}]
    return q


DONE = {"stage": "psf", "state": "published", "scope": {"sca": 8}}


@pytest.mark.parametrize("jobs, clean", [([DONE], True), ([DONE, {**DONE, "state": "failed"}], False), ([], False)],
                         ids=["published", "failed", "no-jobs"])
def test_counts_fails_a_run_with_a_failed_job_or_no_jobs(monkeypatch, jobs, clean):
    monkeypatch.setattr(validate_run, "q", fake_q(jobs))
    assert validate_run.check_counts(1) is clean


def test_tables_fails_a_run_with_no_published_job(monkeypatch, capsys):
    monkeypatch.setattr(validate_run, "q", fake_q([]))
    assert validate_run.check_tables(1) is False
    assert "no published jobs" in capsys.readouterr().out
