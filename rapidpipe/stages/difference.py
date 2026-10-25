# ported-from: pipeline/awsBatchSubmitJobs_runSingleSciencePipeline.py @ d6aac515
"""``difference``: one epoch's L2 image minus its reference, ZOGY then SFFT, as `dev` does it.

Ported from `origin/dev`'s ``pipeline/awsBatchSubmitJobs_runSingleSciencePipeline.py``
(its science only: the S3 transfers, the job-config parsing and the
reference-image generation branch are not the stage's job), refreshed to
dev d6aac515. It runs as ``rapidpipe job local|run|submit --stage
difference`` under :class:`rapidpipe.foundation.local.JobContext`:
``run(job)`` reads what its job's recorded inputs name, writes the files
and ``manifest.json`` under ``job.path`` and returns; the caller
publishes. The steps run in `dev`'s order, each one a function in
``rapidpipe.science.difference``:

reformat and simple-model uncertainty -> SExtractor science catalog and
point-source FWHM -> SIP to PV and three SWarp resamplings of the
reference bundle -> PSF normalisation -> bkgest on the science image and
on the resampled reference -> the reference PSF measured from the
resampled reference's stars -> gain matching (``scalefacref``, x/y RMS and
medians) -> NaN replacement and artifact repair -> subpixel offset of the
reference by the medians -> ZOGY -> coverage mask, NaN restore, negation
-> difference uncertainty -> SExtractor and Photutils catalogs, positive
and negative. SFFT and the naive subtraction follow ZOGY and run as `dev`
runs them (``[sfft] run_sfft``, ``[naive_diffimage] naive_diffimage_flag``).
``[difference] diff_flavor`` names the published differencer, ``zogy``
(the shipped default, with the measurement in the toml) or ``sfft`` (`dev`'s
since b725007d, 2026-04-20); under ``sfft``, ``run_sfft = false`` fails the
settings check. A nonzero exit of either tool fails the job as any tool's
does (:class:`~rapidpipe.science.difference.tools.ToolFailed`), never a
fallback to the other.

Scope: ``{"expid", "sca", "fid"}``, the epoch's key without the dataset
(the run's). Settings: the run's ``[difference]`` table, whose subtables
are `dev`'s ini sections (``rapidpipe/settings/foundation-thin.toml``).

Inputs, every one an ``input`` row, read in one short transaction and
fetched with :func:`rapidpipe.foundation.prefix.get`:

- ``l2files``: exactly one, the epoch's key.
- ``refimages``: exactly one, of the scope's filter (an imported
  reference or one the reference stage made; the stage never cares
  which). Its coverage and uncertainty maps sit beside the mosaic, and
  its mosaic SExtractor catalog (``refimcatalogs`` cattype 1) gives the
  reference FWHM.
- ``psfs``: exactly one, the science-image PSF (key ``kind`` ``science``)
  of the scope's filter and SCA.

The reference-image PSF is not an input: ZOGY needs the PSF of the image
it is given, the reference after the per-epoch SWarp resample, so the job
measures it there (:func:`rapidpipe.science.difference.refpsf.measure`,
after the reference background subtraction, on pixels finite and covered
at ``post_zogy_keep_diffimg_lower_cov_map_thresh``) and writes it as
``refimage_psf.fits``. Too few stars fails the job with exit 65 and the
reason (``refpsf: N stars, 10 needed``) on the job row; there is no
fallback. ``[difference] reference_psf = "science"`` is a dataset's
explicit choice instead (the GBTDS overlay's): no measurement, the science
PSF written as ``refimage_psf.fits``, the manifest's ``refpsf`` saying so,
the reference PSF's own metrics absent, and the residual, ring and
registration stars taken from the reference mosaic's SExtractor catalog
mapped onto the science grid (:func:`_catalog_stars`) instead of the
measurement's finder. `dev` uses a static offline-compiled reference PSF per detector
instead, so this is a recorded divergence (rapid_docs ``decisions.md``,
2026-10-24: the empirical PSF was 15% wider than the science ePSF the
``psf`` stage had handed ZOGY for both, run 17).

Outputs under ``job.path``. ``[paths] work_subdirectory`` holds what the
packet stage and a person read, and nothing else (:func:`trim`, Ben's
ruling of 2026-10-22): the published differencer's difference image
(``[difference] diff_flavor``), its uncertainty, its mask (the resampled reference coverage map it is masked with), the
positive and negative PSF-fit catalogs of SFFT and of ZOGY
(``*_psfcat[_negative].parquet``, uncapped; all but the published
differencer's positive one are diagnostics, read by no stage; SFFT's two carry `dev`'s ``sumrat``,
``nneg`` and ``nbad`` on every source, :mod:`rapidpipe.science.difference.columns`,
each catalog measured on the image its sources are positive in), and the packet
stage's other reads beside the difference image, the published
differencer's difference PSF (forced photometry), the background-subtracted science
image (forced photometry and the science cutout) and the resampled
gain-matched reference (the template cutout), and the measured reference
PSF (``refimage_psf.fits``). Every FITS file is
gzipped (``.fits.gz``, level 1, as the intake's). The rest of `dev`'s
diagnostic files (the other differencer's and naive's images, S_corr, intermediate
images, text catalogs) is not published; a variant is re-created by
rerunning the job. Declared tables: ``diffimages`` (one
row, the published differencer's image, ``filename`` the ``.fits.gz``; ``pid`` is drawn from the
table's identity sequence so the sources can carry it; ``infobitssci`` is
`dev`'s catalog-outcome mask, ``infobitsref`` the reference's infobits)
and ``sources`` (the published differencer's positive PSF-fit catalog through
:func:`rapidpipe.science.load.catalogs.source_set`, under ``diff_flavor =
"sfft"`` through `dev`'s database prefilter first
(``[difference.database_prefilter]``), then capped at ``max_sources``
as the import caps it; a source in a bright reference source's footprint
carries :data:`rapidpipe.products.diffimage.SOURCE_FLAG_BRIGHT_FOOTPRINT`
in ``flags``, informational, nothing dropped: :func:`flag_bright_footprint`),
``objects`` and ``memberships`` (the association, :func:`associate`: the
plan's amendment of 2026-10-24 moved it here from the retired associate
stage; the job reads the objects of its run in its sources' fields and
their neighbours as they stand, records that read in the manifest's
``reads``, matches its sources in memory, each founding in its own field
wherever on the image it falls (the field rule, runbook part 12 step 1),
and publishes the objects it founded and its memberships, which the
publisher reconciles under the touched fields' locks against those founded
since the read, :mod:`rapidpipe.foundation.catalog`).
The manifest records `dev`'s gain-matching and
catalog measurements under ``difference``, the reference PSF's
measurement under ``difference.refpsf``, and ``metrics``, one flat dict of
named numbers (``refpsf_sigma_px`` and ``sci_psf_sigma_px``, the two
stamps' second-moment sigma within 4 px, px; ``refpsf_stars``, the stars
built from; ``psf_centroid_offset_x_px`` and ``_y_px``, the reference
stamp's centroid minus the science stamp's, px: their relative centring
is what ZOGY sees; ``ref_sigma_px``, the median second-moment sigma of
the stars the reference PSF was built from, on the resampled reference,
px, and ``ref_sci_sigma_ratio``, it over ``sci_psf_sigma_px``;
``noise_correlation``, the published difference's 4 x 4 block-mean
sigma times 4 over its pixel sigma, both clipped, 1 for white noise,
absent when the image has too few full blocks,
:func:`rapidpipe.science.difference.noise.noise_correlation`; and
:mod:`rapidpipe.science.difference.metrics`' registration offset, core
and ring fractions, usable fraction and source counts, ``n_candidates``
before the cap and ``n_sources`` after it, the bright footprint's
``n_in_bright_footprint`` and ``bright_footprint_fraction``; ``scalefacref``,
the gain match's scale applied to the reference; ``sci_star_sigma_px``, the
median second-moment sigma of the registration stars on the
background-subtracted science image, and ``sci_star_psf_sigma_ratio``, it
over ``sci_psf_sigma_px``; and the
uncapped catalog's ``n_sources_near_bright_px`` and
``n_sources_ring_snr_gt20``, defined there; ``pixel_columns_s``, the
seconds the pixel columns took for both SFFT catalogs; ``n_objects_read``,
``n_objects_founded``, ``n_memberships`` and ``n_sources_out_of_field``,
the accepted sources outside the image's own field, the association's). The
residual stars' positions sit beside them as ``difference.residual_stars``
for a reader's gallery. A metric the job cannot make is absent.

Determinism: on identical inputs the stage writes identical pixels and
rows (FITS ``DATE`` cards and the bkgest tables' time stamps aside) when
``[statistics] clip_correction_seed`` is non-negative, as the shipped
settings have it. `dev`'s unseeded clipping-correction draw (negative)
moves every clipped standard deviation by about 3e-4 and the source count
by a few (foundation/determinism).

Departures from `dev` (difference page): only the published differencer's
files are rows (a run has one differencer; `dev` publishes SFFT's), the
other differencer's PSF-fit catalogs are kept under the prefix as
diagnostics and naive's files are not (the trim above; all three still
run, for the outcome bits and the diagnostics); the naive subtraction's
Photutils PSF-fit catalogs are not made (its image, uncertainty and
SExtractor catalogs are, as `dev` makes them), so its two outcome bits
are never set (decisions page, 28 to 72 min of a RimTimSim F213
difference went to PSF-fit catalogs, PR 437); a missing PSF-fit catalog
of the published differencer publishes an empty source set with its outcome bit set, where
`dev`'s loader skips the job.

This module may import ``rapidpipe.products``, ``rapidpipe.db``,
``rapidpipe.foundation`` and ``rapidpipe.science``; never another stage,
``rapidpipe.launch`` or ``rapidpipe.cli``.
"""

