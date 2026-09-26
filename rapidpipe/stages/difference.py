"""`difference`: an l2 image minus its reference, ZOGY first, as `dev` does it.

Ported from `origin/dev`'s ``pipeline/awsBatchSubmitJobs_runSingleSciencePipeline.py``
(its science only: the S3 transfers, the job-config parsing and the
reference-image generation branch are not the stage's job). The steps run
in `dev`'s order, each one a function in ``rapidpipe.science.difference``:

reformat and simple-model uncertainty -> SExtractor science catalog and
FWHM -> SIP to PV and three SWarp resamplings of the reference bundle ->
PSF normalisation -> bkgest -> gain matching (``scalefacref``, x/y RMS and
medians) -> NaN replacement and artifact repair -> subpixel offset of the
reference by the medians -> ZOGY -> coverage mask, NaN restore, negation ->
difference uncertainty -> SExtractor and Photutils catalogs, positive and
negative. SFFT and the naive subtraction follow ZOGY and run as `dev`
runs them (``[sfft] run_sfft``, ``[naive_diffimage] naive_diffimage_flag``).

Inputs. The stage's ``--inputs`` manifest is an input set: its
``outputs`` list the products this attempt consumes, each with its member
files beside the manifest --

- one ``l2-image`` entry (``admit``'s, with its registration block),
- one ``reference-image`` entry, members ``image``, ``coverage``,
  ``uncertainty``; registration ``{"infobits": int, "rfid": int|null}``,
  ``rfid`` naming the legacy ``refimages`` row of a reference registered
  by `dev` (no instance),
- one ``reference-catalog`` entry (the reference's SExtractor catalog,
  role ``catalog``; `dev` reads its FWHM column),
- two ``psf`` entries, keys carrying ``"applies_to": "science"`` or
  ``"reference"`` (role ``psf``).

Outputs. One ``difference-image`` instance for ZOGY, registered as in
`dev`; an SFFT instance only when ``[sfft] register_sfft`` is on and SFFT
succeeded (never a partial bundle); ``source-catalog`` entries for each
registered instance's catalogs. Every working file stays in
``[paths] work_subdirectory`` of the attempt's output location, as `dev`
uploads its intermediates for diagnosis; the naive subtraction's files
are among them and are never registered. A non-fatal SFFT failure is noted
in the execution record.

This module may import ``rapidpipe.products``, ``rapidpipe.db``,
``rapidpipe.runs`` and ``rapidpipe.science``; never another stage,
``rapidpipe.launch`` or ``rapidpipe.cli``.
"""

from __future__ import annotations

import gzip
import hashlib
import importlib
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from astropy.io import fits

from rapidpipe.db.ids import new_ulid
from rapidpipe.products.diffimage import (
    DIFFERENCERS,
    DifferenceImageRegistration,
    DifferenceImageRegistrationError,
    catalog_outcome_bit,
)
from rapidpipe.products.l2image import L2ImageRegistration, L2ImageRegistrationError
from rapidpipe.products.manifest import Manifest, Member, OutputEntry, member_for_file
from rapidpipe.science.difference import (
    background,
    gainmatch,
    masking,
    naive,
    offsets,
    psf,
    psfcat,
    reformat,
    repair,
    resample,
    sextractor,
    sfft,
    statistics,
    uncertainty,
    zogy,
)
from rapidpipe.science.difference.fitsops import scale_image_data
from rapidpipe.science.difference.tools import ToolRunner
from rapidpipe.stages.contract import (
    InputRejected,
    StageContext,
    StageDeclaration,
    StageResult,
    UsageError,
    run_stage,
)

_SETTINGS_PATH = Path(__file__).resolve().parent.parent / "settings" / "difference.toml"

DECLARATION = StageDeclaration(
    name="difference",
    unit="detector-image",
    argument_schema={
        "description": (
            "rapidpipe stage difference --run <run-id> --unit <unit-id> "
            "--attempt <attempt-id> --inputs <dir> --outputs <dir> "
            "[--settings <toml>] [--dry-run]. --inputs holds an input-set "
            "manifest.json listing one l2-image, one reference-image, one "
            "reference-catalog and two psf entries (applies_to science and "
            "reference), with their member files."
        ),
    },
    settings_schema_path=str(_SETTINGS_PATH),
    consumes=("l2-image", "reference-image", "reference-catalog", "psf"),
    produces=("difference-image", "source-catalog"),
    database_access="none",
    resource_defaults={"vcpus": 4, "memory_mib": 16384},
)


# ----------------------------------------------------------------------
# Injectable tools
# ----------------------------------------------------------------------


@dataclass
class Toolkit:
    """The stage's boundary with external software, replaceable in tests.

    ``runner`` runs SExtractor, SWarp, bkgest, py_zogy.py and SFFT;
    ``sip_to_pv`` rewrites a FITS header's SIP distortion as PV in place;
    ``psf_catalog`` makes one Photutils catalog
    (:func:`rapidpipe.science.difference.psfcat.psf_catalog`'s signature).
    """

    runner: Any = field(default_factory=ToolRunner)
    sip_to_pv: Callable = resample._default_sip_to_pv
    psf_catalog: Callable = psfcat.psf_catalog


#: Names a ``module:factory`` returning a :class:`Toolkit` to use instead
#: of the real tools. Unset in every deployment; the stage fixture
#: (``make stage-difference``) and the ``run local`` smoke test set it to
#: the fakes, since a stage run as a subprocess cannot be monkeypatched.
TOOLKIT_ENV = "RAPIDPIPE_DIFFERENCE_TOOLKIT"


