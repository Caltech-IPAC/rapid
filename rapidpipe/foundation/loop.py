"""The loop: ``rapidpipe loop``, one verb that carries a selection one stage forward per pass.

Foundation plan, Part 2: the live run admits what is new under its source
root, and each stage runs when its inputs are official; association is per
field. One pass, :func:`run_pass`, reads the source root's listing of the
selected filters' observations (:func:`list_stems`) and one snapshot of the
tables (:func:`snapshot`), decides every submission from them
(:func:`plan`, pure), submits each through the caller's ``submit``
without waiting, and ends by producing the run's newly official packet sets
to the Kafka topic. The verb repeats passes until one finds nothing new,
nothing pending and nothing to submit (drained).

The selection (filters, SCAs, pointings, passes) is the operator's instruction for
this invocation, recorded in every pass's summary; it is not part of the
run's recipe and widening it changes nothing in the run's settings body.
A pointing is ``VVV_EEEE``, a visit and exposure, the same in every pass
of a filter; a pass is the survey's (the stem's three digits after the
plan), so a sampled proof of a few epochs is ``--passes 1,2,3,4``.

"Pending" is a job of the run in state ``launched`` or ``running``. A job
of the run in state ``failed`` is reported and never resubmitted: its key
counts as taken until a person reruns it (``job run --job <id>``, or
``job submit --job <id>`` on Batch).

Each pass first reads Batch for the run's unpublished jobs that carry a
``batch_job_id`` (:func:`reconcile`, one ``describe-jobs`` call per hundred
ids), one lookup read two ways: a Batch job that has ended while its row is
still ``launched`` or ``running`` is a container that ended without ``job
run`` recording it, and the row is marked ``failed`` with Batch's status
and reason in ``refusal``; a row ``job run`` marked ``failed`` whose Batch
job is still queued or running is Batch's own retry (exit 75) in flight,
and the pass holds it pending. Once Batch has ended, the job's own record
is the authority.

One pass, in order:

- ``psf``: a job per (filter, SCA) of the new stems whose science
  ``psfs`` key has no official row, scoped to the first new stem of that
  SCA and expecting the key unbound, unless a ``psf`` job of this run
  already serves the pair (pending, failed or published). That SCA's stems
  wait for intake until the row is official. (The reference PSF is the
  difference job's own measurement, not a product.)
- ``intake``: a job per listed ``_cal.asdf`` stem the selection admits for
  which no intake job of the dataset is published and no intake job of this
  run exists, and whose filter and SCA have official PSFs, at most
  ``max_intake``; it names the science PSF of its filter and SCA, and
  expects the ``exposures`` key only when nothing holds or claims it (the
  18 SCAs of an exposure share it).
- ``reference``: a job per (field, filter, SCA) with at least
  ``[reference.selection] min_frames`` official L2 rows, no official
  reference and none pending or failed, while no intake of the run is
  pending (so a reference coadds every epoch a drain admits). A field two
  SCAs share gets one, from the first SCA in order with enough frames.
- With ``[reference.selection] before_mjd`` set, the reference's epochs are
  bounded: it names only the field's official L2 rows with ``mjdobs <
  before_mjd``, and waits until the selection's intake has drained (no
  listed stem of the selection unpublished and no intake of the run
  pending: a pass admits at most ``max_intake`` stems, so a pass with no
  intake pending alone does not mean every pre-bound epoch is in). A field
  with fewer than ``min_frames`` such rows once the intake has drained gets
  no reference and is reported under ``held`` with the reason, every pass;
  it does not stop the drain. Rows before the bound are the reference's:
  they get no difference, associate or packet.
- ``difference``: a job per official L2 row whose field has an official
  reference and whose key has no official difference and no difference job
  of this run pending or failed.
- ``associate``: per field, one job for the earliest-``mjdobs`` official
  source set no associate job of this run names, predecessor the field's
  open object set, and only when no associate of the field is pending, no
  earlier selected L2 row of the field lacks its official source set and no
  intake of the run is pending (an intake's field is known only once it
  publishes), so each field's chain stays chronological. Fields advance
  independently.
- ``packet``: per field, one job for the earliest published associate of
  the run whose epoch has no packet set, once every earlier epoch's packet
  set is official, naming its inputs as the 2026-10-21 night did: the
  reference, the run's Gaia catalog, the science PSF, the object set, the
  field's difference images and L2 rows in the forced window, the epoch's
  source set and the field's earlier packet sets.
- ``kafka``: the caller's ``publish()`` over the run's unstamped official
  packet sets.
"""

