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

import base64
import dataclasses
import datetime
import hashlib
import logging
from typing import Any, Iterable, Iterator, Protocol, Sequence

from database.modules.utils.checked import RapidDBCallFailed
from pipeline.repositories.association import AssociationRepository
from pipeline.repositories.data_class import DataClassRepository
from pipeline.repositories.errors import RepositoryQueryFailed
from database.modules.utils.roman_tessellation_db import (
    RomanTessellationClosedForm)
from . import blocked
from . import data_class as data_class_rules
from . import payloads
from .manifest import ProcessingUnit
from .routes import (JOB_TYPE_ALERT_PRODUCTION, JOB_TYPE_CATALOG_LOAD,
                     JOB_TYPE_CROSSMATCH, JOB_TYPE_MERGE_CURRENCY,
                     JOB_TYPE_MERGE_DEDUP, JOB_TYPE_REFERENCE_IMAGE,
                     JOB_TYPE_SCIENCE, JOB_TYPE_SOURCE_CURRENCY,
                     JOB_TYPE_STATISTICS, ppid_for)
from .subjects import SubjectError, parse_exposure_sca_scope
from .submit import is_precondition_failed

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


class NotReadyYet(GatheringError):
    """This field cannot support the product asked of it — yet.

    The other half of the distinction `GatheringError`'s own docstring
    names. One class carrying both meanings was enough while nothing
    caught it, but `gather_reference_units` does catch it, and it caught
    the pair: a field with two frames instead of three and a database
    that had stopped answering both arrived as `GatheringError` and were
    both logged "no reference image for this unit yet" at INFO. The
    survey then ran a night with zero reference images and no ERROR
    anywhere in the log.

    Only a shortfall of ready inputs is this. Everything a query does
    wrong stays a bare `GatheringError`, so the catch can be narrowed to
    the one case that is genuinely ordinary and the other case travels.
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

    # The batched form (2026-08-14, closing `coadd_input_rows`'s N+1): one
    # round trip for every rid in the overlap set instead of one per rid.
    # Returns a dict keyed by rid; a rid with no L2Files row is simply
    # absent, the batched equivalent of the singular method's `None`.
    def get_info_for_l2files(
            self, rids: Sequence[int]) -> dict[int, Sequence[Any]]: ...

    def get_exposure_filter(self, fid: int) -> Any: ...

    def get_best_psf(self, sca: int, fid: int) -> Sequence[Any]: ...

    def get_best_reference_image(self, ppid: int, field: int,
                                 fid: int) -> Any: ...

    # `rid` is typed loosely because it is not a rid in the caller that
    # matters: `_overlapping_l2files` passes the string 'null' to select the
    # branch of the query that excludes no row. See that function.
    def get_overlapping_l2files(self, rid: Any, fid: int, mjdobs: float,
                                *corners: float,
                                radius_of_initial_cone_search: float | None
                                = ...,
                                start_mjdobs: float | None = ...,
                                end_mjdobs: float | None = ...
                                ) -> Sequence[Any]: ...

    # The post-DB science chain's three enumerations (step-3 conversion).
    def get_scas_with_science_jobs_for_processing_date(
            self, proc_date: str) -> Sequence[Any]: ...

    def get_fields_with_science_jobs_for_processing_date(
            self, proc_date: str) -> Sequence[Any]: ...

    def get_fields_with_per_field_table(
            self, prototype: str) -> Sequence[Any]: ...

    # The loader's re-source: the registered products a (date, SCA) unit
    # loads, each carrying the attempt-scoped URI its catalogue sits beside.
    def get_registered_diffimages_for_processing_date_sca(
            self, proc_date: str, sca: int) -> Sequence[Any]: ...

    # The durable-state ordering predicate crossmatch (and alert production,
    # indirectly through the same fact class) gate on — co-design ruling 1.
    def get_scas_with_incomplete_catalog_load_for_processing_date(
            self, proc_date: str) -> Sequence[Any]: ...

    # The complement of the above, over the same rows (2026-08-14): the
    # SCAs whose catalog load for this date IS complete, which is what
    # names the `sources_<proc_date>_<sca>` tables `gather_crossmatch_units`
    # declares as `CrossmatchPayload.source_tables`.
    def get_scas_with_completed_catalog_load_for_processing_date(
            self, proc_date: str) -> Sequence[Any]: ...

    # The resubmission gates (mission mock, live 2026-08-09): gather sets
    # and exclusion sets over pending-or-successful attempts, so a
    # state-blind enumeration cannot resubmit a subject every accumulator
    # cut. Failed attempts free the subject — retry by re-gathering.
    def get_scas_with_gatherable_catalog_load_for_processing_date(
            self, proc_date: str) -> Sequence[Any]: ...

    def get_fields_with_blocking_crossmatch_attempt_for_processing_date(
            self, proc_date: str) -> Sequence[Any]: ...

    def get_fields_with_blocking_attempt_for_job_type_since(
            self, job_type: str, since: Any) -> Sequence[Any]: ...

    # THE ORDERING GATE'S TWO READS ARE DELIBERATELY ABSENT FROM THIS
    # PROTOCOL. `RAPIDDB` is frozen (brief G's ratified merge decision), so
    # the association watermark and the earliest-owed date are read through
    # `pipeline.repositories.association.AssociationRepository` over the
    # handle's connection instead of through new handle methods — see
    # `_association_repository`. What this protocol declares is what gathering
    # asks the LEGACY handle for, and that set does not grow.

    def get_blocking_exposure_scas_for_job_type(
            self, job_type: str, expids: Sequence[int]) -> Sequence[Any]: ...

    # The alert-production trigger (step-4 co-design).
    def get_attempts_awaiting_alert_emission(
            self, release_identity: str,
            limit: int | None = ...) -> Sequence[Any]: ...

    # Watermark SEEDING only (migration 037 / integration ruling 3): the
    # live CAS claim/confirm path is a different pair of methods
    # (`claim_alert_emission` / `confirm_alert_emission`) that
    # `pipeline.stages.alert_production` calls directly through
    # `RAPIDDB.borrowing(...)`, not through this gathering-time protocol —
    # gathering only ever SEEDS the watermark, at `initialize_alert_
    # watermark`, so this is the one method of the pair this protocol
    # declares.
    def seed_alert_emission_watermark(self, exposure_id: int, sca: int,
                                      release_identity: str,
                                      attempt_id: int,
                                      pid: int | None = ...) -> bool: ...

    # The campaign gatherer's one enumeration (IR-13-a): every READY
    # work_unit of every ACTIVE test-class campaign, one joined query
    # rather than "list active campaigns" then "list ready units per
    # campaign" — a campaign has no scale problem this v1 needs to guard
    # (mission-mock campaigns are staged by `create_mock_campaign_from_
    # staged`'s own `max_units` guard), so the two-query round trip buys
    # nothing a join does not already give in one.
    def get_ready_test_campaign_units(self) -> Sequence[Any]: ...

    # The campaign gatherer's second lookup: one work unit's source L2
    # identity (rid, field, fid), recorded in that unit's creation detail
    # by `create_mock_campaign_from_staged` — see
    # `_campaign_unit_l2_identity`'s own docstring for why this is a
    # detail-keyed read rather than a reverse exposure/SCA -> rid query.
    def get_campaign_unit_source_l2_identity(
            self, work_unit_id: int) -> Sequence[Any]: ...


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


def _tile_position(rtid: int) -> dict[str, float] | None:
    """The tessellation tile's centre and corners, closed-form.

    The deleted reference-image launcher took these from
    `roman_tessellation_db` per field, immediately before computing the
    coadd's mosaic centre from `ra0`/`dec0`
    (`e03f22c^:pipeline/awsBatchSubmitJobs_launchSingleReferenceImagePipeline.py`
    lines 352-364). `UnitFacts.tile_position` has always declared the fact;
    nothing populated it, so the mosaic centre had no source and every
    reference-image attempt died `internal_error` on the missing
    `awaicgen_RA_center`.

    The closed form opens no connection and does no I/O, so this adds no
    per-unit query — the concern that retired the per-unit R-tree lookup in
    W7 does not apply.
    """
    tessellation = RomanTessellationClosedForm()
    tessellation.get_center_sky_position(int(rtid))
    tessellation.get_corner_sky_positions(int(rtid))
    return _positions(
        [tessellation.ra0, tessellation.ra1, tessellation.ra2,
         tessellation.ra3, tessellation.ra4,
         tessellation.dec0, tessellation.dec1, tessellation.dec2,
         tessellation.dec3, tessellation.dec4],
        ["ra0", "ra1", "ra2", "ra3", "ra4"],
        ["dec0", "dec1", "dec2", "dec3", "dec4"])


def science_facts(handle: UnitSource, rid: int, field: int, fid: int,
                  reference_ppid: int | None = None,
                  science_ppid: int | None = None) -> dict:
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

    # `get_l2filemeta_record`/`get_info_for_l2file` used to be read only for
    # `None` — a failed query (nonzero exit_code, `None` returned) and a
    # genuinely absent row looked identical. The adapter now raises
    # `RapidDBCallFailed` for the former, so `None` here means only "no
    # such row" (or the harmless code-7 not-found convention passed
    # through clean — see `database.modules.utils.checked`).
    try:
        meta = handle.get_l2filemeta_record(rid)
    except RapidDBCallFailed as exc:
        raise GatheringError(
            f"L2FileMeta lookup failed for rid {rid}: {exc}") from exc
    if meta is None or len(meta) < 12:
        raise GatheringError(
            f"rid {rid} has no L2FileMeta row; it cannot be positioned on "
            "the sky and no science unit can be built from it")
    sca = int(meta[0])

    try:
        info = handle.get_info_for_l2file(rid)
    except RapidDBCallFailed as exc:
        raise GatheringError(
            f"L2Files lookup failed for rid {rid}: {exc}") from exc
    # (filename, expid, sca, field, mjdobs, exptime, infobits, status,
    #  vbest, version) — the order get_info_for_l2file selects in.
    if info is None or len(info) < 8:
        raise GatheringError(f"rid {rid} has no L2Files row")

    facts = dict(
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
        # `status` was resolved here and carried as a `UnitFacts` member that
        # NOTHING ever read (survey of every accessor, D4). Dropped rather
        # than moved onto the payload: carrying a fact no consumer wants is
        # how an all-optional carrier grows in the first place.
        science_image_uri=_maybe_str(info[0]),
        sky_position=_sky_position(meta),
        tile_position=_tile_position(int(field)),
    )

    try:
        filter_name = handle.get_exposure_filter(fid)
    except RapidDBCallFailed as exc:
        raise GatheringError(
            f"filter lookup failed for fid {fid}: {exc}") from exc
    if filter_name is not None:
        facts["filter_name"] = str(filter_name)

    try:
        psf = handle.get_best_psf(sca, fid)
    except RapidDBCallFailed as exc:
        raise GatheringError(
            f"PSF lookup failed for sca {sca} fid {fid}: {exc}") from exc
    if psf is not None and len(psf) >= 2 and psf[0] is not None:
        facts["psfid"] = _maybe_int(psf[0])
        facts["psf_uri"] = _maybe_str(psf[1])

    reference = _best_reference(handle, reference_ppid, science_ppid,
                                field, fid)
    if reference is not None:
        facts["reference_image_id"] = _maybe_int(reference.get("rfid"))
        facts["reference_image_uri"] = _maybe_str(reference.get("filename"))
        facts["reference_image_infobits"] = _maybe_int(
            reference.get("infobits"))
        facts["reference_image_ppid"] = _maybe_int(reference.get("ppid"))

    # THE DATA CLASS IS INHERITED HERE, at the one place both product-
    # producing gatherers pass through. Science and reference-image are
    # exactly the job types that mint object keys
    # (`submission.subjects.is_product_producing`), and this function
    # resolves the facts for both — so the class lands on every unit whose
    # products need a leading key component, and on no unit whose products
    # do not exist.
    #
    # The reference image is deliberately NOT folded into the input set,
    # even though this unit reads one. The unit's identity is its L2 input;
    # the reference is a resolved dependency, and a science unit does not
    # become validation data because it differenced against a reference
    # built from injected pixels — that reference has its own class on its
    # own products. Widening the input set to dependencies would make the
    # rule transitive over the whole build graph, which the design does not
    # say and which would eventually classify everything as the least
    # eligible thing it ever touched.
    data_class = _data_class_for_inputs(handle, [rid])
    if data_class is not None:
        facts["data_class"] = data_class
    return facts


def _data_class_repository(handle):
    """A `DataClassRepository` over the handle's connection, or `None`.

    **THE CARVED PATH, NOT A `RAPIDDB` METHOD**, and this function exists
    because the first revision of this change got that wrong: it added
    `get_data_classes_for_l2files` straight to the frozen `RAPIDDB` and was
    refused by `pipeline/contract/test_deletion_exclusivity.py`, whose own
    message records that the D, F and E workers each made the same mistake.
    Same seam as `_association_repository` above, for the same reason and by
    the same mechanism: `RAPIDDB.conn` is an attribute, and `CheckedHandle`
    passes non-callables through unchecked, so no new method is added to the
    frozen class and none is needed.

    Returns `None` when the handle exposes no usable connection — the
    operator probes and the gathering stubs. Those callers inherit no class,
    which is the same answer an input with no admission manifest gives, and
    is correct for them: a stub has no admission rows to read.

    A test double may inject `data_class_repository` directly, so the stub
    tier can exercise the inheritance without a database while the contract
    tier executes the SQL — the division the stub-blind rule draws.
    """
    injected = getattr(handle, "data_class_repository", None)
    if injected is not None:
        return injected

    conn = getattr(handle, "conn", None)
    if conn is None:
        return None
    return DataClassRepository(conn)


def _data_class_for_inputs(handle: UnitSource,
                           rids: Sequence[int]) -> str | None:
    """The class a unit built from these L2 inputs inherits.

    Absent — None — rather than defaulted when nothing knows: an L2 file
    admitted before migration 090, or registered by a legacy path with no
    admission row, carries no class, and inventing one would file real
    objects under a prefix chosen by this function rather than by the data.
    The builder's fallback to the deployment-wide parameter is what serves
    those units, and it is a deliberate, documented path — unlike a guess
    made here, which would be indistinguishable from knowledge.

    A FAILED QUERY IS NOT AN ABSENT CLASS. `RepositoryQueryFailed` becomes a
    named `GatheringError` rather than an empty result, because the two are
    indistinguishable downstream: both would resolve to "no class", and a
    transient failure would then file a unit's products under the fallback
    class instead of its own. This is the failure mode the repository
    pattern exists to prevent, and the reason this read must not go through
    a handle method that reports failure by returning None.
    """
    repository = _data_class_repository(handle)
    if repository is None:
        return None
    try:
        recorded = repository.classes_for_l2files(list(rids))
    except RepositoryQueryFailed as exc:
        raise GatheringError(
            f"data-class lookup failed for rids {list(rids)}: {exc}") from exc
    if not recorded:
        return None
    # `most_restrictive` refuses an unregistered token rather than passing it
    # through. That refusal is wanted here: the column is CHECK-constrained
    # by 090, so a value outside the registry means the constraint was
    # dropped or bypassed, and building an object key from it would file
    # bytes where nothing looks.
    return data_class_rules.most_restrictive(recorded)


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
        # exit_code 7 is the documented "no reference yet" signal, and the
        # adapter passes it through as a clean (empty) result rather than
        # raising — see `database.modules.utils.checked`. Any other query
        # failure raises `RapidDBCallFailed` here, and is not caught: it
        # must not read as "no reference".
        try:
            record = handle.get_best_reference_image(ppid, field, fid)
        except RapidDBCallFailed as exc:
            raise GatheringError(
                f"reference lookup failed for field {field} fid {fid} "
                f"under ppid {ppid}: {exc}") from exc
        if record:
            found = dict(record)
            found.setdefault("ppid", ppid)
            return found
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

    candidates: list[ProcessingUnit] = []
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
            exposure = facts.get("expid")
            if exposure is None:
                raise GatheringError(
                    f"rid {rid} has no exposure id; a processing unit is "
                    "keyed by exposure/SCA and cannot be built without one")
            sca = _sca_of(handle, rid)
            job_type_for_payload = (JOB_TYPE_REFERENCE_IMAGE
                                    if make_references else JOB_TYPE_SCIENCE)
            candidates.append(ProcessingUnit(
                payload=payloads.build(job_type_for_payload,
                                       exposure=int(exposure), sca=int(sca),
                                       **facts)))

    # THE RESUBMISSION GATE (final convergence round, 2026-08-09): the two
    # EXPOSURE_SCA arrival-driven types were the last state-blind
    # enumerations — a fixed window re-yielded every unit each poll for the
    # whole flight of its first attempt and forever after success. Same
    # predicate as every other gate: pending-or-success blocks, failure
    # frees (retry is re-gathering). Scoped to the enumerated exposures.
    if candidates:
        job_type = (JOB_TYPE_REFERENCE_IMAGE if make_references
                    else JOB_TYPE_SCIENCE)
        try:
            blocked_rows = handle.get_blocking_exposure_scas_for_job_type(
                job_type, sorted({u.exposure for u in candidates}))
        except RapidDBCallFailed as exc:
            raise GatheringError(
                f"blocking-attempt check failed for job type {job_type}: "
                f"{exc}") from exc
        blocked = {(int(r[0]), int(r[1])) for r in blocked_rows or ()}
        for unit in candidates:
            if (unit.exposure, unit.sca) in blocked:
                continue
            yield unit


# ---------------------------------------------------------------------------
# The campaign-unit gatherer (integration review, IR-13-a): lets a
# test-class mission-mock campaign flow through the DEPLOYED operator /
# accumulator / submission path, unmodified downstream of gathering.
# ---------------------------------------------------------------------------
#
# **THE SUPERVISOR RULING THIS IMPLEMENTS**, quoted from the run ledger: "a
# campaign-unit gatherer: a registry row under the test operational class
# whose gathering enumerates ready test-class work_units of ACTIVE campaigns
# and yields production-shaped ProcessingUnits under the campaign's declared
# route." And the v1 restriction: "test campaigns declare the SCIENCE route
# (assert at campaign creation and at gathering; a non-science test campaign
# is refused loudly)."
#
# **THE W2-FALLBACK SUBSTRATE.** Campaign work units are created FROM
# already-registered simulation input rows (the l2files/l2filemeta g0001
# slice `pipeline.mock.transformer.create_mock_campaign_from_staged`
# enumerates), so every unit this gatherer yields is exactly science-shaped
# — same `UnitFacts`, same `science_facts` call, same reference-image
# resolution — and the whole downstream chain (Batch science attempt,
# reconciler, registration, catalog load, alert production) runs
# unmodified. This gatherer's ONLY job is: find the ready campaign units,
# recover their (exposure, sca) identity, and hand them to `science_facts`
# exactly as `gather_science_units` does for an arrival-driven unit.
#
# **ROUTE RESTRICTION, ENFORCED HERE TOO.** `create_mock_campaign_from_
# staged` asserts SCIENCE at campaign CREATION time (the other half of "at
# campaign creation and at gathering"); this function re-asserts it at
# GATHER time against the `job_type` the campaign's work units actually
# carry, because a unit could in principle reach this gatherer's query
# having been created by a path that did not go through that assertion —
# refusing here is the second, independent guard the ruling's parenthetical
# calls for, not a redundant one.
def gather_campaign_units(handle: UnitSource) -> Iterator[ProcessingUnit]:
    """Yield science-shaped units for every READY unit of every ACTIVE test
    campaign.

    One query (`get_ready_test_campaign_units`) enumerates every ready
    work_unit belonging to an active, test-operational-class campaign —
    "enumerates ready test-class work_units of ACTIVE campaigns" exactly.
    Each row's `input_scope` is parsed back to `(exposure, sca)` via
    `submission.subjects.parse_exposure_sca_scope` — the SAME grammar
    `pipeline.seams._input_scope_for` (now `submission.subjects.
    build_input_scope`) used to build it at creation time, so the unit this
    function yields and the unit `pipeline.seams._attach_work_unit` later
    FINDS (rather than re-creates) agree on identity by construction. See
    that module's grammar section for why one shared helper, not two.

    A unit whose backing L2 rows are missing (the campaign work unit cites
    an (exposure, sca) that `science_facts` cannot resolve) RAISES rather
    than being silently skipped — refusal, not skip, per the stub-blind-
    testing rule this build follows: a campaign unit is a promise that its
    backing rows exist (the mock transformer only ever creates units over
    ALREADY-REGISTERED rows), and a promise the database cannot make good
    on is a real defect, not an ordinary "not ready yet".

    Yields `ProcessingUnit` built through the exact same `science_facts`
    call `gather_science_units` makes for the same `(exposure, sca)` — so a
    campaign unit and an arrival-driven unit over the same L2 row are
    indistinguishable downstream of gathering. The unit's job type now rides
    in its typed payload (`submission.payloads`), which every unit carries —
    a campaign unit builds a `SciencePayload` exactly as a plain science
    unit does, so the two are the same shape as well as the same grain.
    """
    try:
        rows = handle.get_ready_test_campaign_units()
    except RapidDBCallFailed as exc:
        raise GatheringError(
            f"campaign-unit enumeration failed: {exc}") from exc

    for row in rows or ():
        # (work_unit_id, campaign_id, campaign_name, job_type, input_scope)
        # — this query's column order (database.modules.utils.rapid_db.
        # RAPIDDB.get_ready_test_campaign_units).
        work_unit_id, campaign_id, campaign_name, job_type, input_scope = (
            row[0], row[1], row[2], row[3], row[4])

        # THE V1 ROUTE RESTRICTION, re-asserted at gather time (see this
        # section's header): a test campaign whose work units do not carry
        # job_type == science is refused loudly, never silently skipped —
        # a non-science test campaign reaching this far is a defect in the
        # creation-time assertion, and gathering must not paper over it.
        if job_type != JOB_TYPE_SCIENCE:
            raise GatheringError(
                f"campaign {campaign_name!r} (id={campaign_id}) work unit "
                f"{work_unit_id} declares job_type={job_type!r}, not "
                f"{JOB_TYPE_SCIENCE!r}; v1 test campaigns are restricted to "
                f"the science route and this must be refused, not "
                f"gathered")

        try:
            exposure, sca = parse_exposure_sca_scope(input_scope)
        except SubjectError as exc:
            raise GatheringError(
                f"campaign {campaign_name!r} (id={campaign_id}) work unit "
                f"{work_unit_id} has an unparseable input_scope "
                f"{input_scope!r}: {exc}") from exc

        # THE REFUSAL: a campaign unit's backing L2 row must exist, because
        # the mock transformer only ever creates campaign units over
        # ALREADY-REGISTERED rows (create_mock_campaign_from_staged's own
        # docstring). `science_facts` raises `GatheringError` on a missing
        # L2FileMeta/L2Files row already (see that function) — not caught
        # here, so it propagates as the loud refusal the ruling calls for
        # rather than a skip. `field`/`fid` are read off the row itself
        # (get_info_for_l2file's own return, inside science_facts) rather
        # than re-derived here, matching gather_science_units's own call
        # shape exactly.
        rid, field, fid = _campaign_unit_l2_identity(
            handle, exposure, sca, work_unit_id, campaign_name)
        facts = science_facts(handle, rid, field, fid)

        logger.info(
            "campaign %s (id=%s): gathered work unit %s as %s/%s",
            campaign_name, campaign_id, work_unit_id, exposure, sca)
        yield ProcessingUnit(
            payload=payloads.build(job_type, exposure=int(exposure),
                                   sca=int(sca), **facts))


def _campaign_unit_l2_identity(handle: UnitSource, exposure: int, sca: int,
                               work_unit_id: int, campaign_name: str
                               ) -> tuple[int, int, int]:
    """The (rid, field, fid) `science_facts` needs, for one campaign unit's
    (exposure, sca).

    Campaign work units carry no `rid` directly — `input_scope` names
    `(exposure, sca)`, the SAME identity `gather_science_units` resolves
    through the (field, filter) -> L2Files loop. Rather than a second,
    parallel L2-row lookup keyed by exposure/SCA (a query this repo's
    `rapid_db` does not currently offer, since nothing before this gatherer
    needed to go from exposure/SCA back to `rid` — every existing caller
    already had `rid` in hand from the (field, filter) loop), this reuses
    `_sca_of`'s sibling read: `get_l2filemeta_record` is keyed by `rid`, not
    exposure/SCA, so campaign units instead thread `rid` through
    `unit_events.detail`/`work_units` at CREATE time — see
    `pipeline.mock.transformer.create_mock_campaign_from_staged`, which
    stores the source L2 row's `rid` in exactly the same `detail` shape
    `create_mock_campaign` already uses for `generation_id`/`manifest_key`.

    Raises
    ------
    GatheringError
        No `rid`/`field`/`fid` was recorded for this campaign unit, or the
        recorded `rid`'s L2FileMeta row is missing — either is a defect in
        the campaign's own creation-time bookkeeping, not an unready state.
    """
    try:
        detail = handle.get_campaign_unit_source_l2_identity(work_unit_id)
    except RapidDBCallFailed as exc:
        raise GatheringError(
            f"campaign {campaign_name!r} work unit {work_unit_id}: could "
            f"not read its source L2 identity: {exc}") from exc
    if not detail or detail[0] is None:
        raise GatheringError(
            f"campaign {campaign_name!r} work unit {work_unit_id} "
            f"({exposure}/{sca}) carries no source L2 rid in its creation "
            f"detail; a campaign unit must be created from an already-"
            f"registered L2 row (create_mock_campaign_from_staged) and "
            f"this one was not, or its detail was not recorded")
    rid, field, fid = (int(detail[0]), int(detail[1]), int(detail[2]))
    return rid, field, fid


# The CSV column order `generateReferenceImage` parses, taken from the deleted
# launcher (awsBatchSubmitJobs_launchSingleReferenceImagePipeline.py, at the
# commit before the W6 cutover fence). It is a positional format with no
# header, so the order IS the contract and is written down here rather than
# left implicit in a join expression.
COADD_INPUT_COLUMNS = (
    "rid", "ra0", "dec0", "ra1", "dec1", "ra2", "dec2", "ra3", "dec3",
    "ra4", "dec4", "filename", "expid", "sca", "field", "mjdobs", "exptime",
    "infobits", "status", "vbest", "version",
)

# The two sentinels the deleted launcher passed into `get_overlapping_l2files`,
# named here because they are the query's semantics and not placeholder values
# somebody forgot to fill in. See `_overlapping_l2files` for why each is what
# the reference stage wants.
#
# MJD 999999.9 is 4692-05-11; the query's window is `[start, mjdobs)` and the
# launcher's own comment says this is how you ask for "everything ever
# observed", the start being 0.0 in the same branch.
REFERENCE_OVERLAP_OPEN_MJDOBS = 999999.9
#: No representative exclusion: None, which `rapid_db` renders as the ABSENCE
#: of an exclusion clause. It was the string 'null' until round-4 finding #3
#: — a sentinel that selected a `a.rid is not %s` branch which, once the query
#: was parameterized, sent PostgreSQL the invalid `a.rid IS NOT 'null'` and
#: failed the whole overlap query with exit_code 67.
REFERENCE_OVERLAP_NO_EXCLUSION = None


def reference_observation_window(override=None) -> tuple[float, float]:
    """The reference image's observation window: override, else release.

    Two homes and no third. The authoritative value is release content
    (`[ref_image]` in `cdf/science/pipeline.toml`); the submission
    manifest's `reference_observation_window` is the sole per-run override
    carrier (design/compute.md § Job definitions). The environment is
    neither: `STARTREFIMMJDOBS`/`ENDREFIMMJDOBS` selected which frames
    entered a science product and are deleted, on "nothing that can alter a
    science product is reachable from the environment".

    Returns the half-open `[start, end)` pair the overlap query binds.
    """
    if override is not None:
        return (float(override.start_mjdobs), float(override.end_mjdobs))

    from pipeline.runtime import science_config

    ref_image = science_config.section(science_config.load(), "ref_image")
    missing = [key for key in ("start_refimage_mjdobs", "end_refimage_mjdobs")
               if ref_image.get(key) is None]
    if missing:
        raise GatheringError(
            "release content does not carry the reference observation "
            "window; missing [ref_image] " + ", ".join(missing)
            + ". It is release content and has no default here — the "
            "environment path that used to supply it is deleted.")
    return (float(ref_image["start_refimage_mjdobs"]),
            float(ref_image["end_refimage_mjdobs"]))


def _overlapping_l2files(handle: UnitSource, rid: int, fid: int,
                         corners: Sequence[Any],
                         radius: float | None,
                         window: tuple[float, float] | None = None
                         ) -> Sequence[Any]:
    """`get_overlapping_l2files` asked the way the reference stage means it.

    Two defects are closed here, and both are the same mistake: the legacy
    method's arguments were read as descriptions of the representative image
    when they are in fact controls on the query, and passing the
    representative's own values turned each control on.

    **The window.** `rid`/`fid`/`mjdobs` are not "the image we are building
    around". `mjdobs` is the EXCLUSIVE upper bound of the query's own MJD
    window when no window is passed. Handing it the representative's
    `mjdobs` asks for "frames
    observed strictly before the representative" — and the representative is
    `rows[0]`, the EARLIEST frame in time order, so that window is empty by
    construction. Every field returned zero coadd inputs, every reference
    unit was logged "not ready yet", and no reference image was ever built.
    The launcher passed 999999.9 for exactly this reason and said so in a
    comment. So do we — but the window is now passed EXPLICITLY, as
    `window`, and 999999.9 survives only as release content's default upper
    bound. There is no longer an environment override winning inside
    `rapid_db`: that path is deleted, and the two homes are release content
    and the submission manifest's enumerated override
    (`reference_observation_window`).

    **The exclusion.** The tail parameter renders as `a.rid != %s` for a real
    rid — which drops the representative from its own coadd. It is an input
    to the coadd like any other frame, so with N frames available the query
    returned N-1 and a field with exactly `min_images_to_coadd` frames was
    skipped forever. The reference stage wants NO exclusion, and asks for it
    with `REFERENCE_OVERLAP_NO_EXCLUSION` — None, which `rapid_db` renders as
    no exclusion clause at all.

    That sentinel was the string 'null' until round-4 finding #3, chosen
    because the launcher passed it and the branch it selected —
    `a.rid is not %s` — excluded nothing. It excluded nothing by accident: as
    a literal substitution it read `IS NOT null`, a type predicate that is
    true for every row of an integer column. Once the query was parameterized
    the string was bound through the placeholder instead, PostgreSQL received
    `a.rid IS NOT 'null'`, and the query failed outright — exit_code 67, no
    coadd inputs, and (before the exit-code check below) reported as an
    unready field. "Exclude nothing" is now the absence of a predicate, which
    is what it means and cannot be broken by how the value is bound.

    **The exit code.** `get_overlapping_l2files` reports a failed query by
    setting `exit_code = 67` and returning None — silently, no exception. The
    launcher checked `>= 64` after every such call and exited; gathering did
    not, so None flowed into `overlapping or ()`, iterated as nothing, and the
    short-row check downstream reported a database outage as "this field is
    not ready". Checked here, as `_best_reference` and the `get_info_for_l2file`
    call below already do, and raised as a bare `GatheringError` so it is NOT
    the `NotReadyYet` that `gather_reference_units` swallows.
    """
    start_mjdobs, end_mjdobs = (window if window is not None
                                else reference_observation_window())
    try:
        overlapping = handle.get_overlapping_l2files(
            REFERENCE_OVERLAP_NO_EXCLUSION, fid,
            REFERENCE_OVERLAP_OPEN_MJDOBS, *corners,
            radius_of_initial_cone_search=radius,
            start_mjdobs=start_mjdobs, end_mjdobs=end_mjdobs)
    except RapidDBCallFailed as exc:
        # This is a query failure, not an unready field, and must not be
        # reported as one — hence `GatheringError`, not `NotReadyYet`.
        raise GatheringError(
            f"overlap query failed for rid {rid} fid {fid}: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise GatheringError(
            f"overlap query failed for rid {rid} fid {fid}: {exc}") from exc

    return overlapping or ()


def coadd_input_rows(handle: UnitSource, rid: int, fid: int, mjdobs: float,
                     sky_position: dict, min_images_to_coadd: int,
                     radius: float | None = None,
                     window: tuple[float, float] | None = None
                     ) -> list[list[Any]]:
    """The reference image's coadd inputs, as CSV rows.

    The launcher's aggregation, preserved because it is science logic: every
    L2 file overlapping this one's sky tile in the same filter, ordered by
    distance from tile centre — then filtered to the rows that may actually
    be coadded.

    The two exclusions are the launcher's and are not optional. `status == 0`
    is a file marked bad; `vbest == 0` is a superseded version. Coadding
    either would build a reference from images the database says not to use.

    `mjdobs` is taken but NOT forwarded to the overlap query, which is the
    fix rather than an oversight: it names the representative image for the
    error messages, while the query itself runs over the open window
    `_overlapping_l2files` documents. Passing it through was what made every
    field look unready.

    Raises `NotReadyYet` rather than returning a short list when there are
    too few inputs, and a bare `GatheringError` when a query failed: "this
    field cannot support a reference image yet" and "the query failed" must
    not look the same to the submitter, which is the whole reason the
    submitter catches only the first.
    """
    corners = [sky_position.get(key) for key in
               ("ra0", "dec0", "ra1", "dec1", "ra2", "dec2", "ra3", "dec3",
                "ra4", "dec4")]
    if any(value is None for value in corners):
        raise GatheringError(
            f"rid {rid} has no complete sky position; the overlap query is a "
            f"cone search about the tile corners and cannot run without them")

    overlapping = _overlapping_l2files(handle, rid, fid, corners, radius,
                                       window=window)

    # BATCHED, NOT ONE `get_info_for_l2file` CALL PER OVERLAPPING IMAGE
    # (2026-08-14, closing the N+1). One round trip for every input rid in
    # the overlap set, before the loop below reads from it — the loop's
    # per-row semantics (the `info is None` skip, the status/vbest filter,
    # the field-agreement check, ordering) are unchanged; only how `info`
    # is obtained changed, from a query per row to a dict lookup per row.
    input_rids = [int(image[0]) for image in overlapping]
    try:
        info_by_rid = handle.get_info_for_l2files(input_rids)
    except RapidDBCallFailed as exc:
        raise GatheringError(
            f"get_info_for_l2files failed for rids {input_rids}: "
            f"{exc}") from exc

    rows: list[list[Any]] = []
    for image in overlapping:
        input_rid = int(image[0])
        field_from_overlap = image[11]
        info = info_by_rid.get(input_rid)
        if info is None:
            continue

        (filename, expid, sca, field, image_mjdobs, exptime,
         infobits, status, vbest, version) = info[:10]

        if not status or not vbest:
            continue

        # The launcher's sanity check, kept: the overlap query and the file's
        # own row must agree about which field this is. They disagreeing means
        # one of the two is describing a different image, and coadding on that
        # basis would build a reference from the wrong sky.
        if field is not None and field_from_overlap is not None \
                and int(field) != int(field_from_overlap):
            raise GatheringError(
                f"rid {input_rid} is field {field} in L2Files but field "
                f"{field_from_overlap} in the overlap query; refusing to "
                f"coadd on disagreeing identities")

        rows.append([input_rid, *image[1:11], filename, expid, sca, field,
                     image_mjdobs, exptime, infobits, status, vbest, version])

    if len(rows) < min_images_to_coadd:
        # `NotReadyYet`, specifically: the query ran and answered, the sky
        # simply has not been visited enough times. With the window and the
        # exclusion both fixed the representative counts itself, so a field
        # holding exactly `min_images_to_coadd` frames now passes here — it
        # used to arrive one short and be skipped for the life of the survey.
        raise NotReadyYet(
            f"rid {rid} has {len(rows)} coaddable inputs, fewer than the "
            f"{min_images_to_coadd} the release requires for a reference "
            f"image")
    return rows


#: The positions of the mission-identity columns in a `coadd_input_rows` row.
#: The row is built as `[input_rid, *image[1:11], filename, expid, sca, field,
#: mjdobs, exptime, infobits, status, vbest, version]`, so these three are
#: fixed by that construction and named here rather than repeated as literals
#: at the read site — a column inserted into that list must move these
#: together with it, and one place to change is the whole point.
_COADD_EXPID_COLUMN = 12
_COADD_SCA_COLUMN = 13
_COADD_INFOBITS_COLUMN = 17


def coadd_input_identities(rows: Iterable[Sequence[Any]]) -> list:
    """The coadd inputs' MISSION identities: `[[expid, sca, infobits], ...]`.

    What a reference image's product key digests its inputs as (rule 10).
    Deliberately NOT the CSV's checksum and NOT `input_rid`/`filename`: the
    first hides a path-and-surrogate dependency behind a digest, and the
    other two are exactly the forbidden identity sources — a database
    surrogate and a storage path.

    `infobits` is included because it is a property of the input file's
    CONTENT (its quality mask), so two L2 files sharing an `(expid, sca)`
    but differing in quality are genuinely different inputs to a coadd.

    Returned as lists rather than tuples because this value is serialized
    into the manifest as JSON, where a tuple round-trips as a list anyway —
    returning what survives the round trip means the value compares equal
    to itself after a manifest write and read, which a tuple would not.

    Order is NOT fixed here. `coadd_input_rows` returns the overlap query's
    order (`order by dist`, which has no tie-breaker), and the canonical
    total order is imposed where identity is computed
    (`pipeline.registration.identity.ordered_science_inputs`) so that one
    definition governs. Sorting here as well would be a second place for the
    order to be defined, and two definitions of a canonical order is one too
    many.
    """
    identities = []
    for row in rows:
        expid = row[_COADD_EXPID_COLUMN]
        sca = row[_COADD_SCA_COLUMN]
        if expid is None or sca is None:
            # A coadd input with no mission identity cannot enter a product
            # key, and silently dropping it would change the product's
            # identity to one computed over fewer inputs than were actually
            # coadded — a key that claims a provenance the product does not
            # have.
            raise GatheringError(
                f"a coadd input row carries no mission identity "
                f"(expid={expid!r}, sca={sca!r}); a reference image's "
                f"product key is a digest over its inputs' identities and "
                f"cannot be computed over an unidentified one")
        infobits = row[_COADD_INFOBITS_COLUMN]
        identities.append([int(expid), int(sca),
                           0 if infobits is None else int(infobits)])
    return identities


def publish_coadd_inputs(s3_client, bucket: str, key: str,
                         rows: Iterable[Sequence[Any]]) -> tuple[str, str]:
    """Write the coadd-inputs CSV to S3. Return `(uri, checksum)`.

    The reference stage reads `coadd_inputs_uri`, splits the bucket off, and
    downloads the object — so the URI must name an object that exists before
    the job starts. Gathering publishes it because gathering is what knows the
    rows; the stage only consumes.

    **Created, never overwritten** (review finding #9). This was an
    unconditional `put_object`, and of everything in the tree that wrote
    without a condition it was the sharpest case, because the bytes genuinely
    change. `rows` come from a live overlap query over images that are still
    arriving: two gathering passes under ONE run_id — a resumed submission, a
    rerun after a partial failure — legitimately see different sets of
    overlapping L2 files, and the key is built from run_id and unit alone. So
    the second pass silently replaced an object that units already submitted
    were citing through `coadd_inputs_uri`, and those jobs then coadded a
    different set of frames than the one their submission described. Nothing
    downstream could notice: the consuming stages split the bucket off the URI
    and download whatever is there.

    `IfNoneMatch="*"` makes S3 refuse the second writer instead of letting it
    win. An identical body is an ordinary replay of the same gathering pass and
    is not an error. A different body under a used key is the defect above, and
    it raises here — where the submitter can pick a fresh run identity — rather
    than at read time in a job that has no way to tell.

    The checksum comes back with the URI because a URI alone is not a citation:
    it names a key, and this fix is precisely about a key whose bytes could
    change. The caller threads it into `UnitFacts.coadd_inputs_checksum` so the
    consuming stage can verify what it downloaded is what was published.
    """
    body = "\n".join(",".join("" if value is None else str(value)
                              for value in row)
                     for row in rows)
    if body:
        body += "\n"
    payload = body.encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    uri = f"s3://{bucket}/{key}"

    try:
        s3_client.put_object(Bucket=bucket, Key=key, Body=payload,
                             ContentType="text/csv",
                             ChecksumSHA256=base64.b64encode(
                                 hashlib.sha256(payload).digest()
                             ).decode("ascii"),
                             IfNoneMatch="*")
        return uri, digest
    except Exception as exc:  # noqa: BLE001 - narrowed immediately below
        if not is_precondition_failed(exc):
            raise GatheringError(
                f"could not publish the coadd inputs to {uri}: {exc}") from exc

    # The key is taken. Only the bytes decide whether that is this same pass
    # replaying or a second pass with a different answer, so read them.
    try:
        existing = s3_client.get_object(Bucket=bucket, Key=key)
        current = existing["Body"].read()
    except Exception as exc:  # noqa: BLE001
        raise GatheringError(
            f"the coadd inputs at {uri} already exist but could not be read "
            f"back to check whether this pass agrees with them: {exc}") from exc

    if current == payload:
        logger.info("coadd inputs %s already published with identical "
                    "content; treating as a replayed gathering pass", key)
        return uri, digest

    raise GatheringError(
        f"coadd inputs already exist at {uri} listing DIFFERENT images than "
        f"this gathering pass found. The overlap query returns more frames as "
        f"the survey advances, so a second pass under one run identity does "
        f"not agree with the first — and units already submitted cite this "
        f"key, so replacing it would make them coadd a set their submission "
        f"never described. Gather under a new run identity instead.")


# `publish_coadd_inputs` returns `(uri, checksum)` rather than a bare URI
# because a URI is not a citation on its own — it names a key, and the key's
# bytes are exactly what review finding #9 established could change underneath
# a submitted unit. `gather_reference_units` below is the sole caller and
# threads both halves into the facts; `UnitFacts.coadd_inputs_checksum` is the
# second half's home.
#
# The consuming stages (`pipeline/stages/reference_image.py` and
# `pipeline/stages/science.py`) verify the checksum when the fact is present
# and skip verification when it is absent, so a manifest written before the
# fact existed is legacy work that still runs rather than a failure.


def gather_reference_units(handle: UnitSource, start, end,
                           start_mjdobs: float, end_mjdobs: float,
                           min_images_to_coadd: int,
                           s3_client: Any, job_bucket: str,
                           run_id: str,
                           fids: Iterable[int] | None = None,
                           radius: float | None = None,
                           reference_window: tuple[float, float] | None = None,
                           on_blocked: Any = None,
                           on_unblocked: Any = None
                           ) -> Iterator[ProcessingUnit]:
    """Yield reference-image units, each with its coadd inputs published.

    `gather_science_units(make_references=True)` yields the representative
    image per (field, filter) but NOT `coadd_inputs_uri` — which
    `reference_image.download_inputs` requires as its first act, so every
    reference job it produced would fail `input_missing` before doing any
    work. The inputs are aggregated and published here, and the unit carries
    the URI of the object that now exists.

    A field that cannot yet support a reference image is SKIPPED, not fatal:
    it is the ordinary state of a field early in the survey, and one such
    field must not stop the others being submitted. Only `NotReadyYet` is
    that state. A query that failed raises past this loop, because a night
    in which every field was skipped because the database was unreachable
    must not read the same as a night early in the survey.

    **SKIPPED IS NO LONGER SILENT** (conformance rule 13, brief C4). Skipping
    was the whole disposition: the unit was logged at INFO and omitted from
    the yield, and nothing was persisted — so an operator asking "what is
    blocked, and why" had nothing to query, and a field that never ripened
    was indistinguishable from a field never gathered. `on_blocked` is that
    repair. Called with `(job_type, input_scope, dependency,
    operational_class)` for each skipped unit, it parks a queryable BLOCKED
    work unit naming the missing dependency; `on_unblocked` is its inverse,
    called for a unit that now HAS its coverage so a previously parked unit
    transitions `blocked -> ready` through the existing graph edge. No
    attempt is created or consumed on either path — a parked unit has never
    been submitted.

    **THIS MODULE STILL WRITES NOTHING ITSELF.** Both are injected callables,
    for the reason `submission.blocked`'s own docstring gives at length: this
    module's `UnitSource` is a deliberately narrow READ-ONLY protocol, and its
    testability rests on gathering needing no database to write to. The
    callbacks are supplied by `pipeline.operator.gathering`, which has the
    connection; `None` — the default, and what every probe and test passes —
    means gather without recording, which behaves exactly as this function
    did before.

    `start_mjdobs`/`end_mjdobs` bound which frames are gathered as UNITS and
    are passed on for that; they are deliberately not the coadd window.

    `reference_window` IS the coadd window — a distinct parameter because
    the two were only ever confusable while one of them had no name here.
    Its default is release content's, which is every good frame ever taken
    of that tile, and that is what a reference image is; a submission whose
    manifest carries the `reference_observation_window` override passes that
    instead. Resolved once per gathering pass rather than per unit, so every
    unit of one submission is built against one window.
    """
    if reference_window is None:
        reference_window = reference_observation_window()
    for unit in gather_science_units(handle, start, end, start_mjdobs,
                                     end_mjdobs, min_images_to_coadd,
                                     fids=fids, make_references=True):
        facts = unit.facts
        rid = facts.rid
        # All three are dereferenced below, and `UnitFacts` documents every
        # field as optional — `science_facts` builds `mjdobs` with
        # `_maybe_float`, which exists to let a NULL `L2Files.mjdobs` through.
        # Guarding only `rid` left `float(facts.mjdobs)` to raise a bare
        # `TypeError: float() argument must be ... not 'NoneType'` from inside
        # a call three arguments wide, naming neither the field nor the unit.
        # Same failure the `rid` guard above already prevents, extended to the
        # facts that are actually read, and naming every missing one at once.
        missing = [name for name in ("rid", "fid", "mjdobs")
                   if getattr(facts, name, None) is None]
        if missing:
            raise GatheringError(
                f"unit {unit.key} is missing {', '.join(missing)}; the coadd "
                f"inputs are aggregated from the representative image's own "
                f"overlap query, which needs all three")

        try:
            rows = coadd_input_rows(
                handle, int(rid), int(facts.fid), float(facts.mjdobs),
                facts.sky_position or {}, min_images_to_coadd, radius=radius,
                window=reference_window)
        except NotReadyYet as exc:
            # Narrowed from `GatheringError` on purpose. The broad catch
            # swallowed every failure this module raises — a stale sky
            # position, disagreeing field identities, an unreachable database
            # — and logged all of them at INFO as "not yet", which is the
            # sentence an operator reads and does nothing about.
            logger.info("no reference image for unit %s yet: %s",
                        unit.key, exc)
            # ...and the INFO line is no longer the whole disposition (rule
            # 13). The unit is parked BLOCKED with the dependency named, so
            # the same query that finds package A's application-failure parks
            # finds this one too. `continue` still follows: a blocked unit is
            # not yielded, so nothing is submitted for it and no attempt is
            # consumed.
            _record_blocked_unit(on_blocked, unit,
                                 blocked.REFERENCE_COVERAGE)
            continue

        # Under `submissions/<run_id>/`, beside the manifest that cites it:
        # the coadd-input list is part of what this submission published to
        # describe its work, and keeping the two together means one prefix
        # holds everything a run authored — which is also the prefix the
        # submitting identity is granted (`roman-rapid-products/submissions/*`).
        key = (f"submissions/{run_id}/coadd-inputs/{unit.key}/"
               f"input_images_for_refimage_rid{int(rid)}.csv")
        # The checksum travels with the URI (review finding #9). A URI names a
        # key, and the whole point of the conditional create above is that a
        # key's bytes could change; the consuming stage can only tell it got
        # what was published if the manifest says what that was.
        uri, checksum = publish_coadd_inputs(s3_client, job_bucket, key, rows)
        logger.info("unit %s: %d coadd inputs at %s",
                    unit.key, len(rows), uri)

        # THE COADD INPUTS' OWN IDENTITIES, carried separately from the CSV
        # (rule 10). A reference image's product key is a digest over its
        # ordered inputs, and those inputs must enter it by MISSION identity
        # — `(expid, sca)` — never by `input_rid`, never by `filename`, and
        # never as the CSV's checksum. The checksum is the tempting shortcut
        # and it is specifically forbidden: the CSV's rows embed `input_rid`
        # and `filename`, so hashing that document would put a surrogate id
        # and a path into product identity with a digest hiding the fact.
        #
        # Derived HERE because this is where the rows exist. The consuming
        # stage reads the CSV for the pixels; identity is a submission fact
        # and is resolved at submission, in the same pass that published the
        # list it describes.
        coadd_identities = coadd_input_identities(rows)

        # THE DEPENDENCY IS SATISFIED (rule 13's second half). If an earlier
        # pass parked this unit blocked on reference coverage, the coverage
        # now exists — this very pass just aggregated it — so the unit is
        # released `blocked -> ready` through the existing graph edge before
        # it is yielded for submission. A unit that was never blocked has
        # nothing to release, and the release reports that as a no-op rather
        # than an error.
        _release_blocked_unit(on_unblocked, unit)

        # THE SECOND STAGE COMPLETES THE CANDIDATE. `gather_science_units`
        # yielded this unit with its imaging facts resolved and its coadd
        # facts absent — the overlap query had not run yet. Now it has, so
        # the payload is rebuilt with all three. `dataclasses.replace` on
        # the frozen payload rather than on the unit: one object changes,
        # and the subject components are carried through unchanged, which is
        # what keeps the completed unit the SAME unit the candidate was.
        yield dataclasses.replace(
            unit,
            payload=dataclasses.replace(
                unit.payload,
                coadd_inputs_uri=uri, coadd_inputs_checksum=checksum,
                coadd_input_identities=coadd_identities))


def _blocked_identity(unit):
    """The `(job_type, input_scope, operational_class)` a blocked unit takes.

    Built through `submission.subjects.build_input_scope` and
    `pipeline.seams.operational_class_for` — the SAME two functions the
    submission path uses to identify a work unit — rather than assembled
    locally. That is the whole correctness requirement of this identity: a
    unit parked at gathering and the unit later submitted for the same field
    must be ONE row under migration 036's partial unique index on
    `(job_type, input_scope)`, so the blocked row is the row that transitions
    to ready rather than a second row shadowing it. Two spellings of the
    identity would produce two units, and the release would fire against a
    row nobody submits.

    Imported inside the function: `pipeline` imports `submission` in nine
    places and `submission` imports `pipeline` in none (see
    `submission.submit.is_precondition_failed`'s note on that direction). A
    module-level import here would invert that dependency for the whole
    package; a call-time one keeps the layering and costs a dict lookup.
    """
    from pipeline.seams import operational_class_for
    from submission.subjects import build_input_scope

    job_type = JOB_TYPE_REFERENCE_IMAGE
    return (job_type, build_input_scope(job_type, unit),
            operational_class_for(job_type))


def _record_blocked_unit(on_blocked, unit, dependency):
    """Park one unit blocked, if the caller supplied a recorder."""
    if on_blocked is None:
        return None
    job_type, input_scope, operational_class = _blocked_identity(unit)
    return on_blocked(job_type, input_scope, dependency, operational_class)


def _release_blocked_unit(on_unblocked, unit):
    """Release one unit blocked->ready, if the caller supplied a releaser."""
    if on_unblocked is None:
        return False
    job_type, input_scope, _ = _blocked_identity(unit)
    return on_unblocked(job_type, input_scope)


# ---------------------------------------------------------------------------
# The post-DB science chain (step-3 conversion)
# ---------------------------------------------------------------------------
#
# Six job types, three unit shapes, one rule: THE WORK LIST IS BUILT HERE.
#
# Every one of the six replaced a script that discovered its own work at
# runtime — `to_regclass` probes across SCAs 1-18, `select distinct field`
# against tables the previous step had just written, `pg_tables like
# 'merges_%'`. The co-design's first ruling ends that: "a job type never
# discovers its work by catalog introspection at runtime; every unit is
# individually retryable and individually reconcilable in attempt records".
#
# What that buys, concretely: a catalog-load pass over 18 SCAs used to be one
# process whose failure lost the whole date. It is now 18 units with 18 attempt
# rows, and a rerun re-submits the ones that failed. The manifest names each
# unit's declared inputs, so what a unit was FOR is recoverable from the
# submission rather than from re-running the discovery query against a catalog
# that has since changed.
#
# THE UNITS ARE NOT EXPOSURE/SCA, AND THEY NO LONGER PRETEND TO BE (rule 11).
# These job types once rode the array layer's single carrier: the real key
# (processing date, SCA, or field) went into the open `fields` dict, while
# `exposure` carried a synthetic date ordinal and `sca` a fixed `0`, purely
# so `unit.key` stayed unique. Each now builds the typed payload its own
# grain declares (`submission.payloads`), so the key is derived from what the
# unit actually is and there is no synthetic identity to keep unique.


def _validate_proc_date(proc_date: str) -> str:
    """Refuse a processing date that is not `yyyymmdd`.

    Kept from `_proc_date_ordinal`, which this replaces. That function
    existed to manufacture the synthetic `exposure` ordinal the typed
    payloads made unnecessary — but its VALIDATION is still worth having in
    the same place, because the date is now a subject component and a
    malformed one would reach the payload, the storage key and the attempt
    record before anything objected.

    Raises
    ------
    GatheringError
        If the date is not `yyyymmdd`.
    """
    text = str(proc_date)
    if not (len(text) == 8 and text.isdigit()):
        raise GatheringError(
            f"processing date {proc_date!r} is not yyyymmdd; it is a subject "
            f"component of this unit and a malformed one would reach the "
            f"storage key and the attempt record unchallenged")
    return text


def gather_catalog_load_units(handle: UnitSource, proc_date: str
                              ) -> Iterator[ProcessingUnit]:
    """Yield catalog-load units — one per (processing date, SCA).

    The unit grain is the OUTPUT TABLE's grain: `sources_<yyyymmdd>_<sca>`.
    That is deliberate and it is what makes the staging-plus-upsert load
    convergent — a unit owns exactly one target table, so a rerun of that
    unit re-loads that table and cannot interleave with another unit's
    writes.

    `loadPSFCatIntoDBSourcesTable.py` ran one process per processing DATE
    over all 18 SCAs with a thread-pool fan-out inside it. The fan-out is
    what the array layer replaces: 18 array children, 18 attempt rows, each
    retryable alone.

    **THE DECLARED INPUTS ARE PRODUCT URIS, NOT JIDS** (the re-source). This
    used to name a `jids` list that `download_psf_catalogs` turned into
    `<proc_date>/jid<N>/<name>` keys. Both halves were stale: `Jobs` is empty
    so the list was always empty, and no such prefix exists in the product
    bucket — everything since the submission restructure is attempt-scoped.

    What the unit now carries is the registered difference image's own URI
    per contributing attempt, taken from `diffimages.filename`. The catalogue
    is that object's SIBLING, so the loader resolves a real key against a
    real one rather than assembling a key from parts that must all be right.
    That also survives the live key-grammar split — the newer rows are
    zero-padded C-core keys, the older ones are not — because no component is
    reconstructed.
    """
    proc_date = _validate_proc_date(proc_date)

    # THE RESUBMISSION GATE (mission mock, live 2026-08-09): enumerate only
    # SCAs still LACKING a pending-or-successful catalog-load attempt for
    # this date. The unconditional science-SCA enumeration resubmitted the
    # same units every accumulator cut for the whole flight of the first
    # attempt and forever after its success. Failed attempts free the SCA
    # again — re-gathering is the retry path. See the handle method's
    # docstring for the exact blocking predicate.
    try:
        scas = handle.get_scas_with_gatherable_catalog_load_for_processing_date(
            proc_date)
    except RapidDBCallFailed as exc:
        raise GatheringError(
            f"SCA enumeration failed for processing date {proc_date}: "
            f"{exc}") from exc

    for sca in scas or ():
        sca = int(sca[0] if isinstance(sca, (list, tuple)) else sca)

        try:
            products = handle.get_registered_diffimages_for_processing_date_sca(
                proc_date, sca)
        except RapidDBCallFailed as exc:
            raise GatheringError(
                f"product enumeration failed for {proc_date} SCA {sca}: "
                f"{exc}") from exc

        # (pid, expid, sca, attempt_id, filename, field, fid, mjdobs) — the
        # query's column order. Carried as mappings rather than raw tuples
        # because the manifest is what a human reads when a unit has to be
        # explained, and a bare tuple explains nothing. field/fid/mjdobs
        # ride per PRODUCT (mission mock, live 2026-08-09): a (date, SCA)
        # unit loads catalogues from MANY products, and the sources rows'
        # identity columns (pid, expid, field, fid, mjdobs) vary per
        # catalogue file — a unit-constant fact wrote NULL pid for every
        # source and the load's NOT NULL constraint refused the COPY.
        inputs = [{"pid": _maybe_int(row[0]), "expid": _maybe_int(row[1]),
                   "attempt_id": _maybe_int(row[3]),
                   "difference_image_uri": _maybe_str(row[4]),
                   "field": _maybe_int(row[5]), "fid": _maybe_int(row[6]),
                   "mjdobs": None if row[7] is None else float(row[7])}
                  for row in products or ()]

        yield ProcessingUnit(
            payload=payloads.build(
                JOB_TYPE_CATALOG_LOAD,
                proc_date=str(proc_date), sca=sca,
                # The declared input: the table this unit loads. Named in
                # the manifest so the unit's target is a submission fact
                # rather than something the job builds from its own
                # environment and hopes matches.
                target_table=f"sources_{proc_date}_{sca}",
                # The declared inputs: which registered products this
                # unit's catalogues come from. A unit with none loads
                # nothing and records that through its effect counts —
                # the empty-product-set disposition, not an error.
                product_inputs=tuple(inputs)))


def _association_repository(handle):
    """An `AssociationRepository` over the handle's connection, or `None`.

    **THE CARVED PATH, NOT A `RAPIDDB` METHOD.** `RAPIDDB` is frozen (brief G's
    ratified merge decision): no new capability lands in it. The two reads the
    ordering gate needs are therefore a repository over a connection, and this
    is the seam that gets one from a gathering handle — `RAPIDDB.conn` is an
    attribute, and `CheckedHandle` passes non-callables through unchecked, so
    no new method is added to the frozen class and none is needed.

    Returns `None` when the handle exposes no usable connection, which is the
    case for the operator probes and every gathering stub. Those callers get
    the same unordered behaviour a database without DRAFT 049 gets, and the
    log line in `_association_claim_position` says which happened.

    A test double may instead supply `association_repository` directly, so the
    stub tier can exercise the ordering arithmetic without a database — the
    contract tier is where the SQL itself executes, which is the division the
    stub-blind rule draws.
    """
    injected = getattr(handle, "association_repository", None)
    if injected is not None:
        return injected

    conn = getattr(handle, "conn", None)
    if conn is None:
        return None
    return AssociationRepository(conn)


def _association_claim_position(handle: UnitSource):
    """The live lane's watermark, or `None` when the schema is absent.

    Returns `(watermark_proc_date, watermark_field)` — the last ACCEPTED
    unit's position in the canonical order — where `(None, None)` is the
    origin and means nothing has been accepted in this set yet.

    **A DATABASE WITHOUT MIGRATION 049 DEGRADES TO TODAY'S BEHAVIOUR,
    EXPLICITLY.** 049 (`association_watermarks`) landed in the authoritative
    stream on 2026-08-12 (`migrations-draft/README.md`), so this is no longer
    "the draft is unadopted" — it is the ordinary lag between a migration
    landing and a given database's applier run. This module has NO SCHEMA
    VISIBILITY OF ITS OWN to tell the two apart: gathering runs on hosts with
    no science stack (this file must not import the payload's stage
    packages), and `pipeline.intent.schema_contract`'s per-route preflight
    (`ROUTE_MIGRATIONS["crossmatch"]`) runs in the PAYLOAD, at job start, on
    whatever database THAT job resolves — a process this module has already
    finished submitting to by the time it runs, on a connection gathering
    does not share. So the degradation stays: the handle answers `None` when
    the table is not there, and this returns `None`, and the caller then
    gathers exactly as it did before — unordered, every ready field. Raising
    here would take the whole crossmatch chain down on every database behind
    049, for a fact this layer cannot distinguish from "not adopted yet".

    It is degradation and not a silent pass because the log line says which
    one happened, and because the contract tier asserts the ordered behaviour
    against a database that DOES carry the migration.
    """
    repository = _association_repository(handle)
    if repository is None:
        position = None
    else:
        try:
            position = repository.claim_position()
        except RepositoryQueryFailed as exc:
            raise GatheringError(
                f"association watermark read failed: {exc}") from exc

    if position is None:
        logger.info(
            "crossmatch: no association watermark available (DRAFT 049 not "
            "applied); gathering without the rule 19 ordering gate")
        return None

    # A `Watermark(proc_date, field)` — the named tuple
    # `AssociationRepository.claim_position` returns. Unpacked positionally
    # rather than defensively unwrapped: a repository answering some other
    # shape is a bug to surface here, not one to absorb into a guess about
    # which element was meant.
    wm_date, wm_field = position
    return (None if wm_date is None else str(wm_date),
            None if wm_field is None else int(wm_field))


def _earliest_owed_date(handle: UnitSource):
    """The earliest processing date still owing crossmatch work, or `None`.

    The cross-date half of the ordering gate. `None` means either "nothing is
    owed" or "the ordering schema is absent"; both readings make the same
    decision downstream (do not block this date on an earlier one), and the
    watermark check is what distinguishes an ordered deployment from an
    unordered one, so nothing is lost by collapsing them here.
    """
    repository = _association_repository(handle)
    if repository is None:
        return None

    try:
        return repository.earliest_unaccepted_date()
    except RepositoryQueryFailed as exc:
        raise GatheringError(
            f"earliest-unaccepted crossmatch date read failed: {exc}") from exc


def _next_claimable_field(candidates: Sequence[int], proc_date: str,
                          position, earliest_owed=None) -> Iterator[int]:
    """Yield AT MOST ONE field: the next one in canonical order, or nothing.

    Canonical claim order is ascending `(proc_date, field)` — the vocabulary
    mapping's reading of rule 19's `(observation_time, detection_id)` at the
    grain association is actually claimed in. `position` is the watermark;
    a unit is claimable only when it is strictly AHEAD of it.

    **THE HARD CONCURRENCY CAP OF 1 (§2.5) FALLS OUT OF THIS, NOT OUT OF A
    QUEUE PROPERTY.** One lane, one next unit, and nothing claimable past an
    unaccepted predecessor: a pass yields one unit, and the next unit is not
    yielded until that one's acceptance advances the watermark. No route
    changed, no queue depth changed, no Batch setting is load-bearing.

    **AN EARLIER FAILED-AND-RETRYABLE UNIT BLOCKS EVERY LATER ONE.** This is
    the §2.5 sentence the whole item exists for: "serial execution does not by
    itself guarantee that a later observation's association cannot run ahead
    of an earlier one still in retry". A failed-retryable unit did not advance
    the watermark — acceptance is what advances it — so the frontier still
    sits behind it and nothing later is ahead of the frontier by one step. The
    blocking is therefore not a rule this function enforces on top of the
    watermark; it IS the watermark, read correctly.

    Concretely, for a gathering pass on date `d` with watermark `(wd, wf)`:

      * `d < wd` — the whole date is behind the frontier. Nothing.
      * `d == wd` — the next field of this date strictly greater than `wf`,
        if it is ready. If the ready field set skips over `wf`'s successors
        because they are blocked or unready, the smallest READY field ahead of
        `wf` is next: a blocked field is not a claimable predecessor, and the
        resubmission gate already means "in flight or done".
      * `d > wd` — the date is ahead of the frontier, so its smallest ready
        field is next, BUT ONLY IF NO EARLIER DATE IS STILL OWED. Gathering is
        invoked once per date (`pipeline/operator/gathering.py:246-249`), so
        the watermark alone cannot see that d1 is still in retry while this
        pass asks about d2. `earliest_owed` is the fact that closes it: the
        earliest processing date with crossmatch work not yet accepted. When
        that date is earlier than this one, this pass yields NOTHING — which
        is acceptance criterion 1's second half, "(d1,f1) failed-retryable and
        (d2,f1) ready gathers nothing", and §2.5's sentence made operational.

    `position is None` means no ordering schema (see
    `_association_claim_position`) and yields the candidates unchanged.
    """
    if position is None:
        for field in candidates:
            yield field
        return

    wm_date, wm_field = position
    ready = sorted(candidates)
    if not ready:
        return

    if wm_date is None:
        # THE ORIGIN — and it still has a predecessor question to answer.
        # Nothing accepted in this set yet does NOT mean nothing is owed: the
        # very first units may have run and failed, and this pass may be for a
        # LATER date than the one still owing. An earlier version returned
        # `ready[0]` here unconditionally and let a later date claim past a
        # failed earlier one — the exact §2.5 violation the whole item exists
        # to close, live at the origin, which is where every deployment
        # starts. The cross-date check therefore comes FIRST and applies at
        # the origin exactly as it does after the frontier has moved.
        if earliest_owed is not None and str(earliest_owed) < str(proc_date):
            logger.info(
                "crossmatch: nothing accepted in this association set yet, "
                "but date %s still owes crossmatch work; date %s is not "
                "claimable past an unaccepted predecessor",
                earliest_owed, proc_date)
            return
        yield ready[0]
        return

    if str(proc_date) < str(wm_date):
        # Behind the frontier. A date whose units were all accepted has
        # nothing left to claim, and a stale gathering pass for it yields
        # nothing rather than re-submitting accepted work.
        logger.info(
            "crossmatch: processing date %s is behind the association "
            "watermark (%s, %s); nothing claimable",
            proc_date, wm_date, wm_field)
        return

    if str(proc_date) == str(wm_date):
        ahead = [f for f in ready if f > int(wm_field)]
        if not ahead:
            logger.info(
                "crossmatch: no field of processing date %s is ahead of the "
                "association watermark (%s, %s); nothing claimable",
                proc_date, wm_date, wm_field)
            return
        yield ahead[0]
        return

    # `proc_date > wm_date`: ahead of the frontier by date. Claimable only if
    # no EARLIER date still owes work — the failed-and-retryable predecessor
    # case (§2.5). An earlier owed date did not advance the watermark, because
    # acceptance is what advances it, so it is still the frontier's successor
    # and this later date waits behind it.
    if earliest_owed is not None and str(earliest_owed) < str(proc_date):
        logger.info(
            "crossmatch: processing date %s is ahead of the association "
            "watermark (%s, %s) but date %s still owes crossmatch work; "
            "later units are not claimable past an unaccepted predecessor",
            proc_date, wm_date, wm_field, earliest_owed)
        return

    yield ready[0]


def gather_crossmatch_units(handle: UnitSource, proc_date: str
                            ) -> Iterator[ProcessingUnit]:
    """Yield crossmatch units — one per (processing date, field).

    **DURABLE-STATE READINESS, NOT OPERATOR SEQUENCING** (co-design ruling 1;
    design/operations.md: "Crossmatch readiness is durable state, not
    operator sequencing: its gathering predicate checks recorded
    catalog-load completion facts directly ... never an ordering convention
    among gatherer invocations"). This used to gather unconditionally and
    rely on the OPERATOR submitting catalog load before crossmatch each
    pass — an ordering convention among gatherer invocations, exactly what
    the design forbids, because it gives the wrong answer the moment two
    passes interleave (a crossmatch poll racing a still-running catalog-load
    batch) or the operator restarts mid-chain. The predicate now reads the
    same fact the alert-production predicate reads
    (`get_scas_with_incomplete_catalog_load_for_processing_date`): whether a
    successful catalog-load attempt is RECORDED, never whether one was
    submitted first.

    **COVERAGE IS PER PROCESSING DATE, NOT PER FIELD** — see the handle
    method's own docstring for why: `crossMatchSources.py` reads every SCA
    of the date for every field it cross-matches, so one incomplete SCA
    blocks every field of that date, not just the ones that happen to share
    it. The check therefore runs ONCE per gathering pass, not once per
    field.

    A date with any incomplete SCA yields NOTHING — `NotReadyYet` is not
    raised here (unlike the reference-image gatherer) because "gather again
    next poll" is the intended behaviour for a data dependency that will
    resolve on its own, and an empty yield is exactly what every other
    gatherer does for "nothing ready yet".

    **ORDERED CLAIMING BEHIND THE WATERMARK** (conformance rule 19, target
    §2.5, brief F2). Readiness is not order. Once the two readiness gates
    above have said which fields COULD run, this yields AT MOST ONE of them:
    the next unit in canonical `(proc_date, field)` order that is strictly
    ahead of the live lane's watermark. Both readiness gates are unchanged —
    they answer "is this unit runnable", and the ordering gate answers "is it
    this unit's turn", which is a different question asked afterwards.

    §2.5's hard concurrency cap of 1 falls out of that claim discipline rather
    than being configured anywhere: one lane, one next unit, and nothing
    claimable past an unaccepted predecessor. NO ROUTE OR QUEUE CHANGED —
    crossmatch remains `CLASS_BULK` on the unbounded bulk queue
    (`submission/routes.py`). That is deliberate and is the reading this
    implementation records: ordering enforcement belongs in the claim path,
    where the pipeline can reason about it, and NEVER in a Batch queue
    property. The scheduler stays free-running; correctness never depends on
    Batch running things in the order it received them, because Batch is
    never told an order to begin with.

    A unit earlier in canonical order that is failed-and-retryable therefore
    blocks every later unit in its set: it did not advance the watermark —
    only acceptance does — so the frontier still sits behind it. On TERMINAL
    disposition the watermark may pass it, and does so EXPLICITLY: a cancelled
    or parked unit is skipped by an operator advancing the watermark past it
    (`derived.advance_association_watermark`), never by gathering quietly
    deciding a unit no longer counts. The disposition rule, fixed here: only
    ACCEPTANCE advances the watermark automatically; cancellation and parking
    advance it only through that explicit operator action, and until one
    happens the set stays stopped at the disposed unit. A set that silently
    stepped over its own failures would be an ordering guarantee that quietly
    stops guaranteeing exactly when it matters.

    On a database without DRAFT 049 this degrades to the previous unordered
    behaviour with a log line saying so — see `_association_claim_position`.
    """
    proc_date = _validate_proc_date(proc_date)

    try:
        incomplete = handle.get_scas_with_incomplete_catalog_load_for_processing_date(
            proc_date)
    except RapidDBCallFailed as exc:
        raise GatheringError(
            f"catalog-load coverage check failed for processing date "
            f"{proc_date}: {exc}") from exc
    if incomplete:
        logger.info(
            "crossmatch: processing date %s has %d SCA(s) with no completed "
            "catalog-load attempt yet (%s); gathering nothing this pass",
            proc_date, len(incomplete), sorted(int(s) for s in incomplete))
        return

    try:
        fields = handle.get_fields_with_science_jobs_for_processing_date(
            proc_date)
    except RapidDBCallFailed as exc:
        raise GatheringError(
            f"field enumeration failed for processing date {proc_date}: "
            f"{exc}") from exc

    # THE RESUBMISSION GATE (mission mock, live 2026-08-09): a field with a
    # pending-or-successful crossmatch attempt for this date is not
    # re-gathered; a field whose attempts all failed is — retry by
    # re-gathering, same predicate as the catalog-load gather set.
    try:
        blocked = handle.get_fields_with_blocking_crossmatch_attempt_for_processing_date(
            proc_date)
    except RapidDBCallFailed as exc:
        raise GatheringError(
            f"crossmatch blocking-attempt check failed for processing date "
            f"{proc_date}: {exc}") from exc
    blocked_fields = {int(row[0] if isinstance(row, (list, tuple)) else row)
                      for row in blocked or ()}

    # THE ORDERING GATE (rule 19, brief F2). Everything above this point is
    # READINESS — is this date's data complete, is this field already in
    # flight. Ordering is a separate question asked afterwards, and it is
    # asked here, in the CLAIM PATH, not of the queue: the scheduler stays
    # free-running and crossmatch stays CLASS_BULK on the unbounded bulk
    # queue (`submission/routes.py`), unchanged. Correctness never depends on
    # Batch preserving an order, because Batch is never told one.
    position = _association_claim_position(handle)
    earliest_owed = _earliest_owed_date(handle) if position is not None else None
    candidates = []
    for field in fields or ():
        field = int(field[0] if isinstance(field, (list, tuple)) else field)
        if field in blocked_fields:
            continue
        candidates.append(field)

    # THE SOURCE TABLES A CLAIMED UNIT READS FROM (2026-08-14, closing the
    # gap `CrossmatchPayload.source_tables`'s own docstring named). The
    # readiness gate above already confirmed the whole date's catalog load
    # is complete before any field could reach this point — `incomplete`
    # was empty — so the completed set is simply every SCA that ran science
    # on this date, read through the complement query rather than
    # re-deriving "loaded" from a second notion of completeness. Read once,
    # outside the per-field loop below: readiness, like the incomplete
    # check above it, is a per-DATE fact, not a per-field one.
    try:
        completed = handle.get_scas_with_completed_catalog_load_for_processing_date(
            proc_date)
    except RapidDBCallFailed as exc:
        raise GatheringError(
            f"catalog-load coverage check failed for processing date "
            f"{proc_date}: {exc}") from exc
    source_tables = tuple(
        f"sources_{proc_date}_"
        f"{int(sca[0] if isinstance(sca, (list, tuple)) else sca)}"
        for sca in completed or ())

    for field in _next_claimable_field(candidates, proc_date, position,
                                       earliest_owed):
        yield ProcessingUnit(
            payload=payloads.build(
                JOB_TYPE_CROSSMATCH,
                proc_date=str(proc_date), field=field,
                target_tables=(f"astroobjects_{field}",
                               f"merges_{field}"),
                source_tables=source_tables))


def _per_field_units(handle: UnitSource, job_type: str, prototype: str
                     ) -> Iterator[ProcessingUnit]:
    """Yield one unit per field that has a clone of `prototype`.

    The shared body of the three corpus-wide sweeps and statistics. They
    differ only in which prototype's clones define their work list and what
    they then do to each field, so the enumeration is written once.

    **THE FIELD RIDES IN `exposure`, NOT IN `sca`** — and that is a live
    finding, not a style choice. `attempts.sca` is `smallint` (max 32767)
    because an SCA is 1-18; a Roman field identifier is seven digits
    (4641773 in the live l2files), so putting one in `sca` raised
    `NumericValueOutOfRange: smallint out of range` from `create_submitted`
    on the first real submission. `exposure_id` is `integer`, which holds it.
    The unit tests missed this because their synthetic fields were small
    enough to fit — only a live row insert could surface it.

    `sca` is therefore 0: these units have no SCA, and 0 says so rather than
    a field identifier pretending to be one. Uniqueness comes from the field
    in `exposure`, which is what `unit.key` and the run-scoped
    `logical_job_key` are built from.
    """
    try:
        fields = handle.get_fields_with_per_field_table(prototype)
    except RapidDBCallFailed as exc:
        raise GatheringError(
            f"per-field enumeration failed for prototype {prototype}: "
            f"{exc}") from exc

    # THE RESUBMISSION GATE (mission mock, live 2026-08-09): the FIELD-grain
    # types carry no processing date in their identity (ruling 2 — only
    # applicable identifiers), so the durable-state dedup is "no pending or
    # successful attempt of this job type for this field since UTC
    # midnight" — at most one run per field per UTC day. Without it this
    # state-blind enumeration resubmitted every accumulator cut (found live
    # at the mock's first prompt-class enablement: 3 sweep children per
    # cut). The day cadence is a recorded, revisitable v1 call — the real
    # sweep cadence policy is an open design item; failed attempts free the
    # field again, retry by re-gathering.
    since = datetime.datetime.now(datetime.timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0)
    try:
        blocked = handle.get_fields_with_blocking_attempt_for_job_type_since(
            job_type, since)
    except RapidDBCallFailed as exc:
        raise GatheringError(
            f"blocking-attempt check failed for job type {job_type}: "
            f"{exc}") from exc
    blocked_fields = {int(row[0] if isinstance(row, (list, tuple)) else row)
                      for row in blocked or ()}

    for field in fields or ():
        field = int(field[0] if isinstance(field, (list, tuple)) else field)
        if field in blocked_fields:
            continue
        yield ProcessingUnit(
            payload=payloads.build(job_type, field=field,
                                   target_table=f"{prototype}_{field}"))


def gather_statistics_units(handle: UnitSource) -> Iterator[ProcessingUnit]:
    """Yield statistics units — one per field with an astroobjects clone.

    Statistics rebuilds `astroobjectsmeta_<field>` from `astroobjects_<field>`,
    so the astroobjects clones are what define the work.
    """
    return _per_field_units(handle, JOB_TYPE_STATISTICS, "astroobjects")


def gather_merge_currency_units(handle: UnitSource
                                ) -> Iterator[ProcessingUnit]:
    """Yield merge currency-sweep units — one per field with a merges clone.

    The sweep removes merge rows whose difference image has been demoted out
    of best status. Row currency in this family is a DERIVED property (the
    operations design's derived-currency invariant): a row is current while
    the image it derives from holds best status, and between a demotion and
    the next sweep superseded rows are present by design.
    """
    return _per_field_units(handle, JOB_TYPE_MERGE_CURRENCY, "merges")


def gather_source_currency_units(handle: UnitSource
                                 ) -> Iterator[ProcessingUnit]:
    """Yield source currency-sweep units — one per field with a merges clone.

    The source-side counterpart, and one of the two sweeps the co-design's
    ruling 3 brings into the operational chain despite never having been
    invoked by the VPO: they "are the only maintainers of integrity
    properties the schema does not enforce", and an unmaintained invariant
    is a defect under the cross-cutting rules.

    Enumerated from the merges clones rather than the sources children
    because the sweep is per FIELD — `pruneNotBestSources.py` is a per-field
    sweep like its sibling, and the sources children are per (date, SCA).
    """
    return _per_field_units(handle, JOB_TYPE_SOURCE_CURRENCY, "merges")


def gather_merge_dedup_units(handle: UnitSource) -> Iterator[ProcessingUnit]:
    """Yield merge-dedup units — one per field with a merges clone.

    **A should-find-nothing integrity check, not a maintenance dependency.**
    Migration 027 put a unique index on the merges prototype's (aid, sid) and
    the clone path carries it, so duplicates are now PREVENTED rather than
    swept (co-design ruling 6). This job type stays in the chain to prove
    that: a nonzero find is a defect report about the constraint, and the
    job records it as one rather than quietly deleting rows.
    """
    return _per_field_units(handle, JOB_TYPE_MERGE_DEDUP, "merges")


# ---------------------------------------------------------------------------
# The alert-production trigger (step-4 conversion)
# ---------------------------------------------------------------------------


def gather_alert_production_units(handle: UnitSource, release_identity: str,
                                  limit: int | None = None
                                  ) -> Iterator[ProcessingUnit]:
    """Yield alert-production units — one per unit awaiting emission.

    The trigger the step-4 co-design ruled, in its adopted shape: gathering
    over registration OUTCOMES, on the prompt queue, through the accumulator
    like all prompt work (gate 2). The rejected alternative was an
    in-process after-commit seam inside registration — not durable, coupling
    registration to the stream, and leaving the alert work with no attempt of
    its own to be recorded against.

    The unit is the registered attempt (gate 1), keyed by (exposure, SCA) —
    the SCA attempt, which is the promotion scope and the grain a difference
    image has. Deduplication is by attempt identity, which is permanent and
    replay-guarded; emission scoping is by unit and release (gate 4), which
    is the watermark's primary key.

    The manifest names the ATTEMPT IDENTITY AND THE PROMOTED DIFFERENCE-IMAGE
    IDENTITY as declared inputs, per the design: those two facts are what the
    job needs to produce this unit's alerts, and naming them in the
    submission is what makes the unit recoverable without re-running the
    gathering query against a catalog that has since moved on.

    `release_identity` is not defaulted. Emission scope includes the release,
    so a gathering pass that did not know its release would either emit
    against the wrong scope or invent one — and "once per unit per release"
    would then be enforced against a scope nobody chose.
    """
    if not str(release_identity or "").strip():
        raise GatheringError(
            "alert gathering needs the release identity: emission is scoped "
            "once per logical unit per RELEASE, so a pass without one cannot "
            "tell an already-emitted unit from a new one")

    try:
        rows = handle.get_attempts_awaiting_alert_emission(release_identity,
                                                           limit=limit)
    except RapidDBCallFailed as exc:
        raise GatheringError(
            f"alert gathering failed for release {release_identity}: "
            f"{exc}") from exc

    for row in rows or ():
        # (attempt_id, expid, sca, pid, product, role_resolved_from,
        #  registered_at, sequence) — the query's column order.
        attempt_id, expid, sca, pid = (_maybe_int(row[index])
                                       for index in (0, 1, 2, 3))
        if expid is None or sca is None:
            raise GatheringError(
                f"attempt {attempt_id} promoted a difference image but has no "
                f"exposure/SCA identity; the alert unit is keyed by it")

        yield ProcessingUnit(
            payload=payloads.build(
                JOB_TYPE_ALERT_PRODUCTION,
                exposure=int(expid), sca=int(sca),
                promoted_attempt_id=attempt_id,
                release_identity=str(release_identity),
                difference_image_pid=pid,
                difference_image_product=_maybe_str(row[4]),
                role_resolved_from=_maybe_str(row[5]),
                promotion_sequence=_maybe_int(row[7])))


def initialize_alert_watermark(handle: UnitSource, release_identity: str,
                               reason: str = "deployment") -> int:
    """Seed the emission watermark so existing promotions do not emit.

    **PROPOSED DISPOSITION, IMPLEMENTED CONSERVATIVELY.** The design rules
    that emission is once per unit per release and says nothing about what a
    watermark should contain the moment it is created — because until now
    there was no watermark and no emitter. Left empty, every promotion
    already in the catalog is "not yet emitted" and the first production run
    would emit the entire backlog at once.

    That backlog is the 1,086 replay-backfilled promotions from the
    difference-image role binding: simulation data, registered by a
    deliberate replay of attempts whose registration had refused, for a
    release nobody is consuming a stream from. A retroactive flood of them
    serves no consumer, and the design's emission is explicitly live-flow —
    "the first outcome in which the unit's difference image became current",
    an event, not a backlog scan.

    So this claims the watermark for every unit that has ALREADY promoted,
    recording `alerts_published = 0`: those units are marked as having had
    their emission accounted for, without anything being published. New
    promotions after this point are unaffected and emit normally.

    It is recorded in the run ledger as a PROPOSED disposition for the
    register, not as a settled ruling: it is a real decision about what the
    system does, and the co-design did not make it.

    Returns the number of units claimed.
    """
    try:
        rows = handle.get_attempts_awaiting_alert_emission(release_identity)
    except RapidDBCallFailed as exc:
        raise GatheringError(
            f"watermark initialization could not read the outstanding units "
            f"for release {release_identity}: {exc}") from exc

    claimed = 0
    for row in rows or ():
        attempt_id, expid, sca, pid = (_maybe_int(row[index])
                                       for index in (0, 1, 2, 3))
        if expid is None or sca is None:
            continue
        # `seed_alert_emission_watermark` (migration 037 / integration ruling
        # 3 split: seeding and the live CAS claim are different writes now —
        # see its docstring). Used to be read only for its return value — a
        # failed claim (nonzero exit_code) and a claim that lost the
        # `ON CONFLICT DO NOTHING` race both fell through as "not claimed"
        # and this unit was silently skipped rather than the pass being
        # told a query had failed mid-backfill. The adapter now raises
        # instead, so only a genuine race loss reaches the `if` below.
        try:
            won = handle.seed_alert_emission_watermark(
                int(expid), int(sca), str(release_identity),
                int(attempt_id), pid=pid)
        except RapidDBCallFailed as exc:
            raise GatheringError(
                f"watermark initialization could not claim unit "
                f"{expid}/{sca} for release {release_identity}: "
                f"{exc}") from exc
        if won:
            claimed += 1

    logger.info("alert watermark initialized for release %s (%s): %d unit(s) "
                "claimed with zero alerts published; they will not emit "
                "retroactively", release_identity, reason, claimed)
    return claimed


def _sca_of(handle: UnitSource, rid: int) -> int:
    meta = handle.get_l2filemeta_record(rid)
    if meta is None or meta[0] is None:
        raise GatheringError(f"rid {rid} has no SCA in L2FileMeta")
    return int(meta[0])


def _maybe_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _maybe_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _maybe_str(value: Any) -> str | None:
    return None if value is None else str(value)
