"""scripts/validate_run.py's forced check under packet sets in flight at once.

Contract: a set's row of a pair its recorded predecessors also hold passes
when it equals one predecessor's measurement (two sets in flight each
measured it, and the later set copied one), and fails when it equals none
(measured again).
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


class Table:
    def __init__(self, rows):
        self.rows = rows

    def to_pylist(self):
        return self.rows


def row(time_proc):
    r = {f: 1.0 for f in validate_run.FORCED_COMPARED}
    return {**r, "aid": 7, "rid": 3, "time_proc": time_proc}


def run_check(monkeypatch, later_time):
    sets = {1: [row(10.0)], 2: [row(11.0)], 3: [row(later_time)]}
    monkeypatch.setattr(validate_run, "run_jobs", lambda run, stage: [1, 2, 3])
    monkeypatch.setattr(validate_run, "read_output", lambda job, table: Table(sets[job]))
    monkeypatch.setattr(validate_run, "q", lambda sql, params: [{"source_job": 1}, {"source_job": 2}]
                        if params == (3,) else [])
    return validate_run.check_forced(0)


def test_a_copy_of_either_predecessor_passes(monkeypatch):
    assert run_check(monkeypatch, 11.0)
    assert run_check(monkeypatch, 10.0)


def test_a_third_measurement_fails(monkeypatch):
    assert not run_check(monkeypatch, 12.0)