from __future__ import annotations

import dataclasses
import re
import time
from collections import defaultdict
from typing import Any, Callable, Iterable

import psycopg2.extras

#: A SOC L2 stem, the fields the selection reads (the intake stage parses all of them).
STEM = re.compile(r"r\d{5}\d{2}(?P<pass>\d{3})\d{3}(?P<observation>\d{3})(?P<visit>\d{3})"
                  r"_(?P<exposure>\d{4})_wfi(?P<sca>\d{2})_(?P<filter>f\d{3})$")
#: Any key under the source root that begins with a stem.
KEY = re.compile(STEM.pattern.removesuffix("$"))
SUFFIX = "_cal.asdf"
PENDING = ("launched", "running")
#: Batch's job states before it ends one (``SUCCEEDED`` and ``FAILED`` end it).
BATCH_ACTIVE = ("SUBMITTED", "PENDING", "RUNNABLE", "STARTING", "RUNNING")
STAGES = ("psf", "intake", "reference", "difference", "associate", "packet")


@dataclasses.dataclass(frozen=True)
class Selection:
    """Filters (``F146``), SCAs, pointings (``(visit, exposure)``) and the survey's passes, ``None`` for
    all pointings or all passes."""

    filters: frozenset[str]
    scas: frozenset[int]
    pointings: frozenset[tuple[int, int]] | None
    passes: frozenset[int] | None = None

    @classmethod
    def parse(cls, filters: str, scas: str, pointings: str, passes: str = "all") -> "Selection":
        points = None if pointings == "all" else frozenset(
            (int(v), int(e)) for v, e in (p.split("_") for p in pointings.split(",")))
        return cls(frozenset(f.strip().upper() for f in filters.split(",")),
                   frozenset(int(s) for s in scas.split(",")), points,
                   None if passes == "all" else frozenset(int(p) for p in passes.split(",")))

    def admits_stem(self, stem: str) -> dict | None:
        """The stem's SCA, filter and pointing when the selection admits it, else ``None``."""
        m = STEM.match(stem)
        if m is None:
            return None
        sca, band = int(m["sca"]), m["filter"].upper()
        point = (int(m["visit"]), int(m["exposure"]))
        if sca not in self.scas or band not in self.filters:
            return None
        if self.pointings is not None and point not in self.pointings:
            return None
        if self.passes is not None and int(m["pass"]) not in self.passes:
            return None
        return {"sca": sca, "filter": band, "expid": stem_expid(stem)}

    def admits_row(self, expid: int, sca: int, fid: int, fids: set[int]) -> bool:
        """Whether an L2 key is selected, ``fids`` the selected filters' fids; ``expid``
        packs ``pass * 10**6`` and ``visit * 100 + exposure`` in its last digits."""
        if sca not in self.scas or fid not in fids:
            return False
        if self.passes is not None and expid // 10**6 not in self.passes:
            return False
        return self.pointings is None or ((expid // 100) % 100, expid % 100) in self.pointings

    def summary(self) -> dict:
        return {"filters": sorted(self.filters), "scas": sorted(self.scas),
                "pointings": "all" if self.pointings is None
                else [f"{v:03d}_{e:04d}" for v, e in sorted(self.pointings)],
                "passes": "all" if self.passes is None else sorted(self.passes)}


def stem_expid(stem: str) -> int:
    """The intake stage's expid packing, ``pass * 10**6 + observation * 10**4 + visit * 10**2 + exposure``."""
    m = STEM.match(stem)
    return (int(m["pass"]) * 10**6 + int(m["observation"]) * 10**4 + int(m["visit"]) * 10**2
            + int(m["exposure"]))


def list_stems(client: Any, source_root: str, selection: Selection) -> list[str]:
    """Every ``_cal.asdf`` stem under ``source_root`` (``s3://bucket/prefix/``) in an observation of a
    selected filter.

    An observation (``r<program><plan><pass><segment><observation>``, the
    stem's first 17 characters) is one filter, and its number differs by
    pass, so the observations are found from the listing itself: one
    one-key request per observation, each starting after the last one's
    prefix, reads its first key's filter. Each selected observation's prefix
    is then listed through the paginator; nothing outside them is listed.
    """
    bucket, _, root = source_root.removeprefix("s3://").partition("/")
    observations, after = [], root
    while first := client.list_objects_v2(Bucket=bucket, Prefix=root, StartAfter=after,
                                          MaxKeys=1).get("Contents"):
        name = first[0]["Key"][len(root):]
        m = KEY.match(name)
        if m is None:
            after = first[0]["Key"]
            continue
        observation = name[: m.end("observation")]
        after = root + observation + "~"          # past every key of the observation
        if m["filter"].upper() in selection.filters:
            observations.append(observation)
    stems = []
    for observation in observations:
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket,
                                                                     Prefix=root + observation):
            for item in page.get("Contents", []):
                name = item["Key"].rsplit("/", 1)[-1]
                if name.endswith(SUFFIX):
                    stems.append(name[: -len(SUFFIX)])
    return stems


@dataclasses.dataclass
class Snapshot:
    """What one pass reads from the tables, every row of the run's dataset.

    Keys are tuples: an L2 key is ``(expid, sca, fid)``, a reference key
    ``(field, fid)``. ``jobs`` are this run's jobs of the loop's stages:
    dicts with ``id``, ``stage``, ``state``, ``scope`` and, for an
    associate, ``sources`` (the source set job it names).
    """

    dataset: str
    survey: str
    settings: dict
    fids: dict[str, int]
    published_stems: set[str]
    jobs: list[dict]
    psfs: dict[tuple[int, int, str], tuple[int, int]]          # (fid, sca, kind) -> (job, binding)
    exposures: set[int]                                          # expids with an open binding
    l2: dict[tuple[int, int, int], dict]                         # key -> {field, mjdobs, job}
    refs: dict[tuple[int, int], int]                             # (field, fid) -> job
    diffs: dict[tuple[int, int, int], int]                       # key -> difference job
    sources: dict[tuple[int, int, int], int]                     # key -> source set job
    objects: dict[int, tuple[int, int]]                          # field -> (job, binding)
    chain: dict[int, int]                                        # source set job -> its associate in the
                                                                 # chain of its field's official object set
    packets: dict[tuple[int, int, int], int]                     # key -> packet set job
    gaia: dict | None                                            # the dataset's {"key", "job"}


def _rows(cur, sql: str, params=()) -> list[dict]:
    cur.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def _l2key(key: dict) -> tuple[int, int, int]:
    return int(key["expid"]), int(key["sca"]), int(key["fid"])


def snapshot(conn, run: int) -> Snapshot:
    """One read of the tables for one pass, in one transaction."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        meta = _rows(cur, "SELECT dataset, survey, settings FROM runs WHERE id = %s", (run,))
        if not meta:
            raise LookupError(f"run {run} does not exist")
        d = meta[0]["dataset"]
        fids = {r["filter"]: r["fid"] for r in _rows(cur, "SELECT fid, filter FROM filters")}
        stems = {r["file"] for r in _rows(
            cur, "SELECT scope->>'file' AS file FROM jobs WHERE stage = 'intake'"
                 " AND state = 'published' AND scope->>'dataset' = %s", (d,))}
        jobs = _rows(cur, "SELECT id, stage, state, scope FROM jobs WHERE run = %s AND stage = ANY(%s)"
                          " ORDER BY id", (run, list(STAGES)))
        named = {r["job"]: r["source_job"] for r in _rows(
            cur, "SELECT i.job, i.source_job FROM job_inputs i JOIN jobs j ON j.id = i.job"
                 " WHERE j.run = %s AND j.stage = 'associate' AND i.kind = 'input'"
                 " AND i.product = 'sources'", (run,))}
        for job in jobs:
            if job["stage"] == "associate":
                job["sources"] = named.get(job["id"])
        bound = _rows(cur, "SELECT id, product, key, job FROM official WHERE closed_at IS NULL"
                           " AND job IS NOT NULL AND key->>'dataset' = %s", (d,))
        links = _rows(cur, "SELECT i.job, i.product, i.source_job FROM job_inputs i"
                           " JOIN jobs j ON j.id = i.job JOIN runs r ON r.id = j.run"
                           " WHERE r.dataset = %s AND j.stage = 'associate' AND i.kind = 'input'"
                           " AND i.product IN ('sources', 'objects')", (d,))
        l2 = {_l2key(r): {"field": r["field"], "mjdobs": r["mjdobs"], "job": r["job"]}
              for r in _rows(cur, "SELECT expid, sca, fid, field, mjdobs, job FROM l2files_official"
                                  " WHERE dataset = %s", (d,))}
    conn.commit()
    by = defaultdict(list)
    for b in bound:
        by[b["product"]].append(b)
    gaia = [{"key": b["key"], "job": b["job"]} for b in by["catalogs"]]
    if len(gaia) > 1:
        raise LookupError(f"dataset {d} has {len(gaia)} official catalogs; the loop names one Gaia catalog")
    objects = {int(b["key"]["field"]): (b["job"], b["id"]) for b in by["objects"]
               if b["key"].get("survey") == meta[0]["survey"]}
    return Snapshot(
        dataset=d, survey=meta[0]["survey"], settings=meta[0]["settings"], fids=fids,
        published_stems=stems, jobs=jobs,
        psfs={(int(b["key"]["fid"]), int(b["key"]["sca"]), b["key"]["kind"]): (b["job"], b["id"])
              for b in by["psfs"]},
        exposures={int(b["key"]["expid"]) for b in by["exposures"]},
        l2=l2,
        refs={(int(b["key"]["field"]), int(b["key"]["fid"])): b["job"] for b in by["refimages"]},
        diffs={_l2key(b["key"]): b["job"] for b in by["diffimages"]},
        sources={_l2key(b["key"]): b["job"] for b in by["sources"]},
        objects=objects, chain=_chain(objects, links),
        packets={_l2key(b["key"]): b["job"] for b in by["packets"]},
        gaia=gaia[0] if gaia else None)


def reconcile(conn, run: int, client: Any) -> dict:
    """Read Batch for the run's unpublished jobs with a ``batch_job_id``; mark the dead ``failed``.

    Returns ``{"described", "ended", "retrying", "seconds"}``: ``ended`` the
    rows this call marked ``failed`` (``job``, ``stage``, ``reason``),
    ``retrying`` the ids of ``failed`` rows whose Batch job has not ended. A
    job without a Batch id (a local one), or one Batch no longer describes,
    is left alone.
    """
    started = time.monotonic()
    with conn.cursor() as cur:
        cur.execute("SELECT id, stage, state, batch_job_id FROM jobs WHERE run = %s"
                    " AND state <> 'published' ORDER BY id", (run,))
        rows = [r for r in cur.fetchall() if r[3]]
    conn.commit()
    ids = [r[3] for r in rows]
    batch = {}
    for i in range(0, len(ids), 100):
        batch.update((b["jobId"], b) for b in client.describe_jobs(jobs=ids[i:i + 100])["jobs"])
    ended, retrying = [], []
    for job, stage, state, batch_id in rows:
        described = batch.get(batch_id)
        if described is None:
            continue
        status = described["status"]
        if status in BATCH_ACTIVE:
            if state == "failed":
                retrying.append(job)
        elif state in PENDING:
            reason = f"Batch {status}: {described.get('statusReason', '')}".strip()
            with conn.cursor() as cur:
                cur.execute("UPDATE jobs SET state = 'failed', ended = now(), exit_code = %s,"
                            " refusal = %s WHERE id = %s AND state IN ('launched', 'running')",
                            (described.get("container", {}).get("exitCode"), reason, job))
                if cur.rowcount:
                    ended.append({"job": job, "stage": stage, "reason": reason})
            conn.commit()
    return {"described": len(batch), "ended": ended, "retrying": retrying,
            "seconds": round(time.monotonic() - started, 2)}


def _chain(objects: dict[int, tuple[int, int]], links: list[dict]) -> dict[int, int]:
    """Source set job -> associate job, for every associate in the chain of a field's official object
    set: from the bound set back through each associate's recorded ``objects`` input, whatever run
    made it. An associate outside every chain (a binding refused or undone) associated nothing."""
    sources, previous = {}, {}
    for link in links:
        (sources if link["product"] == "sources" else previous)[link["job"]] = link["source_job"]
    chain = {}
    for job, _ in objects.values():
        while job is not None and job in sources:
            chain[sources[job]] = job
            job = previous.get(job)
    return chain


def _fid(band: str, fids: dict[str, int]) -> int:
    """The filters table's fid for a Roman filter name: ``F146`` is the table's ``W146``."""
    for name, fid in fids.items():
        if name[1:] == band[1:]:
            return fid
    raise LookupError(f"no filters row for {band}")


def plan(snap: Snapshot, stems: Iterable[str], selection: Selection,
         max_intake: int = 100) -> tuple[list[dict], dict]:
    """Every submission one pass makes, and the pass's counts; reads nothing but its arguments.

    A submission is ``{"stage", "scope", "inputs", "expects"}``, ``job submit``'s arguments.
    """
    d = snap.dataset
    fids = {name: _fid(name, snap.fids) for name in selection.filters}
    run_jobs = defaultdict(list)
    for job in snap.jobs:
        run_jobs[job["stage"]].append(job)
    pending = {s: [j for j in run_jobs[s] if j["state"] in PENDING] for s in STAGES}
    failed = [j for j in snap.jobs if j["state"] == "failed"]
    taken = {s: [j for j in run_jobs[s] if j["state"] != "published"] for s in STAGES}

    def key(expid, sca, fid):
        return {"dataset": d, "expid": expid, "sca": sca, "fid": fid}

    def scope_l2(job):
        return int(job["scope"]["expid"]), int(job["scope"]["sca"]), int(job["scope"]["fid"])

    def psf(fid, sca, kind):
        if (fid, sca, kind) not in snap.psfs:
            raise LookupError(f"no official {kind} psfs row for fid {fid} sca {sca} in {d}")
        job, binding = snap.psfs[fid, sca, kind]
        return {"product": "psfs", "key": {"dataset": d, "fid": fid, "sca": sca, "kind": kind},
                "source_job": job, "binding": binding}

    out: list[dict] = []
    stems = list(stems)
    admitted = {s: a for s in stems if (a := selection.admits_stem(s)) is not None}
    known = snap.published_stems | {j["scope"]["file"] for j in run_jobs["intake"]}
    new = sorted(s for s in admitted if s not in known)
    claimed = set(snap.exposures) | {stem_expid(j["scope"]["file"]) for j in pending["intake"]
                                     if STEM.match(j["scope"]["file"])}

    # PSF: per (filter, SCA) of the new stems without an official science row, one job; its stems wait.
    served = set()
    for job in run_jobs["psf"]:
        m = STEM.match(job["scope"]["file"])
        served.add((_fid(m["filter"].upper(), snap.fids), int(m["sca"])))
    unready = {}
    for stem in new:
        a = admitted[stem]
        pair = (fids[a["filter"]], a["sca"])
        if (*pair, "science") not in snap.psfs:
            unready.setdefault(pair, stem)
    for (fid, sca), stem in sorted(unready.items()):
        if (fid, sca) in served:
            continue
        out.append({"stage": "psf", "scope": {"dataset": d, "file": stem}, "inputs": [],
                    "expects": [{"product": "psfs", "key": {"dataset": d, "fid": fid, "sca": sca,
                                                            "kind": "science"}, "binding": None}]})
    ready_stems = [s for s in new if (fids[admitted[s]["filter"]], admitted[s]["sca"]) not in unready]
    for stem in ready_stems[:max_intake]:
        a = admitted[stem]
        fid, expid = fids[a["filter"]], a["expid"]
        expects = [{"product": "l2files", "key": key(expid, a["sca"], fid), "binding": None}]
        if expid not in claimed:
            expects.insert(0, {"product": "exposures", "key": {"dataset": d, "expid": expid},
                               "binding": None})
            claimed.add(expid)
        out.append({"stage": "intake", "scope": {"dataset": d, "file": stem},
                    "inputs": [psf(fid, a["sca"], "science")], "expects": expects})

    selected = {k: v for k, v in snap.l2.items() if selection.admits_row(*k, set(fids.values()))}
    intake_pending = bool(pending["intake"])
    bound = snap.settings["reference"]["selection"].get("before_mjd")
    after = {k: v for k, v in selected.items() if bound is None or v["mjdobs"] >= float(bound)}

    # Reference: per (field, filter, SCA), once its frames are official and no intake is pending;
    # with the bound, from the rows before it, once the selection's intake has drained.
    min_frames = int(snap.settings["reference"]["selection"]["min_frames"])
    held_refs = {(int(j["scope"]["field"]), int(j["scope"]["fid"])) for j in taken["reference"]}
    frames = defaultdict(list)
    for (expid, sca, fid), row in sorted(selected.items(), key=lambda kv: kv[1]["mjdobs"]):
        rows = frames[row["field"], fid, sca]     # a field with no row before the bound keeps its entry
        if bound is None or (expid, sca, fid) not in after:
            rows.append((expid, row["job"]))
    ready = not intake_pending and (bound is None or all(s in snap.published_stems for s in admitted))
    held = []
    if ready:
        for (field, fid, sca), rows in sorted(frames.items()):
            if len(rows) < min_frames or (field, fid) in snap.refs or (field, fid) in held_refs:
                continue
            held_refs.add((field, fid))
            out.append({"stage": "reference", "scope": {"field": field, "fid": fid, "sca": sca},
                        "inputs": [{"product": "l2files", "key": key(e, sca, fid), "source_job": j}
                                   for e, j in rows],
                        "expects": [{"product": "refimages",
                                     "key": {"dataset": d, "field": field, "fid": fid}, "binding": None}]})
        most = defaultdict(int)                 # (field, fid) -> the most pre-bound rows of one SCA
        for (field, fid, _), rows in frames.items():
            most[field, fid] = max(most[field, fid], len(rows))
        held = [{"field": field, "fid": fid,
                 "reason": f"{n} official L2 rows before before_mjd {bound}, fewer than min_frames {min_frames}"}
                for (field, fid), n in sorted(most.items())
                if bound is not None and (field, fid) not in snap.refs and (field, fid) not in held_refs]

    # Difference: per official L2 row with an official reference, after the bound when one is set.
    held_diffs = {scope_l2(j) for j in taken["difference"]}
    for k, row in sorted(after.items()):
        expid, sca, fid = k
        ref = snap.refs.get((row["field"], fid))
        if ref is None or k in snap.diffs or k in held_diffs:
            continue
        out.append({"stage": "difference", "scope": {"expid": expid, "sca": sca, "fid": fid},
                    "inputs": [{"product": "l2files", "key": key(*k), "source_job": row["job"]},
                               psf(fid, sca, "science"),
                               {"product": "refimages", "key": {"dataset": d, "field": row["field"],
                                                                "fid": fid}, "source_job": ref}],
                    "expects": [{"product": "diffimages", "key": key(*k), "binding": None},
                                {"product": "sources", "key": key(*k), "binding": None}]})

    # Associate: per field, the earliest unassociated official source set, one at a time.
    by_field = defaultdict(list)
    for k, row in after.items():
        by_field[row["field"]].append((row["mjdobs"], k))
    # An epoch is associated when its source set is in its field's official chain, by any run of the
    # dataset (a run resumed in place of another must not associate it again), or when an associate
    # of this run naming it has not published.
    associated = {sset: {"id": job, "state": "published"} for sset, job in snap.chain.items()}
    associated.update({j["sources"]: j for j in run_jobs["associate"]
                       if j.get("sources") is not None and j["state"] != "published"})
    assoc_busy = {int(j["scope"]["field"]) for j in taken["associate"]}
    if not intake_pending:
        for field, epochs in sorted(by_field.items()):
            if field in assoc_busy:
                continue
            for _, k in sorted(epochs):
                sset = snap.sources.get(k)
                if sset is None:
                    break                       # an earlier epoch is still upstream
                if sset in associated:
                    continue
                prev = snap.objects.get(field)
                inputs = [{"product": "sources", "key": key(*k), "source_job": sset}]
                okey = {"dataset": d, "survey": snap.survey, "field": field}
                if prev is not None:
                    inputs.append({"product": "objects", "key": okey, "source_job": prev[0]})
                out.append({"stage": "associate", "scope": {"field": field}, "inputs": inputs,
                            "expects": [{"product": "objects", "key": okey,
                                         "binding": prev[1] if prev else None}]})
                break

    # Packet: per field, the earliest associated epoch without a packet set, one at a time.
    window = float(snap.settings["packet"]["forced_window_days"])
    packet_keys = {scope_l2(j) for j in taken["packet"]}
    for field, epochs in sorted(by_field.items()):
        if any(snap.l2.get(scope_l2(j), {}).get("field") == field for j in pending["packet"]):
            continue
        for mjd, k in sorted(epochs):
            assoc = associated.get(snap.sources.get(k))
            if assoc is None or assoc["state"] != "published":
                break
            if k in snap.packets:
                continue
            if k in packet_keys:
                break
            if snap.gaia is None:
                raise LookupError("the run has no official catalogs binding for the packet stage's Gaia")
            expid, sca, fid = k
            images = [(m, kk) for m, kk in sorted(epochs) if kk in snap.diffs and mjd - window <= m <= mjd]
            inputs = [{"product": "refimages", "key": {"dataset": d, "field": field, "fid": fid},
                       "source_job": snap.refs[field, fid]},
                      {"product": "catalogs", "key": snap.gaia["key"], "source_job": snap.gaia["job"]},
                      psf(fid, sca, "science"),
                      {"product": "objects", "key": {"dataset": d, "survey": snap.survey, "field": field},
                       "source_job": assoc["id"]}]
            for _, kk in images:
                inputs += [{"product": "diffimages", "key": key(*kk), "source_job": snap.diffs[kk]},
                           {"product": "l2files", "key": key(*kk), "source_job": snap.l2[kk]["job"]}]
            inputs.append({"product": "sources", "key": key(*k), "source_job": snap.sources[k]})
            inputs += [{"product": "packets", "key": key(*kk), "source_job": snap.packets[kk]}
                       for m, kk in sorted(epochs) if m < mjd and kk in snap.packets]
            out.append({"stage": "packet", "scope": {"expid": expid, "sca": sca, "fid": fid},
                        "inputs": inputs,
                        "expects": [{"product": "packets", "key": key(*k), "binding": None}]})
            break

    counts = {"listed": len(stems), "admitted": len(admitted), "new": len(new),
              "submitted": {s: sum(1 for o in out if o["stage"] == s) for s in STAGES},
              "pending": {s: len(pending[s]) for s in STAGES},
              "failed": [{"job": j["id"], "stage": j["stage"]} for j in failed], "held": held}
    return out, counts


def run_pass(conn_factory: Callable[[], Any], run: int, selection: Selection, stems: list[str],
             submit: Callable[[dict], Any], publish: Callable[[], dict] | None,
             max_intake: int = 100, batch: Any = None) -> dict:
    """One pass: reconcile with ``batch`` (a Batch client) when given, snapshot, plan, submit
    each without waiting, then publish; return the summary."""
    with conn_factory() as conn:
        checked = reconcile(conn, run, batch) if batch is not None else None
        snap = snapshot(conn, run)
    for job in snap.jobs:
        if checked and job["id"] in checked["retrying"]:
            job["state"] = "running"            # Batch's retry in flight: pending, not failed
    submissions, counts = plan(snap, stems, selection, max_intake)
    for item in submissions:
        submit(item)
    produced = publish() if publish is not None else {"packets": 0, "sets": []}
    counts["packets_produced"] = produced["packets"]
    counts["sets_produced"] = produced["sets"]
    counts["drained"] = (counts["new"] == 0 and not submissions
                         and not any(counts["pending"].values()))
    if checked is not None:
        counts["batch"] = checked
    return {"run": run, "selection": selection.summary(), **counts}
