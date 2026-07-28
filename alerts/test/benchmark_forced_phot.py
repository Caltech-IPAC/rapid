#!/usr/bin/env python3
"""Forced-photometry cost benchmark: decision data for alert-time FP
versus storing FP products (and, if stored, how often full re-runs are
affordable).

Two tiers, sharing one JSONL output file (kinds: meta, object, field,
fp_run, summary):

  survey (default; needs only the DB environment)
      Walks the real alert path for one chip (iter_sources + object
      association) and, for every alert-triggering object, counts the
      forced-photometry workload behind it: how many difference-image
      epochs cover its position (the work an alert-time FP request would
      redo), alongside its detection history. This is the "varying source
      histories" distribution.

  measure (--run; needs the full RAPID pipeline container + AWS + DB)
      Actually executes pipeline/forcedPhotometryForField.py for batches
      of surveyed object positions (request CSVs written with
      reqid = aid), at several batch sizes. Batch-size variation
      separates the fixed per-field cost (staging every overlapping
      epoch from S3) from the marginal per-position cost -- the two
      numbers that decide the architecture:

        alert-time FP:   every alert pays  fixed + marginal
        stored FP:       each new epoch appends incrementally; a full
                         re-run (e.g. after object centroids move) pays
                         fixed + marginal * n_positions, and its cost
                         sets the affordable re-run cadence

Usage (like benchmark.py, from the alerts/ package root):

    # survey only: history/epoch distribution for one chip
    python3.11 test/benchmark_forced_phot.py --pid 338173 -o fp.jsonl

    # survey + real FP runs at batch sizes 1, 4 and 16
    python3.11 test/benchmark_forced_phot.py --pid 338173 --run \
        --batches 1,4,16 -o fp.jsonl

    # summarize one or more result files
    python3.11 test/benchmark_forced_phot.py report fp.jsonl

The measure tier is expensive: each run downloads every difference image
overlapping the field (the FP backend works per field, not per position)
and leaves its scratch directory in place for inspection. Run it inside
the pipeline container with RAPID_SW/RAPID_WORK/ROMANTESSELLATIONDBNAME,
DB variables, and AWS credentials set.
"""

import argparse
import configparser
import csv
import json
import logging
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from benchmark import TimingLog, collect_meta, percentile, _mib

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
FP_SCRIPT = REPO_ROOT / "pipeline" / "forcedPhotometryForField.py"
FP_CONFIG = REPO_ROOT / "cdf" / "awsBatchSubmitJobs_launchSingleSciencePipeline.ini"

# The C module the FP backend shells out to (path hardcoded there too).
CFORCEPSFAPER = "/code/c/bin/cforcepsfaper"

# Cone-search radius around an object position when counting overlapping
# difference images. An SCA footprint is ~0.125 deg on a side (half
# diagonal ~0.09 deg); the margin only costs a few extra rows that the
# exact footprint test below then rejects.
POSITION_CONE_RADIUS_DEG = 0.15

FP_LC_SENTINEL = -99999.0  # "no calculation possible" in lightcurve files


def load_fp_config():
    """The FP backend parameters the survey must mirror so its epoch
    counts describe the same query the real job would issue."""
    config = configparser.ConfigParser()
    if not config.read(FP_CONFIG):
        raise SystemExit(f"cannot read FP pipeline config {FP_CONFIG}")
    return {
        "ppid_sci": int(config["SCI_IMAGE"]["ppid"]),
        "jd_earliest": float(config["FORCED_PHOTOMETRY"]["jd_earliest"]),
        "match_radius_overlap_field":
            float(config["FORCED_PHOTOMETRY"]["match_radius_overlap_field"]),
    }


# ---------------------------------------------------------------------------
# geometry: does a difference image's footprint contain a sky position?
# ---------------------------------------------------------------------------

