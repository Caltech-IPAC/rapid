#!/usr/bin/env python3
"""Timing benchmark harness for alert production (test-side tooling; the
production modules in rapid_alerts/ are not instrumented and need no
changes -- see TimedProvider below for how).

Writes one JSON Lines file per run, mixing four record kinds:

    {"kind": "meta", ...}            one per run: machine + software +
                                     run identity, so files from different
                                     machines are directly comparable
    {"kind": "provider_call", ...}   one per data-access call (method,
                                     sid, seconds); the first get_cutouts
                                     of a batch carries the S3-staging
                                     cost, the first iter_sources fetch
                                     carries the chip query + prefetch
    {"kind": "source", ...}          one per source: wall time from the
                                     provider yielding the source until
                                     the next source is requested, i.e.
                                     the complete assemble+serialize
                                     (+publish) cost of that alert
    {"kind": "summary", ...}         one per run: totals and percentiles

This is a data product, not a log stream: it is written directly to the
requested path, while everything human-oriented goes through logging.

Usage (works on any machine with the DB environment variables set):

    python test/benchmark.py --exposure 80982 --sca 18 -o timing.jsonl
    python test/benchmark.py --pid 338173 -o timing.jsonl
    python test/benchmark.py report timing.jsonl [more.jsonl ...]
"""

import argparse
import datetime
import json
import logging
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rapid_alerts.providers import AlertDataProvider

logger = logging.getLogger(__name__)

VERSIONED_PACKAGES = ("numpy", "fitsio", "astropy", "fastavro",
                      "psycopg2-binary", "boto3")


# ---------------------------------------------------------------------------
# run metadata: everything needed to interpret the numbers later, or to
# compare a laptop-container run against the production machine
# ---------------------------------------------------------------------------

def cpu_model():
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.lower().startswith(("model name", "hardware")):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine()


def git_sha():
    try:
        return subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parents[2]),
             "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5).stdout.strip() or None
    except Exception:
        return None


