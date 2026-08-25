"""Stage 3 -- run the detection matrix over each difference image.

The variant set matches the SOC downselect so the two studies can be compared
directly: three DAOStarFinder families, two StarFinder (gaussian-kernel) families,
four SExtractor families, each at four thresholds, plus the production SExtractor
configuration -- 37 variants per image.

Two things differ from the SOC run, both forced by the data:

* Detection runs on BOTH sign branches.  The TRExS variables are eclipsing
  binaries and transiting planets, i.e. dips, which appear as negative residuals;
  the RAPID-added variables are brightenings.  The pipeline already writes an
  explicit `*_masked_negative.fits` for every method, so each branch is a real
  product rather than a locally negated image.
* Matched-filter kernels are built per filter.  The SOC kernels were sized for
  W146; Z087 and K213 sit at opposite ends of the Roman wavelength range and have
  substantially different PSF widths.
"""
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import warnings

import numpy as np

# photutils 3.0 renamed xcentroid/ycentroid and warns on every call; the compat
# mapping still works and the rename is handled where the columns are read.
warnings.filterwarnings("ignore", message=".*centroid.* was deprecated.*")
warnings.filterwarnings("ignore", category=UserWarning, module="photutils")
from astropy.io import fits
from astropy.stats import sigma_clipped_stats
from scipy.spatial import cKDTree

from photutils.detection import DAOStarFinder, StarFinder


DIFF_FILES = {
    "sfft":  "sfftdiffimage_masked.fits",
    "zogy":  "zogy_diffimage_masked.fits",
    "naive": "naive_diffimage_masked.fits",
}
DIFF_FILES_NEG = {
    "sfft":  "sfftdiffimage_masked_negative.fits",
    "zogy":  "zogy_diffimage_masked_negative.fits",
    "naive": "naive_diffimage_masked_negative.fits",
}

# DAOStarFinder families.  "production" carries the pipeline's own sharpness and
# roundness gates; the "case6" families open them up, which is what makes the pair
# an in-place morphology-filter A/B.
DAO_FAMS = {
    "DAO-production": dict(sharplo=0.2, sharphi=1.0, roundlo=-1.0, roundhi=1.0),
    "DAO-case6-fN":   dict(sharplo=-1.0, sharphi=10.0, roundlo=-2.0, roundhi=2.0),
    "DAO-case6-fW":   dict(sharplo=-1.0, sharphi=10.0, roundlo=-2.0, roundhi=2.0),
}
SE_PRODUCTION = dict(thresh=2.5, minarea=5, filt="N", kernel=None)


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)


def gauss_kernel(fwhm):
    """Unit-sum gaussian, matching the SExtractor gauss_*.conv profile."""
    sig = fwhm / (2 * np.sqrt(2 * np.log(2)))
    r = int(np.ceil(2.5 * sig))
    yy, xx = np.mgrid[-r:r + 1, -r:r + 1]
    k = np.exp(-(xx ** 2 + yy ** 2) / (2 * sig * sig))
    return k / k.sum()


def dao_kernel(fwhm):
    """The ZERO-SUM kernel DAOStarFinder builds internally, as a filter array.

    This is the other half of the {gaussian, dao} axis.  DAOStarFinder does not
    correlate with a unit-sum PSF but with a mean-subtracted one, which suppresses
    a flat background instead of passing it through.  Handing SExtractor the same
    kernel is what makes `SE-dao` a real comparison against `SE-gauss` rather than
    a second name for it.

    photutils keeps this class private, so it is imported defensively and the
    profile is reconstructed if the private name ever moves.  It is deliberately
    photutils' own kernel and not an independent implementation, so that the
    `DAO-*` and `SE-dao-*` families are filtering with literally the same thing.
    """
    try:
        from photutils.detection.core import _StarFinderKernel
    except ImportError:                              # moved or removed upstream
        try:
            from photutils.detection.daofinder import _StarFinderKernel
        except ImportError:
            return _dao_kernel_fallback(fwhm)
    return _StarFinderKernel(fwhm, ratio=1.0, theta=0.0, sigma_radius=1.5).data


