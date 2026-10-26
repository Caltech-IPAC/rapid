"""scripts/compare_runs.py's epoch intersection and null columns, on faked rows.

Contracts: only epochs both runs have are compared, an epoch in one run only
is returned as unshared; one epoch's columns are its difference job's
metrics, its packet count and the registration offset's hypotenuse, a metric
the job did not record a dash, never zero.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "compare_runs", Path(__file__).resolve().parents[2] / "scripts" / "compare_runs.py")
compare_runs = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(compare_runs)


def test_only_shared_epochs_are_compared_and_the_rest_listed_unshared():
    a = {(4040101, 8): 11, (5040101, 8): 12, (6010101, 8): 13}
    b = {(5040101, 8): 21, (4040101, 8): 22, (4040101, 2): 23}
    both, only_a, only_b = compare_runs.shared(a, b)
    assert both == [(4040101, 8), (5040101, 8)]
    assert only_a == [(6010101, 8)]
    assert only_b == [(4040101, 2)]


def test_one_epochs_columns_come_from_its_metrics_and_packets():
    values = {"n_candidates": 777.0, "n_sources": 500.0, "n_sources_snr_gt20": 20.0,
              "core_fraction": -0.00105, "ring_fraction": 0.00172, "registration_dx_px": 0.003,
              "registration_dy_px": 0.004, "container_seconds": 249.3, "packets": 182}
    row = compare_runs.null_row(values)
    assert row[:6] == [777.0, 500.0, 20.0, 182, -0.00105, 0.00172] and row[7] == 249.3
    assert row[6] == pytest.approx(0.005)   # hypot(dx, dy), the gate's registration offset
    sparse = compare_runs.null_row({"n_candidates": 271.0, "packets": 0})
    assert sparse[0] == 271.0 and sparse[3] == 0 and sparse[6] is None
    table = compare_runs.null_table({113: {(4040101, 8): sparse}}, [(4040101, 8)])
    assert "| 4040101 sca 8 | 113 | 271 | — | — | 0 | — | — | — | — |" in table.splitlines()
