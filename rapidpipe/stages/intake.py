# ported-from: pipeline/ingestL2Files.py @ 2350a884
"""``intake``: one SOC L2 ASDF file in, a published ``l2files`` row and its FITS out.

The first stage of a dataset whose L2 inputs are Roman WFI ASDF files (the
SOC simulations; later MAST's deliveries). Its contract is
:class:`rapidpipe.foundation.local.JobContext`: ``run(job)`` writes files
and ``manifest.json`` and returns. The job's scope names one file by its
stem under the run's ``[intake] source_root``::

    {"dataset": "r00342", "file": "r0034201001001004001_0001_wfi08_f146"}

The stage fetches ``<source_root><file>_cal.asdf`` (anonymously when
``unsigned``), converts it with `dev`'s ``build_fits_file``
(:mod:`rapidpipe.science.intake`), gzips it at ``gzip_level``, measures the
limiting magnitude with the science PSF of the ``psfs`` set the job names
as an input (refusing the file, before a manifest, when the PSF names an
ePSF reference, ``EPSFREF``, other than the file's own
``meta.ref_file.epsf``), and declares one ``l2files`` row with the centre, corners,
tessellation field, overlapping fields and HEALPix indexes the import
stage derives the same way, and one ``exposures`` row only when the job
expects the ``exposures`` key (the loop gives that expectation to one
SCA of an exposure; a row nothing binds is not written). The row's
``program_id`` is ``[intake] program_id``, `dev`'s ``RAPIDDATASOURCE`` rule
(a simulation's fixed negative id, a flight program's own number), and its
Roman observation identifiers (``execution_plan``, ``pass``, ``segment``,
``observation``, ``visit``, ``exposure``, ``visit_id``, ``observation_id``)
are the converted header's (:func:`rapidpipe.science.intake.exposure_identifiers`,
`dev`'s e212d40d and e05e29a1), null for a reader whose header lacks them. The calibrated file
carries the gWCS and the whole ``meta`` tree, so its ``_wcs.asdf`` sidecar
is not read.

The key comes from the file name, ``r<program:5><plan:2><pass:3><segment:3>
<observation:3><visit:3>_<exposure:4>_wfi<sca:2>_<filter>``, and must agree
with the file's own ``meta``. The foundation's ``expid`` is the producer's
id; a SOC exposure has none that fits an ``integer``, so the stage packs
the identifiers that vary, ``pass * 10**6 + observation * 10**4 + visit *
10**2 + exposure`` (observation, visit and exposure each below 100), the
same for every SCA of the exposure as `dev`'s one ``Exposures`` row per
exposure is. The manifest records the file's provenance: its observation
identifiers, romancal's version and the Gaia fit's summary.

``[intake] reader`` names the input format. ``soc-l2`` (the default,
`dev`'s) is the above. ``openuniverse`` reads OpenUniverse 2024's
``<source_root><filter>/<pointing>/<file>_simple.asdf``
(:mod:`rapidpipe.science.intake_openuniverse`): the stem is
``roman_<survey>_<filter>_<pointing>_wfi<sca>``, the file's own filter and
detector must agree with it, and the ``expid`` is the pointing, the index
into the survey's observing sequence (one exposure per pointing).
``rimtimsim`` reads RimTimSim's ``<source_root><stem>.fits.gz``, `dev`'s
converted ``lite`` files (:mod:`rapidpipe.science.intake_rimtimsim`): the
stem is ``rimtimsim_WFI_<filter>_SCA<NN>_<exposure>_lite``, the file's own
filter and SCA must agree with it, and the ``expid`` is the exposure
number. ``socsims-lite`` reads `dev`'s socsims lite files, the SOC
simulation as `dev`'s converter wrote it (fake sources and `dev`'s
TAN-SIP WCS), ``<source_root><stem>_cal_lite.fits.gz`` with the SOC stem,
by the same module: the key and ``expid`` are the SOC reader's, the
observation identifiers the stem's.

``[intake] dq_mask_bits`` is commissioning #238's DQ mask (0, `dev`'s
behaviour, masks nothing): the conversion sets ``SCI``, ``ERR`` and
``VAR_*`` NaN where ``DQ`` has any of the bits. The manifest's ``dq``
records the bits and three pixel counts of the science image: the file's
own NaN (``nan_input``), the masked pixels (``masked``) and the NaN written
(``nan_output``, their union); ``metrics.masked_fraction`` is ``masked``
over the image's pixels.

``[intake] crpix_shift`` is a translation of the WCS, ``[dx, dy]`` pixels
added to ``CRPIX1`` and ``CRPIX2`` of every HDU that carries them, before
anything reads the header (``[0, 0]``, `dev`'s behaviour, moves nothing): a
dataset whose WCS sits off its own stars by a constant measured offset
carries the correction on its overlay. When the job names the dataset's
Gaia ``catalogs`` product as an input (the loop does once one is official),
``metrics`` records ``gaia_dx_px``, ``gaia_dy_px`` and ``n_gaia_stars``,
the median offset of the frame's stars from the copy through the shifted
WCS, measured minus predicted (:mod:`rapidpipe.science.astrometry`).
``[intake] gaia_proper_motion`` (false: the copy's positions as they are)
first carries each star by its proper motion from the copy's ``ref_epoch``
to the frame's mid-exposure, for a sky that moves (flight).

``[intake] rederive_wcs`` (false, `dev`'s behaviour: the file's gWCS) is
the GBTDS WCS fix: the stage fetches the distortion reference the file
names (``meta.ref_file.distortion``) by name from ``[psf] crds_server``
and the conversion re-derives the gWCS from it before the SIP fit
(:func:`rapidpipe.science.intake.rederive_wcs`, romancal's
``AssignWcsStep`` body). The file's own reference, not CRDS's current
one: r00340 names ``roman_wfi_distortion_0003.asdf``, and the current
context's ``0076`` puts the frame about 1,770 px away. The manifest's
``wcs`` records the reference and the shift the fix made at the image
centre (``metrics.wcs_shift_dx_px`` and ``_dy_px``).
"""