def _dao_kernel_fallback(fwhm):
    """DAOStarFinder's kernel, reimplemented -- used only if photutils moves it.

    A circular gaussian truncated at `sigma_radius` sigma, mean-subtracted over
    the pixels inside that radius so the kernel sums to zero, with the pixels
    outside it set to zero.  This mirrors `_StarFinderKernel`; it is a fallback,
    not the primary path, because matching photutils exactly is the point.
    """
    sigma = fwhm / (2.0 * np.sqrt(2.0 * np.log(2.0)))
    r = max(2, int(np.ceil(1.5 * sigma)))
    yy, xx = np.mgrid[-r:r + 1, -r:r + 1]
    g = np.exp(-0.5 * (xx ** 2 + yy ** 2) / sigma ** 2)
    inside = (xx ** 2 + yy ** 2) <= r ** 2
    g = np.where(inside, g, 0.0)
    g[inside] -= g[inside].mean()
    return np.where(inside, g, 0.0)


def write_conv(path, kernel, comment, norm=True):
    """Write a SExtractor .conv filter file.

    A zero-sum kernel must be declared NONORM: SExtractor normalises a NORM
    filter by its sum, which for a mean-subtracted kernel is ~0 and blows the
    coefficients up.
    """
    with open(path, "w") as fh:
        fh.write("CONV %s\n# %s\n" % ("NORM" if norm else "NONORM", comment))
        for row in kernel:
            fh.write(" ".join("%.6f" % v for v in row) + "\n")


def build_kernels(kdir, fwhm_narrow, fwhm_wide):
    """Create the per-filter matched-filter kernels and return their paths.

    Two profiles per filter width: a unit-sum gaussian and the zero-sum DAO
    kernel.  Returns {tag: (gauss_path, dao_path, fwhm)}.
    """
    os.makedirs(kdir, exist_ok=True)
    out = {}
    for tag, fwhm in (("fN", fwhm_narrow), ("fW", fwhm_wide)):
        g = os.path.join(kdir, "gauss_%s.conv" % tag)
        write_conv(g, gauss_kernel(fwhm), "gaussian, FWHM %.3f px" % fwhm)
        d = os.path.join(kdir, "dao_%s.conv" % tag)
        write_conv(d, dao_kernel(fwhm),
                   "photutils StarFinderKernel (zero-sum), FWHM %.3f px" % fwhm,
                   norm=False)
        out[tag] = (g, d, fwhm)
    return out


def run_sex(sexbin, cdf, img, tmp, thresh, minarea, filt, kernel):
    """Run SExtractor and return detections as an (N,2) array of 0-based x,y."""
    par = os.path.join(tmp, "p.param")
    with open(par, "w") as fh:
        fh.write("NUMBER\nXWIN_IMAGE\nYWIN_IMAGE\nFLUX_APER(1)\nFLAGS\n")
    cat = os.path.join(tmp, "out.cat")
    cfg = ["-CATALOG_NAME %s" % cat, "-CATALOG_TYPE ASCII_HEAD",
           "-PARAMETERS_NAME %s" % par, "-DETECT_TYPE CCD",
           "-DETECT_MINAREA %d" % minarea, "-THRESH_TYPE RELATIVE",
           "-DETECT_THRESH %.3f" % thresh, "-ANALYSIS_THRESH %.3f" % thresh,
           "-FILTER %s" % filt, "-DEBLEND_NTHRESH 32", "-DEBLEND_MINCONT 0.005",
           "-CLEAN Y", "-CLEAN_PARAM 1.0", "-WEIGHT_TYPE NONE", "-PHOT_APERTURES 6",
           "-STARNNW_NAME %s" % os.path.join(cdf, "rapidSexDiffImageStarGalaxyClassifier.nnw"),
           "-BACK_SIZE 64", "-BACK_FILTERSIZE 3", "-VERBOSE_TYPE QUIET"]
    if filt == "Y" and kernel:
        cfg.append("-FILTER_NAME %s" % kernel)
    sh("%s %s %s" % (sexbin, img, " ".join(cfg)))
    if not os.path.exists(cat):
        return None
    try:
        d = np.genfromtxt(cat, comments="#")
    except Exception:
        return None
    if d.size == 0:
        return np.zeros((0, 2))
    d = np.atleast_2d(d)
    # SExtractor image coordinates are 1-based; the truth positions are 0-based.
    return np.column_stack([d[:, 1] - 1.0, d[:, 2] - 1.0])


