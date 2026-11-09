"""Where one run's hours go, from its records alone: the throughput report (throughput-metrics).

    scripts/throughput_report.py --run <id> --batch-records FILE [--out FILE]

Inputs, nothing else: the database's ``job_metrics`` view, the run's
``jobs`` rows (their ``attempts``, keyed by Batch job id) and
``job_inputs`` (the critical path's edges), and Batch's own record of every
Batch job of the run and of every other run's job that shared a host with
it (``describe-jobs``: ``createdAt``, ``startedAt``, ``stoppedAt``, the
container instance, the job's vCPU). Batch keeps a job for a limited time,
so the records are saved to ``--batch-records`` on the first read and read
back from it after; a committed report names its file. A host's vCPU count
is the job's ``host_vcpus`` metric where the job wrote one, else EC2's
``describe-instance-types`` for the type the job recorded.

What it prints, in Markdown, for the run's published jobs:

- **Job vCPU-hours** (the historical view, the exercise's ranking): each
  job's ``container_seconds`` times its Batch vCPU reservation, split by
  the stage's named timers (difference ``rb_s``, ``xsources_s``,
  ``refpsf_s``, ``gzip_s``, ``pixel_columns_s``; packet ``packet_*_s``),
  the rest of each job "untimed"; ranked by share of the run's total.
- **Steps**: the same seconds split by the ``step_<name>_s`` metrics
  (:mod:`rapidpipe.foundation.steps`) where the jobs wrote them, the
  remainder stated, the S3 bytes and the CPU seconds against wall
  (``cgroup_cpu_s`` / ``cgroup_cpu_wall_s``); absent on older runs.
- **Host-hours**: a host is a container instance. Its hours are the span
  from its first job's start to its last job's stop among every job the
  records place on it (the run's and other runs', which is the coverage
  line), so boot and idle time outside that span is not counted. At every
  moment the host's time is split equally among the jobs running on it
  (time-weighted co-tenancy); moments with none are the host's idle share.
  A job's host-hours are split across its steps (or timers) in proportion
  to their seconds. The ranking by host-hours sits beside the vCPU one.
- **Co-tenancy** per host and per instance type: mean and largest number
  of jobs running at once while the host was occupied.
- **Queue wait** per stage, Batch's ``createdAt`` to ``startedAt``.
- **Fixed against per-source cost**: a job's fixed seconds are its
  ``startup_s`` and the steps :data:`FIXED_STEPS` names (input reads, model
  and catalog loads); the rest over its sources (difference: the scored
  candidates ``n_rb_scored`` + ``n_rb_scored_negative``; packet:
  ``n_packets``). Absent where the jobs wrote no steps.
- **The critical path of one exposure**: the packet job of median
  end-to-end time, back through each job's gating upstream job: of its
  ``job_inputs.source_job`` jobs that stopped before it was submitted, the
  last to stop (an input whose job was still running, such as an earlier
  epoch's difference read for association, did not hold it back); each
  hop's gap, queue wait and run.
- **Leads**: the largest host-hour items and what the numbers say of idle
  hosts and queue wait, ranked.

A metric a run's jobs did not write is printed "absent", never zero.
The database is named as for every rapidpipe command; Batch and EC2 are
read with the environment's AWS credentials.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

#: The stage timers of the historical view (stage-timing, rapid PR 550), in the exercise's order.
TIMERS = {"difference": ["rb_s", "xsources_s", "refpsf_s", "gzip_s", "pixel_columns_s"],
          "packet": ["packet_read_s", "packet_forced_s", "packet_matching_s", "packet_cutouts_s",
                     "packet_assembly_s"]}
#: The steps counted as a job's fixed cost: what it reads and loads before its sources matter.
FIXED_STEPS = ("step_read_inputs_s", "step_rb_model_load_s", "step_stage_inputs_s", "step_read_s",
               "step_s3_read_s", "step_catalog_mirror_s", "step_crds_fetch_s", "step_distortion_s")
#: A stage's source count for the per-source cost.
SOURCES = {"difference": ("n_rb_scored", "n_rb_scored_negative"), "packet": ("n_packets",)}


def _t(value) -> dt.datetime | None:
    """A Batch epoch-milliseconds or an ISO string as an aware datetime."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value / 1000, dt.timezone.utc)
    return dt.datetime.fromisoformat(value)


