# ported-from: alerts/produce.py, alerts/providers.py, alerts/forced_phot.py @ 883e3876
"""``packet``: one difference image's alert packets, schema 00.07, from named inputs.

Foundation plan v5, Part 2: a new port of `dev`'s ``alerts/`` package. It
runs as ``rapidpipe job local --stage packet`` under
:class:`rapidpipe.foundation.local.JobContext`: it reads what its job's
recorded inputs name in one short transaction, never the official views,
builds every packet in memory, writes the Avro container, the index and
the forced-photometry and Gaia rows and returns; ``job local`` publishes.

Scope: ``{"expid", "sca", "fid"}``, the packet set's key without the
dataset (the run's). Recorded inputs at launch, every one an ``input`` row:

- ``sources``: exactly one, the epoch's source set, whose key is the
  packet set's key. Its sources with no rejecting flag (`dev`'s
  ``ALERTABLE_FLAGS``, 0, once the informational bits of
  :data:`rapidpipe.products.diffimage.INFORMATIONAL_SOURCE_FLAGS` are
  cleared, so a source in a bright footprint is a trigger and its packet
  carries the bit in ``psfFitFlags``) that the difference job made a
  member of an object are the triggers; `dev`'s 2,000-per-chip cap is not
  ported (plan, Part 2). A trigger in several objects takes the lowest
  ``aid`` (`dev`'s rule).
- ``psfs``: exactly one, the science-image PSF (key ``kind`` ``science``).
- ``catalogs``: exactly one, the Gaia product, by its whole key (``dataset``,
  ``name``, ``version``; a key without ``dataset``, as runs before it named, reads too).
- ``refimages``: exactly one, the reference whose mosaic catalog
  (``refimcatalogs`` cattype 1) gives the star and galaxy matches.

Read at execution, as the object catalog stands (the plan's amendment of
2026-10-24: best information at the time), and recorded exactly in the
manifest's ``reads``, which the publisher writes to ``job_inputs``:

- the triggers' objects and their members: every membership of those
  objects whose source set is official (``memberships_official``), and the
  set's own; ``sources`` reads, one per source set read;
- each of those sources' exposure: the official ``exposures`` row of its
  (dataset, expid), the one intake wrote, giving ``exposureTime``, the
  Roman observation identifiers (``program``, ``plan``, ``pass``,
  ``segment``, ``observation``, ``visit``, ``exposure``,
  ``observation_id``) and, through ``surveys``, ``survey`` (null when the
  program has no row, as `dev`); ``exposures`` reads, one per exposure;
- the forced-photometry window: every difference image of the dataset
  whose difference is official, or the trigger's own, whose L2 ``mjdobs``
  is in ``[trigger - forced_window_days, trigger]`` and whose footprint
  holds one of the triggers' objects, whichever field the image is centred
  on (the field rule puts objects wherever a source falls, runbook part 12
  step 1), with its L2 row; ``diffimages`` and ``l2files`` reads;
- the official packet sets' ``forced`` rows measured on those images,
  reused, never re-measured; ``packets`` reads.

A late epoch's memberships therefore appear in packets issued after it
arrives, never in those already sent.

The job's ``expects`` row names the previous packet binding for its key,
or none.

Per trigger, the packet (`dev`'s ``assemble_alert_for_source``):

- ``prvDiaSources``: the object's other members read, with ``trigger - prv_window_days <= mjdobs < trigger``: the
  trigger bound the plan adds (`dev` has no upper bound).
- ``diaObject``: ``ra0, dec0`` and ``raSigma, decSigma`` from the sums of
  the members read (:mod:`rapidpipe.science.statistics.sums`);
  ``nDiaSources`` is the count as of the trigger, the members with
  ``mjdobs <= trigger``, the trigger counted. The schema's doc string is
  `dev`'s ("Total number"), kept verbatim with the 00.07 files.
- ``prvDiaForcedSources``: `dev`'s forced photometry
  (:mod:`rapidpipe.science.alerts.forced`) at the object's founding
  position on every window image whose footprint holds it, both the
  difference image (with its PSF and uncertainty) and the background-
  subtracted science image (with the science PSF; no uncertainty image is
  imported, so `dev`'s :func:`~rapidpipe.science.alerts.forced.error_array`
  fallback applies), merged one row per (object, image) as `dev`'s
  ``_run_forced`` merges them. An (object, image) pair whose
  ``forced_id`` an official packet set holds is copied from it. The difference
  image measured is the window's published one (SFFT since the
  differencer switch, with ``sfftdiffpsf.fits``, as `dev`'s ``diff_flavor =
  sfft``; ZOGY's ``diffpsf.fits`` for a window image published before it).
- ``gaiaMatches``: the Gaia product, propagated by proper motion from its
  ``ref_epoch`` to the trigger, within ``gaia_radius_arcsec``, at most
  ``gaia_max_matches``; ``refStarMatches``/``refGalaxyMatches`` from the
  reference mosaic catalog, likewise; ``ssMatches``, ``nedMatches`` and
  ``lvsMatches`` null (not run: deferred by the plan).
- ``reliability`` from ``sources.rb`` (the difference job's real/bogus
  score, SFFT only), and on a scored trigger ``reliabilityThreshold`` and
  ``reliabilityVersion`` from ``[packet] rb_threshold`` and ``rb_version``
  (:func:`reliability`); all three null on an unscored source.
- three cutouts, ``(2 stamp_half_width + 1)`` pixels square from the
  difference image, the background-subtracted science image and the
  resampled gain-matched reference beside it, off-chip pixels zero.

A difference image's neighbours (its PSF, uncertainty, science image and
template) are read under the difference image's own suffix
(:func:`neighbour`): ``.fits.gz`` from a difference job that gzips its
files, ``.fits`` from one before it (run 12's). A gzipped file is
decompressed once per job when it is first read (:func:`_plain`), to a
plain file beside its staged copy, or in the download cache for a
``file://`` product, which is never changed; forced photometry and the cutouts then
read the plain file, so the trigger's difference and science images, read
by both, and the science PSF, read on every window image, are not
decompressed again.

Outputs under the prefix: ``packets/<expid>-<sca>-<fid>.avro`` (one Avro
object container, deflate level 1, one record per block), declared tables
``packets`` (``packets.parquet``, the index: each record's block offset and
length from ``fastavro.block_reader``), ``forced`` (``forced.parquet``,
every row the packets carry, measured or reused, so the set is
self-contained) and ``external`` (``external.parquet``, the Gaia matches).
The manifest's ``metrics``: ``n_packets``, the set's packets, and
``n_packets_snr_gt20``, those whose source's fit flux over its error is
above 20, and ``n_packets_bright_footprint``, those whose source carries
the bright-footprint flag
(:data:`rapidpipe.products.diffimage.SOURCE_FLAG_BRIGHT_FOOTPRINT`).
"""

