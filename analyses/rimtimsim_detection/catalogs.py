"""Stage 1 -- ingest the variable catalogue and light curves into a compact table.

Inputs are the delivery archive (`catalog_RAPID.txt` plus one wide parquet of light
curves per filter).  Output is one `catalog_<FILTER>.npz` per filter holding the
sources that actually have a light curve, their sky positions and magnitudes, the
epoch grid, and the light-curve matrix.

Two facts about the delivery that this stage depends on, both verified against the
20260813 products:

* The parquet is WIDE -- rows are epochs, columns are one per source named by
  `sicbro_id`.  It is NOT a long-format table.
* Light-curve values are already FLUX IN DN/s on the same zeropoint as the
  difference images, so no photometric conversion is needed anywhere downstream.
  (Implied zeropoint from quiescent flux vs catalogue magnitude: 26.2982 for Z087
  against a header ZPTMAG of 26.29818; 25.8573 for K213 against 25.85727.)

Note that `pyarrow` 19.0.0 cannot read these files -- it raises "Repetition level
histogram size mismatch" on their SizeStatistics.  Use >= 20.
"""
import os
import zipfile

import numpy as np


def _require_pyarrow():
    import pyarrow
    major = int(pyarrow.__version__.split(".")[0])
    if major < 20:
        raise RuntimeError(
            "pyarrow %s cannot read the RimTimSim light-curve parquet "
            "(SizeStatistics bug); need >= 20.0" % pyarrow.__version__)
    import pyarrow.parquet as pq
    return pq


def _open_member(cfg, name, workdir):
    """Return a filesystem path to `name`, extracting it from the archive if needed."""
    direct = os.path.join(workdir, name)
    if os.path.exists(direct):
        return direct
    archive = os.path.join(workdir, cfg.catalogs["archive"])
    if not os.path.exists(archive):
        raise FileNotFoundError(
            "neither %s nor the archive %s is present" % (direct, archive))
    with zipfile.ZipFile(archive) as zf:
        zf.extract(name, workdir)
    return direct


def read_catalog(cfg, workdir, wanted_ids):
    """Read the TAB-separated master catalogue, keeping only `wanted_ids`.

    The file is ~795 MB and ~5 M rows, so it is streamed rather than loaded.
    """
    path = _open_member(cfg, cfg.catalogs["catalog"], workdir)
    want = set(int(i) for i in wanted_ids)
    out = {}
    with open(path) as fh:
        hdr = fh.readline().rstrip("\n").split("\t")
        col = {n: i for i, n in enumerate(hdr)}
        for line in fh:
            p = line.rstrip("\n").split("\t")
            try:
                sid = int(p[col["sicbro_id"]])
            except (ValueError, IndexError):
                continue                      # the header repeats in some deliveries
            if sid in want:
                out[sid] = p
    return hdr, col, out


def build(cfg, filt, workdir=None, force=False):
    """Build (or reuse) the per-filter catalogue table."""
    pq = _require_pyarrow()
    workdir = workdir or cfg.catalog_dir
    dest = os.path.join(cfg.work, "catalog_%s.npz" % filt)
    if os.path.exists(dest) and not force:
        return dest

    lc_path = _open_member(cfg, cfg.lightcurve_file(filt), workdir)
    table = pq.read_table(lc_path)
    names = table.schema.names
    tcol = cfg.catalogs["time_column"]
    if names[0] != tcol:
        raise ValueError("expected %r as the first column, found %r" % (tcol, names[0]))

    mjd = table[tcol].to_numpy() - cfg.catalogs["jd_to_mjd"]
    src_names = names[1:]
    sid = np.array([int(n) for n in src_names], dtype=np.int64)
    flux = np.column_stack([table[n].to_numpy() for n in src_names]).astype(np.float32)

    hdr, col, rows = read_catalog(cfg, workdir, sid)
    missing = [int(s) for s in sid if int(s) not in rows]
    if missing:
        raise ValueError("%d light-curve sources absent from the catalogue (e.g. %s)"
                         % (len(missing), missing[:5]))

    magcol = cfg.trexs_filter(filt)
    ra = np.array([float(rows[int(s)][col["RA_DEG"]]) for s in sid])
    dec = np.array([float(rows[int(s)][col["DEC_DEG"]]) for s in sid])
    mag = np.array([float(rows[int(s)][col[magcol]]) for s in sid])
    mag[mag >= cfg.catalogs["mag_sentinel"]] = np.nan

    # TRExS variability-class flags travel with the source so results can be
    # stratified by class later without re-reading the 795 MB catalogue.
    flags = {}
    for f in ("transitHost", "dimEB", "blendedEB", "lowmassEB",
              "lowRedNoise", "hiRedNoise", "variable"):
        if f in col:
            flags[f] = np.array([int(float(rows[int(s)][col[f]])) for s in sid], np.int8)

    is_rapid = sid >= int(cfg.catalogs["rapid_id_min"])
    np.savez_compressed(dest, sicbro_id=sid, ra=ra, dec=dec, mag=mag, mjd=mjd,
                        flux=flux, is_rapid=is_rapid, filt=filt, magcol=magcol,
                        **flags)
    return dest
