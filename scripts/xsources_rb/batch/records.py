"""Per difference job: host (container instance), type, start/stop, co-tenant counts and rb_s / xsources_s.

Co-tenancy over a job's life: the time-weighted mean number of other jobs running on its host
(each one-vCPU job; all stages), and the count of distinct jobs that overlapped it at all."""
import bisect, json, statistics as st, sys, collections
rows = json.load(open(sys.argv[1])); bd = json.load(open(sys.argv[2])); out = sys.argv[3]
jobs = bd["jobs"]
# Every Batch attempt is a container on a host for [started, stopped].
spans = []  # (host, t0, t1, rapid job id, stage)
for r in rows:
    for bid in r["attempts"] or {}:
        d = jobs.get(bid)
        if d and d["started"] and d["stopped"]:
            spans.append((d["ci"].rsplit("/", 1)[1], d["started"] / 1e3, d["stopped"] / 1e3, r["id"], r["stage"]))
by_host = collections.defaultdict(list)
for s in spans: by_host[s[0]].append(s)
res = []
for r in rows:
    if r["stage"] != "difference" or r["state"] != "published":
        continue
    m = r["metrics"] or {}
    bid = r["batch_job_id"]; d = jobs[bid]; host = d["ci"].rsplit("/", 1)[1]
    t0, t1 = d["started"] / 1e3, d["stopped"] / 1e3
    others = [s for s in by_host[host] if s[3] != r["id"] and s[1] < t1 and s[2] > t0]
    weight = sum(min(t1, s[2]) - max(t0, s[1]) for s in others) / (t1 - t0)
    # concurrency during the rb step is not timestamped; the whole job is the window.
    peak = 0
    for t in [t0] + [s[1] for s in others if s[1] > t0]:
        peak = max(peak, sum(1 for s in others if s[1] <= t < s[2]))
    res.append(dict(job=r["id"], sca=r["scope"].get("sca"), expid=r["scope"].get("expid"), host=host[:8],
                    type=r["instance_type"], t0=t0, t1=t1, n_overlap=len(others),
                    n_overlap_diff=sum(1 for s in others if s[4] == "difference"), mean_cotenants=weight,
                    peak_cotenants=peak, rb_s=m.get("rb_s"), xsources_s=m.get("xsources_s"),
                    container_s=t1 - t0, n_rb_scored=(m.get("n_rb_scored") or 0) + (m.get("n_rb_scored_negative") or 0), n_xsources=m.get("n_xsources"),
                    host_jobs=len(by_host[host])))
json.dump(res, open(out, "w"))
print(len(res), "difference jobs;", len(by_host), "hosts")
