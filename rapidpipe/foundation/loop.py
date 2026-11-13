"""The loop: ``rapidpipe loop``, one verb that carries a selection one stage forward per pass.

Foundation plan, Part 2: the live run admits what is new under its source
root, and each stage runs when its inputs are official; association is in
the difference job (the plan's amendment of 2026-10-24), so there are no
field chains, no admission order and no pass interval per epoch: the
loop's only waits are the reference's frames and the queue. One pass, :func:`run_pass`, reads the source root's listing of the
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

With ``[intake] reader = "openuniverse"`` the source root holds
OpenUniverse ``simple_model`` files, ``<filter>/<pointing>/roman_tds_<filter>
_<pointing>_wfi<sca>_simple.asdf`` with the filter in the RAPID spelling
(``H158``): ``--filters`` stays in the pipeline's names (``F158``) and is
mapped for the listing, a pointing is the survey's observing-sequence row
(``1012``), which the intake writes as the ``expid``, and there are no
passes. With ``rimtimsim`` the source root holds RimTimSim's lite files,
``rimtimsim_WFI_<filter>_SCA<NN>_<exposure>_lite.fits.gz``, flat; a pointing
is GBTDS's exposure number (``411``), the ``expid``, and there are no passes.
With ``socsims-lite`` the source root holds `dev`'s socsims lite files,
``<SOC stem>_cal_lite.fits.gz``, flat, listed and selected as the SOC's.

``--expids`` bounds the selection by the intake's ``expid``, inclusive
ranges ``LO-HI`` (a single id is ``ID``), comma separated, a stem or row
admitted when its ``expid`` is in any of them, with the other terms. A SOC
``expid`` packs pass, observation, visit and exposure in that order, so a
range is a contiguous stretch of a pass's observations (pass 2 whole and
pass 5 observation 3 visits 61 to 64 are ``2000000-2999999,5036101-5036401``).

``--programs`` selects programs within the dataset, comma-separated
program numbers (``1049,1018``, or ``-2``), all by default. A stem's program
is the one intake will write for it: the run's ``[intake] program_id`` when
its dataset sets one, else the SOC stem's own five digits; a stem with
neither is not admitted. An official L2 row's program is its exposure's
official ``exposures`` row's ``program_id``; a row whose exposure has no
official row yet is not selected until it has one. So references are built
only from the selected programs' frames and only their images are
differenced and packeted, while an official reference of the field, built
from any program, is reused. Objects are matched within the run, as always.

"Pending" is a job of the run in state ``launched`` or ``running``; a
``declined`` job is neither pending nor failed (below). A job
of the run in state ``failed`` is reported (``failed``, with its scope) and
not resubmitted, and the work that does not depend on it goes on, so a
selection drains around it: its key counts as taken until a person reruns it (``job run --job <id>``, or
``job submit --job <id>`` on Batch, which marks the row ``launched``). The one exception is a container
that died for a reason not the code's (Batch ended it while its row was
``launched`` or ``running``: a host reclaimed, a task that failed to start,
a kill): the pass resubmits it once, its Batch job named with
:data:`RERUN_SUFFIX`, and holds it pending; if the rerun's container dies
too, the job stays ``failed``.

A pass submits at most the run's ``[batch] max_submissions_per_pass``, in
stage order; the rest are ``deferred`` to the next pass.

Spot, a probe and never production (Ben, 2026-10-24), only when the run's
``[batch.spot]`` names a ``queue`` (:func:`assign_queues`, :func:`reconcile`):

- queue by backlog: a stage's submission goes to the Spot queue while the
  pass has more than ``backlog`` of that stage's jobs still to submit, else
  to the prompt queue, so each stage's last wave is on-demand;
- one attempt on Spot: a Spot submission carries a one-attempt retry
  strategy, and a Spot Batch job that ends with its row unpublished, for
  any reason (a reclaim, exit 75, ``Task failed to start``, the code's own
  failure), is the job's one rerun, on the prompt queue under the job
  definition's own retries;
- hedge: a Spot job not started, or not finished, ``hedge_factor`` times
  the stage's median ``container_seconds`` (``job_metrics``, the run's
  survey) after it was submitted or started is terminated and rerun on the
  prompt queue the same way, ``hedged`` on its submission's record;
- measure: every pass reports the run's submissions per queue, reclaims,
  hedges, lost job-hours and the fill time to 1,000 vCPU
  (``rapidpipe.foundation.attempts.report``) under ``attempts``, and the
  pass's own submissions by queue under ``queues``.

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
  18 SCAs of an exposure share it); with the dataset's ``gaia`` catalog
  official, it names that too, for the intake's WCS offset metric.
- ``reference``: a job per (field, filter) that a selected official L2
  row has as its own ``field`` (with the bound, a row at or after it), no
  official reference and none pending or failed, while no intake of the
  run is pending (so a reference coadds every epoch a drain admits). Its
  frames are the selected official rows of the filter, of any SCA, whose
  ``overlapfields`` list the field, in ``mjdobs`` order, as `dev`'s
  ``get_overlapping_l2files`` selects them (not its second key, the
  distance from the tile centre), at least ``[reference.selection]
  min_frames`` of them; its scope's SCA is the first in order among the
  rows that will be differenced against it. A dithered visit's later
  epoch so gets a reference from earlier epochs centred in other fields.
- With ``[reference.selection] selection_type = "coverage"``
  (``rapidpipe.products.coverage``) the frames are chosen by how much of the
  reference they cover: the candidates are the selected official rows of the
  filter whose ``overlapfields`` meet any tile under the reference's
  footprint, walked in ``mjdobs`` order, then distance from the reference's
  centre, then L2 key; a frame whose footprint misses the reference is
  skipped, and selection stops once ``max_coverage_fraction`` of its pixels
  reach ``max_coverage_depth`` or at ``max_frames``. The job names exactly
  the selected frames, and ``min_frames`` is not used. A field whose
  estimated fraction at ``min_coverage_depth`` is below
  ``min_coverage_fraction`` less ``coverage_estimate_tolerance`` gets no job
  and is reported under ``held`` ("estimated coverage X < Z"), with or
  without the bound, every pass, so it is planned once enough frames arrive.
- A reference job that ``declined`` (its coverage map short of the
  minimum: nothing published, ``rapidpipe.foundation.publish``) is neither
  pending nor failed: its field is reported under ``held`` with the reason
  while the frames it would name are the declined job's, and planned again
  once they differ (a new frame selected), so the same coadd is never
  rebuilt.
- With ``[reference.selection] before_mjd`` set, the reference's epochs are
  bounded: it names only those rows with ``mjdobs < before_mjd``, and
  waits until the selection's intake has drained (no
  listed stem of the selection unpublished and no intake of the run
  pending: a pass admits at most ``max_intake`` stems, so a pass with no
  intake pending alone does not mean every pre-bound epoch is in). A field
  with fewer than ``min_frames`` such rows once the intake has drained gets
  no reference and is reported under ``held`` with the reason, every pass;
  it does not stop the drain. Rows before the bound are the reference's:
  they get no difference or packet.
- ``difference``: a job per official L2 row whose field has an official
  reference and whose key has no official difference and no difference job
  of this run pending or failed. It associates its sources with the
  field's objects as they stand when it reads them, in any order.
- ``packet``: a job per official source set of a selected key with no
  official packet set and no packet job of this run pending or failed,
  naming the reference, the dataset's catalogs whose ``[packet] <name>_match``
  is on (``gaia``, ``ned``, ``lvs``; a missing one is a usage error), the science PSF and the
  source set; it reads the object catalog and the forced window as they
  stand at its read (``rapidpipe.stages.packet``).
- ``kafka``: the caller's ``publish()`` over the run's unstamped official
  packet sets: each packet job sends its own set as its last act, so this
  is the recovery sweep for a set whose job died before its
  acknowledgements, and a set still inside its job's send window is
  reported under ``sets_waiting`` and keeps the selection from draining.
  ``sets_late`` counts the run's sets a sweep sent after that window, so
  a failing container send shows in the pass line once the sweep has
  sent its set (none in a healthy run whose gate held nothing).
"""

