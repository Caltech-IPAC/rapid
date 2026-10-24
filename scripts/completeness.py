"""Join a run's official sources and packets to an injected dataset's truth: completeness and purity.

Outside the pipeline, like ``validate_run.py``: it reads the database, the
truth tables ``inject_dataset.py`` wrote and the packet files, and writes
nothing but its own tables. The denominator is every truth row (an
injected source in an injected epoch) of an (expid, SCA) the run has an
official difference image for; truth epochs without one are reported as
unprocessed, never counted as missed. For each truth row, the nearest
official source of the same (expid, SCA) within ``--radius`` arcsec is its
detection; the detection is **alerted** when ``packets_official`` has a
row for it (a left join, so a detection without a packet stays detected),
and **sent** when its packet set carries the topic's acknowledgement
(``jobs.kafka_published_at``); an alerted set not sent is the quality
gate's hold (or a send still owed), reported as held. ``in_field`` marks a
detection in its difference image's own field, a reader of `dev`'s rule's
effect: under it only those founded objects (a neighbouring field's source
joined only an object already within the radius), so run 72 alerted 0.40
of what it detected; under the field rule (runbook part 12 step 1) every
accepted source founds or joins in its own field, and the column says only
where on the image a detection fell.

The truth positions are compared through the corrected intake WCS: the
intake adds ``[intake] crpix_shift`` ``(sx, sy)`` to CRPIX, so the
pipeline's RA/Dec of the pixel ``(x, y)`` where a source was rendered is
the stored WCS at ``(x - sx, y - sy)``. Each epoch's stored WCS is fitted
from its own truth rows (RA and Dec quadratic in ``x``, ``y``; affine under
twelve rows) and evaluated
there; with ``crpix_shift`` ``[0, 0]`` the truth's own RA/Dec is used.

Purity, per magnitude bin of the packets' own magnitudes (``zp - 2.5
log10(fluxfit)``, ``zp`` the epoch's truth zero point): packets on injected
sources, on sources carrying the bright-footprint flag (``flags & 16384``,
informational) and on anything else; the last two are false positives
relative to this catalog. Per epoch the same three counts, beside the
difference job's own counts from ``job_metrics``.

History, over every alerted injected source: for each of its packets after
its first alerted epoch, the earlier alerted epochs of that source are
expected in ``prvDiaSources`` (by source id); a missing one whose source set
the packet job recorded reading is **missing**, one it did not read was
published after the packet's read (**late**, the best-information
contract); earlier injected epochs never detected are counted apart. The
packet records are decoded from their files by their index locators, as
``read_packets.py`` does. The topic itself is tied to these rows by
``check_run.sh``'s consumer check (every stamped set's messages read
back), so this script does not read the topic.

Usage::

    python scripts/completeness.py --run N [--truth F.parquet ...] [--radius 0.3] [--out DIR]

Without ``--truth`` it reads every Parquet under the dataset's
``catalog/`` prefix, the run's ``[intake] source_root`` with its last
component (``l2/``) replaced. Writes ``truth.parquet`` (one row per truth
row, its match), ``bins.parquet``, ``epochs.parquet``, ``purity.parquet``
and ``history.parquet`` under ``--out`` (default ``$TMPDIR/completeness-<run>``)
and prints the tables.
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

BRIGHT_FOOTPRINT = 16384
BIN = 0.5


def _terms(x, y):
    return np.column_stack([np.ones_like(x), x, y, x * x, x * y, y * y])


def corrected_positions(truth: pd.DataFrame, shift: tuple[float, float]) -> pd.DataFrame:
    """``ra_c``, ``dec_c``: each truth row's position through the shifted WCS, per epoch."""
    truth = truth.copy()
    sx, sy = shift
    if not (sx or sy):
        truth["ra_c"], truth["dec_c"] = truth["ra"], truth["dec"]
        return truth
    ra_c = np.empty(len(truth))
    dec_c = np.empty(len(truth))
    for _, idx in truth.groupby(["expid", "sca"]).indices.items():
        t = truth.iloc[idx]
        x, y = t["x"].to_numpy(), t["y"].to_numpy()
        terms = 6 if len(t) >= 12 else 3          # quadratic where the rows determine it, else affine
        design = _terms(x, y)[:, :terms]
        moved = _terms(x - sx, y - sy)[:, :terms]
        ra0 = t["ra"].to_numpy()[0]
        dra = (t["ra"].to_numpy() - ra0 + 180.0) % 360.0 - 180.0
        ra_c[idx] = ra0 + moved @ np.linalg.lstsq(design, dra, rcond=None)[0]
        dec_c[idx] = moved @ np.linalg.lstsq(design, t["dec"].to_numpy(), rcond=None)[0]
    truth["ra_c"], truth["dec_c"] = ra_c % 360.0, dec_c
    return truth


