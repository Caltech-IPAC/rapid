import json, sys, numpy as np, collections
from scipy.stats import spearmanr
full = json.load(open(sys.argv[1]))
for run in ("187", "189", "190"):
    xs = [v[run] for v in full.values()]
    ms = np.array([x["rb_s"] / x["n"] * 1e3 for x in xs]); cot = np.array([x["mean_cot"] for x in xs])
    dd = np.array([x["mean_cot_default_diff"] for x in xs])
    print(run, "ms/score median", round(float(np.median(ms)), 1), "IQR", np.percentile(ms, [25, 75]).round(1),
          "rho(co-tenants)", round(spearmanr(ms, cot).statistic, 2), "rho(default co-tenants)", round(spearmanr(ms, dd).statistic, 2) if run != "187" else "")
    for t in sorted({x["type"] for x in xs}):
        k = [i for i, x in enumerate(xs) if x["type"] == t]
        print("   ", t, len(k), "ms/score", round(float(np.median(ms[k])), 1), "co-tenants", round(float(np.median(cot[k])), 1))
    if run != "187":
        cpu = np.array([x["m"]["rb_cpu_s"] / x["n"] * 1e3 for x in xs]); print("    CPU ms/score", round(float(np.median(cpu)), 1))
        # bins of default-arm co-tenants
        for lo, hi in ((0, 2), (2, 4), (4, 6), (6, 12)):
            k = (dd >= lo) & (dd < hi)
            if k.sum(): print(f"    default co-tenants {lo}-{hi}: {k.sum()} jobs, ms/score {np.median(ms[k]):.1f}")
n187 = np.array([v["187"]["n"] for v in full.values()]); n189 = np.array([v["189"]["n"] for v in full.values()])
print("scored n sample/187 median", round(float(np.median(n189 / n187)), 3))
v = full.get("5033601"); print("5033601", {r: (round(v[r]["rb_s"], 1), round(v[r]["xs_s"], 1), v[r]["n"], v[r]["type"], round(v[r]["mean_cot"], 1)) for r in v})
