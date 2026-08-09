#!/usr/bin/env python3
"""Timing benchmark harness for alert production (test-side tooling; the
production modules in alerts/ are not instrumented and need no
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
    {"kind": "summary", ...}         one per run: timing totals and
                                     percentiles, peak/bracketing memory,
                                     and (with --save) the output archive
                                     size

This is a data product, not a log stream: it is written directly to the
requested path (and the summary is also narrated to the console), while
everything else human-oriented goes through logging.

Usage (works on any machine with the DB environment variables set):

    python test/benchmark.py --exposure 80982 --sca 18 -o timing.jsonl
    python test/benchmark.py --pid 338173 --save alerts.avro -o timing.jsonl
    python test/benchmark.py report timing.jsonl [more.jsonl ...]

Memory tooling note: peak RSS comes from resource.ru_maxrss (exact, no
sampling, but single-process); current RSS from psutil when available
(else /proc). tracemalloc is intentionally unused -- it misses the
C-level numpy/fitsio image buffers that dominate this workload. See
MemoryMonitor for the process-pool extension point.
"""

import argparse
import contextlib
import datetime
import json
import logging
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

try:
    import resource                       # Unix only (Linux, macOS)
except ImportError:                       # pragma: no cover - Windows
    resource = None

try:
    import psutil                         # optional; enables tree-aware RSS
except ImportError:
    psutil = None

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

logger = logging.getLogger(__name__)

VERSIONED_PACKAGES = ("numpy", "fitsio", "astropy", "fastavro",
                      "psycopg2-binary", "boto3", "psutil")


# ---------------------------------------------------------------------------
# memory measurement
#
# The dominant memory user here is the ~200 MB of chip images fitsio/numpy
# load per difference image -- C-level allocations that Python-level tools
# (tracemalloc) cannot see. Two complementary measures, both near-zero
# overhead so they don't distort the timing measured in the same run:
#
#   peak_rss_bytes()     exact process high-water mark (resource.ru_maxrss).
#                        No sampling, so it never misses a spike -- but it
#                        is single-process only.
#   MemoryMonitor        current RSS bracketing the batch, via psutil (or a
#                        /proc fallback). The seam for the future
#                        process-pool case: see its TODO.
#
# tracemalloc is deliberately unused (misses the numpy image buffers and
# ~2x's runtime); psutil is optional (guarded import) so a machine without
# it still benchmarks, just without the tree-aware numbers.
# ---------------------------------------------------------------------------

def peak_rss_bytes():
    """Peak resident set size of this process so far, in bytes, or None if
    unavailable. ru_maxrss is KiB on Linux but bytes on macOS."""
    if resource is None:
        return None
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw if platform.system() == "Darwin" else raw * 1024


def current_rss_bytes():
    """Current (not peak) resident set size of this process in bytes, or
    None if unavailable. Prefers psutil (portable); falls back to /proc
    (Linux)."""
    if psutil is not None:
        return psutil.Process().memory_info().rss
    try:
        with open("/proc/self/statm") as f:
            resident_pages = int(f.read().split()[1])
        return resident_pages * (resource.getpagesize() if resource
                                 else 4096)
    except (OSError, IndexError, ValueError):
        return None


class MemoryMonitor:
    """Records RSS at the start and end of a batch.

    Today this brackets one process. When production fans alert work out
    to a multiprocessing pool, resource.ru_maxrss (RUSAGE_SELF) will miss
    the workers, so extend here rather than at the call sites.

    TODO (process-pool parallelism): sample psutil.Process().children(
    recursive=True) on a background timer for a live aggregate tree peak,
    and report memory_full_info().pss instead of rss so pages shared via
    the mmap'd chip images are not double-counted across workers.
    """

    def __init__(self):
        self.rss_start = None
        self.rss_end = None

    def __enter__(self):
        self.rss_start = current_rss_bytes()
        return self

    def __exit__(self, *exc):
        self.rss_end = current_rss_bytes()


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