from __future__ import annotations

import dataclasses
import re
import time
from collections import defaultdict
from typing import Any, Callable, Iterable

import psycopg2.extras

from rapidpipe.foundation import attempts
from rapidpipe.foundation.official import NotFound
from rapidpipe.products import coverage
from rapidpipe.products.filters import RAPID_TO_ROMAN_FILTER_NAMES, rapid_filter_name

#: A SOC L2 stem, the fields the selection reads (the intake stage parses all of them).
STEM = re.compile(r"r(?P<program>\d{5})\d{2}(?P<pass>\d{3})\d{3}(?P<observation>\d{3})(?P<visit>\d{3})"
                  r"_(?P<exposure>\d{4})_wfi(?P<sca>\d{2})_(?P<filter>f\d{3})$")
#: Any key under the source root that begins with a stem.
KEY = re.compile(STEM.pattern.removesuffix("$"))
SUFFIX = "_cal.asdf"
#: An OpenUniverse ``simple_model`` stem (the intake parses it, ``rapidpipe.science.intake_openuniverse``):
#: the filter in the RAPID spelling, the observing sequence's row and the SCA.
OU_STEM = re.compile(r"roman_(?:tds|was)_(?P<filter>[A-Z]\d{3})_(?P<pointing>\d+)_wfi(?P<sca>\d{2})$")
OU_SUFFIX = "_simple.asdf"
#: The ``[intake] reader`` whose stems are :data:`OU_STEM`'s.
OPENUNIVERSE = "openuniverse"
#: A RimTimSim lite stem (``rapidpipe.science.intake_rimtimsim``): the filter, the SCA and GBTDS's
#: exposure number.
RTS_STEM = re.compile(r"rimtimsim_WFI_(?P<filter>F\d{3})_SCA(?P<sca>\d{2})_(?P<exposure>\d{9})_lite$")
RTS_SUFFIX = ".fits.gz"
#: The ``[intake] reader`` whose stems are :data:`RTS_STEM`'s; every other is the SOC's.
RIMTIMSIM = "rimtimsim"
#: The ``[intake] reader`` of `dev`'s socsims lite files: SOC stems, :data:`LITE_SUFFIX`.
SOCSIMS_LITE = "socsims-lite"
LITE_SUFFIX = "_cal_lite.fits.gz"
#: The readers whose data have no passes: a pointing is one integer, the row's ``expid``.
PASSLESS = (OPENUNIVERSE, RIMTIMSIM)
PENDING = ("launched", "running")
#: Batch's job states before it ends one (``SUCCEEDED`` and ``FAILED`` end it).
BATCH_ACTIVE = ("SUBMITTED", "PENDING", "RUNNABLE", "STARTING", "RUNNING")
STAGES = ("psf", "intake", "reference", "difference", "packet")


