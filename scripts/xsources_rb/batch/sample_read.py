"""The sample's table: per expid, run 187's job and each arm's (runs 189 one thread, 190 default):
rb_s, xsources_s, the probe (CPU against wall, cgroup throttling, quota), placement and co-tenancy.

usage: sample_read.py JOBS.json BATCH.json OUT.md   (JOBS.json: every run whose jobs shared the hosts)"""
import json, sys, collections, numpy as np
rows = json.load(open(sys.argv[1])); jobs = json.load(open(sys.argv[2]))["jobs"]; out = sys.argv[3]
ARMS = {189: "one", 190: "default"}
spans = []
for r in rows:
    for bid in r["attempts"] or {}:
        d = jobs.get(bid)
        if d and d.get("started") and d.get("stopped"):
            spans.append(dict(host=d["ci"].rsplit("/", 1)[1], t0=d["started"] / 1e3, t1=d["stopped"] / 1e3,
                              job=r["id"], run=r["run"], stage=r["stage"]))
by_host = collections.defaultdict(list)
for s in spans: by_host[s["host"]].append(s)

def placement(r):
    d = jobs[r["batch_job_id"]]; host = d["ci"].rsplit("/", 1)[1]; t0, t1 = d["started"] / 1e3, d["stopped"] / 1e3
    ov = [s for s in by_host[host] if s["job"] != r["id"] and s["t0"] < t1 and s["t1"] > t0]
    w = lambda f: sum(min(t1, s["t1"]) - max(t0, s["t0"]) for s in ov if f(s)) / (t1 - t0)
    return dict(host=host[:8], n_overlap=len(ov), mean_cot=w(lambda s: True),
                mean_cot_default_diff=w(lambda s: s["run"] == 190 and s["stage"] == "difference"),
                mean_cot_one_diff=w(lambda s: s["run"] == 189 and s["stage"] == "difference"),
                other_runs=sorted({s["run"] for s in ov} - {189, 190}))

table = collections.defaultdict(dict)
for r in rows:
    if r["stage"] != "difference" or r["state"] != "published" or r["scope"].get("sca") != 3:
        continue
    if r["run"] not in (187, 189, 190):
        continue
    m = r["metrics"] or {}
    e = dict(job=r["id"], type=r["instance_type"], rb_s=m.get("rb_s"), xs_s=m.get("xsources_s"),
             n=(m.get("n_rb_scored") or 0) + (m.get("n_rb_scored_negative") or 0), m=m,
             container_s=None, **placement(r))
    table[r["scope"]["expid"]][r["run"]] = e
full = {e: v for e, v in table.items() if all(k in v for k in (187, 189, 190))}
json.dump({str(e): {str(k): {kk: vv for kk, vv in x.items()} for k, x in v.items()} for e, v in full.items()}, open(out.replace(".md", ".json"), "w"))
f = lambda v, p=1: "" if v is None else f"{v:,.{p}f}"
lines = ["| expid | 187 `rb_s` / `xsources_s` / co-tenants | one: `rb_s` / `xsources_s` / CPU÷wall / throttled s / co-tenants (default diffs) / host | default: `rb_s` / `xsources_s` / CPU÷wall / throttled s / co-tenants (default diffs) / host |",
         "|---|---|---|---|"]
for e in sorted(full):
    v = full[e]; cells = [f"{f(v[187]['rb_s'])} / {f(v[187]['xs_s'])} / {f(v[187]['mean_cot'])}"]
    for run in (189, 190):
        x = v[run]; m = x["m"]
        cw = m.get("rb_cpu_s", 0) / m["rb_wall_s"] if m.get("rb_wall_s") else None
        cells.append(f"{f(x['rb_s'])} / {f(x['xs_s'])} / {f(cw, 2)} / {f(m.get('rb_cg_throttled_s'))} / "
                     f"{f(x['mean_cot'])} ({f(x['mean_cot_default_diff'])}) / {x['host']}")
    lines.append(f"| {e} | " + " | ".join(cells) + " |")
open(out, "w").write("\n".join(lines) + "\n")

# Summary.
def med(run, key, fn=None):
    vals = [fn(v[run]) if fn else v[run][key] for v in full.values()]
    vals = [x for x in vals if x is not None]
    return float(np.median(vals)) if vals else None
summ = {"matched expids": len(full), "expids per run": {r: sum(1 for v in table.values() if r in v) for r in (187, 189, 190)}}
for run in (187, 189, 190):
    summ[run] = dict(rb_s=med(run, "rb_s"), xs_s=med(run, "xs_s"),
                     ms_per_score=med(run, None, lambda x: x["rb_s"] / x["n"] * 1e3 if x["n"] else None),
                     mean_cot=med(run, "mean_cot"), mean_cot_default_diff=med(run, "mean_cot_default_diff"),
                     types=dict(collections.Counter(v[run]["type"] for v in full.values())),
                     hosts=len({v[run]["host"] for v in full.values()}),
                     other_runs=sorted({o for v in full.values() for o in v[run]["other_runs"]}))
    if run != 187:
        for k in ("rb_cpu_s", "rb_wall_s", "rb_cg_usage_s", "rb_cg_nr_periods", "rb_cg_nr_throttled", "rb_cg_throttled_s",
                  "xsrb_cpu_s", "xsrb_wall_s", "xsrb_cg_throttled_s", "cgroup_cpu_quota_us", "cgroup_cpu_period_us",
                  "cgroup_cpu_weight", "cgroup_cpu_shares", "cpu_count", "cpu_affinity", "exp_ort_threads"):
            summ[run][k] = med(run, None, lambda x, k=k: x["m"].get(k))
        summ[run]["quota values"] = sorted({v[run]["m"].get("cgroup_cpu_quota_us") for v in full.values()}, key=str)
        summ[run]["rb_s ratio to 187 (median of per-expid)"] = float(np.median([v[run]["rb_s"] / v[187]["rb_s"] for v in full.values()]))
summ["one/default rb_s per-expid median"] = float(np.median([v[189]["rb_s"] / v[190]["rb_s"] for v in full.values()])) if full else None
summ["same host one & default"] = sum(1 for v in full.values() if v[189]["host"] == v[190]["host"])
print(json.dumps(summ, indent=1, default=str))
