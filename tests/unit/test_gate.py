"""The quality gate's query on synthetic difference-job metrics.

Contracts: a set is held when its difference's ``n_sources_snr_gt20`` is
above the factor times the run's median, ``core_fraction`` or
``ring_fraction`` is beyond its absolute bound, ``usable_fraction`` is below
its floor or the registration offset's magnitude above its bound, and sent
just inside each; nothing is held before the run has ``min_epochs``
published differences; a metric the job could not compute is no reason to
hold; the defaults are the
shipped ``[gate]`` table's; ``decide`` reads the set's difference job through
its ``sources`` input, its coverage and registration among its metrics.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from rapidpipe.foundation import gate

SHIPPED = Path(__file__).resolve().parents[2] / "rapidpipe" / "settings" / "foundation-thin.toml"
HEALTHY = {"n_sources_snr_gt20": 19.0, "core_fraction": 0.006, "ring_fraction": -0.002}


@pytest.mark.parametrize("metric, inside, beyond", [
    ("n_sources_snr_gt20", 60.0, 60.5),          # the median 20 times the factor 3
    ("core_fraction", -0.025, -0.0251),
    ("ring_fraction", 0.015, 0.0151),
    ("usable_fraction", 0.9, 0.8999),            # run 72 SCA 9's reference from another SCA: 0.704
])
def test_each_bound_sends_inside_and_holds_beyond(metric, inside, beyond):
    assert gate.judge({**HEALTHY, metric: inside}, 20.0, 5)["send"]
    held = gate.judge({**HEALTHY, metric: beyond}, 20.0, 5)
    assert not held["send"] and held["metric"] == metric and held["value"] == beyond
    assert metric in held["reason"]


def test_a_registration_offset_beyond_the_bound_holds_by_its_magnitude():
    # Synthetic: no recorded job is misregistered past the bound; HLWAS's 1 px
    # WCS offset was fixed on its overlay before the metric existed.
    assert gate.judge({**HEALTHY, "registration_dx_px": 0.15, "registration_dy_px": -0.2}, 20.0, 5)["send"]
    held = gate.judge({**HEALTHY, "registration_dx_px": 0.18, "registration_dy_px": -0.18}, 20.0, 5)
    assert not held["send"] and held["metric"] == "registration" and held["value"] == pytest.approx(0.2546, abs=1e-4)
    assert "registration" in held["reason"]


def test_nothing_is_held_before_the_runs_floor_of_epochs():
    assert gate.judge({**HEALTHY, "core_fraction": 0.5, "usable_fraction": 0.5,
                       "registration_dx_px": 1.0, "registration_dy_px": 0.0}, 20.0, 4)["send"]


def test_a_metric_the_job_could_not_compute_is_no_reason_to_hold():
    assert gate.judge({}, 20.0, 9)["send"] and gate.judge({"n_sources_snr_gt20": 500.0}, None, 9)["send"]
    assert gate.judge({**HEALTHY, "registration_dx_px": 1.0}, 20.0, 9)["send"]


def test_a_set_inside_every_bound_sends():
    inside = {**HEALTHY, "usable_fraction": 0.9994, "registration_dx_px": 0.024, "registration_dy_px": -0.121}
    assert gate.judge(inside, 20.0, 5)["send"]


def test_the_defaults_are_the_shipped_gate_table():
    assert tomllib.loads(SHIPPED.read_text())["gate"] == gate.DEFAULTS


class Cursor:
    """Answers the three reads in order: the sources input, the metrics, the run's count and median."""

    def __init__(self, answers):
        self.answers, self.sql = list(answers), []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return self

    def execute(self, sql, params):
        self.sql.append((sql, params))

    def fetchone(self):
        return self.answers.pop(0)

    def fetchall(self):
        return self.answers.pop(0)

    def commit(self):
        pass


def test_decide_reads_the_difference_job_the_set_names_and_its_runs_median():
    conn = Cursor([(77,), [("n_sources_snr_gt20", 90.0), ("core_fraction", 0.001)], (6, 20.0)])
    answer = gate.decide(conn, 99, {"snr_gt20_factor": 3.0})
    assert answer["difference_job"] == 77 and not answer["send"] and answer["metric"] == "n_sources_snr_gt20"
    assert conn.sql[0][1] == (99,) and conn.sql[1][1] == (77,) and conn.sql[2][1] == (77,)


def test_decide_names_a_set_without_a_sources_input():
    with pytest.raises(LookupError):
        gate.decide(Cursor([None]), 99)


def test_decide_reads_coverage_and_registration_from_the_difference_job_the_set_names():
    conn = Cursor([(21837,), [("n_sources_snr_gt20", 20.0), ("usable_fraction", 0.7044),
                              ("registration_dx_px", -0.0721), ("registration_dy_px", 0.1429)], (162, 20.0)])
    answer = gate.decide(conn, 99)
    assert answer["difference_job"] == 21837 and not answer["send"] and answer["metric"] == "usable_fraction"
    assert conn.sql[1][1] == (21837,)
