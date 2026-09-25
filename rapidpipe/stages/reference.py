"""`reference`: a field's reference image coadded from its selected frames, as `dev` does it.

Ported from `origin/dev`'s reference-image pipeline (ppid 12):
``pipeline/awsBatchSubmitJobs_runSingleReferenceImagePipeline.py`` and
``pipeline/referenceImageSubs.py`` (``generateReferenceImage``,
``generateSExtractorReferenceImageCatalog``,
``addKeywordsToReferenceImageHeader``, ``compute_cov5percent``), with the
mosaic geometry `dev`'s launcher
(``awsBatchSubmitJobs_launchSingleReferenceImagePipeline.py``) fills in.
The steps run in `dev`'s order, each one a function in
``rapidpipe.science.reference``:

per frame: gunzip -> HDU 1 -> DN/s -> scale to the filter's reference
zero point -> simple-model uncertainty (``prep``) -> awaicgen list files ->
awaicgen, tile-centred (``awaicgen``) -> cov5percent and the coverage,
uncertainty and image statistics (``measure``) -> SExtractor on the mosaic
with the uncertainty as weight, and its FWHM statistics (``catalog``) ->
the header stamp on the mosaic and its uncertainty image (``header``).

Unit: ``field``, id ``<rtid>/<filter>`` (R1), e.g. ``4711398/W146``. The
filter may be given in either spelling (``F146`` or ``W146``) and is
normalised to the RAPID spelling FITS ``FILTER`` headers and the
``filters`` table carry (`dev`'s ``roman_to_rapid_filter_names``); the
logical key and the registration block use the RAPID spelling. A bad unit
id is a usage error (64).

Inputs (R2). ``--inputs`` is an input-set manifest (unit kind ``field``)
listing N ``l2-image`` entries, member role ``image`` (the delivered
``.fits.gz``) -- the selection, in the order the launcher chose (`dev`:
``get_overlapping_l2files``, ordered by mjdobs then distance). Every
frame's HDU 1 ``FILTER``, normalised to the RAPID spelling, must equal the
unit's,
N must be at least ``[selection] min_frames``, and at most
``[selection] max_frames`` are coadded, in manifest order; frames past
that are named in the execution record's notes. Any other entry kind, a
missing or corrupt member, or an unreadable frame is an input rejection
(65). The stage reads no database and does not check overlap with the
field: selection is the launcher's.

Outputs (R5, R6). One ``reference-image`` entry: primary member ``image``
(``ref/awaicgen_output_mosaic_image.fits``), members ``coverage`` and
``uncertainty``; key ``{"field", "filter" (RAPID spelling), "recipe": "awaicgen",
"version": <selection digest, 64 hex>}``; the registration block R6 fixes
(:func:`registration_block`), with the SExtractor count named
``nsxcatsources`` as the ``refimmeta`` column is. ``zero_point`` is the
zero point the coadd was scaled to, also stamped as ``MAGZP``; the
difference stage today uses its own ``[awaicgen] zprefimg`` setting for
gain matching and does not read ``MAGZP`` (a residual the supervisor
records). All three bundle members are PRIMARY-HDU images carrying
awaicgen's output WCS unchanged (TAN, no PV/SIP: the difference stage
resamples the reference with SWarp assuming no distortion); the stamp
adds keywords to the image and uncertainty headers and never touches the
WCS. The catalog is written with the packaged
``cdf/rapidSexParamsRefImage.inp``, the parameter file the difference
stage reads it by (``FWHM_IMAGE``). One ``reference-catalog`` entry: member
``catalog`` (``ref/awaicgen_output_mosaic_refimsexcat.txt``), key
``{"reference": <instance>, "catalog_type": "sextractor"}``, registration
``md5``, ``status``, ``catalog_type``, ``source_count``. ``inputs.products``
names each coadded constituent as ``l2-image/001``, ``l2-image/002``, ...
(one kind may be read many times; the ``source-catalog/positive`` form
``load`` uses). The reformatted inputs and awaicgen's list files are
written to a scratch directory outside the output location and removed,
as `dev` never uploads them.

Departures from `dev`, each a ruling:

- ``FID`` is not stamped (R4): a database id; `register` derives ``fid``
  from the filter name. The run model's ``RPRUN``, ``RPATTMPT``,
  ``RPINST``, ``RPSTAGE`` are stamped instead, as finalize stamps them.
- The registration ``md5`` is the published (stamped) mosaic's. `dev`
  records the MD5 of the mosaic before its header is stamped and uploads
  the stamped file, so `dev`'s ``refimages.checksum`` never matches its
  own S3 object; the rebuild's matches the member it names.
- The Photutils reference catalog is not produced (R3, ``[psfcat]
  enabled = false``): it needs a reference PSF the input set does not
  carry; ``npucatsources`` is null. ``enabled = true`` is refused (64).
- Fake-source injection is not ported (a test-only branch in `dev`);
  ``[fake_sources] inject_fake_sources_flag = true`` is refused (64).
- `dev` downloads a PSF and the input-images CSV, writes
  ``input_images_used_for_refimage_jid<jid>.csv`` for ``refimimages``, and
  registers the rows itself; here the input set is the selection, the
  ordered ``constituents`` list replaces the CSV, and `register` writes
  the rows (R7).
- `dev` never checks a tool's exit code; this stage fails (70) when
  awaicgen or SExtractor leaves no output. A catalog with no sources (no
  FWHM) is rejected (65): `dev`'s ``nanmin`` raises there, and the
  difference stage cannot use such a reference.

This module may import ``rapidpipe.products``, ``rapidpipe.db``,
``rapidpipe.runs`` and ``rapidpipe.science``; never another stage,
``rapidpipe.launch`` or ``rapidpipe.cli``.
"""