from __future__ import annotations

import hashlib
import logging
import re
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import asdf
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from astropy.io import fits
from astropy.wcs import WCS

from rapidpipe.foundation.local import JobContext
from rapidpipe.foundation.prefix import get
from rapidpipe.science import intake_openuniverse, intake_rimtimsim
from rapidpipe.science.alerts.crossmatch import epoch_offset_years, propagate_positions
from rapidpipe.science.astrometry import gaia_offset
from rapidpipe.science.intake import asdf_to_fits, exposure_identifiers, gzip_file
from rapidpipe.science.limmag import limiting_magnitude
from rapidpipe.science.spatial import (
    healpix_indexes, image_centre_and_corners, overlapping_fields, tessellation_field)

log = logging.getLogger(__name__)

#: A SOC L2 file stem: program, plan, pass, segment, observation, visit, exposure, SCA, filter.
STEM = re.compile(r"r(?P<program>\d{5})(?P<execution_plan>\d{2})(?P<pass>\d{3})(?P<segment>\d{3})"
                  r"(?P<observation>\d{3})(?P<visit>\d{3})_(?P<exposure>\d{4})_wfi(?P<sca>\d{2})"
                  r"_(?P<filter>f\d{3})")

#: The filters table's fids by RAPID filter name (schema.sql).
FIDS = {"F184": 1, "H158": 2, "J129": 3, "K213": 4, "R062": 5, "Y106": 6, "Z087": 7, "W146": 8}

#: Rows of a converted file are good: the conversion finished.
STATUS_GOOD = 1

#: The ``meta.wcs_fit_results`` entries the manifest keeps.
WCS_FIT_SUMMARY = ("status", "fitgeom", "nmatches", "rmse", "mae", "proper_rot")


def parse_stem(stem: str) -> dict[str, int | str]:
    """The identifiers a SOC L2 file name carries; ``ValueError`` for another name."""
    match = STEM.fullmatch(stem)
    if match is None:
        raise ValueError(f"{stem!r} is not a SOC L2 file stem")
    fields = {k: int(v) for k, v in match.groupdict().items() if k != "filter"}
    return {**fields, "filter": match["filter"].upper()}


def expid(ids: dict) -> int:
    """The exposure's integer id: pass, observation, visit and exposure packed in decimal digits."""
    for name in ("observation", "visit", "exposure"):
        if not 0 <= ids[name] < 100:
            raise ValueError(f"{name} {ids[name]} does not fit the expid packing (below 100)")
    return ids["pass"] * 10**6 + ids["observation"] * 10**4 + ids["visit"] * 10**2 + ids["exposure"]