class TimedProvider:
    """Times any provider without touching production code.

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
                    topic="alerts", archive_path=None, compress=True):
    """Run batch_produce(pid) through the timing harness; returns the
    alert count. Callable from pytest with a fake provider.

    With archive_path set, the run's alerts are written to that Avro
    object-container file (compressed unless compress=False), so the
    benchmark measures the real, compressed output and reports its size.
    """
    from alerts.produce import (batch_produce, load_schema,
                                      open_alert_archive)

    meta = collect_meta(pid=pid,
                        archive="deflate" if (archive_path and compress)
                        else "uncompressed" if archive_path else None,
                        **(meta_extra or {}))
    log = TimingLog(out_path)
    timed = TimedProvider(provider, log)
    try:
        log.write(meta)
        schema = load_schema()          # excluded from the timed window:
                                        # schema parsing is per-process,
                                        # not per-batch
        archive_cm = (open_alert_archive(
            archive_path, schema=schema,
            codec="deflate" if compress else "null")
            if archive_path else contextlib.nullcontext())

        with MemoryMonitor() as mem:
            t0 = time.perf_counter()
            with archive_cm as archive:
                count = batch_produce(timed, pid, producer=producer,
                                      topic=topic, schema=schema,
                                      archive=archive)
            total = time.perf_counter() - t0

        archive_bytes = (os.path.getsize(archive_path)
                         if archive_path else None)
        ordered = sorted(timed.source_seconds)
        summary = {
            "kind": "summary",
            "n_alerts": count,
            "total_s": round(total, 3),
            "alerts_per_s": round(count / total, 2) if total else None,
            "per_source_median_s": percentile(ordered, 0.50),
            "per_source_p95_s": percentile(ordered, 0.95),
            "per_source_max_s": percentile(ordered, 1.00),
            "phase_totals_s": {k: round(v, 3)
                               for k, v in sorted(timed.phase_totals.items())},
            # memory: peak is the exact process high-water mark; start/end
            # bracket the batch (see MemoryMonitor)
            "peak_rss_bytes": peak_rss_bytes(),
            "rss_start_bytes": mem.rss_start,
            "rss_end_bytes": mem.rss_end,
            # output: the on-disk size of the produced alert archive
            "archive_bytes": archive_bytes,
            "archive_bytes_per_alert": (round(archive_bytes / count)
                                        if archive_bytes and count else None),
        }
        log.write(summary)
    finally:
        log.close()
    # the summary is a data product (saved to out_path) AND narrated to the
    # console, so a direct caller (pytest, a manual run) sees the numbers
    for line in summary_lines(meta, summary):
        logger.info("%s", line)
    logger.info("benchmark written to %s", out_path)
    return count


# ---------------------------------------------------------------------------
# reading benchmarks back
# ---------------------------------------------------------------------------

def _mib(n_bytes):
    return None if n_bytes is None else round(n_bytes / (1024 * 1024), 1)


def summary_lines(meta, summary):
    """Human-readable summary as a list of lines. Shared by report()
    (prints them) and benchmark_batch() (logs them), so the console
    format has one definition."""
    lines = []
    if meta:
        lines.append(f"{meta['started_utc']}  {meta['host']} "
                     f"({meta['arch']}, {meta['cores']} cores)  "
                     f"python {meta['python']}  git {meta.get('git_sha')}")
        lines.append(f"cpu: {meta['cpu']}")
        identity = {k: meta[k] for k in
                    ("pid", "expid", "sca", "difference_image", "archive")
                    if meta.get(k) is not None}
        lines.append(f"run: {identity}")
    if summary is None:
        lines.append("(no summary record -- crashed or still running?)")
        return lines

    lines.append(f"{summary['n_alerts']} alerts in {summary['total_s']} s "
                 f"= {summary['alerts_per_s']} alerts/s")
    lines.append(f"per-source: median {summary['per_source_median_s']} s, "
                 f"p95 {summary['per_source_p95_s']} s, "
                 f"max {summary['per_source_max_s']} s")

    peak = _mib(summary.get("peak_rss_bytes"))
    if peak is not None:
        start, end = (_mib(summary.get("rss_start_bytes")),
                      _mib(summary.get("rss_end_bytes")))
        bracket = (f" (batch RSS {start} -> {end} MiB)"
                   if start is not None and end is not None else "")
        lines.append(f"peak RSS: {peak} MiB{bracket}")

    archive_bytes = summary.get("archive_bytes")
    if archive_bytes is not None:
        codec = (meta or {}).get("archive", "?")
        lines.append(f"archive: {_mib(archive_bytes)} MiB "
                     f"({summary.get('archive_bytes_per_alert')} bytes/alert, "
                     f"{codec})")

    for phase, seconds in summary["phase_totals_s"].items():
        lines.append(f"  {phase:24s} {seconds:10.3f} s")
    return lines


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
        for line in summary_lines(meta, summary):
            print(f"  {line}")


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
                        help="timing JSONL output path (default: %(default)s)")
    parser.add_argument("--save", metavar="FILE",
                        help="also write the alerts to this Avro archive and "
                             "report its size (measures real output)")
    parser.add_argument("--no-compress", action="store_true",
                        help="store the --save archive uncompressed; by "
                             "default it is deflate-compressed (MAST format)")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level))

    if (args.exposure is None) != (args.sca is None):
        parser.error("--exposure and --sca must be given together")
    if (args.pid is not None) == (args.exposure is not None):
        parser.error("give exactly one of --pid or --exposure/--sca")

    from alerts.cli import make_provider
    provider = make_provider()
    pid = (args.pid if args.pid is not None
           else provider.resolve_pid(args.exposure, args.sca))

    # WHICH difference image was measured is still part of the run's
    # identity — it is just read from the registered row now instead of
    # being asserted by a flag, so the report cannot claim an image the
    # cutouts did not come from.
    difference_image = provider.registered_difference_image(pid)

    # benchmark_batch narrates the full summary to the console (logging)
    # and saves it to --out; no separate report() call needed here
    count = benchmark_batch(
        provider, pid, args.out,
        meta_extra={"expid": args.exposure, "sca": args.sca,
                    "difference_image": difference_image},
        archive_path=args.save, compress=not args.no_compress)
    print(f"pid={pid}: {count} alerts benchmarked -> {args.out}"
          + (f" (+ archive {args.save})" if args.save else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