def toolkit() -> Toolkit:
    """The real tools, unless ``RAPIDPIPE_DIFFERENCE_TOOLKIT`` names others.

    In-process tests monkeypatch this name instead.
    """
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
# Inputs
# ----------------------------------------------------------------------


@dataclass
class _InputSet:
    l2: OutputEntry
    l2_registration: L2ImageRegistration
    reference: OutputEntry
    reference_infobits: int
    reference_rfid: int | None
    science_image: str          # file names in the work directory
    reference_image: str
    reference_coverage: str
    reference_uncertainty: str
    reference_catalog: str
    science_psf: str
    reference_psf: str


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


def _one(manifest: Manifest, kind: str) -> OutputEntry:
    entries = [e for e in manifest.outputs if e.kind == kind]
    if len(entries) != 1:
        raise InputRejected(
            f"input manifest has {len(entries)} {kind!r} entries, expected exactly one")
    return entries[0]


def _member(entry: OutputEntry, role: str) -> Member:
    members = [m for m in entry.members if m.role == role]
    if len(members) != 1:
        raise InputRejected(
            f"{entry.kind} entry {entry.instance!r} has {len(members)} members "
            f"with role {role!r}, expected exactly one")
    return members[0]


def _stage_input(context: StageContext, member: Member, work_dir: Path,
                 taken: set[str]) -> str:
    """Verify one input member and copy it into the work directory by base name."""
    source = _verified_member_path(context.inputs_dir, member)
    name = Path(member.path).name
    if name in taken:
        raise InputRejected(f"two input members share the file name {name!r}")
    taken.add(name)
    shutil.copyfile(source, work_dir / name)
    return name


def _read_input_set(context: StageContext, work_dir: Path) -> _InputSet:
    manifest = context.input_manifest
    if manifest.unit.kind != "detector-image":
        raise InputRejected(
            f"input manifest unit kind is {manifest.unit.kind!r}, expected 'detector-image'")

    l2 = _one(manifest, "l2-image")
    try:
        l2_registration = L2ImageRegistration.from_dict(l2.registration)
        l2_registration.validate()
    except (L2ImageRegistrationError, TypeError) as exc:
        raise InputRejected(f"l2-image registration block: {exc}") from exc

    reference = _one(manifest, "reference-image")
    ref_registration = reference.registration
    infobits = ref_registration.get("infobits")
    rfid = ref_registration.get("rfid")
    if not isinstance(infobits, int) or isinstance(infobits, bool) or infobits < 0:
        raise InputRejected(
            f"reference-image registration 'infobits' must be a non-negative integer, "
            f"got {infobits!r}")
    if rfid is not None and (not isinstance(rfid, int) or isinstance(rfid, bool) or rfid <= 0):
        raise InputRejected(
            f"reference-image registration 'rfid' must be a positive integer or null, got {rfid!r}")

    reference_catalog = _one(manifest, "reference-catalog")

    psfs = [e for e in manifest.outputs if e.kind == "psf"]
    by_use = {e.key.get("applies_to"): e for e in psfs}
    if len(psfs) != 2 or set(by_use) != {"science", "reference"}:
        raise InputRejected(
            "input manifest must carry exactly two psf entries, keyed applies_to "
            f"'science' and 'reference'; got {sorted(str(k) for k in by_use)}")

    taken: set[str] = set()
    science_image = _stage_input(context, _member(l2, "image"), work_dir, taken)
    if l2.primary != _member(l2, "image").path:
        raise InputRejected("the l2-image entry's primary member is not its 'image' role")

    return _InputSet(
        l2=l2,
        l2_registration=l2_registration,
        reference=reference,
        reference_infobits=infobits,
        reference_rfid=rfid,
        science_image=science_image,
        reference_image=_stage_input(context, _member(reference, "image"), work_dir, taken),
        reference_coverage=_stage_input(context, _member(reference, "coverage"), work_dir, taken),
        reference_uncertainty=_stage_input(
            context, _member(reference, "uncertainty"), work_dir, taken),
        reference_catalog=_stage_input(
            context, _member(reference_catalog, "catalog"), work_dir, taken),
        science_psf=_stage_input(context, _member(by_use["science"], "psf"), work_dir, taken),
        reference_psf=_stage_input(context, _member(by_use["reference"], "psf"), work_dir, taken),
    )


def _unzip_science_image(work_dir: Path, name: str) -> str:
    """`dev` gunzips a ``.fits.gz`` delivery unless the ``.fits`` already exists."""
    if not name.endswith(".fits.gz"):
        return name
    unzipped = name.replace(".fits.gz", ".fits")
    if not (work_dir / unzipped).exists():
        with gzip.open(work_dir / name, "rb") as src, (work_dir / unzipped).open("wb") as dst:
            shutil.copyfileobj(src, dst)
        (work_dir / name).unlink()
    return unzipped


# ----------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------


def _check_settings(settings: dict[str, Any]) -> None:
    if settings["fake_sources"]["inject_fake_sources_flag"]:
        raise UsageError(
            "[fake_sources] inject_fake_sources_flag = true is not supported: "
            "fake-source injection is not ported to the rebuild")
    _zero_point_override(settings)
    for differencer, table in (("zogy", "zogy"), ("sfft", "sfft")):
        role = settings[table]["detection_role"]
        if role not in DIFFERENCERS[differencer].declared:
            raise UsageError(
                f"[{table}] detection_role {role!r} is not a {differencer} bundle role; "
                f"declared: {list(DIFFERENCERS[differencer].declared)}")


def _seed(settings: dict[str, Any]) -> int | None:
    seed = int(settings["statistics"]["clip_correction_seed"])
    return seed if seed >= 0 else None


