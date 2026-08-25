"""Stage driver.

    python3 -m analyses.rimtimsim_detection.cli catalogs
    python3 -m analyses.rimtimsim_detection.cli kernels
    python3 -m analyses.rimtimsim_detection.cli truth   [--jids 143919,143920]
    python3 -m analyses.rimtimsim_detection.cli sweep   [--diff sfft --branch positive]
    python3 -m analyses.rimtimsim_detection.cli aggregate

Every stage is idempotent: finished work is skipped unless `--force` is given, so a
run can be interrupted and resumed, or a single stage re-run after a config change
without regenerating the ones before it.
"""
import argparse
import json
import os
import sys

import numpy as np

from . import aggregate, catalogs, config, provenance, sweep, truth


def _psf_fwhm(path, zoom=8):
    """FWHM in pixels, measured as the area above half maximum.

    These PSFs are undersampled -- Z087's core falls to ~0.19 of peak one pixel
    off centre -- so any measurement that bins in radius floors out at the bin
    width and reports every filter as the same width.  Counting the area above
    half maximum on an upsampled stamp avoids both the binning and the need to
    assume a profile shape:  A = pi (FWHM/2)^2  =>  FWHM = 2 sqrt(A/pi).
    """
    from astropy.io import fits
    from scipy.ndimage import zoom as ndzoom

    with fits.open(path) as hdul:
        img = np.asarray(hdul[0].data, dtype=float)
        if img.ndim > 2:                       # some PSF products carry a stack
            img = img[0]
    img = np.nan_to_num(img)

    # Work on a small window around the peak so the upsample stays cheap and the
    # wings cannot contribute spurious above-half-max pixels.
    pk = np.unravel_index(np.argmax(img), img.shape)
    half_w = 12
    y0, y1 = max(0, pk[0] - half_w), min(img.shape[0], pk[0] + half_w + 1)
    x0, x1 = max(0, pk[1] - half_w), min(img.shape[1], pk[1] + half_w + 1)
    cut = img[y0:y1, x0:x1]

    fine = ndzoom(cut, zoom, order=3)
    area = float((fine >= 0.5 * fine.max()).sum()) / (zoom * zoom)
    if area <= 0:
        raise ValueError("no pixels above half maximum in %s" % path)
    return 2.0 * np.sqrt(area / np.pi)


def cmd_catalogs(cfg, args):
    made = []
    for filt in cfg.filters:
        p = catalogs.build(cfg, filt, force=args.force)
        C = np.load(p, allow_pickle=True)
        print("%s: %d sources, %d epochs, %d RAPID-added"
              % (filt, len(C["sicbro_id"]), len(C["mjd"]), int(C["is_rapid"].sum())))
        made.append(p)
    provenance.write(cfg, "catalogs", inputs=made)


def cmd_kernels(cfg, args):
    """Measure the PSF FWHM per filter and build the matched-filter kernels."""
    cachedir = cfg.cache
    fwhm = {}
    for filt in cfg.filters:
        conf = cfg.sweep["fwhm_px"].get(filt, 0.0)
        if conf and not args.force:
            fwhm[filt] = float(conf)
            print("%s: FWHM %.3f px (from config)" % (filt, fwhm[filt]))
            continue
        jid = _first_jid_for_filter(cfg, filt, cachedir)
        name = "WFI_SCA%02d_%s_PSF_DET_DIST_normalized.fits" % (
            int(cfg.survey["sca"]), cfg.trexs_filter(filt))
        p = truth.fetch(cfg.product_uri(jid, name), os.path.join(cachedir, name))
        fwhm[filt] = _psf_fwhm(p)
        print("%s: FWHM %.3f px (measured from %s, jid%d)" % (filt, fwhm[filt], name, jid))
    kdir = os.path.join(cfg.work, "kernels")
    ks = sweep.build_kernels(kdir, min(fwhm.values()), max(fwhm.values()))
    for tag, (path, w) in ks.items():
        print("  kernel %s -> FWHM %.3f px  %s" % (tag, w, path))
    # Persist what was measured so `sweep` uses the real widths.  Writing a
    # sidecar rather than editing the TOML keeps the config a human-owned file
    # and keeps a measured value from silently masquerading as a chosen one.
    with open(_kernel_sidecar(cfg), "w") as fh:
        json.dump({"fwhm_px": fwhm,
                   "kernels": {t: {"path": p, "fwhm_px": w} for t, (p, w) in ks.items()}},
                  fh, indent=2)
    provenance.write(cfg, "kernels", inputs=[p for p, _ in ks.values()],
                     extra={"fwhm_px": fwhm})


def _kernel_sidecar(cfg):
    return os.path.join(cfg.work, "kernels.json")


def _first_jid_for_filter(cfg, filt, cachedir):
    """Find the first science job taken in `filt` (from its difference header)."""
    for jid in cfg.science_jids:
        try:
            hdr, _ = truth.diff_header(cfg, jid, cachedir)
        except IOError:
            continue
        if str(hdr["FILTER"]).strip() == filt:
            return jid
    raise SystemExit("no science job found for filter %s" % filt)