def collect_meta(**run_identity):
    import importlib.metadata
    versions = {}
    for pkg in VERSIONED_PACKAGES:
        try:
            versions[pkg] = importlib.metadata.version(pkg)
        except importlib.metadata.PackageNotFoundError:
            versions[pkg] = None
    return {
        "kind": "meta",
        "started_utc": datetime.datetime.now(
            datetime.timezone.utc).isoformat(timespec="seconds"),
        "host": platform.node(),
        "arch": platform.machine(),
        "cpu": cpu_model(),
        "cores": len(os.sched_getaffinity(0)) if hasattr(
            os, "sched_getaffinity") else os.cpu_count(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "versions": versions,
        "git_sha": git_sha(),
        **run_identity,
    }


# ---------------------------------------------------------------------------
# the instrumented pieces
# ---------------------------------------------------------------------------

class TimingLog:
    """Appends JSON records to the output file, one per line, flushing
    each so a crashed run still leaves a parseable file."""

    def __init__(self, path):
        self.path = path
        self._file = open(path, "w")

    def write(self, record):
        self._file.write(json.dumps(record) + "\n")
        self._file.flush()

    def close(self):
        self._file.close()


class TimedProvider(AlertDataProvider):
    """Times any AlertDataProvider without touching production code.

    get_* calls are timed directly. Per-source wall time falls out of the
    batch flow's shape: batch_produce pulls sources one at a time from
    iter_sources() and fully processes each before requesting the next,
    so the gap between successive yields IS that source's total
    assemble + serialize + publish cost.
    """

    def __init__(self, inner, log):
        self.inner = inner
        self.log = log
        self.source_seconds = []          # kept for the summary percentiles
        self.phase_totals = {}            # method -> summed seconds

    def _timed(self, method, sid, call):
        t0 = time.perf_counter()
        result = call()
        dt = time.perf_counter() - t0
        self.phase_totals[method] = self.phase_totals.get(method, 0.0) + dt
        self.log.write({"kind": "provider_call", "method": method,
                        "sid": sid, "seconds": round(dt, 6)})
        return result

    def get_detection(self, sid):
        return self._timed("get_detection", sid,
                           lambda: self.inner.get_detection(sid))

    def get_object_for_source(self, detection):
        return self._timed("get_object_for_source", detection.sid,
                           lambda: self.inner.get_object_for_source(detection))

    def get_prv_detections(self, detection, obj, **kwargs):
        return self._timed("get_prv_detections", detection.sid,
                           lambda: self.inner.get_prv_detections(
                               detection, obj, **kwargs))

    def get_forced_photometry(self, detection, obj):
        return self._timed("get_forced_photometry", detection.sid,
                           lambda: self.inner.get_forced_photometry(
                               detection, obj))

    def get_cutouts(self, detection):
        return self._timed("get_cutouts", detection.sid,
                           lambda: self.inner.get_cutouts(detection))

    def resolve_pid(self, expid, sca):
        return self._timed("resolve_pid", None,
                           lambda: self.inner.resolve_pid(expid, sca))

    def iter_sources(self, pid):
        inner_iter = self.inner.iter_sources(pid)
        previous_sid = None
        yielded_at = None
        n = 0
        first_fetch = True
        while True:
            requested_at = time.perf_counter()
            if previous_sid is not None:
                # time since we handed out the previous source = its full
                # processing cost in batch_produce's loop
                n += 1
                seconds = requested_at - yielded_at
                self.source_seconds.append(seconds)
                self.log.write({"kind": "source", "n": n,
                                "sid": previous_sid,
                                "seconds": round(seconds, 6)})
            try:
                source = next(inner_iter)
            except StopIteration:
                break
            if first_fetch:
                # the first next() runs the whole-image query + the
                # association/history prefetch
                self._note_fetch(time.perf_counter() - requested_at)
                first_fetch = False
            previous_sid = source.sid
            yielded_at = time.perf_counter()
            yield source

    def _note_fetch(self, dt):
        self.phase_totals["iter_sources_prefetch"] = dt
        self.log.write({"kind": "provider_call",
                        "method": "iter_sources_prefetch",
                        "sid": None, "seconds": round(dt, 6)})


# ---------------------------------------------------------------------------
# running a benchmark
# ---------------------------------------------------------------------------

def percentile(sorted_values, fraction):
    if not sorted_values:
        return None
    index = min(len(sorted_values) - 1,
                round(fraction * (len(sorted_values) - 1)))
    return sorted_values[index]


def benchmark_batch(provider, pid, out_path, meta_extra=None, producer=None,
                    topic="alerts"):
    """Run batch_produce(pid) through the timing harness; returns the
    alert count. Callable from pytest with a fake provider."""
    from rapid_alerts.produce import batch_produce, load_schema

    log = TimingLog(out_path)
    timed = TimedProvider(provider, log)
    try:
        log.write(collect_meta(pid=pid, **(meta_extra or {})))
        schema = load_schema()          # excluded from the timed window:
        t0 = time.perf_counter()        # schema parsing is per-process, not
        count = batch_produce(timed, pid, producer=producer,  # per-batch
                              topic=topic, schema=schema)
        total = time.perf_counter() - t0

        ordered = sorted(timed.source_seconds)
        log.write({
            "kind": "summary",
            "n_alerts": count,
            "total_s": round(total, 3),
            "alerts_per_s": round(count / total, 2) if total else None,
            "per_source_median_s": percentile(ordered, 0.50),
            "per_source_p95_s": percentile(ordered, 0.95),
            "per_source_max_s": percentile(ordered, 1.00),
            "phase_totals_s": {k: round(v, 3)
                               for k, v in sorted(timed.phase_totals.items())},
        })
    finally:
        log.close()
    logger.info("benchmark written to %s", out_path)
    return count


# ---------------------------------------------------------------------------
# reading benchmarks back
# ---------------------------------------------------------------------------

def report(paths):
    for path in paths:
        meta = summary = None
        with open(path) as f:
            for line in f:
                record = json.loads(line)
                if record["kind"] == "meta":
                    meta = record
                elif record["kind"] == "summary":
                    summary = record
        print(f"\n=== {path}")
        if meta:
            print(f"  {meta['started_utc']}  {meta['host']} "
                  f"({meta['arch']}, {meta['cores']} cores)  "
                  f"python {meta['python']}  git {meta.get('git_sha')}")
            print(f"  cpu: {meta['cpu']}")
            identity = {k: meta[k] for k in
                        ("pid", "expid", "sca", "diff_flavor")
                        if meta.get(k) is not None}
            print(f"  run: {identity}")
        if summary is None:
            print("  (no summary record -- crashed or still running?)")
            continue
        print(f"  {summary['n_alerts']} alerts in {summary['total_s']} s "
              f"= {summary['alerts_per_s']} alerts/s")
        print(f"  per-source: median {summary['per_source_median_s']} s, "
              f"p95 {summary['per_source_p95_s']} s, "
              f"max {summary['per_source_max_s']} s")
        for phase, seconds in summary["phase_totals_s"].items():
            print(f"    {phase:24s} {seconds:10.3f} s")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "report":
        report(argv[1:])
        return 0

    parser = argparse.ArgumentParser(
        description="Benchmark batch alert production against the live "
                    "database (or run `benchmark.py report FILE...` to "
                    "summarize existing timing files)")
    parser.add_argument("--pid", type=int,
                        help="difference-image processing ID to batch")
    parser.add_argument("--exposure", type=int, metavar="EXPID",
                        help="resolve the newest vbest>0 processing "
                             "(needs --sca)")
    parser.add_argument("--sca", type=int)
    parser.add_argument("-o", "--out", default="alert_timing.jsonl",
                        help="output JSONL path (default: %(default)s)")
    parser.add_argument("--diff-flavor", choices=["sfft", "zogy"],
                        default="sfft")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level))

    if (args.exposure is None) != (args.sca is None):
        parser.error("--exposure and --sca must be given together")
    if (args.pid is not None) == (args.exposure is not None):
        parser.error("give exactly one of --pid or --exposure/--sca")

    from rapid_alerts.cli import make_provider
    provider = make_provider(diff_flavor=args.diff_flavor)
    pid = (args.pid if args.pid is not None
           else provider.resolve_pid(args.exposure, args.sca))

    count = benchmark_batch(
        provider, pid, args.out,
        meta_extra={"expid": args.exposure, "sca": args.sca,
                    "diff_flavor": args.diff_flavor})
    print(f"pid={pid}: {count} alerts benchmarked -> {args.out}")
    report([args.out])
    return 0


if __name__ == "__main__":
    sys.exit(main())