def match(truth: pd.DataFrame, sources: pd.DataFrame, radius_arcsec: float) -> pd.DataFrame:
    """Each truth row's nearest source of its (expid, sca) within the radius: ``job``, ``sid``,
    ``offset_arcsec`` (NaN columns when none)."""
    out = truth.copy()
    out["job"] = pd.array([pd.NA] * len(out), dtype="Int64")
    out["sid"] = pd.array([pd.NA] * len(out), dtype="Int64")
    out["offset_arcsec"] = np.nan
    by_epoch = sources.groupby(["expid", "sca"]).indices
    for key, idx in out.groupby(["expid", "sca"]).indices.items():
        if key not in by_epoch:
            continue
        s = sources.iloc[by_epoch[key]]
        t = out.iloc[idx]
        cosd = np.cos(np.radians(t["dec_c"].to_numpy()))[:, None]
        dx = ((s["ra"].to_numpy()[None, :] - t["ra_c"].to_numpy()[:, None] + 180.0) % 360.0 - 180.0) * cosd
        dy = s["dec"].to_numpy()[None, :] - t["dec_c"].to_numpy()[:, None]
        d = np.hypot(dx, dy) * 3600.0
        best = np.argmin(d, axis=1)
        dist = d[np.arange(len(t)), best]
        hit = dist <= radius_arcsec
        rows = out.index[idx]
        out.loc[rows[hit], "job"] = s["job"].to_numpy()[best[hit]]
        out.loc[rows[hit], "sid"] = s["sid"].to_numpy()[best[hit]]
        out.loc[rows[hit], "offset_arcsec"] = dist[hit]
    return out


def join(matched: pd.DataFrame, sources: pd.DataFrame, packets: pd.DataFrame) -> pd.DataFrame:
    """Left joins: a truth row's detection, then its packet; ``detected``, ``alerted``, ``sent``,
    ``flux_ratio``, ``flags``."""
    src = sources.rename(columns={"ra": "src_ra", "dec": "src_dec"})[
        ["job", "sid", "fluxfit", "flags", "in_field", "src_ra", "src_dec"]]
    pk = packets.rename(columns={"job": "packet_job", "source_job": "job"})[
        ["job", "sid", "packet_job", "object_job", "aid", "sent", "trigger_mjd"]]
    out = matched.merge(src, on=["job", "sid"], how="left").merge(pk, on=["job", "sid"], how="left")
    out["detected"] = out["job"].notna()
    out["in_field"] = out["in_field"].fillna(False).astype(bool) & out["detected"]
    out["alerted"] = out["packet_job"].notna()
    out["sent"] = out["sent"].fillna(False).astype(bool) & out["alerted"]
    out["flux_ratio"] = out["fluxfit"] / out["dnps"]
    return out


def bins(rows: pd.DataFrame, by: list[str] | None = None) -> pd.DataFrame:
    """Per magnitude bin (and ``by``): injected, detected, alerted, sent, the fractions, the median
    offset and flux ratio of the detections."""
    rows = rows.assign(mag_bin=np.floor(rows["mag_ab"] / BIN) * BIN)
    keys = (by or []) + ["mag_bin"]
    g = rows.groupby(keys)
    table = pd.DataFrame({
        "injected": g.size(), "detected": g["detected"].sum(), "alerted": g["alerted"].sum(),
        "sent": g["sent"].sum(), "in_field": g["in_field"].sum(),
        "median_offset_arcsec": g["offset_arcsec"].median(),
        "median_flux_ratio": g["flux_ratio"].median()}).reset_index()
    for c in ("detected", "alerted", "sent"):
        table[f"f_{c}"] = table[c] / table["injected"]
    table["f_alerted_in_field"] = g.apply(lambda t: t.loc[t["in_field"], "alerted"].mean(),
                                          include_groups=False).to_numpy()
    return table


