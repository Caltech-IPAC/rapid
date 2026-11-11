"""``scripts/throughput_report.py``: a run's hours ranked from its records alone.

Contracts: the historical view splits each job's ``container_seconds`` by
its stage timers, the rest untimed, weighted by its Batch vCPU; the step
view uses the ``step_*_s`` metrics and is absent for jobs that wrote none;
a host's time is split equally among the jobs running at each moment, the
moments with none idle and other runs' jobs counted apart; queue wait is
``createdAt`` to ``startedAt``; the critical path follows only an upstream
job that stopped before its downstream job was submitted; fixed cost is
absent without steps; a published job with no ``container_seconds`` is
left out of the sums and named.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "throughput_report", Path(__file__).resolve().parents[2] / "scripts" / "throughput_report.py")
report = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(report)

S = 1000  # Batch's epoch milliseconds per second


def job(jid, stage, metrics, bid, state="published"):
    return {"id": jid, "stage": stage, "state": state, "scope": {}, "instance_type": "r7i.4xlarge",
            "attempts": {bid: {"instance_type": "r7i.4xlarge"}}, "metrics": metrics}


def fixture():
    jobs = {
        1: job(1, "intake", {"container_seconds": 100.0}, "b1"),
        2: job(2, "difference", {"container_seconds": 1000.0, "rb_s": 400.0, "xsources_s": 300.0},
               "b2"),
        3: job(3, "difference", {"container_seconds": 1000.0, "rb_s": 400.0, "xsources_s": 300.0,
                                 "step_rb_s": 400.0, "step_read_inputs_s": 50.0, "startup_s": 7.0,
                                 "n_rb_scored": 90.0, "n_rb_scored_negative": 10.0}, "b3"),
        4: job(4, "packet", {"container_seconds": 200.0}, "b4"),
        5: job(5, "packet", {}, "b5"),                       # published, its ended never written
    }
    batch = {
        "b1": {"createdAt": 0, "startedAt": 10 * S, "stoppedAt": 110 * S, "host": "h1", "vcpus": 1.0},
        "b2": {"createdAt": 120 * S, "startedAt": 200 * S, "stoppedAt": 1200 * S, "host": "h1",
               "vcpus": 1.0},
        "b3": {"createdAt": 120 * S, "startedAt": 200 * S, "stoppedAt": 1200 * S, "host": "h1",
               "vcpus": 2.0},
        "b4": {"createdAt": 1300 * S, "startedAt": 1301 * S, "stoppedAt": 1501 * S, "host": "h2",
               "vcpus": 1.0},
        "other": {"createdAt": 0, "startedAt": 1301 * S, "stoppedAt": 1501 * S, "host": "h2",
                  "vcpus": 1.0},
    }
    run = {"run": 9, "dataset": "d", "revision": "r", "survey": "GBTDS", "jobs": jobs,
           "edges": {4: [2, 3], 2: [1], 3: [1]}}
    return run, batch


def test_the_historical_view_splits_by_the_timers_weighted_by_the_jobs_vcpu():
    run, batch = fixture()
    hours, total, _ = report.vcpu_hours(report.published(run["jobs"]), batch, "timers")
    assert hours["difference: rb_s"] * 3600 == 400 + 2 * 400
    assert hours["difference: untimed"] * 3600 == 300 + 2 * 300
    assert hours["intake"] * 3600 == 100
    assert total * 3600 == 100 + 1000 + 2000 + 200


def test_the_step_view_covers_only_the_jobs_that_wrote_steps():
    run, batch = fixture()
    hours, _, covered = report.vcpu_hours(report.published(run["jobs"]), batch, "steps")
    assert covered == 1
    assert hours["difference: remainder"] * 3600 == 2 * (1000 - 450)


def test_a_hosts_time_is_split_among_the_jobs_running_and_its_gaps_are_idle():
    run, batch = fixture()
    host = report.host_hours(run, batch, {"h1": "r7i.4xlarge", "h2": "r7i.4xlarge"}, {})
    # h1: the intake alone 100 s, 90 s idle, then the two differences together 1,000 s.
    assert round(host["idle"] * 3600) == 90
    assert round(host["other"] * 3600) == 100                     # h2 shared half and half
    assert round(host["run"] * 3600) == 100 + 1000 + 100
    assert round(host["span"] * 3600) == 1190 + 200


def test_queue_wait_is_created_to_started():
    run, batch = fixture()
    assert report.queue_waits(run, batch)["difference"] == [80.0, 80.0]


def test_the_critical_path_follows_only_a_gating_upstream_job():
    run, batch = fixture()
    run["edges"][4] = [2, 3, 1]
    batch["b3"]["stoppedAt"] = 1350 * S                            # still running at b4's submission
    path = report.critical_path(run, batch)
    assert [p["job"] for p in path] == [1, 2, 4]
    assert path[1]["gap_s"] == 10 and path[2]["wait_s"] == 1


def test_fixed_cost_is_absent_without_steps_and_counts_startup_and_reads_with_them():
    run, _ = fixture()
    fixed = report.fixed_and_per_source(report.published(run["jobs"]))
    assert fixed["packet"]["fixed_s"] is None
    assert fixed["difference"]["fixed_s"] == 57.0
    assert fixed["difference"]["per_source_s"] == (1000 - 50) / 100


def test_the_report_names_a_published_job_without_seconds_and_prints_absent_fields():
    run, batch = fixture()
    text = report.render(run, batch, {"r7i.4xlarge": 16})
    assert "Published without it (left out): packet 1." in text
    assert "| packet | 1 | absent |" in text
    assert "## Leads, ranked" in text