from __future__ import annotations

import dataclasses
import gzip
import logging
import math
import shutil
import time
from pathlib import Path

import fastavro
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from astropy.time import Time
from psycopg2.extras import RealDictCursor

from rapidpipe.foundation.local import JobContext
from rapidpipe.foundation.prefix import cache_dir, get, is_staged
from rapidpipe.products.diffimage import INFORMATIONAL_SOURCE_FLAGS, SOURCE_FLAG_BRIGHT_FOOTPRINT
from rapidpipe.science.alerts import forced as fp
from rapidpipe.science.alerts.assemble import (
    VERSION, AlertContainer, assemble_alert, load_schema, sync_marker_for)
from rapidpipe.science.alerts.crossmatch import (
    GAIA_COLUMNS, build_gaiacat, epoch_offset_years, load_refcat, match_gaiacat, match_refcat)
from rapidpipe.science.alerts.cutouts import CUTOUT_FILES, extract_stamp, load_fits_image
from rapidpipe.science.alerts.records import (
    ALERTABLE_FLAGS, Cutouts, ForcedPhot, ObjectRecord, Source)
from rapidpipe.science.difference.realbogus import version_of
from rapidpipe.science.statistics.sums import Sums

logger = logging.getLogger(__name__)

#: `refimcatalogs.cattype` of the reference's mosaic SExtractor catalog.
REFCAT_CATTYPE = 1

