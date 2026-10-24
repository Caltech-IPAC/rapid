"""A synthetic world for the procedure proof (foundation plan, Part 3, amended 2026-10-24).

Not a test module and no science: one dataset, one survey, one field, two
epochs of one exposure each on two SCAs in one filter. Every product is
written the way a stage writes it, as Parquet plus a manifest under a
``file://`` prefix holding the job id and attempt, and loaded by
:func:`rapidpipe.foundation.publish`; every run and job is created through
:func:`create_run` and :func:`create_job` with the inputs and expected
bindings Part 1 statement 4 names.

* :meth:`World.difference` stands for the difference stage: one
  difference image and its source set of :data:`NSOURCES` sources at fixed
  positions (offset by ``jitter`` arcseconds per epoch), one job per
  (epoch, SCA), associated as the stage does it: the run's objects of the
  field read as they stand (or as ``catalog`` says, for two jobs in flight
  at once), matched by :func:`rapidpipe.science.crossmatch.memory.associate_epoch`,
  the read and the catalog key in the manifest, so the publisher reconciles.
* :meth:`World.packets` writes a packet set for one image key: one index
  row and one forced row per source of the epoch's source set that is a
  member of an object.

Expected bindings default to :data:`CURRENT`: the key's open binding at
launch, what the loop would choose.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from rapidpipe.foundation import change, create_job, create_run, publish
from rapidpipe.science.crossmatch.memory import associate_epoch
from tests.db.foundation_helpers import write_attempt

SURVEY, DATASET, FIELD, FID = "S", "D", 1, 1
SCAS = (1, 2)
EPOCHS = (1, 2)
NSOURCES = 24
FIELD_KEY = {"dataset": DATASET, "survey": SURVEY, "field": FIELD}
#: The match radius, degrees: 0.36 arcseconds, far below the sources' 7-arcsecond spacing.
RADIUS = 1e-4

#: ``expects`` default: the key's open binding at launch (or none).
CURRENT = object()


def image_key(epoch: int, sca: int) -> dict:
    """The scientific key of an image product, source set or packet set."""
    return {"dataset": DATASET, "expid": epoch, "sca": sca, "fid": FID}


def position(sca: int, i: int, jitter: float = 0.0) -> tuple[float, float]:
    """Source ``i`` of SCA ``sca``: one sky position, moved ``jitter`` arcseconds in both axes."""
    return 10.0 + 0.5 * (sca - 1) + 0.01 * i + jitter / 3600, -5.0 + 0.002 * i + jitter / 3600


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
            inputs=(), expects=(), attempt: int = 1, published: bool = True) -> int:
        """Create a job, write its attempt, and (unless told not to) publish it; return its id."""
        expects = [{**e, "binding": self.binding(e["product"], e["key"])}
                   if e["binding"] is CURRENT else e for e in expects]
        job = create_job(self.conn, run, stage, scope, inputs, expects)
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

    def catalog(self, run: int) -> list[tuple]:
        """The field's objects of ``run`` as a difference job reads them: (job, aid, ra0, dec0)."""
        return self.rows("SELECT o.job, o.aid, o.ra0, o.dec0 FROM objects o JOIN jobs j ON j.id = o.job"
                         " WHERE j.run = %s AND o.field = %s ORDER BY o.job, o.aid", (run, FIELD))

    def difference(self, run: int, epoch: int, sca: int, flux_scale: float = 1.0,
                   jitter: float = 0.0, catalog: list[tuple] | None = None, expects=CURRENT,
                   published: bool = True) -> int:
        """One difference image, its source set and its association; the job is the source set.

        ``catalog`` is what the job read (:meth:`catalog`'s rows), the run's
        current objects when not given.
        """
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
            ra, dec = position(sca, i, jitter)
            sources.append({
                "sid": i, **key, "pid": pid, "id": i, "field": FIELD, "hp6": 0, "hp9": 0,
                "mjdobs": 60000.0 + epoch, "ra": ra, "dec": dec,
                "xfit": float(i), "yfit": float(i), "xerr": 0.1, "yerr": 0.1,
                "fluxfit": flux_scale * (100.0 + i + 10.0 * epoch), "fluxerr": 1.0,
                "flags": 0, "qfit": 0.0, "cfit": 0.0, "npixfit": 9, "sharpness": 0.5,
                "roundness1": 0.0, "roundness2": 0.0, "peak": 10.0})
        read = self.catalog(run) if catalog is None else catalog
        expects = [{"product": product, "key": key, "binding": expects}
                   for product in ("diffimages", "sources")]
        expects = [{**e, "binding": self.binding(e["product"], e["key"])}
                   if e["binding"] is CURRENT else e for e in expects]
        job = create_job(self.conn, run, "difference", {"epoch": epoch, "sca": sca}, (), expects)
        epoch_rows = associate_epoch({aid: (owner, ra0, dec0) for owner, aid, ra0, dec0 in read},
                                     sources, job=job, field=FIELD, cone=None, radius=RADIUS)
        outputs = {"diffimages": [diffimage], "sources": sources,
                   "objects": [{**o, "dataset": DATASET, "survey": SURVEY, "field": FIELD}
                               for o in epoch_rows.founded],
                   "memberships": epoch_rows.memberships}
        root = self.root / f"job-{job}" / "attempt-1"
        write_attempt(root, outputs)
        manifest = json.loads((root / "manifest.json").read_text())
        manifest["reads"] = [{"product": "objects", "key": FIELD_KEY, "source_job": owner}
                             for owner in sorted({r[0] for r in read})]
        manifest["catalog"] = {**FIELD_KEY, "radius": RADIUS}
        (root / "manifest.json").write_text(json.dumps(manifest))
        if published:
            publish(self.conn, job, 1, self.prefix(job, 1))
        return job

    def differences(self, run: int, epochs=EPOCHS) -> dict[tuple[int, int], int]:
        """Difference every (epoch, SCA) in turn; return the source-set job per (epoch, SCA)."""
        return {(e, s): self.difference(run, e, s) for e in epochs for s in SCAS}

    def packets(self, run: int, epoch: int, sca: int, source_set: int,
                expects=CURRENT, attempt: int = 1, published: bool = True) -> int:
        """A packet set for one image key: the index and forced rows for its member sources."""
        key = image_key(epoch, sca)
        index, forced = [], []
        for n, (sid, pid, ra, dec, mjd, flux, object_job, aid) in enumerate(self.rows(
                "SELECT DISTINCT ON (s.sid) s.sid, s.pid, s.ra, s.dec, s.mjdobs, s.fluxfit,"
                " m.object_job, m.aid FROM sources s JOIN memberships m"
                " ON m.source_job = s.job AND m.sid = s.sid WHERE s.job = %s"
                " ORDER BY s.sid, m.aid", (source_set,))):
            index.append({**key, "object_job": object_job, "aid": aid, "source_job": source_set,
                          "sid": sid, "trigger_mjd": mjd, "band": "F184",
                          "schema_version": "00.06", "filename": f"packets-{epoch}-{sca}.avro",
                          "byte_offset": 1000 * n, "byte_length": 1000})
            forced.append({"forced_id": _forced_id(pid, aid, ra, dec), "aid": aid, "rid": 0,
                           "pid": pid, "expid": epoch, "sca": sca, "fid": FID, "band": "F184",
                           "mjdobs": mjd, "ra": ra, "dec": dec, "x": 0.0, "y": 0.0,
                           "psf_flux": flux, "psf_fluxerr": 1.0, "science_flux": flux,
                           "science_fluxerr": 1.0, "flags": 0, "time_proc": 0.0})
        inputs = [{"product": "sources", "key": key, "source_job": source_set}]
        return self.job(run, "packet", {"epoch": epoch, "sca": sca},
                        {"packets": index, "forced": forced}, inputs=inputs,
                        expects=[{"product": "packets", "key": key, "binding": expects}],
                        attempt=attempt, published=published)

    def recipe(self, run: int, epochs=EPOCHS) -> dict:
        """Differences and packets for ``epochs`` under ``run``, in order.

        Returns ``{"differences": {(epoch, sca): job}, "packets": {(epoch, sca): job}}``.
        """
        made = {"differences": {}, "packets": {}}
        for epoch in epochs:
            for sca in SCAS:
                made["differences"][(epoch, sca)] = job = self.difference(run, epoch, sca)
                made["packets"][(epoch, sca)] = self.packets(run, epoch, sca, job)
        return made

    def cutover(self, to_run: int, made: dict) -> list[dict]:
        """The cutover group: every key ``made`` names rebound to its job, and ``to_run`` live."""
        group = []
        for (epoch, sca), job in sorted(made["differences"].items()):
            for product in ("diffimages", "sources"):
                key = image_key(epoch, sca)
                group.append({"close": self.binding(product, key),
                              "open": {"product": product, "key": key, "job": job}})
        for (epoch, sca), job in sorted(made["packets"].items()):
            key = image_key(epoch, sca)
            group.append({"close": self.binding("packets", key),
                          "open": {"product": "packets", "key": key, "job": job}})
        group.append({"close": self.live()[0], "open": {"survey": SURVEY, "dataset": DATASET,
                                               "run": to_run}})
        return group


def _forced_id(pid: int, aid: int, ra: float, dec: float) -> int:
    """A stable 63-bit id from image, object and position."""
    digest = hashlib.sha256(f"{pid}:{aid}:{ra:.9f}:{dec:.9f}".encode()).digest()
    return int.from_bytes(digest[:8], "big") >> 1