@dataclasses.dataclass(frozen=True)
class Selection:
    """Filters (``F146``), SCAs, pointings (``(visit, exposure)``, or the observing sequence's row for
    :data:`OPENUNIVERSE`) and the survey's passes, ``None`` for all pointings or all passes; ``reader``
    the run's ``[intake] reader``; ``expids`` inclusive ``(lo, hi)`` ranges of the ``expid``, ``None``
    for all; ``programs`` the program numbers, ``None`` for all; ``fixed_program`` the run's
    ``[intake] program_id`` when its dataset sets one, every stem's program then, ``None`` when unset."""

    filters: frozenset[str]
    scas: frozenset[int]
    pointings: frozenset | None
    passes: frozenset[int] | None = None
    reader: str = "soc-l2"
    expids: tuple[tuple[int, int], ...] | None = None
    programs: frozenset[int] | None = None
    fixed_program: int | None = None

    @classmethod
    def parse(cls, filters: str, scas: str, pointings: str, passes: str = "all",
              reader: str = "soc-l2", expids: str = "all", programs: str = "all",
              fixed_program: int | str = "") -> "Selection":
        """A selection from the ``loop`` verb's terms, each comma-separated or ``all``.

        Parameters
        ----------
        filters, scas, pointings, passes, expids : str
            The verb's ``--filters``, ``--scas``, ``--pointings``, ``--passes`` and ``--expids``.
        reader : str
            The run's ``[intake] reader``.
        programs : str
            The verb's ``--programs``: program numbers, e.g. ``1049,1018`` or ``-2``, or ``all``.
        fixed_program : int or str
            The run's ``[intake] program_id``; ``""`` means unset, each stem's own program.

        Returns
        -------
        Selection
            The parsed selection; ``ValueError`` names a malformed term.
        """
        ranges = None
        if expids != "all":
            ranges = tuple(sorted((int(lo), int(hi or lo)) for lo, _, hi in
                                  (r.strip().partition("-") for r in expids.split(","))))
            if any(lo > hi for lo, hi in ranges):
                raise ValueError(f"--expids {expids}: a range's low end is above its high end")
        if reader in PASSLESS:
            if passes != "all":
                raise ValueError(f"a {reader} selection has no passes: select its pointings")
            points = None if pointings == "all" else frozenset(int(p) for p in pointings.split(","))
        else:
            points = None if pointings == "all" else frozenset(
                (int(v), int(e)) for v, e in (p.split("_") for p in pointings.split(",")))
        return cls(frozenset(f.strip().upper() for f in filters.split(",")),
                   frozenset(int(s) for s in scas.split(",")), points,
                   None if passes == "all" else frozenset(int(p) for p in passes.split(",")), reader, ranges,
                   None if programs == "all" else frozenset(int(p) for p in programs.split(",")),
                   None if fixed_program == "" else int(fixed_program))

    def admits_expid(self, expid: int) -> bool:
        return self.expids is None or any(lo <= expid <= hi for lo, hi in self.expids)

    def admits_stem(self, stem: str) -> dict | None:
        """The stem's SCA, filter and pointing when the selection admits it, else ``None``."""
        ids = stem_ids(stem)
        if ids is None or ids["reader"] != (self.reader if self.reader in PASSLESS else "soc-l2"):
            return None
        if ids["sca"] not in self.scas or ids["filter"] not in self.filters:
            return None
        if self.pointings is not None and ids["pointing"] not in self.pointings:
            return None
        if self.passes is not None and ids["pass"] not in self.passes:
            return None
        if not self.admits_expid(ids["expid"]):
            return None
        if self.programs is not None and self.stem_program(ids) not in self.programs:
            return None
        return {"sca": ids["sca"], "filter": ids["filter"], "expid": ids["expid"]}

    def stem_program(self, ids: dict) -> int | None:
        """The program intake writes for a stem: the fixed program when the run sets one, else the stem's own.

        Parameters
        ----------
        ids : dict
            :func:`stem_ids` of the stem.

        Returns
        -------
        int or None
            The program number; ``None`` for a stem with no program and no fixed program.
        """
        return self.fixed_program if self.fixed_program is not None else ids["program"]

    def admits_row(self, expid: int, sca: int, fid: int, fids: set[int], program: int | None = None) -> bool:
        """Whether an L2 key is selected, ``fids`` the selected filters' fids; ``expid``
        packs ``pass * 10**6`` and ``visit * 100 + exposure`` in its last digits, or is the
        OpenUniverse pointing; ``program`` its official ``exposures`` row's ``program_id``,
        ``None`` when the exposure has none, which a program selection never admits."""
        if sca not in self.scas or fid not in fids or not self.admits_expid(expid):
            return False
        if self.programs is not None and program not in self.programs:
            return False
        if self.reader in PASSLESS:
            return self.pointings is None or expid in self.pointings
        if self.passes is not None and expid // 10**6 not in self.passes:
            return False
        return self.pointings is None or ((expid // 100) % 100, expid % 100) in self.pointings

    def summary(self) -> dict:
        points = "all" if self.pointings is None else sorted(self.pointings)
        if self.reader not in PASSLESS and points != "all":
            points = [f"{v:03d}_{e:04d}" for v, e in points]
        return {"filters": sorted(self.filters), "scas": sorted(self.scas), "pointings": points,
                "passes": "all" if self.passes is None else sorted(self.passes),
                "expids": "all" if self.expids is None else [f"{lo}-{hi}" for lo, hi in self.expids],
                "programs": "all" if self.programs is None else sorted(self.programs)}


def stem_expid(stem: str) -> int:
    """The intake stage's expid packing, ``pass * 10**6 + observation * 10**4 + visit * 10**2 + exposure``."""
    m = STEM.match(stem)
    return (int(m["pass"]) * 10**6 + int(m["observation"]) * 10**4 + int(m["visit"]) * 10**2
            + int(m["exposure"]))


def stem_ids(stem: str) -> dict | None:
    """A SOC, OpenUniverse or RimTimSim stem's ``reader``, ``sca``, ``filter`` (the pipeline's name,
    ``F158``), ``expid``, ``pointing``, ``pass`` and ``program`` (both ``None`` but for the SOC's);
    ``None`` for any other name."""
    if m := STEM.match(stem):
        return {"reader": "soc-l2", "sca": int(m["sca"]), "filter": m["filter"].upper(),
                "expid": stem_expid(stem), "pointing": (int(m["visit"]), int(m["exposure"])),
                "pass": int(m["pass"]), "program": int(m["program"])}
    if m := OU_STEM.match(stem):
        return {"reader": OPENUNIVERSE, "sca": int(m["sca"]),
                "filter": RAPID_TO_ROMAN_FILTER_NAMES.get(m["filter"], m["filter"]),
                "expid": int(m["pointing"]), "pointing": int(m["pointing"]), "pass": None, "program": None}
    if m := RTS_STEM.match(stem):
        return {"reader": RIMTIMSIM, "sca": int(m["sca"]), "filter": m["filter"],
                "expid": int(m["exposure"]), "pointing": int(m["exposure"]), "pass": None, "program": None}
    return None


def list_stems(client: Any, source_root: str, selection: Selection) -> list[str]:
    """Every ``_cal.asdf`` stem (``_cal_lite.fits.gz``, :data:`SOCSIMS_LITE`) under ``source_root`` (``s3://bucket/prefix/``) in an observation of a
    selected filter.

    An observation (``r<program><plan><pass><segment><observation>``, the
    stem's first 17 characters) is one filter, and its number differs by
    pass, so the observations are found from the listing itself: one
    one-key request per observation, each starting after the last one's
    prefix, reads its first key's filter. Each selected observation's prefix
    is then listed through the paginator; nothing outside them is listed.

    For :data:`OPENUNIVERSE`, each selected filter's prefix (``<root>H158/``) is
    listed through the paginator and its ``_simple.asdf`` stems kept. For
    :data:`RIMTIMSIM` (one flat prefix of 263 files) the root is listed through
    the paginator and the selected filters' lite stems kept.
    """
    bucket, _, root = source_root.removeprefix("s3://").partition("/")
    if selection.reader == RIMTIMSIM:
        found = []
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=root):
            for item in page.get("Contents", []):
                name = item["Key"][len(root):]
                if (name.endswith(RTS_SUFFIX) and (m := RTS_STEM.match(name.removesuffix(RTS_SUFFIX)))
                        and m["filter"] in selection.filters):
                    found.append(name.removesuffix(RTS_SUFFIX))
        return found
    if selection.reader == OPENUNIVERSE:
        found = []
        for band in sorted(selection.filters):
            for page in client.get_paginator("list_objects_v2").paginate(
                    Bucket=bucket, Prefix=root + rapid_filter_name(band) + "/"):
                for item in page.get("Contents", []):
                    name = item["Key"].rsplit("/", 1)[-1]
                    if name.endswith(OU_SUFFIX):
                        found.append(name[: -len(OU_SUFFIX)])
        return found
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
    suffix = LITE_SUFFIX if selection.reader == SOCSIMS_LITE else SUFFIX
    for observation in observations:
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket,
                                                                     Prefix=root + observation):
            for item in page.get("Contents", []):
                name = item["Key"].rsplit("/", 1)[-1]
                if name.endswith(suffix):
                    stems.append(name[: -len(suffix)])
    return stems