#: `dev`'s upper edge of the forced window, so the trigger epoch is in it.
TRIGGER_EPSILON_DAYS = 1e-6

#: Slack on the window's reach for images a little larger than the trigger's (distortion).
WINDOW_REACH_MARGIN = 1.05

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

#: A source row with its band, and from its exposure the exposure time, the Roman observation
#: identifiers and the program's survey title, as `dev`'s provider selects it (``SOURCE_SELECT``).
SOURCE_COLUMNS = (
    "s.job AS source_job, s.dataset, s.sid, s.expid, s.sca, s.mjdobs, s.ra, s.dec, s.xfit, s.yfit,"
    " s.xerr, s.yerr, s.fluxfit, s.fluxerr, s.flags, s.field, s.hp6, s.hp9, s.pid,"
    " s.isdiffpos, s.qfit, s.cfit, s.redchi, s.npixfit, s.sharpness, s.roundness1,"
    " s.roundness2, s.peak, s.rb, s.rblabel, f.filter AS band, e.job AS exposure_job, e.exptime,"
    " e.program_id, e.execution_plan, e.pass AS obs_pass, e.segment, e.observation, e.visit,"
    " e.exposure, e.observation_id, sv.program_title AS survey")
#: The exposure is the official ``exposures`` row of the source's (dataset, expid), the one intake
#: wrote; the survey is a LEFT JOIN, as `dev`'s, so a program not in ``surveys`` drops nothing.
SOURCE_JOINS = (
    " JOIN filters f ON f.fid = s.fid"
    " LEFT JOIN official oe ON oe.closed_at IS NULL AND oe.product = 'exposures'"
    " AND oe.key = jsonb_build_object('dataset', s.dataset, 'expid', s.expid)"
    " LEFT JOIN exposures e ON e.job = oe.job AND e.dataset = s.dataset AND e.expid = s.expid"
    " LEFT JOIN surveys sv ON sv.program_id = e.program_id")

#: The trigger set's memberships: its sources' objects.
TRIGGER_SQL = ("SELECT sid, object_job, aid FROM memberships WHERE job = %s"
               " ORDER BY sid, aid, object_job")

#: Every member of the triggers' objects whose source set is official, and the set's own.
MEMBERS_SQL = (
    f"SELECT DISTINCT ON (m.aid, s.job, s.sid) m.aid AS member_aid, {SOURCE_COLUMNS}"
    " FROM memberships m JOIN sources s ON s.job = m.source_job AND s.sid = m.sid"
    f"{SOURCE_JOINS}"
    " WHERE (m.object_job, m.aid) IN (SELECT * FROM unnest(%(jobs)s::bigint[], %(aids)s::bigint[]))"
    " AND (m.job = %(own)s OR EXISTS (SELECT 1 FROM official o WHERE o.closed_at IS NULL"
    " AND o.product = 'sources' AND o.job = m.job))"
    " ORDER BY m.aid, s.job, s.sid")

