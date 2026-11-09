import json, sys, statistics as st, collections, numpy as np
r = json.load(open(sys.argv[1]))
a = lambda k: np.array([x[k] if x[k] is not None else np.nan for x in r], float)
rb, xs, n, mc, pk, nov = a("rb_s"), a("xsources_s"), a("n_rb_scored"), a("mean_cotenants"), a("peak_cotenants"), a("n_overlap")
print("types", collections.Counter(x["type"] for x in r))
print("rb_s quantiles", np.nanpercentile(rb, [0, 10, 25, 50, 75, 90, 100]).round(1))
print("n_rb_scored quantiles", np.nanpercentile(n, [0, 10, 50, 90, 100]).round(0))
per = rb / n * 1e3
print("rb ms per scored", np.nanpercentile(per, [0, 10, 25, 50, 75, 90, 100]).round(2))
print("mean co-tenants quantiles", np.nanpercentile(mc, [0, 10, 25, 50, 75, 90, 100]).round(2))
print("peak co-tenants", collections.Counter(pk.astype(int)))
edges = [0, 2, 4, 6, 8, 9, 10, 40]
print("| mean co-tenants | jobs | median rb_s | median rb ms/score | median xsources_s |")
for lo, hi in zip(edges, edges[1:]):
    k = (mc >= lo) & (mc < hi)
    if k.sum(): print(f"| {lo} to {hi} | {k.sum()} | {np.nanmedian(rb[k]):.0f} | {np.nanmedian(per[k]):.2f} | {np.nanmedian(xs[k]):.0f} |")
from scipy.stats import spearmanr
print("spearman rb ms/score vs mean co-tenants", spearmanr(per, mc, nan_policy="omit"))
print("spearman rb_s vs n_rb_scored", spearmanr(rb, n, nan_policy="omit"))
# host effect: variance of per-score cost within vs between hosts
h = collections.defaultdict(list)
for x, p in zip(r, per): h[x["host"]].append(p)
meds = [np.median(v) for v in h.values() if len(v) >= 5]
print("hosts", len(h), "host-median ms/score quantiles", np.percentile(meds, [0, 25, 50, 75, 100]).round(2))
j = [x for x in r if x["job"] == 46889][0]; print("46889", j)