def purity(packets: pd.DataFrame, injected_keys: set[tuple[int, int]], zp: dict) -> pd.DataFrame:
    """Each packet classed ``injected`` (its source matched a truth row), ``bright`` (the bright-footprint
    flag) or ``other``, with its magnitude."""
    p = packets.copy()
    key = list(zip(p["source_job"], p["sid"]))
    p["class"] = np.where([k in injected_keys for k in key], "injected",
                          np.where((p["flags"] & BRIGHT_FOOTPRINT) != 0, "bright", "other"))
    z = np.array([zp.get((e, s), np.nan) for e, s in zip(p["expid"], p["sca"])])
    with np.errstate(invalid="ignore", divide="ignore"):
        p["mag"] = z - 2.5 * np.log10(p["fluxfit"].where(p["fluxfit"] > 0))
    return p


def purity_bins(p: pd.DataFrame) -> pd.DataFrame:
    p = p.assign(mag_bin=np.floor(p["mag"] / BIN) * BIN)
    t = p.pivot_table(index="mag_bin", columns="class", values="sid", aggfunc="count", fill_value=0)
    for c in ("injected", "bright", "other"):
        if c not in t:
            t[c] = 0
    t = t[["injected", "bright", "other"]].reset_index()
    t["packets"] = t["injected"] + t["bright"] + t["other"]
    t["purity"] = t["injected"] / t["packets"]
    return t


def history(rows: pd.DataFrame, records: dict, read_sets: dict) -> pd.DataFrame:
    """One row per alerted injected source's later packet: ``expected``, ``present``, ``missing``,
    ``late`` earlier alerted epochs, and ``undetected`` earlier injected epochs.

    ``records`` maps ``(packet_job, sid)`` to the set of ``prvDiaSources`` source ids; ``read_sets``
    maps a packet job to the source-set jobs it recorded reading.
    """
    out = []
    for src_id, g in rows.groupby("src_id"):
        g = g.sort_values("mjd")
        alerted = g[g["alerted"]]
        for i, (_, r) in enumerate(alerted.iterrows()):
            if i == 0:
                continue
            earlier = alerted.iloc[:i]
            prv = records.get((int(r["packet_job"]), int(r["sid"])), set())
            read = read_sets.get(int(r["packet_job"]), set())
            present = sum(int(e["sid"]) in prv for _, e in earlier.iterrows())
            absent = [e for _, e in earlier.iterrows() if int(e["sid"]) not in prv]
            missing = sum(int(e["job"]) in read for e in absent)
            out.append({"src_id": src_id, "packet_job": int(r["packet_job"]), "sid": int(r["sid"]),
                        "expid": int(r["expid"]), "expected": len(earlier), "present": present,
                        "missing": missing, "late": len(absent) - missing,
                        "undetected": int((~g["detected"] & (g["mjd"] < r["mjd"])).sum()),
                        "objects": int(alerted.iloc[: i + 1]["aid"].nunique())})
    return pd.DataFrame(out, columns=["src_id", "packet_job", "sid", "expid", "expected", "present",
                                      "missing", "late", "undetected", "objects"])


