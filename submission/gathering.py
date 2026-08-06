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

    def get_overlapping_l2files(self, rid: int, fid: int, mjdobs: float,
                                *corners: float,
                                radius_of_initial_cone_search: float | None
                                = ...) -> Sequence[Any]: ...

    def get_job_record(self, jid: int) -> Sequence[Any] | None: ...

    def get_best_difference_image(self, rid: int,
                                  ppid: int) -> dict[str, Any]: ...

    def get_reference_image(self, rfid: int) -> dict[str, Any]: ...

    def get_jids_of_normal_science_pipeline_jobs_for_processing_date(
            self, proc_date: str) -> Sequence[Any]: ...


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


def coadd_input_rows(handle: UnitSource, rid: int, fid: int, mjdobs: float,
                     sky_position: dict, min_images_to_coadd: int,
                     radius: float | None = None) -> list[list[Any]]:
    """The reference image's coadd inputs, as CSV rows.

    The launcher's aggregation, preserved because it is science logic: every
    L2 file overlapping this one's sky tile in the same filter, acquired
    earlier, ordered by distance from tile centre — then filtered to the rows
    that may actually be coadded.

    The two exclusions are the launcher's and are not optional. `status == 0`
    is a file marked bad; `vbest == 0` is a superseded version. Coadding
    either would build a reference from images the database says not to use.

    Raises `GatheringError` rather than returning a short list when there are
    too few inputs: "this field cannot support a reference image yet" and
    "the query failed" must not look the same to the submitter.
    """
    corners = [sky_position.get(key) for key in
               ("ra0", "dec0", "ra1", "dec1", "ra2", "dec2", "ra3", "dec3",
                "ra4", "dec4")]
    if any(value is None for value in corners):
        raise GatheringError(
            f"rid {rid} has no complete sky position; the overlap query is a "
            f"cone search about the tile corners and cannot run without them")

    try:
        overlapping = handle.get_overlapping_l2files(
            rid, fid, mjdobs, *corners, radius_of_initial_cone_search=radius)
    except Exception as exc:  # noqa: BLE001
        raise GatheringError(
            f"overlap query failed for rid {rid} fid {fid}: {exc}") from exc

    rows: list[list[Any]] = []
    for image in overlapping or ():
        input_rid = int(image[0])
        field_from_overlap = image[11]
        info = handle.get_info_for_l2file(input_rid)
        code = getattr(handle, "exit_code", 0)
        if code >= 64:
            raise GatheringError(
                f"get_info_for_l2file failed for rid {input_rid}: "
                f"rapid_db exit_code {code}")
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
        raise GatheringError(
            f"rid {rid} has {len(rows)} coaddable inputs, fewer than the "
            f"{min_images_to_coadd} the release requires for a reference "
            f"image")
    return rows


def publish_coadd_inputs(s3_client, bucket: str, key: str,
                         rows: Iterable[Sequence[Any]]) -> str:
    """Write the coadd-inputs CSV to S3 and return its URI.

    The reference stage reads `coadd_inputs_uri`, splits the bucket off, and
    downloads the object — so the URI must name an object that exists before
    the job starts. Gathering publishes it because gathering is what knows the
    rows; the stage only consumes.
    """
    body = "\n".join(",".join("" if value is None else str(value)
                              for value in row)
                     for row in rows)
    if body:
        body += "\n"
    try:
        s3_client.put_object(Bucket=bucket, Key=key,
                             Body=body.encode("utf-8"),
                             ContentType="text/csv")
    except Exception as exc:  # noqa: BLE001
        raise GatheringError(
            f"could not publish the coadd inputs to s3://{bucket}/{key}: "
            f"{exc}") from exc
    return f"s3://{bucket}/{key}"


