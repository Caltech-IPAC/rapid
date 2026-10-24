# ported-from: alerts/produce.py, alerts/providers.py, alerts/forced_phot.py @ 54fd7ea8
"""``packet``: one difference image's alert packets, schema 00.06, from named inputs.

Foundation plan v5, Part 2: a new port of `dev`'s ``alerts/`` package. It
runs as ``rapidpipe job local --stage packet`` under
:class:`rapidpipe.foundation.local.JobContext`: it reads what its job's
recorded inputs name in one short transaction, never the official views,
builds every packet in memory, writes the Avro container, the index and
the forced-photometry and Gaia rows and returns; ``job local`` publishes.

Scope: ``{"expid", "sca", "fid"}``, the packet set's key without the
dataset (the run's). Recorded inputs, every one an ``input`` row:

- ``sources``: exactly one, the epoch's source set, whose key is the
  packet set's key. Its ``flags = 0`` sources (`dev`'s ``ALERTABLE_FLAGS``)
  that the object set holds are the triggers; `dev`'s 2,000-per-chip cap
  is not ported (plan, Part 2).
- ``objects``: exactly one, the object set; through it its predecessor
  chain and the chain's exclusions, as ``members_official`` follows them.
  A trigger in several objects takes the lowest ``aid`` (`dev`'s rule).
- ``diffimages`` and ``l2files``: the forced-photometry window, every
  difference image (and the L2 row it differenced, for ``mjdobs``) the
  launcher names; the trigger's own image is among them and is the
  cutouts' source. Images outside ``[trigger - forced_window_days,
  trigger]`` are not measured.
- ``psfs``: exactly one, the science-image PSF (key ``kind`` ``science``).
- ``packets``: the predecessor packet sets, none for a first epoch;
  their ``forced`` rows are reused, never re-measured.
- ``catalogs``: exactly one, the Gaia product, by its whole key (``dataset``,
  ``name``, ``version``; a key without ``dataset``, as runs before it named, reads too).
- ``refimages``: exactly one, the reference whose mosaic catalog
  (``refimcatalogs`` cattype 1) gives the star and galaxy matches.

The job's ``expects`` row names the previous packet binding for its key,
or none.

Per trigger, the packet (`dev`'s ``assemble_alert_for_source``):

- ``prvDiaSources``: the object's other members, through the chain minus
  exclusions, with ``trigger - prv_window_days <= mjdobs < trigger``: the
  trigger bound the plan adds (`dev` has no upper bound).
- ``diaObject``: ``ra0, dec0`` and ``raSigma, decSigma`` from the object
  set's sums (:mod:`rapidpipe.science.statistics.sums`), as of the set;
  ``nDiaSources`` is the count as of the trigger, the members with
  ``mjdobs <= trigger``, the trigger counted. The schema's doc string is
  `dev`'s ("Total number"), kept verbatim with the 00.06 files.
- ``prvDiaForcedSources``: `dev`'s forced photometry
  (:mod:`rapidpipe.science.alerts.forced`) at the object's founding
  position on every window image whose footprint holds it, both the
  difference image (with its PSF and uncertainty) and the background-
  subtracted science image (with the science PSF; no uncertainty image is
  imported, so `dev`'s :func:`~rapidpipe.science.alerts.forced.error_array`
  fallback applies), merged one row per (object, image) as `dev`'s
  ``_run_forced`` merges them. An (object, image) pair whose
  ``forced_id`` a predecessor set holds is copied from it. The difference
  image measured is the one the job names (ZOGY here, with
  ``diffpsf.fits``); `dev` fixes the SFFT image.
- ``gaiaMatches``: the Gaia product, propagated by proper motion from its
  ``ref_epoch`` to the trigger, within ``gaia_radius_arcsec``, at most
  ``gaia_max_matches``; ``refStarMatches``/``refGalaxyMatches`` from the
  reference mosaic catalog, likewise; ``ssMatches``, ``nedMatches`` and
  ``lvsMatches`` null (not run: deferred by the plan).
- three cutouts, ``(2 stamp_half_width + 1)`` pixels square from the
  difference image, the background-subtracted science image and the
  resampled gain-matched reference beside it, off-chip pixels zero.

A difference image's neighbours (its PSF, uncertainty, science image and
template) are read under the difference image's own suffix
(:func:`neighbour`): ``.fits.gz`` from a difference job that gzips its
files, ``.fits`` from one before it (run 12's).

Outputs under the prefix: ``packets/<expid>-<sca>-<fid>.avro`` (one Avro
object container, deflate level 1, one record per block), declared tables
``packets`` (``packets.parquet``, the index: each record's block offset and
length from ``fastavro.block_reader``), ``forced`` (``forced.parquet``,
every row the packets carry, measured or reused, so the set is
self-contained) and ``external`` (``external.parquet``, the Gaia matches).
The manifest's ``metrics``: ``n_packets``, the set's packets, and
``n_packets_snr_gt20``, those whose source's fit flux over its error is
above 20.
"""