def _zero_point_override(settings: dict[str, Any]) -> float | None:
    """``[awaicgen] zprefimg``: empty (the default) means no override, so
    the reference zero point comes from the reference image's own MAGZP
    header keyword; otherwise the number to use instead, unconditionally
    (the lead, 2026-09-26)."""
    value = settings["awaicgen"]["zprefimg"]
    if value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UsageError(f"[awaicgen] zprefimg must be empty or a number, got {value!r}")
    return float(value)


def _reference_header_magzp(work_dir: Path, reference_image: str) -> float | None:
    """The reference image's own ``MAGZP``, read from its primary header.

    Reads the original reference input file (as delivered, before SWarp
    resamples it onto the science grid): the resampled copy's header is
    the science image's, carried through by SWarp, and ``[swarp]
    swarp_copy_keywords`` never names ``MAGZP``, so the resampled file
    never carries it.
    """
    with fits.open(work_dir / reference_image) as hdul:
        header = hdul[0].header
        if "MAGZP" not in header:
            return None
        return float(header["MAGZP"])


def _resolve_zero_point(
    settings: dict[str, Any], work_dir: Path, reference_image: str, log: Any,
) -> tuple[float, str]:
    """The reference zero point gain matching uses, and where it came from.

    An explicit ``[awaicgen] zprefimg`` override always wins; absent that,
    the reference image's ``MAGZP`` header keyword is read. Neither
    present is an input rejection (65): the stage was given a reference
    image with no recorded zero point and no override to fall back on.
    """
    override = _zero_point_override(settings)
    header_value = _reference_header_magzp(work_dir, reference_image)
    if override is not None:
        if header_value is not None:
            log.info(
                "zero point source=override; value=%s (overrides reference header MAGZP=%s)",
                override, header_value)
        else:
            log.info(
                "zero point source=override; value=%s (reference header has no MAGZP)", override)
        return override, "override"
    if header_value is None:
        raise InputRejected(
            "reference image header has no MAGZP keyword and [awaicgen] zprefimg is not set: "
            "either set [awaicgen] zprefimg as an explicit override, or supply a reference "
            "image whose header carries MAGZP (the reference stage stamps it on every coadd "
            "it produces)")
    log.info("zero point source=header; value=%s", header_value)
    return header_value, "header"


# ----------------------------------------------------------------------
# Catalog blocks
# ----------------------------------------------------------------------


@dataclass
class _Catalogs:
    """One differencer's catalog outcome: counts, files, and the mask bits it set."""

    sextractor: dict[str, int] = field(default_factory=dict)
    sextractor_files: dict[str, str] = field(default_factory=dict)
    photutils: dict[str, int | None] = field(default_factory=dict)
    photutils_files: dict[str, dict[str, str | None]] = field(default_factory=dict)
    bits: int = 0


def _photutils_files(catalog: str, finder: str, residual: str, parquet: str | None) -> dict:
    return {"catalog": catalog, "finder": finder, "residual": residual, "parquet": parquet}


def _run_catalogs(
    *,
    differencer: str,
    kit: Toolkit,
    work_dir: Path,
    settings: dict[str, Any],
    positive: dict[str, str],
    negative: dict[str, str],
    weight_image: str,
    psf_image: str,
    sky_coords_image: str,
    psfcat_names: dict[str, str],
    override_weight_type_and_filter: bool,
    write_parquet: bool,
    seed: int | None,
) -> _Catalogs:
    """`dev`'s SExtractor then Photutils blocks, positive then negative.

    ``positive``/``negative`` map ``detection``, ``input`` and ``catalog``
    to file names. ``psfcat_names`` holds the configured positive
    Photutils names; the negative ones are derived as `dev` derives them.
    """
    paths = settings["paths"]
    result = _Catalogs()
    for sign, names in (("positive", positive), ("negative", negative)):
        result.sextractor[sign] = sextractor.difference_image_catalog(
            kit.runner, work_dir, settings["sextractor_diffimage"], paths["cfg_path"],
            detection_image=names["detection"],
            input_image=names["input"],
            weight_image=weight_image,
            catalog_name=names["catalog"],
            override_weight_type_and_filter=override_weight_type_and_filter,
            executable=paths["sextractor"])
        result.sextractor_files[sign] = names["catalog"]

    psfcat_settings = psfcat.PsfCatalogSettings.from_table(settings["psfcat_diffimage"])
    for sign, names in (("positive", positive), ("negative", negative)):
        catalog = psfcat_names["catalog"]
        finder = psfcat_names["finder"]
        residual = psfcat_names["residual"]
        if sign == "negative":
            catalog = catalog.replace(".txt", "_negative.txt")
            finder = finder.replace(".txt", "_negative.txt")
            residual = residual.replace(".fits", "_negative.fits")
        parquet = catalog.replace(".txt", ".parquet") if write_parquet else None
        outcome = kit.psf_catalog(
            psfcat_settings,
            image=str(work_dir / names["input"]),
            uncertainty=str(work_dir / weight_image),
            psf=str(work_dir / psf_image),
            sky_coords_image=str(work_dir / sky_coords_image),
            catalog=str(work_dir / catalog),
            finder=str(work_dir / finder),
            residual=str(work_dir / residual),
            parquet=str(work_dir / parquet) if parquet else None,
            label=f"{sign} {differencer} difference image",
            seed=seed)
        if not outcome.produced:
            result.bits |= catalog_outcome_bit(differencer, sign)
        result.photutils[sign] = outcome.nsources
        result.photutils_files[sign] = _photutils_files(catalog, finder, residual, parquet)
    return result