def pct(part: float, whole: float) -> str:
    return f"{100 * part / whole:.1f} %" if whole else "absent"


def median(values: list[float]):
    return statistics.median(values) if values else None


def fmt(value, digits: int = 1) -> str:
    if value is None:
        return "absent"
    return f"{value:,.{digits}f}"


# ----------------------------------------------------------------------
# The records, read once
# ----------------------------------------------------------------------


def read_run(conn, run: int) -> dict:
    """The run's jobs (stage, state, scope, attempts), metrics by job, and job_inputs edges."""
    with conn.cursor() as cur:
        cur.execute("SELECT r.dataset, r.revision, r.survey FROM runs r WHERE r.id = %s", (run,))
        dataset, revision, survey = cur.fetchone()
        cur.execute("SELECT id, stage, state, scope, attempts, instance_type FROM jobs WHERE run = %s",
                    (run,))
        jobs = {r[0]: {"id": r[0], "stage": r[1], "state": r[2], "scope": r[3],
                       "attempts": r[4] or {}, "instance_type": r[5]} for r in cur.fetchall()}
        cur.execute("SELECT job, metric, value FROM job_metrics WHERE run = %s", (run,))
        for job, metric, value in cur.fetchall():
            jobs[job].setdefault("metrics", {})[metric] = value
        cur.execute("SELECT i.job, i.source_job FROM job_inputs i JOIN jobs j ON j.id = i.job"
                    " WHERE j.run = %s AND i.source_job IS NOT NULL", (run,))
        edges = defaultdict(set)
        for job, source in cur.fetchall():
            edges[job].add(source)
    conn.commit()
    return {"run": run, "dataset": dataset, "revision": revision, "survey": survey,
            "jobs": jobs, "edges": {k: sorted(v) for k, v in edges.items()}}


def neighbour_batch_ids(conn, run: int, start: dt.datetime, end: dt.datetime) -> dict[str, dict]:
    """Every other run's Batch submission whose recorded span overlaps ``start`` to ``end``."""
    with conn.cursor() as cur:
        cur.execute("SELECT j.id, j.run, j.stage, a.key, a.value FROM jobs j"
                    " CROSS JOIN LATERAL jsonb_each(j.attempts) a"
                    " WHERE j.run <> %s AND a.value ? 'started'"
                    " AND (a.value ->> 'started')::timestamptz < %s"
                    " AND coalesce(a.value ->> 'ended', a.value ->> 'stopped',"
                    "              a.value ->> 'hedged', 'infinity')::timestamptz > %s",
                    (run, end, start))
        rows = cur.fetchall()
    conn.commit()
    return {bid: {"job": job, "run": r, "stage": stage, **entry} for job, r, stage, bid, entry in rows}


def describe_batch(ids: list[str]) -> dict[str, dict]:
    """Batch's record of each job id that it still holds: times, container instance, vCPU."""
    import boto3

    batch = boto3.client("batch")
    out = {}
    for i in range(0, len(ids), 100):
        for j in batch.describe_jobs(jobs=ids[i:i + 100])["jobs"]:
            container = j.get("container", {})
            vcpus = next((float(r["value"]) for r in container.get("resourceRequirements", [])
                          if r["type"] == "VCPU"), container.get("vcpus"))
            out[j["jobId"]] = {"createdAt": j.get("createdAt"), "startedAt": j.get("startedAt"),
                               "stoppedAt": j.get("stoppedAt"), "status": j.get("status"),
                               "host": container.get("containerInstanceArn"), "vcpus": vcpus}
    return out


def instance_vcpus(types: set[str]) -> dict[str, int]:
    """EC2's default vCPU count for each instance type."""
    import boto3

    if not types:
        return {}
    ec2 = boto3.client("ec2")
    out = {}
    for page in ec2.get_paginator("describe_instance_types").paginate(InstanceTypes=sorted(types)):
        for t in page["InstanceTypes"]:
            out[t["InstanceType"]] = t["VCpuInfo"]["DefaultVCpus"]
    return out