from __future__ import annotations

import dataclasses
import logging
import math
import time
from pathlib import Path

import fastavro
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from astropy.time import Time
from psycopg2.extras import RealDictCursor

from rapidpipe.foundation.local import JobContext
from rapidpipe.foundation.prefix import get
from rapidpipe.science.alerts import forced as fp
from rapidpipe.science.alerts.assemble import (
    VERSION, AlertContainer, assemble_alert, load_schema, sync_marker_for)
from rapidpipe.science.alerts.crossmatch import (
    GAIA_COLUMNS, build_gaiacat, epoch_offset_years, load_refcat, match_gaiacat, match_refcat)
from rapidpipe.science.alerts.cutouts import CUTOUT_FILES, extract_stamp, load_fits_image
from rapidpipe.science.alerts.records import (
    ALERTABLE_FLAGS, Cutouts, ForcedPhot, ObjectRecord, Source)
from rapidpipe.science.statistics.sums import Sums

logger = logging.getLogger(__name__)

#: `refimcatalogs.cattype` of the reference's mosaic SExtractor catalog.
REFCAT_CATTYPE = 1

#: `dev`'s upper edge of the forced window, so the trigger epoch is in it.
TRIGGER_EPSILON_DAYS = 1e-6

INDEX_SCHEMA = pa.schema([
    ("dataset", pa.string()), ("expid", pa.int32()), ("sca", pa.int16()), ("fid", pa.int16()),
    ("object_job", pa.int64()), ("aid", pa.int64()), ("source_job", pa.int64()),
    ("sid", pa.int64()), ("trigger_mjd", pa.float64()), ("band", pa.string()),
    ("schema_version", pa.string()), ("filename", pa.string()),
    ("byte_offset", pa.int64()), ("byte_length", pa.int64()),
])
FORCED_SCHEMA = pa.schema([
    ("forced_id", pa.int64()), ("aid", pa.int64()), ("rid", pa.int32()), ("pid", pa.int32()),
    ("expid", pa.int32()), ("sca", pa.int16()), ("fid", pa.int16()), ("band", pa.string()),
    ("mjdobs", pa.float64()), ("ra", pa.float64()), ("dec", pa.float64()),
    ("x", pa.float32()), ("y", pa.float32()),
    ("psf_flux", pa.float64()), ("psf_fluxerr", pa.float64()),
    ("science_flux", pa.float64()), ("science_fluxerr", pa.float64()),
    ("flags", pa.int32()), ("time_proc", pa.float64()),
])
EXTERNAL_SCHEMA = pa.schema([
    ("catid", pa.string()), ("ra", pa.float64()), ("dec", pa.float64()),
    ("sep", pa.float64()), ("pa", pa.float64()), ("source_job", pa.int64()),
    ("sid", pa.int64()), ("catalog_job", pa.int64()),
])

#: A source row with its band and exposure time, as `dev`'s provider selects it.
SOURCE_COLUMNS = (
    "s.job AS source_job, s.sid, s.expid, s.sca, s.mjdobs, s.ra, s.dec, s.xfit, s.yfit,"
    " s.xerr, s.yerr, s.fluxfit, s.fluxerr, s.flags, s.field, s.hp6, s.hp9, s.pid,"
    " s.isdiffpos, s.qfit, s.cfit, s.redchi, s.npixfit, s.sharpness, s.roundness1,"
    " s.roundness2, s.peak, f.filter AS band, e.exptime")
SOURCE_JOINS = (
    " JOIN filters f ON f.fid = s.fid"
    " LEFT JOIN exposures e ON e.job = s.job AND e.dataset = s.dataset AND e.expid = s.expid")

