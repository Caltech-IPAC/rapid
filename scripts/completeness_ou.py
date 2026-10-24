"""Join an OpenUniverse run's official sources and packets to the simulation's per-image truth.

The sibling of ``completeness.py`` for an ``[intake] reader = "openuniverse"``
run, outside the pipeline like it: it reads the database, IRSA's per-image
truth index for each official difference image's (filter, pointing, SCA)
(``Roman_TDS_index_<filter>_<pointing>_<sca>.txt``: one row per object,
one-based ``x``, ``y``, ``realized_flux`` in counts, ``mag``) and the
published L2 files' headers and truth tables, and writes nothing but its
own tables. The match, the joins to sources and packets and the per-bin
table are ``completeness.py``'s (``match``, ``join``, ``bins``, ``history``).

The injected set of an epoch is its index's transient rows with realised
counts above zero inside the frame; its AB magnitude is ``mag`` + 2.5
log10(37570 x EXPTIME) (runbook part 11 step 2). Its position is the
published L2 WCS at ``(x - 1, y - 1)``, which puts the index's objects on
the image to 0.0016 px; a detection is the nearest official source of the
epoch within ``--radius-px`` pixels (0.11 arcsec each). The reference was
built from the first visits under ``[reference.selection] before_mjd``; each injected transient is then one of three
groups, and only the first is counted:

- ``evaluated``: its ``start_mjd`` (the L2 file's TRANSIENTS table) is after
  the reference's last frame, and it has no realised counts in any
  reference frame's index;
- ``reference``: realised counts above zero in a reference frame (the
  reference holds some of its light), reported apart, never counted missed;
- ``early``: it started before the reference's last frame but has no
  counts in any reference frame, reported apart.

The reference is the one each official difference job named as its
``refimages`` input, so a run that differences against another run's
reference reads that reference's frames.

Purity, over every official packet, by the packet's own magnitude
(``ZPTMAG`` - 2.5 log10(fluxfit)): its source matched to a transient row of
its epoch with counts (any group), else carrying the bright-footprint flag
(``flags & 16384``), else matched to a galaxy row, else to a star row, else
``other``.

Usage::

    python scripts/completeness_ou.py --run N [--radius-px 1.5] [--out DIR] [--no-history]

Writes ``truth.parquet`` (one row per injected row, its group and match),
``bins.parquet`` (evaluated rows), ``bins_group.parquet`` (per group),
``bins_sca.parquet``, ``purity.parquet``, ``epochs.parquet`` (per epoch: the
classes, ``false_pos`` the packets not on a transient, and the difference job's
``n_candidates`` and ``container_seconds``) and
``history.parquet`` under ``--out`` (default ``$TMPDIR/completeness-ou-<run>``)
and prints the tables.
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

_SPEC = importlib.util.spec_from_file_location("completeness", Path(__file__).resolve().parent / "completeness.py")
completeness = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(completeness)

#: The OpenUniverse collecting area: the truth's photon flux to counts is 37570 cm^2 x EXPTIME.
AREA_CM2 = 37570.0
PIXEL_ARCSEC = 0.11
NPIX = 4088
INDEX = ("openuniverse2024/roman/full/RomanTDS/truth/{filter}/{pointing}/"
         "Roman_TDS_index_{filter}_{pointing}_{sca}.txt")
INDEX_BUCKET = "nasa-irsa-simulations"
CLASSES = ("transient", "bright", "galaxy", "star", "other")


def read_index(text: str) -> pd.DataFrame:
    """IRSA's truth index: ``object_id``, ``ra``, ``dec``, ``x``, ``y`` (one-based), ``realized_flux``,
    ``flux``, ``mag``, ``obj_type``."""
    table = pd.read_csv(io.StringIO(text), sep=r"\s+", comment="#", header=None,
                        names=["object_id", "ra", "dec", "x", "y", "realized_flux", "flux", "mag", "obj_type"])
    return table.astype({"object_id": "int64"})


def injected(index: pd.DataFrame, exptime: float) -> pd.DataFrame:
    """The transient rows with realised counts above zero inside the frame, with ``x0``, ``y0`` (zero-based),
    ``mag_ab`` and ``dnps`` (counts per second)."""
    t = index[(index["obj_type"] == "transient") & (index["realized_flux"] > 0)].copy()
    t["x0"], t["y0"] = t["x"] - 1.0, t["y"] - 1.0
    t = t[(t["x0"] >= -0.5) & (t["x0"] < NPIX - 0.5) & (t["y0"] >= -0.5) & (t["y0"] < NPIX - 0.5)]
    t["mag_ab"] = t["mag"] + 2.5 * math.log10(AREA_CM2 * exptime)
    t["dnps"] = t["realized_flux"] / exptime
    return t


def groups(rows: pd.DataFrame, start_mjd: dict[int, float], in_reference: set[int],
           reference_end: float) -> pd.Series:
    """Each injected row's group: ``reference`` (counts in a reference frame), ``early`` (started before
    the reference's last frame, an unknown start counted as early), else ``evaluated``."""
    start = rows["object_id"].map(start_mjd)
    return pd.Series(np.where(rows["object_id"].isin(in_reference), "reference",
                              np.where(~(start > reference_end), "early", "evaluated")), index=rows.index)


