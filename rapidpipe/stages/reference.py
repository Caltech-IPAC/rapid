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
with ``[reference.psfcat] enabled``, the reference PSF, the Photutils
PSF-fit catalog and the limiting magnitudes (``psfcat``) ->
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

Under ``[selection] selection_type = "coverage"``
(``rapidpipe.products.coverage``) the loop has chosen the frames by their
coverage of the reference, and ``min_frames`` is not used: the stage
estimates the named frames' coverage from their ``l2files`` corners (step
``coverage_estimate``), coadds those that meet the reference (a frame that
misses it is skipped) in the loop's order (``mjdobs``, then distance from
the reference's centre, then L2 key: ``coverage.candidate_order``), and
measures the fraction of the coverage map's pixels at
``min_coverage_depth`` or deeper (step ``verify``). An estimate below
``min_coverage_fraction`` less ``coverage_estimate_tolerance``, or a
measured fraction below ``min_coverage_fraction``, fails the job before a
manifest, exit 65 with the fraction as its reason. A reference that passes
carries ``COVFRAC``, ``COVDEPTH`` and ``COVTARG`` in its header; the
manifest's ``reference.coverage`` names the selected and skipped
``rid``\\ s, the estimate and the measured fraction. A run's body from
before ``selection_type`` existed lacks it and reads ``"nframes"``; the
coverage keys are read only under ``"coverage"``.

With ``[reference.psfcat] enabled`` the job also names ``psfs`` inputs of
its filter and scope SCA, as ``[difference] reference_psf`` says
(:func:`psf_kinds`), and its PSF (:func:`reference_psf`) follows that mode:
``"measured"`` measures it on the mosaic with
:func:`rapidpipe.science.difference.refpsf.measure`, the science row's
stamp giving its size, width floor and wings, and on any measurement
error falls back to the set's row when the run names
``reference_psf_set``, or else makes no catalog (InfoBit 16);
``"precomputed"`` uses the set's row, its MD5 and stamp checked;
``"science"`` the science row. The science PSF is in the detector frame
and the mosaic north up, so a non-round science PSF's wings and, under
``"science"``, its core are not rotated to the mosaic: only the
``"precomputed"`` per-filter set is computed for any orientation.