#: The window's candidate difference images: official, or the trigger's own, of the dataset, by L2
#: mjdobs, centred within ``reach`` degrees of the trigger image's centre (two of its half-diagonals,
#: so every image that can overlap it); :func:`_read` keeps those whose footprint holds an object.
WINDOW_SQL = (
    "SELECT d.job, d.pid, d.rid, d.dataset, d.expid, d.sca, d.fid, d.filename,"
    " d.ra1, d.dec1, d.ra2, d.dec2, d.ra3, d.dec3, d.ra4, d.dec4, f.filter,"
    " l.mjdobs, l.job AS l2_job"
    " FROM diffimages d JOIN filters f ON f.fid = d.fid JOIN l2files l ON l.rid = d.rid"
    " WHERE d.dataset = %(dataset)s"
    " AND 2 * asin(sqrt(power(sin(radians(d.dec0 - %(dec0)s) / 2), 2) + cos(radians(d.dec0))"
    " * cos(radians(%(dec0)s)) * power(sin(radians(d.ra0 - %(ra0)s) / 2), 2))) <= radians(%(reach)s)"
    " AND l.mjdobs BETWEEN %(lo)s AND %(hi)s"
    " AND (d.job = %(own)s OR EXISTS (SELECT 1 FROM official o WHERE o.closed_at IS NULL"
    " AND o.product = 'diffimages' AND o.job = d.job))"
    " ORDER BY l.mjdobs, d.expid, d.sca, d.fid")

#: Official packet sets' forced rows on the window's images.
REUSE_SQL = ("SELECT f.*, p.dataset AS p_dataset, p.expid AS p_expid, p.sca AS p_sca, p.fid AS p_fid"
             " FROM forced f JOIN official o ON o.closed_at IS NULL AND o.product = 'packets'"
             " AND o.job = f.job JOIN LATERAL (SELECT dataset, expid, sca, fid FROM packets"
             " WHERE job = f.job LIMIT 1) p ON true WHERE f.pid = ANY(%s)")


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
    object_job: dict[int, int]                   # aid -> the object's founding job
    flags_zero: int                              # the set's sources with no rejecting flag
    triggers: list[Source]                       # those that are members of an object
    aid_of: dict[int, int]                       # trigger sid -> aid
    objects: dict[int, tuple[float, float, Sums]]
    members: dict[int, list[tuple[int, Source]]]  # aid -> (source set, member)
    images: list[WindowImage]
    trigger_image: WindowImage
    reuse: dict[int, dict]                       # forced_id -> predecessor row
    gaia_job: int
    gaia_path: Path
    refcat_path: Path | None
    reads: list[dict] = dataclasses.field(default_factory=list)  # what was read at execution


def _one(inputs: list[dict], product: str, job: int) -> dict:
    rows = [i for i in inputs if i["product"] == product]
    if len(rows) != 1:
        raise ValueError(f"job {job}: {len(rows)} {product} inputs; exactly one")
    return rows[0]


def _key_where(key: dict, alias: str) -> tuple[str, list]:
    names = sorted(key)
    return " AND ".join(f"{alias}.{n} = %s" for n in names), [key[n] for n in names]