@dataclasses.dataclass
class Snapshot:
    """What one pass reads from the tables, every row of the run's dataset.

    Keys are tuples: an L2 key is ``(expid, sca, fid)``, a reference key
    ``(field, fid)``. ``jobs`` are this run's jobs of the loop's stages:
    dicts with ``id``, ``stage``, ``state``, ``scope`` and ``refusal``.
    """

    dataset: str
    survey: str
    settings: dict
    fids: dict[str, int]
    published_stems: set[str]
    jobs: list[dict]
    psfs: dict[tuple[int, int, str], tuple[int, int]]          # (fid, sca, kind) -> (job, binding)
    exposures: set[int]                                          # expids with an open binding
    l2: dict[tuple[int, int, int], dict]                         # key -> {field, overlapfields, mjdobs, job, program, ra0, dec0, corners}
    refs: dict[tuple[int, int], int]                             # (field, fid) -> job
    diffs: dict[tuple[int, int, int], int]                       # key -> difference job
    sources: dict[tuple[int, int, int], int]                     # key -> source set job
    packets: dict[tuple[int, int, int], int]                     # key -> packet set job
    catalogs: dict[str, dict]                                    # name -> the dataset's {"key", "job"}
    declined: dict[int, frozenset] = dataclasses.field(default_factory=dict)  # declined reference job -> its L2 keys


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
            raise NotFound(f"run {run} does not exist")
        d = meta[0]["dataset"]
        fids = {r["filter"]: r["fid"] for r in _rows(cur, "SELECT fid, filter FROM filters")}
        stems = {r["file"] for r in _rows(
            cur, "SELECT scope->>'file' AS file FROM jobs WHERE stage = 'intake'"
                 " AND state = 'published' AND scope->>'dataset' = %s", (d,))}
        jobs = _rows(cur, "SELECT id, stage, state, scope, refusal FROM jobs WHERE run = %s AND stage = ANY(%s)"
                          " ORDER BY id", (run, list(STAGES)))
        bound = _rows(cur, "SELECT id, product, key, job FROM official WHERE closed_at IS NULL"
                           " AND job IS NOT NULL AND key->>'dataset' = %s", (d,))
        # A row's program is its exposure's official exposures row's, null while none is bound.
        l2 = {_l2key(r): {"field": r["field"], "overlapfields": r["overlapfields"], "mjdobs": r["mjdobs"],
                          "job": r["job"], "program": r["program_id"], "ra0": r["ra0"], "dec0": r["dec0"],
                          "corners": ([r[f"ra{i}"] for i in range(1, 5)], [r[f"dec{i}"] for i in range(1, 5)])}
              for r in _rows(cur, "SELECT l.expid, l.sca, l.fid, l.field, l.overlapfields, l.mjdobs, l.job,"
                                  " l.ra0, l.dec0, l.ra1, l.dec1, l.ra2, l.dec2, l.ra3, l.dec3, l.ra4, l.dec4,"
                                  " e.program_id FROM l2files_official l LEFT JOIN exposures_official e"
                                  " ON e.dataset = l.dataset AND e.expid = l.expid WHERE l.dataset = %s", (d,))}
        declined = defaultdict(set)
        for r in _rows(cur, "SELECT i.job, i.key FROM job_inputs i JOIN jobs j ON j.id = i.job"
                            " WHERE j.run = %s AND j.stage = 'reference' AND j.state = 'declined'"
                            " AND i.kind = 'input' AND i.product = 'l2files'", (run,)):
            declined[r["job"]].add(_l2key(r["key"]))
    conn.commit()
    by = defaultdict(list)
    for b in bound:
        by[b["product"]].append(b)
    catalogs: dict[str, dict] = {}
    for b in by["catalogs"]:
        name = b["key"].get("name")
        if name in catalogs:
            raise NotFound(f"dataset {d} has two official catalogs named {name!r}; the loop names one per name")
        catalogs[name] = {"key": b["key"], "job": b["job"]}
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
        packets={_l2key(b["key"]): b["job"] for b in by["packets"]},
        catalogs=catalogs, declined={job: frozenset(keys) for job, keys in declined.items()})