def run_dao(data, std, nsig, fwhm, pars):
    t = DAOStarFinder(threshold=nsig * std, fwhm=fwhm, min_separation=1.0, **pars)(data)
    if t is None or len(t) == 0:
        return np.zeros((0, 2))
    return np.column_stack([np.asarray(t["xcentroid"]), np.asarray(t["ycentroid"])])


def run_pu(data, std, nsig, fwhm):
    t = StarFinder(threshold=nsig * std, kernel=gauss_kernel(fwhm), min_separation=1.0)(data)
    if t is None or len(t) == 0:
        return np.zeros((0, 2))
    return np.column_stack([np.asarray(t["xcentroid"]), np.asarray(t["ycentroid"])])


def measure(det, tx, ty, match_px):
    """Match detections against truth positions; return per-source recovery flags."""
    n = len(det)
    if n == 0:
        return dict(n_det=0, matched=np.zeros(len(tx), bool), n_fp=0)
    d_inj, _ = cKDTree(det).query(np.column_stack([tx, ty]))
    d_det, _ = cKDTree(np.column_stack([tx, ty])).query(det)
    return dict(n_det=n, matched=d_inj <= match_px,
                n_fp=int((d_det > match_px).sum()))


def signature(kind, params):
    """A short hash identifying exactly what a variant computes.

    Stored alongside each variant's results so a later run can tell which entries
    are still current.  For SExtractor variants the kernel FILE CONTENT is hashed,
    not its path -- that is what caught `SE-dao` and `SE-gauss` silently sharing
    one kernel, and it means editing a kernel invalidates precisely the variants
    that used it instead of the whole matrix.
    """
    h = hashlib.sha256()
    h.update(kind.encode())
    for k in sorted(params):
        if k == "kernel" and params[k]:
            h.update(b"kernel=")
            with open(params[k], "rb") as fh:
                h.update(fh.read())
        else:
            h.update(("%s=%r;" % (k, params[k])).encode())
    return h.hexdigest()[:16]


def variants(kernels, thresholds):
    """Enumerate (label, kind, params) for the whole matrix."""
    out = []
    for fam, pars in DAO_FAMS.items():
        tag = "fN" if fam.endswith("fN") else ("fW" if fam.endswith("fW") else "fW")
        fwhm = kernels[tag][2]
        for t in thresholds:
            out.append(("%s@%g" % (fam, t), "dao", dict(nsig=t, fwhm=fwhm, pars=pars)))
    for tag in ("fN", "fW"):
        for t in thresholds:
            out.append(("PU-gauss-%s@%g" % (tag, t), "pu",
                        dict(nsig=t, fwhm=kernels[tag][2])))
    for tag in ("fN", "fW"):
        for t in thresholds:
            # gauss = unit-sum PSF filter; dao = the zero-sum StarFinderKernel.
            # These MUST be different files -- handing both the same kernel makes
            # the two families bitwise duplicates of each other.
            out.append(("SE-gauss-%s@%g" % (tag, t), "sex",
                        dict(thresh=t, minarea=1, filt="Y", kernel=kernels[tag][0])))
            out.append(("SE-dao-%s@%g" % (tag, t), "sex",
                        dict(thresh=t, minarea=1, filt="Y", kernel=kernels[tag][1])))
    out.append(("SE-production@2.5", "sex", dict(**SE_PRODUCTION)))
    return out


