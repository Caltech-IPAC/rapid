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
     "gaia": {"name": "gaia", "version": "DR3", "path": "<Gaia Parquet>"}}

Every key is optional. ``epochs`` holds at most one exposure, because a
source set's job binds one key of ``sources`` (``schema.sql``). The ids are
the source system's and are kept as they are in the id columns.

An epoch writes ``exposures``, ``l2files``, ``diffimages`` and ``sources``
and copies the L2 file and the difference job's images (difference image,
its uncertainty, its PSF, the background-subtracted science image and the
resampled gain-matched reference: the packet stage's cutout inputs). Its
L2 row's ``limmag`` is measured with the science PSF (``kind``
``science``) of the ``psfs`` set the job names as an input. ``sources`` holds the
``max_sources`` brightest rows of the difference job's PSF-fit catalog by
``flux_fit / flux_err`` (0 keeps every row), inside `dev`'s fit-position
bounds, mapped as :func:`rapidpipe.science.load.catalogs.source_set` maps
them, as the difference stage does. ``reference`` writes
``refimages`` and ``refimcatalogs`` (the mosaic SExtractor catalog, type
1); ``psf`` writes ``psfs`` (a science-image PSF, ``kind`` ``science``, or
the reference-image PSF the difference stage also reads, ``kind``
``reference``; its ``psfid`` is drawn from the table's identity, never
supplied: an explicit id ahead of the sequence makes a later drawn insert
fail on the key, and the ``psf`` stage's rows are drawn); ``gaia`` writes ``catalogs`` under the job's dataset.

The kind is declared in the scope and is part of the PSF's key. `dev`'s
PSF files carry it as ``PSFKIND`` (:data:`PSFKIND`): a file whose keyword
names the other kind fails the job before a manifest; a file with no
keyword imports under the declared kind, and the manifest records the
keyword as it was (``psf.PSFKIND``, null when absent).
"""

from __future__ import annotations

import hashlib
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
from rapidpipe.products.filters import rapid_filter_name
from rapidpipe.science.limmag import image_hdu_index, limiting_magnitude
from rapidpipe.science.load.catalogs import source_set
from rapidpipe.science.spatial import (
    healpix_indexes, image_centre_and_corners, overlapping_fields, tessellation_field)

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
    source = Path(scope["source_dir"])
    epochs = scope.get("epochs", [])
    if len(epochs) > 1:
        raise ValueError("an import job imports at most one epoch: a source set's job binds one key")
    tables: dict[str, list[dict] | pa.Table] = {}
    recorded = {"provenance": PROVENANCE}
    for epoch in epochs:
        tables.update(_epoch(job, settings, source, epoch))
    if "reference" in scope:
        tables.update(_reference(job, source, scope["reference"]))
    if "psf" in scope:
        row, keyword = _psf(job, source, scope["psf"])
        tables["psfs"] = [row]
        recorded["psf"] = {"kind": row["kind"], "PSFKIND": keyword}
    if "gaia" in scope:
        gaia = scope["gaia"]
        tables["catalogs"] = [{"dataset": job.dataset, "name": gaia["name"], "version": gaia["version"],
                               "location": _copy(job, source / gaia["path"])[0]}]
    outputs = []
    for table, rows in tables.items():
        data = rows if isinstance(rows, pa.Table) else pa.Table.from_pylist(rows)
        pq.write_table(data, job.path / f"{table}.parquet")
        outputs.append((table, f"{table}.parquet"))
    job.write_manifest(outputs, **recorded)


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


def _epoch(job: JobContext, settings: dict, source: Path, epoch: dict) -> dict:
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
    sources = _sources(settings, difference / DIFFERENCE_CATALOG, key, epoch["pid"], mjdobs,
                       diff_header["NAXIS1"], diff_header["NAXIS2"])
    return {"exposures": [{"dataset": job.dataset, "expid": epoch["expid"], "dateobs": dateobs,
                           "exptime": exptime, "program_id": None}],
            "l2files": [l2file], "diffimages": [diffimage], "sources": sources}


def _fid(filter_name: str) -> int:
    names = ("F184", "H158", "J129", "K213", "R062", "Y106", "Z087", "W146")  # schema.sql's filters
    return names.index(rapid_filter_name(filter_name)) + 1


def _sources(settings: dict, catalog_path: Path, key: dict, pid: int, mjdobs: float,
             naxis1: int, naxis2: int) -> pa.Table:
    """The capped source set: dev's position bounds, then the brightest by flux_fit / flux_err."""
    table, _ = source_set(pq.read_table(catalog_path), key, pid, mjdobs, naxis1, naxis2,
                          max_sources=settings.get("max_sources"),
                          xy_fit_min=settings["xy_fit_min"],
                          xy_fit_max_offset=settings["xy_fit_max_offset"])
    return table


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