def gather_reference_units(handle: UnitSource, start, end,
                           start_mjdobs: float, end_mjdobs: float,
                           min_images_to_coadd: int,
                           s3_client: Any, job_bucket: str,
                           run_id: str,
                           fids: Iterable[int] | None = None,
                           radius: float | None = None
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
    field must not stop the others being submitted.
    """
    for unit in gather_science_units(handle, start, end, start_mjdobs,
                                     end_mjdobs, min_images_to_coadd,
                                     fids=fids, make_references=True):
        facts = unit.facts
        rid = facts.rid
        if rid is None:
            raise GatheringError(
                f"unit {unit.key} has no rid; the coadd inputs are aggregated "
                f"from the representative image's own overlap query")

        try:
            rows = coadd_input_rows(
                handle, int(rid), int(facts.fid), float(facts.mjdobs),
                facts.sky_position or {}, min_images_to_coadd, radius=radius)
        except GatheringError as exc:
            logger.info("no reference image for unit %s yet: %s",
                        unit.key, exc)
            continue

        key = (f"coadd-inputs/{run_id}/{unit.key}/"
               f"input_images_for_refimage_rid{int(rid)}.csv")
        uri = publish_coadd_inputs(s3_client, job_bucket, key, rows)
        logger.info("unit %s: %d coadd inputs at %s",
                    unit.key, len(rows), uri)

        yield dataclasses.replace(
            unit, facts=_replace(facts, coadd_inputs_uri=uri))


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

        # The job's own row, then the products it produced. `UnitFacts()` with
        # no arguments used to be yielded here — no product URIs, no database
        # identities — while `post_process.stamp_reference_image` requires
        # `reference_image_uri` and `stamp_difference_image` requires
        # `difference_image_uri`, `pid`, `rid`, `expid`, `fid` and `field` as
        # its first act. Every post-process job would have failed
        # `input_missing` before stamping either product.
        #
        # `get_job_record` was called behind a `hasattr` guard that was always
        # false against the real handle, because the method did not exist. It
        # does now; the guard is gone, so a handle that cannot answer is an
        # error rather than a silent fall back to the degenerate key.
        job = handle.get_job_record(jid)
        exposure, sca = _job_identity(job, jid)
        facts = post_process_facts(handle, job)

        yield ProcessingUnit(exposure=exposure, sca=sca,
                             facts=facts,
                             fields={"jid": jid,
                                     "job_type": JOB_TYPE_POST_PROCESS})


def post_process_facts(handle: Any, job: Any) -> UnitFacts:
    """The facts a post-process unit's stages require, from real queries.

    Post-process stamps identities into the reference and difference images
    this job produced, so it needs both products' URIs and the identities that
    go into their headers. Every one traces to a column: the Jobs row for the
    unit's own identity, DiffImages for the difference image and its pid, and
    RefImages (through the difference image's rfid) for the reference.

    A fact that cannot be resolved is left absent rather than defaulted, per
    the module's stated rule — `UnitFacts.require` turns that into one named
    failure at startup instead of a header stamped with a zero.
    """
    if job is None:
        return UnitFacts()

    # `get_job_record` returns the row tuple in its declared column order; a
    # mapping is accepted too, because that is what a stub naturally supplies
    # and the names are the same either way.
    # NOTE no `sca`: it identifies the processing UNIT (`ProcessingUnit.sca`),
    # not the facts, and `_job_identity` reads it from the same row for that
    # purpose. One home per fact.
    if isinstance(job, dict):
        expid, field, fid, rid = (
            _maybe_int(job.get(name))
            for name in ("expid", "field", "fid", "rid"))
    else:
        expid, field, fid, rid = (
            _maybe_int(job[index]) for index in (0, 2, 3, 4))
    facts = UnitFacts(expid=expid, field=field, fid=fid, rid=rid)

    if rid is None:
        return facts

    difference = handle.get_best_difference_image(rid, ppid_for(JOB_TYPE_SCIENCE))
    code = getattr(handle, "exit_code", 0)
    if code not in (0, 7):
        raise GatheringError(
            f"difference-image lookup failed for rid {rid}: "
            f"rapid_db exit_code {code}")
    if not difference:
        return facts

    facts = _replace(
        facts,
        pid=_maybe_int(difference.get("pid")),
        difference_image_uri=_maybe_str(difference.get("filename")),
        infobits=_maybe_int(difference.get("infobitssci")),
        difference_image_version=_maybe_int(difference.get("version")))

    # The reference this difference image was made against — named by the
    # difference image's own rfid, which is the only thing that knows WHICH
    # reference was used. Looking one up by field/filter instead could return a
    # newer reference than the one actually differenced against.
    rfid = _maybe_int(difference.get("rfid"))
    if rfid is None:
        return facts

    reference = handle.get_reference_image(rfid)
    if not reference:
        return _replace(facts, reference_image_id=rfid)
    return _replace(
        facts,
        reference_image_id=rfid,
        reference_image_uri=_maybe_str(reference.get("filename")),
        reference_image_infobits=_maybe_int(reference.get("infobits")),
        reference_image_version=_maybe_int(reference.get("version")))


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