from __future__ import annotations

import gzip
import hashlib
import logging
import math
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from astropy.io import fits
from astropy.wcs import WCS

from rapidpipe.exitcodes import CommandExit, ExitCode
from rapidpipe.foundation.local import JobContext
from rapidpipe.foundation.prefix import get
from rapidpipe.products.diffimage import (
    INFORMATIONAL_SOURCE_FLAGS, SOURCE_FLAG_BRIGHT_FOOTPRINT, catalog_outcome_bit)
from rapidpipe.science.crossmatch.memory import associate_epoch
from rapidpipe.science.difference import (
    background,
    columns,
    gainmatch,
    masking,
    naive,
    noise,
    offsets,
    metrics as quality,
    psf,
    psfcat,
    reformat,
    refpsf,
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
from rapidpipe.science.intake import gzip_file
from rapidpipe.science.load.catalogs import source_set
from rapidpipe.science.spatial import field_neighbours, image_centre_and_corners, tessellation_field

logger = logging.getLogger(__name__)

#: `refimcatalogs.cattype` of the reference mosaic's SExtractor catalog.
REFERENCE_CATALOG_TYPE = 1
#: The status of a difference image the stage finished.
STATUS_GOOD = 1
#: The ``psfs`` key kind the job names, one.
PSF_KINDS = ("science",)
#: The measured reference PSF's file name in the work directory.
REFERENCE_PSF_NAME = "refimage_psf.fits"
#: The gzip level of the published FITS files, the intake's.
GZIP_LEVEL = 1
#: The reference mosaic catalog's columns the job reads: position, flux, the footprint's aperture S/N.
REFERENCE_CATALOG_COLUMNS = ["ALPHAWIN_J2000", "DELTAWIN_J2000", "FLUX_AUTO", "FLUX_APER_4",
                             "FLUXERR_APER_4"]

#: `dev`'s pass 2 uses the field's inclusion cone only for a tile with this many neighbours.
#: The fields' objects of the job's run as they stand: every published difference job's founded ones.
OBJECTS_SQL = ("SELECT o.job, o.aid, o.field, o.ra0, o.dec0 FROM objects o JOIN jobs j ON j.id = o.job"
               " WHERE j.run = %s AND o.dataset = %s AND o.survey = %s AND o.field = ANY(%s)")

OBJECTS_SCHEMA = pa.schema([
    ("aid", pa.int64()), ("dataset", pa.string()), ("survey", pa.string()), ("field", pa.int32()),
    ("ra0", pa.float64()), ("dec0", pa.float64()),
])
MEMBERSHIPS_SCHEMA = pa.schema([
    ("object_job", pa.int64()), ("aid", pa.int64()), ("source_job", pa.int64()), ("sid", pa.int64()),
    ("dra", pa.float64()), ("ddec", pa.float64()), ("flux", pa.float64()),
])

DIFFIMAGES_SCHEMA = pa.schema([
    ("pid", pa.int32()), ("dataset", pa.string()), ("expid", pa.int32()), ("sca", pa.int16()),
    ("fid", pa.int16()), ("rid", pa.int32()), ("rfid", pa.int32()), ("field", pa.int32()),
    ("jd", pa.float64()),
    *((f"{c}{n}", pa.float64()) for n in range(5) for c in ("ra", "dec")),
    ("infobitssci", pa.int32()), ("infobitsref", pa.int32()), ("filename", pa.string()),
    ("checksum", pa.string()), ("status", pa.int16()),
])


@dataclass
class Toolkit:
    """The stage's boundary with external software; a test replaces :func:`toolkit`.

    ``runner`` runs SExtractor, SWarp, bkgest, py_zogy.py and SFFT;
    ``sip_to_pv`` rewrites a FITS header's SIP distortion as PV in place;
    ``psf_catalog`` makes one Photutils catalog
    (:func:`rapidpipe.science.difference.psfcat.psf_catalog`'s signature);
    ``reference_psf`` measures the reference PSF
    (:func:`rapidpipe.science.difference.refpsf.measure`'s signature).
    """

    runner: Any = field(default_factory=ToolRunner)
    sip_to_pv: Callable = resample._default_sip_to_pv
    psf_catalog: Callable = psfcat.psf_catalog
    reference_psf: Callable = refpsf.measure


def toolkit() -> Toolkit:
    return Toolkit()


# ----------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------


def check_settings(settings: dict[str, Any]) -> None:
    """Validate the ``[difference]`` table; every problem is a ``ValueError``."""
    if settings["fake_sources"]["inject_fake_sources_flag"]:
        raise ValueError(
            "[difference.fake_sources] inject_fake_sources_flag = true is not supported: "
            "fake-source injection is not ported to the rebuild")
    flavor = settings.get("diff_flavor", "zogy")
    if flavor not in ("zogy", "sfft"):
        raise ValueError(f"[difference] diff_flavor = {flavor!r}: zogy or sfft")
    if flavor == "sfft" and not settings["sfft"]["run_sfft"]:
        raise ValueError(
            "[difference.sfft] run_sfft = false: SFFT is the published differencer, so the "
            "job would publish nothing; there is no fallback to ZOGY")
    _zero_point_override(settings)
    _prefilter_threshold(settings)
    cap = settings.get("max_sources", 0)
    if not isinstance(cap, int) or isinstance(cap, bool) or cap < 0:
        raise ValueError(f"[difference] max_sources must be a non-negative integer, got {cap!r}")


def _prefilter_threshold(settings: dict[str, Any]) -> float | None:
    """``[difference.database_prefilter] sumrat_prefilter_threshold``: empty
    means no prefilter at all (`dev`: the section missing), otherwise the
    number. Applied to SFFT's catalog only, the one that carries sumrat."""
    value = settings.get("database_prefilter", {}).get("sumrat_prefilter_threshold", "")
    if value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("[difference.database_prefilter] sumrat_prefilter_threshold must be "
                         f"empty or a number, got {value!r}")
    return float(value)


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
        raise ValueError(f"[difference.awaicgen] zprefimg must be empty or a number, got {value!r}")
    return float(value)


def _resolve_zero_point(settings: dict[str, Any], reference_image: Path) -> tuple[float, str]:
    """The reference zero point gain matching uses, and where it came from.

    An explicit ``[awaicgen] zprefimg`` override always wins. Otherwise the
    reference image's ``MAGZP`` (the file as named, not the resampled copy,
    whose header is the science image's): the value the reference was
    built on, which is what `dev`'s per-filter ``zprefimg_<filter>`` lookup
    (a2825eba) must resolve to. Missing or not a finite number fails.
    """
    override = _zero_point_override(settings)
    if override is not None:
        logger.info("zero point source=override; value=%s", override)
        return override, "override"
    header = fits.getheader(reference_image, 0)
    if "MAGZP" not in header:
        raise ValueError("reference image header has no MAGZP keyword and [awaicgen] zprefimg "
                         "is not set")
    try:
        value = float(header["MAGZP"])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"reference image header MAGZP is not a number: {exc}") from exc
    if not math.isfinite(value):
        raise ValueError(f"reference image header MAGZP is not finite ({value!r})")
    logger.info("zero point source=header; value=%s", value)
    return value, "header"