#: The Batch job name's suffix of the loop's one automatic rerun of a job (``launch.submit``'s
#: ``name_suffix``): a job whose rerun's container died too stays ``failed``.
RERUN_SUFFIX = "-rerun"


def _queue_name(described: dict) -> str:
    """The job queue's name from Batch's description (``jobQueue`` is an ARN, ``.../job-queue/<name>``)."""
    return described.get("jobQueue", "").rsplit("/", 1)[-1]


def _iso(ms: int | None) -> str | None:
    """Batch's epoch milliseconds as an ISO 8601 UTC string."""
    if not ms:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(ms / 1000))


def median_seconds(conn, run: int) -> dict[str, float]:
    """Per stage, the median ``container_seconds`` of ``job_metrics`` over the run's survey."""
    with conn.cursor() as cur:
        cur.execute("SELECT stage, percentile_cont(0.5) WITHIN GROUP (ORDER BY value) FROM job_metrics"
                    " WHERE metric = 'container_seconds' AND survey = (SELECT survey FROM runs WHERE id = %s)"
                    " GROUP BY stage", (run,))
        found = {stage: float(m) for stage, m in cur.fetchall()}
    conn.commit()
    return found


def reconcile(conn, run: int, client: Any, spot: dict | None = None,
              medians: dict[str, float] | None = None, clock: Callable[[], float] = time.time) -> dict:
    """Read Batch for the run's unpublished jobs with a ``batch_job_id``; mark the dead ``failed``.

    Returns ``{"described", "ended", "retrying", "rerun", "hedged", "reclaims", "seconds"}``:
    ``ended`` the rows this call marked ``failed`` (``job``, ``stage``,
    ``reason``), ``retrying`` the ids of ``failed`` rows whose Batch job has
    not ended, ``rerun`` the ``failed`` rows whose container died without
    ``job run`` recording it (the reason Batch's, as ``ended`` records it,
    not the code's) on a first submission, not on a rerun
    (:data:`RERUN_SUFFIX`): each is owed the loop's one automatic rerun. A
    job without a Batch id (a local one), or one Batch no longer describes,
    is left alone. Every ended Batch job's stop is added to its
    submission's record (``rapidpipe.foundation.attempts``).

    With ``spot`` (the run's ``[batch.spot]``), a first submission on its
    queue that ended unpublished is owed the rerun whatever the reason, and
    with ``medians`` (:func:`median_seconds`) a Spot job past ``hedge_factor``
    times its stage's median since its submission (not started) or its start
    is terminated and owed it too, listed under ``hedged``; ``reclaims``
    counts this call's Spot jobs ended by a reclaim.
    """
    started = time.monotonic()
    with conn.cursor() as cur:
        cur.execute("SELECT id, stage, state, batch_job_id, refusal FROM jobs WHERE run = %s"
                    " AND state NOT IN ('published', 'declined') ORDER BY id", (run,))
        rows = [r for r in cur.fetchall() if r[3]]
    conn.commit()
    ids = [r[3] for r in rows]
    batch = {}
    for i in range(0, len(ids), 100):
        batch.update((b["jobId"], b) for b in client.describe_jobs(jobs=ids[i:i + 100])["jobs"])
    spot_queue = (spot or {}).get("queue")
    factor = (spot or {}).get("hedge_factor")
    ended, retrying, rerun, hedged, reclaims = [], [], [], [], 0
    for job, stage, state, batch_id, refusal in rows:
        described = batch.get(batch_id)
        if described is None:
            continue
        status = described["status"]
        first = not described.get("jobName", "").endswith(RERUN_SUFFIX)
        on_spot = bool(spot_queue) and _queue_name(described) == spot_queue and first
        if status in BATCH_ACTIVE:
            if state == "failed":
                retrying.append(job)
            elif on_spot and factor and medians and stage in medians and state in PENDING:
                since = described.get("startedAt") or described.get("createdAt")
                waited = clock() - since / 1000 if since else 0.0
                if waited > float(factor) * medians[stage]:
                    reason = (f"hedge: {'running' if described.get('startedAt') else 'not started'}"
                              f" {waited:.0f} s, over {factor} x the {stage} median {medians[stage]:.0f} s")
                    client.terminate_job(jobId=batch_id, reason=reason[:250])
                    with conn.cursor() as cur:
                        attempts.record(cur, job, batch_id, {"hedged": attempts.now(), "status_reason": reason})
                    conn.commit()
                    hedged.append({"job": job, "stage": stage, "reason": reason})
                    rerun.append({"job": job, "stage": stage, "reason": reason})
            continue
        reason = f"Batch {status}: {described.get('statusReason', '')}".strip()
        exit_code = described.get("container", {}).get("exitCode")
        with conn.cursor() as cur:
            attempts.record(cur, job, batch_id, {
                "stopped": _iso(described.get("stoppedAt")), "status": status,
                "status_reason": described.get("statusReason"), "exit_code": exit_code})
        conn.commit()
        if on_spot and (described.get("statusReason") or "").startswith(attempts.RECLAIM):
            reclaims += 1
        if state in PENDING:
            with conn.cursor() as cur:
                cur.execute("UPDATE jobs SET state = 'failed', ended = now(), exit_code = %s,"
                            " refusal = %s WHERE id = %s AND state IN ('launched', 'running')",
                            (exit_code, reason, job))
                if cur.rowcount:
                    ended.append({"job": job, "stage": stage, "reason": reason})
                    state, refusal = "failed", reason
            conn.commit()
        if state == "failed" and first and (on_spot or (refusal or "").startswith("Batch ")):
            rerun.append({"job": job, "stage": stage, "reason": refusal})
    return {"described": len(batch), "ended": ended, "retrying": retrying, "rerun": rerun,
            "hedged": hedged, "reclaims": reclaims, "seconds": round(time.monotonic() - started, 2)}


