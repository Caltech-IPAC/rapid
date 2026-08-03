"""The timing-benchmark harness (benchmark.py) produces a well-formed
JSONL file with timing, memory, and output-size records. Runs the fake
chip through the real batch path, so this also guards TimedProvider's
transparency -- wrapping a provider must not change what gets produced.

TODO (not yet implemented):
  - overhead guard: TimedProvider-wrapped batch within a few percent of
    the unwrapped run on the fake chip (catches accidentally expensive
    instrumentation)
  - report() smoke test on a file with no summary record (crashed run)
  - memory: once production fans out to a process pool, assert the
    tree-aware peak (MemoryMonitor's children/PSS path) exceeds a
    single worker's
"""

import json

import fastavro

from conftest import CHIP_PID
from benchmark import benchmark_batch, current_rss_bytes, peak_rss_bytes


def read_records(path):
    with open(path) as f:
        return [json.loads(line) for line in f]


def test_benchmark_writes_wellformed_timing_log(make_provider, chip_data,
                                                tmp_path):
    out = tmp_path / "timing.jsonl"
    archive = tmp_path / "alerts.avro"
    count = benchmark_batch(make_provider(), CHIP_PID, str(out),
                            meta_extra={"diff_flavor": "sfft"},
                            archive_path=str(archive))
    assert count == len(chip_data.sources)

    records = read_records(out)
    by_kind = {}
    for record in records:
        by_kind.setdefault(record["kind"], []).append(record)

    # exactly one meta record, first in the file, carrying the fields a
    # cross-machine comparison needs (incl. the archive codec)
    assert records[0]["kind"] == "meta"
    (meta,) = by_kind["meta"]
    for field in ("arch", "cpu", "cores", "python", "versions",
                  "started_utc", "pid", "diff_flavor"):
        assert field in meta, f"meta record missing {field}"
    assert meta["pid"] == CHIP_PID
    assert meta["archive"] == "deflate"

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

    # memory: peak and current RSS are populated and agree to well within
    # a factor of 2 -- the real bug this guards is a unit mismatch (the
    # KiB-vs-bytes ru_maxrss quirk would be ~1000x off). They are NOT
    # strictly ordered: the kernel's ru_maxrss high-water mark updates
    # lazily, so an instantaneous psutil read can sit a fraction of a
    # percent above it.
    assert summary["peak_rss_bytes"] > 0
    assert summary["rss_start_bytes"] > 0
    assert 0.5 < summary["peak_rss_bytes"] / summary["rss_end_bytes"] < 2.0

    # output size: the archive was written and its size recorded, and the
    # recorded size matches the real file, which reads back as `count`
    # alerts
    assert summary["archive_bytes"] == archive.stat().st_size
    assert summary["archive_bytes_per_alert"] > 0
    with open(archive, "rb") as f:
        assert len(list(fastavro.reader(f))) == count


def test_memory_helpers_available():
    # both helpers work on the CI/dev platform (Linux); if this ever fails
    # the summary's memory fields would silently go null
    assert peak_rss_bytes() > 0
    assert current_rss_bytes() > 0