# ----------------------------------------------------------------------
# Inputs
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Inputs:
    """What the job's inputs name, with local copies of their files."""

    rid: int
    exptime: float
    mjdobs: float
    infobits_science: int
    rfid: int
    infobits_reference: int
    science_image: Path
    reference_image: Path
    reference_coverage: Path
    reference_uncertainty: Path
    reference_catalog: Path
    science_psf: Path
    pid: int


def _beside(url: str, name: str) -> Path:
    """The file ``name`` in the same directory as the file ``url``, fetched like it."""
    return get(url.rsplit("/", 1)[0] + "/" + name)


def _rejected(reason: str) -> CommandExit:
    """A declared input the job cannot use: exit 65, input rejected, the reason on its row."""
    return CommandExit(int(ExitCode.INPUT_REJECTED), reason)


def read_inputs(job: JobContext) -> Inputs:
    """Read the named rows in one short transaction, fetch their files and draw a ``pid``."""
    key = {"dataset": job.dataset, "expid": int(job.scope["expid"]),
           "sca": int(job.scope["sca"]), "fid": int(job.scope["fid"])}
    named = [i for i in job.inputs if i["kind"] == "input"]
    by_product: dict[str, list[dict]] = {}
    for item in named:
        by_product.setdefault(item["product"], []).append(item)
    others = sorted(set(by_product) - {"l2files", "refimages", "psfs"})
    if others:
        raise _rejected(f"job {job.job}: difference reads l2files, refimages and psfs, not {others}")
    counts = {p: len(by_product.get(p, [])) for p in ("l2files", "refimages")}
    psf_kinds = sorted((i["key"].get("kind") for i in by_product.get("psfs", [])), key=str)
    if counts != {"l2files": 1, "refimages": 1} or psf_kinds != sorted(PSF_KINDS):
        raise _rejected(f"job {job.job}: difference names one l2files, one refimages and one "
                        f"psfs input of key kind {PSF_KINDS}, got {counts} and psfs "
                        f"kinds {psf_kinds}")
    (l2_in,), (ref_in,) = by_product["l2files"], by_product["refimages"]
    if l2_in["key"] != key:
        raise _rejected(f"job {job.job}: l2files input {l2_in['key']} is not the epoch {key}")
    if int(ref_in["key"]["fid"]) != key["fid"]:
        raise _rejected(f"job {job.job}: refimages input {ref_in['key']} is not the scope's fid")

    with job.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT rid, filename, exptime, mjdobs, infobits FROM l2files WHERE job = %s"
                    " AND dataset = %s AND expid = %s AND sca = %s AND fid = %s",
                    (l2_in["source_job"], key["dataset"], key["expid"], key["sca"], key["fid"]))
        l2 = cur.fetchone()
        if l2 is None:
            raise _rejected(f"job {job.job}: no l2files row {key} in job {l2_in['source_job']}")
        ref_key = ref_in["key"]
        cur.execute("SELECT rfid, filename, infobits FROM refimages WHERE job = %s"
                    " AND dataset = %s AND field = %s AND fid = %s",
                    (ref_in["source_job"], ref_key["dataset"], ref_key["field"], ref_key["fid"]))
        ref = cur.fetchone()
        if ref is None:
            raise _rejected(f"job {job.job}: no refimages row {ref_key} in job "
                            f"{ref_in['source_job']}")
        cur.execute("SELECT filename FROM refimcatalogs WHERE job = %s AND rfid = %s"
                    " AND cattype = %s ORDER BY rfcatid DESC LIMIT 1",
                    (ref_in["source_job"], ref[0], REFERENCE_CATALOG_TYPE))
        refcat = cur.fetchone()
        if refcat is None:
            raise _rejected(f"job {job.job}: reference rfid {ref[0]} has no mosaic catalog "
                            f"(refimcatalogs cattype {REFERENCE_CATALOG_TYPE})")
        psf_urls = {}
        for item in by_product["psfs"]:
            psf_key = item["key"]
            if (int(psf_key["fid"]), int(psf_key["sca"])) != (key["fid"], key["sca"]):
                raise _rejected(f"job {job.job}: psfs input {psf_key} is not the scope's fid "
                                f"and sca")
            cur.execute("SELECT filename FROM psfs WHERE job = %s AND dataset = %s AND fid = %s"
                        " AND sca = %s AND kind = %s",
                        (item["source_job"], psf_key["dataset"], psf_key["fid"], psf_key["sca"],
                         psf_key["kind"]))
            row = cur.fetchone()
            if row is None:
                raise _rejected(f"job {job.job}: no psfs row {psf_key} in job "
                                f"{item['source_job']}")
            psf_urls[psf_key["kind"]] = row[0]
        cur.execute("SELECT nextval(pg_get_serial_sequence('diffimages', 'pid'))")
        pid = int(cur.fetchone()[0])
        conn.commit()

    ref_url = ref[1]
    image_name = ref_url.rsplit("/", 1)[-1]
    if not image_name.endswith("_image.fits"):
        raise _rejected(f"job {job.job}: reference mosaic {image_name} is not awaicgen's "
                        f"*_image.fits")
    return Inputs(
        rid=int(l2[0]), exptime=float(l2[2]), mjdobs=float(l2[3]), infobits_science=int(l2[4] or 0),
        rfid=int(ref[0]), infobits_reference=int(ref[2] or 0),
        science_image=get(l2[1]), reference_image=get(ref_url),
        reference_coverage=_beside(ref_url, image_name.replace("_image.fits", "_cov_map.fits")),
        reference_uncertainty=_beside(ref_url,
                                      image_name.replace("_image.fits", "_uncert_image.fits")),
        reference_catalog=get(refcat[0]),
        science_psf=get(psf_urls["science"]), pid=pid)


def _stage_input(source: Path, work_dir: Path, taken: set[str]) -> str:
    """Copy one input file into the work directory by base name; return the name."""
    name = source.name
    if name in taken:
        raise ValueError(f"two input files share the name {name!r}")
    taken.add(name)
    shutil.copyfile(source, work_dir / name)
    return name


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
# Catalog blocks
# ----------------------------------------------------------------------


@dataclass
class _Catalogs:
    """One differencer's catalog outcome: counts, files, and the mask bits it set."""

    sextractor: dict[str, int] = field(default_factory=dict)
    photutils: dict[str, int | None] = field(default_factory=dict)
    parquet: dict[str, str | None] = field(default_factory=dict)
    bits: int = 0
    pixel_columns_s: float | None = None


def _sextractor_catalogs(*, kit: Toolkit, work_dir: Path, settings: dict[str, Any],
                         positive: dict[str, str], negative: dict[str, str], weight_image: str,
                         override_weight_type_and_filter: bool,
                         overrides: dict[str, str] | None) -> dict[str, int]:
    """`dev`'s SExtractor block, positive then negative; the source count per sign."""
    paths = settings["paths"]
    return {sign: sextractor.difference_image_catalog(
        kit.runner, work_dir, settings["sextractor_diffimage"], paths["cfg_path"],
        detection_image=names["detection"], input_image=names["input"],
        weight_image=weight_image, catalog_name=names["catalog"],
        override_weight_type_and_filter=override_weight_type_and_filter,
        overrides=overrides, executable=paths["sextractor"])
        for sign, names in (("positive", positive), ("negative", negative))}