#: The object set's chain (itself first), through each set's `objects` input.
CHAIN_SQL = (
    "WITH RECURSIVE chain (job) AS ("
    " SELECT %s::bigint"
    " UNION ALL SELECT i.source_job FROM chain c JOIN job_inputs i ON i.job = c.job"
    " AND i.kind = 'input' AND i.product = 'objects' WHERE i.source_job IS NOT NULL)"
    " SELECT job FROM chain")


@dataclasses.dataclass
class WindowImage:
    """One named difference image of the window, with the files measured on it."""

    pid: int
    rid: int
    expid: int
    sca: int
    fid: int
    band: str
    mjdobs: float
    corners: tuple[float, ...]
    diff: Path
    diff_psf: Path
    diff_uncert: Path | None
    sci: Path
    sci_psf: Path


@dataclasses.dataclass
class Read:
    """Everything the job reads, as plain values."""

    key: dict
    source_job: int
    object_job: int
    flags_zero: int                              # the set's flags = 0 sources
    triggers: list[Source]                       # those the object set holds
    aid_of: dict[int, int]                       # trigger sid -> aid
    objects: dict[int, tuple[float, float, Sums]]
    members: dict[int, list[tuple[int, Source]]]  # aid -> (source set, member)
    images: list[WindowImage]
    trigger_image: WindowImage
    reuse: dict[int, dict]                       # forced_id -> predecessor row
    gaia_job: int
    gaia_path: Path
    refcat_path: Path | None


def _one(inputs: list[dict], product: str, job: int) -> dict:
    rows = [i for i in inputs if i["product"] == product]
    if len(rows) != 1:
        raise ValueError(f"job {job}: {len(rows)} {product} inputs; exactly one")
    return rows[0]


def _key_where(key: dict, alias: str) -> tuple[str, list]:
    names = sorted(key)
    return " AND ".join(f"{alias}.{n} = %s" for n in names), [key[n] for n in names]