def contains_position(ra, dec, corners):
    """True if (ra, dec) lies inside the quadrilateral spanned by
    `corners` [(ra, dec) x 4, any order]. Corners are projected onto a
    local tangent plane at the test position (RA wrap-safe), sorted into
    perimeter order around their centroid (the DB does not promise a
    winding order), and tested with an even-odd ray crossing."""
    cos_dec = math.cos(math.radians(dec))
    points = []
    for corner_ra, corner_dec in corners:
        delta_ra = (corner_ra - ra + 180.0) % 360.0 - 180.0
        points.append((delta_ra * cos_dec, corner_dec - dec))
    center_x = sum(x for x, _ in points) / len(points)
    center_y = sum(y for _, y in points) / len(points)
    points.sort(key=lambda p: math.atan2(p[1] - center_y, p[0] - center_x))
    inside = False
    for i, (x1, y1) in enumerate(points):
        x2, y2 = points[(i + 1) % len(points)]
        if (y1 > 0.0) != (y2 > 0.0):
            if x1 + (0.0 - y1) * (x2 - x1) / (y2 - y1) > 0.0:
                inside = not inside
    return inside


# ---------------------------------------------------------------------------
# survey: per-object FP workload from the DB alone
# ---------------------------------------------------------------------------

def chip_positions(provider, pid):
    """One position per alert-triggering object on the chip (deduped by
    aid), with its detection-history size -- plus the count of
    unassociated triggers (no object yet; the precovery case)."""
    positions = {}
    n_sources = 0
    n_unassociated = 0
    for source in provider.iter_sources(pid):
        n_sources += 1
        obj = provider.get_object_for_source(source)
        if obj is None:
            n_unassociated += 1
            continue
        if obj.aid in positions:
            positions[obj.aid]["n_triggers"] += 1
            continue
        prv = provider.get_prv_detections(source, obj)
        positions[obj.aid] = {
            "aid": obj.aid, "sid": source.sid, "field": source.field,
            "ra": obj.ra0, "dec": obj.dec0, "n_prv": len(prv),
            "n_triggers": 1,
        }
    return list(positions.values()), n_sources, n_unassociated


def epochs_at_position(provider, ra, dec, fp_cfg,
                       cone_radius=POSITION_CONE_RADIUS_DEG):
    """Difference-image epochs whose footprint contains (ra, dec), under
    the same ppid/jd/status/vbest filters the FP backend's query uses.
    This is the per-position workload an alert-time FP request re-does."""
    rows = provider._query("""
        SELECT pid, jd, fid, ra1, dec1, ra2, dec2, ra3, dec3, ra4, dec4
        FROM diffimages
        WHERE ppid = %s AND jd >= %s AND status > 0 AND vbest > 0
          AND q3c_radial_query(ra0, dec0, %s, %s, %s)
    """, (fp_cfg["ppid_sci"], fp_cfg["jd_earliest"],
          float(ra), float(dec), float(cone_radius)))
    return [row for row in rows if contains_position(
        ra, dec, [(row[f"ra{i}"], row[f"dec{i}"]) for i in (1, 2, 3, 4)])]


def field_job_epochs(provider, field, fp_cfg):
    """How many difference images one FP job for this field would pull
    from the DB (its cone search around the field center): the staging
    upper bound. None if the fields table has no row."""
    rows = provider._query(
        "SELECT ra0, dec0 FROM fields WHERE field = %s", (int(field),))
    if not rows:
        return None
    count = provider._query("""
        SELECT count(*) AS n
        FROM diffimages
        WHERE ppid = %s AND jd >= %s AND status > 0 AND vbest > 0
          AND q3c_radial_query(ra0, dec0, %s, %s, %s)
    """, (fp_cfg["ppid_sci"], fp_cfg["jd_earliest"],
          rows[0]["ra0"], rows[0]["dec0"],
          fp_cfg["match_radius_overlap_field"]))
    return int(count[0]["n"])


def sample_evenly(items, n):
    """Up to n items spread evenly across the list (all of them when it
    is short enough). Deterministic, order-preserving."""
    if n is None or n <= 0 or len(items) <= n:
        return list(items)
    step = len(items) / n
    return [items[int(i * step)] for i in range(n)]