def _run_catalogs(*, differencer: str, kit: Toolkit, work_dir: Path, settings: dict[str, Any],
                  positive: dict[str, str], negative: dict[str, str], weight_image: str,
                  psf_image: str, sky_coords_image: str, psfcat_names: dict[str, str],
                  override_weight_type_and_filter: bool, write_parquet: bool,
                  overrides: dict[str, str] | None, seed: int | None) -> _Catalogs:
    """`dev`'s SExtractor then Photutils blocks, positive then negative.

    ``positive``/``negative`` map ``detection``, ``input`` and ``catalog``
    to file names. ``psfcat_names`` holds the configured positive
    Photutils names; the negative ones are derived as `dev` derives them.
    """
    result = _Catalogs(sextractor=_sextractor_catalogs(
        kit=kit, work_dir=work_dir, settings=settings, positive=positive, negative=negative,
        weight_image=weight_image, override_weight_type_and_filter=override_weight_type_and_filter,
        overrides=overrides))

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
            psfcat_settings, image=str(work_dir / names["input"]),
            uncertainty=str(work_dir / weight_image), psf=str(work_dir / psf_image),
            sky_coords_image=str(work_dir / sky_coords_image), catalog=str(work_dir / catalog),
            finder=str(work_dir / finder), residual=str(work_dir / residual),
            parquet=str(work_dir / parquet) if parquet else None,
            label=f"{sign} {differencer} difference image", seed=seed)
        if not outcome.produced:
            result.bits |= catalog_outcome_bit(differencer, sign)
        result.photutils[sign] = outcome.nsources
        result.parquet[sign] = parquet if outcome.produced else None
    return result


# ----------------------------------------------------------------------
# The body
# ----------------------------------------------------------------------