def _read(job: JobContext) -> Read:
    """Everything the job's inputs name, in one short transaction."""
    inputs = [i for i in job.inputs if i["kind"] == "input"]
    sources_in = _one(inputs, "sources", job.job)
    key = dict(sources_in["key"])
    if {k: key.get(k) for k in ("expid", "sca", "fid")} != {
            k: job.scope[k] for k in ("expid", "sca", "fid")}:
        raise ValueError(f"job {job.job}: the sources input {key} is not the scope's key {job.scope}")
    source_job = sources_in["source_job"]
    object_job = _one(inputs, "objects", job.job)["source_job"]
    psf_in = _one(inputs, "psfs", job.job)
    gaia_in = _one(inputs, "catalogs", job.job)
    ref_in = _one(inputs, "refimages", job.job)
    predecessors = [i["source_job"] for i in inputs if i["product"] == "packets"]

    with job.connect() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(f"SELECT {SOURCE_COLUMNS} FROM sources s{SOURCE_JOINS}"
                    " WHERE s.job = %s AND s.flags = %s ORDER BY s.sid",
                    (source_job, ALERTABLE_FLAGS))
        alertable = [Source.from_row(r) for r in cur.fetchall()]

        cur.execute(CHAIN_SQL, (object_job,))
        chain = [r["job"] for r in cur.fetchall()]
        cur.execute("SELECT DISTINCT source_job FROM job_inputs"
                    " WHERE job = ANY(%s) AND kind = 'excludes'", (chain,))
        excluded = [r["source_job"] for r in cur.fetchall()]
        cur.execute("SELECT sid, min(aid) AS aid FROM memberships"
                    " WHERE job = ANY(%s) AND source_job = %s AND NOT source_job = ANY(%s)"
                    " GROUP BY sid", (chain, source_job, excluded))
        aid_of = {r["sid"]: r["aid"] for r in cur.fetchall()}
        triggers = [s for s in alertable if s.sid in aid_of]
        aids = sorted({aid_of[s.sid] for s in triggers})

        cur.execute("SELECT aid, ra0, dec0, nsources, sumra, sumra2, sumdec, sumdec2,"
                    " sumflux, sumflux2 FROM objects WHERE job = %s AND aid = ANY(%s)",
                    (object_job, aids))
        objects = {r.pop("aid"): (r["ra0"], r["dec0"], Sums(**r)) for r in cur.fetchall()}

        cur.execute(f"SELECT DISTINCT ON (m.aid, s.job, s.sid) m.aid AS member_aid, {SOURCE_COLUMNS}"
                    " FROM memberships m JOIN sources s ON s.job = m.source_job AND s.sid = m.sid"
                    f"{SOURCE_JOINS}"
                    " WHERE m.job = ANY(%s) AND m.aid = ANY(%s) AND NOT m.source_job = ANY(%s)"
                    " ORDER BY m.aid, s.job, s.sid", (chain, aids, excluded))
        members: dict[int, list[tuple[int, Source]]] = {}
        for row in cur.fetchall():
            aid = row.pop("member_aid")
            member = Source.from_row(row)
            member.aid = aid
            members.setdefault(aid, []).append((row["source_job"], member))

        cur.execute("SELECT filename FROM psfs WHERE job = %s AND "
                    + _key_where(psf_in["key"], "psfs")[0],
                    (psf_in["source_job"], *_key_where(psf_in["key"], "psfs")[1]))
        sci_psf = get(cur.fetchone()["filename"])

        images = []
        for d_in in (i for i in inputs if i["product"] == "diffimages"):
            where, params = _key_where(d_in["key"], "d")
            cur.execute("SELECT d.pid, d.rid, d.expid, d.sca, d.fid, d.filename,"
                        " d.ra1, d.dec1, d.ra2, d.dec2, d.ra3, d.dec3, d.ra4, d.dec4, f.filter"
                        " FROM diffimages d JOIN filters f ON f.fid = d.fid"
                        f" WHERE d.job = %s AND {where}", (d_in["source_job"], *params))
            d = cur.fetchone()
            l2_jobs = [i["source_job"] for i in inputs
                       if i["product"] == "l2files" and i["key"] == d_in["key"]]
            cur.execute("SELECT mjdobs FROM l2files WHERE rid = %s AND job = ANY(%s)",
                        (d["rid"], l2_jobs))
            l2 = cur.fetchone()
            if l2 is None:
                raise ValueError(f"job {job.job}: difference image {d['pid']} has no named"
                                 f" l2files input for rid {d['rid']}")
            diff = get(d["filename"])
            plain = diff.name.removesuffix(".gz")
            uncert = fp.DIFF_UNCERT_BASENAMES.get(plain)
            if (d["expid"], d["sca"], d["fid"]) == (key["expid"], key["sca"], key["fid"]):
                for name in CUTOUT_FILES.values():  # write_set finds them beside the trigger
                    _beside(d["filename"], name)
            images.append(WindowImage(
                pid=d["pid"], rid=d["rid"], expid=d["expid"], sca=d["sca"], fid=d["fid"],
                band=d["filter"], mjdobs=l2["mjdobs"],
                corners=tuple(d[c] for c in ("ra1", "dec1", "ra2", "dec2",
                                             "ra3", "dec3", "ra4", "dec4")),
                diff=diff, diff_psf=_beside(d["filename"], fp.DIFF_PSF_BASENAMES[plain]),
                diff_uncert=_beside(d["filename"], uncert) if uncert else None,
                sci=_beside(d["filename"], fp.SCI_BASENAME), sci_psf=sci_psf))

        reuse: dict[int, dict] = {}
        if predecessors:
            cur.execute("SELECT * FROM forced WHERE job = ANY(%s)", (predecessors,))
            for row in cur.fetchall():
                row.pop("job")
                reuse[row["forced_id"]] = dict(row)

        where, values = _key_where(gaia_in["key"], "c")
        cur.execute("SELECT c.location FROM catalogs c WHERE c.job = %s AND " + where,
                    (gaia_in["source_job"], *values))
        gaia_path = get(cur.fetchone()["location"])
        cur.execute("SELECT c.filename FROM refimcatalogs c JOIN refimages r"
                    " ON r.job = c.job AND r.rfid = c.rfid"
                    " WHERE c.job = %s AND c.cattype = %s AND "
                    + _key_where(ref_in["key"], "r")[0] + " ORDER BY c.rfcatid DESC LIMIT 1",
                    (ref_in["source_job"], REFCAT_CATTYPE, *_key_where(ref_in["key"], "r")[1]))
        ref = cur.fetchone()
        conn.rollback()

    trigger_image = [i for i in images if (i.expid, i.sca, i.fid) == (key["expid"], key["sca"], key["fid"])]
    if len(trigger_image) != 1:
        raise ValueError(f"job {job.job}: the trigger's difference image is not among its inputs")
    return Read(key=key, source_job=source_job, object_job=object_job,
                flags_zero=len(alertable), triggers=triggers,
                aid_of=aid_of, objects=objects, members=members, images=images,
                trigger_image=trigger_image[0], reuse=reuse,
                gaia_job=gaia_in["source_job"], gaia_path=gaia_path,
                refcat_path=get(ref["filename"]) if ref else None)