def assign_queues(submissions: list[dict], spot: dict | None) -> None:
    """Name the Spot queue on each submission (``"queue"``) while more than the run's ``backlog`` of its
    stage's submissions remain to be made in this pass, itself included; the rest name none (the
    prompt queue). Nothing without a ``[batch.spot]`` queue."""
    if not spot:
        return
    backlog = int(spot.get("backlog", 0))
    left = defaultdict(int)
    for item in submissions:
        left[item["stage"]] += 1
    for item in submissions:
        if left[item["stage"]] > backlog:
            item["queue"] = spot["queue"]
        left[item["stage"]] -= 1


def _fid(band: str, fids: dict[str, int]) -> int:
    """The filters table's fid for a Roman filter name: ``F146`` is the table's ``W146``."""
    for name, fid in fids.items():
        if name[1:] == band[1:]:
            return fid
    raise NotFound(f"no filters row for {band}")


def reference_center(reference: dict, field: int) -> tuple[float, float]:
    """The reference's centre: ``[reference.mosaic]`` ``ra_center``/``dec_center`` when set, else the field's.

    Parameters
    ----------
    reference : dict
        The run's ``[reference]`` table.
    field : int
        The reference's field (Roman tile).

    Returns
    -------
    tuple of float
        ``(ra, dec)`` [deg], as the reference stage centres its mosaic.
    """
    mosaic = reference["mosaic"]
    if mosaic.get("ra_center", "") == "":
        return coverage.tile_center(field)
    return float(mosaic["ra_center"]), float(mosaic["dec_center"])


def select_by_coverage(reference: dict, pool: list[tuple[tuple, dict]],
                       needs: dict[tuple[int, int], list]) -> dict[tuple[int, int], str]:
    """Choose each needed reference's frames by coverage (``selection_type = "coverage"``), in place.

    For each ``(field, fid)`` of ``needs``: the candidates are the rows of
    ``pool`` of that filter whose ``overlapfields`` meet any tile under
    the reference's footprint (``overlapfields &&``), walked in ``mjdobs``
    order, then distance from the reference's centre, then L2 key, through
    :func:`rapidpipe.products.coverage.select_refimage_inputs`. A build or
    verify fills ``needs[(field, fid)]`` with the selected frames; a reject
    is held.

    Parameters
    ----------
    reference : dict
        The run's ``[reference]`` table.
    pool : list of (key, row)
        The selected official L2 rows a reference may take (before the bound, when set).
    needs : dict
        ``(field, fid)`` to an empty list, filled with the chosen ``(expid, sca, job)``.

    Returns
    -------
    dict
        ``(field, fid)`` to the reason it is held, ``"estimated coverage X < Z ..."``.
    """
    chosen, mosaic = reference["selection"], reference["mosaic"]
    naxis1, naxis2 = int(mosaic["naxis1"]), int(mosaic["naxis2"])
    held = {}
    tiles_of: dict[int, tuple] = {}
    for field, fid in sorted(needs):
        if field not in tiles_of:
            center = reference_center(reference, field)
            w = coverage.make_refimage_wcs(*center, naxis1, naxis2, float(mosaic["cdelt1"]),
                                           float(mosaic["cdelt2"]), float(mosaic["rotation"]))
            tiles_of[field] = (center, w, set(coverage.reference_tiles(w, naxis1, naxis2)))
        center, w, tiles = tiles_of[field]
        candidates = sorted(((k, row) for k, row in pool if k[2] == fid and tiles.intersection(row["overlapfields"])),
                            key=lambda kv: coverage.candidate_order(kv[1]["mjdobs"], kv[1]["ra0"], kv[1]["dec0"],
                                                                    kv[0], center))
        picked, estimate, _, decision = coverage.select_refimage_inputs(
            w, naxis1, naxis2, [row["corners"] for _, row in candidates],
            float(chosen["min_coverage_depth"]), float(chosen["min_coverage_fraction"]),
            float(chosen["max_coverage_depth"]), float(chosen["max_coverage_fraction"]), int(chosen["max_frames"]),
            float(chosen["coverage_estimate_tolerance"]))
        if decision == "reject":
            held[field, fid] = (f"estimated coverage {estimate:.3f} < {chosen['min_coverage_fraction']}"
                                f" (pixels at depth {chosen['min_coverage_depth']} or more, {len(picked)} of"
                                f" {len(candidates)} candidate frames)")
            continue
        needs[field, fid].extend((k[0], k[1], row["job"]) for k, row in (candidates[i] for i in picked))
    return held


