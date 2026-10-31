"""
Generate a dated test entry for docs/source/dev/onorbit_tests.rst from the STDOUT of a
Virtual Pipeline Operator (VPO) run, and optionally from the STDOUT of stream_l2_files.py
and ingestL2Files.py and from the per-job pipeline logs.

Example:

    python docs/make_onorbit_test_entry.py virtualPipelineOperator_20261021.out \
        --stream-log stream_l2_files_20261021.out --ingest-log ingestL2Files_20261021.out \
        --job-logs-dir job_logs_20261021 --count-db
"""

import argparse
import configparser
import json
import os
import re
import statistics
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path


DEFAULT_RST = Path(__file__).resolve().parent / "source" / "dev" / "onorbit_tests.rst"
# Configuration file the AWS Batch job launchers read; [JOB_PARAMS] job_logs_s3_bucket_base is the job-logs bucket.
DEFAULT_INI = Path(__file__).resolve().parent.parent / "cdf" / "awsBatchSubmitJobs_launchSingleSciencePipeline.ini"
# Script that counts the Merges, AstroObjects and AstroObjectsMeta records (and their X counterparts)
# for the fields of a processing date.
COUNT_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "count_astroobjects_merges_for_procdate.py"
COUNT_TABLES = ["Merges", "AstroObjects", "AstroObjectsMeta", "XMerges", "XAstroObjects", "XAstroObjectsMeta"]

# Doc labels for the stages timed by "VPO Elapsed time in seconds (to|after) <stage> = <sec>".
VPO_STAGE_LABELS = {
    "launch reference-image pipelines": "Launching reference-image pipelines",
    "wait for reference-image-pipeline AWS Batch jobs to finish": "Waiting for reference-image-pipeline AWS Batch jobs to finish",
    "register reference-image pipeline metadata into operations database": "Registering reference-image pipeline metadata into operations database",
    "launch science pipelines": "Launching science pipelines",
    "wait for science-pipeline AWS Batch jobs to finish": "Waiting for science-pipeline AWS Batch jobs to finish",
    "register science-pipeline metadata into operations database": "Registering science-pipeline metadata into operations database",
    "launching postproc pipelines": "Launching post-processing pipelines",
    "waiting for postproc-pipeline AWS Batch jobs to finish": "Waiting for post-processing AWS Batch jobs to finish",
    "registering postproc-pipeline metadata into operations database": "Registering post-processing metadata into operations database",
    "loading Sources database records": "Loading Sources database tables (PSF-fit SFFT catalogs)",
    "loading XSources database records": "Loading XSources database tables (SExtractor SFFT catalogs)",
    "crossmatching Sources and AstroObjects database records": "Cross-matching Sources with AstroObjects",
    "crossmatching XSources and XAstroObjects database records": "Cross-matching XSources with XAstroObjects",
    "computing statistics for AstroObjects database records": "Computing statistics for AstroObjects",
    "computing statistics for XAstroObjects database records": "Computing statistics for XAstroObjects",
    "deleting not-best Merges database records": "Deleting not-best Merges records",
    "deleting not-best XMerges database records": "Deleting not-best XMerges records",
    "producing alerts": "Producing alerts",
}

JOB_TYPE_LABELS = {
    "refimage": "Reference-image pipeline",
    "science": "Science pipeline",
    "postproc": "Post-processing pipeline",
}

ALERT_LABELS = [
    ("n_chips_ok", "Number of SCAs processed successfully"),
    ("n_chips_failed", "Number of SCAs failed"),
    ("n_alerts", "Number of alerts archived"),
    # produceAlertsForProcDate.py sums the per-chip n_failed (sources whose alert could not be built) into this key.
    ("n_sources_dropped", "Sources whose alert could not be built"),
    ("n_archive_bytes", "Total size of Avro alert archives, bytes"),
]

# Timing fields of the per-chip "forced photometry: ..." line of alerts/providers.py.
FORCED_PHOT_STEPS = ["read", "search", "stage+fit", "write", "total"]

# Roman L2 filenames start with r<5-digit program ID><14 more digits>_<4 digits>_wfi<SCA>_<filter>
ROMAN_FILENAME_RE = re.compile(r"\br(\d{5})\d{14}_\d{4}_wfi(\d{2})_(f\d{3})", re.IGNORECASE)
# Roman filter names by the f<NNN> code of the L2 filename.
FILTER_NAMES = {"f062": "R062", "f087": "Z087", "f106": "Y106", "f129": "J129", "f146": "W146",
                "f158": "H158", "f184": "F184", "f213": "K213"}
# SFFT difference-image catalogs that are real-bogus scored: (filename, source finder, sign).
RB_CATALOGS = [("sfftdiffimage_masked.txt", "SExtractor", "positive"),
               ("sfftdiffimage_masked_negative.txt", "SExtractor", "negative"),
               ("sfftdiffimage_masked_psfcat.txt", "photutils", "positive"),
               ("sfftdiffimage_masked_psfcat_negative.txt", "photutils", "negative")]
PT_FORMAT = "%Y-%m-%dT%H:%M:%S"
SEC = r"([0-9.eE+-]+)"


def filter_name(code):
    """
    Return the Roman filter name of the f<NNN> code of an L2 filename.

    Parameters
    ----------
    code : str
        Filter code from the filename, e.g. ``f158``.

    Returns
    -------
    name : str
        Filter name, e.g. ``H158``; the upper-cased code if the code is not known.
    """
    return FILTER_NAMES.get(code.lower(), code.upper())


def first(pattern, lines, default=None):
    """
    Return the first capture group of the first line that matches a regular expression.

    Parameters
    ----------
    pattern : str
        Regular expression with one capture group, matched at the start of each line.
    lines : list of str
        Lines to search.
    default : object, optional
        Value returned when no line matches.

    Returns
    -------
    value : str or object
        The captured text, or ``default``.
    """
    regex = re.compile(pattern)
    for line in lines:
        m = regex.match(line)
        if m:
            return m.group(1)
    return default


def last(pattern, lines, default=None):
    """
    Return the first capture group of the last line that matches a regular expression.

    Parameters
    ----------
    pattern : str
        Regular expression with one capture group, matched at the start of each line.
    lines : list of str
        Lines to search.
    default : object, optional
        Value returned when no line matches.

    Returns
    -------
    value : str or object
        The captured text, or ``default``.
    """
    return first(pattern, lines[::-1], default)


def parse_pt(value):
    """
    Parse a Pacific-time timestamp as printed by the RAPID scripts (e.g. ``2026-10-13T14:45:20 PT``).

    Parameters
    ----------
    value : str or None
        Timestamp text.

    Returns
    -------
    timestamp : datetime.datetime or None
        The parsed (naive, Pacific time) timestamp, or None if ``value`` is None.
    """
    if value is None:
        return None
    return datetime.strptime(value.replace(" PT", "").strip(), PT_FORMAT)


def rst_table(headers, rows, numeric_from=1):
    """
    Format rows as a reStructuredText simple table, with numeric columns right-justified.

    Parameters
    ----------
    headers : list of str
        Column headers.
    rows : list of list
        Table rows; each value is converted with ``str``.
    numeric_from : int, optional
        Index of the first right-justified column.

    Returns
    -------
    table : str
        The table text.
    """
    rows = [[str(v) for v in row] for row in rows]
    widths = [max(len(r[i]) for r in [headers] + rows) + 2 for i in range(len(headers))]
    border = "  ".join("=" * w for w in widths)

    def fmt(row):
        return "  ".join(v.rjust(w) if i >= numeric_from else v.ljust(w)
                         for i, (v, w) in enumerate(zip(row, widths))).rstrip()

    header = "  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip()
    return "\n".join([border, header, border] + [fmt(r) for r in rows] + [border])