def run(job: JobContext) -> None:
    settings = job.settings["intake"]
    stem = job.scope["file"]
    if job.scope["dataset"] != job.dataset:
        raise ValueError(f"scope dataset {job.scope['dataset']!r} is not the run's {job.dataset!r}")
    reader = settings["reader"]
    if reader not in READERS:
        raise ValueError(f"[intake] reader {reader!r} is not one of {sorted(READERS)}")
    ids = READERS[reader]["parse_stem"](stem)
    timings: dict[str, float] = {}
    mark = time.monotonic()

    def lap(step: str) -> None:
        nonlocal mark
        now = time.monotonic()
        timings[step] = round(now - mark, 3)
        mark = now

    source = settings["source_root"].rstrip("/") + "/" + READERS[reader]["source_key"](stem)
    asdf_path = get(source, unsigned=bool(settings["unsigned"]))
    lap("fetch")

    fits_name = f"{stem}_cal.fits.gz"
    with tempfile.TemporaryDirectory(prefix="rapidpipe-intake-") as scratch:
        fits_path = Path(scratch) / f"{stem}_cal.fits"
        distortion = None
        if settings["rederive_wcs"]:
            distortion = fetch_reference(job.settings["psf"]["crds_server"],
                                         ref_file(asdf_path, "distortion"), Path(scratch))
            lap("distortion")
        conversion = READERS[reader]["convert"](
            asdf_path, fits_path, distortion=distortion, sip_degree=int(settings["sip_degree"]),
            sip_max_pix_error=float(settings["sip_max_pix_error"]),
            nominal_zptmag=settings["nominal_zptmag"], swname="rapidpipe intake",
            swvers=f"job {job.job}",  # the job names its run, the run its revision
            created=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            dq_mask_bits=int(settings["dq_mask_bits"]))
        lap("convert")
        shift = [float(v) for v in settings["crpix_shift"]]
        if any(shift):
            _shift_crpix(fits_path, shift)
        gzip_file(fits_path, job.path / fits_name, int(settings["gzip_level"]))
        lap("gzip")
        with fits.open(fits_path) as hdul:
            primary, header = hdul[0].header.copy(), hdul[1].header.copy()
            n_pixels = int(hdul[1].data.size)
            nan_output = int(np.count_nonzero(np.isnan(hdul[1].data)))
            gaia = _gaia_metrics(job, hdul[1].data, WCS(header), header)
        if reader == "soc-l2":
            _check_key(stem, ids, header)  # the openuniverse reader checks its file's own in the conversion
        fid = FIDS[header["FILTER"]]
        psf = _science_psf(job, fid, ids["sca"])
        _check_epsf(stem, primary, psf)
        limmag = limiting_magnitude(fits_path, psf,
                                    settings["limmag_nsigma"], settings["limmag_clip_sigma"],
                                    settings["limmag_maxiters"])
        lap("limmag" if gaia is None else "limmag_gaia")
        fits_bytes = fits_path.stat().st_size

    key = {"dataset": job.dataset, "expid": READERS[reader]["expid"](ids), "sca": ids["sca"], "fid": fid}
    l2file = _l2file(job, header, key, fits_name, limmag)
    dateobs = datetime.fromisoformat(header["DATE-OBS"])
    exposure = {"dataset": job.dataset, "expid": key["expid"], "dateobs": dateobs,
                "exptime": float(header["EXPTIME"]), "program_id": int(settings["program_id"]),
                **exposure_identifiers(header)}
    tables = [("l2files", [l2file])]
    if any(i["kind"] == "expects" and i["product"] == "exposures" for i in job.inputs):
        tables.insert(0, ("exposures", [exposure]))
    outputs = []
    for table, rows in tables:
        pq.write_table(pa.Table.from_pylist(rows), job.path / f"{table}.parquet")
        outputs.append((table, f"{table}.parquet"))
    lap("rows")
    fit = primary_fit(primary)
    masked = conversion["dq_masked"] or 0
    dq = {"mask_bits": hex(int(settings["dq_mask_bits"])), "masked": masked,
          "nan_input": conversion["sci_nan_input"], "nan_output": nan_output}
    metrics = {"masked_fraction": masked / n_pixels}
    wcs = {"rederived": distortion is not None}
    if distortion is not None:
        dx, dy = conversion["wcs_shift_px"]
        wcs |= {"distortion": distortion.name, "shift_px": [round(dx, 4), round(dy, 4)]}
        metrics |= {"wcs_shift_dx_px": dx, "wcs_shift_dy_px": dy}
    job.write_manifest(
        outputs, source=source,
        provenance={**ids,
                    "romancal": primary.get("HIERARCH meta calibration_software_version"),
                    "wcs_fit_results": fit},
        dq=dq, wcs=wcs, metrics={**metrics, **(gaia or {})},
        crpix_shift=shift,
        conversion={"hdus": conversion["hdus"], "asdf_bytes": asdf_path.stat().st_size,
                    "fits_bytes": fits_bytes, "fits_gz_bytes": (job.path / fits_name).stat().st_size,
                    "timings_s": timings})
    log.info("intake %s: %s", stem, timings)


