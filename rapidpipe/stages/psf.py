# ported-from: none
"""``psf``: a filter and SCA's science PSF from the CRDS ePSF reference its L2 file names.

New science, not `dev`'s (dev has no code that writes a PSF file from an
ePSF; its F146 files were compiled offline). Its contract is
:class:`rapidpipe.foundation.local.JobContext`. The job's scope names one
SOC L2 file of the (filter, SCA) it serves, any one, by its stem under
the run's ``[intake] source_root``::

    {"dataset": "r00342", "file": "r0034201001001004001_0001_wfi08_f146"}

The stage reads that ``_cal.asdf`` (anonymously when ``[intake]
unsigned``), takes the basename of its ``meta.ref_file.epsf`` and fetches
that reference **by name** from ``[psf] crds_server``
(``<server>/unchecked_get/references/roman/<name>``): the name is the
identity, with no rmap match and no CRDS context. With ``[psf]
epsf_cache`` set (a prefix holding copies by the same names,
``scripts/copy_epsf.py``), the stage reads ``<epsf_cache>/<name>`` first and
CRDS only for a name the cache lacks; the manifest's ``psf.fetched_from``
says which. The default, empty, is CRDS alone. The reference's
``meta.instrument`` must name the file's detector and filter.

The reduction, fixed: the reference's ``psf[focus 0, spectral type G2V]``
grid as a photutils ``GriddedPSFModel`` (as ``romanisim.psf`` builds it),
evaluated at the detector centre :data:`POSITION` on ``oversample x size``
points per axis (``oversample`` the reference's own ``meta.oversample``,
``size`` the one setting, ``[psf] size``), centred on the stamp's centre,
each ``oversample x oversample`` block summed to one detector pixel and the
stamp normalised to unit sum. The reference stores flux per oversampled
sample (its samples sum to about 1), so the block sum is the pixel's flux.
The stamp is ``size x size`` at ``OVERSAMP = 1``, its centre on the central
pixel for odd ``size`` and on the central corner for even.

One FITS file and one ``psfs`` row: the science-image PSF
(``sciimage_psf_<filter>_sca<NN>.fits``, ``PSFKIND = sciimage_psf``, kind
``science``). The reference-image PSF is not this stage's: the difference
job measures it from the resampled reference it makes, because the PSF
ZOGY needs is the one after the per-epoch resample (run 17: 15% wider than
this stamp, which until 2026-10-24 was also written as the ``reference``
kind; ``rapidpipe.science.difference.refpsf``). The header and the manifest
carry the reference name (``EPSFREF``, the keyword the copied F146 files carry), the file's
CRDS context, the position, the oversampling and the block, so the intake
can compare a file's own ePSF with its bound PSF's.

With ``[intake] reader = "openuniverse"`` the job's stem is an OpenUniverse
file's (``roman_tds_H158_1012_wfi04``), which names no CRDS reference: the
simulation drew its stars with galsim.roman's PSF, so the stage draws that
(:func:`galsim_stamp`, as the stamp run 64's intake imported was made,
runbook part 11 step 2): ``galsim.roman.getPSF`` for the stem's filter and
SCA at :data:`GALSIM_POSITION`, achromatic at the bandpass's effective
wavelength, drawn ``size x size`` at the detector's pixel scale with the
pixel response (method ``auto``), unit sum. The file is not read. The
header carries ``PSFSRC`` (the galsim version) and ``PSFWAVE``; the manifest
the same under ``psf``. The SOC path is unchanged.

With ``[intake] reader = "rimtimsim"`` the file (`dev`'s converted lite
FITS) names no reference either; ``[psf.epsf]`` names one per filter, the
CRDS reference for its detector and filter (the overlay's, with the
context that chose it), and the stage reduces it as for a SOC file, the
file unread and ``CRDSCTX`` empty.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import tempfile
import time
import urllib.request
from pathlib import Path, PurePosixPath

import asdf
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from astropy.io import fits
from astropy.nddata import NDData

from rapidpipe.foundation.local import JobContext
from rapidpipe.foundation.prefix import get
from rapidpipe.products.filters import RAPID_TO_ROMAN_FILTER_NAMES, rapid_filter_name
from rapidpipe.science import intake_rimtimsim
from rapidpipe.science.intake_openuniverse import parse_stem

log = logging.getLogger(__name__)

#: The filters table's fids by RAPID filter name (schema.sql).
FIDS = {"F184": 1, "H158": 2, "J129": 3, "K213": 4, "R062": 5, "Y106": 6, "Z087": 7, "W146": 8}

#: Where on the detector the stamp is evaluated: the SCA's centre, (x, y), 0-based pixels.
POSITION = (2048.0, 2048.0)
#: Where galsim.roman evaluates an OpenUniverse SCA's PSF: its centre in GalSim's one-based pixels, as
#: the stamp run 64's intake imported was drawn.
GALSIM_POSITION = (2044.0, 2044.0)
#: The reference's in-focus plane and the spectral type taken, as ``romanisim.psf`` takes them.
FOCUS, SPECTRAL_TYPE = 0, "G2V"

#: `dev`'s ``PSFKIND`` keyword value, also the file name's stem, for each ``psfs.kind``.
KINDS = {"science": "sciimage_psf"}

#: Rows of a derived PSF are good: the reduction finished.
STATUS_GOOD = 1


def run(job: JobContext) -> None:
    settings = job.settings["psf"]
    source_settings = job.settings["intake"]
    stem = job.scope["file"]
    if job.scope["dataset"] != job.dataset:
        raise ValueError(f"scope dataset {job.scope['dataset']!r} is not the run's {job.dataset!r}")
    timings: dict[str, float] = {}
    mark = time.monotonic()

    def lap(step: str) -> None:
        nonlocal mark
        now = time.monotonic()
        timings[step] = round(now - mark, 3)
        mark = now

    if source_settings.get("reader", "soc-l2") == "openuniverse":
        ids = parse_stem(stem)
        stamp, provenance = galsim_stamp(str(ids["filter"]), int(ids["sca"]), int(settings["size"]))
        lap("draw")
        cards = {"PSFSRC": (provenance["source"], "the simulation's PSF model"),
                 "PSFWAVE": (provenance["wavelength_nm"], "[nm] achromatic at the effective wavelength"),
                 "PSFX": GALSIM_POSITION[0], "PSFY": GALSIM_POSITION[1]}
        write(job, stamp, RAPID_TO_ROMAN_FILTER_NAMES[str(ids["filter"])], int(ids["sca"]), cards,
              timings, lap, source=None, psf=provenance)
        return
    if source_settings.get("reader", "soc-l2") == "rimtimsim":
        source, cal = None, named_epsf(stem, settings["epsf"])
    else:
        source = source_settings["source_root"].rstrip("/") + "/" + stem + "_cal.asdf"
        cal = cal_meta(get(source, unsigned=bool(source_settings["unsigned"])))
        lap("read_cal")
    name = PurePosixPath(cal["epsf"]).name
    url = settings["crds_server"].rstrip("/") + "/unchecked_get/references/roman/" + name
    size = int(settings["size"])
    with tempfile.TemporaryDirectory(prefix="rapidpipe-psf-") as scratch:
        reference, fetched_from = None, url
        if settings["epsf_cache"]:
            try:
                fetched_from = settings["epsf_cache"].rstrip("/") + "/" + name
                reference = get(fetched_from)
                if not reference.is_file():         # a file:// cache returns its path unread
                    raise FileNotFoundError(fetched_from)
            except FileNotFoundError:
                reference = None
                log.warning("psf: %s is not in the ePSF cache; fetching it from CRDS", name)
                fetched_from = url
        if reference is None:
            reference = fetch(url, Path(scratch) / name)
        lap("fetch")
        reference_bytes = reference.stat().st_size
        with asdf.open(reference, lazy_load=True, memmap=False) as af:
            meta = af["roman"]["meta"]
            own = (str(meta["instrument"]["detector"]), str(meta["instrument"]["optical_element"]))
            if own != (cal["detector"], cal["filter"]):
                raise ValueError(f"{name} is for {own[0]} {own[1]}, not the file's "
                                 f"{cal['detector']} {cal['filter']}")
            oversample = int(meta["oversample"])
            model = gridded_model(np.asarray(af["roman"]["psf"]), meta)
        lap("model")
    stamp, recorded = reduce(model, oversample, size)
    lap("reduce")

    provenance = {"epsf": name, "context": cal["context"], "position": list(POSITION),
                  "focus": FOCUS, "spectral_type": SPECTRAL_TYPE, "oversample": oversample,
                  "block": oversample, "size": size, "epsf_bytes": reference_bytes,
                  "fetched_from": fetched_from, **recorded}
    cards = {"EPSFREF": (name, "CRDS ePSF reference reduced"),
             "CRDSCTX": (cal["context"], "the L2 file's CRDS context"),
             "PSFX": POSITION[0], "PSFY": POSITION[1],
             "EPSFOVS": (oversample, "the reference's oversampling"),
             "EPSFBIN": (oversample, "samples summed per pixel per axis")}
    write(job, stamp, cal["filter"], int(cal["detector"].removeprefix("WFI")), cards, timings, lap,
          source=source, psf=provenance)


def write(job: JobContext, stamp: np.ndarray, filter_name: str, sca: int, cards: dict, timings: dict,
          lap, **manifest) -> None:
    """The stamp's FITS file per kind, its ``psfs`` rows and the manifest; ``filter_name`` the Roman
    spelling (``F146``), ``cards`` the header's provenance keywords, ``manifest`` its extra keys."""
    fid = FIDS[rapid_filter_name(filter_name)]
    rows = []
    for kind, keyword in KINDS.items():
        filename = f"{keyword}_{filter_name.lower()}_sca{sca:02d}.fits"
        header = fits.Header()
        header["SCA"] = sca
        header["FILTER"] = filter_name
        header["PSFKIND"] = keyword
        header["OVERSAMP"] = (1, "detector pixels")
        for card, value in cards.items():
            header[card] = value
        fits.PrimaryHDU(stamp.astype(np.float32), header).writeto(job.path / filename)
        digest = hashlib.md5((job.path / filename).read_bytes()).hexdigest()  # dev's checksum column
        rows.append({"dataset": job.dataset, "fid": fid, "sca": sca, "kind": kind,
                     "filename": job.prefix + filename, "checksum": digest, "status": STATUS_GOOD})
    pq.write_table(pa.Table.from_pylist(rows), job.path / "psfs.parquet")
    lap("rows")
    job.write_manifest([("psfs", "psfs.parquet")], timings_s=timings, **manifest)
    log.info("psf %s %s sca %d from %s: %s", job.dataset, filter_name, sca,
             manifest["psf"].get("epsf") or manifest["psf"].get("source"), timings)