def code_block(lines):
    """
    Format lines as a reStructuredText code block.

    Parameters
    ----------
    lines : list of str
        Lines of the block.

    Returns
    -------
    block : str
        The code-block directive and indented lines.
    """
    return ".. code-block::\n\n" + "\n".join("    " + line if line else "" for line in lines)


def program_ids(texts):
    """
    Collect the Roman program IDs of the L2 filenames that appear in some log texts.

    Parameters
    ----------
    texts : list of str
        Log file contents.

    Returns
    -------
    programs : list of int
        Sorted distinct program IDs.
    """
    return sorted({int(m.group(1)) for text in texts for m in ROMAN_FILENAME_RE.finditer(text)})


def job_logs_bucket(ini_path):
    """
    Read the S3 bucket of the per-job pipeline logs from the AWS Batch job-launcher configuration file.

    Parameters
    ----------
    ini_path : pathlib.Path
        Configuration file with a ``[JOB_PARAMS] job_logs_s3_bucket_base`` setting.

    Returns
    -------
    bucket : str
        The job-logs S3 bucket name.
    """
    config = configparser.ConfigParser()
    if not config.read(ini_path):
        raise SystemExit(f"Could not read configuration file {ini_path}")
    try:
        return config["JOB_PARAMS"]["job_logs_s3_bucket_base"]
    except KeyError:
        raise SystemExit(f"No [JOB_PARAMS] job_logs_s3_bucket_base in {ini_path}")


def normalize_message(line):
    """
    Replace the numbers in a log message by ``N`` so that per-job or per-chip messages collapse together.

    Numbers (with any decimals) are replaced; digits after a letter or "." are kept, so that e.g. s3:// and
    python3.11 survive, except in jidN and reqN.

    Parameters
    ----------
    line : str
        Log message.

    Returns
    -------
    message : str
        The normalized message, truncated to 160 characters.
    """
    return re.sub(r"(?<![A-Za-z.])\d+(?:\.\d+)?|(?<=jid)\d+|(?<=req)\d+", "N", line.strip())[:160]


def parse_vpo(text, reqid=None):
    """
    Extract the run settings, stage timings, job counts, failures and alert summary from VPO STDOUT.

    Parameters
    ----------
    text : str
        Contents of the VPO STDOUT file (which includes the STDOUT of the scripts it runs).
    reqid : int, optional
        Processing request to report; required when the VPO ran more than one (open loop).

    Returns
    -------
    vpo : dict
        Parsed values; keys are absent or None when the log does not contain them.
    """
    lines = text.splitlines()
    req_starts = [i for i, line in enumerate(lines) if re.match(r"^reqid = \d+$", line)]
    reqids = [int(lines[i].split("=")[1]) for i in req_starts]
    if len(reqids) > 1 and reqid is None:
        raise SystemExit(f"VPO log has processing requests {reqids}; choose one with --reqid")
    if reqid is not None and reqid not in reqids:
        raise SystemExit(f"--reqid {reqid} not found in VPO log (found {reqids})")

    if reqids:
        k = reqids.index(reqid) if reqid is not None else 0
        start = req_starts[k]
        end = req_starts[k + 1] if k + 1 < len(req_starts) else len(lines)
    else:
        start, end = 0, len(lines)
    before, block = lines[:start + 1], lines[start:end]

    vpo = {
        "swvers": first(r"^swvers = (\S+)", lines),
        "datearg": first(r"^datearg = (\d{8})", lines),
        "reqid": reqids[reqids.index(reqid) if reqid is not None else 0] if reqids else None,
        "n_requests": len(reqids),
        "dbname": first(r"^dbserver,dbname,dbport,dbuser = \S+ (\S+)", lines),
        "product_bucket": first(r"^product_s3_bucket_base = (\S+)", lines),
        "startrefimmjdobs": first(r"^STARTREFIMMJDOBS = (\S+)", lines),
        "endrefimmjdobs": first(r"^ENDREFIMMJDOBS = (\S+)", lines),
        "run_fid": first(r"^run_fid = (\S+)", block),
        "started": parse_pt(last(r"^proc_pt_datetime_started = (.*)$", before)),
        "total_seconds": last(rf"^VPO Elapsed total time in seconds to run VPO = {SEC}", lines),
        "exitcode": last(r"^Terminating: exitcode = (\S+)", lines),
    }
    open_loop = last(r"^Open-loop observation range: (.*)$", before)
    if open_loop:
        m = re.match(r"startdatetime = (.*?) , enddatetime = (.*)$", open_loop)
        vpo["startdatetime"], vpo["enddatetime"] = m.groups() if m else (None, None)
    else:
        vpo["startdatetime"] = first(r"^startdatetime = (.*)$", lines)
        vpo["enddatetime"] = first(r"^enddatetime = (.*)$", lines)
    if vpo["total_seconds"] is not None:
        vpo["total_seconds"] = float(vpo["total_seconds"])

    # Stage timings, with the resubmission checks summed into one row.
    stages, resubmit_seconds, n_resubmit_checks = [], 0.0, 0
    stage_re = re.compile(rf"^VPO Elapsed time in seconds (?:to|after) (.+?) = {SEC}$")
    for line in block:
        m = stage_re.match(line)
        if not m:
            continue
        stage, seconds = m.group(1), float(m.group(2))
        if stage.startswith("resubmit failed"):
            resubmit_seconds += seconds
            n_resubmit_checks += 1
        else:
            stages.append((VPO_STAGE_LABELS.get(stage, stage[0].upper() + stage[1:]), seconds))
    vpo["stages"] = stages
    vpo["resubmit"] = (n_resubmit_checks, resubmit_seconds)

    # AWS Batch job counts from the first wait for each job type.
    jobs, current = {}, None
    for line in block:
        m = re.match(r"^Waiting until AWS Batch jobs have finished for job_type=(\w+)", line)
        if m:
            current = m.group(1) if m.group(1) not in jobs else None
            if current:
                jobs[current] = {"ppid": None, "total": 0, "succeeded": 0, "failed": 0}
            continue
        if current is None:
            continue
        if line.startswith("Okay, all AWS Batch jobs have finished"):
            current = None
        elif m := re.match(r"^ppid = (\d+)$", line):
            jobs[current]["ppid"] = jobs[current]["ppid"] or m.group(1)
        elif m := re.match(r"^njobs_total = (\d+)$", line):
            jobs[current]["total"] = int(m.group(1))
        elif m := re.match(r"^n_succeeded,n_failed = (\d+),(\d+)$", line):
            jobs[current]["succeeded"], jobs[current]["failed"] = int(m.group(1)), int(m.group(2))
    vpo["jobs"] = jobs

    vpo["failed_jobs"] = re.findall(
        r"^Failed AWS Batch job: job_type=(\w+), jid=(\d+), .*?: (.*); (infrastructure failure|not resubmitted)$",
        "\n".join(block), re.MULTILINE)
    vpo["n_resubmitted"] = sum(line.startswith("Resubmitted AWS Batch job") for line in block)
    vpo["bad_exitcodes"] = [line for line in block
                            if re.match(r"^\*\*\* (Error|Warning): .* returned exit code = \d+", line)]

    vpo["messages"] = Counter(normalize_message(line) for line in block if line.startswith(("*** Error", "*** Warning")))

    if any(line.startswith("n_chips_ok = ") for line in block):
        vpo["alerts"] = {key: int(last(rf"^{key} = (\d+)$", block, 0)) for key, _ in ALERT_LABELS}
    else:
        vpo["alerts"] = None
    return vpo


