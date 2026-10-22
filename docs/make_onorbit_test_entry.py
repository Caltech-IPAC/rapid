"""
Generate a dated test entry for docs/source/dev/onorbit_tests.rst from the STDOUT of a
Virtual Pipeline Operator (VPO) run, and optionally from the STDOUT of stream_l2_files.py
and ingestL2Files.py and from the per-job pipeline logs.

Example:

    python docs/make_onorbit_test_entry.py virtualPipelineOperator_20261021.out \
        --stream-log stream_l2_files_20261021.out --ingest-log ingestL2Files_20261021.out \
        --job-logs-dir job_logs_20261021
"""

import argparse
import re
import statistics
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path


DEFAULT_RST = Path(__file__).resolve().parent / "source" / "dev" / "onorbit_tests.rst"

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
    ("n_chips_ok", "Number of chips processed successfully"),
    ("n_chips_failed", "Number of chips failed"),
    ("n_alerts", "Number of alerts archived"),
    ("n_sources_dropped", "Number of sources dropped"),
    ("n_archive_bytes", "Total size of Avro alert archives, bytes"),
]

# Roman L2 filenames start with r<5-digit program ID><14 more digits>_<4 digits>_wfi<SCA>_<filter>
ROMAN_FILENAME_RE = re.compile(r"\br(\d{5})\d{14}_\d{4}_wfi(\d{2})_(f\d{3})", re.IGNORECASE)
PT_FORMAT = "%Y-%m-%dT%H:%M:%S"
SEC = r"([0-9.eE+-]+)"


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

    # Errors and warnings, grouped after replacing numbers so that per-job messages collapse together
    # (digits after a letter or "." are kept, so that e.g. s3:// and python3.11 survive, except in jidN/reqN).
    messages = Counter(re.sub(r"(?<![A-Za-z.])\d+|(?<=jid)\d+|(?<=req)\d+", "N", line.strip())[:160]
                       for line in block if line.startswith(("*** Error", "*** Warning")))
    vpo["messages"] = messages

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
        Summed counts, earliest start time, per-step elapsed times, and filter/SCA counts.
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
        Directory searched recursively for ``rapid_pipeline_job_*_jid*_log.txt`` files.

    Returns
    -------
    job_logs : dict
        For each of ``refimage`` and ``science``: per-step lists of seconds, per-jid total seconds,
        and the earliest start and latest end times.  ``n_incomplete`` counts logs with no total time.
    """
    step_re = re.compile(rf"^(?:=====> )?Elapsed time in seconds (after|just to|to) (.+?) =\s*{SEC}\s*$")
    totals = {"refimage": "run one instance of reference-image pipeline",
              "science": "run one instance of science pipeline"}
    job_logs = {kind: {"steps": {}, "totals": {}, "started": [], "ended": []} for kind in totals}
    job_logs["n_incomplete"] = 0

    for path in sorted(job_logs_dir.rglob("rapid_pipeline_job_*_jid*_log.txt")):
        text = path.read_text(errors="replace")
        kind = next((k for k, phrase in totals.items() if f"to {phrase} =" in text), None)
        if kind is None:
            job_logs["n_incomplete"] += 1
            continue
        jid = int(re.search(r"_jid(\d+)_log\.txt$", path.name).group(1))
        per_step = {}
        for line in text.splitlines():
            if m := step_re.match(line):
                step = m.group(2)
                label = "Total time to " + step if step == totals[kind] else step[0].upper() + step[1:]
                per_step[label] = per_step.get(label, 0.0) + float(m.group(3))
        for label, seconds in per_step.items():
            job_logs[kind]["steps"].setdefault(label, []).append(seconds)
        total = per_step["Total time to " + totals[kind]]
        job_logs[kind]["totals"][jid] = total
        started = parse_pt(first(r"^proc_pt_datetime_started = (.*)$", text.splitlines()))
        if started:
            job_logs[kind]["started"].append(started)
            job_logs[kind]["ended"].append(started + timedelta(seconds=total))
    return job_logs


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
    rows = [[step, f"{min(s):.1f}", f"{statistics.median(s):.1f}", f"{max(s):.1f}"]
            for step, s in steps.items()]
    return rst_table([title, "Min (sec)", "Med (sec)", "Max (sec)"], rows)


def make_entry(vpo, ingest=None, streams=None, job_logs=None, programs=()):
    """
    Assemble the reStructuredText test entry from the parsed logs.

    Parameters
    ----------
    vpo : dict
        Output of ``parse_vpo``.
    ingest : dict, optional
        Output of ``parse_ingest``.
    streams : list of dict, optional
        Outputs of ``parse_stream``.
    job_logs : dict, optional
        Output of ``parse_job_logs``.
    programs : list of int, optional
        Roman program IDs found in the logs.

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
                ["Elapsed time to ingest L2 files (sec)", f"{ingest['elapsed']:.1f}"]]
        add(rst_table(["Item", "Number"], rows) + "\n")
        if ingest["filters"]:
            by_filter = ", ".join(f"{f}: {n}" for f, n in sorted(ingest["filters"].items()))
            add(f"The ingested files cover {len(ingest['scas'])} SCAs; by filter, {by_filter}.\n")
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
    rows = [[label, f"{s:.1f}"] for label, s in vpo["stages"]]
    if n_checks:
        rows.append([f"Resubmitting failed AWS Batch jobs ({n_checks} checks)", f"{vpo['resubmit'][1]:.1f}"])
    if total is not None:
        rows.append(["Total time to run VPO", f"{total:.1f}"])
    else:
        rows.append(["Sum over VPO stages for this processing request",
                     f"{sum(s for _, s in vpo['stages']) + vpo['resubmit'][1]:.1f}"])
    add(rst_table(["VPO stage", "Elapsed time (sec)"], rows) + "\n")
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
            text.append(f"The summed run time over all {n} instance(s) is {sum(k['totals'].values()) / 3600:.1f} hours.\n")
            add("\n".join(text))
            add(step_table(f"{heading} step ({n} instance(s))", k["steps"]) + "\n")
        if job_logs["n_incomplete"]:
            add(f"{job_logs['n_incomplete']} job log(s) had no total run time (failed or unrecognized) and are not included above.\n")

    if vpo["alerts"]:
        add(f"Alert production\n{'-' * 34}\n")
        rows = [[label, fmt_int(vpo["alerts"][key])] for key, label in ALERT_LABELS]
        add(rst_table(["Item", "Number"], rows) + "\n")

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
                   help="directory with the per-job logs rapid_pipeline_job_<procdate>_jid<jid>_log.txt "
                        "(from s3://rapid-pipeline-logs/<procdate>/req<reqid>/)")
    p.add_argument("--reqid", type=int, default=None, help="processing request to report, if the VPO ran several")
    p.add_argument("--rst", type=Path, default=DEFAULT_RST, help=f"page to append to (default: {DEFAULT_RST})")
    p.add_argument("--stdout", action="store_true", help="print the entry instead of appending it")
    args = p.parse_args()

    vpo_text = args.vpo_log.read_text(errors="replace")
    ingest_texts = [path.read_text(errors="replace") for path in args.ingest_log]
    stream_texts = [path.read_text(errors="replace") for path in args.stream_log]

    entry = make_entry(
        parse_vpo(vpo_text, args.reqid),
        ingest=parse_ingest(ingest_texts) if ingest_texts else None,
        streams=[parse_stream(path) for path in args.stream_log],
        job_logs=parse_job_logs(args.job_logs_dir) if args.job_logs_dir else None,
        programs=program_ids([vpo_text] + ingest_texts + stream_texts),
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