def plan(snap: Snapshot, stems: Iterable[str], selection: Selection,
         max_intake: int = 100, max_submissions: int | None = None) -> tuple[list[dict], dict]:
    """Every submission one pass makes, and the pass's counts; reads nothing but its arguments.

    A submission is ``{"stage", "scope", "inputs", "expects"}``, ``job submit``'s arguments.
    With ``max_submissions``, only the first that many are made, in stage order, and
    ``deferred`` counts the rest; the next pass plans them again. (An exposure's
    ``exposures`` expectation rides on its first SCA's intake, earlier in the order than
    its other SCAs', so a cut never strands it.) A job published but refused its binding
    (``refusal`` set) is reported under ``failed`` with a failed one, each with its
    ``refusal``, and like a failed one holds its key: neither is submitted again.
    """
    d = snap.dataset
    fids = {name: _fid(name, snap.fids) for name in selection.filters}
    run_jobs = defaultdict(list)
    for job in snap.jobs:
        run_jobs[job["stage"]].append(job)
    pending = {s: [j for j in run_jobs[s] if j["state"] in PENDING] for s in STAGES}
    failed = [j for j in snap.jobs if j["state"] == "failed" or (j["state"] == "published" and j.get("refusal"))]
    taken = {s: [j for j in run_jobs[s] if j["state"] in ("launched", "running", "failed")
                 or (j["state"] == "published" and j.get("refusal"))] for s in STAGES}

    def key(expid, sca, fid):
        return {"dataset": d, "expid": expid, "sca": sca, "fid": fid}

    def scope_l2(job):
        return int(job["scope"]["expid"]), int(job["scope"]["sca"]), int(job["scope"]["fid"])

    def psf(fid, sca, kind):
        if (fid, sca, kind) not in snap.psfs:
            raise NotFound(f"no official {kind} psfs row for fid {fid} sca {sca} in {d}")
        job, binding = snap.psfs[fid, sca, kind]
        return {"product": "psfs", "key": {"dataset": d, "fid": fid, "sca": sca, "kind": kind},
                "source_job": job, "binding": binding}

    out: list[dict] = []
    stems = list(stems)
    admitted = {s: a for s in stems if (a := selection.admits_stem(s)) is not None}
    known = snap.published_stems | {j["scope"]["file"] for j in run_jobs["intake"]}
    new = sorted(s for s in admitted if s not in known)
    claimed = set(snap.exposures) | {ids["expid"] for j in pending["intake"]
                                     if (ids := stem_ids(j["scope"]["file"])) is not None}

    # PSF: per (filter, SCA) of the new stems without an official science row, one job; its stems wait.
    served = set()
    for job in run_jobs["psf"]:
        ids = stem_ids(job["scope"]["file"])
        served.add((_fid(ids["filter"], snap.fids), ids["sca"]))
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
        inputs = [psf(fid, a["sca"], "science")]
        if "gaia" in snap.catalogs:            # the intake measures the WCS against it
            inputs.append({"product": "catalogs", "key": snap.catalogs["gaia"]["key"],
                           "source_job": snap.catalogs["gaia"]["job"]})
        out.append({"stage": "intake", "scope": {"dataset": d, "file": stem},
                    "inputs": inputs, "expects": expects})

    selected = {k: v for k, v in snap.l2.items() if selection.admits_row(*k, set(fids.values()), v["program"])}
    intake_pending = bool(pending["intake"])
    bound = snap.settings["reference"]["selection"].get("before_mjd")
    after = {k: v for k, v in selected.items() if bound is None or v["mjdobs"] >= float(bound)}

    # Reference: per (field, filter) a row to be differenced has as its own field, from the rows
    # (before the bound, when set) whose overlapfields list it ("nframes") or any tile under the
    # reference ("coverage"), of any SCA, once no intake is pending; with the bound, once the
    # selection's intake has drained.
    chosen = snap.settings["reference"]["selection"]
    mode = chosen.get("selection_type", "nframes")
    if mode not in coverage.SELECTION_TYPES:
        raise ValueError(f"[reference.selection] selection_type {mode!r} is not one of {coverage.SELECTION_TYPES}")
    min_frames = int(chosen["min_frames"])
    held_refs = {(int(j["scope"]["field"]), int(j["scope"]["fid"])) for j in taken["reference"]}
    declined = defaultdict(list)                # (field, fid) -> its declined jobs' (L2 keys, job, reason)
    for j in run_jobs["reference"]:
        if j["state"] == "declined":
            declined[int(j["scope"]["field"]), int(j["scope"]["fid"])].append(
                (snap.declined.get(j["id"], frozenset()), j["id"], j.get("refusal")))
    scope_sca = {}                              # (field, fid) -> the first SCA of the rows it serves
    for (_, sca, fid), row in after.items():
        scope_sca[row["field"], fid] = min(sca, scope_sca.get((row["field"], fid), sca))
    pool = sorted(((k, row) for k, row in selected.items() if bound is None or k not in after),
                  key=lambda kv: (kv[1]["mjdobs"], kv[0]))
    frames: dict[tuple[int, int], list] = {need: [] for need in scope_sca}
    held_why: dict[tuple[int, int], str] = {}
    ready = not intake_pending and (bound is None or all(s in snap.published_stems for s in admitted))
    if mode == "nframes":
        for (expid, sca, fid), row in pool:
            for field in row["overlapfields"]:
                if (field, fid) in frames:
                    frames[field, fid].append((expid, sca, row["job"]))
        held_why = {need: f"{len(rows)} official L2 rows before before_mjd {bound}, fewer than min_frames {min_frames}"
                    for need, rows in frames.items() if bound is not None and len(rows) < min_frames}
    elif ready:
        skip = set(snap.refs) | held_refs
        held_why = select_by_coverage(snap.settings["reference"], pool,
                                      {need: rows for need, rows in frames.items() if need not in skip})
    held = []
    if ready:
        for (field, fid), rows in sorted(frames.items()):
            if (field, fid) in snap.refs or (field, fid) in held_refs:
                continue
            if (field, fid) in held_why:
                held.append({"field": field, "fid": fid, "reason": held_why[field, fid]})
                continue
            if mode == "nframes" and len(rows) < min_frames:
                continue
            same = [(job, why) for keys, job, why in declined[field, fid]
                    if keys == {(e, s, fid) for e, s, _ in rows}]
            if same:
                held.append({"field": field, "fid": fid,
                             "reason": f"job {same[-1][0]} declined these frames: {same[-1][1]}"})
                continue
            held_refs.add((field, fid))
            out.append({"stage": "reference", "scope": {"field": field, "fid": fid, "sca": scope_sca[field, fid]},
                        "inputs": [{"product": "l2files", "key": key(e, s, fid), "source_job": j}
                                   for e, s, j in rows],
                        "expects": [{"product": "refimages",
                                     "key": {"dataset": d, "field": field, "fid": fid}, "binding": None}]})

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

    # Packet: per official source set of a selected key, once; it reads the catalog as it stands.
    packet_keys = {scope_l2(j) for j in taken["packet"]}
    for k, row in sorted(after.items()):
        if k not in snap.sources or k in snap.packets or k in packet_keys:
            continue
        # A switch the run's body lacks is off: the body was resolved before that catalog
        # existed and the run never named it (ss joined on 2026-11-12).
        wanted = [n for n in ("gaia", "ned", "lvs", "ss") if snap.settings["packet"].get(f"{n}_match", False)]
        missing = [n for n in wanted if n not in snap.catalogs]
        if missing:
            raise NotFound(f"the run has no official catalogs binding named {', '.join(missing)} for the"
                           f" packet stage, and [packet] {', '.join(f'{n}_match' for n in missing)} is on")
        expid, sca, fid = k
        out.append({"stage": "packet", "scope": {"expid": expid, "sca": sca, "fid": fid},
                    "inputs": [{"product": "refimages", "key": {"dataset": d, "field": row["field"],
                                                                "fid": fid},
                                "source_job": snap.refs[row["field"], fid]},
                               *[{"product": "catalogs", "key": snap.catalogs[n]["key"],
                                  "source_job": snap.catalogs[n]["job"]} for n in wanted],
                               psf(fid, sca, "science"),
                               {"product": "sources", "key": key(*k), "source_job": snap.sources[k]}],
                    "expects": [{"product": "packets", "key": key(*k), "binding": None}]})

    deferred = len(out) - max_submissions if max_submissions is not None and len(out) > max_submissions else 0
    if deferred:
        out = out[:max_submissions]
    spot = snap.settings.get("batch", {}).get("spot") or {}
    assign_queues(out, spot if spot.get("queue") else None)
    counts = {"listed": len(stems), "admitted": len(admitted), "new": len(new), "deferred": deferred,
              "submitted": {s: sum(1 for o in out if o["stage"] == s) for s in STAGES},
              "pending": {s: len(pending[s]) for s in STAGES},
              "failed": [{"job": j["id"], "stage": j["stage"], "scope": j["scope"],
                          "refusal": j.get("refusal")} for j in failed],
              "held": held}
    return out, counts