def parse_ingest(texts):
    """
    Extract the work-list counts and per-step timings from ingestL2Files.py STDOUT.

    Parameters
    ----------
    texts : list of str
        Contents of one or more ingestL2Files.py STDOUT files.

    Returns
    -------
    ingest : dict
        Summed counts, earliest start time, per-step elapsed times, and filter/SCA/program counts.
    """
    count_patterns = {
        "n_input": r"^n_input_asdf_files = (\d+)$",
        "n_already": r"^n_already_ingested = (\d+)$",
        "n_redelivered": r"^n_redelivered = (\d+)$",
        "n_worklist": r"^Number of L2 files on the work list = (\d+)$",
        "n_ingested": r"^Number of L2 files ingested = (\d+)$",
        "n_failed": r"^Number of L2 files failed = (\d+)$",
        "n_not_attempted": r"^Number of L2 files not attempted = (\d+)$",
    }
    ingest = {key: 0 for key in count_patterns}
    ingest.update({"elapsed": 0.0, "steps": {}, "files": set(), "started": [], "exitcodes": []})
    step_re = re.compile(rf"^Elapsed time in seconds to (.*) = {SEC} \((.*)\)$")

    for text in texts:
        lines = text.splitlines()
        for key, pattern in count_patterns.items():
            ingest[key] += int(last(pattern, lines, 0))
        ingest["elapsed"] += float(last(rf"^Elapsed time in seconds to ingest L2 files = {SEC}", lines, 0.0))
        started = first(r"^proc_pt_datetime_started = (.*)$", lines)
        if started:
            ingest["started"].append(parse_pt(started))
        ingest["exitcodes"].append(last(r"^terminating_exitcode = (\S+)", lines))
        for line in lines:
            if m := step_re.match(line):
                step = re.sub(r" = [\d,]+", "", m.group(1))
                step = "Total time to ingest one L2 file" if step == "ingest L2 file" else step[0].upper() + step[1:]
                ingest["steps"].setdefault(step, []).append(float(m.group(2)))
            elif m := re.match(r"^Reading (\S+)\.\.\.$", line):
                ingest["files"].add(Path(m.group(1)).name)

    ingest["started"] = min(ingest["started"]) if ingest["started"] else None
    matches = [ROMAN_FILENAME_RE.search(f) for f in ingest["files"]]
    ingest["filters"] = Counter(m.group(3).upper() for m in matches if m)
    ingest["scas"] = {int(m.group(2)) for m in matches if m}
    ingest["programs"] = Counter(int(m.group(1)) for m in matches if m)
    return ingest


def parse_stream(path):
    """
    Extract the file counts and transfer time from one stream_l2_files.py STDOUT file.

    Parameters
    ----------
    path : pathlib.Path
        Path to the STDOUT file.

    Returns
    -------
    stream : dict
        Log name, number of files to download, number failed, and elapsed minutes.
    """
    lines = path.read_text(errors="replace").splitlines()
    n_files = int(last(r"^(\d+) to download$", lines, 0))
    return {
        "name": path.name,
        "n_files": n_files,
        "n_failed": sum(line.startswith("FAILED ") for line in lines),
        "elapsed_min": float(last(r"^Elapsed: ([0-9.]+) min$", lines, "nan")),
        "dryrun": n_files > 0 and len(ROMAN_FILENAME_RE.findall("\n".join(lines))) >= n_files,
    }


def parse_job_logs(job_logs_dir):
    """
    Collect per-step elapsed times from reference-image and science pipeline job logs.

    Parameters
    ----------
    job_logs_dir : pathlib.Path
        Directory whose top-level ``rapid_pipeline_job_*_log.txt`` files are read (subdirectories and other
        files are ignored); the pipeline type is recognized from each file's contents.

    Returns
    -------
    job_logs : dict
        For each of ``refimage`` and ``science``: per-step lists of seconds, per-jid total seconds,
        and the earliest start and latest end times.  ``n_incomplete`` counts job logs with no
        reference-image or science total time (failed or unfinished jobs).  ``rb`` maps each
        catalog filename to a Counter of its sources, real-bogus-scored sources, and not-scored reasons.
        ``science_programs`` counts the science-pipeline instances by the Roman program ID of the science image.
        ``catalog_stats`` lists, per science-pipeline catalog, a dict of ``jid``, ``sca``, ``filter``,
        ``catalog``, ``sources``, ``scored`` and ``above`` (None when the log predates the above-threshold count).
    """
    rb_re = re.compile(r"rb scored (\d+) of (\d+) sources(?:, (\d+) above threshold)? .*?not scored: \{(.*?)\}")
    step_re = re.compile(rf"^(?:=====> )?Elapsed time in seconds (after|just to|to) (.+?) =\s*{SEC}\s*$")
    totals = {"refimage": "run one instance of reference-image pipeline",
              "science": "run one instance of science pipeline"}
    job_logs = {kind: {"steps": {}, "totals": {}, "started": [], "ended": []} for kind in totals}
    job_logs["n_incomplete"] = 0
    job_logs["rb"] = {}
    job_logs["science_programs"] = Counter()
    job_logs["catalog_stats"] = []

    for path in sorted(p for p in job_logs_dir.glob("rapid_pipeline_job_*_log.txt") if p.is_file()):
        text = path.read_text(errors="replace")
        kind = next((k for k, phrase in totals.items() if f"to {phrase} =" in text), None)
        jid = re.search(r"jid(\d+)", path.name) or re.search(r"^jid = (\d+)$", text, re.MULTILINE)
        if kind is None or jid is None:
            job_logs["n_incomplete"] += 1
            continue
        jid = int(jid.group(1))
        image = ROMAN_FILENAME_RE.search(text)
        per_step, catalog = {}, None
        for line in text.splitlines():
            if m := step_re.match(line):
                step = m.group(2)
                label = "Total time to " + step if step == totals[kind] else step[0].upper() + step[1:]
                per_step[label] = per_step.get(label, 0.0) + float(m.group(3))
            elif m := re.search(r"catalog_filename = (\S+)", line):
                catalog = m.group(1)
            elif m := rb_re.search(line):
                rb = job_logs["rb"].setdefault(catalog, Counter())
                rb["scored"] += int(m.group(1))
                rb["sources"] += int(m.group(2))
                for reason, n in re.findall(r"'(\w+)': (\d+)", m.group(4)):
                    rb[reason] += int(n)
                if image:
                    job_logs["catalog_stats"].append(
                        {"jid": jid, "sca": int(image.group(2)), "filter": filter_name(image.group(3)),
                         "catalog": catalog, "sources": int(m.group(2)), "scored": int(m.group(1)),
                         "above": int(m.group(3)) if m.group(3) else None})
        for label, seconds in per_step.items():
            job_logs[kind]["steps"].setdefault(label, []).append(seconds)
        total = per_step["Total time to " + totals[kind]]
        job_logs[kind]["totals"][jid] = total
        if kind == "science" and image:
            job_logs["science_programs"][int(image.group(1))] += 1
        started = parse_pt(first(r"^proc_pt_datetime_started = (.*)$", text.splitlines()))
        if started:
            job_logs[kind]["started"].append(started)
            job_logs[kind]["ended"].append(started + timedelta(seconds=total))
    return job_logs


