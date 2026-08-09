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
from database.modules.utils.roman_tessellation_db import (
    RomanTessellationClosedForm)
from .manifest import ProcessingUnit, UnitFacts
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
        tile_position=_tile_position(int(field)),
    )

    try:
        filter_name = handle.get_exposure_filter(fid)
    except RapidDBCallFailed as exc:
        raise GatheringError(
            f"filter lookup failed for fid {fid}: {exc}") from exc
    if filter_name is not None:
        facts = _replace(facts, filter_name=str(filter_name))

    try:
        psf = handle.get_best_psf(sca, fid)
    except RapidDBCallFailed as exc:
        raise GatheringError(
            f"PSF lookup failed for sca {sca} fid {fid}: {exc}") from exc
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
    indistinguishable downstream of gathering. `unit.fields["job_type"]` is
    NOT set here (unlike the post-DB gatherers): the campaign gatherer's
    units are exposure/SCA-grain exactly like plain science units
    (`submission.subjects.SUBJECTS`'s `JOB_TYPE_SCIENCE` row already
    declares that grain), so `dedup_key`/`logical_job_key` need no extra
    field to disambiguate them, and setting a redundant `fields["job_type"]`
    here would be a second home for a fact the manifest's own `job_type`
    already carries.
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
        yield ProcessingUnit(exposure=int(exposure), sca=int(sca),
                             facts=facts)


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

    rows: list[list[Any]] = []
    for image in overlapping:
        input_rid = int(image[0])
        field_from_overlap = image[11]
        try:
            info = handle.get_info_for_l2file(input_rid)
        except RapidDBCallFailed as exc:
            raise GatheringError(
                f"get_info_for_l2file failed for rid {input_rid}: "
                f"{exc}") from exc
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
                           reference_window: tuple[float, float] | None = None
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

        yield dataclasses.replace(
            unit, facts=_replace(facts, coadd_inputs_uri=uri,
                                 coadd_inputs_checksum=checksum))


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
# THE UNITS ARE NOT EXPOSURE/SCA, AND `ProcessingUnit` STILL CARRIES THEM.
# The array layer knows one carrier, and `ProcessingUnit(exposure, sca)` is
# it. These follow the pattern gathering already used for units not keyed by
# exposure: the real key (processing date, SCA, or field) rides in `fields`,
# and `exposure`/`sca` carry a stable synthetic identity that keeps `unit.key`
# unique, which is
# what `logical_job_key` needs to keep run-scoped rows from colliding.


def _proc_date_ordinal(proc_date: str) -> int:
    """`yyyymmdd` as an integer, for the synthetic unit key.

    The unit key must be unique per unit and stable across a retry of the
    same unit — that is all `ProcessingUnit.key` promises. A processing date
    is already an integer written as a string, so it needs no hashing.

    Raises
    ------
    GatheringError
        If the date is not `yyyymmdd`. A malformed date would silently
        produce colliding unit keys, so it is refused where it is read
        rather than propagated into a manifest.
    """
    text = str(proc_date)
    if not (len(text) == 8 and text.isdigit()):
        raise GatheringError(
            f"processing date {proc_date!r} is not yyyymmdd; the unit key is "
            f"derived from it and a malformed date collides silently")
    return int(text)


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
    ordinal = _proc_date_ordinal(proc_date)

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

        # (pid, expid, sca, attempt_id, filename) — the query's column order.
        # Carried as mappings rather than raw tuples because the manifest is
        # what a human reads when a unit has to be explained, and a bare
        # 5-tuple explains nothing.
        inputs = [{"pid": _maybe_int(row[0]), "expid": _maybe_int(row[1]),
                   "attempt_id": _maybe_int(row[3]),
                   "difference_image_uri": _maybe_str(row[4])}
                  for row in products or ()]

        yield ProcessingUnit(
            exposure=ordinal, sca=sca,
            facts=UnitFacts(),
            fields={"proc_date": str(proc_date), "sca": sca,
                    "job_type": JOB_TYPE_CATALOG_LOAD,
                    # The declared input: the table this unit loads. Named in
                    # the manifest so the unit's target is a submission fact
                    # rather than something the job builds from its own
                    # environment and hopes matches.
                    "target_table": f"sources_{proc_date}_{sca}",
                    # The declared inputs: which registered products this
                    # unit's catalogues come from. A unit with none loads
                    # nothing and records that through its effect counts —
                    # the empty-product-set disposition, not an error.
                    "product_inputs": inputs})


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
    """
    ordinal = _proc_date_ordinal(proc_date)

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

    for field in fields or ():
        field = int(field[0] if isinstance(field, (list, tuple)) else field)
        if field in blocked_fields:
            continue
        yield ProcessingUnit(
            exposure=ordinal, sca=0,
            facts=UnitFacts(field=field),
            fields={"proc_date": str(proc_date), "field": field,
                    "job_type": JOB_TYPE_CROSSMATCH,
                    "target_tables": [f"astroobjects_{field}",
                                      f"merges_{field}"]})


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
            exposure=field, sca=0,
            facts=UnitFacts(field=field),
            fields={"field": field, "job_type": job_type,
                    "target_table": f"{prototype}_{field}"})


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
            exposure=int(expid), sca=int(sca),
            # `pid` IS the declared difference-image identity, and it is what
            # `batch_produce` is called with. It rides in the facts because
            # `UnitFacts.pid` is its one home, not in `fields` beside it.
            facts=UnitFacts(pid=pid, expid=expid),
            fields={"job_type": JOB_TYPE_ALERT_PRODUCTION,
                    "attempt_id": attempt_id,
                    "release_identity": str(release_identity),
                    "difference_image_pid": pid,
                    "difference_image_product": _maybe_str(row[4]),
                    "role_resolved_from": _maybe_str(row[5]),
                    "promotion_sequence": _maybe_int(row[7])})


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


def _replace(facts: UnitFacts, **changes: Any) -> UnitFacts:
    """`dataclasses.replace` for the frozen facts record."""
    return dataclasses.replace(facts, **changes)


def _maybe_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _maybe_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _maybe_str(value: Any) -> str | None:
    return None if value is None else str(value)
