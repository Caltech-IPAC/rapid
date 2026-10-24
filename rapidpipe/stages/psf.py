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
identity, with no rmap match and no CRDS context. The reference's
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
from rapidpipe.products.filters import rapid_filter_name

log = logging.getLogger(__name__)

#: The filters table's fids by RAPID filter name (schema.sql).
FIDS = {"F184": 1, "H158": 2, "J129": 3, "K213": 4, "R062": 5, "Y106": 6, "Z087": 7, "W146": 8}

#: Where on the detector the stamp is evaluated: the SCA's centre, (x, y), 0-based pixels.
POSITION = (2048.0, 2048.0)
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

    source = source_settings["source_root"].rstrip("/") + "/" + stem + "_cal.asdf"
    cal = cal_meta(get(source, unsigned=bool(source_settings["unsigned"])))
    lap("read_cal")
    name = PurePosixPath(cal["epsf"]).name
    url = settings["crds_server"].rstrip("/") + "/unchecked_get/references/roman/" + name
    size = int(settings["size"])
    with tempfile.TemporaryDirectory(prefix="rapidpipe-psf-") as scratch:
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

    sca = int(cal["detector"].removeprefix("WFI"))
    filter_name = cal["filter"]
    fid = FIDS[rapid_filter_name(filter_name)]
    provenance = {"epsf": name, "context": cal["context"], "position": list(POSITION),
                  "focus": FOCUS, "spectral_type": SPECTRAL_TYPE, "oversample": oversample,
                  "block": oversample, "size": size, "epsf_bytes": reference_bytes, **recorded}
    rows = []
    for kind, keyword in KINDS.items():
        filename = f"{keyword}_{filter_name.lower()}_sca{sca:02d}.fits"
        header = fits.Header()
        header["SCA"] = sca
        header["FILTER"] = filter_name
        header["PSFKIND"] = keyword
        header["OVERSAMP"] = (1, "detector pixels")
        header["EPSFREF"] = (name, "CRDS ePSF reference reduced")
        header["CRDSCTX"] = (cal["context"], "the L2 file's CRDS context")
        header["PSFX"], header["PSFY"] = POSITION[0], POSITION[1]
        header["EPSFOVS"] = (oversample, "the reference's oversampling")
        header["EPSFBIN"] = (oversample, "samples summed per pixel per axis")
        fits.PrimaryHDU(stamp.astype(np.float32), header).writeto(job.path / filename)
        digest = hashlib.md5((job.path / filename).read_bytes()).hexdigest()  # dev's checksum column
        rows.append({"dataset": job.dataset, "fid": fid, "sca": sca, "kind": kind,
                     "filename": job.prefix + filename, "checksum": digest, "status": STATUS_GOOD})
    pq.write_table(pa.Table.from_pylist(rows), job.path / "psfs.parquet")
    lap("rows")
    job.write_manifest([("psfs", "psfs.parquet")], source=source, psf=provenance, timings_s=timings)
    log.info("psf %s %s sca %d from %s: %s", job.dataset, filter_name, sca, name, timings)


def cal_meta(path: Path) -> dict[str, str]:
    """The L2 file's ePSF reference, CRDS context, detector (``WFI08``) and filter (``F146``)."""
    with asdf.open(path, lazy_load=True, memmap=False) as af:
        meta = af["roman"]["meta"]
        return {"epsf": str(meta["ref_file"]["epsf"]),
                "context": str(meta["ref_file"]["crds"]["context"]),
                "detector": str(meta["instrument"]["detector"]),
                "filter": str(meta["instrument"]["optical_element"])}


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