from __future__ import annotations

import hashlib
import importlib
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rapidpipe.db.ids import new_ulid
from rapidpipe.products.manifest import Member, OutputEntry, member_for_file
from rapidpipe.science.difference.tools import ToolRunner
from rapidpipe.science.reference import awaicgen, catalog, header, identity, measure, prep
from rapidpipe.science.spatial import field_center
from rapidpipe.stages.contract import (
    InputRejected,
    StageContext,
    StageDeclaration,
    StageError,
    StageResult,
    UsageError,
    run_stage,
)

_SETTINGS_PATH = Path(__file__).resolve().parent.parent / "settings" / "reference.toml"

DECLARATION = StageDeclaration(
    name="reference",
    unit="field",
    argument_schema={
        "description": (
            "rapidpipe stage reference --run <run-id> --unit <rtid>/<filter> "
            "--attempt <attempt-id> --inputs <dir-or-s3-prefix> "
            "--outputs <dir-or-s3-prefix> [--settings <toml>] [--dry-run]. "
            "--inputs holds an input-set manifest.json (unit kind field) listing "
            "the l2-image entries to coadd, one filter, in selection order, with "
            "their member files."
        ),
    },
    settings_schema_path=str(_SETTINGS_PATH),
    consumes=("l2-image",),
    produces=("reference-image", "reference-catalog"),
    database_access="none",
    # A 7000x7000 float32 coadd of <= 25 4088x4088 frames: the difference
    # stage's 4 vCPUs, twice its memory for awaicgen's cell grid.
    resource_defaults={"vcpus": 4, "memory_mib": 32768},
)

#: `dev`'s cattype for the SExtractor reference catalog (refimcatalogs).
CATALOG_TYPE = "sextractor"
#: The registration status `dev` writes for a new reference and its catalog.
STATUS = 1
#: `dev` sets no infobits for a reference ("TODO" in generateReferenceImage).
INFOBITS = 0


# ----------------------------------------------------------------------
# Injectable tools
# ----------------------------------------------------------------------


@dataclass
class Toolkit:
    """The stage's boundary with external software, replaceable in tests.

    ``runner`` runs awaicgen and SExtractor (``ToolRunner``'s ``run``).
    """

    runner: Any = field(default_factory=ToolRunner)


#: Names a ``module:factory`` returning a :class:`Toolkit` to use instead
#: of the real tools. Unset in every deployment; the selftest sets it to
#: the fakes, since a stage run as a subprocess cannot be monkeypatched.
TOOLKIT_ENV = "RAPIDPIPE_REFERENCE_TOOLKIT"


def toolkit() -> Toolkit:
    """The real tools, unless ``RAPIDPIPE_REFERENCE_TOOLKIT`` names others."""
    override = os.environ.get(TOOLKIT_ENV)
    if not override:
        return Toolkit()
    module_name, _, factory_name = override.partition(":")
    try:
        factory = getattr(importlib.import_module(module_name), factory_name)
    except (ImportError, AttributeError) as exc:
        raise UsageError(f"{TOOLKIT_ENV}={override!r} does not name a factory: {exc}") from exc
    return factory()


# ----------------------------------------------------------------------
# Unit and settings
# ----------------------------------------------------------------------