def parse_catalogs(products_dirs):
    """
    Count the sources, real-bogus-scored sources and sources above the real-bogus threshold in the
    SFFT difference-image catalogs of downloaded science-pipeline products.

    Parameters
    ----------
    products_dirs : list of pathlib.Path
        Directories with one ``jid<N>`` folder per science-pipeline instance (as in
        ``s3://<product_s3_bucket_base>/<procdate>/req<reqid>/``); only the ``RB_CATALOGS`` files and
        the science-image filename of each ``jid<N>`` folder are used.

    Returns
    -------
    catalog_stats : list of dict
        Per catalog, ``jid``, ``sca``, ``filter``, ``catalog``, ``sources``, ``scored`` and ``above``.
    """
    catalog_stats = []
    for jid_dir in sorted(d for products_dir in products_dirs for d in products_dir.glob("jid*") if d.is_dir()):
        image = next((m for f in jid_dir.glob("r*_wfi*_cal*") if (m := ROMAN_FILENAME_RE.search(f.name))), None)
        if image is None:
            continue
        for catalog, _, _ in RB_CATALOGS:
            path = jid_dir / catalog
            if not path.is_file():
                continue
            lines = path.read_text(errors="replace").splitlines()
            # SExtractor headers are "# <column number> <NAME>" (vector columns span numbers); photutils names are the first line.
            columns = {line.split()[2].lower(): int(line.split()[1]) - 1 for line in lines if line.startswith("#")}
            rows = [line.split() for line in lines if line.strip() and not line.startswith("#")]
            if not columns and rows:
                columns, rows = {name: i for i, name in enumerate(rows[0])}, rows[1:]
            if "rb_label" not in columns:
                continue
            labels = Counter(int(float(row[columns["rb_label"]])) for row in rows)
            catalog_stats.append({"jid": int(jid_dir.name[3:]), "sca": int(image.group(2)),
                                  "filter": filter_name(image.group(3)), "catalog": catalog, "sources": len(rows),
                                  "scored": len(rows) - labels[-1], "above": labels[1]})
    return catalog_stats


def merge_catalog_stats(from_logs, from_catalogs):
    """
    Combine the per-catalog counts from the job logs and from the catalogs, using the catalog counts
    where the job log has no above-threshold count or no entry.

    Parameters
    ----------
    from_logs : list of dict
        ``job_logs["catalog_stats"]`` from ``parse_job_logs``.
    from_catalogs : list of dict
        Output of ``parse_catalogs``.

    Returns
    -------
    catalog_stats : list of dict
        One dict per (jid, catalog).
    """
    merged = {(c["jid"], c["catalog"]): c for c in from_catalogs}
    for c in from_logs:
        key = (c["jid"], c["catalog"])
        if c["above"] is None and key in merged:
            continue
        merged[key] = c
    return list(merged.values())