def classify(packets: pd.DataFrame, index_rows: pd.DataFrame, radius_arcsec: float) -> pd.Series:
    """Each packet's class (:data:`CLASSES`, in that precedence) from its source's ``ra``, ``dec``,
    ``flags`` and its epoch's index rows (``ra_c``, ``dec_c``, ``obj_type``, ``realized_flux``)."""
    out = pd.Series("other", index=packets.index, dtype=object)
    by_epoch = index_rows.groupby(["expid", "sca"]).indices
    for key, idx in packets.groupby(["expid", "sca"]).indices.items():
        p = packets.iloc[idx]
        found = {}
        if key in by_epoch:
            t = index_rows.iloc[by_epoch[key]]
            cosd = np.cos(np.radians(p["dec"].to_numpy(float)))[:, None]
            dx = ((t["ra_c"].to_numpy()[None, :] - p["ra"].to_numpy(float)[:, None] + 180.0) % 360.0 - 180.0) * cosd
            dy = t["dec_c"].to_numpy()[None, :] - p["dec"].to_numpy(float)[:, None]
            near = np.hypot(dx, dy) * 3600.0 <= radius_arcsec
            for kind in ("transient", "galaxy", "star"):
                mask = (t["obj_type"] == kind).to_numpy()
                if kind == "transient":
                    mask = mask & (t["realized_flux"] > 0).to_numpy()
                found[kind] = (near & mask[None, :]).any(axis=1)
        bright = ((p["flags"].astype("int64") & completeness.BRIGHT_FOOTPRINT) != 0).to_numpy()
        label = np.full(len(p), "other", dtype=object)
        for kind, hit in (("star", found.get("star")), ("galaxy", found.get("galaxy")),
                          ("bright", bright), ("transient", found.get("transient"))):
            if hit is not None:
                label[hit] = kind                  # later assignments take precedence
        out.iloc[idx] = label
    return out