def galsim_stamp(filter_name: str, sca: int, size: int) -> tuple[np.ndarray, dict]:
    """galsim.roman's PSF for ``filter_name`` (galsim's spelling, ``H158``) and ``sca``, ``size x size``
    at the pixel scale, unit sum, and what it records."""
    import galsim
    import galsim.roman as roman

    bandpass = roman.getBandpasses()[filter_name]
    model = roman.getPSF(sca, filter_name, SCA_pos=galsim.PositionD(*GALSIM_POSITION),
                         wavelength=bandpass.effective_wavelength)
    stamp = model.drawImage(nx=size, ny=size, scale=roman.pixel_scale, method="auto").array.astype(np.float64)
    held = float(stamp.sum())
    stamp = stamp / held
    rows, cols = np.indices(stamp.shape)
    centre = (size - 1) / 2
    return stamp, {"source": f"galsim.roman {galsim.__version__} getPSF", "galsim": galsim.__version__,
                   "position": list(GALSIM_POSITION), "wavelength_nm": float(bandpass.effective_wavelength),
                   "size": size, "flux_held": held,
                   "centroid_offset": [float((stamp * cols).sum() - centre), float((stamp * rows).sum() - centre)]}


def cal_meta(path: Path) -> dict[str, str]:
    """The L2 file's ePSF reference, CRDS context, detector (``WFI08``) and filter (``F146``)."""
    with asdf.open(path, lazy_load=True, memmap=False) as af:
        meta = af["roman"]["meta"]
        return {"epsf": str(meta["ref_file"]["epsf"]),
                "context": str(meta["ref_file"]["crds"]["context"]),
                "detector": str(meta["instrument"]["detector"]),
                "filter": str(meta["instrument"]["optical_element"])}


