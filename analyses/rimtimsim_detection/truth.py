"""Stage 2 -- per-science-image truth: source pixel positions and differential flux.

For each science job the differential flux of every catalogued source is

    dflux = f(t_sci) - < f(t_ref,j) >_j

where the average runs over the reference image's constituent exposures.  Both
terms come straight out of the delivered light curves, which are already in DN/s
on the difference image's own zeropoint, so no photometry or unit conversion is
involved.

Three properties of this dataset make the reconstruction exact rather than
approximate, all verified against the 20260813 products:

* The light-curve epoch grid IS the image epoch grid -- `OBS_TIME_BJD - 2400000.5`
  equals the difference image's `MJD-OBS` to full precision.  Truth is a lookup,
  not an interpolation.
* Each reference is built from 25 named constituent exposures, listed with their
  MJDs in the reference job's log.  The reference is shared by every science image
  of that filter, so the baseline is computed once per filter.
* The reference window covers only the first ~13 days of a ~70 day survey, so
  early science frames are themselves reference constituents.  That self-inclusion
  dilutes their own signal by ~1/25; the formula above accounts for it exactly
  because the constituent list is known.
"""
import os
import re
import subprocess

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS


# A reference log records each constituent as a long comma-separated row.  The
# columns used here (1-based): 12 = the S3 path of the input exposure, 16 = its
# MJD-OBS.  Rows are identified by having enough fields and an s3:// path.
_REF_ROW_MIN_FIELDS = 17
_S3_FIELD = 11          # 0-based
_MJD_FIELD = 15         # 0-based


def _sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)


def fetch_status(uri, dest):
    """Download `uri` to `dest` unless already cached; report which happened.

    Callers that clean up after themselves need to know the difference: a file
    that was already there belongs to whoever put it there -- possibly a cache
    shared with other people -- and must not be deleted.
    """
    if os.path.exists(dest):
        return dest, False
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if _sh("aws s3 cp %s %s --quiet" % (uri, dest)).returncode != 0:
        raise IOError("download failed: %s" % uri)
    return dest, True


def fetch(uri, dest):
    """Download `uri` to `dest` unless it is already cached."""
    return fetch_status(uri, dest)[0]


def reference_constituents(cfg, ref_jid, cachedir):
    """MJDs and filter of the exposures that went into one reference image."""
    log = fetch(cfg.log_uri(ref_jid), os.path.join(cachedir, "reflog_%d.txt" % ref_jid))
    mjds, paths = [], []
    with open(log, errors="replace") as fh:
        for line in fh:
            if "s3://" not in line:
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < _REF_ROW_MIN_FIELDS:
                continue
            if not parts[_S3_FIELD].startswith("s3://"):
                continue
            try:
                mjds.append(float(parts[_MJD_FIELD]))
            except ValueError:
                continue
            paths.append(parts[_S3_FIELD])
    if not mjds:
        raise ValueError("no constituent rows found in the log for jid%d" % ref_jid)
    order = np.argsort(mjds)
    mjds = np.array(mjds)[order]
    paths = [paths[i] for i in order]
    # de-duplicate: the log echoes each constituent more than once
    keep = np.concatenate([[True], np.diff(mjds) > 1e-9])
    mjds, paths = mjds[keep], [p for p, k in zip(paths, keep) if k]
    filt = None
    m = re.search(r"_(F\d{3})_", paths[0])
    if m:
        filt = m.group(1)
    return mjds, paths, filt


def reference_map(cfg, cachedir):
    """Map each Roman filter to its reference jid and constituent MJDs."""
    out = {}
    for jid in cfg.reference_jids:
        mjds, paths, trexs_filt = reference_constituents(cfg, jid, cachedir)
        # translate the TRExS spelling in the filenames back to the Roman name
        roman = next((f for f in cfg.filters if cfg.trexs_filter(f) == trexs_filt), None)
        if roman is None:
            raise ValueError("reference jid%d has unrecognised filter %r" % (jid, trexs_filt))
        out[roman] = dict(jid=jid, mjd=mjds, n=len(mjds))
    missing = [f for f in cfg.filters if f not in out]
    if missing:
        raise ValueError("no reference image found for filter(s) %s" % missing)
    return out


# A difference image is ~67 MB but its header is a few kB, and the truth stage
# needs nothing else from it.  Pulling the whole file for every job would move
# ~18 GB to read 263 headers, so fetch a leading byte range instead.
_HEADER_BYTES = 131072