def neighbour(name: str, diff: str) -> str:
    """The published name of the difference image ``diff``'s neighbour ``name``: gzipped beside a gzipped image."""
    return name + ".gz" if diff.endswith(".fits.gz") else name


def _beside(url: str, name: str) -> Path:
    """The neighbour ``name`` of the difference image ``url``, in its directory, fetched like it."""
    return get(url.rsplit("/", 1)[0] + "/" + neighbour(name, url))


def measure_forced(images: list[WindowImage], positions: list[fp.Position],
                   reuse: dict[int, dict], time_proc: float) -> tuple[list[dict], int]:
    """One forced row per (object, image) whose footprint holds the object.

    A pair whose ``forced_id`` is in ``reuse`` is that row, unchanged; the
    rest are measured on both images of the epoch and merged as `dev`'s
    ``_run_forced`` merges them (``x``, ``y`` from the difference image,
    ``flags`` the OR of both fits'). A position the footprint test admits
    but the pixel margin rejects gets no row, as in `dev`. Returns the rows
    and how many were measured.
    """
    if not positions or not images:
        return [], 0
    holds = fp.contains_positions(np.array([i.corners for i in images]),
                                  [p.ra for p in positions], [p.dec for p in positions])
    rows: list[dict] = []
    measured = 0
    for image, row_mask in zip(images, holds):
        todo = []
        for p in (positions[j] for j in np.flatnonzero(row_mask)):
            forced_id = fp.forced_measurement_id(image.rid, p.pos_id, p.ra, p.dec)
            if forced_id in reuse:
                rows.append(dict(reuse[forced_id]))
            else:
                todo.append(p)
        if not todo:
            continue
        merged: dict[int, dict] = {}
        for product, path, psf, uncert in (("diff", image.diff, image.diff_psf, image.diff_uncert),
                                           ("science", image.sci, image.sci_psf, None)):
            for m in fp.forced_photometry(str(path), str(psf), todo, rid=image.rid,
                                          product=product,
                                          uncert_path=str(uncert) if uncert else None):
                rec = merged.setdefault(m.pos_id, {
                    "aid": m.pos_id, "rid": image.rid, "pid": image.pid, "expid": image.expid,
                    "sca": image.sca, "fid": image.fid, "band": image.band,
                    "mjdobs": image.mjdobs, "x": m.x, "y": m.y,
                    "psf_flux": math.nan, "psf_fluxerr": math.nan,
                    "science_flux": math.nan, "science_fluxerr": math.nan, "flags": 0})
                if product == "diff":
                    rec["psf_flux"], rec["psf_fluxerr"], rec["x"], rec["y"] = m.flux, m.fluxerr, m.x, m.y
                else:
                    rec["science_flux"], rec["science_fluxerr"] = m.flux, m.fluxerr
                rec["flags"] |= int(m.flags)
        by_aid = {p.pos_id: p for p in todo}
        for rec in merged.values():
            p = by_aid[rec["aid"]]
            rec.update(ra=p.ra, dec=p.dec, time_proc=time_proc,
                       forced_id=fp.forced_measurement_id(image.rid, p.pos_id, p.ra, p.dec))
            for name in ("psf_flux", "psf_fluxerr", "science_flux", "science_fluxerr"):
                if not math.isfinite(rec[name]):
                    rec[name] = None
            rows.append(rec)
            measured += 1
    return rows, measured


def _forced_phot(row: dict) -> ForcedPhot:
    return ForcedPhot(forced_id=row["forced_id"], aid=row["aid"], expid=row["expid"],
                      sca=row["sca"], ra=row["ra"], dec=row["dec"], mjdobs=row["mjdobs"],
                      time_proc=row["time_proc"], band=row["band"] or None,
                      flux=row["psf_flux"], fluxerr=row["psf_fluxerr"],
                      science_flux=row["science_flux"],
                      science_fluxerr=row["science_fluxerr"], flags=row["flags"])