#: Each ``[intake] reader``: its stem parser, the file's path under the source root, its
#: conversion and the exposure's integer id.
READERS = {
    "soc-l2": {"parse_stem": parse_stem, "source_key": lambda stem: stem + "_cal.asdf",
               "convert": asdf_to_fits, "expid": expid},
    "openuniverse": {"parse_stem": intake_openuniverse.parse_stem,
                     "source_key": intake_openuniverse.source_key,
                     "convert": intake_openuniverse.simple_model_to_fits,
                     "expid": lambda ids: ids["pointing"]},
    "rimtimsim": {"parse_stem": intake_rimtimsim.parse_stem, "source_key": intake_rimtimsim.source_key,
                  "convert": intake_rimtimsim.lite_to_fits, "expid": lambda ids: ids["exposure"]},
    "socsims-lite": {"parse_stem": parse_stem, "source_key": intake_rimtimsim.source_key,
                     "convert": intake_rimtimsim.lite_to_fits, "expid": expid},
}


def ref_file(path: Path, reftype: str) -> str:
    """The CRDS reference of ``reftype`` the L2 file names, without the ``crds://`` scheme."""
    with asdf.open(path, lazy_load=True, memmap=False) as af:
        return PurePosixPath(str(af["roman"]["meta"]["ref_file"][reftype])).name


def fetch_reference(server: str, name: str, directory: Path) -> Path:
    """The CRDS reference file ``name``, fetched by name from ``server`` into ``directory``."""
    target = directory / name
    url = server.rstrip("/") + "/unchecked_get/references/roman/" + name
    with urllib.request.urlopen(url, timeout=60) as reply, target.open("wb") as stream:
        stream.write(reply.read())
    return target


def primary_fit(primary) -> dict:
    """The Gaia fit's summary, from the primary header's ``meta wcs_fit_results`` cards."""
    return {name: primary.get(f"HIERARCH meta wcs_fit_results {name}") for name in WCS_FIT_SUMMARY}


def _check_key(stem: str, ids: dict, header) -> None:
    """The file's own identifiers must be the ones its name gives."""
    own = {"program": header["PROGRAM"], "execution_plan": header["EXECPLAN"], "pass": header["PASS"],
           "segment": header["SEGMENT"], "observation": header["OBSERVTN"], "visit": header["VISIT"],
           "exposure": header["EXPOSURE"], "sca": header["SCA_NUM"]}
    named = {k: ids[k] for k in own}
    if own != named:
        raise ValueError(f"{stem}: the file's own identifiers {own} are not its name's {named}")


def _check_epsf(stem: str, primary, psf: Path) -> None:
    """The file's own CRDS ePSF reference must be the one its science PSF names (``EPSFREF``).

    The ``psf`` stage writes the name; a PSF file without it (an import
    whose source never named one) is accepted.
    """
    named = fits.getheader(psf, 0).get("EPSFREF")
    own = primary.get("HIERARCH meta ref_file epsf")
    if named is not None and own is not None and PurePosixPath(str(own)).name != named:
        raise ValueError(f"{stem}: the file names the ePSF reference {own}; its science PSF is "
                         f"from {named}")


def _shift_crpix(path: Path, shift: list[float]) -> None:
    """Add ``shift`` to ``CRPIX1`` and ``CRPIX2`` of every HDU of the FITS file that has them."""
    with fits.open(path, mode="update") as hdul:
        for hdu in hdul:
            for axis, delta in enumerate(shift, start=1):
                if f"CRPIX{axis}" in hdu.header:
                    hdu.header[f"CRPIX{axis}"] += delta