def _read(job: JobContext) -> Read:
    """Everything the job reads, its inputs and the catalog as it stands, in one short transaction."""
    inputs = [i for i in job.inputs if i["kind"] == "input"]
    sources_in = _one(inputs, "sources", job.job)
    key = dict(sources_in["key"])
    if {k: key.get(k) for k in ("expid", "sca", "fid")} != {
            k: job.scope[k] for k in ("expid", "sca", "fid")}:
        raise ValueError(f"job {job.job}: the sources input {key} is not the scope's key {job.scope}")
    source_job = sources_in["source_job"]
    psf_in = _one(inputs, "psfs", job.job)
    gaia_in = _one(inputs, "catalogs", job.job)
    ref_in = _one(inputs, "refimages", job.job)
    window_days = float(job.settings["packet"]["forced_window_days"])
    reads: list[dict] = []

    with job.connect() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(f"SELECT {SOURCE_COLUMNS} FROM sources s{SOURCE_JOINS}"
                    " WHERE s.job = %s AND s.flags & ~%s = %s ORDER BY s.sid",
                    (source_job, INFORMATIONAL_SOURCE_FLAGS, ALERTABLE_FLAGS))
        rows = cur.fetchall()
        alertable = [Source.from_row(r) for r in rows]
        exposures = {(r["dataset"], r["expid"]): r["exposure_job"] for r in rows}

        cur.execute(TRIGGER_SQL, (source_job,))
        aid_of, object_job = {}, {}
        for r in cur.fetchall():                 # ordered by aid: the lowest is first
            if r["sid"] not in aid_of:
                aid_of[r["sid"]] = r["aid"]
                object_job[r["aid"]] = r["object_job"]
        triggers = [s for s in alertable if s.sid in aid_of]
        aids = sorted({aid_of[s.sid] for s in triggers})
        cur.execute("SELECT o.aid, o.ra0, o.dec0 FROM objects o WHERE (o.job, o.aid) IN"
                    " (SELECT * FROM unnest(%s::bigint[], %s::bigint[]))",
                    ([object_job[a] for a in aids], aids))
        origin = {r["aid"]: (r["ra0"], r["dec0"]) for r in cur.fetchall()}

        cur.execute(MEMBERS_SQL, {"jobs": [object_job[a] for a in aids], "aids": aids,
                                  "own": source_job})
        members: dict[int, list[tuple[int, Source]]] = {}
        objects: dict[int, tuple[float, float, Sums]] = {
            aid: (*origin[aid], Sums(*origin[aid])) for aid in aids}
        member_sets = set()
        for row in cur.fetchall():
            aid = row.pop("member_aid")
            member = Source.from_row(row)
            member.aid = aid
            members.setdefault(aid, []).append((row["source_job"], member))
            objects[aid][2].add(row["ra"], row["dec"], row["fluxfit"])
            member_sets.add(row["source_job"])
            exposures[row["dataset"], row["expid"]] = row["exposure_job"]
        reads += [{"product": "exposures", "key": {"dataset": d, "expid": e}, "source_job": j}
                  for (d, e), j in sorted(exposures.items()) if j is not None]
        if member_sets:
            cur.execute("SELECT DISTINCT job, dataset, expid, sca, fid FROM diffimages"
                        " WHERE job = ANY(%s) ORDER BY job", (sorted(member_sets),))
            reads += [{"product": "sources", "source_job": r.pop("job"), "key": dict(r)}
                      for r in cur.fetchall()]

        cur.execute("SELECT filename FROM psfs WHERE job = %s AND "
                    + _key_where(psf_in["key"], "psfs")[0],
                    (psf_in["source_job"], *_key_where(psf_in["key"], "psfs")[1]))
        sci_psf = get(cur.fetchone()["filename"])

        cur.execute("SELECT d.ra0, d.dec0, d.ra1, d.dec1, d.ra2, d.dec2, d.ra3, d.dec3, d.ra4, d.dec4,"
                    " l.mjdobs FROM diffimages d JOIN l2files l ON l.rid = d.rid"
                    " WHERE d.job = %s", (source_job,))
        own = cur.fetchone()
        if own is None:
            raise ValueError(f"job {job.job}: source set {source_job} has no difference image row")
        cur.execute(WINDOW_SQL, {"dataset": key["dataset"], "ra0": own["ra0"], "dec0": own["dec0"],
                                 "reach": window_reach(own), "own": source_job,
                                 "lo": own["mjdobs"] - window_days,
                                 "hi": own["mjdobs"] + TRIGGER_EPSILON_DAYS})
        images = []
        for d in covering(cur.fetchall(), [objects[a][:2] for a in aids], source_job):
            image_key = {"dataset": d["dataset"], "expid": d["expid"], "sca": d["sca"], "fid": d["fid"]}
            reads += [{"product": "diffimages", "key": image_key, "source_job": d["job"]},
                      {"product": "l2files", "key": image_key, "source_job": d["l2_job"]}]
            diff = get(d["filename"])
            plain = diff.name.removesuffix(".gz")
            uncert = fp.DIFF_UNCERT_BASENAMES.get(plain)
            if (d["expid"], d["sca"], d["fid"]) == (key["expid"], key["sca"], key["fid"]):
                for name in CUTOUT_FILES.values():  # write_set finds them beside the trigger
                    _beside(d["filename"], name)
            images.append(WindowImage(
                pid=d["pid"], rid=d["rid"], expid=d["expid"], sca=d["sca"], fid=d["fid"],
                band=d["filter"], mjdobs=d["mjdobs"],
                corners=tuple(d[c] for c in ("ra1", "dec1", "ra2", "dec2",
                                             "ra3", "dec3", "ra4", "dec4")),
                diff=diff, diff_psf=_beside(d["filename"], fp.DIFF_PSF_BASENAMES[plain]),
                diff_uncert=_beside(d["filename"], uncert) if uncert else None,
                sci=_beside(d["filename"], fp.SCI_BASENAME), sci_psf=sci_psf))

        reuse: dict[int, dict] = {}
        reused_sets = {}
        cur.execute(REUSE_SQL, ([i.pid for i in images],))
        for row in cur.fetchall():
            reused_sets[row["job"]] = {"dataset": row.pop("p_dataset"), "expid": row.pop("p_expid"),
                                       "sca": row.pop("p_sca"), "fid": row.pop("p_fid")}
            row.pop("job")
            reuse[row["forced_id"]] = dict(row)
        reads += [{"product": "packets", "key": k, "source_job": j}
                  for j, k in sorted(reused_sets.items())]

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
    if ref is None:  # the reference stage always writes its catalog's row
        raise ValueError(f"job {job.job}: reference {ref_in['key']} of job {ref_in['source_job']}"
                         " has no refimcatalogs row")

    trigger_image = [i for i in images if (i.expid, i.sca, i.fid) == (key["expid"], key["sca"], key["fid"])]
    if len(trigger_image) != 1:
        raise ValueError(f"job {job.job}: the trigger's difference image is not in its window")
    return Read(key=key, source_job=source_job, object_job=object_job,
                flags_zero=len(alertable), triggers=triggers,
                aid_of=aid_of, objects=objects, members=members, images=images,
                trigger_image=trigger_image[0], reuse=reuse,
                gaia_job=gaia_in["source_job"], gaia_path=gaia_path,
                refcat_path=get(ref["filename"]), reads=reads)


