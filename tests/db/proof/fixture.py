"""A synthetic world for the procedure proof (foundation plan v5, Part 3).

Not a test module and no science: one dataset, one survey, one field, two
epochs of one exposure each on two SCAs in one filter. Every product is
written the way a stage writes it, as Parquet plus a manifest under a
``file://`` prefix holding the job id and attempt, and loaded by
:func:`rapidpipe.foundation.publish`; every run and job is created through
:func:`create_run` and :func:`create_job` with the inputs, expected
bindings and exclusions Part 1 statement 4 names.

* :meth:`World.import_image` stands for the import stage: one difference
  image and its source set of :data:`NSOURCES` sources at fixed positions,
  one job per (epoch, SCA), so each source set is its own set.
* :meth:`World.associate` writes an object set: the predecessor's objects
  with this epoch's sources added to their sufficient sums, the excluded
  source sets' members taken out of them, and this epoch's memberships.
  A source matches the object whose id is its position hash
  (``radec_index``), which is exact here because positions repeat.
* :meth:`World.packets` writes a packet set for one image key: one index
  row and one forced row per source of the epoch's source set.

Expected bindings default to :data:`CURRENT`: the key's open binding at
launch, what the loop would choose.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from rapidpipe.foundation import change, create_job, create_run, publish
from rapidpipe.science.spatial import radec_index
from tests.db.foundation_helpers import write_attempt

SURVEY, DATASET, FIELD, FID = "S", "D", 1, 1
SCAS = (1, 2)
EPOCHS = (1, 2)
NSOURCES = 24
FIELD_KEY = {"dataset": DATASET, "survey": SURVEY, "field": FIELD}

#: ``expects`` default: the key's open binding at launch (or none).
CURRENT = object()


def image_key(epoch: int, sca: int) -> dict:
    """The scientific key of an image product, source set or packet set."""
    return {"dataset": DATASET, "expid": epoch, "sca": sca, "fid": FID}


def position(sca: int, i: int) -> tuple[float, float]:
    """Source ``i`` of SCA ``sca``: the same sky position in every epoch."""
    return 10.0 + 0.5 * (sca - 1) + 0.01 * i, -5.0 + 0.002 * i


def aid_of(ra: float, dec: float) -> int:
    return int(radec_index(ra, dec))


class World:
    """The synthetic world on one connection, with a root directory for attempt prefixes."""

    def __init__(self, conn, root: Path):
        self.conn = conn
        self.root = root
        self._pids = 0

    # -- bookkeeping -------------------------------------------------------

    def run(self, why: str, settings: dict | None = None, live: bool = False) -> int:
        run =create_run(self.conn, "proof", why, "synthetic",
                         settings or {"recipe": why}, SURVEY, DATASET)
        if live:
            change(self.conn, [{"open": {"survey": SURVEY, "dataset": DATASET, "run": run}}],
                   "proof", f"{why} goes live")
        return run

    def binding(self, product: str, key: dict) -> int | None:
        """The open binding on (product, key), or None."""
        return self._one("SELECT id FROM official WHERE closed_at IS NULL AND product = %s"
                         " AND key = %s::jsonb", (product, json.dumps(key)))

    def live(self) -> tuple:
        """The open live binding for the survey and dataset and its run: (binding, run)."""
        rows = self.rows("SELECT id, run FROM official WHERE closed_at IS NULL"
                         " AND run IS NOT NULL AND survey = %s AND dataset = %s", (SURVEY, DATASET))
        return rows[0] if rows else (None, None)

    def _one(self, query, params):
        rows = self.rows(query, params)
        return rows[0][0] if rows else None

    def rows(self, query, params=()) -> list[tuple]:
        """Run one read in its own short transaction."""
        with self.conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
        self.conn.commit()
        return rows

    def job(self, run: int, stage: str, scope: dict, outputs: dict[str, list[dict]],
            inputs=(), expects=(), excludes=(), attempt: int = 1,
            published: bool = True) -> int:
        """Create a job, write its attempt, and (unless told not to) publish it; return its id."""
        expects = [{**e, "binding": self.binding(e["product"], e["key"])}
                   if e["binding"] is CURRENT else e for e in expects]
        job = create_job(self.conn, run, stage, scope, inputs, expects, excludes)
        self.write(job, attempt, outputs)
        if published:
            publish(self.conn, job, attempt, self.prefix(job, attempt))
        return job

    def prefix(self, job: int, attempt: int) -> str:
        return (self.root / f"job-{job}" / f"attempt-{attempt}").as_uri()

    def write(self, job: int, attempt: int, outputs: dict[str, list[dict]]) -> str:
        """Write an attempt's declared outputs under its job-and-attempt prefix."""
        return write_attempt(self.root / f"job-{job}" / f"attempt-{attempt}", outputs)

    # -- the stages, standing in -------------------------------------------

    def import_image(self, run: int, epoch: int, sca: int, flux_scale: float = 1.0) -> int:
        """One difference image and its source set; the job is the source set."""
        key = image_key(epoch, sca)
        self._pids += 1
        pid = self._pids
        diffimage = {"pid": pid, **key, "field": FIELD, "jd": 2460000.5 + epoch,
                     "infobitssci": 0, "infobitsref": 0, "status": 1,
                     "filename": f"diff-{epoch}-{sca}.fits",
                     **{f"{c}{n}": v for n in range(5)
                        for c, v in (("ra", position(sca, 0)[0]), ("dec", position(sca, 0)[1]))}}
        sources = []
        for i in range(NSOURCES):
            ra, dec = position(sca, i)
            sources.append({
                "sid": i, **key, "pid": pid, "id": i, "field": FIELD, "hp6": 0, "hp9": 0,
                "mjdobs": 60000.0 + epoch, "ra": ra, "dec": dec,
                "xfit": float(i), "yfit": float(i), "xerr": 0.1, "yerr": 0.1,
                "fluxfit": flux_scale * (100.0 + i + 10.0 * epoch), "fluxerr": 1.0,
                "flags": 0, "qfit": 0.0, "cfit": 0.0, "npixfit": 9, "sharpness": 0.5,
                "roundness1": 0.0, "roundness2": 0.0, "peak": 10.0})
        expected = [{"product": product, "key": key, "binding": CURRENT}
                    for product in ("diffimages", "sources")]
        return self.job(run, "import", {"epoch": epoch, "sca": sca},
                        {"diffimages": [diffimage], "sources": sources}, expects=expected)

    def import_epochs(self, run: int) -> dict[tuple[int, int], int]:
        """Import every (epoch, SCA); return the source-set job per (epoch, SCA)."""
        return {(e, s): self.import_image(run, e, s) for e in EPOCHS for s in SCAS}

    def associate(self, run: int, epoch: int, source_sets: list[int], predecessor: int | None,
                  excludes: list[int] = (), expects=CURRENT, published: bool = True) -> int:
        """An object set: the predecessor's objects updated by ``source_sets``, minus ``excludes``."""
        objects = {}
        if predecessor is not None:
            for row in self.rows(
                    "SELECT aid, ra0, dec0, nsources, sumra, sumra2, sumdec, sumdec2, sumflux,"
                    " sumflux2 FROM objects WHERE job = %s", (predecessor,)):
                objects[row[0]] = dict(zip(_OBJECT_COLUMNS, row))
        for aid, ra, dec, flux in self._excluded_members(predecessor, excludes):
            _accumulate(objects[aid], ra, dec, flux, -1)
        memberships = []
        for source_job, sid, ra, dec, flux in self.rows(
                "SELECT job, sid, ra, dec, fluxfit FROM sources WHERE job = ANY(%s)"
                " AND flags = 0 ORDER BY job, sid", (list(source_sets),)):
            aid = aid_of(ra, dec)
            obj = objects.setdefault(aid, {"aid": aid, "ra0": ra, "dec0": dec, **_ZERO_SUMS})
            _accumulate(obj, ra, dec, flux, +1)
            memberships.append({"aid": aid, "source_job": source_job, "sid": sid})
        rows = [{**obj, "dataset": DATASET, "survey": SURVEY, "field": FIELD}
                for obj in objects.values()]
        inputs = [{"product": "sources", "key": self._key_of(s), "source_job": s}
                  for s in source_sets]
        if predecessor is not None:
            inputs.append({"product": "objects", "key": FIELD_KEY, "source_job": predecessor})
        return self.job(run, "associate", {"field": FIELD, "epoch": epoch},
                        {"objects": rows, "memberships": memberships}, inputs=inputs,
                        expects=[{"product": "objects", "key": FIELD_KEY, "binding": expects}],
                        excludes=[{"key": self._key_of(s), "source_job": s} for s in excludes],
                        published=published)

    def packets(self, run: int, epoch: int, sca: int, source_set: int, object_set: int,
                expects=CURRENT, attempt: int = 1, published: bool = True) -> int:
        """A packet set for one image key: the index and forced rows for its sources."""
        key = image_key(epoch, sca)
        index, forced = [], []
        for n, (sid, pid, ra, dec, mjd, flux) in enumerate(self.rows(
                "SELECT sid, pid, ra, dec, mjdobs, fluxfit FROM sources WHERE job = %s"
                " ORDER BY sid", (source_set,))):
            aid = aid_of(ra, dec)
            index.append({**key, "object_job": object_set, "aid": aid, "source_job": source_set,
                          "sid": sid, "trigger_mjd": mjd, "band": "F184",
                          "schema_version": "00.06", "filename": f"packets-{epoch}-{sca}.avro",
                          "byte_offset": 1000 * n, "byte_length": 1000})
            forced.append({"forced_id": _forced_id(pid, aid, ra, dec), "aid": aid, "rid": 0,
                           "pid": pid, "expid": epoch, "sca": sca, "fid": FID, "band": "F184",
                           "mjdobs": mjd, "ra": ra, "dec": dec, "x": 0.0, "y": 0.0,
                           "psf_flux": flux, "psf_fluxerr": 1.0, "science_flux": flux,
                           "science_fluxerr": 1.0, "flags": 0, "time_proc": 0.0})
        inputs = [{"product": "objects", "key": FIELD_KEY, "source_job": object_set},
                  {"product": "sources", "key": key, "source_job": source_set}]
        return self.job(run, "packet", {"epoch": epoch, "sca": sca},
                        {"packets": index, "forced": forced}, inputs=inputs,
                        expects=[{"product": "packets", "key": key, "binding": expects}],
                        attempt=attempt, published=published)

    def recipe(self, run: int, source_sets: dict[tuple[int, int], int], epochs=EPOCHS,
               predecessor: int | None = None) -> dict:
        """Associate and packets for ``epochs`` under ``run``, each epoch's set the next's predecessor.

        Returns ``{"objects": {epoch: job}, "packets": {(epoch, sca): job}}``.
        """
        made = {"objects": {}, "packets": {}}
        for epoch in epochs:
            sets = [source_sets[(epoch, s)] for s in SCAS]
            predecessor = self.associate(run, epoch, sets, predecessor)
            made["objects"][epoch] = predecessor
            for sca in SCAS:
                made["packets"][(epoch, sca)] = self.packets(
                    run, epoch, sca, source_sets[(epoch, sca)], predecessor)
        return made

    def cutover(self, to_run: int, made: dict) -> list[dict]:
        """The cutover group: every key ``made`` names rebound to its job, and ``to_run`` live."""
        group = [{"close": self.binding("objects", FIELD_KEY),
                  "open": {"product": "objects", "key": FIELD_KEY,
                           "job": made["objects"][max(made["objects"])]}}]
        for (epoch, sca), job in sorted(made["packets"].items()):
            key = image_key(epoch, sca)
            group.append({"close": self.binding("packets", key),
                          "open": {"product": "packets", "key": key, "job": job}})
        group.append({"close": self.live()[0], "open": {"survey": SURVEY, "dataset": DATASET,
                                               "run": to_run}})
        return group

    # -- internals ---------------------------------------------------------

    def _key_of(self, source_set: int) -> dict:
        (key,) = self.rows("SELECT DISTINCT jsonb_build_object('dataset', dataset, 'expid', expid,"
                           " 'sca', sca, 'fid', fid) FROM sources WHERE job = %s", (source_set,))[0]
        return key

    def _excluded_members(self, predecessor: int | None, excludes) -> list[tuple]:
        """The members of the predecessor's chain whose source set is excluded, with their values."""
        if predecessor is None or not excludes:
            return []
        return self.rows("""
            WITH RECURSIVE chain (job) AS (
                SELECT %s::bigint
                UNION ALL
                SELECT i.source_job FROM chain c
                JOIN job_inputs i ON i.job = c.job AND i.kind = 'input' AND i.product = 'objects'
            )
            SELECT m.aid, s.ra, s.dec, s.fluxfit
            FROM chain c
            JOIN memberships m ON m.job = c.job
            JOIN sources s ON s.job = m.source_job AND s.sid = m.sid
            WHERE m.source_job = ANY(%s)""", (predecessor, list(excludes)))


_OBJECT_COLUMNS = ("aid", "ra0", "dec0", "nsources", "sumra", "sumra2", "sumdec", "sumdec2",
                   "sumflux", "sumflux2")
_ZERO_SUMS = {"nsources": 0, "sumra": 0.0, "sumra2": 0.0, "sumdec": 0.0, "sumdec2": 0.0,
              "sumflux": 0.0, "sumflux2": 0.0}


def _accumulate(obj: dict, ra: float, dec: float, flux: float, sign: int) -> None:
    obj["nsources"] += sign
    for name, value in (("ra", ra), ("dec", dec), ("flux", flux)):
        obj[f"sum{name}"] += sign * value
        obj[f"sum{name}2"] += sign * value * value


def _forced_id(pid: int, aid: int, ra: float, dec: float) -> int:
    """A stable 63-bit id from image, object and position."""
    digest = hashlib.sha256(f"{pid}:{aid}:{ra:.9f}:{dec:.9f}".encode()).digest()
    return int.from_bytes(digest[:8], "big") >> 1