def fetch_header(uri, dest):
    """Download just enough of a FITS file to contain its primary header."""
    if not os.path.exists(dest):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        bucket, key = uri[len("s3://"):].split("/", 1)
        cmd = ("aws s3api get-object --bucket %s --key %s --range bytes=0-%d %s"
               % (bucket, key, _HEADER_BYTES - 1, dest))
        if _sh(cmd + " >/dev/null").returncode != 0:
            raise IOError("header fetch failed: %s" % uri)
    return dest


def diff_header(cfg, jid, cachedir, diff="sfft"):
    """Header of a science job's difference image (filter, epoch, zeropoint, WCS).

    Reads a truncated copy, so astropy is told not to object to the missing data.
    """
    from . import sweep as _sweep
    name = _sweep.DIFF_FILES[diff]
    full = os.path.join(cachedir, "%d_%s" % (jid, name))
    if os.path.exists(full):
        with fits.open(full) as hdul:
            return dict(hdul[0].header), full
    stub = fetch_header(cfg.product_uri(jid, name),
                        os.path.join(cachedir, "hdr_%d_%s" % (jid, name)))
    with fits.open(stub, ignore_missing_end=True) as hdul:
        return dict(hdul[0].header), stub


def build(cfg, jid, refmap, cachedir=None, force=False):
    """Build (or reuse) the truth table for one science job."""
    cachedir = cachedir or cfg.cache
    dest = os.path.join(cfg.work, cfg.paths["truth"], "%d.npz" % jid)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.exists(dest) and not force:
        try:
            with np.load(dest, allow_pickle=True) as chk:
                _ = chk["dflux"]
            return dest
        except Exception:
            os.remove(dest)      # truncated or corrupt: rebuild rather than skip

    hdr, path = diff_header(cfg, jid, cachedir)
    filt = str(hdr["FILTER"]).strip()
    mjd_sci = float(hdr["MJD-OBS"])
    zp = float(hdr["ZPTMAG"])
    wcs = WCS(fits.Header(hdr))
    ny, nx = int(hdr["NAXIS2"]), int(hdr["NAXIS1"])

    cat = np.load(os.path.join(cfg.work, "catalog_%s.npz" % filt), allow_pickle=True)
    mjd_grid, flux = cat["mjd"], cat["flux"]

    # Epoch lookup: the light-curve grid and the image grid are the same set.
    k = int(np.argmin(np.abs(mjd_grid - mjd_sci)))
    if abs(mjd_grid[k] - mjd_sci) > 1e-6:
        raise ValueError("jid%d MJD-OBS %.8f has no light-curve epoch within 1e-6 d "
                         "(closest %.8f)" % (jid, mjd_sci, mjd_grid[k]))

    ref = refmap[filt]
    idx = [int(np.argmin(np.abs(mjd_grid - m))) for m in ref["mjd"]]
    bad = [m for m, i in zip(ref["mjd"], idx) if abs(mjd_grid[i] - m) > 1e-6]
    if bad:
        raise ValueError("jid%d: %d reference constituent epochs are absent from the "
                         "light-curve grid (e.g. %.6f)" % (jid, len(bad), bad[0]))

    f_sci = flux[k, :].astype(np.float64)
    f_ref = np.nanmean(flux[idx, :].astype(np.float64), axis=0)
    dflux = f_sci - f_ref

    # Sky -> pixel on the reformatted difference-image frame (origin 0).
    x, y = wcs.all_world2pix(cat["ra"], cat["dec"], 0)
    on = np.isfinite(x) & np.isfinite(y) & (x >= 0) & (y >= 0) & (x < nx) & (y < ny)

    keep = dict(sicbro_id=cat["sicbro_id"][on], x=x[on], y=y[on],
                dflux=dflux[on], f_sci=f_sci[on], f_ref=f_ref[on],
                mag=cat["mag"][on], is_rapid=cat["is_rapid"][on])
    for f in ("transitHost", "dimEB", "blendedEB", "lowmassEB",
              "lowRedNoise", "hiRedNoise", "variable"):
        if f in cat:
            keep[f] = cat[f][on]

    # Write to a sibling temporary file and rename.  Rename is atomic on POSIX,
    # so an interrupted run leaves either no file or a complete one -- never a
    # truncated one that the `os.path.exists` check above would trust forever.
    # NB: savez_compressed appends ".npz" when handed a path that lacks it, which
    # would defeat the rename below -- so hand it an open file object instead.
    tmp_dest = dest + ".partial"
    with open(tmp_dest, "wb") as fh:
        np.savez_compressed(fh, jid=jid, filt=filt, mjd_sci=mjd_sci, zp=zp,
                            ref_jid=ref["jid"], n_ref=ref["n"],
                            self_in_ref=bool(np.any(np.abs(ref["mjd"] - mjd_sci) < 1e-6)),
                            **keep)
    os.replace(tmp_dest, dest)
    return dest