def run_count_script(procdate, reqid, out_path):
    """
    Run count_astroobjects_merges_for_procdate.py for a processing date and request and save its STDOUT.

    Parameters
    ----------
    procdate : str
        Processing date (YYYYMMDD), passed as env. var. JOBPROCDATE.
    reqid : int or None
        Processing request, passed as env. var. PROCREQ when not None.
    out_path : pathlib.Path
        File to write the combined STDOUT and STDERR to.

    Returns
    -------
    text : str
        The script output.
    """
    env = dict(os.environ, JOBPROCDATE=procdate)
    if reqid is not None:
        env["PROCREQ"] = str(reqid)
    print(f"Running {COUNT_SCRIPT.name} with JOBPROCDATE={procdate}, PROCREQ={reqid}...", file=sys.stderr)
    result = subprocess.run([sys.executable, str(COUNT_SCRIPT)], env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out_path.write_text(result.stdout)
    if result.returncode != 0:
        raise SystemExit(f"{COUNT_SCRIPT.name} exited with code {result.returncode}; see {out_path}")
    print(f"Saved its output to {out_path}", file=sys.stderr)
    return result.stdout


def parse_counts(text):
    """
    Extract the per-table totals from count_astroobjects_merges_for_procdate.py STDOUT.

    Parameters
    ----------
    text : str
        Contents of the count script's STDOUT.

    Returns
    -------
    counts : dict
        For each table in ``COUNT_TABLES`` (e.g. ``Merges``, ``XAstroObjects``): a dict with the number of
        fields whose table exists (``nfields``), the summed record count (``n``), and the minimum,
        median and maximum per-field count (``per_field``, empty if no fields).
    """
    lines = text.splitlines()
    counts = {}
    for table in COUNT_TABLES:
        m = re.search(rf"^Totals for {table}: nfields_\w+,n_\w+ = (\d+),(\d+)$", text, re.MULTILINE)
        if m is None:
            raise SystemExit(f"No totals for {table} in the count script output")
        per_field = [int(m2.group(1)) for line in lines
                     if (m2 := re.match(rf"^{table}: field,count = \d+,(\d+)$", line))]
        counts[table] = {"nfields": int(m.group(1)), "n": int(m.group(2)), "per_field": per_field}
    return counts


def parse_alerts(work_dir, procdate):
    """
    Extract detection counts, forced-photometry timing, failures and warnings from the alert-production
    thread logs, and the per-chip statistics from the alert-production summary JSON file.

    Parameters
    ----------
    work_dir : pathlib.Path
        VPO working directory with ``produceAlertsForProcDate_thread*.out`` and
        ``produceAlertsForProcDate_<procdate>_summary.json``.
    procdate : str
        Processing date (YYYYMMDD).

    Returns
    -------
    alerts : dict or None
        Parsed values, or None if neither the thread logs nor the summary file exist.
    """
    thread_logs = sorted(work_dir.glob("produceAlertsForProcDate_thread*.out"))
    summary_path = work_dir / f"produceAlertsForProcDate_{procdate}_summary.json"
    if not thread_logs and not summary_path.exists():
        return None

    text = "\n".join(path.read_text(errors="replace") for path in thread_logs)
    capped = re.findall(r"keeping the (\d+) highest-S/N of (\d+) alertable detections; (\d+) dropped", text)
    forced = re.findall(r"forced photometry: \d+ objects, \d+ stored rows read; measured \d+ of \d+ images in "
                        r"the window \((\d+) new rows\); (\d+) month files written; read ([\d.]+) s, "
                        r"search ([\d.]+) s, stage\+fit ([\d.]+) s, write ([\d.]+) s, total ([\d.]+) s", text)
    alerts = {
        "n_thread_logs": len(thread_logs),
        "n_flagged": sum(int(n) for n in re.findall(r"pid=\d+: (\d+) flagged detections", text)),
        "max_sources": sorted({int(c[0]) for c in capped}),
        "n_capped_chips": len(capped),
        "n_capped_dropped": sum(int(c[2]) for c in capped),
        "forced_steps": {step: [float(f[i + 2]) for f in forced] for i, step in enumerate(FORCED_PHOT_STEPS)},
        "forced_new_rows": sum(int(f[0]) for f in forced),
        "forced_files": sum(int(f[1]) for f in forced),
        "forced_failures": re.findall(r"pid=(\d+): forced photometry failed after ([\d.]+) s", text),
        "n_tracebacks": text.count("Traceback (most recent call last):"),
        "messages": Counter(normalize_message(f"{m.group(1)} {m.group(2)}: {m.group(3)}") for m in
                            re.finditer(r"^\S+ \S+ (WARNING|ERROR) (\S+): (.*)$", text, re.MULTILINE)),
        "summary": None,
    }

    if summary_path.exists():
        summary = json.loads(summary_path.read_text())
        chips = [c for c in summary["chips"] if c.get("stats")]
        stats = [c["stats"] for c in chips]
        matches = {}
        for s in stats:
            for field, states in s["match_states"].items():
                for state, n in states.items():
                    matches.setdefault(field, Counter())[state] += n
        cutouts = Counter()
        for s in stats:
            cutouts.update(s["cutouts_present"])
        alerts["summary"] = {
            "totals": {key: summary[key] for key, _ in ALERT_LABELS if key in summary},
            "chip_seconds": [c["elapsed_seconds"] for c in chips],
            "n_with_prv": sum(s["n_with_prv"] for s in stats),
            "n_prv_total": sum(s["n_prv_total"] for s in stats),
            "max_prv": max((s["max_prv"] for s in stats), default=0),
            "n_with_forced": sum(s["n_with_forced"] for s in stats),
            "matches": matches,
            "cutouts": cutouts,
        }
    return alerts


def fmt_seconds(seconds):
    """
    Format an elapsed time in seconds, adding the time in hours when it is at least one hour.

    Parameters
    ----------
    seconds : float
        Elapsed time.

    Returns
    -------
    text : str
        For example ``176.5`` or ``57,015.3 (15.84 hours)``.
    """
    text = f"{seconds:,.1f}"
    return text + f" ({seconds / 3600:.2f} hours)" if seconds >= 3600 else text


def fmt_duration(seconds):
    """
    Format an elapsed time for running text, adding the time in hours when it is at least one hour.

    Parameters
    ----------
    seconds : float
        Elapsed time.

    Returns
    -------
    text : str
        For example ``176 seconds`` or ``57,015 seconds (15.84 hours)``.
    """
    text = f"{seconds:,.0f} seconds"
    return text + f" ({seconds / 3600:.2f} hours)" if seconds >= 3600 else text


def fmt_int(n):
    """
    Format an integer with thousands separators.

    Parameters
    ----------
    n : int
        Number to format.

    Returns
    -------
    text : str
        Formatted number.
    """
    return f"{n:,}"


def step_table(title, steps):
    """
    Format a Min/Med/Max table of per-step elapsed times.

    Parameters
    ----------
    title : str
        Header of the step column.
    steps : dict
        Step label mapped to a list of elapsed seconds, in display order.

    Returns
    -------
    table : str
        The reStructuredText table.
    """
    rows = [[step, fmt_seconds(min(s)), fmt_seconds(statistics.median(s)), fmt_seconds(max(s))]
            for step, s in steps.items()]
    return rst_table([title, "Min (sec)", "Med (sec)", "Max (sec)"], rows)


def rb_table(rb):
    """
    Format the real-bogus scoring counts of the science-pipeline catalogs as a sentence and table.

    Parameters
    ----------
    rb : dict
        Catalog filename mapped to a Counter of ``sources``, ``scored`` and not-scored reasons
        (``job_logs["rb"]`` from ``parse_job_logs``).

    Returns
    -------
    text : str
        The sentence and reStructuredText table.
    """
    reasons = sorted({key for c in rb.values() for key in c} - {"sources", "scored"})
    total = sum(rb.values(), Counter())
    rows = [[f"``{catalog}``", fmt_int(c["sources"]), fmt_int(c["scored"]),
             f"{100 * c['scored'] / c['sources']:.1f}" if c["sources"] else "--"]
            + [fmt_int(c[reason]) for reason in reasons]
            for catalog, c in sorted(rb.items(), key=lambda item: str(item[0]))]
    rows.append(["All catalogs", fmt_int(total["sources"]), fmt_int(total["scored"]),
                 f"{100 * total['scored'] / total['sources']:.1f}" if total["sources"] else "--"]
                + [fmt_int(total[reason]) for reason in reasons])
    text = ("Real-bogus scoring of the catalog sources, summed over the science-pipeline instances "
            "(the last columns count the sources not scored, by reason):\n\n")
    return text + rst_table(["Catalog", "Sources", "Scored", "Scored (%)"] + [f"Not scored: {r}" for r in reasons], rows)


def sca_filter_table(catalog_stats, catalog, value, fmt, reduce=statistics.median):
    """
    Format the median (or another reduction) over science images of a per-catalog value as a table
    with one row per SCA and one column per filter, plus an all-SCAs row and an all-filters column.

    Parameters
    ----------
    catalog_stats : list of dict
        Per-catalog counts (output of ``merge_catalog_stats``).
    catalog : str
        Catalog filename whose entries are used.
    value : callable
        Function of a catalog entry returning the value, or None when it cannot be computed.
    fmt : str
        Format specification of the reduced values, e.g. ``",.0f"``.
    reduce : callable, optional
        Function of the list of per-image values of an SCA/filter, e.g. ``len`` to count the images.

    Returns
    -------
    table : str or None
        The reStructuredText table, with ``--`` where an SCA/filter has no value; None if no entry has a value.
        The all-SCAs row and all-filters column reduce over all the images of that filter or SCA.
    """
    values = {}
    for c in catalog_stats:
        if c["catalog"] == catalog and (v := value(c)) is not None:
            values.setdefault((c["sca"], c["filter"]), []).append(v)
    if not values:
        return None
    filters = sorted({f for _, f in values}, key=lambda f: (f[1:], f))
    scas = sorted({sca for sca, _ in values})

    def cell(keys):
        pooled = [v for key in keys if key in values for v in values[key]]
        return format(reduce(pooled), fmt) if pooled else "--"

    rows = [[f"{sca:02d}"] + [cell([(sca, f)]) for f in filters] + [cell([(sca, f) for f in filters])]
            for sca in scas]
    rows.append(["All SCAs"] + [cell([(sca, f) for sca in scas]) for f in filters] + [cell(list(values))])
    return rst_table(["SCA"] + filters + ["All filters"], rows)


def sca_filter_section(catalog_stats):
    """
    Format the per-SCA, per-filter medians of the number of catalog sources, the percent of them that
    are real-bogus scored, and the percent of the scored sources above the real-bogus threshold.

    Parameters
    ----------
    catalog_stats : list of dict
        Per-catalog counts (output of ``merge_catalog_stats``).

    Returns
    -------
    text : str
        The section heading, sentences and reStructuredText tables.
    """
    out = [f"Sources and real-bogus scoring by SCA and filter\n{'-' * 48}\n",
           "Each value is the median over the science images of that SCA and filter (or, in the All SCAs row "
           "and All filters column, over all the images of that filter or SCA); ``--`` means no image "
           "(or, for the above-threshold percent, no image with that count). The SExtractor catalogs are "
           "``sfftdiffimage_masked[_negative].txt``; the photutils catalogs are "
           "``sfftdiffimage_masked_psfcat[_negative].txt``.\n",
           "Number of science images:\n",
           sca_filter_table(catalog_stats, RB_CATALOGS[0][0], lambda c: 1, ",d", reduce=len) + "\n"]
    metrics = [("Number of sources", lambda c: c["sources"], ",.0f"),
               ("Percent of sources real-bogus scored",
                lambda c: 100 * c["scored"] / c["sources"] if c["sources"] else None, ".1f"),
               ("Percent of scored sources above the real-bogus threshold",
                lambda c: 100 * c["above"] / c["scored"] if c["scored"] and c["above"] is not None else None, ".1f")]
    for title, value, fmt in metrics:
        tables = [(catalog, finder, sign, sca_filter_table(catalog_stats, catalog, value, fmt))
                  for catalog, finder, sign in RB_CATALOGS]
        if not any(table for *_, table in tables):
            out.append(f"{title}: not available (the job logs predate this count and no catalogs were read).\n")
        for catalog, finder, sign, table in tables:
            if table:
                out.append(f"{title}, {finder} catalog of the {sign} difference image (``{catalog}``):\n")
                out.append(table + "\n")
    return "\n".join(out)


def alert_section(vpo, alerts):
    """
    Format the alert-production section from the VPO STDOUT and, if available, the alert-production
    thread logs and summary file.

    Parameters
    ----------
    vpo : dict
        Output of ``parse_vpo``.
    alerts : dict or None
        Output of ``parse_alerts``.

    Returns
    -------
    section : str
        The section text, ending with a newline.
    """
    out = [f"Alert production\n{'-' * 34}\n"]
    add = out.append
    summary = alerts["summary"] if alerts else None
    totals = vpo["alerts"] or (summary["totals"] if summary else {})

    if summary and summary["chip_seconds"]:
        cs = summary["chip_seconds"]
        add(f"Per SCA, alert production took {min(cs):,.0f} to {fmt_duration(max(cs))} "
            f"(median {statistics.median(cs):,.0f} seconds); summed over the {len(cs)} SCAs, "
            f"{fmt_duration(sum(cs))}.\n")

    rows = []
    if alerts:
        rows.append(["Flagged detections skipped (flags <> 0, not alertable)", fmt_int(alerts["n_flagged"])])
        cap = "/".join(str(c) for c in alerts["max_sources"]) or "max_sources"
        rows.append([f"Alertable detections dropped by the cap of {cap} per SCA ({alerts['n_capped_chips']} SCAs)",
                     fmt_int(alerts["n_capped_dropped"])])
    rows += [[label, fmt_int(totals[key])] for key, label in ALERT_LABELS if key in totals]
    if summary:
        rows += [["Alerts with prior detections", fmt_int(summary["n_with_prv"])],
                 ["Prior-detection records in the alerts", fmt_int(summary["n_prv_total"])],
                 ["Most prior detections in one alert", fmt_int(summary["max_prv"])],
                 ["Alerts with forced photometry", fmt_int(summary["n_with_forced"])]]
    add(rst_table(["Item", "Number"], rows) + "\n")

    if summary and summary["matches"]:
        labels = {"ssMatches": "Solar-system objects", "refStarMatches": "Reference-catalog stars",
                  "refGalaxyMatches": "Reference-catalog galaxies", "nedMatches": "NED",
                  "lvsMatches": "NED-LVS", "gaiaMatches": "Gaia"}
        add("Cross-matches in the alerts (null means the match was not attempted):\n")
        rows = [[labels.get(field, field), fmt_int(c["matched"]), fmt_int(c["empty"]), fmt_int(c["null"])]
                for field, c in summary["matches"].items()]
        add(rst_table(["Cross-match", "Matched", "Empty", "Null"], rows) + "\n")
        cutouts = ", ".join(f"{field}: {fmt_int(n)}" for field, n in summary["cutouts"].items())
        add(f"Alerts with each cutout present: {cutouts}.\n")

    if alerts and alerts["forced_steps"]["total"]:
        steps = alerts["forced_steps"]
        n = len(steps["total"])
        text = (f"Forced photometry ran on {n} SCAs, writing {fmt_int(alerts['forced_new_rows'])} new rows "
                f"to {fmt_int(alerts['forced_files'])} files.")
        if summary and summary["chip_seconds"]:
            text += (f" It took {100 * sum(steps['total']) / sum(summary['chip_seconds']):.0f}% of the summed "
                     "per-SCA alert-production time.")
        add(text + "\n")
        rows = [[step, fmt_seconds(sum(steps[step])), fmt_seconds(statistics.median(steps[step])),
                 fmt_seconds(max(steps[step]))] for step in FORCED_PHOT_STEPS]
        add(rst_table(["Forced-photometry step", "Summed (sec)", "Med per SCA (sec)", "Max per SCA (sec)"], rows) + "\n")
    if alerts and alerts["forced_failures"]:
        failed = ", ".join(f"pid = {pid} after {sec} s" for pid, sec in alerts["forced_failures"])
        add(f"Forced photometry failed on {len(alerts['forced_failures'])} SCA(s) ({failed}); "
            "the alerts of those SCAs carry null ``prvDiaForcedSources``.\n")

    if alerts and alerts["messages"]:
        add(f"Warning and error messages in the {alerts['n_thread_logs']} alert-production thread logs "
            f"(numbers replaced by ``N``; {alerts['n_tracebacks']} tracebacks), most frequent first:\n")
        add(code_block([f"{n:6d}  {msg}" for msg, n in alerts["messages"].most_common(15)]) + "\n")
    return "\n".join(out)


def make_entry(vpo, ingest=None, streams=None, job_logs=None, programs=(), logs_bucket=None, counts=None,
               alerts=None, catalog_stats=None):
    """
    Assemble the reStructuredText test entry from the parsed logs.

    Parameters
    ----------
    vpo : dict
        Output of ``parse_vpo``.
    logs_bucket : str, optional
        S3 bucket of the per-job pipeline logs (from ``job_logs_bucket``).
    ingest : dict, optional
        Output of ``parse_ingest``.
    streams : list of dict, optional
        Outputs of ``parse_stream``.
    job_logs : dict, optional
        Output of ``parse_job_logs``.
    programs : list of int, optional
        Roman program IDs found in the logs.
    counts : dict, optional
        Output of ``parse_counts``.
    alerts : dict, optional
        Output of ``parse_alerts``.
    catalog_stats : list of dict, optional
        Per-catalog source and real-bogus counts (output of ``merge_catalog_stats``).

    Returns
    -------
    entry : str
        The dated section, ending with a newline.
    """
    out = []
    add = out.append

    procdate = vpo["datearg"]
    if procdate:
        d = datetime.strptime(procdate, "%Y%m%d")
        title = f"{d.month}/{d.day}/{d.year}"
    else:
        title = "TODO: date"
    add(f"{title}\n{'*' * 36}\n")
    add(".. TODO: Describe the purpose of this test, the data used, and the differences from the previous test.\n")
    if programs:
        add(f"Roman program ID(s) of the L2 files: {', '.join(f'``{p}``' for p in programs)} (from the L2 filenames).\n")
    else:
        add(".. TODO: Roman program ID (no L2 filenames were found in the logs).\n")

    if streams:
        add(f"L2 file streaming\n{'-' * 34}\n")
        add("The L2 files were streamed from MAST into the ingest S3 bucket with ``stream_l2_files.py``:\n")
        rows = [[s["name"] + (" (dry run)" if s["dryrun"] else ""), fmt_int(s["n_files"]), fmt_int(s["n_failed"]),
                 f"{s['elapsed_min']:.2f}",
                 f"{s['elapsed_min'] / s['n_files']:.3f}" if s["n_files"] else "--"] for s in streams]
        add(rst_table(["stream_l2_files.py STDOUT", "Files", "Failed", "Elapsed (min)", "Min per file"], rows) + "\n")

    if ingest:
        add(f"L2 file ingest\n{'-' * 34}\n")
        text = "The L2 files were converted to FITS and registered in the database with ``ingestL2Files.py``"
        if ingest["started"]:
            text += f", starting at {ingest['started'].strftime(PT_FORMAT)} PT"
        add(text + ".\n")
        rows = [["ASDF files in the ingest S3 bucket", fmt_int(ingest["n_input"])],
                ["Already ingested (skipped)", fmt_int(ingest["n_already"])],
                ["Redelivered (re-ingested)", fmt_int(ingest["n_redelivered"])],
                ["L2 files on the work list", fmt_int(ingest["n_worklist"])],
                ["L2 files ingested", fmt_int(ingest["n_ingested"])],
                ["L2 files failed", fmt_int(ingest["n_failed"])],
                ["L2 files not attempted", fmt_int(ingest["n_not_attempted"])],
                ["Elapsed time to ingest L2 files (sec)", fmt_seconds(ingest["elapsed"])]]
        add(rst_table(["Item", "Number"], rows) + "\n")
        if ingest["filters"]:
            by_filter = ", ".join(f"{f}: {n}" for f, n in sorted(ingest["filters"].items()))
            by_program = ", ".join(f"``{p}``: {fmt_int(n)}" for p, n in sorted(ingest["programs"].items()))
            add(f"The ingested files cover {len(ingest['scas'])} SCAs; by filter, {by_filter}; "
                f"by Roman program ID, {by_program}.\n")
        if ingest["steps"]:
            add(step_table("Ingest step (per L2 file)", ingest["steps"]) + "\n")
        bad = [c for c in ingest["exitcodes"] if c not in ("0", None)]
        if bad:
            add(f"``ingestL2Files.py`` terminated with exit code(s) {', '.join(bad)}.\n")

    add("Here are details about how the test was executed via the Virtual Pipeline Operator (VPO)"
        + (f", version {vpo['swvers']}:\n" if vpo["swvers"] else ":\n"))
    env = [f"export DBNAME={vpo['dbname'] or 'TODO'}",
           f'export STARTDATETIME="{vpo["startdatetime"] or "TODO"}"',
           f'export ENDDATETIME="{vpo["enddatetime"] or "TODO"}"',
           f"export STARTREFIMMJDOBS={vpo['startrefimmjdobs'] or 'TODO'}",
           f"export ENDREFIMMJDOBS={vpo['endrefimmjdobs'] or 'TODO'}",
           f"export RUNFID={vpo['run_fid'] or 'TODO'}",
           "",
           f"python3.11 /code/pipeline/virtualPipelineOperator.py {procdate} >& virtualPipelineOperator_{procdate}.out &"]
    add(code_block(env) + "\n")

    if vpo["reqid"] is not None:
        add(f"The VPO run was registered as processing request ``reqid = {vpo['reqid']}``.")
        add("The pipeline product files are located in the usual place:\n")
        add(code_block([f"s3://{vpo['product_bucket'] or 'TODO'}/{procdate}/req{vpo['reqid']}/"]) + "\n")
        if logs_bucket:
            add("The per-job pipeline logs are located here:\n")
            add(code_block([f"s3://{logs_bucket}/{procdate}/req{vpo['reqid']}/"]) + "\n")

    if vpo["jobs"]:
        add("Here is a summary of the pipeline instances run on AWS Batch:\n")
        rows = [[f"{JOB_TYPE_LABELS.get(jt, jt)} (``ppid = {j['ppid']}``)" if j["ppid"] else JOB_TYPE_LABELS.get(jt, jt),
                 fmt_int(j["total"]), fmt_int(j["succeeded"]), fmt_int(j["failed"])]
                for jt, j in vpo["jobs"].items()]
        add(rst_table(["Pipeline", "Instances", "Succeeded", "Failed"], rows) + "\n")

    n_checks = vpo["resubmit"][0]
    if vpo["failed_jobs"]:
        add(f"{len(vpo['failed_jobs'])} AWS Batch job(s) failed, and {vpo['n_resubmitted']} were resubmitted:\n")
        add(code_block([f"job_type={jt}, jid={jid}: {reason}; {disp}"
                        for jt, jid, reason, disp in vpo["failed_jobs"]]) + "\n")
    elif n_checks:
        add("No AWS Batch jobs needed to be resubmitted.\n")

    if vpo["bad_exitcodes"]:
        add("These VPO steps returned a nonzero exit code:\n")
        add(code_block(vpo["bad_exitcodes"]) + "\n")

    if vpo["messages"]:
        add("Error and warning messages in the VPO STDOUT (numbers replaced by ``N``), most frequent first:\n")
        add(code_block([f"{n:6d}  {msg}" for msg, n in vpo["messages"].most_common(15)]) + "\n")
    if vpo["exitcode"] is not None:
        add(f"The VPO terminated with ``exitcode = {vpo['exitcode']}``.\n")

    add(f"Overall timing\n{'-' * 34}\n")
    total = vpo["total_seconds"] if vpo["n_requests"] <= 1 else None
    if vpo["started"] and total is not None:
        ended = vpo["started"] + timedelta(seconds=total)
        add(f"The VPO started at {vpo['started'].strftime(PT_FORMAT)} PT and finished at "
            f"{ended.strftime('%H:%M:%S')} PT, a total of {total:,.0f} seconds ({total / 3600:.2f} hours).")
    add("The breakdown by VPO stage is shown below.\n")
    rows = [[label, fmt_seconds(s)] for label, s in vpo["stages"]]
    if n_checks:
        rows.append([f"Resubmitting failed AWS Batch jobs ({n_checks} checks)", fmt_seconds(vpo["resubmit"][1])])
    stage_sum = sum(s for _, s in vpo["stages"]) + vpo["resubmit"][1]
    if total is not None:
        rows.append(["Total time to run VPO", fmt_seconds(total)])
    else:
        rows.append(["Sum over VPO stages for this processing request", fmt_seconds(stage_sum)])
    add(rst_table(["VPO stage", "Elapsed time (sec)"], rows) + "\n")
    if vpo["stages"]:
        label, longest = max(vpo["stages"], key=lambda stage: stage[1])
        add(f"The longest stage was {label[0].lower() + label[1:]}, at {fmt_duration(longest)}, "
            f"{100 * longest / (total or stage_sum):.0f}% of the total.\n")
    n_science = vpo["jobs"].get("science", {}).get("total", 0)
    if total is not None and n_science:
        add(f"For the {n_science} science images, this is {total / n_science:,.1f} seconds per input science image overall.\n")

    if job_logs:
        for kind, heading in (("refimage", "Reference-image pipeline"), ("science", "Science pipeline")):
            k = job_logs[kind]
            if not k["totals"]:
                continue
            n = len(k["totals"])
            add(f"{heading}\n{'-' * 34}\n")
            text = []
            if k["started"]:
                wall = (max(k["ended"]) - min(k["started"])).total_seconds()
                text.append(f"The {n} {heading.lower()} instance(s) ran from {min(k['started']).strftime('%H:%M:%S')} to "
                            f"{max(k['ended']).strftime('%H:%M:%S')} PT ({wall / 3600:.2f} hours of wall-clock time).")
            slow = max(k["totals"], key=k["totals"].get)
            fast = min(k["totals"], key=k["totals"].get)
            text.append(f"The slowest instance (jid = {slow}) took {k['totals'][slow]:,.0f} seconds; "
                        f"the fastest (jid = {fast}) took {k['totals'][fast]:,.0f} seconds.")
            text.append(f"The summed run time over all {n} instance(s) is {sum(k['totals'].values()) / 3600:.1f} hours.")
            if kind == "science" and job_logs["science_programs"]:
                by_program = ", ".join(f"``{p}``: {fmt_int(c)}" for p, c in sorted(job_logs["science_programs"].items()))
                text.append(f"Science images processed by Roman program ID: {by_program}.")
            text[-1] += "\n"
            add("\n".join(text))
            add(step_table(f"{heading} step ({n} instance(s))", k["steps"]) + "\n")
            if kind == "science" and job_logs["rb"]:
                add(rb_table(job_logs["rb"]) + "\n")
        if job_logs["n_incomplete"]:
            add(f"{job_logs['n_incomplete']} job log(s) had no total run time (failed or unfinished jobs) and are not included above.\n")

    if catalog_stats:
        add(sca_filter_section(catalog_stats))

    if counts:
        add(f"Lightcurve database\n{'-' * 34}\n")
        add("Records in the per-field lightcurve database tables for the fields covered by this processing date, "
            "from ``scripts/count_astroobjects_merges_for_procdate.py`` "
            "(the counts include records from other processing dates of the same fields):\n")
        rows = []
        for table in COUNT_TABLES:
            c = counts[table]
            pf = c["per_field"]
            rows.append([f"{table}_<field>", fmt_int(c["nfields"]), fmt_int(c["n"])]
                        + ([fmt_int(min(pf)), fmt_int(round(statistics.median(pf))), fmt_int(max(pf))]
                           if pf else ["--"] * 3))
        add(rst_table(["Table", "Fields", "Records", "Min per field", "Med per field", "Max per field"], rows) + "\n")
        for x in ("", "X"):
            n_ao, n_meta = counts[f"{x}AstroObjects"]["n"], counts[f"{x}AstroObjectsMeta"]["n"]
            if n_ao != n_meta:
                add(f"{x}AstroObjects and {x}AstroObjectsMeta differ by {fmt_int(abs(n_ao - n_meta))} records.\n")

    if vpo["alerts"] or alerts:
        add(alert_section(vpo, alerts))

    add(".. TODO: Observations.\n")
    return "\n".join(out) + "\n"


def main():
    """
    Parse the command line, build the test entry, and append it to the docs page or print it.
    """
    p = argparse.ArgumentParser(description="Generate an on-orbit test entry for docs/source/dev/onorbit_tests.rst")
    p.add_argument("vpo_log", type=Path, help="STDOUT of virtualPipelineOperator.py")
    p.add_argument("--ingest-log", type=Path, nargs="+", default=[], help="STDOUT of ingestL2Files.py (one or more)")
    p.add_argument("--stream-log", type=Path, nargs="+", default=[], help="STDOUT of stream_l2_files.py (one or more)")
    p.add_argument("--job-logs-dir", type=Path, default=None,
                   help="directory with the per-job STDOUT files (rapid_pipeline_job_<procdate>_jid<jid>_log.txt "
                        "from s3://<job_logs_s3_bucket_base>/<procdate>/req<reqid>/); only those files at "
                        "the top level are read")
    p.add_argument("--ini", type=Path, default=DEFAULT_INI,
                   help=f"job-launcher configuration file with [JOB_PARAMS] job_logs_s3_bucket_base (default: {DEFAULT_INI})")
    p.add_argument("--products-dir", type=Path, nargs="+", default=[],
                   help="directories with one jid<jid> folder of downloaded science-pipeline products each (from "
                        "s3://<product_s3_bucket_base>/<procdate>/req<reqid>/); their SFFT difference-image catalogs "
                        "give the real-bogus counts where the job logs do not")
    p.add_argument("--work-dir", type=Path, default=None,
                   help="VPO working directory with produceAlertsForProcDate_thread*.out and "
                        "produceAlertsForProcDate_<procdate>_summary.json")
    count = p.add_mutually_exclusive_group()
    count.add_argument("--count-db", action="store_true",
                       help=f"run {COUNT_SCRIPT.name} for the VPO processing date (needs the DB* env. vars., RAPID_SW, "
                            "RAPID_WORK and ROMANTESSELLATIONDBNAME, as in the VPO container) and save its STDOUT "
                            "next to the VPO log")
    count.add_argument("--count-log", type=Path, default=None,
                       help=f"saved STDOUT of {COUNT_SCRIPT.name} to read instead of running it")
    p.add_argument("--reqid", type=int, default=None, help="processing request to report, if the VPO ran several")
    p.add_argument("--rst", type=Path, default=DEFAULT_RST, help=f"page to append to (default: {DEFAULT_RST})")
    p.add_argument("--stdout", action="store_true", help="print the entry instead of appending it")
    args = p.parse_args()

    vpo_text = args.vpo_log.read_text(errors="replace")
    ingest_texts = [path.read_text(errors="replace") for path in args.ingest_log]
    stream_texts = [path.read_text(errors="replace") for path in args.stream_log]

    vpo = parse_vpo(vpo_text, args.reqid)
    if args.count_db:
        if not vpo["datearg"]:
            raise SystemExit("No processing date (datearg) in the VPO log, so the count script cannot be run")
        count_path = args.vpo_log.parent / f"count_astroobjects_merges_for_procdate_{vpo['datearg']}_req{vpo['reqid']}.out"
        count_text = run_count_script(vpo["datearg"], vpo["reqid"], count_path)
    elif args.count_log:
        count_text = args.count_log.read_text(errors="replace")
    else:
        count_text = None

    job_logs = parse_job_logs(args.job_logs_dir) if args.job_logs_dir else None
    catalog_stats = merge_catalog_stats(job_logs["catalog_stats"] if job_logs else [], parse_catalogs(args.products_dir))

    entry = make_entry(
        vpo,
        ingest=parse_ingest(ingest_texts) if ingest_texts else None,
        streams=[parse_stream(path) for path in args.stream_log],
        job_logs=job_logs,
        programs=program_ids([vpo_text] + ingest_texts + stream_texts),
        logs_bucket=job_logs_bucket(args.ini),
        counts=parse_counts(count_text) if count_text is not None else None,
        alerts=parse_alerts(args.work_dir, vpo["datearg"]) if args.work_dir else None,
        catalog_stats=catalog_stats,
    )

    if args.stdout:
        print(entry, end="")
    else:
        existing = args.rst.read_text()
        with open(args.rst, "a") as f:
            f.write(("" if existing.endswith("\n\n") else "\n" if existing.endswith("\n") else "\n\n") + entry)
        print(f"Appended entry to {args.rst}")


if __name__ == "__main__":
    main()
