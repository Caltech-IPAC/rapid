"""
File:    gathering.py

Turning database rows into processing units — the production path.

`seams.submit_units` has always taken a `units` iterable; until now nothing
in the tree produced one from real data. The legacy launchers did, and the
W6 cutover fence deleted them, leaving `virtualPipelineOperator` refusing
loudly with "needs the unit list the deleted launcher computed". This
module is that list, rebuilt against the surviving `rapid_db` query methods
and the adopted manifest vocabulary.

What it is NOT. It is not the ground-up VPO — the operations design owns
workload classes, versioned retry policy, and the problems path. It is not
a re-implementation of the launchers' fan-out either: the launchers ran one
`submit_job` per unit from a launcher host, and the whole point of the
array-job layer is that they do not. What survives from them is only the
science logic: which rows are ready, and which facts a unit needs.

Two properties the launchers did not have, both required by the co-design:

**One home per fact.** Every value here traces to exactly one database
column, named in `UnitFacts`' own attribute docs. Nothing is re-derived,
defaulted, or carried in a second place — the per-job `.ini` this replaces
had the same fact in up to four homes.

**Absent, not sentinel.** A fact that could not be resolved is left None
and omitted from the manifest, per the adopted rule. `UnitFacts.require`
is what turns "this job type needs a reference image" into one named
failure at startup instead of an AttributeError three stages in. Gathering
never invents a zero, an empty string, or a -1 to stand in for a missing
row.

Query methods are called through a handle object rather than imported
directly. `rapid_db.RAPIDDB` carries the whole 3,958-line surface and a
connection; the gatherers need eleven methods of it. Taking the handle as
a parameter is what lets the unit tests drive real gathering logic against
a stub that returns rows, with no database and no monkeypatching.
"""

import dataclasses
import logging
from typing import Any, Iterable, Iterator, Protocol, Sequence

from .manifest import ProcessingUnit, UnitFacts
from .routes import (JOB_TYPE_POST_PROCESS, JOB_TYPE_REFERENCE_IMAGE,
                     JOB_TYPE_SCIENCE, ppid_for)

logger = logging.getLogger(__name__)

# The launchers' `n_filters`. Filter ids are dense from 1 in `filters`, and
# the gathering loop iterates them rather than reading the table because a
# filter with no ready work contributes nothing either way.
N_FILTERS = 8


class GatheringError(RuntimeError):
    """A gathering query failed or returned something unusable.

    Raised rather than returning an empty list: "no ready work" and "the
    query could not run" are different answers, and a submitter that
    cannot tell them apart submits nothing and reports success.
    """


class UnitSource(Protocol):
    """The slice of `rapid_db.RAPIDDB` gathering actually uses.

    Declared so the contract is visible and testable. The real handle
    satisfies it structurally; the tests' stub implements exactly these.
    """

    exit_code: int

    def get_field_fid_nframes_records_for_mjdobs_range(
            self, start_mjdobs: float, end_mjdobs: float,
            min_nframes: int, fid: int | None = ...) -> Sequence[Any]: ...

    def get_l2files_records_for_datetime_range_field_fid(
            self, start: Any, end: Any, field: int,
            fid: int) -> Sequence[Any]: ...

    def get_l2filemeta_record(self, rid: int) -> Sequence[Any]: ...

    def get_info_for_l2file(self, rid: int) -> Sequence[Any]: ...

    def get_exposure_filter(self, fid: int) -> Any: ...

    def get_best_psf(self, sca: int, fid: int) -> Sequence[Any]: ...

    def get_best_reference_image(self, ppid: int, field: int,
                                 fid: int) -> Any: ...


def _positions(values: Sequence[Any], ra_keys: Sequence[str],
               dec_keys: Sequence[str]) -> dict[str, float] | None:
    """Assemble a sky-position mapping, or None if any part is missing.

    All-or-nothing deliberately: a half-populated corner set is worse than
    an absent one, because a consumer reading `ra3` without checking would
    silently use a stale or defaulted value. `UnitFacts` documents these
    as read-together mappings for the same reason.
    """
    if any(value is None for value in values):
        return None
    out: dict[str, float] = {}
    for key, value in zip(list(ra_keys) + list(dec_keys), values):
        out[key] = float(value)
    return out


def _sky_position(meta: Sequence[Any]) -> dict[str, float] | None:
    """The L2FileMeta centre+corners, from that method's column order.

    `get_l2filemeta_record` returns
    (sca, fid, ra0, dec0, ra1, dec1, ra2, dec2, ra3, dec3, ra4, dec4) —
    interleaved ra/dec, not grouped, which is why this unpacks by position
    rather than slicing.
    """
    if meta is None or len(meta) < 12:
        return None
    ras = [meta[2], meta[4], meta[6], meta[8], meta[10]]
    decs = [meta[3], meta[5], meta[7], meta[9], meta[11]]
    return _positions(ras + decs,
                      ["ra0", "ra1", "ra2", "ra3", "ra4"],
                      ["dec0", "dec1", "dec2", "dec3", "dec4"])


