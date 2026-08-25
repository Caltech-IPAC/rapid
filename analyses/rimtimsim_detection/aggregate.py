"""Stage 4 -- sign-aware completeness and false-positive aggregation.

This differs from the SOC aggregation in one essential way: differential flux is
NOT folded to its absolute value.  RimTimSim carries both signs -- TRExS eclipsing
binaries and transits are dips, the RAPID-added variables are brightenings -- and
each sign is only recoverable on its own branch of the difference image.  Scoring
a fading source against the positive branch would count a guaranteed miss as a
detection failure.

Completeness is floor-corrected as (c_obs - f) / (1 - f), where f is the match rate
measured on the static control population -- catalogued sources whose |dflux| sits
below the noise.  Those are real sources at real positions and magnitudes, so the
floor they measure is the true chance-match rate at this source density.
"""
import glob
import os

import numpy as np


FLUX_EDGES = [1, 3, 10, 30, 100, 300, 1000, np.inf]


def load(cfg, diff, branch):
    """Collect every per-job result for one (difference image, branch)."""
    pattern = os.path.join(cfg.work, cfg.paths["sweep"], "%s_%s" % (diff, branch), "*.npz")
    files = sorted(glob.glob(pattern))
    if not files:
        raise SystemExit("no sweep output for %s/%s" % (diff, branch))
    dflux, is_rapid, matched, scalars = [], [], {}, {}
    labels, nimg, filts = None, 0, []
    for f in files:
        T = np.load(f, allow_pickle=True)
        vs = [str(v) for v in T["variants"]]
        if labels is None:
            labels = vs
        dflux.append(T["dflux"])
        is_rapid.append(T["is_rapid"])
        filts.append(str(T["filt"]))
        nimg += 1
        for v in labels:
            key = v + "|matched"
            if key not in T:
                matched.setdefault(v, []).append(np.zeros(len(T["dflux"]), bool))
                continue
            matched.setdefault(v, []).append(T[key])
            scalars[v] = scalars.get(v, 0) + T[v + "|scalars"]
    return dict(d=np.concatenate(dflux), is_rapid=np.concatenate(is_rapid),
                M={v: np.concatenate(a) for v, a in matched.items()},
                labels=labels, scalars=scalars, nimg=nimg,
                filters=sorted(set(filts)))


def _rows(d, m, ctrl, signed):
    """Floor-corrected completeness per signed dflux bin."""
    floor = m[ctrl].mean() if ctrl.sum() else 0.0
    out = []
    for lo, hi in zip(FLUX_EDGES[:-1], FLUX_EDGES[1:]):
        sel = (signed >= lo) & (signed < hi)
        if sel.sum() == 0:
            out.append((int(sel.sum()), np.nan))
            continue
        c = m[sel].mean()
        cc = (c - floor) / (1 - floor) if floor < 1 else np.nan
        out.append((int(sel.sum()), max(cc, 0.0)))
    return floor, out


def report(cfg, diff, branch, population="all"):
    R = load(cfg, diff, branch)
    d, ctrl_max = R["d"], float(cfg.truth["static_max_dflux"])

    # On the positive branch a source is recoverable only if it brightened; on the
    # negative branch only if it faded.  `signed` is the recoverable amplitude.
    signed = d if branch == "positive" else -d
    ctrl = np.abs(d) < ctrl_max

    pop = np.ones(len(d), bool)
    if population == "rapid":
        pop = R["is_rapid"].astype(bool)
    elif population == "trexs":
        pop = ~R["is_rapid"].astype(bool)
    # controls are always drawn from the same population being scored
    ctrl = ctrl & pop
    scored = pop & (signed > 0)

    print("=" * 104)
    print("%s / %s   population=%s   %d images   filters %s"
          % (diff.upper(), branch, population, R["nimg"], ",".join(R["filters"])))
    print("%d sources scored (recoverable sign), %d static controls"
          % (scored.sum(), ctrl.sum()))
    hdr = "%-22s %10s |" % ("variant", "FP/img")
    for lo, hi in zip(FLUX_EDGES[:-1], FLUX_EDGES[1:]):
        hdr += "%9s" % ("%g-%g" % (lo, hi) if np.isfinite(hi) else ">%g" % lo)
    print(hdr + "%8s" % "floor")

    out = []
    for v in R["labels"]:
        m = R["M"][v]
        floor, rows = _rows(d[scored | ctrl], m[scored | ctrl], ctrl[scored | ctrl],
                            signed[scored | ctrl])
        fp = R["scalars"].get(v, np.zeros(2, np.int64))[1] / max(R["nimg"], 1)
        line = "%-22s %10.0f |" % (v, fp)
        for n, cc in rows:
            line += "%9s" % ("%.3f" % cc if np.isfinite(cc) else "-")
        out.append((fp, line + "%8.3f" % floor))
    for _, line in sorted(out):
        print(line)
    print()
    return R
