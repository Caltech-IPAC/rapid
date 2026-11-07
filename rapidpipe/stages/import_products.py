# ported-from: none
"""``import``: publish another system's stored products as foundation rows.

Foundation plan v5, milestone 1. The stage reads products already made by
the team's existing pipeline from a directory on disk, copies their files
under its attempt prefix and writes the rows the publisher loads. It runs
as ``rapidpipe job local --stage import``; the module is
``import_products`` because ``import`` is a keyword. Its contract is
:class:`rapidpipe.foundation.local.JobContext`: ``run(job)`` writes files
and ``manifest.json`` and returns.

The job's scope names what to import, relative to ``source_dir``::

    {"source_dir": "<directory>",
     "epochs": [{"expid": 1, "rid": 2, "pid": 3, "rfid": 4,
                 "l2": "<L2 FITS>", "difference": "<difference job directory>"}],
     "reference": {"rfid": 4, "dir": "<reference job directory>"},
     "psf": {"kind": "science", "path": "<science-image PSF>"},
     "gaia": {"version": "DR3", "prefix": "<the Gaia mirror's prefix>"},
     "ned": {"version": "36.1_20260527_v2", "prefix": "<the NED copy's prefix>"},
     "lvs": {"version": "20260424", "prefix": "<the same prefix: lvs/ is under it>"}}

Every key is optional, ``source_dir`` too when only catalogs are given.
``epochs`` holds at most one exposure, because a source set's job binds
one key of ``sources`` (``schema.sql``). The ids are the source system's
and are kept as they are in the id columns.

``gaia``, ``ned`` and ``lvs`` each register a catalog copy (rapid_docs
``decision-gaia-mirror`` and ``decision-catalog-matches``: the layouts
``python -m rapidpipe.science.catalogs.store`` writes, checked for the
copy's record, which must say complete, or ``"partial": true`` for a copy
of some leaves) as one of the dataset's ``catalogs`` rows, named by its
kind, its location the prefix's URL (``s3://``, or a directory as
``file://``), nothing copied; the manifest records each copy's release,
rows and completeness under its name. The box per dataset that preceded
the mirror is no longer a form of this scope.

An epoch writes ``exposures``, ``l2files``, ``diffimages``, ``sources``
and ``xsources`` and copies the L2 file and the difference job's images (difference image,
its uncertainty, its PSF, the background-subtracted science image and the
resampled gain-matched reference: the packet stage's cutout inputs). Its
``exposures`` row carries the L2 header's Roman observation identifiers
(:func:`rapidpipe.science.intake.exposure_identifiers`, null where `dev`'s
file predates them) and no ``program_id``. Its
L2 row's ``limmag`` is measured with the science PSF (``kind``
``science``) of the ``psfs`` set the job names as an input. ``sources`` holds,
for each of the difference job's two PSF-fit catalogs, the ``max_sources``
brightest rows by ``flux_fit / flux_err`` (-1 is no cap, keeping every row),
inside `dev`'s fit-position bounds and through the run's
``[difference.sources_prefilter]``, mapped as
:func:`rapidpipe.science.load.catalogs.source_set` maps them, as the
difference stage does: the negative catalog's rows ``isdiffpos`` false, each
``sid`` its catalog ``id`` plus the positive catalog's largest. A directory
without the negative catalog imports no negative sources. ``xsources`` holds
the rows of SFFT's two SExtractor catalogs that `dev`'s XSources loader
(``loadSECatIntoDBSourcesTable.py``) keeps, with the extra columns and rb
scores `dev` wrote into them (:mod:`rapidpipe.science.difference.xsources`)
and the run's ``[difference.database_prefilter]`` cuts; a directory with
neither catalog imports none, and one with only one is rejected (exit 65).
The manifest records the sources kept per sign (``sources_kept``) and the
xsources read and kept per sign (``xsources``, null when none were
imported). rapid_docs ``decisions.md``, import-both-signs. ``reference`` writes
``refimages`` and ``refimcatalogs`` (the mosaic SExtractor catalog, type
1); ``psf`` writes ``psfs`` (a science-image PSF, ``kind`` ``science``, or
the reference-image PSF the difference stage also reads, ``kind``
``reference``; its ``psfid`` is drawn from the table's identity, never
supplied: an explicit id ahead of the sequence makes a later drawn insert
fail on the key, and the ``psf`` stage's rows are drawn); the catalogs write ``catalogs`` under the job's
dataset, each copy's prefix named.

The kind is declared in the scope and is part of the PSF's key. `dev`'s
PSF files carry it as ``PSFKIND`` (:data:`PSFKIND`): a file whose keyword
names the other kind fails the job before a manifest; a file with no
keyword imports under the declared kind, and the manifest records the
keyword as it was (``psf.PSFKIND``, null when absent).
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from astropy.io import fits
from astropy.wcs import WCS

from rapidpipe.exitcodes import CommandExit, ExitCode
from rapidpipe.foundation.local import JobContext
from rapidpipe.foundation.prefix import get
from rapidpipe.science.catalogs.readers import CATALOG_KINDS, mirror_root
from rapidpipe.science.catalogs.store import GAIA_INFO, LVS_INFO, LVS_PARQUET, MANIFEST_NAME, Store
from rapidpipe.science.difference import xsources
from rapidpipe.products.filters import rapid_filter_name
from rapidpipe.science.intake import exposure_identifiers
from rapidpipe.science.limmag import image_hdu_index, limiting_magnitude
from rapidpipe.science.load.catalogs import source_set, sources_prefilter_threshold
from rapidpipe.science.spatial import (
    healpix_indexes, image_centre_and_corners, overlapping_fields, tessellation_field)

logger = logging.getLogger(__name__)

#: Recorded in the manifest: where the imported products came from.
PROVENANCE = "imported from IMSS socsimsdb 20260821 run; producer unknown"

#: The difference job's files the import copies, and its PSF-fit catalog: SFFT's,
#: the differencer `dev` loads since b725007d (2026-04-20) and the difference stage publishes.
DIFFERENCE_IMAGE = "sfftdiffimage_masked.fits"
DIFFERENCE_FILES = (
    DIFFERENCE_IMAGE,
    "sfftdiffimage_uncert_masked.fits",
    "sfftdiffpsf.fits",
    "bkg_subbed_science_image.fits",
    "awaicgen_output_mosaic_image_resampled_gainmatched.fits",
)
DIFFERENCE_CATALOG = "sfftdiffimage_masked_psfcat.parquet"
DIFFERENCE_CATALOG_NEGATIVE = "sfftdiffimage_masked_psfcat_negative.parquet"
#: SFFT's SExtractor catalogs by sign, `dev`'s names with crossconv_flag false, as the
#: images above are named; `dev`'s pipeline wrote the extra columns into them.
XSOURCES_CATALOGS = {"positive": "sfftdiffimage_masked.txt",
                     "negative": "sfftdiffimage_masked_negative.txt"}

#: The reference job's image, its coverage and uncertainty maps, and its
#: mosaic SExtractor catalog (`refimcatalogs` cattype 1, as dev registers it).
REFERENCE_IMAGE = "awaicgen_output_mosaic_image.fits"
REFERENCE_FILES = (
    REFERENCE_IMAGE,
    "awaicgen_output_mosaic_cov_map.fits",
    "awaicgen_output_mosaic_uncert_image.fits",
)
REFERENCE_CATALOG, REFERENCE_CATALOG_TYPE = "awaicgen_output_mosaic_refimsexcat.txt", 1

#: Rows of an imported image are good: the source system finished them.
STATUS_GOOD = 1

#: `dev`'s ``PSFKIND`` keyword value for each ``psfs.kind``.
PSFKIND = {"science": "sciimage_psf", "reference": "refimage_psf"}


def run(job: JobContext) -> None:
    settings = job.settings["import"]
    scope = job.scope
    source = Path(scope["source_dir"]) if "source_dir" in scope else None
    if source is None and (scope.get("epochs") or "reference" in scope or "psf" in scope):
        raise ValueError("the scope names files to copy but no source_dir")
    epochs = scope.get("epochs", [])
    if len(epochs) > 1:
        raise ValueError("an import job imports at most one epoch: a source set's job binds one key")
    tables: dict[str, list[dict] | pa.Table] = {}
    recorded = {"provenance": PROVENANCE}
    for epoch in epochs:
        epoch_tables, epoch_record = _epoch(job, settings, source, epoch)
        tables.update(epoch_tables)
        recorded.update(epoch_record)
    if "reference" in scope:
        tables.update(_reference(job, source, scope["reference"]))
    if "psf" in scope:
        row, keyword = _psf(job, source, scope["psf"])
        tables["psfs"] = [row]
        recorded["psf"] = {"kind": row["kind"], "PSFKIND": keyword}
    catalogs = []
    for kind in CATALOG_KINDS:
        if kind in scope:
            spec = scope[kind]
            if spec.get("name", kind) != kind:
                raise ValueError(f"a {kind} catalog is named {kind!r}, not {spec['name']!r}")
            location, recorded[kind] = _mirror(kind, spec["prefix"], partial=bool(spec.get("partial")))
            catalogs.append({"dataset": job.dataset, "name": kind, "version": spec["version"],
                             "location": location})
    if catalogs:
        tables["catalogs"] = catalogs
    outputs = []
    for table, rows in tables.items():
        data = rows if isinstance(rows, pa.Table) else pa.Table.from_pylist(rows)
        pq.write_table(data, job.path / f"{table}.parquet")
        outputs.append((table, f"{table}.parquet"))
    job.write_manifest(outputs, **recorded)


def _mirror(kind: str, prefix: str, partial: bool = False) -> tuple[str, dict]:
    """A catalog copy at ``prefix``, named as a ``catalogs`` location, and its record.

    ``prefix`` is ``s3://bucket/prefix``, a ``file://`` URL or a directory;
    the location recorded is the URL (a directory as ``file://``). Nothing
    is copied. What the copy must hold, by kind: ``gaia``,
    ``partition_info.csv`` and the ingest record ``gaia.json``; ``ned``, the
    store's ``manifest.json`` (its ``hp6`` inventory says complete); ``lvs``,
    ``lvs/nedlvs.parquet`` and ``lvs/nedlvs.json``. A record that does not
    say complete is refused unless ``partial`` says a copy of some leaves
    or pixels is meant (a developer's; a job whose cone falls off it does
    not match).
    """
    url = prefix if prefix.startswith(("s3://", "file://")) else Path(prefix).resolve().as_uri()
    store = Store(mirror_root(url))
    required, record_file = {"gaia": (("partition_info.csv", GAIA_INFO), GAIA_INFO),
                             "ned": ((MANIFEST_NAME,), MANIFEST_NAME),
                             "lvs": ((LVS_PARQUET, LVS_INFO), LVS_INFO)}[kind]
    for name in required:
        if not store.exists(name):
            raise ValueError(f"{prefix}: no {name}; not a {kind} copy "
                             "(python -m rapidpipe.science.catalogs.store)")
    info = json.loads(store.read_text(record_file))
    complete = bool(info.get("hp6", {}).get("complete") if kind == "ned" else
                    info.get("complete", kind == "lvs"))
    if not complete and not partial:
        raise ValueError(f"{prefix}: the record says the {kind} copy is not complete"
                         " (a copy of some leaves is registered with \"partial\": true)")
    if kind == "ned":
        hp6 = info.get("hp6", {})
        return url.rstrip("/") + "/", {"release": info.get("release"), "complete": complete,
                                       **{k: hp6.get(k) for k in ("leaves_done", "leaves_total", "rows")},
                                       "tool": info.get("tool")}
    keys = (("release", "ref_epoch", "rows", "leaves_done", "leaves_total", "complete", "columns", "tool")
            if kind == "gaia" else ("release", "rows", "parquet", "tool"))
    return url.rstrip("/") + "/", {**{k: info.get(k) for k in keys}, "complete": complete}


def _copy(job: JobContext, path: Path) -> tuple[str, str]:
    """Copy ``path`` under the prefix; return its URL and its MD5 (dev's checksum column)."""
    target = job.path / path.name
    shutil.copyfile(path, target)
    digest = hashlib.md5()
    with target.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return job.prefix + target.name, digest.hexdigest()


def _header(path: Path):
    with fits.open(path) as hdul:
        return hdul[image_hdu_index(hdul)].header.copy()


def _footprint(header) -> dict:
    centre, corners = image_centre_and_corners(WCS(header), header["NAXIS1"], header["NAXIS2"])
    footprint = {"ra0": centre["ra"], "dec0": centre["dec"]}
    for n, (ra, dec) in enumerate(corners, start=1):
        footprint[f"ra{n}"], footprint[f"dec{n}"] = ra, dec
    return footprint


def _rejected(reason: str) -> CommandExit:
    """A declared input the job cannot use: exit 65, input rejected, the reason on its row."""
    return CommandExit(int(ExitCode.INPUT_REJECTED), reason)


def _science_psf(job: JobContext, fid: int, sca: int) -> Path:
    """The science PSF file of the ``psfs`` set the job names as an input."""
    psf_jobs = [i["source_job"] for i in job.inputs if i["kind"] == "input" and i["product"] == "psfs"]
    if not psf_jobs:
        raise _rejected("an epoch's import names its science-image PSF as a psfs input")
    with job.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT filename FROM psfs WHERE job = ANY(%s) AND fid = %s AND sca = %s"
                    " AND kind = 'science'", (psf_jobs, fid, sca))
        row = cur.fetchone()
    if row is None:
        raise _rejected(f"no science psfs row for fid {fid} sca {sca} in jobs {psf_jobs}")
    return get(row[0])


def _epoch(job: JobContext, settings: dict, source: Path, epoch: dict) -> tuple[dict, dict]:
    l2_path = source / epoch["l2"]
    difference = source / epoch["difference"]
    header = _header(l2_path)
    sca = int(header["SCA_NUM"])
    fid = _fid(header["FILTER"])
    dateobs = datetime.fromisoformat(header["DATE-OBS"])
    mjdobs = float(header["MJD-OBS"])
    exptime = float(header["EXPTIME"])
    key = {"dataset": job.dataset, "expid": epoch["expid"], "sca": sca, "fid": fid}

    l2_url, l2_md5 = _copy(job, l2_path)
    footprint = _footprint(header)
    field = tessellation_field(footprint["ra0"], footprint["dec0"])
    hp6, hp9 = healpix_indexes(footprint["ra0"], footprint["dec0"])
    wcs_columns = {"crval1": header["CRVAL1"], "crval2": header["CRVAL2"],
                   "crpix1": header["CRPIX1"], "crpix2": header["CRPIX2"],
                   "cd11": header["CD1_1"], "cd12": header["CD1_2"],
                   "cd21": header["CD2_1"], "cd22": header["CD2_2"]}
    l2file = {
        "rid": epoch["rid"], **key, "field": field, "hp6": hp6, "hp9": hp9,
        "dateobs": dateobs, "mjdobs": mjdobs, "exptime": exptime, "infobits": 0,
        "filename": l2_url, "checksum": l2_md5, "status": STATUS_GOOD, **wcs_columns,
        "ra": header["TARGRA"], "dec": header["TARGDEC"], "zptmag": header["ZPTMAG"],
        "limmag": limiting_magnitude(
            l2_path, _science_psf(job, fid, sca), settings["limmag_nsigma"],
            settings["limmag_clip_sigma"], settings["limmag_maxiters"]),
        "overlapfields": overlapping_fields(
            *wcs_columns.values(), header["NAXIS1"], header["NAXIS2"], field=field),
        **footprint,
    }

    copies = {name: _copy(job, difference / name) for name in DIFFERENCE_FILES}
    diff_header = _header(difference / DIFFERENCE_IMAGE)
    diff_footprint = _footprint(diff_header)
    diffimage = {
        "pid": epoch["pid"], **key, "rid": epoch["rid"], "rfid": epoch["rfid"],
        "field": tessellation_field(diff_footprint["ra0"], diff_footprint["dec0"]),
        "jd": mjdobs + 2400000.5, **diff_footprint,
        "infobitssci": int(diff_header.get("INFOBITS", 0)), "infobitsref": 0,
        "filename": copies[DIFFERENCE_IMAGE][0], "checksum": copies[DIFFERENCE_IMAGE][1],
        "status": STATUS_GOOD,
    }
    naxis = (diff_header["NAXIS1"], diff_header["NAXIS2"])
    sources, kept = _sources(settings, sources_prefilter_threshold(job.settings["difference"]),
                             difference, key, epoch["pid"], mjdobs, *naxis)
    xsources_rows, xsources_record = _xsources(job.settings["difference"]["database_prefilter"],
                                               difference, key, epoch["pid"], mjdobs, *naxis)
    tables = {"exposures": [{"dataset": job.dataset, "expid": epoch["expid"], "dateobs": dateobs,
                             "exptime": exptime, "program_id": None, **exposure_identifiers(header)}],
              "l2files": [l2file], "diffimages": [diffimage], "sources": sources}
    if xsources_rows is not None:
        tables["xsources"] = xsources_rows
    return tables, {"sources_kept": kept, "xsources": xsources_record}


def _fid(filter_name: str) -> int:
    names = ("F184", "H158", "J129", "K213", "R062", "Y106", "Z087", "W146")  # schema.sql's filters
    return names.index(rapid_filter_name(filter_name)) + 1


def _sources(settings: dict, sumrat_threshold: float | None, difference: Path, key: dict, pid: int,
             mjdobs: float, naxis1: int, naxis2: int) -> tuple[pa.Table, dict]:
    """Both PSF-fit catalogs' capped source sets: dev's position bounds and prefilter, then the brightest by flux_fit / flux_err.

    Parameters
    ----------
    settings : dict
        The ``[import]`` table: ``max_sources``, ``xy_fit_min`` and ``xy_fit_max_offset``.
    sumrat_threshold : float or None
        The run's sources prefilter
        (:func:`rapidpipe.science.load.catalogs.sources_prefilter_threshold`),
        None for none.
    difference : pathlib.Path
        The difference job's directory, holding :data:`DIFFERENCE_CATALOG`
        and, when `dev` made one, :data:`DIFFERENCE_CATALOG_NEGATIVE`.
    key : dict
        The set's ``dataset``, ``expid``, ``sca`` and ``fid``.
    pid : int
        The difference-image row.
    mjdobs : float
        The exposure's MJD-OBS.
    naxis1, naxis2 : int
        The difference image's size [pixels].

    Returns
    -------
    tuple
        The rows, positive first, as one table; and the rows kept per sign,
        ``negative`` null when the directory has no negative catalog.
    """
    options = dict(max_sources=settings.get("max_sources"), xy_fit_min=settings["xy_fit_min"],
                   xy_fit_max_offset=settings["xy_fit_max_offset"],
                   sumrat_prefilter_threshold=sumrat_threshold)
    catalog = pq.read_table(difference / DIFFERENCE_CATALOG)
    sources, _ = source_set(catalog, key, pid, mjdobs, naxis1, naxis2, **options)
    negative_path = difference / DIFFERENCE_CATALOG_NEGATIVE
    if not negative_path.exists():
        logger.warning("no %s: the set has no negative sources", DIFFERENCE_CATALOG_NEGATIVE)
        return sources, {"positive": sources.num_rows, "negative": None}
    # A negative source's sid follows the positive catalog's ids, as the difference stage numbers it.
    offset = int(max(catalog.column("id").to_pylist(), default=0))
    negative, _ = source_set(pq.read_table(negative_path), key, pid, mjdobs, naxis1, naxis2,
                             isdiffpos=False, sid_offset=offset, **options)
    # An empty catalog's columns may be typed otherwise: take the other sign's types.
    schema = negative.schema if negative.num_rows and not sources.num_rows else sources.schema
    return (pa.concat_tables([sources.cast(schema), negative.cast(schema)]),
            {"positive": sources.num_rows, "negative": negative.num_rows})


def _xsources(prefilter: dict, difference: Path, key: dict, pid: int, mjdobs: float,
              naxis1: int, naxis2: int) -> tuple[pa.Table | None, dict | None]:
    """dev's XSources loader on SFFT's two SExtractor catalogs, their extra columns as dev wrote them.

    Parameters
    ----------
    prefilter : dict
        The run's ``[difference.database_prefilter]`` table.
    difference : pathlib.Path
        The difference job's directory, holding :data:`XSOURCES_CATALOGS`.
    key : dict
        The set's ``dataset``, ``expid``, ``sca`` and ``fid``.
    pid : int
        The difference-image row.
    mjdobs : float
        The exposure's MJD-OBS.
    naxis1, naxis2 : int
        The difference image's size [pixels].

    Returns
    -------
    tuple
        The ``xsources`` rows, positive first, and the rows read and kept per
        sign; both None when the directory holds neither catalog.

    Raises
    ------
    CommandExit
        Exit 65 when the directory holds one of the two catalogs only.
    """
    paths = {sign: difference / name for sign, name in XSOURCES_CATALOGS.items()}
    present = [sign for sign, path in paths.items() if path.exists()]
    if not present:
        logger.warning("no SExtractor catalogs in %s: no xsources imported", difference)
        return None, None
    if len(present) == 1:
        missing = paths["negative" if present == ["positive"] else "positive"]
        raise _rejected(f"{missing.name} is missing beside {paths[present[0]].name}")
    thresholds = {"sumrat": xsources.threshold(prefilter, "sumrat_prefilter_threshold"),
                  "rb": xsources.threshold(prefilter, "rb_prefilter_threshold"),
                  "snr": xsources.threshold(prefilter, "snr_prefilter_threshold")}
    parts, record = [], {"read": {}, "kept": {}}
    for sign, path in paths.items():
        catalog = xsources.read_catalog(path)
        rows, read = xsources.xsource_rows(
            catalog, xsources.catalog_extra_columns(catalog), isdiffpos=sign == "positive",
            key=key, pid=pid, mjdobs=mjdobs, naxis1=naxis1, naxis2=naxis2, thresholds=thresholds)
        parts.append(rows)
        record["read"][sign], record["kept"][sign] = read, len(rows["num"])
    return xsources.xsources_table(parts), record


def _reference(job: JobContext, source: Path, reference: dict) -> dict:
    directory = source / reference["dir"]
    copies = {name: _copy(job, directory / name) for name in REFERENCE_FILES}
    header = _header(directory / REFERENCE_IMAGE)
    catalog_url, catalog_md5 = _copy(job, directory / REFERENCE_CATALOG)
    refimage = {
        "rfid": reference["rfid"], "dataset": job.dataset, "field": int(header["FIELD"]),
        "fid": int(header["FID"]), "filename": copies[REFERENCE_IMAGE][0],
        "checksum": copies[REFERENCE_IMAGE][1], "status": STATUS_GOOD, "infobits": 0,
        "nframes": header.get("NFRAMES"), "cov5percent": header.get("COV5PERC"),
    }
    return {"refimages": [refimage],
            "refimcatalogs": [{"rfid": reference["rfid"], "cattype": REFERENCE_CATALOG_TYPE,
                               "filename": catalog_url, "checksum": catalog_md5}]}


def _psf(job: JobContext, source: Path, psf: dict) -> tuple[dict, str | None]:
    """The ``psfs`` row of the declared kind, and the file's ``PSFKIND`` as it was."""
    kind = psf["kind"]
    path = source / psf["path"]
    with fits.open(path) as hdul:
        header = hdul[0].header
        sca, fid = int(header["SCA"]), _fid(header["FILTER"])
        keyword = header.get("PSFKIND")
    if keyword is not None and keyword != PSFKIND.get(kind):
        raise ValueError(f"{path.name}: PSFKIND {keyword!r} is not a {kind} PSF ({PSFKIND.get(kind)!r})")
    url, md5 = _copy(job, path)
    return {"dataset": job.dataset, "fid": fid, "sca": sca, "kind": kind,
            "filename": url, "checksum": md5, "status": STATUS_GOOD}, keyword