def _gaia_metrics(job: JobContext, image, wcs, header) -> dict | None:
    """The Gaia offset metrics, when the job names a ``catalogs`` input; else ``None``.

    Under ``[intake] gaia_proper_motion`` each star is first carried from
    the copy's ``ref_epoch`` to the frame's mid-exposure by its proper
    motion (the packet stage's propagation).
    """
    named = [i for i in job.inputs if i["kind"] == "input" and i["product"] == "catalogs"]
    if not named:
        return None
    key = named[0]["key"]
    with job.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT location FROM catalogs WHERE job = %s AND dataset = %s AND name = %s"
                    " AND version = %s", (named[0]["source_job"], key["dataset"], key["name"],
                                          key["version"]))
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"no catalogs row for {key} in job {named[0]['source_job']}")
    if not job.settings["intake"]["gaia_proper_motion"]:
        gaia = pq.read_table(get(row[0]), columns=["ra", "dec"])
        return gaia_offset(np.asarray(image, float), wcs, gaia["ra"].to_numpy(), gaia["dec"].to_numpy())
    gaia = pq.read_table(get(row[0]), columns=["ra", "dec", "pmra", "pmdec", "ref_epoch"])
    mjd = float(header["MJD-OBS"]) + float(header["EXPTIME"]) / 2 / 86400.0
    dt = epoch_offset_years(mjd, gaia["ref_epoch"].to_numpy(zero_copy_only=False))
    ra, dec = propagate_positions(*(gaia[c].to_numpy(zero_copy_only=False)
                                    for c in ("ra", "dec", "pmra", "pmdec")), dt)
    return gaia_offset(np.asarray(image, float), wcs, ra, dec)


def _science_psf(job: JobContext, fid: int, sca: int) -> Path:
    """The science PSF file of the ``psfs`` set the job names as an input."""
    psf_jobs = [i["source_job"] for i in job.inputs if i["kind"] == "input" and i["product"] == "psfs"]
    if not psf_jobs:
        raise ValueError("an intake job names its science-image PSF as a psfs input")
    with job.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT filename FROM psfs WHERE job = ANY(%s) AND fid = %s AND sca = %s"
                    " AND kind = 'science'", (psf_jobs, fid, sca))
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"no science psfs row for fid {fid} sca {sca} in jobs {psf_jobs}")
    return get(row[0])


def _l2file(job: JobContext, header, key: dict, fits_name: str, limmag: float) -> dict:
    digest = hashlib.md5()  # dev's checksum column: the MD5 of the gzipped file
    with (job.path / fits_name).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    naxis1, naxis2 = header["NAXIS1"], header["NAXIS2"]
    centre, corners = image_centre_and_corners(WCS(header), naxis1, naxis2)
    footprint = {"ra0": centre["ra"], "dec0": centre["dec"]}
    for n, (ra, dec) in enumerate(corners, start=1):
        footprint[f"ra{n}"], footprint[f"dec{n}"] = ra, dec
    field = tessellation_field(footprint["ra0"], footprint["dec0"])
    hp6, hp9 = healpix_indexes(footprint["ra0"], footprint["dec0"])
    wcs_columns = {"crval1": header["CRVAL1"], "crval2": header["CRVAL2"],
                   "crpix1": header["CRPIX1"], "crpix2": header["CRPIX2"],
                   "cd11": header["CD1_1"], "cd12": header["CD1_2"],
                   "cd21": header["CD2_1"], "cd22": header["CD2_2"]}
    return {
        **key, "field": field, "hp6": hp6, "hp9": hp9,
        "dateobs": datetime.fromisoformat(header["DATE-OBS"]), "mjdobs": float(header["MJD-OBS"]),
        "exptime": float(header["EXPTIME"]), "infobits": 0, "filename": job.prefix + fits_name,
        "checksum": digest.hexdigest(), "status": STATUS_GOOD, **wcs_columns,
        "ra": header["TARGRA"], "dec": header["TARGDEC"], "zptmag": header["ZPTMAG"], "limmag": limmag,
        "overlapfields": overlapping_fields(*wcs_columns.values(), naxis1, naxis2, field=field),
        **footprint,
    }