def run(job: JobContext) -> None:
    settings = job.settings["difference"]
    check_settings(settings)
    inputs = read_inputs(job)
    kit = toolkit()
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

    work_dir = job.path / paths["work_subdirectory"]
    work_dir.mkdir(parents=True, exist_ok=True)
    taken: set[str] = set()
    staged = {name: _stage_input(path, work_dir, taken) for name, path in (
        ("science", inputs.science_image), ("reference", inputs.reference_image),
        ("coverage", inputs.reference_coverage), ("uncertainty", inputs.reference_uncertainty),
        ("catalog", inputs.reference_catalog), ("science_psf", inputs.science_psf))}
    science_image_filename = _unzip_science_image(work_dir, staged["science"])

    def w(name: str) -> Path:
        return work_dir / name

    # Image statistics for resizing (dev: HDU 1 of the delivered file).
    stats_sci_img = statistics.fits_data_statistics_with_clipping(
        w(science_image_filename), 3.0, 1, saturation_level_sciimage, seed=seed)
    avg_sci_img = stats_sci_img["clippedavg"]

    # Reformat and simple-model uncertainty (dev's OpenUniverse ZPTMAG fix is
    # off by default and not ported).
    reformatted_science_image_filename = science_image_filename.replace(".fits", "_reformatted.fits")
    reformatted_science_uncert_image_filename = science_image_filename.replace(
        ".fits", "_reformatted_unc.fits")
    reformat.reformat_simdata_fits_file_and_compute_uncertainty_image_via_simple_model(
        w(science_image_filename), sca_gain, sca_readout_noise, avg_sci_img,
        w(reformatted_science_image_filename), w(reformatted_science_uncert_image_filename))

    # Science-image catalog and point-source FWHM; reference FWHM from its catalog.
    filename_sciimage_catalog = reformatted_science_image_filename.replace(".fits", "_secat.txt")
    sextractor.generate_science_image_catalog(
        kit.runner, work_dir, reformatted_science_image_filename,
        reformatted_science_uncert_image_filename, cfg_path, settings["sextractor_sciimage"],
        filename_sciimage_catalog, executable=paths["sextractor"])
    fwhm_sci, _ = sextractor.fwhm_from_catalog(
        w(filename_sciimage_catalog), cfg_path + "/rapidSexParamsSciImage.inp", "sci")
    fwhm_ref, _ = sextractor.fwhm_from_catalog(
        w(staged["catalog"]), cfg_path + "/rapidSexParamsRefImage.inp", "ref")

    # SWarp the reference bundle into the science image's distortion frame.
    # The reference (an awaicgen coadd) has no distortion: no PV conversion.
    (sci_fits_file_with_pv, _ref_pv, _ref_cov_pv, _ref_unc_pv,
     output_resampled_reference_image,
     output_resampled_reference_cov_map,
     output_resampled_reference_uncert_image) = \
        resample.resample_reference_image_to_science_image_with_pv_distortion(
            kit.runner, work_dir, reformatted_science_image_filename, 0,
            staged["reference"], staged["coverage"], staged["uncertainty"],
            0, False, settings["swarp"], executable=paths["swarp"], sip_to_pv=kit.sip_to_pv)
    for output in (output_resampled_reference_image, output_resampled_reference_cov_map,
                   output_resampled_reference_uncert_image):
        # SWarp has read the .head symlinks; they would publish as copies.
        w(output.replace(".fits", ".head")).unlink(missing_ok=True)

    # Normalise the science PSF (the reference PSF is measured below).
    filename_sciimage_psf_normalized = staged["science_psf"].replace(".fits", "_normalized.fits")
    psf.normalize_image(w(staged["science_psf"]), 0, w(filename_sciimage_psf_normalized))

    # Background-subtract the science image and, with [bkgest]
    # subtract_from_reference, the resampled reference with the same bkgest.
    bkgest = settings["bkgest"]
    swarp_subtracts = settings["swarp"]["swarp_subtract_back"].strip().upper() != "N"
    filename_bkg_subbed_science_image = background.subtract_background(
        kit.runner, work_dir, paths["bkgest_code"], paths["bkgest_include_dir"],
        sci_fits_file_with_pv, bkgest)
    if not bkgest["subtract_from_reference"] and not swarp_subtracts:
        logger.warning("neither SWarp nor bkgest subtracts the reference-image background")
    if bkgest["subtract_from_reference"]:
        if swarp_subtracts:
            logger.warning("SWarp SUBTRACT_BACK is on, so bkgest only removes what SWarp left behind")
        background.subtract_reference_background(
            kit.runner, work_dir, paths["bkgest_code"], paths["bkgest_include_dir"],
            output_resampled_reference_image, output_resampled_reference_cov_map, bkgest)

    # The reference PSF, measured on the resampled, background-subtracted reference.
    refpsf_record, metrics, measured_psf = _measure_reference_psf(
        kit, w(output_resampled_reference_image), w(output_resampled_reference_cov_map), thresh,
        w(filename_sciimage_psf_normalized), w(REFERENCE_PSF_NAME), settings["reference_psf"])
    # The reference catalog's sky positions; the metrics' stars, the measurement's finder stars
    # or, under reference_psf = "science", the catalog's own (_catalog_stars).
    reference_sky = sextractor.parse_ascii_text_sextractor_catalog(
        w(staged["catalog"]), cfg_path + "/rapidSexParamsRefImage.inp", REFERENCE_CATALOG_COLUMNS)
    science_pixels = fits.getdata(w(filename_bkg_subbed_science_image), 0).astype(float)
    reference_pixels = fits.getdata(w(output_resampled_reference_image), 0).astype(float)
    found, stars_xy = measured_psf.found, measured_psf.stars_xy
    if settings["reference_psf"] == "science":
        found, stars_xy = _catalog_stars(
            reference_sky, fits.getheader(w(filename_bkg_subbed_science_image), 0), reference_pixels)
    # Registration, before the subpixel offset corrects it; the science stars' own width beside
    # the ePSF's.
    metrics.update(quality.registration(science_pixels, reference_pixels, stars_xy))
    sci_star_sigma = quality.star_sigma(science_pixels, stars_xy)
    if sci_star_sigma is not None:
        metrics["sci_star_sigma_px"] = sci_star_sigma
        if metrics["sci_psf_sigma_px"] > 0:
            metrics["sci_star_psf_sigma_ratio"] = sci_star_sigma / metrics["sci_psf_sigma_px"]
    del science_pixels, reference_pixels

    # Gain matching, with the reference zero point the reference was built on.
    zero_point, zero_point_source = _resolve_zero_point(settings, w(staged["reference"]))
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
    logger.info("scalefac,dxrmsfin,dyrmsfin,dxmedianfin,dymedianfin = %s %s %s %s %s",
                scalefac, dxrmsfin_measured, dyrmsfin_measured, dxmedianfin, dymedianfin)
    scalefacref = 1. / scalefac
    metrics["scalefacref"] = float(scalefacref)

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
        logger.info("Number of extreme artifact pixels repaired in science image = %s",
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

    repair.replace_nans_with_value(w(reformatted_science_uncert_image_filename), std_sci_img)
    repair.replace_nans_with_value(w(output_resampled_gainmatched_reference_uncert_image), std_ref_img)

    # Subpixel offset of the reference by the measured medians.
    offsets.apply_subpixel_orthogonal_offsets(
        w(output_resampled_gainmatched_reference_image), dxmedianfin, dymedianfin)

    if "rimtimsim" in science_image_filename:
        psf.transpose_image_data(w(filename_sciimage_psf_normalized))

    # ZOGY. dev feeds it a fixed astrometric uncertainty (0.0 by default),
    # not the measured RMS, which the manifest records.
    filename_diffimage = zogy_settings["zogy_output_diffimage_file"]
    filename_diffpsf = zogy_settings["zogy_output_diffpsf_file"]
    filename_scorrimage = zogy_settings["zogy_output_scorrimage_file"]
    zogy_astrometric_sigma = float(zogy_settings["astrometric_sigma"])
    zogy_sn, zogy_sr = zogy.zogy_background_sigmas(
        zogy_settings["zogy_sn_sr_from_uncertainty_maps"],
        w(reformatted_science_uncert_image_filename),
        w(output_resampled_gainmatched_reference_uncert_image),
        std_sci_img, std_ref_img, scalefacref)
    logger.info("zogy_sn,zogy_sr = %s %s", zogy_sn, zogy_sr)
    kit.runner.run(zogy.build_zogy_command(
        python_cmd, paths["zogy_code"],
        filename_bkg_subbed_science_image, output_resampled_gainmatched_reference_image,
        filename_sciimage_psf_normalized, REFERENCE_PSF_NAME,
        reformatted_science_uncert_image_filename,
        output_resampled_gainmatched_reference_uncert_image,
        zogy_sn, zogy_sr, zogy_astrometric_sigma, zogy_astrometric_sigma,
        filename_diffimage, filename_diffpsf, filename_scorrimage), cwd=work_dir)

    # Mask with the resampled coverage map, restore NaNs, negate.
    filename_diffimage_masked = filename_diffimage.replace(".fits", "_masked.fits")
    filename_scorrimage_masked = filename_scorrimage.replace(".fits", "_masked.fits")
    for source, target in ((filename_diffimage, filename_diffimage_masked),
                           (filename_scorrimage, filename_scorrimage_masked)):
        masking.mask_difference_image_with_resampled_reference_cov_map(
            w(source), w(output_resampled_reference_cov_map), w(target), thresh)
    for target in (filename_diffimage_masked, filename_scorrimage_masked):
        _restore_nans(w(target), nan_indices_sciimage, nan_indices_refimage)
    filename_diffimage_masked_negative = filename_diffimage_masked.replace(".fits", "_negative.fits")
    scale_image_data(w(filename_diffimage_masked), -1.0, w(filename_diffimage_masked_negative))
    filename_scorrimage_masked_negative = filename_scorrimage_masked.replace(".fits", "_negative.fits")
    scale_image_data(w(filename_scorrimage_masked), -1.0, w(filename_scorrimage_masked_negative))

    # Difference uncertainty: the catalogs' weight and error image.
    filename_diffimage_unc_masked = filename_diffimage_masked.replace("masked.fits", "uncert_masked.fits")
    uncertainty.compute_diffimage_uncertainty(
        sca_gain * inputs.exptime,
        w(filename_bkg_subbed_science_image),
        w(output_resampled_gainmatched_reference_image),
        w(output_resampled_reference_cov_map), thresh,
        w(filename_diffimage_masked), w(filename_diffimage_unc_masked), seed=seed)

    # ZOGY's catalogs detect on S_corr, as dev's do.
    psfcat_table = settings["psfcat_diffimage"]
    zogy_catalogs = _run_catalogs(
        differencer="zogy", kit=kit, work_dir=work_dir, settings=settings,
        positive={"detection": filename_scorrimage_masked, "input": filename_diffimage_masked,
                  "catalog": filename_diffimage_masked.replace(".fits", ".txt")},
        negative={"detection": filename_scorrimage_masked_negative,
                  "input": filename_diffimage_masked_negative,
                  "catalog": filename_diffimage_masked_negative.replace(".fits", ".txt")},
        weight_image=filename_diffimage_unc_masked, psf_image=filename_diffpsf,
        sky_coords_image=reformatted_science_image_filename,
        psfcat_names={"catalog": psfcat_table["output_zogy_psfcat_filename"],
                      "finder": psfcat_table["output_zogy_psfcat_finder_filename"],
                      "residual": psfcat_table["output_zogy_psfcat_residual_filename"]},
        override_weight_type_and_filter=True, write_parquet=True, overrides=None, seed=seed)
    infobits = zogy_catalogs.bits

    common = dict(kit=kit, work_dir=work_dir, settings=settings, seed=seed,
                  filename_bkg_subbed_science_image=filename_bkg_subbed_science_image,
                  gainmatched_reference=output_resampled_gainmatched_reference_image,
                  resampled_cov_map=output_resampled_reference_cov_map,
                  nan_indices=(nan_indices_sciimage, nan_indices_refimage),
                  reformatted_science_image=reformatted_science_image_filename,
                  gain_times_exptime=sca_gain * inputs.exptime, thresh=thresh)
    sfft_names, sfft_catalogs = _run_sfft(
        science_image_filename=science_image_filename,
        scigainmatch_catalog=filename_scigainmatchsexcat_catalog,
        refgainmatch_catalog=filename_refgainmatchsexcat_catalog,
        science_psf=filename_sciimage_psf_normalized, reference_psf=REFERENCE_PSF_NAME,
        **common)
    infobits |= sfft_catalogs.bits
    metrics["pixel_columns_s"] = sfft_catalogs.pixel_columns_s
    if settings["naive_diffimage"]["naive_diffimage_flag"]:
        _run_naive(**common)

    # Rows: the published differencer's image and positive PSF-fit catalog ([difference] diff_flavor).
    flavor = settings.get("diff_flavor", "zogy")
    if flavor == "sfft":
        published_image, published_unc, published_psf = (
            sfft_names["diffimage"], sfft_names["diffimage_unc"], sfft_names["diffpsf"])
        published_catalogs = sfft_catalogs
    else:
        published_image, published_unc, published_psf = (
            filename_diffimage_masked, filename_diffimage_unc_masked, filename_diffpsf)
        published_catalogs = zogy_catalogs
    correlation = noise.noise_correlation(fits.getdata(w(published_image), 0))
    if correlation is not None:
        metrics["noise_correlation"] = correlation
    with fits.open(w(published_image)) as hdul:
        header = hdul[0].header.copy()
    parquet = published_catalogs.parquet["positive"]
    if parquet is None:
        logger.warning("no %s PSF-fit catalog: the source set is empty", flavor.upper())
    catalog = pq.read_table(w(parquet)) if parquet else _EMPTY_CATALOG

    diagnostics = [p for c in (sfft_catalogs, zogy_catalogs) for p in c.parquet.values() if p]
    published = trim(work_dir, images=[
        published_image, published_unc,
        output_resampled_reference_cov_map, published_psf,
        filename_bkg_subbed_science_image,
        output_resampled_gainmatched_reference_image, REFERENCE_PSF_NAME],
        others=diagnostics)
    filename_diffimage_published = published_image + ".gz"
    centre, corners = image_centre_and_corners(WCS(header), header["NAXIS1"], header["NAXIS2"])
    footprint = {"ra0": centre["ra"], "dec0": centre["dec"]}
    for n, (ra, dec) in enumerate(corners, start=1):
        footprint[f"ra{n}"], footprint[f"dec{n}"] = ra, dec
    key = {"dataset": job.dataset, "expid": int(job.scope["expid"]),
           "sca": int(job.scope["sca"]), "fid": int(job.scope["fid"])}
    diffimage = {
        "pid": inputs.pid, **key, "rid": inputs.rid, "rfid": inputs.rfid,
        "field": tessellation_field(centre["ra"], centre["dec"]),
        "jd": inputs.mjdobs + 2400000.5, **footprint,
        "infobitssci": infobits, "infobitsref": inputs.infobits_reference,
        "filename": job.prefix + w(filename_diffimage_published).relative_to(job.path).as_posix(),
        "checksum": _md5_of_file(w(filename_diffimage_published)), "status": STATUS_GOOD,
    }
    sources, candidates = source_set(
        catalog, key, inputs.pid, inputs.mjdobs, header["NAXIS1"], header["NAXIS2"],
        max_sources=settings.get("max_sources"), xy_fit_min=settings["xy_fit_min"],
        xy_fit_max_offset=settings["xy_fit_max_offset"],
        sumrat_prefilter_threshold=_prefilter_threshold(settings) if flavor == "sfft" else None)
    reference_xy, reference_sky_rows = _reference_on_grid(reference_sky, header)
    sources, in_footprint = flag_bright_footprint(sources, reference_xy, reference_sky_rows,
                                                  zero_point)
    pq.write_table(pa.Table.from_pylist([diffimage], schema=DIFFIMAGES_SCHEMA),
                   job.path / "diffimages.parquet")
    pq.write_table(sources, job.path / "sources.parquet")
    residual_stars = _quality_metrics(
        metrics, found, thresh=thresh, candidates=candidates, sources=sources,
        catalog=catalog, in_footprint=in_footprint,
        gainmatched_reference=w(output_resampled_gainmatched_reference_image + ".gz"),
        difference=w(filename_diffimage_published),
        coverage=w(output_resampled_reference_cov_map + ".gz"), reference_xy=reference_xy)
    logger.info("difference: expid %s -> pid %s, %d of %d sources kept", key["expid"],
                inputs.pid, sources.num_rows, candidates)
    reads, catalog, association = associate(job, sources, diffimage["field"])
    metrics.update(n_objects_read=association["objects_read"],
                   n_objects_founded=association["founded"],
                   n_memberships=association["memberships"],
                   n_sources_out_of_field=association["sources_out_of_field"])
    job.write_manifest(
        [("diffimages", "diffimages.parquet"), ("sources", "sources.parquet"),
         ("objects", "objects.parquet"), ("memberships", "memberships.parquet")],
        empty_ok=("sources",),                  # a difference with nothing above the cut
        reads=reads, catalog=catalog, association=association,
        difference={
            "pid": inputs.pid, "rid": inputs.rid, "rfid": inputs.rfid,
            "zero_point": {"value": zero_point, "source": zero_point_source},
            "fwhm_arcsec": {"science": fwhm_sci, "reference": fwhm_ref},
            "scalefacref": float(scalefacref),
            "registration_residual": {"x_rms": dxrmsfin_measured, "y_rms": dyrmsfin_measured,
                                      "x_median": float(dxmedianfin),
                                      "y_median": float(dymedianfin)},
            "zogy_astrometric_sigma": zogy_astrometric_sigma,
            "differencer": flavor,
            "source_counts": {"sextractor": published_catalogs.sextractor,
                              "photutils": published_catalogs.photutils},
            "zogy_source_counts": {"sextractor": zogy_catalogs.sextractor,
                                   "photutils": zogy_catalogs.photutils},
            "sfft_source_counts": {"sextractor": sfft_catalogs.sextractor,
                                   "photutils": sfft_catalogs.photutils},
            "sources_before_cap": candidates, "sources_kept": sources.num_rows,
            "infobits_science_l2": inputs.infobits_science,
            "refpsf": refpsf_record, "residual_stars": residual_stars, "published": published},
        metrics=metrics)


def associate(job: JobContext, sources: pa.Table, image_field: int) -> tuple[list, dict, dict]:
    """Match the epoch's sources against their fields' objects of the run as they stand; write the rows.

    The plan's amendment of 2026-10-24 with the field rule (runbook part 12
    step 1): the sources ``[associate]`` accepts (``flags`` equal to
    ``source_flags`` once the informational bits are cleared, ``fluxfit /
    fluxerr >= min_snr``), wherever on the image they fall, are grouped by
    their own field; one short read of the run's objects in every such
    field and its neighbours (:data:`OBJECTS_SQL`), recorded exactly as the
    founding jobs it saw per field (the manifest's ``reads``, one
    ``objects`` input per field and job, which the publisher records in
    ``job_inputs``); then
    :func:`rapidpipe.science.crossmatch.memory.associate_epoch`, each source
    matched against its field's and the neighbours' objects and founding in
    its own field. Writes ``objects.parquet`` (founded here, each with its
    field) and ``memberships.parquet`` (this epoch's, onto existing objects
    too) under ``job.path``; returns the manifest's ``reads``, its
    ``catalog`` (what the publisher locks and reconciles under: ``fields``,
    every field that gains an object or a membership and each one's
    neighbours) and the counts, with ``sources_out_of_field``, the accepted
    sources outside ``image_field``, the image's own (the geometry `dev`'s
    rule left unfounded).
    """
    settings = job.settings["associate"]
    radius = float(settings["match_radius"])
    wanted = int(settings["source_flags"])
    accepted = [r for r in sources.select(["sid", "expid", "mjdobs", "field", "ra", "dec", "fluxfit",
                                           "fluxerr", "flags"]).to_pylist()
                if (r["flags"] & ~INFORMATIONAL_SOURCE_FLAGS) == wanted
                and r["fluxerr"] > 0 and r["fluxfit"] / r["fluxerr"] >= float(settings["min_snr"])]
    near = _with_neighbours({r["field"] for r in accepted})
    with job.connect() as conn, conn.cursor() as cur:
        cur.execute(OBJECTS_SQL, (job.run, job.dataset, job.survey, sorted(near)))
        found = cur.fetchall()
        conn.commit()
    objects = {aid: (owner, ra0, dec0) for owner, aid, _, ra0, dec0 in found}
    field_of = {aid: field for _, aid, field, _, _ in found}
    epoch = associate_epoch(objects, accepted, job=job.job, radius=radius)
    pq.write_table(pa.Table.from_pylist(
        [{**r, "dataset": job.dataset, "survey": job.survey} for r in epoch.founded],
        schema=OBJECTS_SCHEMA), job.path / "objects.parquet")
    pq.write_table(pa.Table.from_pylist(epoch.memberships, schema=MEMBERSHIPS_SCHEMA),
                   job.path / "memberships.parquet")
    field_of.update({r["aid"]: r["field"] for r in epoch.founded})
    touched = {field_of[m["aid"]] for m in epoch.memberships}
    reads = [{"product": "objects",
              "key": {"dataset": job.dataset, "survey": job.survey, "field": field},
              "source_job": owner}
             for field, owner in sorted({(field, owner) for owner, _, field, _, _ in found})]
    catalog = {"dataset": job.dataset, "survey": job.survey,
               "fields": sorted(_with_neighbours(touched)), "radius": radius}
    counts = {**epoch.counts, "fields_read": len(near),
              "sources_out_of_field": sum(r["field"] != image_field for r in accepted)}
    return reads, catalog, counts


def _with_neighbours(fields: set[int]) -> set[int]:
    """``fields`` and every field sharing an edge or a corner with one of them."""
    return set(fields).union(*(field_neighbours(f) for f in fields))


def _measure_reference_psf(kit: Toolkit, reference: Path, coverage: Path, thresh: float,
                           science_psf: Path, target: Path, mode: str = "measured",
                           ) -> tuple[dict, dict, refpsf.ReferencePsf]:
    """Measure the reference PSF into ``target``; return the manifest's record, metrics and the measurement.

    Too few stars stops the job with exit 65 and the measurement's reason.
    ``mode`` ``"science"`` writes the science PSF instead, measuring nothing.
    """
    start = time.perf_counter()
    science = fits.getdata(science_psf, 0).astype(float)
    sci = refpsf.moments(science / science.sum())
    if mode == "science":
        header = fits.Header()
        header["PSFKIND"] = ("refimage_psf", "the science PSF, [difference] reference_psf")
        header["OVERSAMP"] = (1, "detector pixels")
        fits.PrimaryHDU((science / science.sum()).astype("float32"), header).writeto(target)
        logger.info("reference PSF: the science PSF ([difference] reference_psf = science)")
        none = refpsf.ReferencePsf(stamp=science / science.sum(), stars=0, detections=0,
                                   passed_cuts=0, sigma_px=sci[2], centroid_px=(sci[0], sci[1]),
                                   star_sigma_px=float("nan"))
        return ({"mode": "science", "seconds": round(time.perf_counter() - start, 3)},
                {"sci_psf_sigma_px": sci[2]}, none)
    if mode != "measured":
        raise ValueError(f"[difference] reference_psf is {mode!r}, not 'measured' or 'science'")
    image = fits.getdata(reference, 0)
    usable = fits.getdata(coverage, 0) >= thresh
    try:
        measured = kit.reference_psf(image, usable, science)
    except refpsf.TooFewStars as exc:
        raise CommandExit(int(ExitCode.INPUT_REJECTED), str(exc)) from exc
    header = fits.Header()
    header["PSFKIND"] = ("refimage_psf", "measured on the resampled reference")
    header["OVERSAMP"] = (1, "detector pixels")
    header["NSTARS"] = (measured.stars, "stars the ePSF was built from")
    header["PSFSIG"] = (round(measured.sigma_px, 4), "second-moment sigma within 4 px, px")
    fits.PrimaryHDU(measured.stamp.astype("float32"), header).writeto(target)
    seconds = round(time.perf_counter() - start, 3)
    logger.info("reference PSF: %d stars of %d detections, sigma %.3f px (science %.3f), %.1f s",
                measured.stars, measured.detections, measured.sigma_px, sci[2], seconds)
    record = {"mode": "measured", "stars": measured.stars, "detections": measured.detections,
              "passed_cuts": measured.passed_cuts, "seconds": seconds,
              "centroid_px": list(measured.centroid_px), "science_centroid_px": [sci[0], sci[1]]}
    metrics = {"refpsf_sigma_px": measured.sigma_px, "refpsf_stars": measured.stars,
               "ref_sigma_px": measured.star_sigma_px, "sci_psf_sigma_px": sci[2],
               "psf_centroid_offset_x_px": measured.centroid_px[0] - sci[0],
               "psf_centroid_offset_y_px": measured.centroid_px[1] - sci[1]}
    if sci[2] > 0:
        metrics["ref_sci_sigma_ratio"] = measured.star_sigma_px / sci[2]
    return record, metrics, measured


def _reference_on_grid(reference_sky: list, header) -> tuple[np.ndarray, np.ndarray]:
    """The reference mosaic catalog's positions on the science grid and their rows.

    ``reference_sky`` rows are :data:`REFERENCE_CATALOG_COLUMNS`; a row that
    does not map is dropped. Returns ``(xy, rows)``, both ``(n, ...)`` floats.
    """
    if not reference_sky:
        return np.empty((0, 2)), np.empty((0, len(REFERENCE_CATALOG_COLUMNS)))
    rows = np.array(reference_sky, dtype=float)
    cx, cy = WCS(header).all_world2pix(rows[:, 0], rows[:, 1], 0)
    inside = np.isfinite(cx) & np.isfinite(cy)
    return np.column_stack([cx[inside], cy[inside]]), rows[inside]


def flag_bright_footprint(sources: pa.Table, reference_xy: np.ndarray, reference_rows: np.ndarray,
                          zero_point: float) -> tuple[pa.Table, np.ndarray]:
    """Set :data:`SOURCE_FLAG_BRIGHT_FOOTPRINT` on the sources in a bright reference source's footprint.

    The bright reference sources are the mosaic catalog's rows of aperture
    S/N above :data:`rapidpipe.science.difference.metrics.FOOTPRINT_SNR`,
    their radii :func:`~rapidpipe.science.difference.metrics.footprint_radii`
    on the reference's ``zero_point``. Nothing is dropped. Returns the table
    and the per-source mask.
    """
    flux, aperture, aperture_err = (reference_rows[:, i] for i in (2, 3, 4))
    with np.errstate(divide="ignore", invalid="ignore"):
        bright = (aperture_err > 0) & (aperture / aperture_err > quality.FOOTPRINT_SNR)
    columns = sources.to_pydict()
    inside = quality.bright_footprint(
        np.asarray(columns["xfit"], dtype=float), np.asarray(columns["yfit"], dtype=float),
        reference_xy[bright], quality.footprint_radii(flux[bright], zero_point))
    flags = np.asarray(columns["flags"]).astype(np.int64)
    flags[inside] |= SOURCE_FLAG_BRIGHT_FOOTPRINT
    index = sources.schema.get_field_index("flags")
    flagged = pa.array(flags, type=sources.schema.field(index).type)
    return sources.set_column(index, sources.schema.field(index), flagged), inside


def _catalog_stars(reference_sky: list, header, reference: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """The metrics' star rows under ``reference_psf = "science"``, from the reference mosaic's catalog.

    ``reference_sky`` rows are :data:`REFERENCE_CATALOG_COLUMNS`, mapped onto
    the science grid of ``header`` (:func:`_reference_on_grid`) and kept on
    ``reference``'s pixels. Returns ``(found, stars_xy)`` as
    :class:`~rapidpipe.science.difference.refpsf.ReferencePsf` has them:
    ``(x, y, FLUX_AUTO)`` rows, and the registration stars, the ones
    :func:`~rapidpipe.science.difference.metrics.residual_stars` chooses on
    ``reference``, the resampled reference. The refpsf finder gives a
    confusion-limited field no stars (the GBTDS overlay's comment); its
    catalog still has them. Both empty when no row maps.
    """
    xy, rows = _reference_on_grid(reference_sky, header)
    ny, nx = reference.shape
    on = (xy[:, 0] >= 0) & (xy[:, 0] < nx) & (xy[:, 1] >= 0) & (xy[:, 1] < ny)
    found = np.column_stack([xy[on], rows[on, 2]])
    stars = quality.residual_stars(found, reference.shape, reference)
    return found, np.asarray(stars, dtype=float).reshape(-1, 2)


def _quality_metrics(metrics: dict, found: np.ndarray, *, thresh: float,
                     candidates: int, sources: pa.Table, catalog: pa.Table, in_footprint: np.ndarray,
                     gainmatched_reference: Path, difference: Path, coverage: Path,
                     reference_xy: np.ndarray) -> list[dict]:
    """Add :mod:`rapidpipe.science.difference.metrics`' numbers to ``metrics``.

    Reads the job's own published files back from its work directory, all
    on the science grid; ``found`` the star rows ``(x, y, flux)`` the
    residual stars and the ring count read (the reference PSF measurement's
    finder stars, or :func:`_catalog_stars`'), ``reference_xy`` the reference
    mosaic catalog mapped onto it, ``catalog`` the uncapped PSF-fit catalog and
    ``in_footprint`` the kept sources' bright-footprint mask. Returns the
    residual stars with their positions, which the manifest records beside
    the metrics for a reader's gallery.
    """
    diff = fits.getdata(difference, 0).astype(float)
    cov = fits.getdata(coverage, 0)
    gainmatched = fits.getdata(gainmatched_reference, 0).astype(float)
    metrics["usable_fraction"] = quality.usable_fraction(diff, cov, thresh)
    metrics["n_candidates"] = int(candidates)
    stars = quality.residual_stars(found, diff.shape, gainmatched)
    residual = quality.residuals(gainmatched, diff, stars)
    metrics.update(residual)
    columns = sources.to_pydict()
    flux = np.asarray(columns["fluxfit"], dtype=float)
    flux_err = np.asarray(columns["fluxerr"], dtype=float)
    metrics.update(quality.source_counts(
        np.asarray(columns["xfit"], dtype=float), np.asarray(columns["yfit"], dtype=float),
        flux, flux_err, reference_xy if len(reference_xy) else None))
    metrics.update(quality.footprint_counts(in_footprint, flux, flux_err))
    ux, uy, uflux, uerr = (catalog.column(n).to_numpy().astype(float)
                           for n in ("x_fit", "y_fit", "flux_fit", "flux_err"))
    metrics["n_sources_near_bright_px"] = quality.near_bright_count(ux, uy, gainmatched)
    ring = quality.ring_count(ux, uy, uflux, uerr, found)
    if ring is not None:
        metrics["n_sources_ring_snr_gt20"] = ring
    return [{"x": round(x, 2), "y": round(y, 2)} for x, y in stars]


def trim(work_dir: Path, images: list[str], others: list[str]) -> dict[str, Any]:
    """Keep only ``images``, gzipped, and ``others`` in ``work_dir``; remove every other file.

    Returns what the manifest records: the kept names and the bytes and
    seconds of the gzip, beside the bytes removed.
    """
    start = time.perf_counter()
    plain = gzipped = 0
    for name in images:
        plain += (work_dir / name).stat().st_size
        gzip_file(work_dir / name, work_dir / (name + ".gz"), GZIP_LEVEL)
        (work_dir / name).unlink()
        gzipped += (work_dir / (name + ".gz")).stat().st_size
    keep = {name + ".gz" for name in images} | set(others)
    removed = nfiles = 0
    for path in sorted(work_dir.iterdir()):
        if path.name in keep:
            continue
        files = [path] if path.is_symlink() or not path.is_dir() else [
            f for f in path.rglob("*") if not f.is_dir() or f.is_symlink()]
        removed += sum(f.lstat().st_size for f in files)
        nfiles += len(files)
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    return {"files": sorted(keep), "fits_bytes": plain, "fits_gz_bytes": gzipped,
            "gzip_s": round(time.perf_counter() - start, 3), "removed_files": nfiles,
            "removed_bytes": removed}


#: A PSF-fit catalog with no rows, for an image whose catalog was not produced.
_EMPTY_CATALOG = pa.table({name: pa.array([], pa.float64()) for name in (
    "id", "ra", "dec", "x_fit", "y_fit", "flux_fit", "x_err", "y_err", "flux_err",
    "n_pixels_fit", "qfit", "cfit", "reduced_chi2", "flags", "sharpness", "roundness1",
    "roundness2", "n_pixels", "peak")})


def _md5_of_file(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _restore_nans(path: Path, nan_indices_sciimage, nan_indices_refimage) -> None:
    if nan_indices_sciimage:
        repair.restore_nans(path, nan_indices_sciimage)
    if nan_indices_refimage:
        repair.restore_nans(path, nan_indices_refimage)


# ----------------------------------------------------------------------
# SFFT and naive, as dev runs them
# ----------------------------------------------------------------------


def _run_sfft(*, kit, work_dir, settings, seed, science_image_filename,
              filename_bkg_subbed_science_image, gainmatched_reference, resampled_cov_map,
              nan_indices, scigainmatch_catalog, refgainmatch_catalog, science_psf,
              reference_psf, reformatted_science_image, gain_times_exptime,
              thresh) -> tuple[dict[str, str], _Catalogs]:
    """SFFT's command, then its masking, uncertainty and catalogs; ``(file names, catalogs)``.

    A nonzero exit raises :class:`~rapidpipe.science.difference.tools.ToolFailed`
    (`dev` logs it and goes on with ZOGY; here SFFT is the published differencer).

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
    # Checked like every tool: SFFT is the published differencer, so no fallback to ZOGY.
    kit.runner.run_shell(sfft.shell_command(s["activate_cmd"], sfft_cmd), cwd=work_dir)

    def w(name: str) -> Path:
        return work_dir / name

    diffimage = names["diffimage"]
    cconvdiff = names["cconvdiff"]
    # dev 8107a083: mask SFFT's images with the resampled coverage map and
    # restore the zeroed NaNs, as for ZOGY and naive. SFFT writes the final
    # "_masked" names, so each is renamed "_unmasked" and masked back.
    for name in [diffimage] + ([cconvdiff] if crossconv_flag else []):
        unmasked = name.replace("_masked.fits", "_unmasked.fits")
        w(name).replace(w(unmasked))
        masking.mask_difference_image_with_resampled_reference_cov_map(
            w(unmasked), w(resampled_cov_map), w(name), thresh)
        _restore_nans(w(name), *nan_indices)

    diffimage_negative = diffimage.replace(".fits", "_negative.fits")
    scale_image_data(w(diffimage), -1.0, w(diffimage_negative))
    cconvdiff_negative = cconvdiff.replace(".fits", "_negative.fits")
    if crossconv_flag:
        scale_image_data(w(cconvdiff), -1.0, w(cconvdiff_negative))

    diffimage_unc = names["diffimage_unc"]
    uncertainty.compute_diffimage_uncertainty(
        gain_times_exptime, w(filename_bkg_subbed_science_image), w(gainmatched_reference),
        w(resampled_cov_map), thresh, w(diffimage), w(diffimage_unc), seed=seed)

    # dev detects on the cross-convolved image when crossconv_flag is set,
    # and names the negative catalog after its detection image (8f8ea43f).
    detection, detection_negative = (
        (cconvdiff, cconvdiff_negative) if crossconv_flag else (diffimage, diffimage_negative))
    psfcat_table = settings["psfcat_diffimage"]
    catalogs = _run_catalogs(
        differencer="sfft", kit=kit, work_dir=work_dir, settings=settings,
        positive={"detection": detection, "input": diffimage,
                  "catalog": detection.replace(".fits", ".txt")},
        negative={"detection": detection_negative, "input": diffimage_negative,
                  "catalog": detection_negative.replace(".fits", ".txt")},
        weight_image=diffimage_unc, psf_image=names["diffpsf"],
        sky_coords_image=reformatted_science_image,
        psfcat_names={"catalog": psfcat_table["output_sfft_psfcat_filename"],
                      "finder": psfcat_table["output_sfft_psfcat_finder_filename"],
                      "residual": psfcat_table["output_sfft_psfcat_residual_filename"]},
        override_weight_type_and_filter=True, write_parquet=True,
        overrides=sextractor.detection_overrides(
            settings["sextractor_diffimage_sfft"], crossconv_flag, settings["paths"]["cfg_path"]),
        seed=seed)
    # dev e4a49c97 and c32e28f6: sumrat, nneg and nbad on every SFFT PSF-fit source, before the
    # cap, each catalog with the image its sources are positive in.
    start = time.perf_counter()
    for sign, image in (("positive", diffimage), ("negative", diffimage_negative)):
        if catalogs.parquet[sign]:
            columns.add_pixel_columns(w(catalogs.parquet[sign]), w(image), settings)
    catalogs.pixel_columns_s = round(time.perf_counter() - start, 3)
    return names, catalogs


def _run_naive(*, kit, work_dir, settings, seed, filename_bkg_subbed_science_image,
               gainmatched_reference, resampled_cov_map, nan_indices,
               reformatted_science_image, gain_times_exptime, thresh) -> None:
    """`dev`'s naive block but its Photutils PSF-fit catalogs, which are not made.

    ``reformatted_science_image`` (the PSF-fit catalogs' sky coordinates) is
    taken with the other differencers' common arguments and not read.
    """
    def w(name: str) -> Path:
        return work_dir / name

    filename_naive_diffimage = "naive_diffimage.fits"
    naive.compute_naive_difference_image(
        w(filename_bkg_subbed_science_image), w(gainmatched_reference), w(filename_naive_diffimage))

    masked = settings["naive_diffimage"]["naive_output_diffimage_file"]
    masking.mask_difference_image_with_resampled_reference_cov_map(
        w(filename_naive_diffimage), w(resampled_cov_map), w(masked), thresh)
    _restore_nans(w(masked), *nan_indices)

    masked_negative = masked.replace(".fits", "_negative.fits")
    scale_image_data(w(masked), -1.0, w(masked_negative))

    unc = masked.replace("masked.fits", "uncert_masked.fits")
    uncertainty.compute_diffimage_uncertainty(
        gain_times_exptime, w(filename_bkg_subbed_science_image), w(gainmatched_reference),
        w(resampled_cov_map), thresh, w(masked), w(unc), seed=seed)

    _sextractor_catalogs(
        kit=kit, work_dir=work_dir, settings=settings,
        positive={"detection": masked, "input": masked,
                  "catalog": masked.replace(".fits", ".txt")},
        negative={"detection": masked_negative, "input": masked_negative,
                  "catalog": masked_negative.replace(".fits", ".txt")},
        weight_image=unc, override_weight_type_and_filter=False,
        overrides=sextractor.detection_overrides(
            settings["sextractor_diffimage_naive"], False, settings["paths"]["cfg_path"]))