# ----------------------------------------------------------------------
# The body
# ----------------------------------------------------------------------


def _body(context: StageContext) -> StageResult:
    settings = context.settings
    _check_settings(settings)
    kit = toolkit()
    log = context.logger
    seed = _seed(settings)

    paths = settings["paths"]
    cfg_path = paths["cfg_path"]
    python_cmd = paths["python"] or sys.executable
    sca_gain = float(settings["instrument"]["sca_gain"])
    sca_readout_noise = float(settings["instrument"]["sca_readout_noise"])
    saturation_level_sciimage = float(settings["sci_image"]["saturation_level"])
    saturation_level_refimage = float(settings["ref_image"]["saturation_level"])
    zogy_settings = settings["zogy"]
    thresh = float(zogy_settings["post_zogy_keep_diffimg_lower_cov_map_thresh"])

    work_dir = context.outputs_dir / paths["work_subdirectory"]
    work_dir.mkdir(parents=True, exist_ok=True)

    inputs = _read_input_set(context, work_dir)
    exptime_sciimage = float(inputs.l2_registration.exptime)
    science_image_filename = _unzip_science_image(work_dir, inputs.science_image)

    def w(name: str) -> Path:
        return work_dir / name

    # Image statistics for resizing (dev: HDU 1 of the delivered file).
    stats_sci_img = statistics.fits_data_statistics_with_clipping(
        w(science_image_filename), 3.0, 1, saturation_level_sciimage, seed=seed)
    avg_sci_img = stats_sci_img["clippedavg"]

    # Reformat and simple-model uncertainty.
    reformatted_science_image_filename = science_image_filename.replace(".fits", "_reformatted.fits")
    reformatted_science_uncert_image_filename = science_image_filename.replace(
        ".fits", "_reformatted_unc.fits")
    reformat.reformat_simdata_fits_file_and_compute_uncertainty_image_via_simple_model(
        w(science_image_filename), sca_gain, sca_readout_noise, avg_sci_img,
        w(reformatted_science_image_filename), w(reformatted_science_uncert_image_filename))

    # Science-image catalog and FWHM; reference FWHM from its catalog.
    filename_sciimage_catalog = reformatted_science_image_filename.replace(".fits", "_secat.txt")
    sextractor.generate_science_image_catalog(
        kit.runner, work_dir, reformatted_science_image_filename,
        reformatted_science_uncert_image_filename, cfg_path, settings["sextractor_sciimage"],
        filename_sciimage_catalog, executable=paths["sextractor"])
    fwhm_sci, _ = sextractor.fwhm_from_catalog(
        w(filename_sciimage_catalog), cfg_path + "/rapidSexParamsSciImage.inp", "sci")
    fwhm_ref, _ = sextractor.fwhm_from_catalog(
        w(inputs.reference_catalog), cfg_path + "/rapidSexParamsRefImage.inp", "ref")

    # SWarp the reference bundle into the science image's distortion frame.
    # The reference (an awaicgen coadd) has no distortion: no PV conversion.
    (sci_fits_file_with_pv, _ref_pv, _ref_cov_pv, _ref_unc_pv,
     output_resampled_reference_image,
     output_resampled_reference_cov_map,
     output_resampled_reference_uncert_image) = \
        resample.resample_reference_image_to_science_image_with_pv_distortion(
            kit.runner, work_dir, reformatted_science_image_filename, 0,
            inputs.reference_image, inputs.reference_coverage, inputs.reference_uncertainty,
            0, False, settings["swarp"], executable=paths["swarp"], sip_to_pv=kit.sip_to_pv)
    for output in (output_resampled_reference_image, output_resampled_reference_cov_map,
                   output_resampled_reference_uncert_image):
        # SWarp has read the .head symlinks; they would publish as copies.
        w(output.replace(".fits", ".head")).unlink(missing_ok=True)

    # Normalise the science PSF (the reference PSF is already normalised).
    filename_sciimage_psf_normalized = inputs.science_psf.replace(".fits", "_normalized.fits")
    psf.normalize_image(w(inputs.science_psf), 0, w(filename_sciimage_psf_normalized))

    # Background-subtract the science image.
    filename_bkg_subbed_science_image = background.subtract_background(
        kit.runner, work_dir, paths["bkgest_code"], paths["bkgest_include_dir"],
        sci_fits_file_with_pv, settings["bkgest"])

    # Gain matching. The reference zero point is read from the reference
    # image's own MAGZP header (the file as delivered, not the resampled
    # copy), unless [awaicgen] zprefimg overrides it (the lead, 2026-09-26).
    zero_point, zero_point_source = _resolve_zero_point(
        settings, work_dir, inputs.reference_image, log)
    filename_scigainmatchsexcat_catalog = filename_bkg_subbed_science_image.replace(
        ".fits", "_scigainmatchsexcat.txt")
    filename_refgainmatchsexcat_catalog = output_resampled_reference_image.replace(
        ".fits", "_refgainmatchsexcat.txt")
    scalefac, dxrmsfin_measured, dyrmsfin_measured, dxmedianfin, dymedianfin = \
        gainmatch.gain_match_science_and_reference_images(
            kit.runner, work_dir,
            filename_bkg_subbed_science_image, reformatted_science_uncert_image_filename,
            filename_scigainmatchsexcat_catalog,
            output_resampled_reference_image, output_resampled_reference_uncert_image,
            filename_refgainmatchsexcat_catalog,
            zero_point, settings["gainmatch"],
            settings["sextractor_gainmatch"], fwhm_sci, fwhm_ref,
            float(zogy_settings["astrometric_uncert_x"]),
            float(zogy_settings["astrometric_uncert_y"]),
            cfg_path, executable=paths["sextractor"])
    log.info("scalefac,dxrmsfin,dyrmsfin,dxmedianfin,dymedianfin = %s %s %s %s %s",
             scalefac, dxrmsfin_measured, dyrmsfin_measured, dxmedianfin, dymedianfin)
    scalefacref = 1. / scalefac

    output_resampled_gainmatched_reference_image = output_resampled_reference_image.replace(
        ".fits", "_gainmatched.fits")
    scale_image_data(w(output_resampled_reference_image), scalefacref,
                     w(output_resampled_gainmatched_reference_image))
    output_resampled_gainmatched_reference_uncert_image = \
        output_resampled_reference_uncert_image.replace(".fits", "_gainmatched.fits")
    scale_image_data(w(output_resampled_reference_uncert_image), scalefacref,
                     w(output_resampled_gainmatched_reference_uncert_image))

    # NaNs in ZOGY's input images become 0.0; extreme artifacts are repaired.
    nan_indices_sciimage = repair.replace_nans_with_value(w(filename_bkg_subbed_science_image), 0.0)
    if settings["sci_image"]["repair_extreme_artifact_pixels"]:
        n_artifacts_repaired = repair.repair_extreme_artifact_pixels(
            w(filename_bkg_subbed_science_image),
            float(settings["sci_image"]["extreme_artifact_threshold"]))
        log.info("Number of extreme artifact pixels repaired in science image = %s",
                 n_artifacts_repaired)
    nan_indices_refimage = repair.replace_nans_with_value(
        w(output_resampled_gainmatched_reference_image), 0.0)

    # Statistics for ZOGY (dev: the reformatted science image, and the
    # resampled reference before gain matching).
    stats_sci_img = statistics.fits_data_statistics_with_clipping(
        w(reformatted_science_image_filename), 3.0, 0, saturation_level_sciimage, seed=seed)
    std_sci_img = stats_sci_img["clippedstd"]
    stats_ref_img = statistics.fits_data_statistics_with_clipping(
        w(output_resampled_reference_image), 3.0, 0, saturation_level_refimage, seed=seed)
    std_ref_img = stats_ref_img["clippedstd"]
    log.info("avg_sci_img,std_sci_img,cnt_sci_img = %s %s %s",
             stats_sci_img["clippedavg"], std_sci_img, stats_sci_img["nkept"])
    log.info("avg_ref_img,std_ref_img,cnt_ref_img = %s %s %s",
             stats_ref_img["clippedavg"], std_ref_img, stats_ref_img["nkept"])

    repair.replace_nans_with_value(w(reformatted_science_uncert_image_filename), std_sci_img)
    repair.replace_nans_with_value(w(output_resampled_gainmatched_reference_uncert_image), std_ref_img)

    # Subpixel offset of the reference by the measured medians.
    offsets.apply_subpixel_orthogonal_offsets(
        w(output_resampled_gainmatched_reference_image), dxmedianfin, dymedianfin)

    if "rimtimsim" in science_image_filename:
        psf.transpose_image_data(w(filename_sciimage_psf_normalized))

    # ZOGY.
    filename_diffimage = zogy_settings["zogy_output_diffimage_file"]
    filename_diffpsf = zogy_settings["zogy_output_diffpsf_file"]
    filename_scorrimage = zogy_settings["zogy_output_scorrimage_file"]

    # dev feeds ZOGY a fixed astrometric uncertainty (0.0 by default), not
    # the measured RMS; the setting reproduces that override exactly. The
    # measured RMS (dxrmsfin_measured/dyrmsfin_measured) is what gets
    # recorded as dxrmsfin/dyrmsfin (see `residual` below) — production's
    # dxrmsfin/dyrmsfin columns hold the measured value, not this override.
    zogy_astrometric_sigma_x = float(zogy_settings["astrometric_sigma"])
    zogy_astrometric_sigma_y = float(zogy_settings["astrometric_sigma"])
    log.info("measured registration RMS dx,dy = %s %s; ZOGY astrometric_sigma dx,dy = %s %s",
             dxrmsfin_measured, dyrmsfin_measured,
             zogy_astrometric_sigma_x, zogy_astrometric_sigma_y)

    zogy_sn, zogy_sr = zogy.zogy_background_sigmas(
        zogy_settings["zogy_sn_sr_from_uncertainty_maps"],
        w(reformatted_science_uncert_image_filename),
        w(output_resampled_gainmatched_reference_uncert_image),
        std_sci_img, std_ref_img, scalefacref)
    log.info("zogy_sn,zogy_sr,ratio = %s %s %s", zogy_sn, zogy_sr,
             zogy_sn / zogy_sr if zogy_sr else float("nan"))

    kit.runner.run(zogy.build_zogy_command(
        python_cmd, paths["zogy_code"],
        filename_bkg_subbed_science_image, output_resampled_gainmatched_reference_image,
        filename_sciimage_psf_normalized, inputs.reference_psf,
        reformatted_science_uncert_image_filename,
        output_resampled_gainmatched_reference_uncert_image,
        zogy_sn, zogy_sr, zogy_astrometric_sigma_x, zogy_astrometric_sigma_y,
        filename_diffimage, filename_diffpsf, filename_scorrimage), cwd=work_dir)

    # Mask with the resampled coverage map, restore NaNs, negate.
    filename_diffimage_masked = filename_diffimage.replace(".fits", "_masked.fits")
    filename_scorrimage_masked = filename_scorrimage.replace(".fits", "_masked.fits")
    for source, target in ((filename_diffimage, filename_diffimage_masked),
                           (filename_scorrimage, filename_scorrimage_masked)):
        masking.mask_difference_image_with_resampled_reference_cov_map(
            w(source), w(output_resampled_reference_cov_map), w(target), thresh)
    for target in (filename_diffimage_masked, filename_scorrimage_masked):
        if nan_indices_sciimage:
            repair.restore_nans(w(target), nan_indices_sciimage)
        if nan_indices_refimage:
            repair.restore_nans(w(target), nan_indices_refimage)

    filename_diffimage_masked_negative = filename_diffimage_masked.replace(".fits", "_negative.fits")
    scale_image_data(w(filename_diffimage_masked), -1.0, w(filename_diffimage_masked_negative))
    filename_scorrimage_masked_negative = filename_scorrimage_masked.replace(".fits", "_negative.fits")
    scale_image_data(w(filename_scorrimage_masked), -1.0, w(filename_scorrimage_masked_negative))

    # Difference uncertainty: the catalogs' weight and error image.
    filename_diffimage_unc_masked = filename_diffimage_masked.replace("masked.fits", "uncert_masked.fits")
    uncertainty.compute_diffimage_uncertainty(
        sca_gain * exptime_sciimage,
        w(filename_bkg_subbed_science_image),
        w(output_resampled_gainmatched_reference_image),
        w(output_resampled_reference_cov_map), thresh,
        w(filename_diffimage_masked), w(filename_diffimage_unc_masked), seed=seed)

    zogy_files = {
        "difference": filename_diffimage_masked,
        "uncertainty": filename_diffimage_unc_masked,
        "significance": filename_scorrimage_masked,
        "psf": filename_diffpsf,
    }
    zogy_negative_files = {
        "difference": filename_diffimage_masked_negative,
        "significance": filename_scorrimage_masked_negative,
    }
    zogy_detection = zogy_settings["detection_role"]
    psfcat_table = settings["psfcat_diffimage"]
    zogy_catalogs = _run_catalogs(
        differencer="zogy", kit=kit, work_dir=work_dir, settings=settings,
        positive={"detection": zogy_files[zogy_detection],
                  "input": filename_diffimage_masked,
                  "catalog": filename_diffimage_masked.replace(".fits", ".txt")},
        negative={"detection": zogy_negative_files.get(zogy_detection, zogy_files[zogy_detection]),
                  "input": filename_diffimage_masked_negative,
                  "catalog": filename_diffimage_masked_negative.replace(".fits", ".txt")},
        weight_image=filename_diffimage_unc_masked,
        psf_image=filename_diffpsf,
        sky_coords_image=reformatted_science_image_filename,
        psfcat_names={"catalog": psfcat_table["output_zogy_psfcat_filename"],
                      "finder": psfcat_table["output_zogy_psfcat_finder_filename"],
                      "residual": psfcat_table["output_zogy_psfcat_residual_filename"]},
        override_weight_type_and_filter=True, write_parquet=True, seed=seed)
    output_diffimage_file_infobits = zogy_catalogs.bits

    # SFFT, as dev runs it.
    notes: dict[str, Any] = {
        "zogy_astrometric_sigma": {"x": zogy_astrometric_sigma_x,
                                    "y": zogy_astrometric_sigma_y},
        "zero_point": {"value": zero_point, "source": zero_point_source},
    }
    sfft_settings = settings["sfft"]
    sfft_result = None
    if sfft_settings["run_sfft"]:
        sfft_result = _run_sfft(
            kit=kit, work_dir=work_dir, settings=settings, seed=seed,
            science_image_filename=science_image_filename,
            filename_bkg_subbed_science_image=filename_bkg_subbed_science_image,
            gainmatched_reference=output_resampled_gainmatched_reference_image,
            resampled_cov_map=output_resampled_reference_cov_map,
            scigainmatch_catalog=filename_scigainmatchsexcat_catalog,
            refgainmatch_catalog=filename_refgainmatchsexcat_catalog,
            science_psf=filename_sciimage_psf_normalized,
            reference_psf=inputs.reference_psf,
            reformatted_science_image=reformatted_science_image_filename,
            gain_times_exptime=sca_gain * exptime_sciimage, thresh=thresh)
        notes["sfft"] = {"ran": True, "succeeded": sfft_result.succeeded,
                         "exit_code": sfft_result.exit_code}
        if sfft_result.succeeded:
            output_diffimage_file_infobits |= sfft_result.catalogs.bits
        else:
            log.warning("SFFT did not execute successfully (exit %s); continuing with ZOGY "
                        "only, as dev does", sfft_result.exit_code)

    # Naive subtraction, a diagnostic.
    if settings["naive_diffimage"]["naive_diffimage_flag"]:
        output_diffimage_file_infobits |= _run_naive(
            kit=kit, work_dir=work_dir, settings=settings, seed=seed,
            filename_bkg_subbed_science_image=filename_bkg_subbed_science_image,
            gainmatched_reference=output_resampled_gainmatched_reference_image,
            resampled_cov_map=output_resampled_reference_cov_map,
            nan_indices_sciimage=nan_indices_sciimage,
            nan_indices_refimage=nan_indices_refimage,
            reference_psf=inputs.reference_psf,
            reformatted_science_image=reformatted_science_image_filename,
            gain_times_exptime=sca_gain * exptime_sciimage, thresh=thresh)

    # Outputs.
    common = dict(
        inputs=inputs, settings_hash="sha256:" + context.settings_hash,
        outputs_dir=context.outputs_dir, work_dir=work_dir,
        catalog_outcome_bits=output_diffimage_file_infobits,
        residual={"x_rms": dxrmsfin_measured, "y_rms": dyrmsfin_measured,
                  "x_median": float(dxmedianfin), "y_median": float(dymedianfin)},
        scalefacref=float(scalefacref))
    outputs = _instance_outputs(
        differencer="zogy", files=zogy_files, catalogs=zogy_catalogs,
        detection_role=zogy_detection, **common)
    if (sfft_result is not None and sfft_result.succeeded
            and sfft_settings["register_sfft"]):
        outputs += _instance_outputs(
            differencer="sfft", files=sfft_result.files, catalogs=sfft_result.catalogs,
            detection_role=sfft_settings["detection_role"], **common)

    products_read = {"l2-image": inputs.l2.instance}
    if inputs.reference_rfid is None:
        # A reference registered by dev has no instance row to depend on.
        products_read["reference-image"] = inputs.reference.instance
    return StageResult(outputs=outputs, products_read=products_read, execution_notes=notes)


