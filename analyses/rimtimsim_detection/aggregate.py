"""Stage 4 -- sign-aware completeness and false-positive aggregation.

This differs from the SOC aggregation in two essential ways.

**Sign.** Differential flux is NOT folded to its absolute value.  RimTimSim carries
both signs -- TRExS eclipsing binaries and transits are dips, the RAPID-added
variables are brightenings -- and each sign is only recoverable on its own branch
of the difference image.  Scoring a fading source against the positive branch
would count a guaranteed miss as a detection failure.

**Filter.** Results can be reported per filter as well as pooled.  Z087 and K213
sit at opposite ends of the Roman range: measured PSF FWHM 1.32 vs 1.73 px and
zeropoints 26.298 vs 25.857.  Those are two different sensitivity regimes, and the
sweep already builds a separate matched-filter kernel for each, so pooling the
scoring would average over a difference the detection stage treats as real.
Selecting a filter re-normalises FP/img over that filter's images only, and
re-measures the chance-match floor within it -- both are per-filter quantities.

Completeness is floor-corrected as (c_obs - f) / (1 - f), where f is the match rate
measured on the static control population -- catalogued sources whose |dflux| sits
below the noise.  Those are real sources at real positions and magnitudes, so the
floor they measure is the true chance-match rate at this source density.
"""
import glob
import os

import numpy as np


FLUX_EDGES = [1, 3, 10, 30, 100, 300, 1000, np.inf]

# Reading a (diff, branch) set is ~263 decompressions, and `report` is called once
# per population x filter combination over the same set.  Cache the load.
_CACHE = {}


def load(cfg, diff, branch):
    """Collect every per-job result for one (difference image, branch).

    Per-source arrays carry a `filt` entry so a filter subset can be selected
    downstream; detection counts and image counts are kept keyed by filter so
    FP/img can be re-normalised over the right denominator.
    """
    sweepdir = os.path.join(cfg.work, cfg.paths["sweep"], "%s_%s" % (diff, branch))
    key = sweepdir                                  # keyed on the directory, not just
    if key in _CACHE:                               # (diff, branch) -- two configs in
        return _CACHE[key]                          # one process must not collide
    pattern = os.path.join(sweepdir, "*.npz")
    files = sorted(glob.glob(pattern))
    if not files:
        raise SystemExit("no sweep output for %s/%s" % (diff, branch))
    dflux, is_rapid, srcfilt = [], [], []
    matched, scalars, nimg = {}, {}, {}
    labels = None
    for f in files:
        T = np.load(f, allow_pickle=True)
        vs = [str(v) for v in T["variants"]]
        if labels is None:
            labels = vs
        n = len(T["dflux"])
        filt = str(T["filt"])
        dflux.append(T["dflux"])
        is_rapid.append(T["is_rapid"])
        srcfilt.append(np.full(n, filt))
        nimg[filt] = nimg.get(filt, 0) + 1
        for v in labels:
            mkey = v + "|matched"
            if mkey not in T:
                matched.setdefault(v, []).append(np.zeros(n, bool))
                continue
            matched.setdefault(v, []).append(T[mkey])
            s = scalars.setdefault((v, filt), np.zeros(2, np.int64))
            s += T[v + "|scalars"]
    R = dict(d=np.concatenate(dflux), is_rapid=np.concatenate(is_rapid),
             filt=np.concatenate(srcfilt),
             M={v: np.concatenate(a) for v, a in matched.items()},
             labels=labels, scalars=scalars, nimg=nimg,
             filters=sorted(nimg))
    _CACHE[key] = R
    return R


def duplicate_variants(R):
    """Variant labels whose match vectors are bitwise identical.

    Two variants agreeing on every one of millions of sources is not a result, it
    is a configuration error -- `SE-dao` and `SE-gauss` once shared a kernel and
    silently reported the same numbers under two names.  Aggregation reports this
    rather than leaving it to be noticed.
    """
    groups, seen = [], {}
    for v in R["labels"]:
        m = R["M"].get(v)
        if m is None:
            continue
        key = (int(m.sum()), hash(m.tobytes()))
        seen.setdefault(key, []).append(v)
    for vs in seen.values():
        if len(vs) > 1:
            groups.append(sorted(vs))
    return sorted(groups)


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


def report(cfg, diff, branch, population="all", filt=None):
    """Print one completeness/FP table.  `filt` of None pools every filter."""
    R = load(cfg, diff, branch)
    d, ctrl_max = R["d"], float(cfg.truth["static_max_dflux"])

    if filt is not None and filt not in R["filters"]:
        raise SystemExit("no %s/%s output for filter %s (have %s)"
                         % (diff, branch, filt, ",".join(R["filters"])))
    fsel = np.ones(len(d), bool) if filt is None else (R["filt"] == filt)
    # FP/img and the chance floor are both per-filter quantities, so the
    # denominator and the control sample follow the same selection.
    fseen = R["filters"] if filt is None else [filt]
    nimg = sum(R["nimg"][f] for f in fseen)

    # On the positive branch a source is recoverable only if it brightened; on the
    # negative branch only if it faded.  `signed` is the recoverable amplitude.
    signed = d if branch == "positive" else -d

    pop = fsel.copy()
    if population == "rapid":
        pop &= R["is_rapid"].astype(bool)
    elif population == "trexs":
        pop &= ~R["is_rapid"].astype(bool)
    # controls are always drawn from the same population being scored
    ctrl = (np.abs(d) < ctrl_max) & pop
    scored = pop & (signed > 0)

    print("=" * 104)
    print("%s / %s   population=%s   filter=%s   %d images"
          % (diff.upper(), branch, population,
             filt or "pooled(%s)" % ",".join(R["filters"]), nimg))
    print("%d sources scored (recoverable sign), %d static controls"
          % (scored.sum(), ctrl.sum()))
    for g in duplicate_variants(R):
        print("WARNING: identical detections from %s -- these are not "
              "independent variants" % ", ".join(g))
    hdr = "%-22s %10s |" % ("variant", "FP/img")
    for lo, hi in zip(FLUX_EDGES[:-1], FLUX_EDGES[1:]):
        hdr += "%9s" % ("%g-%g" % (lo, hi) if np.isfinite(hi) else ">%g" % lo)
    print(hdr + "%8s" % "floor")

    out = []
    for v in R["labels"]:
        m = R["M"][v]
        keep = scored | ctrl
        floor, rows = _rows(d[keep], m[keep], ctrl[keep], signed[keep])
        nfp = sum(int(R["scalars"].get((v, f), np.zeros(2, np.int64))[1]) for f in fseen)
        line = "%-22s %10.0f |" % (v, nfp / max(nimg, 1))
        for n, cc in rows:
            line += "%9s" % ("%.3f" % cc if np.isfinite(cc) else "-")
        out.append((nfp / max(nimg, 1), line + "%8.3f" % floor))
    for _, line in sorted(out):
        print(line)
    print()
    return R