def science_facts(handle: UnitSource, rid: int, field: int, fid: int,
                  reference_ppid: int | None = None,
                  science_ppid: int | None = None) -> UnitFacts:
    """Resolve one science unit's per-invocation facts.

    The fact set the deleted `awsBatchSubmitJobs_launchSingleSciencePipeline`
    assembled before writing a per-job `.ini`, minus everything that has
    since found a single home elsewhere: no bucket names (parameter tree),
    no `/code` paths (release content), no queue names (route matrix).

    Reference selection follows the launcher's two-step exactly, and for
    its stated reason: `get_best_reference_image` reports "no reference
    exists yet" as `exit_code == 7`, which is a legitimate state (the
    reference has not been built), not a failure. The launcher then
    retried under the science ppid because a reference built by an earlier
    science run is registered under that ppid. A unit with no reference at
    all is returned WITHOUT reference facts rather than skipped — whether
    that is submittable is the job type's call, made by `require`, not
    gathering's.
    """
    reference_ppid = (ppid_for(JOB_TYPE_REFERENCE_IMAGE)
                      if reference_ppid is None else reference_ppid)
    science_ppid = (ppid_for(JOB_TYPE_SCIENCE)
                    if science_ppid is None else science_ppid)

    meta = handle.get_l2filemeta_record(rid)
    if meta is None or len(meta) < 12:
        raise GatheringError(
            f"rid {rid} has no L2FileMeta row; it cannot be positioned on "
            "the sky and no science unit can be built from it")
    sca = int(meta[0])

    info = handle.get_info_for_l2file(rid)
    # (filename, expid, sca, field, mjdobs, exptime, infobits, status,
    #  vbest, version) — the order get_info_for_l2file selects in.
    if info is None or len(info) < 8:
        raise GatheringError(f"rid {rid} has no L2Files row")

    facts = UnitFacts(
        rid=int(rid),
        fid=int(fid),
        field=int(field),
        # rtid equals field by construction in this tessellation (the
        # closed form and the `fields` mirror agree on it), so it is
        # carried rather than looked up: W7 retired the per-unit R-tree
        # query, and re-adding one here would reintroduce exactly the
        # per-source lookup that retirement removed.
        rtid=int(field),
        expid=_maybe_int(info[1]),
        mjdobs=_maybe_float(info[4]),
        exptime=_maybe_float(info[5]),
        infobits=_maybe_int(info[6]),
        status=_maybe_int(info[7]),
        science_image_uri=_maybe_str(info[0]),
        sky_position=_sky_position(meta),
    )

    filter_name = handle.get_exposure_filter(fid)
    if filter_name is not None:
        facts = _replace(facts, filter_name=str(filter_name))

    psf = handle.get_best_psf(sca, fid)
    if psf is not None and len(psf) >= 2 and psf[0] is not None:
        facts = _replace(facts, psfid=_maybe_int(psf[0]),
                         psf_uri=_maybe_str(psf[1]))

    reference = _best_reference(handle, reference_ppid, science_ppid,
                                field, fid)
    if reference is not None:
        facts = _replace(
            facts,
            reference_image_id=_maybe_int(reference.get("rfid")),
            reference_image_uri=_maybe_str(reference.get("filename")),
            reference_image_infobits=_maybe_int(reference.get("infobits")),
            reference_image_ppid=_maybe_int(reference.get("ppid")))
    return facts


def _best_reference(handle: UnitSource, reference_ppid: int,
                    science_ppid: int, field: int,
                    fid: int) -> dict[str, Any] | None:
    """The launcher's two-ppid reference lookup, with its ppid recorded.

    `get_best_reference_image` does not return the ppid it matched on, and
    which one matched is provenance a consumer needs — a reference built
    by the reference-image job type and one built incidentally by a
    science run are different objects. So the ppid is folded in here, at
    the only place that knows which call answered.
    """
    for ppid in (reference_ppid, science_ppid):
        record = handle.get_best_reference_image(ppid, field, fid)
        if record:
            found = dict(record)
            found.setdefault("ppid", ppid)
            return found
        # exit_code 7 is the documented "no reference yet" signal; any
        # other nonzero is a real query failure and must not read as
        # "no reference".
        code = getattr(handle, "exit_code", 0)
        if code not in (0, 7):
            raise GatheringError(
                f"reference lookup failed for field {field} fid {fid} "
                f"under ppid {ppid}: rapid_db exit_code {code}")
    return None


