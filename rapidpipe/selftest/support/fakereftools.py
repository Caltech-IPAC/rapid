"""Fake external tools for the reference stage, and its fixture's synthetic frames.

``FakeReferenceToolRunner`` stands in for
:class:`rapidpipe.science.difference.tools.ToolRunner` in the reference
stage: it recognises ``awaicgen`` and ``sex``. The fake awaicgen reads the
``-f1``/``-f3`` list files, builds the output grid awaicgen would (TAN,
``-R``/``-D`` centre, ``-pa`` arcsec pixels, ``int(size / scale)`` pixels,
north up, ``CRPIX = (naxis + 1) / 2``), drops every input pixel on its
nearest output pixel, and writes the mean stack (``-o1``), a coverage map
of input-pixel counts (``-o2``) and the uncertainty of the mean,
``sqrt(sum sigma^2) / n`` (``-o3``); uncovered pixels are NaN in the image
and uncertainty and 0 in the coverage. The fake ``sex`` is the difference
stage's (:class:`rapidpipe.selftest.support.fakedifftools.FakeToolRunner`):
5-sigma peaks written in the params file's columns, ``FWHM_IMAGE`` 2.4.
The outputs are shaped like the tools' (same files, headers, catalog
columns), not their science. Every call is recorded.

:func:`write_frames` writes the fixture's three frames and input-set
manifest (``rapidpipe/selftest/fixtures/reference/``): L2-shaped
(an empty PRIMARY HDU and a ``SCI`` image HDU with a TAN WCS, ``EXPTIME``,
``ZPTMAG``, ``FILTER`` and ``MJD-OBS``), gzipped, dithered by a few pixels
around the centre of Roman tessellation tile 4711398, the same stars in
each. The committed files are its output; rerun it to regenerate them.

Packaged under ``rapidpipe.selftest.support`` (not ``tests/``, which the
pipeline image excludes) so ``rapidpipe selftest --stage reference`` can
import it inside the image.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS, FITSFixedWarning

from rapidpipe.selftest.support.fakedifftools import FakeToolRunner

# ----------------------------------------------------------------------
# The fixture's frames
# ----------------------------------------------------------------------

RTID = 4711398
UNIT_FILTER = "F146"
HEADER_FILTER = "W146"     # the RAPID spelling; the unit's F146 must match it
#: Tile 4711398's centre (``rapidpipe.science.spatial.field_center``).
TILE_CENTRE = (267.53906, -29.827858)
NAXIS = 96
PIXEL_SCALE_DEG = 0.11 / 3600.0
EXPTIME = 140.0
#: (instance id, file name, dither in pixels (dx, dy), ZPTMAG, MJD-OBS).
FRAMES = (
    ("01K6AREF00000000000000F001", "r0034001002001001001_SCA01_W146.fits.gz", (0.0, 0.0),
     26.84, 61679.0860),
    ("01K6AREF00000000000000F002", "r0034001002001001007_SCA01_W146.fits.gz", (3.0, -2.0),
     26.86, 61679.0960),
    ("01K6AREF00000000000000F003", "r0034001002001001013_SCA01_W146.fits.gz", (-2.0, 4.0),
     26.88, 61679.1060),
)
#: Stars as (dRA arcsec, dDec arcsec, total DN) from the tile centre.
STARS = ((-2.0, 1.5, 60000.0), (1.8, -1.2, 40000.0), (0.5, 2.6, 80000.0),
         (-1.0, -2.4, 30000.0))
SEED = 20260924


def _frame_wcs(dither: tuple[float, float]) -> fits.Header:
    ra0, dec0 = TILE_CENTRE
    h = fits.Header()
    h["CTYPE1"], h["CTYPE2"] = "RA---TAN", "DEC--TAN"
    h["CRVAL1"], h["CRVAL2"] = ra0, dec0
    h["CRPIX1"] = (NAXIS + 1) / 2.0 + dither[0]
    h["CRPIX2"] = (NAXIS + 1) / 2.0 + dither[1]
    h["CD1_1"], h["CD1_2"], h["CD2_1"], h["CD2_2"] = -PIXEL_SCALE_DEG, 0.0, 0.0, PIXEL_SCALE_DEG
    h["CUNIT1"], h["CUNIT2"] = "deg", "deg"
    h["EQUINOX"] = 2000.0
    return h


def _frame_data(wcs_header: fits.Header, rng: np.random.Generator) -> np.ndarray:
    data = rng.normal(1000.0, 20.0, size=(NAXIS, NAXIS))
    wcs = WCS(wcs_header)
    ra0, dec0 = TILE_CENTRE
    yy, xx = np.mgrid[0:NAXIS, 0:NAXIS]
    sigma = 1.2
    for dra, ddec, flux in STARS:
        ra = ra0 + dra / 3600.0 / np.cos(np.radians(dec0))
        dec = dec0 + ddec / 3600.0
        x, y = wcs.all_world2pix([[ra, dec]], 0)[0]
        data += flux * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * sigma ** 2)) \
            / (2 * np.pi * sigma ** 2)
    return data.astype(np.float32)


def _member(path: Path, base: Path) -> dict:
    raw = path.read_bytes()
    return {"role": "image", "path": path.relative_to(base).as_posix(), "bytes": len(raw),
            "sha256": "sha256:" + hashlib.sha256(raw).hexdigest()}


def write_frames(directory: Path) -> Path:
    """Write the three gzipped frames under ``directory/l2/`` and the input-set manifest."""
    rng = np.random.default_rng(SEED)
    (directory / "l2").mkdir(parents=True, exist_ok=True)
    outputs = []
    for instance, name, dither, zptmag, mjdobs in FRAMES:
        wcs_header = _frame_wcs(dither)
        sci = fits.ImageHDU(data=_frame_data(wcs_header, rng), header=wcs_header, name="SCI")
        sci.header["EXPTIME"] = (EXPTIME, "exposure time [s]")
        sci.header["ZPTMAG"] = (zptmag, "zero point [AB mag]")
        sci.header["FILTER"] = HEADER_FILTER
        sci.header["MJD-OBS"] = mjdobs
        sci.header["SCA_NUM"] = 1
        primary = fits.PrimaryHDU()
        primary.header["TELESCOP"] = "ROMAN"
        primary.header["FILTER"] = HEADER_FILTER
        path = directory / "l2" / name
        raw = path.with_suffix("")  # .fits
        fits.HDUList([primary, sci]).writeto(raw, overwrite=True)
        path.write_bytes(gzip.compress(raw.read_bytes(), mtime=0))
        raw.unlink()
        member = _member(path, directory)
        outputs.append({
            "kind": "l2-image", "format_version": "1", "instance": instance,
            "key": {"exposure": name.split("_")[0], "detector": "1", "version": "1"},
            "primary": member["path"], "members": [member], "registration": {},
        })
    manifest = {
        "schema_version": "1",
        "run": "01K6AREF0000000000000000RN",
        "unit": {"kind": "field", "id": f"{RTID}/{UNIT_FILTER}"},
        "stage": "input-set",
        "attempt": "01K6AREF0000000000000000AT",
        "execution_record": "exec/input-set.json",
        "inputs": {"manifest": "input-set", "products": {}, "result_sets": []},
        "outputs": outputs,
    }
    path = directory / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    return path


# ----------------------------------------------------------------------
# The fake runner
# ----------------------------------------------------------------------


def _option(args: list[str], name: str) -> str:
    return args[args.index(name) + 1]


def _list(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text().splitlines() if line.strip()]


@dataclass
class FakeReferenceToolRunner:
    """Recognises the reference stage's commands and fakes their outputs."""

    calls: list[list[str]] = field(default_factory=list)
    _sex: FakeToolRunner = field(default_factory=FakeToolRunner)

    def tools_called(self) -> list[str]:
        return ["sextractor" if a[0].endswith("sex") else Path(a[0]).name for a in self.calls]

    def run(self, args, *, cwd: Path) -> int:
        args = [str(a) for a in args]
        self.calls.append(args)
        cwd = Path(cwd)
        if args[0].endswith("awaicgen"):
            self._awaicgen(args, cwd)
        elif args[0].endswith("sex"):
            self._sex._sextractor(args, cwd)
        else:
            raise AssertionError(f"unexpected tool call {args!r}")
        return 0

    def _awaicgen(self, args: list[str], cwd: Path) -> None:
        images = _list(cwd / _option(args, "-f1"))
        uncerts = _list(cwd / _option(args, "-f3"))
        assert len(images) == len(uncerts), "awaicgen: -f1 and -f3 lists differ in length"
        scale = float(_option(args, "-pa")) / 3600.0
        naxis1 = int(float(_option(args, "-X")) / scale)
        naxis2 = int(float(_option(args, "-Y")) / scale)
        grid = fits.Header()
        grid["NAXIS"] = 2
        grid["NAXIS1"], grid["NAXIS2"] = naxis1, naxis2
        grid["CTYPE1"], grid["CTYPE2"] = "RA---TAN", "DEC--TAN"
        grid["CRVAL1"] = float(_option(args, "-R"))
        grid["CRVAL2"] = float(_option(args, "-D"))
        grid["CRPIX1"] = 0.5 * (naxis1 + 1.0)
        grid["CRPIX2"] = 0.5 * (naxis2 + 1.0)
        grid["CDELT1"], grid["CDELT2"] = -scale, scale
        grid["CROTA2"] = float(_option(args, "-C"))
        out_wcs = WCS(grid)

        total = np.zeros((naxis2, naxis1))
        variance = np.zeros((naxis2, naxis1))
        count = np.zeros((naxis2, naxis1))
        for image, uncert in zip(images, uncerts):
            with fits.open(cwd / image) as hdul, warnings.catch_warnings():
                # MJD-OBS without DATE-OBS: astropy's 'datfix' note, not a problem.
                warnings.simplefilter("ignore", FITSFixedWarning)
                data = np.array(hdul[0].data, dtype=np.float64)
                in_wcs = WCS(hdul[0].header)
            with fits.open(cwd / uncert) as hdul:
                sigma = np.array(hdul[0].data, dtype=np.float64)
            yy, xx = np.mgrid[0:data.shape[0], 0:data.shape[1]]
            world = in_wcs.all_pix2world(np.column_stack([xx.ravel(), yy.ravel()]), 0)
            out = np.rint(out_wcs.all_world2pix(world, 0)).astype(int)
            ox, oy = out[:, 0], out[:, 1]
            keep = (ox >= 0) & (ox < naxis1) & (oy >= 0) & (oy < naxis2)
            keep &= np.isfinite(data.ravel()) & np.isfinite(sigma.ravel())
            np.add.at(total, (oy[keep], ox[keep]), data.ravel()[keep])
            np.add.at(variance, (oy[keep], ox[keep]), sigma.ravel()[keep] ** 2)
            np.add.at(count, (oy[keep], ox[keep]), 1.0)

        with np.errstate(invalid="ignore", divide="ignore"):
            mean = np.where(count > 0, total / count, np.nan)
            unc = np.where(count > 0, np.sqrt(variance) / count, np.nan)
        header = out_wcs.to_header()
        header["BUNIT"] = "DN/s"
        fits.PrimaryHDU(data=mean.astype(np.float32), header=header).writeto(
            cwd / _option(args, "-o1"), overwrite=True)
        fits.PrimaryHDU(data=count.astype(np.float32), header=header).writeto(
            cwd / _option(args, "-o2"), overwrite=True)
        fits.PrimaryHDU(data=unc.astype(np.float32), header=header).writeto(
            cwd / _option(args, "-o3"), overwrite=True)


def fake_toolkit():
    """Every external tool faked: the value ``RAPIDPIPE_REFERENCE_TOOLKIT`` names
    (``rapidpipe.selftest.support.fakereftools:fake_toolkit``)."""
    from rapidpipe.stages.reference import Toolkit

    return Toolkit(runner=FakeReferenceToolRunner())


if __name__ == "__main__":  # pragma: no cover -- regenerates the committed fixture
    import sys

    target = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        Path(__file__).resolve().parents[1] / "fixtures" / "reference")
    print(write_frames(target))