def purity_bins(p: pd.DataFrame) -> pd.DataFrame:
    p = p.assign(mag_bin=np.floor(p["mag"] / completeness.BIN) * completeness.BIN)
    t = p.pivot_table(index="mag_bin", columns="class", values="sid", aggfunc="count", fill_value=0)
    for c in CLASSES:
        if c not in t:
            t[c] = 0
    t = t[list(CLASSES)].reset_index()
    t["packets"] = t[list(CLASSES)].sum(axis=1)
    t["purity"] = t["transient"] / t["packets"]
    return t


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--run", type=int, required=True)
    p.add_argument("--radius-px", type=float, default=1.5, help="match radius, pixels")
    p.add_argument("--out", type=Path)
    p.add_argument("--no-history", action="store_true", help="skip decoding the packets")
    a = p.parse_args(argv)
    out_dir = a.out or Path(os.environ.get("TMPDIR", ".")) / f"completeness-ou-{a.run}"
    out_dir.mkdir(parents=True, exist_ok=True)
    radius = a.radius_px * PIXEL_ARCSEC
    t0 = time.perf_counter()

    import psycopg2.extras
    from astropy.io import fits
    from astropy.wcs import WCS

    from rapidpipe.db.connection import connect
    from rapidpipe.foundation.prefix import get
    from rapidpipe.products import storage

    def q(sql, params=()):
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()]
        conn.commit()
        return rows

    with connect(application_name="completeness-ou") as conn:
        (run,) = q("SELECT dataset FROM runs WHERE id = %s", (a.run,))
        images = q("SELECT d.expid, d.sca, d.field, d.fid, l.filename, l.mjdobs, f.filter"
                   " FROM diffimages_official d JOIN l2files_official l ON l.dataset = d.dataset"
                   " AND l.expid = d.expid AND l.sca = d.sca AND l.fid = d.fid"
                   " JOIN filters f ON f.fid = d.fid WHERE d.run = %s ORDER BY l.mjdobs", (a.run,))
        frames = q("SELECT DISTINCT r.field, r.fid, l.expid, l.sca, l.mjdobs, f.filter FROM diffimages_official d"
                   " JOIN job_inputs n ON n.job = d.job AND n.kind = 'input' AND n.product = 'refimages'"
                   " JOIN refimages r ON r.job = n.source_job JOIN refimimages i ON i.job = r.job AND i.rfid = r.rfid"
                   " JOIN l2files l ON l.rid = i.rid JOIN filters f ON f.fid = l.fid WHERE d.run = %s", (a.run,))
        sources = pd.DataFrame(q("SELECT s.job, s.sid, s.expid, s.sca, s.ra, s.dec, s.fluxfit, s.flags,"
                                 " s.field = d.field AS in_field FROM sources_official s"
                                 " JOIN diffimages d ON d.pid = s.pid WHERE s.run = %s", (a.run,)))
        packets = pd.DataFrame(q(
            "SELECT p.job, p.source_job, p.sid, p.object_job, p.aid, p.expid, p.sca, p.trigger_mjd,"
            " p.filename, p.byte_offset, p.byte_length, j.kafka_published_at IS NOT NULL AS sent"
            " FROM packets_official p JOIN jobs j ON j.id = p.job WHERE p.run = %s", (a.run,)))
        metrics = pd.DataFrame(q(
            "SELECT expid, sca, metric, value FROM job_metrics WHERE run = %s AND stage = 'difference'"
            " AND state = 'published' AND metric IN ('n_candidates', 'container_seconds')", (a.run,)))
        read_sets: dict = {}
        if len(packets):
            for r in q("SELECT i.job, i.source_job FROM job_inputs i WHERE i.kind = 'input'"
                       " AND i.product = 'sources' AND i.job = ANY(%s)",
                       (sorted({int(j) for j in packets["job"]}),)):
                read_sets.setdefault(r["job"], set()).add(r["source_job"])

    client = storage.s3_client(unsigned=True)

    def index_of(filter_name: str, pointing: int, sca: int) -> pd.DataFrame:
        key = INDEX.format(filter=filter_name, pointing=pointing, sca=sca)
        return read_index(client.get_object(Bucket=INDEX_BUCKET, Key=key)["Body"].read().decode())

    def epoch(image: dict) -> tuple[pd.DataFrame, pd.DataFrame, float, dict]:
        """The epoch's index rows through its published WCS, its injected rows, ZPTMAG and start MJDs."""
        with fits.open(get(image["filename"])) as hdul:
            header = hdul[1].header
            wcs = WCS(header)
            starts = {int(i): float(s) for i, s in zip(hdul["TRANSIENTS"].data["id"],
                                                       hdul["TRANSIENTS"].data["start_mjd"])}
        index = index_of(image["filter"], image["expid"], image["sca"])
        ra, dec = wcs.all_pix2world(index["x"].to_numpy() - 1.0, index["y"].to_numpy() - 1.0, 0)
        index = index.assign(ra_c=ra % 360.0, dec_c=dec, expid=image["expid"], sca=image["sca"])
        inj = injected(index, float(header["EXPTIME"]))
        return index, inj, float(header["ZPTMAG"]), starts

    with ThreadPoolExecutor(8) as pool:
        epochs = list(pool.map(epoch, images))
        reference_rows = list(pool.map(lambda f: index_of(f["filter"], f["expid"], f["sca"]), frames))
    in_reference = set()
    for t in reference_rows:
        in_reference |= set(t.loc[(t["obj_type"] == "transient") & (t["realized_flux"] > 0), "object_id"])
    reference_end = max((f["mjdobs"] for f in frames), default=float("-inf"))
    starts: dict[int, float] = {}
    for *_, s in epochs:
        starts.update(s)

    truth = pd.concat([inj.assign(mjd=img["mjdobs"], zp=zp, fid=img["fid"]) for img, (_, inj, zp, _)
                       in zip(images, epochs)], ignore_index=True)
    truth["src_id"] = truth["object_id"]
    truth["group"] = groups(truth, starts, in_reference, reference_end)
    if not len(sources):
        sources = pd.DataFrame(columns=["job", "sid", "expid", "sca", "ra", "dec", "fluxfit", "flags", "in_field"])
    if not len(packets):
        packets = pd.DataFrame(columns=["job", "source_job", "sid", "object_job", "aid", "expid", "sca",
                                        "trigger_mjd", "filename", "byte_offset", "byte_length", "sent"])
    rows = completeness.join(completeness.match(truth, sources, radius), sources, packets)
    evaluated = rows[rows["group"] == "evaluated"]
    table = completeness.bins(evaluated)
    per_group = completeness.bins(rows, ["group"])
    per_sca = completeness.bins(evaluated, ["sca"])

    packets = packets.merge(sources[["job", "sid", "ra", "dec", "fluxfit", "flags"]]
                            .rename(columns={"job": "source_job"}), on=["source_job", "sid"], how="left")
    index_rows = pd.concat([i for i, *_ in epochs], ignore_index=True)
    packets["class"] = classify(packets, index_rows, radius)
    zp = {(img["expid"], img["sca"]): z for img, (_, _, z, _) in zip(images, epochs)}
    z = np.array([zp.get((e, s), np.nan) for e, s in zip(packets["expid"], packets["sca"])])
    with np.errstate(invalid="ignore", divide="ignore"):
        packets["mag"] = z - 2.5 * np.log10(packets["fluxfit"].astype(float).where(packets["fluxfit"] > 0))
    pur = purity_bins(packets)
    ep = packets.groupby(["expid", "sca", "class"]).size().unstack(fill_value=0)
    for c in CLASSES:
        if c not in ep:
            ep[c] = 0
    ep = ep[list(CLASSES)].reset_index()
    det = rows.groupby(["expid", "sca"]).agg(
        mjd=("mjd", "first"), injected=("src_id", "size"), evaluated=("group", lambda g: (g == "evaluated").sum()),
        detected=("detected", "sum"), alerted=("alerted", "sum"), sent=("sent", "sum")).reset_index()
    ep = det.merge(ep, on=["expid", "sca"], how="outer")
    ep["false_pos"] = ep[[c for c in CLASSES if c != "transient"]].fillna(0).sum(axis=1)
    if len(metrics):
        ep = ep.merge(metrics.pivot_table(index=["expid", "sca"], columns="metric", values="value")
                      .reset_index(), on=["expid", "sca"], how="left")
    hist = pd.DataFrame()
    if not a.no_history and evaluated["alerted"].any():
        keys = {(int(j), int(s)) for j, s in zip(evaluated.loc[evaluated["detected"], "job"],
                                                 evaluated.loc[evaluated["detected"], "sid"])}
        want = packets[[(int(j), int(s)) in keys for j, s in zip(packets["source_job"], packets["sid"])]]
        hist = completeness.history(evaluated, completeness._records(want.to_dict("records")), read_sets)

    rows.to_parquet(out_dir / "truth.parquet")
    table.to_parquet(out_dir / "bins.parquet")
    per_group.to_parquet(out_dir / "bins_group.parquet")
    per_sca.to_parquet(out_dir / "bins_sca.parquet")
    pur.to_parquet(out_dir / "purity.parquet")
    ep.to_parquet(out_dir / "epochs.parquet")
    hist.to_parquet(out_dir / "history.parquet")

    pd.set_option("display.width", 200)
    pd.set_option("display.max_rows", 500)
    fmt = {"float_format": lambda v: f"{v:.3f}", "index": False}
    print(f"run {a.run} ({run['dataset']}): {len(images)} official difference images, reference from"
          f" {len(frames)} frames ending MJD {reference_end:.3f}; radius {a.radius_px} px")
    print(f"injected rows {len(rows)}: " + ", ".join(f"{g} {n}" for g, n in rows["group"].value_counts().items()))
    print("\ncompleteness per AB bin, evaluated transients")
    print(table.to_string(**fmt))
    print("\nper group (reference and early reported apart, not counted)")
    print(per_group.to_string(**fmt))
    tot = evaluated[["detected", "alerted", "sent", "in_field"]].sum()
    print(f"\nevaluated: injected {len(evaluated)}, detected {tot['detected']}, alerted {tot['alerted']},"
          f" sent {tot['sent']}, in their image's own field {tot['in_field']};"
          f" offset median {evaluated['offset_arcsec'].median():.3f} arcsec,"
          f" flux ratio median {evaluated['flux_ratio'].median():.3f}")
    print("\npurity per packet magnitude bin")
    print(pur.to_string(**fmt))
    print("\nper epoch (false_pos: packets not on a transient with counts, bright + galaxy + star + other)")
    print(ep.to_string(**fmt))
    print(f"  false positives per epoch median {ep['false_pos'].median():.0f}, min {ep['false_pos'].min():.0f},"
          f" max {ep['false_pos'].max():.0f}; median other (matched to no truth row) {ep['other'].median():.0f}")
    if len(hist):
        h = hist[["expected", "present", "missing", "late", "undetected"]].sum()
        print(f"\nhistory over {hist['src_id'].nunique()} alerted evaluated transients, {len(hist)} later packets:"
              f" expected {h['expected']}, present {h['present']}, missing {h['missing']}, late {h['late']},"
              f" undetected earlier {h['undetected']}")
    print(f"\nwritten to {out_dir} in {time.perf_counter() - t0:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