# ----------------------------------------------------------------------
# SFFT and naive
# ----------------------------------------------------------------------


@dataclass
class _SfftResult:
    succeeded: bool
    exit_code: int
    files: dict[str, str] = field(default_factory=dict)
    catalogs: _Catalogs = field(default_factory=_Catalogs)


def _run_sfft(*, kit, work_dir, settings, seed, science_image_filename,
              filename_bkg_subbed_science_image, gainmatched_reference, resampled_cov_map,
              scigainmatch_catalog, refgainmatch_catalog, science_psf, reference_psf,
              reformatted_science_image, gain_times_exptime, thresh) -> _SfftResult:
    """SFFT's command, then its files and catalogs.

    Empty ``[sfft] python_cmd`` means the stage's own interpreter, the
    same convention ``[paths] python`` uses for ZOGY; empty
    ``activate_cmd`` means no activation (see ``sfft.shell_command``).
    """
    s = settings["sfft"]
    crossconv_flag = bool(s["crossconv_flag"])
    if "rimtimsim" in science_image_filename:
        crossconv_flag = False
    names = sfft.sfft_file_names(crossconv_flag)
    python_cmd = s["python_cmd"] or sys.executable

    sfft_cmd = sfft.build_sfft_command_args(
        python_cmd, s["sfft_code"], filename_bkg_subbed_science_image,
        gainmatched_reference, scigainmatch_catalog, refgainmatch_catalog,
        science_psf, reference_psf, names["scisegm"], names["refsegm"],
        science_image_filename, crossconv_flag,
        s["sfft_bsmask_value"], s["sfft_bsmask_radius"],
        bool(s["sfft_use_gainmatch_catalogs"]), bool(s["sfft_use_segmentation"]))
    exit_code = int(kit.runner.run_shell(
        sfft.shell_command(s["activate_cmd"], sfft_cmd), cwd=work_dir))
    if exit_code != 0:
        return _SfftResult(succeeded=False, exit_code=exit_code)

    def w(name: str) -> Path:
        return work_dir / name

    diffimage = names["diffimage"]
    cconvdiff = names["cconvdiff"]
    repair.replace_nans_with_value(w(diffimage), 0.0)

    diffimage_negative = diffimage.replace(".fits", "_negative.fits")
    scale_image_data(w(diffimage), -1.0, w(diffimage_negative))
    cconvdiff_negative = cconvdiff.replace(".fits", "_negative.fits")
    if crossconv_flag:
        scale_image_data(w(cconvdiff), -1.0, w(cconvdiff_negative))

    diffimage_unc = names["diffimage_unc"]
    uncertainty.compute_diffimage_uncertainty(
        gain_times_exptime, w(filename_bkg_subbed_science_image), w(gainmatched_reference),
        w(resampled_cov_map), thresh, w(diffimage), w(diffimage_unc), seed=seed)

    files = {"difference": diffimage, "uncertainty": diffimage_unc}
    if w(names["diffpsf"]).exists():
        files["psf"] = names["diffpsf"]
    if w(names["soln"]).exists():
        files["kernel"] = names["soln"]

    # dev detects on the cross-convolved image when crossconv_flag is set.
    detection_role = s["detection_role"]
    if detection_role == "difference":
        detection, detection_negative = (
            (cconvdiff, cconvdiff_negative) if crossconv_flag else (diffimage, diffimage_negative))
    else:
        detection = detection_negative = files[detection_role]

    psfcat_table = settings["psfcat_diffimage"]
    catalogs = _run_catalogs(
        differencer="sfft", kit=kit, work_dir=work_dir, settings=settings,
        positive={"detection": detection, "input": diffimage,
                  "catalog": detection.replace(".fits", ".txt")},
        negative={"detection": detection_negative, "input": diffimage_negative,
                  "catalog": diffimage_negative.replace(".fits", ".txt")},
        weight_image=diffimage_unc,
        psf_image=names["diffpsf"],
        sky_coords_image=reformatted_science_image,
        psfcat_names={"catalog": psfcat_table["output_sfft_psfcat_filename"],
                      "finder": psfcat_table["output_sfft_psfcat_finder_filename"],
                      "residual": psfcat_table["output_sfft_psfcat_residual_filename"]},
        override_weight_type_and_filter=True, write_parquet=True, seed=seed)
    return _SfftResult(succeeded=True, exit_code=0, files=files, catalogs=catalogs)