def process(cfg, jid, diff, branch, kernels, sexbin, cdf, cachedir, outdir,
            keep=True, select=None, refresh=False):
    """Run the detection matrix for one (job, difference image, sign branch).

    Resumable AT VARIANT GRANULARITY.  An existing result file is not all-or-
    nothing: each variant carries a signature, and only variants that are absent,
    stale (their parameters or kernel changed), or explicitly selected for refresh
    are recomputed.  Everything else is carried over untouched and merged into the
    output.  Adding one variant to the matrix, or correcting one, therefore costs
    that variant -- not a rerun of the other thirty-six.

    `select` is a regex matched against variant labels; `refresh` recomputes the
    selected ones even when their signature already matches.
    """
    os.makedirs(outdir, exist_ok=True)
    dest = os.path.join(outdir, "%d.npz" % jid)

    want = variants(kernels, cfg.sweep["thresholds"])
    if select:
        rx = re.compile(select)
        want = [v for v in want if rx.search(v[0])]
        if not want:
            return "no variant matches %r" % select

    prev, prev_labels = {}, []
    if os.path.exists(dest):
        try:
            with np.load(dest, allow_pickle=True) as Z:
                prev = {k: Z[k] for k in Z.files}
            prev_labels = [str(v) for v in prev.get("variants", [])]
        except Exception:
            prev, prev_labels = {}, []          # unreadable: rebuild from scratch

    todo = []
    for label, kind, p in want:
        sig = signature(kind, p)
        have_sig = str(prev[label + "|sig"]) if label + "|sig" in prev else None
        if label + "|matched" not in prev:
            todo.append((label, kind, p, sig))          # never computed
        elif refresh:
            todo.append((label, kind, p, sig))          # asked for explicitly
        elif have_sig is not None and have_sig != sig:
            todo.append((label, kind, p, sig))          # parameters or kernel changed
        # A result predating signatures carries none.  It is GRANDFATHERED rather
        # than assumed stale: treating it as stale would make the first run under
        # the new format silently recompute the entire matrix, which is precisely
        # the all-or-nothing behaviour this resume logic exists to remove.  Use
        # --refresh-variants to force such a variant to be recomputed.
    if not todo and prev:
        return "cached (%d variants)" % len(prev_labels)

    T = np.load(os.path.join(cfg.work, cfg.paths["truth"], "%d.npz" % jid), allow_pickle=True)
    tx, ty, dflux = T["x"], T["y"], T["dflux"]
    ok = np.isfinite(dflux)
    tx, ty, dflux = tx[ok], ty[ok], dflux[ok]

    files = DIFF_FILES if branch == "positive" else DIFF_FILES_NEG
    from .truth import fetch_status
    path, fetched = fetch_status(cfg.product_uri(jid, files[diff]),
                                 os.path.join(cachedir, "%d_%s" % (jid, files[diff])))

    with fits.open(path) as hdul:
        data = hdul[0].data.astype(np.float32)
    clean = np.nan_to_num(data, nan=0.0, posinf=0.0, neginf=0.0)
    _, _, std = sigma_clipped_stats(data[np.isfinite(data)], sigma=3.0)

    tmproot = os.path.join(cfg.work, "tmp")
    os.makedirs(tmproot, exist_ok=True)
    tmp = tempfile.mkdtemp(dir=tmproot)

    # Carry over every key already on disk, then overwrite what we recompute.  The
    # per-source arrays come from truth, which is unchanged, so they are simply
    # rewritten; anything belonging to a variant we are not touching survives.
    out = dict(prev)
    out.update(jid=jid, diff=diff, branch=branch, dflux=dflux, clipped_std=std,
               zp=float(T["zp"]), filt=str(T["filt"]), sicbro_id=T["sicbro_id"][ok],
               is_rapid=T["is_rapid"][ok], mag=T["mag"][ok])
    done = []
    try:
        cimg = os.path.join(tmp, "clean.fits")
        fits.PrimaryHDU(clean).writeto(cimg, overwrite=True)
        for label, kind, p, sig in todo:
            if kind == "dao":
                det = run_dao(clean, std, p["nsig"], p["fwhm"], p["pars"])
            elif kind == "pu":
                det = run_pu(clean, std, p["nsig"], p["fwhm"])
            else:
                det = run_sex(sexbin, cdf, cimg, tmp, p["thresh"], p["minarea"],
                              p["filt"], p["kernel"])
                if det is None:
                    continue
            r = measure(det, tx, ty, float(cfg.truth["match_px"]))
            out[label + "|matched"] = r["matched"]
            out[label + "|scalars"] = np.array([r["n_det"], r["n_fp"]], np.int64)
            out[label + "|sig"] = sig
            done.append(label)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        # Only clean up an image THIS call downloaded.  One that was already
        # cached may belong to a cache shared with other people, and deleting it
        # would silently cost them a re-download -- or pull it out from under a
        # concurrent reader.
        if not keep and fetched and os.path.exists(path):
            os.remove(path)

    labels = prev_labels + [l for l in done if l not in prev_labels]
    out["variants"] = np.array(labels)
    # savez_compressed would append ".npz" to a path lacking it; use a handle.
    tmp_dest = dest + ".partial"
    with open(tmp_dest, "wb") as fh:
        np.savez_compressed(fh, **out)
    os.replace(tmp_dest, dest)
    verb = "merged" if prev_labels else "wrote"
    return "std=%.3f n_src=%d %s %d/%d variants" % (std, len(tx), verb,
                                                    len(done), len(labels))