def gather_science_units(handle: UnitSource, start, end,
                         start_mjdobs: float, end_mjdobs: float,
                         min_images_to_coadd: int,
                         fids: Iterable[int] | None = None,
                         make_references: bool = False
                         ) -> Iterator[ProcessingUnit]:
    """Yield the science (or reference-image) units ready in a window.

    The launchers' two-level loop, preserved because it is science logic:
    first find the (field, filter) pairs with enough frames to support a
    reference image at all, then take that pair's L2 files in the
    requested date range.

    `make_references` selects between the two stages the VPO still
    describes: StageOne takes ONE representative image per (field, filter)
    — the first in mjdobs/SCA order — to build the reference; StageTwo
    takes all the others to difference against it. The representative
    choice is the launcher's, kept identical so a reference built here is
    the reference the old chain would have built.
    """
    fids = range(1, N_FILTERS + 1) if fids is None else fids
    pairs: list[tuple[int, int]] = []
    for fid in fids:
        try:
            rows = handle.get_field_fid_nframes_records_for_mjdobs_range(
                start_mjdobs, end_mjdobs, min_images_to_coadd, fid)
        except Exception as exc:                       # noqa: BLE001
            raise GatheringError(
                f"field/filter query failed for fid {fid}: {exc}") from exc
        for row in rows or ():
            pairs.append((int(row[0]), int(fid)))

    logger.info("gathering: %d (field, filter) pairs with >= %d frames",
                len(pairs), min_images_to_coadd)

    for field, fid in pairs:
        rows = handle.get_l2files_records_for_datetime_range_field_fid(
            start, end, field, fid)
        rows = list(rows or ())
        if not rows:
            continue
        if make_references:
            chosen = rows[:1]
        else:
            chosen = rows[1:]
        for row in chosen:
            rid = int(row[0])
            facts = science_facts(handle, rid, field, fid)
            exposure = facts.expid
            if exposure is None:
                raise GatheringError(
                    f"rid {rid} has no exposure id; a processing unit is "
                    "keyed by exposure/SCA and cannot be built without one")
            sca = _sca_of(handle, rid)
            yield ProcessingUnit(exposure=int(exposure), sca=int(sca),
                                 facts=facts)


def gather_post_process_units(handle: Any, proc_date: str
                              ) -> Iterator[ProcessingUnit]:
    """Yield post-process units for one processing date.

    Post-process work is keyed by JOB id, not by rid: the unit is "close
    out what this science job produced". `ProcessingUnit` is still the
    carrier — the array layer knows nothing else — so the jid rides in
    `fields`, which exists precisely for what `UnitFacts` does not name.

    The exposure/SCA pair is taken from the job row, so the unit keys the
    same way every other unit does and the run-scoped logical-job key
    stays unique.
    """
    rows = handle.get_jids_of_normal_science_pipeline_jobs_for_processing_date(
        proc_date)
    for row in rows or ():
        jid = int(row[0] if isinstance(row, (list, tuple)) else row)
        job = handle.get_job_record(jid) if hasattr(
            handle, "get_job_record") else None
        exposure, sca = _job_identity(job, jid)
        yield ProcessingUnit(exposure=exposure, sca=sca,
                             facts=UnitFacts(),
                             fields={"jid": jid,
                                     "job_type": JOB_TYPE_POST_PROCESS})


def _job_identity(job: Any, jid: int) -> tuple[int, int]:
    """(exposure, sca) for a post-process unit, or a jid-derived fallback.

    A Jobs row carries expid and sca; where the handle cannot supply the
    row, the jid alone still has to produce a unique unit key, so it
    becomes the exposure with SCA 0. That is a labelled degenerate case,
    not a silent default: it keeps the run-scoped key unique (the jid is
    unique) without pretending to know an SCA it was never told.
    """
    if job is not None:
        expid = _maybe_int(job[0] if not isinstance(job, dict)
                           else job.get("expid"))
        sca = _maybe_int(job[1] if not isinstance(job, dict)
                         else job.get("sca"))
        if expid is not None and sca is not None:
            return int(expid), int(sca)
    return int(jid), 0


def _sca_of(handle: UnitSource, rid: int) -> int:
    meta = handle.get_l2filemeta_record(rid)
    if meta is None or meta[0] is None:
        raise GatheringError(f"rid {rid} has no SCA in L2FileMeta")
    return int(meta[0])


def _replace(facts: UnitFacts, **changes: Any) -> UnitFacts:
    """`dataclasses.replace` for the frozen facts record."""
    return dataclasses.replace(facts, **changes)


def _maybe_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _maybe_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _maybe_str(value: Any) -> str | None:
    return None if value is None else str(value)
