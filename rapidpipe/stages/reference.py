# ported-from: pipeline/awsBatchSubmitJobs_runSingleReferenceImagePipeline.py, pipeline/referenceImageSubs.py, pipeline/awsBatchSubmitJobs_launchSingleReferenceImagePipeline.py @ 6458d3ca
"""``reference``: a field's reference image coadded from the L2 images its job names, as `dev` does it.

Ported from `origin/dev`'s reference-image pipeline (ppid 12):
``pipeline/awsBatchSubmitJobs_runSingleReferenceImagePipeline.py`` and
``pipeline/referenceImageSubs.py`` (``generateReferenceImage``,
``generateSExtractorReferenceImageCatalog``,
``addKeywordsToReferenceImageHeader``, ``compute_cov5percent``), with the
mosaic geometry `dev`'s launcher
(``awsBatchSubmitJobs_launchSingleReferenceImagePipeline.py``) fills in.
It runs as ``rapidpipe job local|run|submit --stage reference`` under
:class:`rapidpipe.foundation.local.JobContext`: ``run(job)`` reads what
its job's recorded inputs name, writes the files and ``manifest.json``
under ``job.path`` and returns; the caller publishes. The steps run in
`dev`'s order, each one a function in ``rapidpipe.science.reference``:

per frame: gunzip -> HDU 1 -> DN/s -> scale to the filter's reference
zero point -> simple-model uncertainty (``prep``) -> awaicgen list files ->
awaicgen, tile-centred (``awaicgen``) -> cov5percent and the coverage,
uncertainty and image statistics (``measure``) -> SExtractor on the mosaic
with the uncertainty as weight, and its FWHM statistics (``catalog``) ->
the InfoBits and status (``infobits``, dev cb53d2c7) -> the header stamp
on the mosaic and its uncertainty image (``header``).

Scope: ``{"field": <tessellation id>, "fid": <filter id>, "sca": <SCA>}``.
Settings: the run's ``[reference]`` table, whose subtables are `dev`'s
ini sections (``rapidpipe/settings/foundation-thin.toml``).

Inputs, every one an ``input`` row of product ``l2files`` whose key has the
scope's ``fid``, of any SCA (`dev`'s launcher takes every L2 file whose
``overlapfields`` list the field; the scope's ``sca`` is the first SCA the
reference serves): the frames, read through
:func:`rapidpipe.foundation.prefix.get` by the row's ``filename``, coadded
in ``mjdobs`` order (`dev`'s launcher orders by mjdobs, then distance). At
least ``[selection] min_frames``; frames past ``max_frames`` are not
coadded and are named in the manifest. Every frame's HDU 1 ``FILTER``,
normalised to the RAPID spelling, must be the scope's filter.

Outputs under ``job.path``: ``ref/`` holds the mosaic, its coverage and
uncertainty maps (awaicgen's names, beside one another, as the import
copies them) and the SExtractor catalog. Declared tables: ``refimages``
(one row, its ``status`` and ``infobits`` the grade; ``rfid`` is drawn
from the table's own identity sequence, so the child rows can carry it),
``refimimages`` (one row per coadded L2
row) and ``refimcatalogs`` (the mosaic catalog, ``cattype`` 1, which the
packet stage reads for reference-star matches). The manifest also records
`dev`'s refimmeta measurements under ``reference`` and ``metrics``, one
flat dict of named numbers (the compact stars' width, the coverage
histogram, the extremes, awaicgen's wall seconds; :func:`rapidpipe.science.reference.measure.quality_metrics`).

Departures from `dev` (reference page):

- ``FID`` is not stamped; the row carries ``fid``. The foundation's
  ``RPRUN``, ``RPJOB``, ``RPATTMPT`` and ``RPSTAGE`` are stamped instead.
- The row's ``checksum`` is the published (stamped) mosaic's MD5. `dev`
  records the MD5 before its header is stamped, so its checksum never
  matches its own object.
- The Photutils reference catalog is not produced (``[psfcat] enabled =
  false``; ``true`` is refused), so the grade sets none of its bits (5, 6,
  7, 16); fake-source injection is not ported (``true`` is refused).
- A status-0 reference is published and differenced against, as `dev`'s
  science pipeline differences against the reference it has just built
  whatever its status; `dev` then never reuses it (``get_best_reference_image``
  selects ``status > 0``), and the loop builds a field's reference once,
  so the status is a recorded verdict here, not a gate.
- `dev` never checks a tool's exit code; this stage fails when awaicgen
  or SExtractor leaves no output, and when the catalog has no source
  with a usable FWHM (`dev`'s ``nanmin`` raises there).

This module may import ``rapidpipe.products``, ``rapidpipe.db``,
``rapidpipe.foundation`` and ``rapidpipe.science``; never another stage,
``rapidpipe.launch`` or ``rapidpipe.cli``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from rapidpipe.foundation.local import JobContext
from rapidpipe.foundation.prefix import get
from rapidpipe.science.difference.sextractor import parse_ascii_text_sextractor_catalog
from rapidpipe.science.difference.tools import ToolRunner
from rapidpipe.science.reference import awaicgen, catalog, header, infobits, measure, prep
from rapidpipe.science.spatial import field_center

logger = logging.getLogger(__name__)

#: `refimcatalogs.cattype` of the mosaic SExtractor catalog (as dev registers it).
CATALOG_TYPE = 1
REFIMAGES_SCHEMA = pa.schema([
    ("rfid", pa.int32()), ("dataset", pa.string()), ("field", pa.int32()), ("fid", pa.int16()),
    ("filename", pa.string()), ("checksum", pa.string()), ("status", pa.int16()),
    ("infobits", pa.int32()), ("nframes", pa.int16()), ("cov5percent", pa.float32()),
])
REFIMIMAGES_SCHEMA = pa.schema([("rfid", pa.int32()), ("rid", pa.int32())])
REFIMCATALOGS_SCHEMA = pa.schema([
    ("rfid", pa.int32()), ("cattype", pa.int16()), ("filename", pa.string()),
    ("checksum", pa.string()),
])


@dataclass
class Toolkit:
    """The stage's boundary with external software; a test replaces :func:`toolkit`.

    ``runner`` runs awaicgen and SExtractor (``ToolRunner``'s ``run``).
    """

    runner: Any = field(default_factory=ToolRunner)


def toolkit() -> Toolkit:
    return Toolkit()


# ----------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------


def _positive_int(settings: dict[str, Any], table: str, key: str) -> int:
    value = settings[table][key]
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"[reference.{table}] {key} must be a positive integer, got {value!r}")
    return value


def _number(settings: dict[str, Any], table: str, key: str) -> float:
    value = settings[table][key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"[reference.{table}] {key} must be a number, got {value!r}")
    return float(value)


def _optional_degrees(settings: dict[str, Any], key: str) -> float | None:
    value = settings["mosaic"][key]
    if value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"[reference.mosaic] {key} must be empty or a number of degrees, "
                         f"got {value!r}")
    return float(value)


@dataclass(frozen=True)
class _Checked:
    min_frames: int
    max_frames: int
    naxis1: int
    naxis2: int
    cdelt1: float
    rotation: float
    ra_center: float | None
    dec_center: float | None
    sca_gain: float
    sca_readout_noise: float
    seed: int | None
    infobits: dict[str, Any]


def check_settings(settings: dict[str, Any]) -> _Checked:
    """Validate the ``[reference]`` table; every problem is a ``ValueError``."""
    if settings["fake_sources"]["inject_fake_sources_flag"]:
        raise ValueError(
            "[reference.fake_sources] inject_fake_sources_flag = true is not supported: "
            "fake-source injection is not ported to the rebuild")
    if settings["psfcat"]["enabled"]:
        raise ValueError(
            "[reference.psfcat] enabled = true is not supported: the Photutils reference "
            "catalog needs a reference PSF input and is not produced in this build")
    min_frames = _positive_int(settings, "selection", "min_frames")
    max_frames = _positive_int(settings, "selection", "max_frames")
    if max_frames < min_frames:
        raise ValueError(f"[reference.selection] max_frames ({max_frames}) is less than "
                         f"min_frames ({min_frames})")
    ra_center = _optional_degrees(settings, "ra_center")
    dec_center = _optional_degrees(settings, "dec_center")
    if (ra_center is None) != (dec_center is None):
        raise ValueError("[reference.mosaic] ra_center and dec_center must both be set or both empty")
    cdelt1 = _number(settings, "mosaic", "cdelt1")
    cdelt2 = _number(settings, "mosaic", "cdelt2")
    if cdelt1 == 0.0 or abs(abs(cdelt1) - abs(cdelt2)) > 1e-15:
        raise ValueError(
            f"[reference.mosaic] cdelt1/cdelt2 must be non-zero and equal in size (awaicgen's "
            f"pixels are square), got {cdelt1!r}, {cdelt2!r}")
    pixelscale = _number(settings, "awaicgen", "awaicgen_pixelscale_absolute")
    if abs(abs(cdelt1) * 3600.0 - pixelscale) > 1e-6:
        raise ValueError(
            f"[reference.mosaic] |cdelt1| ({cdelt1!r} deg) disagrees with [reference.awaicgen] "
            f"awaicgen_pixelscale_absolute ({pixelscale!r} arcsec)")
    try:
        seed = int(settings["statistics"]["clip_correction_seed"])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"[reference.statistics] clip_correction_seed must be an integer: "
                         f"{exc}") from exc
    for key in ("awaicgen_output_mosaic_image_file", "awaicgen_output_mosaic_cov_map_file",
                "awaicgen_output_mosaic_uncert_image_file"):
        name = settings["awaicgen"][key]
        if not isinstance(name, str) or "/" in name or not name.endswith(".fits"):
            raise ValueError(f"[reference.awaicgen] {key} must be a plain .fits file name, "
                             f"got {name!r}")
    if "image.fits" not in settings["awaicgen"]["awaicgen_output_mosaic_image_file"]:
        raise ValueError(
            "[reference.awaicgen] awaicgen_output_mosaic_image_file must contain 'image.fits' "
            "(`dev` names the catalog by replacing it with 'refimsexcat.txt')")
    return _Checked(
        min_frames=min_frames, max_frames=max_frames,
        naxis1=_positive_int(settings, "mosaic", "naxis1"),
        naxis2=_positive_int(settings, "mosaic", "naxis2"),
        cdelt1=cdelt1, rotation=_number(settings, "mosaic", "rotation"),
        ra_center=ra_center, dec_center=dec_center,
        sca_gain=_number(settings, "instrument", "sca_gain"),
        sca_readout_noise=_number(settings, "instrument", "sca_readout_noise"),
        seed=seed if seed >= 0 else None,
        infobits=infobits.check_params(settings["infobits"]))


# ----------------------------------------------------------------------
# Inputs
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Frame:
    """One named L2 input: its row's ``rid``, its local file and HDU 1's values."""

    rid: int
    path: Path
    name: str
    header: prep.FrameHeader


def _read(job: JobContext) -> tuple[str, list[tuple[int, str]], int]:
    """The scope's filter name, each named L2 row's ``(rid, filename)``, and a new ``rfid``."""
    fid = int(job.scope["fid"])
    l2_inputs = [i for i in job.inputs if i["kind"] == "input" and i["product"] == "l2files"]
    others = sorted({i["product"] for i in job.inputs if i["kind"] == "input"} - {"l2files"})
    if others:
        raise ValueError(f"job {job.job}: reference reads l2files inputs only, not {others}")
    rows = []
    with job.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT filter FROM filters WHERE fid = %s", (fid,))
        found = cur.fetchone()
        if found is None:
            raise ValueError(f"job {job.job}: fid {fid} is not in filters")
        for item in l2_inputs:
            key = item["key"]
            if int(key["fid"]) != fid:
                raise ValueError(f"job {job.job}: l2files input {key} is not the scope's fid {fid}")
            cur.execute("SELECT rid, filename FROM l2files WHERE job = %s AND dataset = %s"
                        " AND expid = %s AND sca = %s AND fid = %s",
                        (item["source_job"], key["dataset"], key["expid"], int(key["sca"]), fid))
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"job {job.job}: no l2files row {key} in job {item['source_job']}")
            rows.append((int(row[0]), row[1]))
        cur.execute("SELECT nextval(pg_get_serial_sequence('refimages', 'rfid'))")
        rfid = int(cur.fetchone()[0])
        conn.commit()
    return found[0], rows, rfid


def read_frames(rows: list[tuple[int, str]], unit_filter: str,
                checked: _Checked) -> tuple[list[Frame], list[int]]:
    """The frames to coadd in ``mjdobs`` order, and the ``rid``\\ s past ``max_frames``."""
    if len(rows) < checked.min_frames:
        raise ValueError(f"{len(rows)} l2files inputs; [reference.selection] min_frames is "
                         f"{checked.min_frames}")
    frames: list[Frame] = []
    for rid, filename in rows:
        path = get(filename)
        try:
            frame_header = prep.read_frame_header(path)
        except Exception as exc:  # noqa: BLE001 -- astropy raises several types
            raise ValueError(f"l2files rid {rid} ({filename}) is not a readable two-HDU FITS "
                             f"file with EXPTIME, ZPTMAG and MJD-OBS: {exc!r}") from exc
        if (frame_header.filter is None
                or prep.rapid_filter_name(frame_header.filter) != unit_filter):
            raise ValueError(f"l2files rid {rid}: FILTER {frame_header.filter!r} is not "
                             f"{unit_filter!r}")
        if frame_header.exptime <= 0.0:
            raise ValueError(f"l2files rid {rid}: EXPTIME {frame_header.exptime} is not positive")
        frames.append(Frame(rid=rid, path=path, name=filename.rsplit("/", 1)[-1],
                            header=frame_header))
    frames.sort(key=lambda f: (f.header.mjdobs, f.rid))
    if len({f.name for f in frames}) != len(frames):
        raise ValueError("two l2files inputs share a file name")
    return frames[:checked.max_frames], [f.rid for f in frames[checked.max_frames:]]


# ----------------------------------------------------------------------
# The body
# ----------------------------------------------------------------------


def _md5_of_file(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _scratch_root() -> Path:
    configured = os.environ.get("RAPIDPIPE_WORK")
    return Path(configured) if configured else Path(tempfile.gettempdir())


def _require_output(path: Path, tool: str, exit_code: int) -> None:
    if not path.is_file():
        raise RuntimeError(f"{tool} exited {exit_code} and did not write {path.name}")


def run(job: JobContext) -> None:
    settings = job.settings["reference"]
    rtid = int(job.scope["field"])
    checked = check_settings(settings)
    unit_filter, rows, rfid = _read(job)
    coadded, not_coadded = read_frames(rows, unit_filter, checked)
    kit = toolkit()
    paths = settings["paths"]

    if checked.ra_center is None:
        ra_center, dec_center = field_center(rtid)
    else:
        ra_center, dec_center = checked.ra_center, checked.dec_center
    awaicgen_dict = awaicgen.awaicgen_dict_for_mosaic(
        settings["awaicgen"], naxis1=checked.naxis1, naxis2=checked.naxis2,
        cdelt1=checked.cdelt1, rotation=checked.rotation,
        ra_center=ra_center, dec_center=dec_center)

    out_dir = job.path / paths["work_subdirectory"]
    out_dir.mkdir(parents=True, exist_ok=True)
    _scratch_root().mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix=f"rapidpipe-reference-{job.job}-{job.attempt}-",
                                    dir=str(_scratch_root())))
    try:
        # Per-frame preparation (dev: refimage_generation subdirectory).
        prepared: list[prep.PreparedFrame] = []
        jdstart, jdend, total_exptime = 999999999.0, 0.0, 0.0
        for frame in coadded:
            shutil.copyfile(frame.path, scratch / frame.name)
            result = prep.reformat_frame(
                scratch, frame.name, awaicgen_dict, checked.sca_gain, checked.sca_readout_noise)
            total_exptime += result.exptime
            jdstart, jdend = min(jdstart, result.jd), max(jdend, result.jd)
            prepared.append(result)
        prep.write_list_files(scratch, awaicgen_dict, prepared, checked.max_frames)
        zprefimg = prepared[-1].zprefimg

        args = awaicgen.build_awaicgen_command_line_args(awaicgen_dict, paths["awaicgen"])
        started = time.monotonic()
        exit_code = int(kit.runner.run(args, cwd=scratch))
        awaicgen_seconds = time.monotonic() - started
        names = {role: awaicgen_dict[key] for role, key in (
            ("image", "awaicgen_output_mosaic_image_file"),
            ("coverage", "awaicgen_output_mosaic_cov_map_file"),
            ("uncertainty", "awaicgen_output_mosaic_uncert_image_file"))}
        for name in names.values():
            _require_output(scratch / name, "awaicgen", exit_code)
            shutil.move(str(scratch / name), str(out_dir / name))
    except BaseException:
        logger.info("reference: scratch directory kept at %s", scratch)
        raise
    shutil.rmtree(scratch, ignore_errors=True)

    def o(name: str) -> Path:
        return out_dir / name

    measurements = measure.measure_mosaic(
        o(names["image"]), o(names["coverage"]), o(names["uncertainty"]),
        sextractor_satur_level=float(settings["sextractor_refimage"]["sextractor_satur_level"]),
        seed=checked.seed)

    catalog_name, sex_exit = catalog.generate_reference_image_catalog(
        kit.runner, out_dir, settings["sextractor_refimage"], paths["cfg_path"],
        names["image"], names["uncertainty"], executable=paths["sextractor"])
    _require_output(o(catalog_name), "SExtractor", sex_exit)
    try:
        fwhm = catalog.fwhm_statistics(o(catalog_name), paths["cfg_path"] + "/" + catalog.PARAMS_FILE,
                                       checked.infobits["fwhm_percentile"])
    except ValueError as exc:
        raise ValueError(f"reference catalog {catalog_name} has no sources with a usable "
                         f"FWHM_IMAGE (the coadd of these frames yields no reference): {exc}") from exc

    metrics = measure.quality_metrics(
        o(names["image"]), o(names["coverage"]),
        parse_ascii_text_sextractor_catalog(
            o(catalog_name), paths["cfg_path"] + "/" + catalog.PARAMS_FILE,
            ["XWIN_IMAGE", "YWIN_IMAGE", "FLAGS", "FLUX_AUTO", "FLUXERR_AUTO"]),
        measurements)
    metrics["awaicgen_seconds"] = round(awaicgen_seconds, 1)

    grade = infobits.grade(
        o(names["image"]), o(names["coverage"]),
        paths["cfg_path"] + "/" + infobits.DEFINITIONS_FILE, checked.infobits,
        gmedian=measurements.gmedian, datascale=measurements.datascale,
        satcount=measurements.npixsat, fwhm_mode_pix=fwhm.fwhmmedpix,
        fwhm_pctl_pix=fwhm.fwhmpctlpix, nsexcat=fwhm.nsexcatsources,
        input_zptmags=[p.zptmag for p in prepared])
    logger.info("reference: InfoBits %d (bits %s), status %d (fatal bits %s); metrics %s",
                grade.infobits, grade.bits, grade.status, checked.infobits["fatalinfobits"],
                grade.metrics)

    stamp = header.StampValues(
        field=rtid, exposure_filter=unit_filter, cov5percent=measurements.cov5percent,
        nframes=len(prepared), refimage_input_filenames=tuple(f.name for f in coadded),
        jdstart=jdstart, jdend=jdend, zprefimg=zprefimg, total_refimage_exptime=total_exptime,
        run=job.run, job=job.job, attempt=job.attempt,
        infobits=grade.infobits, status=grade.status, infobit_cards=tuple(grade.cards))
    header.add_keywords_to_reference_image_header(o(names["image"]), stamp)
    header.add_keywords_to_reference_image_header(o(names["uncertainty"]), stamp)

    def url(name: str) -> str:
        return job.prefix + o(name).relative_to(job.path).as_posix()

    tables = {
        "refimages": pa.table({
            "rfid": [rfid], "dataset": [job.dataset], "field": [rtid],
            "fid": [int(job.scope["fid"])], "filename": [url(names["image"])],
            "checksum": [_md5_of_file(o(names["image"]))], "status": [grade.status],
            "infobits": [grade.infobits], "nframes": [len(prepared)],
            "cov5percent": [measurements.cov5percent]}, schema=REFIMAGES_SCHEMA),
        "refimimages": pa.table({"rfid": [rfid] * len(coadded), "rid": [f.rid for f in coadded]},
                                schema=REFIMIMAGES_SCHEMA),
        "refimcatalogs": pa.table({
            "rfid": [rfid], "cattype": [CATALOG_TYPE], "filename": [url(catalog_name)],
            "checksum": [_md5_of_file(o(catalog_name))]}, schema=REFIMCATALOGS_SCHEMA),
    }
    for table, data in tables.items():
        pq.write_table(data, job.path / f"{table}.parquet")
    logger.info("reference: field %s %s coadded %d frames -> rfid %s, %d catalog sources",
                rtid, unit_filter, len(coadded), rfid, fwhm.nsexcatsources)
    job.write_manifest(
        [(table, f"{table}.parquet") for table in tables],
        reference={"rfid": rfid, "ra_center": ra_center, "dec_center": dec_center,
                   "jd_start": jdstart, "jd_end": jdend, "total_exptime": total_exptime,
                   "zero_point": zprefimg, **measurements.to_dict(),
                   "fwhmmedpix": fwhm.fwhmmedpix, "fwhmminpix": fwhm.fwhmminpix,
                   "fwhmmaxpix": fwhm.fwhmmaxpix, "nsexcatsources": fwhm.nsexcatsources,
                   "fwhmpctlpix": fwhm.fwhmpctlpix, "infobits": grade.infobits,
                   "status": grade.status, "quality": grade.metrics,
                   "not_coadded": not_coadded},
        metrics=metrics)