def survey(provider, pid, log, fp_cfg, cone_radius=POSITION_CONE_RADIUS_DEG,
           max_objects=None):
    """Write one `object` record per (sampled) alerting object and one
    `field` record per involved field; returns the enriched position list
    plus chip totals for the summary.

    With max_objects set, the per-position epoch counting -- one q3c
    query each, the expensive part on a dense chip -- runs on an evenly
    spaced sample; the association walk still covers the whole chip, so
    field-level object counts stay exact."""
    all_positions, n_sources, n_unassociated = chip_positions(provider, pid)
    positions = sample_evenly(all_positions, max_objects)
    logger.info("pid=%s: %d sources -> %d objects (%d unassociated), "
                "epoch-counting %d of them",
                pid, n_sources, len(all_positions), n_unassociated,
                len(positions))
    for position in positions:
        epochs = epochs_at_position(provider, position["ra"],
                                    position["dec"], fp_cfg, cone_radius)
        jds = sorted(row["jd"] for row in epochs)
        position["n_epochs"] = len(epochs)
        position["n_filters"] = len({row["fid"] for row in epochs})
        log.write({"kind": "object", **position,
                   "jd_first": jds[0] if jds else None,
                   "jd_last": jds[-1] if jds else None})

    fields = {}
    for field in sorted({p["field"] for p in positions}):
        job_epochs = field_job_epochs(provider, field, fp_cfg)
        n_objects = sum(p["field"] == field for p in all_positions)
        fields[field] = {"n_objects": n_objects, "job_epochs": job_epochs}
        log.write({"kind": "field", "field": field, "n_objects": n_objects,
                   "job_epochs": job_epochs})
        logger.info("field %s: %d objects, FP job would stage <= %s epochs",
                    field, n_objects, job_epochs)
    return positions, fields, n_sources, n_unassociated, len(all_positions)


# ---------------------------------------------------------------------------
# measure: run the real FP backend for batches of surveyed positions
# ---------------------------------------------------------------------------

def require_fp_environment():
    """The FP backend's prerequisites, checked up front so a misconfigured
    run fails in seconds, not after an hour of S3 downloads."""
    problems = []
    for var in ("RAPID_SW", "RAPID_WORK", "ROMANTESSELLATIONDBNAME",
                "DBSERVER", "DBNAME", "DBUSER", "DBPASS"):
        if not os.environ.get(var):
            problems.append(f"environment variable {var} is not set")
    dbfile = os.environ.get("ROMANTESSELLATIONDBNAME")
    if dbfile and not os.path.isfile(dbfile):
        problems.append(f"ROMANTESSELLATIONDBNAME file missing: {dbfile}")
    if not os.path.isfile(CFORCEPSFAPER):
        problems.append(f"C module missing: {CFORCEPSFAPER} "
                        "(--run works only inside the pipeline container)")
    if not FP_SCRIPT.is_file():
        problems.append(f"FP backend script missing: {FP_SCRIPT}")
    if problems:
        raise SystemExit("cannot --run the FP backend:\n  "
                         + "\n  ".join(problems))