def cmd_truth(cfg, args):
    cachedir = cfg.cache
    refmap = truth.reference_map(cfg, cachedir)
    for filt, r in refmap.items():
        print("reference %s: jid%d, %d constituents, MJD %.4f .. %.4f"
              % (filt, r["jid"], r["n"], r["mjd"].min(), r["mjd"].max()))
    jids = args.jid_list or cfg.science_jids
    ok = fail = 0
    for jid in jids:
        try:
            truth.build(cfg, jid, refmap, cachedir, force=args.force)
            ok += 1
        except Exception as e:                       # keep going; report at the end
            fail += 1
            print("jid%d FAILED %s: %s" % (jid, type(e).__name__, e), flush=True)
    print("truth: %d built, %d failed" % (ok, fail))
    provenance.write(cfg, "truth", inputs=[], extra={"n_ok": ok, "n_fail": fail})


def cmd_sweep(cfg, args):
    cachedir = cfg.cache
    side = _kernel_sidecar(cfg)
    if not os.path.exists(side):
        raise SystemExit("run the `kernels` stage first (%s is missing)" % side)
    with open(side) as fh:
        meta = json.load(fh)
    ks = {t: (d["path"], float(d["fwhm_px"])) for t, d in meta["kernels"].items()}
    sexbin = os.environ.get("RTS_SEX", "/code/c/bin/sex")
    cdf = os.environ.get("RTS_CDF", "/code/cdf")
    diffs = [args.diff] if args.diff else cfg.sweep["diffs"]
    branches = [args.branch] if args.branch else cfg.truth["branches"]
    jids = args.jid_list or cfg.science_jids
    keep = cfg.keep_images
    if args.discard_images:
        keep = False
    elif args.keep_images:
        keep = True
    print("cache %s (%s difference images after use)"
          % (cachedir, "keeping" if keep else "discarding"), flush=True)
    ok = fail = 0
    for diff in diffs:
        for branch in branches:
            outdir = os.path.join(cfg.work, cfg.paths["sweep"], "%s_%s" % (diff, branch))
            for jid in jids:
                try:
                    info = sweep.process(cfg, jid, diff, branch, ks, sexbin, cdf,
                                         cachedir, outdir, keep=keep)
                    ok += 1
                except Exception as e:
                    info = "FAILED %s: %s" % (type(e).__name__, e)
                    fail += 1
                print("%d %s %s %s" % (jid, diff, branch, info), flush=True)
    print("sweep: %d units done, %d failed" % (ok, fail))
    provenance.write(cfg, "sweep", inputs=[side],
                     extra={"n_ok": ok, "n_fail": fail, "diffs": diffs,
                            "branches": branches, "n_jids": len(jids),
                            "keep_images": keep, "cache": cachedir,
                            "fwhm_px": meta.get("fwhm_px", {})})


def _filter_selection(cfg, args):
    """Expand --filter into report() arguments.  None means pooled over filters."""
    out = []
    for f in (args.filter or ["all"]):
        if f == "all":
            out.append(None)
        elif f == "each":
            out.extend(cfg.filters)
        elif f in cfg.filters:
            out.append(f)
        else:
            raise SystemExit("unknown filter %r (configured: %s)"
                             % (f, ", ".join(cfg.filters)))
    seen, uniq = set(), []
    for f in out:                                   # preserve order, drop repeats
        if f not in seen:
            seen.add(f)
            uniq.append(f)
    return uniq


def cmd_aggregate(cfg, args):
    filters = _filter_selection(cfg, args)
    diffs = [args.diff] if args.diff else cfg.sweep["diffs"]
    branches = [args.branch] if args.branch else cfg.truth["branches"]
    pops = args.population or ["all", "rapid", "trexs"]
    for diff in diffs:
        for branch in branches:
            for pop in pops:
                for filt in filters:
                    try:
                        aggregate.report(cfg, diff, branch, pop, filt)
                    except SystemExit as e:
                        print(e)
    provenance.write(cfg, "aggregate", inputs=[],
                     extra={"diffs": diffs, "branches": branches,
                            "populations": pops,
                            "filters": ["pooled" if f is None else f for f in filters],
                            "static_max_dflux": cfg.truth["static_max_dflux"],
                            "match_px": cfg.truth["match_px"]})


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", choices=["catalogs", "kernels", "truth", "sweep", "aggregate"])
    ap.add_argument("--config", default=None)
    ap.add_argument("--force", action="store_true", help="rebuild instead of reusing")
    ap.add_argument("--jids", default=None, help="comma-separated subset of job ids")
    ap.add_argument("--diff", default=None, choices=["sfft", "zogy", "naive"])
    ap.add_argument("--branch", default=None, choices=["positive", "negative"])
    ap.add_argument("--population", action="append",
                    choices=["all", "rapid", "trexs"], default=None)
    retain = ap.add_mutually_exclusive_group()
    retain.add_argument("--keep-images", action="store_true",
                        help="sweep: keep fetched difference images (overrides config)")
    retain.add_argument("--discard-images", action="store_true",
                        help="sweep: delete each difference image this run downloaded "
                             "once its result is written -- bounds disk use at a few "
                             "GB instead of ~100.  Never deletes an already-cached "
                             "image, so it is safe against a shared cache.")
    ap.add_argument("--filter", action="append", default=None,
                    help="aggregate: a configured filter name, 'all' for pooled, "
                         "or 'each' to expand to every filter separately. "
                         "Repeatable; defaults to 'all'.")
    args = ap.parse_args(argv)
    args.jid_list = ([int(x) for x in args.jids.split(",")] if args.jids else None)

    cfg = config.load(args.config)
    os.makedirs(cfg.work, exist_ok=True)
    dict(catalogs=cmd_catalogs, kernels=cmd_kernels, truth=cmd_truth,
         sweep=cmd_sweep, aggregate=cmd_aggregate)[args.stage](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