def window_reach(image: dict) -> float:
    """Degrees from ``image``'s centre within which another image's centre may put it in the window.

    Two of its centre-to-corner separations, with :data:`WINDOW_REACH_MARGIN`:
    an image of the same detector overlapping it is centred within that.
    """
    return 2 * max(_separation(image["ra0"], image["dec0"], image[f"ra{n}"], image[f"dec{n}"])
                   for n in range(1, 5)) * WINDOW_REACH_MARGIN


def covering(candidates: list[dict], positions: list[tuple[float, float]], own: int) -> list[dict]:
    """The candidate window images whose footprint holds one of ``positions``, and the trigger's own (job ``own``)."""
    corners = np.array([[d[c] for c in ("ra1", "dec1", "ra2", "dec2", "ra3", "dec3", "ra4", "dec4")]
                        for d in candidates], dtype=float).reshape(-1, 8)
    holds = (fp.contains_positions(corners, [p[0] for p in positions], [p[1] for p in positions])
             if positions and len(candidates) else np.zeros((len(candidates), 0), dtype=bool))
    return [d for d, row in zip(candidates, holds) if d["job"] == own or row.any()]


def _separation(ra1: float, dec1: float, ra2: float, dec2: float) -> float:
    """Degrees between two positions, the haversine the window's query evaluates."""
    r1, d1, r2, d2 = map(math.radians, (ra1, dec1, ra2, dec2))
    h = math.sin((d2 - d1) / 2) ** 2 + math.cos(d1) * math.cos(d2) * math.sin((r2 - r1) / 2) ** 2
    return math.degrees(2 * math.asin(math.sqrt(min(1.0, h))))


def neighbour(name: str, diff: str) -> str:
    """The published name of the difference image ``diff``'s neighbour ``name``: gzipped beside a gzipped image."""
    return name + ".gz" if diff.endswith(".fits.gz") else name