def _records(conn_rows: list[dict]) -> dict:
    """``prvDiaSources`` ids per (packet job, sid), decoded from the packet files by their locators."""
    import fastavro

    from rapidpipe.foundation.prefix import get

    by_file = defaultdict(list)
    for r in conn_rows:
        by_file[r["filename"]].append(r)

    def decode(item):
        filename, rows = item
        data = get(filename).read_bytes()
        header_end = next(fastavro.block_reader(io.BytesIO(data))).offset
        got = {}
        for r in rows:
            chunk = data[:header_end] + data[r["byte_offset"]:r["byte_offset"] + r["byte_length"]]
            (rec,) = list(fastavro.reader(io.BytesIO(chunk)))
            got[(r["job"], r["sid"])] = {p["diaSourceId"] for p in rec["prvDiaSources"] or []}
        return got

    out = {}
    with ThreadPoolExecutor(8) as pool:
        for got in pool.map(decode, by_file.items()):
            out.update(got)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--run", type=int, required=True)
    p.add_argument("--truth", type=Path, nargs="*", help="truth Parquet files (default: the dataset's catalog/)")
    p.add_argument("--radius", type=float, default=0.3, help="match radius, arcsec")
    p.add_argument("--out", type=Path)
    p.add_argument("--no-history", action="store_true", help="skip decoding the packets")
    a = p.parse_args(argv)
    out_dir = a.out or Path(os.environ.get("TMPDIR", ".")) / f"completeness-{a.run}"
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()

    import psycopg2.extras

    from rapidpipe.db.connection import connect
    from rapidpipe.foundation.prefix import get
    from rapidpipe.products import storage

    def q(sql, params=()):
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()]
        conn.commit()
        return rows

    with connect(application_name="completeness") as conn:
        (run,) = q("SELECT dataset, settings FROM runs WHERE id = %s", (a.run,))
        intake = run["settings"]["settings"]["intake"] if "settings" in run["settings"] else run["settings"]["intake"]
        shift = tuple(float(v) for v in intake.get("crpix_shift", [0.0, 0.0]))
        if a.truth:
            files = [str(f) for f in a.truth]
        else:
            root = intake["source_root"].rstrip("/")
            catalog = root.rsplit("/", 1)[0] + "/catalog/"
            bucket, _, prefix = catalog.removeprefix("s3://").partition("/")
            client = storage.s3_client()
            files = [f"s3://{bucket}/{o['Key']}" for o in client.list_objects_v2(
                Bucket=bucket, Prefix=prefix).get("Contents", []) if o["Key"].endswith(".parquet")]
        truth = pd.concat([pd.read_parquet(get(f) if f.startswith("s3://") else f) for f in files],
                          ignore_index=True)
        sources = pd.DataFrame(q("SELECT s.job, s.sid, s.expid, s.sca, s.ra, s.dec, s.fluxfit, s.flags,"
                                 " s.field = d.field AS in_field FROM sources_official s"
                                 " JOIN diffimages d ON d.pid = s.pid WHERE s.run = %s", (a.run,)))
        packets = pd.DataFrame(q(
            "SELECT p.job, p.source_job, p.sid, p.object_job, p.aid, p.expid, p.sca, p.trigger_mjd,"
            " p.filename, p.byte_offset, p.byte_length, j.kafka_published_at IS NOT NULL AS sent"
            " FROM packets_official p JOIN jobs j ON j.id = p.job WHERE p.run = %s", (a.run,)))
        diffs = {(r["expid"], r["sca"]) for r in q(
            "SELECT expid, sca FROM diffimages_official WHERE run = %s", (a.run,))}
        metrics = pd.DataFrame(q(
            "SELECT expid, sca, metric, value FROM job_metrics WHERE run = %s AND stage = 'difference'"
            " AND state = 'published' AND metric IN ('n_candidates', 'n_sources_snr_gt20',"
            " 'n_sources_near_bright_px', 'n_sources_ring_snr_gt20', 'container_seconds')", (a.run,)))
        read_sets = defaultdict(set)
        if len(packets):
            for r in q("SELECT i.job, i.source_job FROM job_inputs i WHERE i.kind = 'input'"
                       " AND i.product = 'sources' AND i.job = ANY(%s)",
                       (sorted({int(j) for j in packets["job"]}),)):
                read_sets[r["job"]].add(r["source_job"])

    if not len(sources):
        sources = pd.DataFrame(columns=["job", "sid", "expid", "sca", "ra", "dec", "fluxfit", "flags", "in_field"])
    if not len(packets):
        packets = pd.DataFrame(columns=["job", "source_job", "sid", "object_job", "aid", "expid", "sca",
                                        "trigger_mjd", "filename", "byte_offset", "byte_length", "sent"])
    epochs_in_truth = set(zip(truth["expid"], truth["sca"]))
    processed = epochs_in_truth & diffs
    unprocessed = sorted(epochs_in_truth - diffs)
    truth = truth[[k in processed for k in zip(truth["expid"], truth["sca"])]].reset_index(drop=True)
    rows = join(match(corrected_positions(truth, shift), sources, a.radius), sources, packets)
    packets = packets.merge(sources[["job", "sid", "fluxfit", "flags"]].rename(columns={"job": "source_job"}),
                            on=["source_job", "sid"], how="left")
    zp = truth.groupby(["expid", "sca"])["zp"].median().to_dict()
    injected_keys = {(int(j), int(s)) for j, s in zip(rows.loc[rows["detected"], "job"],
                                                      rows.loc[rows["detected"], "sid"])}
    classed = purity(packets, injected_keys, zp)
    table = bins(rows)
    per_sca = bins(rows, ["sca"])
    pur = purity_bins(classed)
    ep = classed.groupby(["expid", "sca", "class"]).size().unstack(fill_value=0)
    for c in ("injected", "bright", "other"):
        if c not in ep:
            ep[c] = 0
    ep = ep[["injected", "bright", "other"]].reset_index()
    det = rows.groupby(["expid", "sca"]).agg(truth=("src_id", "size"), detected=("detected", "sum"),
                                             alerted=("alerted", "sum"), sent=("sent", "sum")).reset_index()
    ep = det.merge(ep, on=["expid", "sca"], how="outer")
    if len(metrics):
        ep = ep.merge(metrics.pivot_table(index=["expid", "sca"], columns="metric", values="value")
                      .reset_index(), on=["expid", "sca"], how="left")
    held_sets = sorted({int(j) for j, s in zip(packets["job"], packets["sent"]) if not s})
    hist = pd.DataFrame()
    if not a.no_history and rows["alerted"].any():
        want = packets[[(int(j), int(s)) in injected_keys for j, s in zip(packets["source_job"], packets["sid"])]]
        hist = history(rows, _records(want.to_dict("records")), read_sets)

    rows.drop(columns=["filename"], errors="ignore").to_parquet(out_dir / "truth.parquet")
    table.to_parquet(out_dir / "bins.parquet")
    per_sca.to_parquet(out_dir / "bins_sca.parquet")
    pur.to_parquet(out_dir / "purity.parquet")
    ep.to_parquet(out_dir / "epochs.parquet")
    hist.to_parquet(out_dir / "history.parquet")

    pd.set_option("display.width", 200)
    pd.set_option("display.max_rows", 500)
    print(f"run {a.run} ({run['dataset']}): truth {len(files)} files, {len(truth)} rows in {len(processed)}"
          f" published epochs; {len(unprocessed)} truth epochs without a difference image"
          f"{' ' + str(unprocessed[:5]) if unprocessed else ''}; crpix_shift {list(shift)};"
          f" radius {a.radius} arcsec")
    print(f"packet sets {packets['job'].nunique()}, {len(held_sets)} not sent (held or owed)"
          f"{' ' + str(held_sets[:10]) if held_sets else ''}")
    print("\ncompleteness per magnitude bin")
    print(table.to_string(index=False, float_format=lambda v: f"{v:.3f}"))
    tot = rows[["detected", "alerted", "sent", "in_field"]].sum()
    clean = rows["in_field"] & (rows["flags"] == 0)
    print(f"\nall: injected {len(rows)}, detected {tot['detected']}, alerted {tot['alerted']}, sent {tot['sent']};"
          f" detected in their image's own field {tot['in_field']}, of them unflagged {int(clean.sum())},"
          f" alerted {int(rows.loc[clean, 'alerted'].sum())};"
          f" offset median {rows['offset_arcsec'].median():.3f} arcsec,"
          f" flux ratio median {rows['flux_ratio'].median():.3f}")
    if per_sca["sca"].nunique() > 1:
        s = rows.groupby("sca").agg(injected=("src_id", "size"), detected=("detected", "mean"),
                                    alerted=("alerted", "mean"), sent=("sent", "mean"))
        print("\nper SCA (fractions over all magnitudes)")
        print(s.to_string(float_format=lambda v: f"{v:.3f}"))
    print("\npurity per packet magnitude bin (injected / bright-flagged / other)")
    print(pur.to_string(index=False, float_format=lambda v: f"{v:.3f}"))
    print("\nper epoch: false positives (bright + other) relative to this catalog")
    fp = ep["bright"].fillna(0) + ep["other"].fillna(0)
    print(f"  median {fp.median():.0f}, min {fp.min():.0f}, max {fp.max():.0f} over {len(ep)} epochs;"
          f" median bright {ep['bright'].median():.0f}, other {ep['other'].median():.0f}")
    if len(hist):
        h = hist[["expected", "present", "missing", "late", "undetected"]].sum()
        print(f"\nhistory over {hist['src_id'].nunique()} alerted injected sources, {len(hist)} later packets:"
              f" earlier alerted epochs expected {h['expected']}, present {h['present']}, missing (read but"
              f" absent) {h['missing']}, late (published after the packet's read) {h['late']};"
              f" earlier injected epochs never detected {h['undetected']};"
              f" sources split over more than one object {int((hist['objects'] > 1).sum())} packets")
    print(f"\nwritten to {out_dir} in {time.perf_counter() - t0:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
