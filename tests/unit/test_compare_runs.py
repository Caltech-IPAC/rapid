"""scripts/compare_runs.py's epoch intersection and null columns, on faked rows.

Contracts: two runs' epochs are paired by exposure name and SCA, each run
keeping its own id for the epoch, since two datasets give one exposure two
ids; only epochs both runs have are compared, an epoch in one run only
is returned as unshared; one epoch's columns are its difference job's
metrics, its packet count and the registration offset's hypotenuse, a metric
the job did not record a dash, never zero; the drift gate calls a value outside
its spread, or absent, red.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "compare_runs", Path(__file__).resolve().parents[2] / "scripts" / "compare_runs.py")
compare_runs = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(compare_runs)


P4, P5, P6 = "r0034201004001004001_0001", "r0034201005001004001_0001", "r0034201006001004001_0001"


def test_two_runs_epochs_pair_by_name_and_sca_each_with_its_own_id():
    jobs_a, ids_a = compare_runs.by_name([(P4, 8, 11, 4001), (P5, 8, 12, 4002), (P6, 8, 13, 4003)])
    jobs_b, ids_b = compare_runs.by_name([(P5, 8, 21, 9002), (P4, 8, 22, 9001), (P4, 2, 23, 9001)])
    both, only_a, only_b = compare_runs.shared(jobs_a, jobs_b)
    assert both == [(P4, 8), (P5, 8)]
    assert only_a == [(P6, 8)]
    assert only_b == [(P4, 2)]
    assert [(jobs_a[e], ids_a[e], jobs_b[e], ids_b[e]) for e in both] == [(11, 4001, 22, 9001), (12, 4002, 21, 9002)]


def test_one_epochs_columns_come_from_its_metrics_and_packets():
    values = {"n_candidates": 777.0, "n_sources": 500.0, "n_sources_snr_gt20": 20.0,
              "core_fraction": -0.00105, "ring_fraction": 0.00172, "registration_dx_px": 0.003,
              "registration_dy_px": 0.004, "container_seconds": 249.3, "packets": 182}
    row = compare_runs.null_row(values)
    assert row[:6] == [777.0, 500.0, 20.0, 182, -0.00105, 0.00172] and row[7] == 249.3
    assert row[6] == pytest.approx(0.005)   # hypot(dx, dy), the gate's registration offset
    sparse = compare_runs.null_row({"n_candidates": 271.0, "packets": 0})
    assert sparse[0] == 271.0 and sparse[3] == 0 and sparse[6] is None
    table = compare_runs.null_table({113: {(P4, 8): sparse}}, [(P4, 8)])
    assert f"| {P4} sca 8 | 113 | 271 | — | — | 0 | — | — | — | — |" in table.splitlines()


def test_a_value_outside_the_spread_is_red():
    spread = {"packets": [266, 338], "core": [-2.6e-05, -2.6e-05], "difference s": [226.1, 350.5]}
    row = {"packets": 338, "core": -2.6e-05, "difference s": 252.4}
    assert compare_runs.red(row, spread) == []
    assert compare_runs.red({**row, "packets": 339}, spread) == ["packets"]
    assert compare_runs.red({**row, "core": -2.5e-05}, spread) == ["core"]   # a [v, v] spread is equality
    assert compare_runs.red({**row, "difference s": None}, spread) == ["difference s"]