def _run_naive(*, kit, work_dir, settings, seed, filename_bkg_subbed_science_image,
               gainmatched_reference, resampled_cov_map, nan_indices_sciimage,
               nan_indices_refimage, reference_psf, reformatted_science_image,
               gain_times_exptime, thresh) -> int:
    """`dev`'s naive block; returns the catalog-outcome bits it set."""
    def w(name: str) -> Path:
        return work_dir / name

    filename_naive_diffimage = "naive_diffimage.fits"
    naive.compute_naive_difference_image(
        w(filename_bkg_subbed_science_image), w(gainmatched_reference), w(filename_naive_diffimage))

    masked = settings["naive_diffimage"]["naive_output_diffimage_file"]
    masking.mask_difference_image_with_resampled_reference_cov_map(
        w(filename_naive_diffimage), w(resampled_cov_map), w(masked), thresh)
    if nan_indices_sciimage:
        repair.restore_nans(w(masked), nan_indices_sciimage)
    if nan_indices_refimage:
        repair.restore_nans(w(masked), nan_indices_refimage)

    masked_negative = masked.replace(".fits", "_negative.fits")
    scale_image_data(w(masked), -1.0, w(masked_negative))

    unc = masked.replace("masked.fits", "uncert_masked.fits")
    uncertainty.compute_diffimage_uncertainty(
        gain_times_exptime, w(filename_bkg_subbed_science_image), w(gainmatched_reference),
        w(resampled_cov_map), thresh, w(masked), w(unc), seed=seed)

    psfcat_table = settings["psfcat_diffimage"]
    catalogs = _run_catalogs(
        differencer="naive", kit=kit, work_dir=work_dir, settings=settings,
        positive={"detection": masked, "input": masked,
                  "catalog": masked.replace(".fits", ".txt")},
        negative={"detection": masked_negative, "input": masked_negative,
                  "catalog": masked_negative.replace(".fits", ".txt")},
        weight_image=unc,
        # dev: the reference PSF ("TODO" in dev).
        psf_image=reference_psf,
        sky_coords_image=reformatted_science_image,
        psfcat_names={"catalog": psfcat_table["output_naive_psfcat_filename"],
                      "finder": psfcat_table["output_naive_psfcat_finder_filename"],
                      "residual": psfcat_table["output_naive_psfcat_residual_filename"]},
        override_weight_type_and_filter=False, write_parquet=False, seed=seed)
    return catalogs.bits