def choose_runs(positions, batch_sizes):
    """(field, [position, ...]) per requested batch size, all on the
    chip's dominant field (FP jobs are per field). Positions are picked
    evenly across the epoch-count range so each batch spans short,
    medium, and long coverage histories."""
    by_field = {}
    for position in positions:
        by_field.setdefault(position["field"], []).append(position)
    field, pool = max(by_field.items(), key=lambda item: len(item[1]))
    pool = sorted(pool, key=lambda p: (p.get("n_epochs", 0), p["aid"]))
    runs = []
    seen_sizes = set()
    for size in batch_sizes:
        size = min(size, len(pool))
        if size < 1 or size in seen_sizes:
            continue
        seen_sizes.add(size)
        if size == 1:
            picks = [pool[len(pool) // 2]]        # a median-history object
        else:
            indices = sorted({round(i * (len(pool) - 1) / (size - 1))
                              for i in range(size)})
            picks = [pool[i] for i in indices]
        runs.append((field, picks))
    return runs


_ELAPSED_RE = re.compile(
    r"^Elapsed time in seconds (.+?) =\s*([-+0-9.eE]+)", re.MULTILINE)


def parse_elapsed_phases(stdout_text):
    """The FP backend's own phase benchmarks, {label: seconds}. Labels
    are its literal print strings ("to determine input difference
    images" carries the S3 staging, "after executing cforcerpsfaper C
    module" the photometry)."""
    return {label.strip(): float(seconds)
            for label, seconds in _ELAPSED_RE.findall(stdout_text)}


def count_lc_rows(path):
    """(rows, valid_rows) of one rapid_req<reqid>_lc.txt: data lines
    after the column-name line, and those whose psfflux is not the
    no-measurement sentinel."""
    columns = None
    rows = valid = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if columns is None:
                columns = line.split()
                continue
            values = line.split()
            rows += 1
            try:
                if float(values[columns.index("psfflux")]) != FP_LC_SENTINEL:
                    valid += 1
            except (ValueError, IndexError):
                pass
    return rows, valid


def directory_bytes(path):
    return sum(entry.stat().st_size
               for entry in Path(path).rglob("*") if entry.is_file())


def run_forced_phot(field, picks, workdir, timeout, log):
    """Execute one FP backend job for `picks` (reqid := aid -- the
    convention proposed for alert integration) and record an fp_run.
    The scratch directory is kept for inspection."""
    scratch = tempfile.mkdtemp(prefix=f"fp_field{field}_n{len(picks)}_",
                               dir=workdir)
    csv_path = os.path.join(scratch, "input_sky_positions.txt")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["reqid", "ra", "dec"])
        for position in picks:
            writer.writerow([position["aid"], position["ra"],
                             position["dec"]])

    env = dict(os.environ,
               FIELD=str(field), SKYPOSITIONSCSVFILE=csv_path,
               PYTHONPATH=os.environ.get("RAPID_SW", str(REPO_ROOT)),
               PYTHONUNBUFFERED="1")
    stdout_path = os.path.join(scratch, "forcedPhotometryForField.out")
    logger.info("field %s: FP run with %d positions -> %s",
                field, len(picks), scratch)
    start = time.perf_counter()
    timed_out = False
    with open(stdout_path, "w", encoding="utf-8") as out:
        try:
            proc = subprocess.run(
                [sys.executable, str(FP_SCRIPT)], cwd=scratch, env=env,
                stdout=out, stderr=subprocess.STDOUT, timeout=timeout)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            exit_code = None
    wall_s = time.perf_counter() - start

    stdout_text = Path(stdout_path).read_text(encoding="utf-8",
                                              errors="replace")
    diffimglist = Path(scratch) / "diffimglist.txt"
    n_epochs = (len(diffimglist.read_text().splitlines())
                if diffimglist.is_file() else None)
    lightcurves = {}
    for position in picks:
        lc_path = Path(scratch) / f"rapid_req{position['aid']}_lc.txt"
        if lc_path.is_file():
            rows, valid = count_lc_rows(lc_path)
            lightcurves[str(position["aid"])] = {"rows": rows,
                                                 "valid": valid}
    record = {
        "kind": "fp_run",
        "field": field,
        "n_positions": len(picks),
        "aids": [p["aid"] for p in picks],
        "wall_s": round(wall_s, 3),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "phases_s": parse_elapsed_phases(stdout_text),
        "n_epochs": n_epochs,
        "staged_bytes": directory_bytes(scratch),
        "lightcurves": lightcurves,
        "scratch_dir": scratch,
    }
    log.write(record)
    logger.info("field %s: %d positions, %s epochs, %.1f s, exit %s",
                field, len(picks), n_epochs, wall_s, exit_code)
    return record


def is_completed_run(record):
    """True if an fp_run actually finished its photometry: exit 0, or the
    backend's documented warning codes (52-63), which still write full
    lightcurves. Crashes (1), catastrophic/DB errors (64, 255) and
    timeouts (exit_code None) must not feed the cost fit."""
    code = record.get("exit_code")
    return code is not None and (code == 0 or 52 <= code < 64)


def fit_linear(xs, ys):
    """(slope, intercept) least squares, or None with <2 distinct x --
    slope is the marginal per-position cost, the intercept the fixed
    per-job (staging) cost."""
    if len(set(xs)) < 2:
        return None
    n = len(xs)
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    slope = sxy / sxx
    return slope, mean_y - slope * mean_x


# ---------------------------------------------------------------------------
# running a benchmark
# ---------------------------------------------------------------------------

def benchmark_forced_phot(provider, pid, out_path, meta_extra=None,
                          run=False, batch_sizes=(1, 4, 16), workdir=None,
                          timeout=7200,
                          cone_radius=POSITION_CONE_RADIUS_DEG,
                          max_objects=None):
    """Survey one chip's FP workload and (with run=True) measure real FP
    backend runs; write the JSONL data product and return the summary."""
    fp_cfg = load_fp_config()
    log = TimingLog(out_path)
    try:
        log.write(collect_meta(pid=pid, mode="survey+run" if run
                               else "survey", fp_config=fp_cfg,
                               max_objects=max_objects,
                               **(meta_extra or {})))
        positions, fields, n_sources, n_unassociated, n_objects = survey(
            provider, pid, log, fp_cfg, cone_radius, max_objects)

        fp_runs = []
        if run:
            require_fp_environment()
            workdir = workdir or os.environ.get("RAPID_WORK") or None
            for field, picks in choose_runs(positions, batch_sizes):
                fp_runs.append(run_forced_phot(field, picks, workdir,
                                               timeout, log))

        epoch_counts = sorted(p["n_epochs"] for p in positions)
        prv_counts = sorted(p["n_prv"] for p in positions)
        completed = [r for r in fp_runs if is_completed_run(r)]
        fit = fit_linear([r["n_positions"] for r in completed],
                         [r["wall_s"] for r in completed])
        summary = {
            "kind": "summary",
            "n_sources": n_sources,
            "n_objects": n_objects,
            "n_surveyed": len(positions),
            "n_unassociated": n_unassociated,
            "epochs_median": percentile(epoch_counts, 0.50),
            "epochs_p95": percentile(epoch_counts, 0.95),
            "epochs_max": percentile(epoch_counts, 1.00),
            "epochs_total": sum(epoch_counts),
            "prv_median": percentile(prv_counts, 0.50),
            "prv_max": percentile(prv_counts, 1.00),
            "fields": {str(field): info for field, info in fields.items()},
            "runs": [{key: r[key] for key in
                      ("field", "n_positions", "n_epochs", "wall_s",
                       "exit_code", "staged_bytes")} for r in fp_runs],
            "fit_fixed_s": round(fit[1], 3) if fit else None,
            "fit_per_position_s": round(fit[0], 3) if fit else None,
        }
        log.write(summary)
    finally:
        log.close()
    for line in fp_summary_lines(summary):
        logger.info("%s", line)
    logger.info("benchmark written to %s", out_path)
    return summary


# ---------------------------------------------------------------------------
# reading benchmarks back: the numbers the team discussion needs
# ---------------------------------------------------------------------------

def fp_summary_lines(summary, meta=None):
    """Human-readable summary plus the decision aid derived from it."""
    lines = []
    if meta:
        lines.append(f"{meta['started_utc']}  {meta['host']} "
                     f"({meta['arch']})  pid={meta.get('pid')} "
                     f"mode={meta.get('mode')}")
    surveyed = summary.get("n_surveyed", summary["n_objects"])
    sample_note = (f", {surveyed} sampled for epoch counts"
                   if surveyed != summary["n_objects"] else "")
    lines.append(
        f"chip: {summary['n_sources']} triggering sources -> "
        f"{summary['n_objects']} objects "
        f"({summary['n_unassociated']} unassociated{sample_note})")
    lines.append(
        f"FP epochs per object: median {summary['epochs_median']}, "
        f"p95 {summary['epochs_p95']}, max {summary['epochs_max']} "
        f"(chip total {summary['epochs_total']}); "
        f"prv detections median {summary['prv_median']}, "
        f"max {summary['prv_max']}")
    for field, info in summary.get("fields", {}).items():
        lines.append(f"  field {field}: {info['n_objects']} objects, "
                     f"one FP job stages <= {info['job_epochs']} epochs")

    runs = summary.get("runs") or []
    for r in runs:
        staged = _mib(r.get("staged_bytes"))
        per_epoch = (round(r["wall_s"] / r["n_epochs"], 2)
                     if r.get("n_epochs") else None)
        lines.append(
            f"  measured: field {r['field']}, {r['n_positions']:3d} "
            f"positions, {r['n_epochs']} epochs -> {r['wall_s']} s "
            f"({per_epoch} s/epoch, {staged} MiB staged, "
            f"exit {r['exit_code']})")

    fixed = summary.get("fit_fixed_s")
    marginal = summary.get("fit_per_position_s")
    if fixed is not None:
        n = summary["n_objects"]
        one = fixed + marginal
        batched = fixed + marginal * n
        lines.append(f"cost model (this field): fixed {fixed} s per job "
                     f"+ {marginal} s per position")
        lines.append("decision aid:")
        lines.append(f"  alert-time FP, one request per alert: "
                     f"~{round(one, 1)} s added latency per alert; "
                     f"{round(one * n / 3600.0, 2)} h for this chip's "
                     f"{n} objects")
        lines.append(f"  alert-time FP, batched per chip: "
                     f"{round(batched / 3600.0, 2)} h "
                     f"({round(batched / max(n, 1), 1)} s/alert amortized)")
        lines.append(f"  stored FP, full re-run of this field "
                     f"({n} positions): {round(batched / 3600.0, 2)} h "
                     f"-- re-running every D days costs "
                     f"{round(batched / 3600.0, 2)}/D core-hours/day "
                     f"per field")
    elif runs:
        lines.append("cost model: need >= 2 completed runs with different "
                     "batch sizes to separate fixed from per-position cost")
    else:
        lines.append("no measured runs (survey only); re-run with --run "
                     "inside the pipeline container for cost numbers")
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
        if summary is None:
            print("  (no summary record -- crashed or still running?)")
            continue
        for line in fp_summary_lines(summary, meta):
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
        description="Survey (and optionally measure) the forced-photometry "
                    "workload behind one chip's alerts, for the alert-time "
                    "vs stored-FP decision (or run `benchmark_forced_phot.py "
                    "report FILE...` to summarize existing results)")
    parser.add_argument("--pid", type=int,
                        help="difference-image processing ID to survey")
    parser.add_argument("--exposure", type=int, metavar="EXPID",
                        help="resolve the newest vbest>0 processing "
                             "(needs --sca)")
    parser.add_argument("--sca", type=int)
    parser.add_argument("-o", "--out", default="fp_benchmark.jsonl",
                        help="JSONL output path (default: %(default)s)")
    parser.add_argument("--run", action="store_true",
                        help="also execute the real FP backend (expensive; "
                             "pipeline container only)")
    parser.add_argument("--batches", default="1,4,16",
                        help="comma-separated batch sizes for --run "
                             "(default: %(default)s)")
    parser.add_argument("--workdir",
                        help="parent directory for --run scratch dirs "
                             "(default: $RAPID_WORK, else system temp)")
    parser.add_argument("--timeout", type=int, default=7200,
                        help="per-FP-run timeout in seconds "
                             "(default: %(default)s)")
    parser.add_argument("--position-radius", type=float,
                        default=POSITION_CONE_RADIUS_DEG,
                        help="cone radius (deg) for per-position epoch "
                             "counting (default: %(default)s)")
    parser.add_argument("--max-objects", type=int, default=1000,
                        help="epoch-count at most this many objects, "
                             "sampled evenly across the chip (0 = all; "
                             "default: %(default)s)")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level))

    if (args.exposure is None) != (args.sca is None):
        parser.error("--exposure and --sca must be given together")
    if (args.pid is not None) == (args.exposure is not None):
        parser.error("give exactly one of --pid or --exposure/--sca")
    try:
        batch_sizes = [int(b) for b in args.batches.split(",") if b.strip()]
    except ValueError:
        parser.error(f"--batches must be comma-separated integers, "
                     f"not {args.batches!r}")

    from rapid_alerts.cli import make_provider
    provider = make_provider()
    pid = (args.pid if args.pid is not None
           else provider.resolve_pid(args.exposure, args.sca))

    benchmark_forced_phot(
        provider, pid, args.out,
        meta_extra={"expid": args.exposure, "sca": args.sca},
        run=args.run, batch_sizes=batch_sizes, workdir=args.workdir,
        timeout=args.timeout, cone_radius=args.position_radius,
        max_objects=args.max_objects or None)
    print(f"pid={pid}: forced-photometry benchmark -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