Outputs under ``job.path``: ``ref/`` holds the mosaic, its coverage and
uncertainty maps (awaicgen's names, beside one another, as the import
copies them) and the SExtractor catalog, and with the catalog on the PSF
it used (``refimage_psf.fits``) and the PSF-fit catalog's text, finder,
Parquet and residual files. Declared tables: ``refimages``
(one row, its ``status`` and ``infobits`` the grade; ``rfid`` is drawn
from the table's own identity sequence, so the child rows can carry it),
``refimimages`` (one row per coadded L2
row), ``refimcatalogs`` (the mosaic catalog, ``cattype`` 1, which the
packet stage reads for reference-star matches, and the PSF-fit catalog,
``cattype`` 2, when it was made), ``refimmeta`` (one row, `dev`'s RefImMeta
columns: :func:`refimmeta_row`, ``npucatsources`` the PSF-fit catalog's
sources when it was attempted) and, when ``"measured"`` measured its
PSF, ``psfs``: 18 ``reference`` rows, one per SCA, sharing
``refimage_psf.fits`` and its MD5, under ``psf_set``
``refimage-<field>-<filter>`` (:func:`rapidpipe.products.filters.refimage_psf_set`),
bound with the reference. A measurement that fell back, or failed with no
set, writes none, and the manifest's ``unbound`` leaves those expected
keys out of the binding. A reference graded status 0 (a fatal InfoBit)
fails the job with exit 65 after its header stamp and before any table or
manifest is written, its fatal bits the reason, so nothing is published and
no difference is made against it (Azalee, 2026-11-24). The manifest also records
`dev`'s refimmeta measurements under ``reference`` and ``metrics``, one
flat dict of named numbers (the compact stars' width, the coverage
histogram, the extremes, awaicgen's wall seconds; :func:`rapidpipe.science.reference.measure.quality_metrics`).

Departures from `dev` (reference page):

- ``FID`` is not stamped; the row carries ``fid``. The foundation's
  ``RPRUN``, ``RPJOB``, ``RPATTMPT`` and ``RPSTAGE`` are stamped instead.
- The row's ``checksum`` is the published (stamped) mosaic's MD5. `dev`
  records the MD5 before its header is stamped, so its checksum never
  matches its own object.
- The Photutils reference catalog is made only with ``[psfcat]
  enabled`` (the flight overlay), so in the base the grade sets
  none of its bits (5, 6, 7, 16); its PSF follows ``[difference]
  reference_psf`` instead of one fixed reference PSF file per filter.
  Fake-source injection is not ported (``true`` is refused).
- A status-0 reference is published but not made official, so no
  difference is made against it (Azalee, 2026-11-10).
- Commissioning's ``[awaicgen] use_nominal_zptmag`` (687c93b2, 8a4a172d):
  when true, each frame is scaled by its filter's nominal ZPTMAG from the
  run's ``[intake.nominal_zptmag]`` table instead of its header's, and the
  stamp's ``ZPSOURCE`` and the manifest's ``zptmag_source`` say
  ``nominal``; ``zptmag_rms`` still reads the header values.
- `dev` never checks a tool's exit code; this stage fails when awaicgen
  or SExtractor leaves no output, and when the catalog has no source
  with a usable FWHM (`dev`'s ``nanmin`` raises there).
- The mosaic catalog's ``MAG_ZEROPOINT`` is ``zprefimg``, the ``MAGZP``
  the coadd was scaled to, set at run time (:func:`catalog_settings`),
  where the settings table leaves it 0.

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
from typing import Any, Callable

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from astropy.io import fits

from rapidpipe.exitcodes import CommandExit, ExitCode
from rapidpipe.foundation.local import JobContext
from rapidpipe.foundation.prefix import get
from rapidpipe.foundation.steps import step
from rapidpipe.products.filters import refimage_psf_set
from rapidpipe.science.difference import refpsf
from rapidpipe.science.difference.psfcat import psf_catalog
from rapidpipe.science.difference.sextractor import parse_ascii_text_sextractor_catalog
from rapidpipe.science.difference.tools import ToolRunner
from rapidpipe.science.reference import awaicgen, catalog, header, infobits, measure, prep, psfcat
from rapidpipe.products import coverage
from rapidpipe.science.spatial import field_center, healpix_indexes

logger = logging.getLogger(__name__)

#: `refimcatalogs.cattype` of the mosaic SExtractor catalog (as dev registers it).
CATALOG_TYPE = 1
#: `refimcatalogs.cattype` of the mosaic's Photutils PSF-fit catalog.
PSFCAT_CATALOG_TYPE = 2
#: The ``[difference] reference_psf`` modes the reference's PSF follows.
REFERENCE_PSF_MODES = ("measured", "science", "precomputed")
#: The reference PSF the catalog used, under ``[paths] work_subdirectory``.
REFERENCE_PSF_NAME = "refimage_psf.fits"
#: The SCAs a measured reference PSF's rows cover, one row each.
SCAS = tuple(range(1, 19))
#: The status of a measured reference PSF's ``psfs`` rows.
PSF_STATUS_GOOD = 1
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
#: `dev`'s RefImMeta columns and types (``rapidOpsTables.sql``); ``npucatsources`` is null with no PSF-fit catalog attempted.
REFIMMETA_SCHEMA = pa.schema([
    ("rfid", pa.int32()), ("field", pa.int32()), ("hp6", pa.int32()), ("hp9", pa.int32()),
    ("fid", pa.int16()), ("nframes", pa.int16()), ("mjdobsmin", pa.float64()),
    ("mjdobsmax", pa.float64()), ("npixnan", pa.int32()), ("clmean", pa.float32()),
    ("clstddev", pa.float32()), ("clnoutliers", pa.int32()), ("gmedian", pa.float32()),
    ("datascale", pa.float32()), ("gmin", pa.float32()), ("gmax", pa.float32()),
    ("cov5percent", pa.float32()), ("medncov", pa.float32()), ("medpixunc", pa.float32()),
    ("fwhmmedpix", pa.float32()), ("fwhmminpix", pa.float32()), ("fwhmmaxpix", pa.float32()),
    ("nsxcatsources", pa.int32()), ("npucatsources", pa.int32()),
])
PSFS_SCHEMA = pa.schema([
    ("dataset", pa.string()), ("fid", pa.int16()), ("sca", pa.int16()), ("kind", pa.string()),
    ("psf_set", pa.string()), ("filename", pa.string()), ("checksum", pa.string()),
    ("status", pa.int16()),
])


@dataclass
class Toolkit:
    """The stage's boundary with external software; a test replaces :func:`toolkit`.

    ``runner`` runs awaicgen and SExtractor (``ToolRunner``'s ``run``);
    ``reference_psf`` measures the mosaic's PSF
    (:func:`rapidpipe.science.difference.refpsf.measure`'s signature);
    ``psf_catalog`` makes the Photutils catalog
    (:func:`rapidpipe.science.difference.psfcat.psf_catalog`'s signature).
    """

    runner: Any = field(default_factory=ToolRunner)
    reference_psf: Callable = refpsf.measure
    psf_catalog: Callable = psf_catalog


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
    use_nominal_zptmag: bool
    selection_type: str
    min_coverage_depth: float | None = None          # the coverage keys: set only under "coverage"
    min_coverage_fraction: float | None = None
    max_coverage_depth: float | None = None
    max_coverage_fraction: float | None = None
    coverage_estimate_tolerance: float | None = None
    psfcat: bool = False


def check_settings(settings: dict[str, Any]) -> _Checked:
    """Validate the ``[reference]`` table; every problem is a ``ValueError``."""
    if settings["fake_sources"]["inject_fake_sources_flag"]:
        raise ValueError(
            "[reference.fake_sources] inject_fake_sources_flag = true is not supported: "
            "fake-source injection is not ported to the rebuild")
    enabled = settings["psfcat"]["enabled"]
    if not isinstance(enabled, bool):
        raise ValueError(f"[reference.psfcat] enabled must be true or false, got {enabled!r}")
    if enabled:
        psfcat.settings_from_table(settings["psfcat"])
        for key in ("output_psfcat_filename", "output_psfcat_finder_filename",
                    "output_psfcat_residual_filename"):
            name = settings["psfcat"][key]
            if not isinstance(name, str) or "/" in name or name == REFERENCE_PSF_NAME:
                raise ValueError(f"[reference.psfcat] {key} must be a plain file name other than "
                                 f"{REFERENCE_PSF_NAME}, got {name!r}")
        if not settings["psfcat"]["output_psfcat_filename"].endswith(".txt"):
            raise ValueError("[reference.psfcat] output_psfcat_filename must end .txt (its Parquet "
                             "takes the same name ending .parquet)")
    min_frames = _positive_int(settings, "selection", "min_frames")
    max_frames = _positive_int(settings, "selection", "max_frames")
    if max_frames < min_frames:
        raise ValueError(f"[reference.selection] max_frames ({max_frames}) is less than "
                         f"min_frames ({min_frames})")
    # A run's body from before the coverage selection lacks its keys: "nframes", as the loop reads it.
    selection_type = settings["selection"].get("selection_type", "nframes")
    if selection_type not in coverage.SELECTION_TYPES:
        raise ValueError(f"[reference.selection] selection_type must be one of {coverage.SELECTION_TYPES}, "
                         f"got {selection_type!r}")
    covering = {}
    if selection_type == "coverage":
        depths = {k: _number(settings, "selection", k) for k in ("min_coverage_depth", "max_coverage_depth")}
        fractions = {k: _number(settings, "selection", k)
                     for k in ("min_coverage_fraction", "max_coverage_fraction")}
        tolerance = _number(settings, "selection", "coverage_estimate_tolerance")
        if any(v < 1 for v in depths.values()) or depths["max_coverage_depth"] < depths["min_coverage_depth"]:
            raise ValueError(f"[reference.selection] coverage depths must be at least 1 with max_coverage_depth "
                             f"no less than min_coverage_depth, got {depths}")
        if any(not 0.0 < v <= 1.0 for v in fractions.values()) or tolerance < 0.0:
            raise ValueError(f"[reference.selection] coverage fractions must be in (0, 1] and "
                             f"coverage_estimate_tolerance not negative, got {fractions}, {tolerance}")
        covering = {**depths, **fractions, "coverage_estimate_tolerance": tolerance}
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
    use_nominal_zptmag = settings["awaicgen"].get("use_nominal_zptmag", False)  # absent from older bodies
    if not isinstance(use_nominal_zptmag, bool):
        raise ValueError(f"[reference.awaicgen] use_nominal_zptmag must be true or false, "
                         f"got {use_nominal_zptmag!r}")
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
        infobits=infobits.check_params(settings["infobits"]),
        use_nominal_zptmag=use_nominal_zptmag,
        selection_type=selection_type, psfcat=enabled, **covering)



def reference_psf_mode(difference: dict[str, Any]) -> tuple[str, str]:
    """The run's ``[difference] reference_psf`` mode and ``reference_psf_set``, which the reference's PSF follows.

    Parameters
    ----------
    difference : dict
        The run's ``[difference]`` table (empty when the body has none).

    Returns
    -------
    tuple of str
        The mode, ``"measured"`` when absent, and the set's name, ``""`` when absent.

    Raises
    ------
    ValueError
        A mode not in :data:`REFERENCE_PSF_MODES`, or ``"precomputed"`` with no set.
    """
    mode, psf_set = difference.get("reference_psf", "measured"), difference.get("reference_psf_set", "")
    if mode not in REFERENCE_PSF_MODES:
        raise ValueError(f"[difference] reference_psf is {mode!r}, not one of {REFERENCE_PSF_MODES}")
    if not isinstance(psf_set, str) or (mode == "precomputed" and not psf_set):
        raise ValueError(f'[difference] reference_psf = {mode!r} with reference_psf_set {psf_set!r}')
    return mode, psf_set


def psf_kinds(psfcat_enabled: bool, mode: str, psf_set: str) -> list[str]:
    """The ``psfs`` kinds a reference job names as inputs, sorted.

    None with the catalog off; the set's row under ``"precomputed"``; the science row under
    ``"science"``; under ``"measured"`` the science row, whose stamp size and width floor the
    measurement takes, and the set's row too when one is named, its fallback.

    Parameters
    ----------
    psfcat_enabled : bool
        ``[reference.psfcat] enabled``.
    mode, psf_set : str
        :func:`reference_psf_mode`'s values.

    Returns
    -------
    list of str
        ``[]``, ``["reference"]``, ``["science"]`` or ``["reference", "science"]``.
    """
    if not psfcat_enabled:
        return []
    if mode == "precomputed":
        return ["reference"]
    if mode == "measured" and psf_set:
        return ["reference", "science"]
    return ["science"]


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


def _rejected(reason: str) -> CommandExit:
    """A declared input the job cannot use: exit 65, input rejected, the reason on its row."""
    return CommandExit(int(ExitCode.INPUT_REJECTED), reason)


@dataclass(frozen=True)
class PsfInputs:
    """The reference job's bound ``psfs`` rows: the science row's file, and the set's row."""

    science: str | None = None
    #: The set's row: ``psfid``, ``filename``, ``checksum`` and ``psf_set``.
    reference: dict | None = None


def _read(job: JobContext, kinds: list[str], psf_set: str,
          measured: bool) -> tuple[str, list[tuple[int, str]], int, PsfInputs]:
    """The scope's filter name, each named L2 row's ``(rid, filename)``, a new ``rfid`` and the PSF rows.

    Parameters
    ----------
    job : JobContext
        The reference job.
    kinds : list of str
        The ``psfs`` kinds it must name (:func:`psf_kinds`).
    psf_set : str
        The run's ``[difference] reference_psf_set``.
    measured : bool
        Whether it measures the PSF it stores (``"measured"`` with the catalog on); its ``psfs``
        expects, if any, must be the 18 keys of :func:`~rapidpipe.products.filters.refimage_psf_set`.

    Returns
    -------
    tuple
        The filter, the rows, the ``rfid`` and the :class:`PsfInputs`.
    """
    fid, sca = int(job.scope["fid"]), int(job.scope["sca"])
    l2_inputs = [i for i in job.inputs if i["kind"] == "input" and i["product"] == "l2files"]
    psf_inputs = [i for i in job.inputs if i["kind"] == "input" and i["product"] == "psfs"]
    others = sorted({i["product"] for i in job.inputs if i["kind"] == "input"} - {"l2files", "psfs"})
    if others:
        raise _rejected(f"job {job.job}: reference reads l2files and psfs inputs only, not {others}")
    named = sorted(str(i["key"].get("kind")) for i in psf_inputs)
    if named != kinds:
        raise _rejected(f"job {job.job}: reference names psfs inputs of kinds {kinds} under its "
                        f"run's settings, got {named}")
    expects = [i["key"] for i in job.inputs if i["kind"] == "expects" and i["product"] == "psfs"]
    rows = []
    psfs = PsfInputs()
    with job.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT filter FROM filters WHERE fid = %s", (fid,))
        found = cur.fetchone()
        if found is None:
            raise _rejected(f"job {job.job}: fid {fid} is not in filters")
        if expects:
            measured_set = refimage_psf_set(int(job.scope["field"]), found[0]) if measured else None
            wanted = {(job.dataset, measured_set, fid, s, "reference") for s in SCAS}
            got = {(k.get("dataset"), k.get("psf_set"), int(k["fid"]), int(k["sca"]), k.get("kind"))
                   for k in expects}
            if not measured or len(got) != len(expects) or got != wanted:
                raise _rejected(f"job {job.job}: psfs expects are only the 18 keys of set "
                                f"{measured_set!r} its measured PSF binds, under \"measured\" with "
                                "[reference.psfcat] enabled")
        for item in l2_inputs:
            key = item["key"]
            if int(key["fid"]) != fid:
                raise _rejected(f"job {job.job}: l2files input {key} is not the scope's fid {fid}")
            cur.execute("SELECT rid, filename FROM l2files WHERE job = %s AND dataset = %s"
                        " AND expid = %s AND sca = %s AND fid = %s",
                        (item["source_job"], key["dataset"], key["expid"], int(key["sca"]), fid))
            row = cur.fetchone()
            if row is None:
                raise _rejected(f"job {job.job}: no l2files row {key} in job {item['source_job']}")
            rows.append((int(row[0]), row[1]))
        for item in psf_inputs:
            key = item["key"]
            if (int(key["fid"]), int(key["sca"])) != (fid, sca):
                raise _rejected(f"job {job.job}: psfs input {key} is not the scope's fid and sca")
            if key["kind"] == "reference":
                if key.get("psf_set") != psf_set:
                    raise _rejected(f"job {job.job}: psfs input {key} is not the run's "
                                    f"reference_psf_set {psf_set!r}")
                cur.execute("SELECT psfid, filename, checksum FROM psfs WHERE job = %s AND dataset = %s"
                            " AND psf_set = %s AND fid = %s AND sca = %s AND kind = 'reference'",
                            (item["source_job"], key["dataset"], psf_set, fid, sca))
                row = cur.fetchone()
                if row is None:
                    raise _rejected(f"job {job.job}: no psfs row {key} in job {item['source_job']}")
                psfs = PsfInputs(science=psfs.science, reference={
                    "psfid": int(row[0]), "filename": row[1], "checksum": row[2], "psf_set": psf_set})
                continue
            cur.execute("SELECT filename FROM psfs WHERE job = %s AND dataset = %s AND fid = %s"
                        " AND sca = %s AND kind = 'science'",
                        (item["source_job"], key["dataset"], fid, sca))
            row = cur.fetchone()
            if row is None:
                raise _rejected(f"job {job.job}: no psfs row {key} in job {item['source_job']}")
            psfs = PsfInputs(science=row[0], reference=psfs.reference)
        cur.execute("SELECT nextval(pg_get_serial_sequence('refimages', 'rfid'))")
        rfid = int(cur.fetchone()[0])
        conn.commit()
    return found[0], rows, rfid, psfs


def read_frames(rows: list[tuple[int, str]], unit_filter: str,
                checked: _Checked) -> tuple[list[Frame], list[int]]:
    """The frames to coadd in ``mjdobs`` order, and the ``rid``\\ s past ``max_frames``."""
    least = checked.min_frames if checked.selection_type == "nframes" else 1
    if len(rows) < least:
        raise ValueError(f"{len(rows)} l2files inputs; [reference.selection] min_frames is "
                         f"{checked.min_frames}" if checked.selection_type == "nframes"
                         else "no l2files inputs")
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


def _footprints(job: JobContext, rids: list[int]) -> dict[int, dict[str, Any]]:
    """Each named L2 row's four corners (``([ra1..ra4], [dec1..dec4])``), centre, ``mjdobs`` and key, by ``rid``."""
    with job.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT rid, ra1, dec1, ra2, dec2, ra3, dec3, ra4, dec4, ra0, dec0, mjdobs, expid, sca, fid"
                    " FROM l2files WHERE rid = ANY(%s)", (list(rids),))
        found = {int(r[0]): {"corners": ([float(r[i]) for i in (1, 3, 5, 7)], [float(r[i]) for i in (2, 4, 6, 8)]),
                             "ra0": float(r[9]), "dec0": float(r[10]), "mjdobs": float(r[11]),
                             "key": (int(r[12]), int(r[13]), int(r[14]))}
                 for r in cur.fetchall()}
        conn.commit()
    return found


def estimate_coverage(job: JobContext, frames: list[Frame], checked: _Checked,
                      ra_center: float, dec_center: float) -> tuple[list[Frame], dict[str, Any]]:
    """The named frames' estimated coverage of the reference, and those of them that meet it.

    Parameters
    ----------
    job : JobContext
        The job, for its ``l2files`` rows' corners.
    frames : list of Frame
        The frames to coadd.
    checked : _Checked
        The validated settings.
    ra_center, dec_center : float
        The reference's centre [deg].

    Returns
    -------
    covering : list of Frame
        The frames whose footprint meets the reference, in the loop's candidate order
        (``coverage.candidate_order``), so the coadd's order does not follow ``rid``.
    record : dict
        ``selected`` and ``skipped`` ``rid``\\ s, ``estimate`` (the fraction at
        ``min_coverage_depth``), ``decision`` (build, verify or reject), ``min_coverage_depth``,
        ``min_coverage_fraction`` and ``tolerance``.
    """
    rows = _footprints(job, [f.rid for f in frames])
    missing = [f.rid for f in frames if f.rid not in rows]
    if missing:
        raise _rejected(f"job {job.job}: no l2files corners for rids {missing}")
    frames = sorted(frames, key=lambda f: coverage.candidate_order(
        rows[f.rid]["mjdobs"], rows[f.rid]["ra0"], rows[f.rid]["dec0"], rows[f.rid]["key"], (ra_center, dec_center)))
    corners = {rid: row["corners"] for rid, row in rows.items()}
    w = coverage.make_refimage_wcs(ra_center, dec_center, checked.naxis1, checked.naxis2, checked.cdelt1,
                                   abs(checked.cdelt1), checked.rotation)
    covering, depth = coverage.footprint_depth(w, checked.naxis1, checked.naxis2, [corners[f.rid] for f in frames])
    estimate = coverage.coverage_fraction(depth, checked.min_coverage_depth)
    kept = [frames[i] for i in covering]
    return kept, {"selected": [f.rid for f in kept],
                  "skipped": [f.rid for f in frames if f not in kept],
                  "estimate": estimate,
                  "decision": coverage.coverage_decision(estimate, checked.min_coverage_fraction,
                                                         checked.coverage_estimate_tolerance),
                  "min_coverage_depth": checked.min_coverage_depth,
                  "min_coverage_fraction": checked.min_coverage_fraction,
                  "tolerance": checked.coverage_estimate_tolerance}


# ----------------------------------------------------------------------
# The body
# ----------------------------------------------------------------------


def refimmeta_row(*, rfid: int, field: int, fid: int, ra_center: float, dec_center: float,
                  mjdobs: list[float], measurements: measure.MosaicMeasurements,
                  fwhm: catalog.FwhmStatistics, npucatsources: int | None = None) -> dict[str, list]:
    """The reference's one ``refimmeta`` row, as `dev`'s ``registerCompletedJobsInDB.py`` registers it.

    Parameters
    ----------
    rfid : int
        The reference's ``refimages`` id.
    field : int
        The reference's Roman tessellation field.
    fid : int
        The filter id.
    ra_center, dec_center : float
        The reference's centre [deg], whose NESTED HEALPix indexes are ``hp6``
        and ``hp9``; any RA wraps (``healpix_indexes`` takes it modulo 360).
    mjdobs : list of float
        The coadded frames' ``mjdobs``, one per frame.
    measurements : rapidpipe.science.reference.measure.MosaicMeasurements
        The mosaic's coverage, uncertainty and clipped image statistics.
    fwhm : rapidpipe.science.reference.catalog.FwhmStatistics
        The SExtractor catalog's FWHM and source count.
    npucatsources : int or None
        The Photutils PSF-fit catalog's sources (``npsfcatsources``, 0 when it could not be
        made); ``None`` when ``[reference.psfcat]`` is off and none was attempted.

    Returns
    -------
    dict of str to list
        One-element columns under :data:`REFIMMETA_SCHEMA`'s names.
    """
    hp6, hp9 = healpix_indexes(ra_center, dec_center)
    m = measurements
    row = {"rfid": rfid, "field": field, "hp6": hp6, "hp9": hp9, "fid": fid, "nframes": len(mjdobs),
           "mjdobsmin": min(mjdobs), "mjdobsmax": max(mjdobs), "npixnan": m.npixnan,
           "clmean": m.clmean, "clstddev": m.clstddev, "clnoutliers": m.clnoutliers,
           "gmedian": m.gmedian, "datascale": m.datascale, "gmin": m.gmin, "gmax": m.gmax,
           "cov5percent": m.cov5percent, "medncov": m.medncov, "medpixunc": m.medpixunc,
           "fwhmmedpix": fwhm.fwhmmedpix, "fwhmminpix": fwhm.fwhmminpix, "fwhmmaxpix": fwhm.fwhmmaxpix,
           "nsxcatsources": fwhm.nsexcatsources, "npucatsources": npucatsources}
    return {name: [value] for name, value in row.items()}


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


def catalog_settings(settings: dict[str, Any], zprefimg: float) -> dict[str, Any]:
    """A copy of ``[sextractor_refimage]`` with ``sextractor_mag_zeropoint`` set to the coadd's zero point.

    Parameters
    ----------
    settings : dict
        The job's ``[reference]`` table; not changed.
    zprefimg : float
        The ``MAGZP`` the coadd was scaled to.

    Returns
    -------
    dict
        The SExtractor table the mosaic catalog runs with.
    """
    logger.info("reference-image SExtractor MAG_ZEROPOINT = %s", zprefimg)
    return {**settings["sextractor_refimage"], "sextractor_mag_zeropoint": str(zprefimg)}


def _stamp(path: Path, where: str) -> np.ndarray:
    """A PSF file's stamp, checked as an odd square with a positive finite sum, normalised to unit sum.

    Parameters
    ----------
    path : pathlib.Path
        The PSF file.
    where : str
        The row it came from, for the reason.

    Returns
    -------
    numpy.ndarray
        The normalised stamp.

    Raises
    ------
    CommandExit
        Exit 65 when the stamp is not an odd square or its sum is not positive and finite.
    """
    stamp = np.asarray(fits.getdata(path, 0), dtype=float)
    if stamp.ndim != 2 or stamp.shape[0] != stamp.shape[1] or stamp.shape[0] % 2 == 0:
        raise _rejected(f"{where}: shape {stamp.shape} is not an odd square")
    total = stamp.sum()
    if not np.isfinite(total) or total <= 0:
        raise _rejected(f"{where}: its sum {total} is not positive and finite")
    return stamp / total


def _write_psf(stamp: np.ndarray, target: Path, kind: str, **cards: tuple) -> None:
    """Write ``stamp`` to ``target`` with ``PSFKIND`` ``refimage_psf`` and ``kind`` as its comment."""
    hdr = fits.Header()
    hdr["PSFKIND"] = ("refimage_psf", kind)
    hdr["OVERSAMP"] = (1, "detector pixels")
    for keyword, card in cards.items():
        hdr[keyword] = card
    fits.PrimaryHDU(stamp.astype("float32"), hdr).writeto(target, overwrite=True)


def _set_psf(row: dict, target: Path) -> dict:
    """Write the set's bound row's file, its MD5 and stamp checked, into ``target``; return its record.

    Parameters
    ----------
    row : dict
        The reference ``psfs`` row: ``psfid``, ``filename``, ``checksum`` and ``psf_set``.
    target : pathlib.Path
        Where the PSF is written.

    Returns
    -------
    dict
        ``psf_set`` and ``psfs`` (``psfid``, ``filename``, ``checksum``).

    Raises
    ------
    CommandExit
        Exit 65 when the file is missing, its MD5 is not the row's checksum, or its stamp fails
        :func:`_stamp`.
    """
    where = f"reference psfs {row['psfid']} of set {row['psf_set']!r} ({row['filename']})"
    try:
        path = get(row["filename"])
    except FileNotFoundError as exc:
        raise _rejected(f"{where}: no such file") from exc
    digest = _md5_of_file(path)
    if digest != row["checksum"]:
        raise _rejected(f"{where}: MD5 {digest} is not the row's checksum {row['checksum']}")
    _write_psf(_stamp(path, where), target, "the reference PSF set's",
               PSFSET=(row["psf_set"], "[difference] reference_psf_set"), PSFID=(row["psfid"], "the psfs row"))
    return {"psf_set": row["psf_set"], "psfs": {k: row[k] for k in ("psfid", "filename", "checksum")}}


def reference_psf(kit: Toolkit, mode: str, psfs: PsfInputs, mosaic: Path, cov_map: Path,
                  target: Path) -> tuple[Path | None, dict, refpsf.ReferencePsf | None]:
    """Write the PSF the reference's catalog uses into ``target``, as ``[difference] reference_psf`` says.

    ``"measured"`` measures it on the mosaic's covered, finite pixels with the science PSF as
    the stamp's size, width floor and wings; on any measurement error it falls back to the set's
    row when the job names one, and otherwise writes none. ``"science"`` writes the science PSF
    and ``"precomputed"`` the set's row.

    Parameters
    ----------
    kit : Toolkit
        The stage's tools; ``kit.reference_psf`` measures.
    mode : str
        ``[difference] reference_psf``.
    psfs : PsfInputs
        The job's bound ``psfs`` rows.
    mosaic, cov_map : pathlib.Path
        The mosaic and its coverage map.
    target : pathlib.Path
        Where the PSF is written.

    Returns
    -------
    tuple
        ``target``, or ``None`` when there is no PSF; the manifest's ``refpsf`` record; and the
        measurement when one was made and is the PSF used.

    Raises
    ------
    CommandExit
        Exit 65 for a set's or science file that fails its checks; a fallback that fails them
        carries the measurement's error too.
    """
    start = time.perf_counter()

    def done(record: dict) -> dict:
        return {"mode": mode, **record, "seconds": round(time.perf_counter() - start, 3)}

    if mode == "precomputed":
        return target, done(_set_psf(psfs.reference, target)), None
    science = _stamp(get(psfs.science), f"science psfs ({psfs.science})")
    if mode == "science":
        _write_psf(science, target, "the science PSF")
        return target, done({}), None
    image = np.asarray(fits.getdata(mosaic, 0))
    usable = np.isfinite(image) & (np.asarray(fits.getdata(cov_map, 0)) > 0)
    try:
        measured = kit.reference_psf(image, usable, science)
    except Exception as exc:  # noqa: BLE001 -- any measurement error falls back or leaves no PSF
        del image, usable
        if psfs.reference is None:
            logger.warning("reference PSF: the measurement failed (%s) and the run names no set; "
                           "no PSF-fit catalog", exc)
            return None, done({"measure_error": str(exc)}), None
        logger.warning("reference PSF: the measurement failed (%s); falling back to set %s, psfs %d",
                       exc, psfs.reference["psf_set"], psfs.reference["psfid"])
        try:
            record = _set_psf(psfs.reference, target)
        except Exception as fallback_exc:
            logger.error("reference PSF: the measurement failed (%s) and so did the fallback (%s)",
                         exc, fallback_exc)
            raise CommandExit(int(ExitCode.INPUT_REJECTED),
                              f"{fallback_exc}; the measurement before it: {exc}") from fallback_exc
        return target, done({**record, "fallback": True, "measure_error": str(exc)}), None
    del image, usable
    _write_psf(measured.stamp, target, "measured on the reference mosaic",
               NSTARS=(measured.stars, "stars the PSF was built from"))
    record = {"stars": measured.stars, "detections": measured.detections,
              "passed_cuts": measured.passed_cuts, "sigma_px": measured.sigma_px,
              "star_sigma_px": measured.star_sigma_px, "centroid_px": list(measured.centroid_px)}
    if psfs.reference is not None:
        record.update(fallback=False, measure_error=None)
    logger.info("reference PSF: measured from %d stars, sigma %.3f px", measured.stars, measured.sigma_px)
    return target, done(record), measured


def run(job: JobContext) -> None:
    settings = job.settings["reference"]
    rtid = int(job.scope["field"])
    checked = check_settings(settings)
    mode, psf_set = reference_psf_mode(job.settings.get("difference", {})) if checked.psfcat else ("", "")
    measures = checked.psfcat and mode == "measured"
    with step("read_inputs"):
        unit_filter, rows, rfid, psf_inputs = _read(job, psf_kinds(checked.psfcat, mode, psf_set),
                                                    psf_set, measures)
        coadded, not_coadded = read_frames(rows, unit_filter, checked)
    kit = toolkit()
    paths = settings["paths"]

    if checked.ra_center is None:
        ra_center, dec_center = field_center(rtid)
    else:
        ra_center, dec_center = checked.ra_center, checked.dec_center
    # Commissioning's use_nominal_zptmag (8a4a172d): the one table intake falls back to.
    nominal_zptmag = job.settings["intake"]["nominal_zptmag"] if checked.use_nominal_zptmag else None
    covered = None
    if checked.selection_type == "coverage":
        with step("coverage_estimate"):
            coadded, covered = estimate_coverage(job, coadded, checked, ra_center, dec_center)
        logger.info("reference: estimated coverage %.4f at depth %s from %d frames (%d skipped), %s",
                    covered["estimate"], checked.min_coverage_depth, len(coadded), len(covered["skipped"]),
                    covered["decision"])
        if covered["decision"] == "reject":
            raise _rejected(f"estimated coverage {covered['estimate']:.4f} < {checked.min_coverage_fraction}")
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
        with step("prepare"):
            for frame in coadded:
                shutil.copyfile(frame.path, scratch / frame.name)
                result = prep.reformat_frame(
                    scratch, frame.name, awaicgen_dict, checked.sca_gain, checked.sca_readout_noise,
                    nominal_zptmag=nominal_zptmag)
                total_exptime += result.exptime
                jdstart, jdend = min(jdstart, result.jd), max(jdend, result.jd)
                prepared.append(result)
        prep.write_list_files(scratch, awaicgen_dict, prepared, checked.max_frames)
        zprefimg = prepared[-1].zprefimg
        zptmag_source = "nominal" if checked.use_nominal_zptmag else "header"

        args = awaicgen.build_awaicgen_command_line_args(awaicgen_dict, paths["awaicgen"])
        started = time.monotonic()
        with step("awaicgen"):
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

    if covered is not None:
        with step("verify"):
            with fits.open(o(names["coverage"])) as hdul:
                covered["measured"] = coverage.coverage_fraction(hdul[0].data, checked.min_coverage_depth)
        logger.info("reference: measured coverage %.4f at depth %s (minimum %s)", covered["measured"],
                    checked.min_coverage_depth, checked.min_coverage_fraction)
        if covered["measured"] < checked.min_coverage_fraction:
            raise _rejected(f"measured coverage {covered['measured']:.4f} < {checked.min_coverage_fraction}")

    with step("measure"):
        measurements = measure.measure_mosaic(
            o(names["image"]), o(names["coverage"]), o(names["uncertainty"]),
            sextractor_satur_level=float(settings["sextractor_refimage"]["sextractor_satur_level"]),
            seed=checked.seed)

    with step("catalog"):
        catalog_name, sex_exit = catalog.generate_reference_image_catalog(
            kit.runner, out_dir, catalog_settings(settings, zprefimg), paths["cfg_path"],
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

    # The reference PSF, then the Photutils catalog made with it and the limiting magnitudes.
    refpsf_record, measured, made = None, None, None
    psfcat_metrics: dict[str, Any] = {}
    if checked.psfcat:
        with step("refpsf"):
            psf_path, refpsf_record, measured = reference_psf(
                kit, mode, psf_inputs, o(names["image"]), o(names["coverage"]), o(REFERENCE_PSF_NAME))
        made = psfcat.ReferenceCatalog(produced=False, nsources=0, catalog=None, finder=None, parquet=None)
        if psf_path is not None:
            with step("psfcat"):
                made = psfcat.reference_psf_catalog(
                    settings["psfcat"], out_dir, o(names["image"]), o(names["uncertainty"]), psf_path,
                    seed=checked.seed, make=kit.psf_catalog)
        maglimcat, maglimit = psfcat.limiting_magnitudes(
            o(names["image"]), psf_path, made.catalog, zprefimg, checked.infobits["maglim_snr"])
        psfcat_metrics = {"psfcat_flag": made.produced, "npsfcat": made.nsources,
                          "maglimcat": maglimcat, "maglimit": maglimit}
        logger.info("reference: PSF-fit catalog %s, %d sources", "made" if made.produced else "not made",
                    made.nsources)

    grade = infobits.grade(
        o(names["image"]), o(names["coverage"]),
        paths["cfg_path"] + "/" + infobits.DEFINITIONS_FILE, checked.infobits,
        gmedian=measurements.gmedian, datascale=measurements.datascale,
        satcount=measurements.npixsat, fwhm_mode_pix=fwhm.fwhmmedpix,
        fwhm_pctl_pix=fwhm.fwhmpctlpix, nsexcat=fwhm.nsexcatsources,
        input_zptmags=[p.zptmag for p in prepared], **psfcat_metrics)
    logger.info("reference: InfoBits %d (bits %s), status %d (fatal bits %s); metrics %s",
                grade.infobits, grade.bits, grade.status, checked.infobits["fatalinfobits"],
                grade.metrics)

    stamp = header.StampValues(
        field=rtid, exposure_filter=unit_filter, cov5percent=measurements.cov5percent,
        nframes=len(prepared), refimage_input_filenames=tuple(f.name for f in coadded),
        jdstart=jdstart, jdend=jdend, zprefimg=zprefimg, total_refimage_exptime=total_exptime,
        run=job.run, job=job.job, attempt=job.attempt,
        infobits=grade.infobits, status=grade.status, infobit_cards=tuple(grade.cards),
        zptmag_source=zptmag_source,
        **({"coverage_fraction": covered["measured"], "coverage_depth": checked.min_coverage_depth,
            "coverage_target": checked.min_coverage_fraction} if covered is not None else {}))
    header.add_keywords_to_reference_image_header(o(names["image"]), stamp)
    header.add_keywords_to_reference_image_header(o(names["uncertainty"]), stamp)
    if grade.status == 0:
        fatal = sorted(set(grade.bits) & set(checked.infobits["fatalinfobits"]))
        raise _rejected(f"reference status 0: fatal InfoBits {', '.join(map(str, fatal))} "
                        f"(infobits {grade.infobits})")

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
        "refimmeta": pa.table(refimmeta_row(
            rfid=rfid, field=rtid, fid=int(job.scope["fid"]), ra_center=ra_center, dec_center=dec_center,
            mjdobs=[f.header.mjdobs for f in coadded], measurements=measurements, fwhm=fwhm,
            npucatsources=made.nsources if made is not None else None),
            schema=REFIMMETA_SCHEMA),
    }
    if made is not None and made.produced:
        tables["refimcatalogs"] = pa.concat_tables([tables["refimcatalogs"], pa.table({
            "rfid": [rfid], "cattype": [PSFCAT_CATALOG_TYPE], "filename": [url(made.catalog.name)],
            "checksum": [_md5_of_file(made.catalog)]}, schema=REFIMCATALOGS_SCHEMA)])
    # A measured PSF is stored as this reference's own set, one row per SCA sharing its one file.
    if measured is not None:
        stored = refimage_psf_set(rtid, unit_filter)
        checksum = _md5_of_file(o(REFERENCE_PSF_NAME))
        tables["psfs"] = pa.table({
            "dataset": [job.dataset] * len(SCAS), "fid": [int(job.scope["fid"])] * len(SCAS),
            "sca": list(SCAS), "kind": ["reference"] * len(SCAS), "psf_set": [stored] * len(SCAS),
            "filename": [url(REFERENCE_PSF_NAME)] * len(SCAS), "checksum": [checksum] * len(SCAS),
            "status": [PSF_STATUS_GOOD] * len(SCAS)}, schema=PSFS_SCHEMA)
        refpsf_record["stored_set"] = stored
    for table, data in tables.items():
        pq.write_table(data, job.path / f"{table}.parquet")
    logger.info("reference: field %s %s coadded %d frames -> rfid %s, %d catalog sources",
                rtid, unit_filter, len(coadded), rfid, fwhm.nsexcatsources)
    extra: dict[str, Any] = {}
    if refpsf_record is not None:
        extra["refpsf"] = refpsf_record
    expects_psfs = any(i["kind"] == "expects" and i["product"] == "psfs" for i in job.inputs)
    if expects_psfs and "psfs" not in tables:
        extra["unbound"] = ["psfs"]
    job.write_manifest(
        [(table, f"{table}.parquet") for table in tables], **extra,
        reference={"rfid": rfid, "ra_center": ra_center, "dec_center": dec_center,
                   "jd_start": jdstart, "jd_end": jdend, "total_exptime": total_exptime,
                   "zero_point": zprefimg, "zptmag_source": zptmag_source,
                   **measurements.to_dict(),
                   "fwhmmedpix": fwhm.fwhmmedpix, "fwhmminpix": fwhm.fwhmminpix,
                   "fwhmmaxpix": fwhm.fwhmmaxpix, "nsexcatsources": fwhm.nsexcatsources,
                   "fwhmpctlpix": fwhm.fwhmpctlpix, "infobits": grade.infobits,
                   "status": grade.status, "quality": grade.metrics,
                   "not_coadded": not_coadded, **({"coverage": covered} if covered is not None else {}),
                   **({"npsfcatsources": made.nsources, "psfcat_flag": made.produced}
                      if made is not None else {})},
        metrics=metrics)