def history(trigger: tuple[int, Source], members: list[tuple[int, Source]],
            window_days: float) -> tuple[list[Source], int]:
    """``prvDiaSources`` and ``nDiaSources`` as of the trigger.

    ``trigger`` and ``members`` are ``(source set job, source)``. Previous
    detections are the members strictly before the trigger and within the
    window, oldest first; the count is every member with ``mjdobs <=
    trigger``, the trigger itself counted once.
    """
    me, trigger = (trigger[0], trigger[1].sid), trigger[1]
    others = [m for job, m in members if (job, m.sid) != me]
    prv = sorted((m for m in others
                  if trigger.mjdobs - window_days <= m.mjdobs < trigger.mjdobs),
                 key=lambda m: (m.mjdobs, m.sid))
    count = 1 + sum(1 for m in others if m.mjdobs <= trigger.mjdobs)
    return prv, count


def _gaia_catalog(path: Path, mjd: float):
    table = pq.read_table(path, columns=[*GAIA_COLUMNS, "ref_epoch"])
    epochs = set(table.column("ref_epoch").to_pylist())
    if len(epochs) > 1:
        raise ValueError(f"{path}: {len(epochs)} reference epochs; one expected")
    columns = {}
    for name in GAIA_COLUMNS:
        column = table.column(name)
        columns[name] = (np.array(column.to_pylist(), dtype=object) if name == "phot_variable_flag"
                         else column.to_numpy(zero_copy_only=False))
    dt = epoch_offset_years(mjd, float(epochs.pop())) if epochs else 0.0
    return build_gaiacat(columns, dt_years=dt)


def run(job: JobContext) -> None:
    summary = write_set(_read(job), job.settings["packet"], job.path, job.prefix, job.dataset,
                        sync_seed=f"{job.job}/{job.attempt}")
    job.write_manifest([("packets", "packets.parquet"), ("forced", "forced.parquet"),
                        ("external", "external.parquet")], packet=summary,
                       metrics={"n_packets": summary["packets"],
                                "n_packets_snr_gt20": summary["packets_snr_gt20"]})