# ----------------------------------------------------------------------
# The arithmetic, on plain records (the unit tests' fixture)
# ----------------------------------------------------------------------


def published(jobs: dict) -> list[dict]:
    return [j for j in jobs.values() if j["state"] == "published"
            and "container_seconds" in j.get("metrics", {})]


def parts(job: dict, view: str) -> dict[str, float]:
    """A job's seconds split by its timers (``timers``) or its steps (``steps``), "untimed" the rest.

    ``steps`` is empty for a job that wrote none.
    """
    m, stage, total = job["metrics"], job["stage"], job["metrics"]["container_seconds"]
    if view == "timers":
        named = {k: m[k] for k in TIMERS.get(stage, []) if k in m}
    else:
        named = {k: m[k] for k in m if k.startswith("step_") and k.endswith("_s")}
        if not named:
            return {}
    out = {f"{stage}: {k}": v for k, v in named.items()}
    label = "untimed" if view == "timers" else "remainder"
    out[f"{stage}: {label}" if named else stage] = total - sum(named.values())
    return out


def vcpu_hours(jobs: list[dict], batch: dict, view: str) -> tuple[dict[str, float], float, int]:
    """Each part's job vCPU-hours (its seconds times the job's Batch vCPU), the total, jobs covered."""
    out: dict[str, float] = defaultdict(float)
    covered = 0
    for job in jobs:
        split = parts(job, view)
        if not split:
            continue
        covered += 1
        vcpus = next((batch[b]["vcpus"] for b in job["attempts"] if b in batch
                      and batch[b].get("vcpus")), 1.0)
        for name, seconds in split.items():
            out[name] += seconds * vcpus / 3600
    return dict(out), sum(out.values()), covered


def intervals(batch: dict, owners: dict[str, tuple]) -> dict[str, list[tuple]]:
    """Per host, ``(start, stop, owner)`` of every Batch job on it; ``owners`` maps Batch id to owner."""
    hosts = defaultdict(list)
    for bid, rec in batch.items():
        start, stop = _t(rec.get("startedAt")), _t(rec.get("stoppedAt"))
        if rec.get("host") and start and stop and stop > start:
            hosts[rec["host"]].append((start, stop, owners.get(bid, ("other", None))))
    return dict(hosts)


def host_shares(spans: list[tuple]) -> tuple[dict, float, float, int]:
    """One host: each owner's time-weighted share, seconds; the idle seconds; the mean and peak co-tenancy.

    The span is the first start to the last stop; at each moment the time
    is split equally among the jobs running; moments with none are idle.
    """
    edges = sorted({t for s, e, _ in spans for t in (s, e)})
    share: dict = defaultdict(float)
    idle = busy_weighted = busy = 0.0
    peak = 0
    for a, b in zip(edges, edges[1:]):
        seconds = (b - a).total_seconds()
        running = [o for s, e, o in spans if s <= a and e >= b]
        if not running:
            idle += seconds
            continue
        peak = max(peak, len(running))
        busy += seconds
        busy_weighted += seconds * len(running)
        for owner in running:
            share[owner] += seconds / len(running)
    return dict(share), idle, busy_weighted / busy if busy else 0.0, peak