def _beside(url: str, name: str) -> Path:
    """The neighbour ``name`` of the difference image ``url``, in its directory, fetched like it."""
    return get(url.rsplit("/", 1)[0] + "/" + neighbour(name, url))


def _plain(path: Path) -> Path:
    """``path``, or for a ``.gz`` its plain file, decompressed on the first read: beside a staged
    download, which is then removed, or for a ``file://`` URL's own file, a published product
    left as it is, in the download cache."""
    if path.suffix != ".gz":
        return path
    staged = is_staged(path)
    plain = (path if staged else cache_dir() / "plain" / path.relative_to(path.anchor)).with_suffix("")
    if not plain.exists():
        plain.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "rb") as src, plain.open("wb") as dst:
            shutil.copyfileobj(src, dst)
        if staged:
            path.unlink()
    return plain


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
            for m in fp.forced_photometry(str(_plain(path)), str(_plain(psf)), todo, rid=image.rid,
                                          product=product,
                                          uncert_path=str(_plain(uncert)) if uncert else None):
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
    read = _read(job)
    summary = write_set(read, job.settings["packet"], job.path, job.prefix, job.dataset,
                        sync_seed=f"{job.job}/{job.attempt}",
                        reliability=reliability(job.settings))
    # A source set with no triggers is a set of no packets, validly; one with triggers is not.
    job.write_manifest([("packets", "packets.parquet"), ("forced", "forced.parquet"),
                        ("external", "external.parquet")],
                       empty_ok=() if read.triggers else ("packets",), packet=summary, reads=read.reads,
                       metrics={"n_packets": summary["packets"],
                                "n_packets_snr_gt20": summary["packets_snr_gt20"],
                                "n_packets_bright_footprint": summary["packets_bright_footprint"]})


def reliability(settings: dict) -> tuple[float, str]:
    """A scored trigger's ``(reliabilityThreshold, reliabilityVersion)``, `dev`'s ``[ALERTS]`` rule.

    ``[packet] rb_threshold``, and ``rb_version`` or, when empty, the
    version of the ``[difference.rubrat]`` model (the last path component of
    its ``model_prefix``, else of its ``model_key``).
    """
    rubrat = settings["difference"]["rubrat"]
    version = (settings["packet"]["rb_version"]
               or version_of(rubrat["model_prefix"] or rubrat["model_key"]))
    return float(settings["packet"]["rb_threshold"]), version


def write_set(read: Read, settings: dict, path: Path, prefix: str, dataset: str,
              sync_seed: str, reliability: tuple[float | None, str | None] = (None, None)) -> dict:
    """Build the packet set from what was read; write its files under ``path``.

    ``prefix`` is ``path``'s URL, recorded in the index. ``reliability`` is
    stamped on every scored trigger (:func:`reliability`). Returns the
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
    stamps = {}
    for kind, stamp_path in (
            ("difference", read.trigger_image.diff),
            ("science", read.trigger_image.diff.with_name(
                neighbour(CUTOUT_FILES["sci"], read.trigger_image.diff.name))),
            ("template", read.trigger_image.diff.with_name(
                neighbour(CUTOUT_FILES["ref"], read.trigger_image.diff.name)))):
        stamps[kind] = load_fits_image(_plain(stamp_path))
        if stamps[kind][0] is None:  # the trigger's own images are required, not optional
            raise ValueError(f"the trigger's {kind} image {stamp_path} could not be read")
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
                                   cutouts=cutouts, forced=forced, time_proc=time_proc,
                                   reliability=reliability)
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
              "fid": read.key["fid"], "object_job": read.object_job[aid], "aid": aid,
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
            "packets_bright_footprint": sum(1 for source, _ in order
                                            if source.flags & SOURCE_FLAG_BRIGHT_FOOTPRINT),
            "window_images": len(window), "forced_rows": len(forced_rows),
            "forced_measured": measured, "forced_reused": len(forced_rows) - measured,
            "gaia_matches": len(external),
            "timing_s": {k: round(v, 3) for k, v in timing.items()}}