def named_epsf(stem: str, names: dict) -> dict[str, str]:
    """A RimTimSim stem's ePSF reference, the one ``[psf.epsf]`` names for its filter, as
    :func:`cal_meta` returns a SOC file's; no CRDS context."""
    ids = intake_rimtimsim.parse_stem(stem)
    name = names.get(str(ids["filter"]), "")
    if not name:
        raise ValueError(f"{stem}: the file names no ePSF reference and [psf.epsf] {ids['filter']} is empty")
    return {"epsf": name, "context": "", "detector": f"WFI{int(ids['sca']):02d}", "filter": str(ids["filter"])}


def fetch(url: str, target: Path) -> Path:
    """Download ``url`` to ``target``."""
    with urllib.request.urlopen(url, timeout=60) as reply, target.open("wb") as stream:
        shutil.copyfileobj(reply, stream, 1 << 20)
    return target


def gridded_model(psf: np.ndarray, meta):
    """The reference's in-focus G2V grid as a photutils ``GriddedPSFModel`` (romanisim's recipe)."""
    from photutils.psf import GriddedPSFModel

    images = psf[FOCUS, list(meta["spectral_type"]).index(SPECTRAL_TYPE)].astype(np.float64)
    positions = list(zip(np.asarray(meta["pixel_x"], dtype=float), np.asarray(meta["pixel_y"], dtype=float)))
    return GriddedPSFModel(NDData(images, meta={"grid_xypos": positions,
                                                "oversampling": int(meta["oversample"])}))


def reduce(model, oversample: int, size: int) -> tuple[np.ndarray, dict]:
    """The ``size x size`` pixel-scale stamp at :data:`POSITION`, unit sum, and what it records.

    Samples at the centres of ``oversample x oversample`` sub-pixels, offsets
    ``(j + 0.5) / oversample - size / 2`` from the position, so the
    position sits at the stamp's centre; each block is summed. Records the
    flux the stamp holds before normalising (the model's flux in the
    stamp) and the centroid's offset from the stamp centre, in pixels.
    """
    x0, y0 = POSITION
    offsets = (np.arange(oversample * size) + 0.5) / oversample - size / 2
    x, y = np.meshgrid(offsets + x0, offsets + y0)
    samples = model.evaluate(x, y, 1.0, x0, y0)
    stamp = samples.reshape(size, oversample, size, oversample).sum(axis=(1, 3))
    held = float(stamp.sum())
    stamp = stamp / held
    rows, cols = np.indices(stamp.shape)
    centre = (size - 1) / 2
    return stamp, {"flux_held": held,
                   "centroid_offset": [float((stamp * cols).sum() - centre),
                                       float((stamp * rows).sum() - centre)]}