# ----------------------------------------------------------------------
# Manifest entries
# ----------------------------------------------------------------------


def _instance_outputs(*, differencer: str, files: dict[str, str], catalogs: _Catalogs,
                      detection_role: str, inputs: _InputSet, settings_hash: str,
                      outputs_dir: Path, work_dir: Path, catalog_outcome_bits: int,
                      residual: dict[str, float], scalefacref: float) -> list[OutputEntry]:
    """One difference-image entry and its source-catalog entries."""
    members = tuple(
        member_for_file(role=role, path=work_dir / name, relative_to=outputs_dir)
        for role, name in files.items())
    primary = next(m.path for m in members if m.role == "difference")

    registration = DifferenceImageRegistration(
        detection_role=detection_role,
        centre=dict(inputs.l2_registration.centre),
        corners=[list(c) for c in inputs.l2_registration.corners],
        catalog_outcome_bits=int(catalog_outcome_bits),
        infobits_science=int(inputs.l2_registration.infobits),
        infobits_reference=int(inputs.reference_infobits),
        source_counts={
            "sextractor": {s: int(catalogs.sextractor[s]) for s in ("positive", "negative")},
            "photutils": {s: (None if catalogs.photutils[s] is None else int(catalogs.photutils[s]))
                          for s in ("positive", "negative")},
        },
        registration_residual=residual,
        reference_scale_factor=scalefacref,
        md5=_md5_of_file(work_dir / files["difference"]),
        reference_rfid=inputs.reference_rfid,
    )
    try:
        registration.validate(differencer)
    except DifferenceImageRegistrationError as exc:
        raise InputRejected(f"{differencer} registration block: {exc}") from exc

    instance = new_ulid()
    entries = [OutputEntry(
        kind="difference-image",
        format_version="1",
        instance=instance,
        key={"l2": inputs.l2.instance, "reference": inputs.reference.instance,
             "differencer": differencer, "settings_hash": settings_hash},
        members=members,
        primary=primary,
        registration=registration.to_dict(),
    )]

    for sign in ("positive", "negative"):
        catalog_member = member_for_file(
            role="catalog", path=work_dir / catalogs.sextractor_files[sign],
            relative_to=outputs_dir)
        entries.append(OutputEntry(
            kind="source-catalog", format_version="1", instance=new_ulid(),
            key={"difference": instance, "catalog_type": "sextractor", "sign": sign},
            members=(catalog_member,), primary=catalog_member.path,
            registration={"source_count": int(catalogs.sextractor[sign])}))

    for sign in ("positive", "negative"):
        names = catalogs.photutils_files[sign]
        if catalogs.photutils[sign] is None or not (work_dir / names["catalog"]).exists():
            continue
        catalog_members = [member_for_file(
            role="catalog", path=work_dir / names["catalog"], relative_to=outputs_dir)]
        for role in ("finder", "residual", "parquet"):
            name = names[role]
            if name is not None and (work_dir / name).exists():
                catalog_members.append(member_for_file(
                    role=role, path=work_dir / name, relative_to=outputs_dir))
        entries.append(OutputEntry(
            kind="source-catalog", format_version="1", instance=new_ulid(),
            key={"difference": instance, "catalog_type": "photutils", "sign": sign},
            members=tuple(catalog_members), primary=catalog_members[0].path,
            registration={"source_count": int(catalogs.photutils[sign])}))
    return entries


def main(argv: list[str]) -> int:
    return run_stage(DECLARATION, _body, argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