def write_set(read: Read, settings: dict, path: Path, prefix: str, dataset: str,
              sync_seed: str) -> dict:
    """Build the packet set from what was read; write its files under ``path``.

    ``prefix`` is ``path``'s URL, recorded in the index. Returns the
    manifest's ``packet`` summary: counts, the Avro size and the timing.
    """
    trigger_mjd = read.trigger_image.mjdobs
    time_proc = float(Time.now().mjd)  # one timeProcessedMjd for the set
    timing = {}

    t0 = time.perf_counter()
    window = [i for i in read.images
              if trigger_mjd - float(settings["forced_window_days"]) <= i.mjdobs
              <= trigger_mjd + TRIGGER_EPSILON_DAYS]
    aids = sorted({read.aid_of[s.sid] for s in read.triggers})
    positions = [fp.Position(aid, *read.objects[aid][:2]) for aid in aids]
    forced_rows, measured = measure_forced(window, positions, read.reuse, time_proc)
    forced_rows.sort(key=lambda r: (r["aid"], r["mjdobs"], r["rid"]))
    timing["forced_s"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    ra = [s.ra for s in read.triggers]
    dec = [s.dec for s in read.triggers]
    gaia = match_gaiacat(ra, dec, _gaia_catalog(read.gaia_path, trigger_mjd),
                         radius_arcsec=float(settings["gaia_radius_arcsec"]),
                         n_max=int(settings["gaia_max_matches"])) if read.triggers else []
    refcat = load_refcat(str(read.refcat_path)) if read.refcat_path else None
    refs = (match_refcat(ra, dec, refcat, radius_arcsec=float(settings["refcat_radius_arcsec"]),
                         n_max=int(settings["refcat_max_matches"]))
            if refcat is not None and read.triggers else [None] * len(read.triggers))
    timing["matching_s"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    half = int(settings["stamp_half_width"])
    stamps = {name: load_fits_image(path) for name, path in (
        ("difference", read.trigger_image.diff),
        ("science", read.trigger_image.diff.with_name(
            neighbour(CUTOUT_FILES["sci"], read.trigger_image.diff.name))),
        ("template", read.trigger_image.diff.with_name(
            neighbour(CUTOUT_FILES["ref"], read.trigger_image.diff.name))))}
    timing["cutouts_s"] = time.perf_counter() - t0

    forced_by_aid: dict[int, list[dict]] = {}
    for row in forced_rows:
        forced_by_aid.setdefault(row["aid"], []).append(row)

    t0 = time.perf_counter()
    schema = load_schema()
    name = f"{read.key['expid']}-{read.key['sca']}-{read.key['fid']}.avro"
    (path / "packets").mkdir()
    avro = path / "packets" / name
    order, external = [], []
    cutout_s = 0.0
    with avro.open("wb") as stream:
        container = AlertContainer(stream, schema, sync_marker=sync_marker_for(sync_seed))
        for source, gaia_matches, ref_matches in zip(read.triggers, gaia, refs):
            aid = read.aid_of[source.sid]
            ra0, dec0, sums = read.objects[aid]
            prv, count = history((read.source_job, source), read.members.get(aid, []),
                                 float(settings["prv_window_days"]))
            stats = sums.statistics()
            obj = ObjectRecord(aid=aid, ra0=ra0, dec0=dec0, nsources=count,
                               stdevra=None if math.isnan(stats["stdevra"]) else stats["stdevra"],
                               stdevdec=None if math.isnan(stats["stdevdec"]) else stats["stdevdec"])
            c0 = time.perf_counter()
            cutouts = Cutouts(**{kind: extract_stamp(pixels, source.xfit + 1.0, source.yfit + 1.0,
                                                     header=header, half_width=half)
                                 for kind, (pixels, header) in stamps.items()})
            cutout_s += time.perf_counter() - c0
            lo = trigger_mjd - float(settings["forced_window_days"])
            forced = [_forced_phot(r) for r in forced_by_aid.get(aid, [])
                      if lo <= r["mjdobs"] <= source.mjdobs + TRIGGER_EPSILON_DAYS]
            alert = assemble_alert(source, obj, prv, ss_matches=None, ref_matches=ref_matches,
                                   ned_matches=None, lvs_matches=None, gaia_matches=gaia_matches,
                                   cutouts=cutouts, forced=forced, time_proc=time_proc)
            container.write(alert)
            container.flush()  # one record per block: the index's locator is exact
            order.append((source, aid))
            external.extend({"catid": str(m.source_id), "ra": m.ra, "dec": m.dec, "sep": m.sep,
                             "pa": m.pa, "source_job": read.source_job, "sid": source.sid,
                             "catalog_job": read.gaia_job} for m in gaia_matches)
    timing["cutouts_s"] += cutout_s
    timing["assembly_s"] = time.perf_counter() - t0 - cutout_s

    with avro.open("rb") as stream:
        blocks = [(b.offset, b.size, b.num_records) for b in fastavro.block_reader(stream)]
    if len(blocks) != len(order) or any(n != 1 for *_, n in blocks):
        raise RuntimeError(f"{avro}: {len(blocks)} blocks for {len(order)} records")
    url = prefix + f"packets/{name}"
    index = [{"dataset": dataset, "expid": read.key["expid"], "sca": read.key["sca"],
              "fid": read.key["fid"], "object_job": read.object_job, "aid": aid,
              "source_job": read.source_job, "sid": source.sid, "trigger_mjd": source.mjdobs,
              "band": source.band, "schema_version": VERSION, "filename": url,
              "byte_offset": offset, "byte_length": size}
             for (source, aid), (offset, size, _) in zip(order, blocks)]

    for rows, schema, filename in ((index, INDEX_SCHEMA, "packets.parquet"),
                                   (forced_rows, FORCED_SCHEMA, "forced.parquet"),
                                   (external, EXTERNAL_SCHEMA, "external.parquet")):
        pq.write_table(pa.Table.from_pylist(rows, schema=schema), path / filename)
    return {"avro": f"packets/{name}", "avro_bytes": avro.stat().st_size,
            "flags_zero": read.flags_zero, "packets": len(order),
            "packets_snr_gt20": sum(1 for source, _ in order
                                    if source.fluxerr and source.fluxfit / source.fluxerr > 20),
            "window_images": len(window), "forced_rows": len(forced_rows),
            "forced_measured": measured, "forced_reused": len(forced_rows) - measured,
            "gaia_matches": len(external),
            "timing_s": {k: round(v, 3) for k, v in timing.items()}}