def host_hours(run: dict, batch: dict, host_type: dict, vcpus_of: dict) -> dict:
    """The run's host-hours: per part, per host, the denominator, idle and other runs' share."""
    owners = {bid: ("run", jid) for jid, j in run["jobs"].items() for bid in j["attempts"]}
    hosts = intervals(batch, owners)
    jobs = {j["id"]: j for j in published(run["jobs"])}
    per_part: dict[str, float] = defaultdict(float)
    per_host = []
    totals = {"span": 0.0, "run": 0.0, "other": 0.0, "idle": 0.0, "unpublished": 0.0}
    for host, spans in hosts.items():
        if not any(o[0] == "run" for _, _, o in spans):
            continue
        share, idle, mean_co, peak = host_shares(spans)
        span = (max(e for _, e, _ in spans) - min(s for s, _, _ in spans)).total_seconds()
        totals["span"] += span
        totals["idle"] += idle
        mine = 0.0
        for owner, seconds in share.items():
            if owner[0] != "run":
                totals["other"] += seconds
                continue
            job = jobs.get(owner[1])
            if job is None:
                totals["unpublished"] += seconds
                continue
            mine += seconds
            split = parts(job, "steps") or parts(job, "timers")
            whole = sum(split.values()) or 1.0
            for name, value in split.items():
                per_part[name] += seconds * max(value, 0.0) / whole / 3600
        totals["run"] += mine
        kind = host_type.get(host)
        per_host.append({"host": host.rsplit("/", 1)[-1], "type": kind, "vcpus": vcpus_of.get(host),
                         "jobs": len(spans), "run_jobs": sum(1 for *_, o in spans if o[0] == "run"),
                         "span_h": span / 3600, "idle_h": idle / 3600, "mean": mean_co, "peak": peak})
    return {"parts": dict(per_part), "hosts": per_host,
            **{k: v / 3600 for k, v in totals.items()}}


def queue_waits(run: dict, batch: dict) -> dict[str, list[float]]:
    out = defaultdict(list)
    for job in run["jobs"].values():
        for bid in job["attempts"]:
            rec = batch.get(bid)
            if rec and rec.get("createdAt") and rec.get("startedAt"):
                out[job["stage"]].append((_t(rec["startedAt"]) - _t(rec["createdAt"])).total_seconds())
    return dict(out)


def fixed_and_per_source(jobs: list[dict]) -> dict[str, dict]:
    """Per stage: median fixed seconds per job and median seconds per source beyond them."""
    out = {}
    for stage in sorted({j["stage"] for j in jobs}):
        rows = [j["metrics"] for j in jobs if j["stage"] == stage]
        with_steps = [m for m in rows if any(k.startswith("step_") for k in m)]
        fixed = [m.get("startup_s", 0.0) + sum(m.get(k, 0.0) for k in FIXED_STEPS) for m in with_steps]
        per = []
        for m in with_steps:
            n = sum(m.get(k, 0.0) for k in SOURCES.get(stage, ()))
            if n:
                inside = sum(m.get(k, 0.0) for k in FIXED_STEPS)
                per.append((m["container_seconds"] - inside) / n)
        out[stage] = {"jobs": len(rows), "with_steps": len(with_steps), "fixed_s": median(fixed),
                      "startup_s": median([m["startup_s"] for m in with_steps if "startup_s" in m]),
                      "per_source_s": median(per),
                      "container_s": median([m["container_seconds"] for m in rows])}
    return out


