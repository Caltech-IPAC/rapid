"""The batched fit against the baseline's photutils fit on the positive xsources (fit columns)."""
import json, pickle, sys, time, os
import numpy as np
from astropy.io import fits
from rapidpipe.science.difference import xsources, columns
root = os.environ.get("XRB_ROOT", "/t1")
snap = f"{root}/base/snap/xs"
limit = int(sys.argv[1]) if len(sys.argv) > 1 else 0
settings = json.load(open(f"{root}/base/snap/settings.json"))["difference"]
call = json.load(open(f"{snap}/call.json"))
old = pickle.load(open(f"{snap}/extra.pkl", "rb"))["extra"]["positive"]
cat = xsources.read_catalog(__import__("pathlib").Path(snap) / call["signs"]["positive"][0])
data = fits.getdata(f"{snap}/{call['signs']['positive'][1]}", 0)
err = fits.getdata(f"{snap}/{call['uncertainty_image']}", 0)
psf = fits.getdata(f"{snap}/{call['psf_image']}", 0)
idx = np.flatnonzero(np.isfinite(old["flux_fit"]) | np.isfinite(old["x_dao_peak"]))
fitted = np.flatnonzero(~np.isnan(old["sharpness"]) | np.isfinite(old["flux_fit"]))
x0 = np.asarray(cat["XWIN_IMAGE"], float) - 1; y0 = np.asarray(cat["YWIN_IMAGE"], float) - 1
sel = np.flatnonzero(np.isfinite(old["x_dao_peak"]))
if limit: sel = sel[:limit]
p = settings["psfcat_diffimage"]; e = settings["extra_catalog_cols"]
t = time.perf_counter()
new = xsources.photutils_columns(data, err, psf, x0[sel], y0[sel], fwhm=float(p["fwhm"]),
    fit_shape=e["photutils_cols_fit_shape"] or p["fit_shape"], aperture_radius=float(p["aperture_radius"]),
    snap_radius=int(e["photutils_cols_snap_radius"]), xy_bounds=e["photutils_cols_xy_bounds"] or None,
    num_processes=1)
sec = time.perf_counter() - t
rep = {"sources": len(sel), "seconds": round(sec, 3), "batch": os.environ.get("RAPID_EXP_FIT_BATCH")}
for name in ("flux_fit", "x_fit", "y_fit", "snr_fit", "reduced_chi2", "n_pixels_fit"):
    a, b = new[name], old[name][sel]
    both = np.isfinite(a) & np.isfinite(b)
    rep[name] = {"identical": bool(np.array_equal(a, b, equal_nan=True)), "both_finite": int(both.sum()),
                 "nan_mismatch": int((np.isfinite(a) != np.isfinite(b)).sum())}
    if both.any():
        d = a[both] - b[both]
        if name == "flux_fit":
            sig = (old["flux_fit"][sel] / old["snr_fit"][sel])[both]
            d = d / np.abs(sig); rep[name]["unit"] = "flux_err"
        d = d[np.isfinite(d)]
        tol = 0.1 if name == "flux_fit" else 0.01
        rep[name].update(median_abs=float(np.median(np.abs(d))), p99_abs=float(np.percentile(np.abs(d), 99)),
                         max_abs=float(np.max(np.abs(d))), within=tol,
                         fraction_within=float(np.mean(np.abs(d) <= tol)))
print(json.dumps(rep))