_UNIT_RE = re.compile(r"^([1-9][0-9]*)/([A-Za-z0-9]+)$")


def parse_unit(unit_id: str) -> tuple[int, str]:
    """``<rtid>/<filter>`` -> ``(rtid, RAPID filter name)``; a bad id is a usage error (64).

    Either spelling of the filter is accepted and normalised (``F146`` ->
    ``W146``).
    """
    match = _UNIT_RE.match(unit_id)
    if match is None:
        raise UsageError(
            f"reference unit id must be '<rtid>/<filter>' (e.g. '4711398/W146'), got {unit_id!r}")
    return int(match.group(1)), prep.rapid_filter_name(match.group(2))


def _positive_int(settings: dict[str, Any], table: str, key: str) -> int:
    value = settings[table][key]
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise UsageError(f"[{table}] {key} must be a positive integer, got {value!r}")
    return value


def _number(settings: dict[str, Any], table: str, key: str) -> float:
    value = settings[table][key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UsageError(f"[{table}] {key} must be a number, got {value!r}")
    return float(value)


def _optional_degrees(settings: dict[str, Any], key: str) -> float | None:
    value = settings["mosaic"][key]
    if value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UsageError(f"[mosaic] {key} must be empty or a number of degrees, got {value!r}")
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


def check_settings(settings: dict[str, Any]) -> _Checked:
    """Validate the resolved settings; every problem is a usage error (64)."""
    if settings["fake_sources"]["inject_fake_sources_flag"]:
        raise UsageError(
            "[fake_sources] inject_fake_sources_flag = true is not supported: "
            "fake-source injection is not ported to the rebuild")
    if settings["psfcat"]["enabled"]:
        raise UsageError(
            "[psfcat] enabled = true is not supported: the Photutils reference catalog "
            "needs a reference PSF input and is not produced in this build")
    min_frames = _positive_int(settings, "selection", "min_frames")
    max_frames = _positive_int(settings, "selection", "max_frames")
    if max_frames < min_frames:
        raise UsageError(
            f"[selection] max_frames ({max_frames}) is less than min_frames ({min_frames})")
    ra_center = _optional_degrees(settings, "ra_center")
    dec_center = _optional_degrees(settings, "dec_center")
    if (ra_center is None) != (dec_center is None):
        raise UsageError("[mosaic] ra_center and dec_center must both be set or both empty")
    cdelt1 = _number(settings, "mosaic", "cdelt1")
    cdelt2 = _number(settings, "mosaic", "cdelt2")
    if cdelt1 == 0.0 or abs(abs(cdelt1) - abs(cdelt2)) > 1e-15:
        raise UsageError(
            f"[mosaic] cdelt1/cdelt2 must be non-zero and equal in size (awaicgen's pixels "
            f"are square), got {cdelt1!r}, {cdelt2!r}")
    pixelscale = _number(settings, "awaicgen", "awaicgen_pixelscale_absolute")
    if abs(abs(cdelt1) * 3600.0 - pixelscale) > 1e-6:
        raise UsageError(
            f"[mosaic] |cdelt1| ({cdelt1!r} deg) disagrees with [awaicgen] "
            f"awaicgen_pixelscale_absolute ({pixelscale!r} arcsec)")
    try:
        seed = int(settings["statistics"]["clip_correction_seed"])
    except (TypeError, ValueError) as exc:
        raise UsageError(f"[statistics] clip_correction_seed must be an integer: {exc}") from exc
    for key in ("awaicgen_output_mosaic_image_file", "awaicgen_output_mosaic_cov_map_file",
                "awaicgen_output_mosaic_uncert_image_file"):
        name = settings["awaicgen"][key]
        if not isinstance(name, str) or "/" in name or not name.endswith(".fits"):
            raise UsageError(f"[awaicgen] {key} must be a plain .fits file name, got {name!r}")
    if "image.fits" not in settings["awaicgen"]["awaicgen_output_mosaic_image_file"]:
        raise UsageError(
            "[awaicgen] awaicgen_output_mosaic_image_file must contain 'image.fits' "
            "(`dev` names the catalog by replacing it with 'refimsexcat.txt')")
    return _Checked(
        min_frames=min_frames, max_frames=max_frames,
        naxis1=_positive_int(settings, "mosaic", "naxis1"),
        naxis2=_positive_int(settings, "mosaic", "naxis2"),
        cdelt1=cdelt1, rotation=_number(settings, "mosaic", "rotation"),
        ra_center=ra_center, dec_center=dec_center,
        sca_gain=_number(settings, "instrument", "sca_gain"),
        sca_readout_noise=_number(settings, "instrument", "sca_readout_noise"),
        seed=seed if seed >= 0 else None)


# ----------------------------------------------------------------------
# Inputs
# ----------------------------------------------------------------------


def _sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _md5_of_file(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verified_member_path(inputs_dir: Path, member: Member) -> Path:
    path = inputs_dir / member.path
    if not path.exists():
        raise InputRejected(f"input member file not found: {path}")
    if path.stat().st_size != member.bytes:
        raise InputRejected(
            f"input member {member.path!r}: manifest declares {member.bytes} bytes, "
            f"file is {path.stat().st_size} bytes")
    if _sha256_of_file(path) != member.sha256.removeprefix("sha256:"):
        raise InputRejected(f"input member {member.path!r}: SHA-256 mismatch")
    return path


def _image_member(entry: OutputEntry) -> Member:
    members = [m for m in entry.members if m.role == "image"]
    if len(members) != 1:
        raise InputRejected(
            f"l2-image entry {entry.instance!r} has {len(members)} members with role 'image', "
            "expected exactly one")
    if entry.primary != members[0].path:
        raise InputRejected(
            f"l2-image entry {entry.instance!r}: the primary member is not its 'image' role")
    return members[0]


@dataclass(frozen=True)
class Frame:
    """One selected input: its instance, verified file and header values."""

    instance: str
    path: Path
    name: str
    header: prep.FrameHeader


@dataclass(frozen=True)
class Selection:
    coadded: list[Frame]
    not_coadded: list[str]    # instance ids past max_frames


def read_selection(context: StageContext, unit_filter: str, checked: _Checked) -> Selection:
    """Validate the input set against R2; every problem is an input rejection (65)."""
    manifest = context.input_manifest
    if manifest.unit.kind != "field":
        raise InputRejected(
            f"input manifest unit kind is {manifest.unit.kind!r}, expected 'field'")
    others = sorted({e.kind for e in manifest.outputs if e.kind != "l2-image"})
    if others:
        raise InputRejected(f"input manifest carries entries reference does not read: {others}")
    entries = list(manifest.outputs)
    if len(entries) < checked.min_frames:
        raise InputRejected(
            f"input set lists {len(entries)} l2-image entries; [selection] min_frames is "
            f"{checked.min_frames}")
    seen: set[str] = set()
    names: set[str] = set()
    frames: list[Frame] = []
    for entry in entries:
        if entry.instance in seen:
            raise InputRejected(f"l2-image instance {entry.instance!r} is listed twice")
        seen.add(entry.instance)
        member = _image_member(entry)
        path = _verified_member_path(context.inputs_dir, member)
        name = Path(member.path).name
        if name in names:
            raise InputRejected(f"two input frames share the file name {name!r}")
        names.add(name)
        try:
            frame_header = prep.read_frame_header(path)
        except KeyError as exc:
            raise InputRejected(f"l2-image {entry.instance!r} ({name}): HDU 1 lacks {exc}") from exc
        except Exception as exc:  # noqa: BLE001 -- astropy raises several types
            raise InputRejected(
                f"l2-image {entry.instance!r} ({name}) is not a readable two-HDU FITS file: "
                f"{exc}") from exc
        if (frame_header.filter is None
                or prep.rapid_filter_name(frame_header.filter) != unit_filter):
            raise InputRejected(
                f"l2-image {entry.instance!r} ({name}): FILTER {frame_header.filter!r} is not "
                f"the unit's filter {unit_filter!r}")
        if frame_header.exptime <= 0.0:
            raise InputRejected(
                f"l2-image {entry.instance!r} ({name}): EXPTIME {frame_header.exptime} is not "
                "positive")
        frames.append(Frame(instance=entry.instance, path=path, name=name, header=frame_header))
    return Selection(coadded=frames[:checked.max_frames],
                     not_coadded=[f.instance for f in frames[checked.max_frames:]])


# ----------------------------------------------------------------------
# Manifest entries
# ----------------------------------------------------------------------


def registration_block(*, md5: str, field_id: int, exposure_filter: str,
                       ra_center: float, dec_center: float, constituents: list[str],
                       mjdobs: list[float], jd_start: float, jd_end: float,
                       total_exptime: float, zero_point: float,
                       measurements: measure.MosaicMeasurements,
                       fwhm: catalog.FwhmStatistics, settings_hash: str) -> dict[str, Any]:
    """The reference-image registration block, exactly R6's fields."""
    block: dict[str, Any] = {
        "md5": md5,
        "status": STATUS,
        "infobits": INFOBITS,
        "field": int(field_id),
        "filter": str(exposure_filter),
        "ra_center": float(ra_center),
        "dec_center": float(dec_center),
        "constituents": list(constituents),
        "nframes": len(constituents),
        "mjdobs_min": float(min(mjdobs)),
        "mjdobs_max": float(max(mjdobs)),
        "jd_start": float(jd_start),
        "jd_end": float(jd_end),
        "total_exptime": float(total_exptime),
        "zero_point": float(zero_point),
    }
    block.update(measurements.to_dict())
    block.update({
        "fwhmmedpix": fwhm.fwhmmedpix,
        "fwhmminpix": fwhm.fwhmminpix,
        "fwhmmaxpix": fwhm.fwhmmaxpix,
        "nsxcatsources": fwhm.nsxcatsources,
        "npucatsources": None,
        "settings_hash": settings_hash,
    })
    return block


#: R6's reference-image registration fields, in order.
REGISTRATION_FIELDS = (
    "md5", "status", "infobits", "field", "filter", "ra_center", "dec_center",
    "constituents", "nframes", "mjdobs_min", "mjdobs_max", "jd_start", "jd_end",
    "total_exptime", "zero_point", "cov5percent", "medncov", "medpixunc", "npixnan",
    "clmean", "clstddev", "clnoutliers", "gmedian", "datascale", "gmin", "gmax",
    "fwhmmedpix", "fwhmminpix", "fwhmmaxpix", "nsxcatsources", "npucatsources",
    "settings_hash",
)

#: R6's reference-catalog registration fields.
CATALOG_REGISTRATION_FIELDS = ("md5", "status", "catalog_type", "source_count")


def products_read(constituents: list[str]) -> dict[str, str]:
    """``inputs.products``: each coadded constituent as ``l2-image/<nnn>``."""
    return {f"l2-image/{i:03d}": instance for i, instance in enumerate(constituents, start=1)}


# ----------------------------------------------------------------------
# The body
# ----------------------------------------------------------------------


def _scratch_root() -> Path:
    configured = os.environ.get("RAPIDPIPE_WORK")
    return Path(configured) if configured else Path(tempfile.gettempdir())


def _require_output(path: Path, tool: str, exit_code: int) -> None:
    if not path.is_file():
        raise StageError(f"{tool} exited {exit_code} and did not write {path.name}")


def _body(context: StageContext) -> StageResult:
    settings = context.settings
    rtid, unit_filter = parse_unit(context.unit_id)
    checked = check_settings(settings)
    selection = read_selection(context, unit_filter, checked)
    kit = toolkit()
    log = context.logger
    paths = settings["paths"]

    if checked.ra_center is None:
        ra_center, dec_center = field_center(rtid)
    else:
        ra_center, dec_center = checked.ra_center, checked.dec_center
    awaicgen_dict = awaicgen.awaicgen_dict_for_mosaic(
        settings["awaicgen"], naxis1=checked.naxis1, naxis2=checked.naxis2,
        cdelt1=checked.cdelt1, rotation=checked.rotation,
        ra_center=ra_center, dec_center=dec_center)

    out_dir = context.outputs_dir / paths["work_subdirectory"]
    out_dir.mkdir(parents=True, exist_ok=True)
    _scratch_root().mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix=f"rapidpipe-reference-{context.attempt_id}-",
                                    dir=str(_scratch_root())))
    try:
        # Per-frame preparation (dev: refimage_generation subdirectory).
        prepared: list[prep.PreparedFrame] = []
        jdstart, jdend, total_exptime = 999999999.0, 0.0, 0.0
        for frame in selection.coadded:
            shutil.copyfile(frame.path, scratch / frame.name)
            result = prep.reformat_frame(
                scratch, frame.name, awaicgen_dict, checked.sca_gain, checked.sca_readout_noise)
            total_exptime += result.exptime
            if result.jd < jdstart:
                jdstart = result.jd
            if result.jd > jdend:
                jdend = result.jd
            prepared.append(result)
        prep.write_list_files(scratch, awaicgen_dict, prepared, checked.max_frames)
        zprefimg = prepared[-1].zprefimg

        # awaicgen.
        args = awaicgen.build_awaicgen_command_line_args(awaicgen_dict, paths["awaicgen"])
        exit_code = int(kit.runner.run(args, cwd=scratch))
        names = {role: awaicgen_dict[key] for role, key in (
            ("image", "awaicgen_output_mosaic_image_file"),
            ("coverage", "awaicgen_output_mosaic_cov_map_file"),
            ("uncertainty", "awaicgen_output_mosaic_uncert_image_file"))}
        for name in names.values():
            _require_output(scratch / name, "awaicgen", exit_code)
            shutil.move(str(scratch / name), str(out_dir / name))
    except BaseException:
        log.info("reference: scratch directory kept at %s", scratch)
        raise
    shutil.rmtree(scratch, ignore_errors=True)

    def o(name: str) -> Path:
        return out_dir / name

    # Measurements, in dev's order.
    measurements = measure.measure_mosaic(
        o(names["image"]), o(names["coverage"]), o(names["uncertainty"]),
        sextractor_satur_level=float(settings["sextractor_refimage"]["sextractor_satur_level"]),
        seed=checked.seed)

    # SExtractor on the mosaic, the uncertainty as weight.
    catalog_name, sex_exit = catalog.generate_reference_image_catalog(
        kit.runner, out_dir, settings["sextractor_refimage"], paths["cfg_path"],
        names["image"], names["uncertainty"], executable=paths["sextractor"])
    _require_output(o(catalog_name), "SExtractor", sex_exit)
    try:
        fwhm = catalog.fwhm_statistics(o(catalog_name), paths["cfg_path"] + "/" + catalog.PARAMS_FILE)
    except ValueError as exc:
        raise InputRejected(
            f"reference catalog {catalog_name} has no sources with a usable FWHM_IMAGE "
            f"(the coadd of this selection yields no reference): {exc}") from exc

    # The header stamp, on the image and its uncertainty image.
    instance = new_ulid()
    exposure_filter = unit_filter   # RAPID spelling; every frame's FILTER normalises to it
    stamp = header.StampValues(
        field=rtid, exposure_filter=str(exposure_filter),
        cov5percent=measurements.cov5percent, nframes=len(prepared),
        refimage_input_filenames=tuple(f.name for f in selection.coadded),
        jdstart=jdstart, jdend=jdend, zprefimg=zprefimg,
        total_refimage_exptime=total_exptime,
        run=context.run_id, attempt=context.attempt_id, instance=instance)
    header.add_keywords_to_reference_image_header(o(names["image"]), stamp)
    header.add_keywords_to_reference_image_header(o(names["uncertainty"]), stamp)

    constituents = [f.instance for f in selection.coadded]
    settings_hash = "sha256:" + context.settings_hash
    digest = identity.selection_digest(constituents, context.settings_hash)
    members = tuple(
        member_for_file(role=role, path=o(names[role]), relative_to=context.outputs_dir)
        for role in ("image", "coverage", "uncertainty"))
    reference_entry = OutputEntry(
        kind="reference-image", format_version="1", instance=instance,
        key=identity.logical_key(field=rtid, filter_name=unit_filter, digest=digest),
        members=members, primary=members[0].path,
        registration=registration_block(
            md5=_md5_of_file(o(names["image"])), field_id=rtid,
            exposure_filter=str(exposure_filter), ra_center=ra_center, dec_center=dec_center,
            constituents=constituents, mjdobs=[f.header.mjdobs for f in selection.coadded],
            jd_start=jdstart, jd_end=jdend, total_exptime=total_exptime, zero_point=zprefimg,
            measurements=measurements, fwhm=fwhm, settings_hash=settings_hash))

    catalog_member = member_for_file(
        role="catalog", path=o(catalog_name), relative_to=context.outputs_dir)
    catalog_entry = OutputEntry(
        kind="reference-catalog", format_version="1", instance=new_ulid(),
        key={"reference": instance, "catalog_type": CATALOG_TYPE},
        members=(catalog_member,), primary=catalog_member.path,
        registration={"md5": _md5_of_file(o(catalog_name)), "status": STATUS,
                      "catalog_type": CATALOG_TYPE, "source_count": fwhm.nsxcatsources})

    log.info("reference: %s/%s coadded %d frames -> %s (version %s), %d catalog sources",
             rtid, unit_filter, len(constituents), instance, digest, fwhm.nsxcatsources)
    notes: dict[str, Any] = {}
    if selection.not_coadded:
        notes["not_coadded"] = selection.not_coadded
    return StageResult(outputs=[reference_entry, catalog_entry],
                       products_read=products_read(constituents), execution_notes=notes)


def main(argv: list[str]) -> int:
    return run_stage(DECLARATION, _body, argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
