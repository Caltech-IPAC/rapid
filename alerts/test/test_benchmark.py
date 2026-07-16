"""The timing-benchmark harness (benchmark.py) produces a well-formed
JSONL file. Runs the fake chip through the real batch path, so this also
guards TimedProvider's transparency -- wrapping a provider must not
change what gets produced.

TODO (not yet implemented):
  - overhead guard: TimedProvider-wrapped batch within a few percent of
    the unwrapped run on the fake chip (catches accidentally expensive
    instrumentation)
  - report() smoke test on a file with no summary record (crashed run)
"""

import json

from conftest import CHIP_PID
from benchmark import benchmark_batch


def read_records(path):
    with open(path) as f:
        return [json.loads(line) for line in f]


def test_benchmark_writes_wellformed_timing_log(make_provider, chip_data,
                                                tmp_path):
    out = tmp_path / "timing.jsonl"
    count = benchmark_batch(make_provider(), CHIP_PID, str(out),
                            meta_extra={"diff_flavor": "sfft"})
    assert count == len(chip_data.sources)

    records = read_records(out)
    by_kind = {}
    for record in records:
        by_kind.setdefault(record["kind"], []).append(record)

    # exactly one meta record, first in the file, carrying the fields a
    # cross-machine comparison needs
    assert records[0]["kind"] == "meta"
    (meta,) = by_kind["meta"]
    for field in ("arch", "cpu", "cores", "python", "versions",
                  "started_utc", "pid", "diff_flavor"):
        assert field in meta, f"meta record missing {field}"
    assert meta["pid"] == CHIP_PID

    # one source record per alert, in sid order, positive wall times
    sources = by_kind["source"]
    assert len(sources) == count
    assert [s["sid"] for s in sources] == sorted(
        row["sid"] for row in chip_data.sources)
    assert all(s["seconds"] > 0 for s in sources)
    assert [s["n"] for s in sources] == list(range(1, count + 1))

    # provider calls include the prefetch and every cutout fetch
    methods = {c["method"] for c in by_kind["provider_call"]}
    assert "iter_sources_prefetch" in methods
    assert "get_cutouts" in methods

    # exactly one summary, last in the file, internally consistent
    assert records[-1]["kind"] == "summary"
    (summary,) = by_kind["summary"]
    assert summary["n_alerts"] == count
    assert summary["total_s"] > 0
    assert summary["alerts_per_s"] > 0
    assert 0 < summary["per_source_median_s"] <= summary["per_source_max_s"]
    assert "get_cutouts" in summary["phase_totals_s"]