def run_pass(conn_factory: Callable[[], Any], run: int, selection: Selection, stems: list[str],
             submit: Callable[[dict], Any], publish: Callable[[], dict] | None,
             max_intake: int = 100, batch: Any = None,
             rerun: Callable[[int, str], Any] | None = None,
             held_sets: Callable[[], list[dict]] | None = None, spot: dict | None = None) -> dict:
    """One pass: reconcile with ``batch`` (a Batch client) when given, rerun once through ``rerun``
    (``(job, stage)``, on the prompt queue) each job whose container died for a reason not the
    code's, or whose Spot submission ended unpublished or was hedged (``spot``, the run's
    ``[batch.spot]``), snapshot, plan at most ``[batch] max_submissions_per_pass`` submissions,
    submit each without waiting (``submit(item)`` returns the job id), then publish; return the
    summary, with ``held_sets``, the quality gate's held packet sets after the publish
    (``held_sets()``), when given, and the run's ``attempts`` report."""
    with conn_factory() as conn:
        if batch is not None:
            medians = median_seconds(conn, run) if spot and spot.get("hedge_factor") else None
            checked = reconcile(conn, run, batch, spot, medians)
        else:
            checked = None
        snap = snapshot(conn, run)
    rerunning = set()
    if checked and rerun is not None:
        for died in checked.get("rerun", []):
            rerun(died["job"], died["stage"])
            rerunning.add(died["job"])
    for job in snap.jobs:
        if checked and job["id"] in checked["retrying"] or job["id"] in rerunning:
            job["state"] = "running"            # Batch's retry or the rerun in flight: pending, not failed
    cap = snap.settings.get("batch", {}).get("max_submissions_per_pass")
    submissions, counts = plan(snap, stems, selection, max_intake, cap)
    queues = defaultdict(list)
    for item in submissions:
        queues[item.get("queue") or "prompt"].append(submit(item))
    counts["queues"] = dict(queues)
    produced = publish() if publish is not None else {"packets": 0, "sets": []}
    counts["packets_produced"] = produced["packets"]
    counts["sets_produced"] = produced["sets"]
    counts["sets_waiting"] = produced.get("waiting", [])
    counts["sets_late"] = produced.get("late", 0)
    counts["drained"] = (counts["new"] == 0 and not submissions
                         and not any(counts["pending"].values()) and not counts["sets_waiting"])
    if held_sets is not None:
        counts["held_sets"] = held_sets()
    if checked is not None:
        counts["batch"] = checked
        with conn_factory() as conn:
            counts["attempts"] = attempts.report(attempts.run_entries(conn, run), (spot or {}).get("queue"))
    counts["rerun"] = sorted(rerunning)
    return {"run": run, "selection": selection.summary(), **counts}


#: The job states ``job wait`` waits for.
ENDED = ("published", "failed", "declined")


def wait(conn_factory: Callable[[], Any], jobs: list[int], batch: Any, timeout: float,
         interval: float = 30.0, sleep: Callable[[float], None] = time.sleep,
         clock: Callable[[], float] = time.monotonic) -> dict:
    """Block until every job of ``jobs`` is ``published``, ``failed`` or ``declined``, or ``timeout`` seconds pass.

    Each check first reconciles the jobs' runs with Batch (``batch``, a Batch
    client, or ``None`` for local jobs), so a container that died without its
    row recording it ends the wait as ``failed``, as a loop pass marks it; a
    ``failed`` row whose Batch job is still active (Batch's own retry) is not
    ended. Returns ``{"states": {job: state}, "ended": bool, "seconds"}``;
    ``NotFound`` names a job that does not exist.
    """
    started = clock()
    while True:
        with conn_factory() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, run, state FROM jobs WHERE id = ANY(%s)", (list(jobs),))
                rows = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
            conn.commit()
            missing = sorted(set(jobs) - set(rows))
            if missing:
                raise NotFound(f"no job {', '.join(map(str, missing))}")
            retrying = set()
            if batch is not None:
                for run in sorted({run for run, state in rows.values() if state not in ("published", "declined")}):
                    retrying |= set(reconcile(conn, run, batch)["retrying"])
                with conn.cursor() as cur:
                    cur.execute("SELECT id, run, state FROM jobs WHERE id = ANY(%s)", (list(jobs),))
                    rows = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
                conn.commit()
        states = {job: rows[job][1] for job in jobs}
        ended = all(state in ENDED and job not in retrying for job, state in states.items())
        seconds = round(clock() - started, 1)
        if ended or seconds >= timeout:
            return {"states": states, "ended": ended, "seconds": seconds}
        sleep(min(interval, max(0.0, timeout - seconds)))