def critical_path(run: dict, batch: dict) -> list[dict]:
    """The median exposure's chain: its packet job back through each job's gating upstream job."""
    def span(job):
        recs = [batch[b] for b in job["attempts"] if b in batch]
        if not recs:
            return None
        rec = max(recs, key=lambda r: r.get("stoppedAt") or 0)
        return _t(rec.get("createdAt")), _t(rec.get("startedAt")), _t(rec.get("stoppedAt"))

    jobs = run["jobs"]
    ends = []
    for job in jobs.values():
        if job["stage"] != "packet" or job["state"] != "published" or not span(job):
            continue
        chain, cur = [job["id"]], job["id"]
        while run["edges"].get(cur):
            ups = [u for u in run["edges"][cur] if u in jobs and span(jobs[u])
                   and span(jobs[u])[2] <= span(jobs[cur])[0]]
            if not ups:
                break
            cur = max(ups, key=lambda u: span(jobs[u])[2])
            chain.append(cur)
        first, last = span(jobs[chain[-1]]), span(job)
        ends.append(((last[2] - first[0]).total_seconds(), chain))
    if not ends:
        return []
    ends.sort(key=lambda x: x[0])
    _, chain = ends[len(ends) // 2]
    out, previous = [], None
    for jid in reversed(chain):
        created, started, stopped = span(jobs[jid])
        out.append({"job": jid, "stage": jobs[jid]["stage"],
                    "gap_s": (created - previous).total_seconds() if previous else 0.0,
                    "wait_s": (started - created).total_seconds(),
                    "run_s": (stopped - started).total_seconds()})
        previous = stopped
    return out


def leads(host: dict, host_vcpu_hours: float, job_vcpu_hours: float, waits: dict) -> list[str]:
    """The ranked leads: the largest host-hour items, then the hosts' unreserved CPUs, idle time
    and queue wait."""
    out = []
    total = host["span"]
    for name, hours in sorted(host["parts"].items(), key=lambda x: -x[1])[:5]:
        out.append(f"{name}: {hours:,.1f} host-h, {pct(hours, host['run'])} of the run's")
    if job_vcpu_hours and host_vcpu_hours > 1.5 * job_vcpu_hours:
        out.append(f"the hosts' vCPU-h are {host_vcpu_hours / job_vcpu_hours:.1f} times the jobs'"
                   " vCPU reservation: memory, not CPU, sets the packing, so CPU is left on the hosts")
    if host["idle"]:
        out.append(f"idle host time inside the span: {host['idle']:,.1f} host-h ({pct(host['idle'], total)})"
                   " - packing and scale-in, not code")
    if host["other"]:
        out.append(f"other runs' jobs on the same hosts: {host['other']:,.1f} host-h"
                   f" ({pct(host['other'], total)})")
    worst = max(((s, median(w)) for s, w in waits.items() if w), key=lambda x: x[1], default=None)
    if worst and worst[1] > 60:
        out.append(f"queue wait: {worst[0]} jobs waited a median {worst[1]:,.0f} s for a host")
    return out


# ----------------------------------------------------------------------
# The report
# ----------------------------------------------------------------------


def render(run: dict, batch: dict, vcpus_by_type: dict) -> str:
    jobs = published(run["jobs"])
    lines = [f"# Throughput report: run {run['run']}", "",
             f"Dataset `{run['dataset']}`, survey {run['survey']}, revision `{run['revision']}`."
             f" Published jobs: " + ", ".join(
                 f"{s} {n}" for s, n in sorted(_count(jobs).items())) + ".", ""]

    lines += ["## Job vCPU-hours, by the stage timers (the historical view)", ""]
    hours, total, _ = vcpu_hours(jobs, batch, "timers")
    lines += [f"Denominator: {total:,.1f} vCPU-h, every published job's `container_seconds`"
              " times its Batch vCPU.", "", "| Rank | Item | vCPU-h | Share |", "|---|---|---|---|"]
    for i, (name, h) in enumerate(sorted(hours.items(), key=lambda x: -x[1]), 1):
        lines.append(f"| {i} | {name} | {h:,.1f} | {pct(h, total)} |")

    lines += ["", "## Job vCPU-hours, by the steps", ""]
    shours, stotal, covered = vcpu_hours(jobs, batch, "steps")
    if not covered:
        lines.append("absent: no job of this run wrote `step_*_s` metrics (before throughput-metrics).")
    else:
        lines += [f"{covered} of {len(jobs)} published jobs wrote steps; {stotal:,.1f} vCPU-h."
                  " The remainder is `container_seconds` less the job's steps.", "",
                  "| Rank | Item | vCPU-h | Share |", "|---|---|---|---|"]
        for i, (name, h) in enumerate(sorted(shours.items(), key=lambda x: -x[1]), 1):
            lines.append(f"| {i} | {name} | {h:,.2f} | {pct(h, stotal)} |")
    lines += ["", "## Per stage: CPU against wall, S3 bytes, memory", "",
              "| Stage | Jobs | CPU s / wall s | S3 read GB | S3 write GB | anon peak median MiB |",
              "|---|---|---|---|---|---|"]
    for stage, n in sorted(_count(jobs).items()):
        ms = [j["metrics"] for j in jobs if j["stage"] == stage]
        cpu = sum(m.get("cgroup_cpu_s", 0) for m in ms)
        wall = sum(m.get("cgroup_cpu_wall_s", 0) for m in ms)
        has = lambda k: any(k in m for m in ms)  # noqa: E731
        ratio = f"{cpu / wall:.2f}" if has("cgroup_cpu_s") and wall else "absent"
        read = fmt(sum(m.get("s3_read_bytes", 0) for m in ms) / 1e9, 2) if has("s3_read_bytes") else "absent"
        write = fmt(sum(m.get("s3_write_bytes", 0) for m in ms) / 1e9, 2) if has("s3_write_bytes") else "absent"
        anon = median([m["cgroup_anon_peak_bytes"] / 2**20 for m in ms if "cgroup_anon_peak_bytes" in m])
        lines.append(f"| {stage} | {n} | {ratio} | {read} | {write} | {fmt(anon, 0)} |")

    owners_type = {}
    host_vcpus = {}
    for job in run["jobs"].values():
        for bid, entry in job["attempts"].items():
            rec = batch.get(bid)
            if rec and rec.get("host"):
                owners_type[rec["host"]] = entry.get("instance_type") or job.get("instance_type")
                v = job.get("metrics", {}).get("host_vcpus")
                host_vcpus[rec["host"]] = int(v) if v else vcpus_by_type.get(owners_type[rec["host"]])
    host = host_hours(run, batch, owners_type, host_vcpus)
    hv = sum((h["span_h"] * (h["vcpus"] or 0)) for h in host["hosts"])
    lines += ["", "## Host-hours", "",
              f"Hosts: {len(host['hosts'])} container instances. Denominator: their span, first job's"
              f" start to last job's stop, {host['span']:,.1f} host-h ({hv:,.1f} host vCPU-h)."
              " Inside it, time is split equally among the jobs running at each moment:"
              f" this run's published jobs {host['run']:,.1f} host-h ({pct(host['run'], host['span'])}),"
              f" its unpublished attempts {host['unpublished']:,.1f}, other runs' jobs (coverage)"
              f" {host['other']:,.1f} ({pct(host['other'], host['span'])}), idle {host['idle']:,.1f}"
              f" ({pct(host['idle'], host['span'])}). Boot and idle time outside the span is not in the"
              " records and not counted. A job's share is split across its steps (or, before"
              " throughput-metrics, its timers) in proportion to their seconds.", "",
              "| Rank | Item | Host-h | Share of the run's |", "|---|---|---|---|"]
    for i, (name, h) in enumerate(sorted(host["parts"].items(), key=lambda x: -x[1]), 1):
        lines.append(f"| {i} | {name} | {h:,.2f} | {pct(h, host['run'])} |")

    lines += ["", "## Co-tenancy", "", "| Instance type | vCPU | Hosts | Jobs | Span host-h | Idle host-h"
              " | Mean jobs at once | Largest |", "|---|---|---|---|---|---|---|---|"]
    by_type = defaultdict(list)
    for h in host["hosts"]:
        by_type[h["type"]].append(h)
    for kind, hs in sorted(by_type.items(), key=lambda x: str(x[0])):
        span = sum(h["span_h"] for h in hs)
        mean = sum(h["mean"] * (h["span_h"] - h["idle_h"]) for h in hs) / max(
            sum(h["span_h"] - h["idle_h"] for h in hs), 1e-9)
        lines.append(f"| {kind or 'absent'} | {hs[0]['vcpus'] or 'absent'} | {len(hs)} |"
                     f" {sum(h['jobs'] for h in hs)} | {span:,.1f} | {sum(h['idle_h'] for h in hs):,.1f} |"
                     f" {mean:.1f} | {max(h['peak'] for h in hs)} |")
    lines += ["", "<details><summary>Per host</summary>", "",
              "| Host | Type | Jobs (this run) | Span h | Idle h | Mean | Largest |",
              "|---|---|---|---|---|---|---|"]
    for h in sorted(host["hosts"], key=lambda h: -h["span_h"]):
        lines.append(f"| {h['host'][:12]} | {h['type'] or 'absent'} | {h['jobs']} ({h['run_jobs']}) |"
                     f" {h['span_h']:.2f} | {h['idle_h']:.2f} | {h['mean']:.1f} | {h['peak']} |")
    lines += ["", "</details>"]

    waits = queue_waits(run, batch)
    lines += ["", "## Queue wait, Batch's createdAt to startedAt", "",
              "| Stage | Submissions | Median s | p90 s | Max s |", "|---|---|---|---|---|"]
    for stage, w in sorted(waits.items()):
        q = sorted(w)
        lines.append(f"| {stage} | {len(q)} | {median(q):,.0f} | {q[int(0.9 * (len(q) - 1))]:,.0f} |"
                     f" {q[-1]:,.0f} |")

    lines += ["", "## Fixed cost per job against per-source cost", "",
              f"Fixed: `startup_s` (the process's start to the job's, outside `container_seconds`) and "
              + ", ".join(f"`{k}`" for k in FIXED_STEPS) + ". Per source: the rest of"
              " `container_seconds` over the difference's scored candidates or the packet's packets;"
              " medians over the jobs that wrote steps.", "",
              "| Stage | Jobs (with steps) | container s | startup s | fixed s | s per source |",
              "|---|---|---|---|---|---|"]
    for stage, f in fixed_and_per_source(jobs).items():
        lines.append(f"| {stage} | {f['jobs']} ({f['with_steps']}) | {fmt(f['container_s'])} |"
                     f" {fmt(f['startup_s'])} | {fmt(f['fixed_s'])} | {fmt(f['per_source_s'], 4)} |")

    path = critical_path(run, batch)
    lines += ["", "## The critical path of one exposure", ""]
    if not path:
        lines.append("absent: no published packet job with Batch records.")
    else:
        total_s = sum(p["gap_s"] + p["wait_s"] + p["run_s"] for p in path)
        lines += [f"The exposure of median end-to-end time, {total_s:,.0f} s from its first job's"
                  " submission to its packet job's stop. Gap: the upstream job's stop to this job's"
                  " submission (the loop's cadence).", "",
                  "| Job | Stage | Gap s | Queue wait s | Run s |", "|---|---|---|---|---|"]
        for p in path:
            lines.append(f"| {p['job']} | {p['stage']} | {p['gap_s']:,.0f} | {p['wait_s']:,.0f} |"
                         f" {p['run_s']:,.0f} |")

    lines += ["", "## Leads, ranked", ""]
    lines += [f"{i}. {text}" for i, text in enumerate(leads(host, hv, total, waits), 1)]
    return "\n".join(lines) + "\n"


def _count(jobs: list[dict]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for j in jobs:
        out[j["stage"]] += 1
    return dict(out)


def _json_default(value):
    if isinstance(value, dt.datetime):
        return value.isoformat()
    raise TypeError(type(value))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--run", type=int, required=True)
    parser.add_argument("--batch-records", type=Path, required=True,
                        help="Batch's records of the run and its hosts' other jobs: read if present,"
                             " else fetched and written")
    parser.add_argument("--out", type=Path, help="the report's file (default: stdout)")
    args = parser.parse_args(argv)

    from rapidpipe.db.connection import connect

    with connect(application_name="throughput_report") as conn:
        run = read_run(conn, args.run)
        if args.batch_records.exists():
            saved = json.loads(args.batch_records.read_text())
        else:
            ids = sorted({b for j in run["jobs"].values() for b in j["attempts"]})
            batch = describe_batch(ids)
            times = [_t(r[k]) for r in batch.values() for k in ("startedAt", "stoppedAt") if r.get(k)]
            others = neighbour_batch_ids(conn, args.run, min(times), max(times)) if times else {}
            hosts = {r["host"] for r in batch.values() if r.get("host")}
            near = {b: r for b, r in describe_batch(sorted(others)).items() if r.get("host") in hosts}
            types = {e.get("instance_type") for j in run["jobs"].values()
                     for e in j["attempts"].values() if e.get("instance_type")}
            saved = {"run": args.run, "read": dt.datetime.now(dt.timezone.utc).isoformat(),
                     "batch": {**near, **batch}, "vcpus_by_type": instance_vcpus(types)}
            args.batch_records.write_text(json.dumps(saved, default=_json_default))
    text = render(run, saved["batch"], saved["vcpus_by_type"])
    if args.out:
        args.out.write_text(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
