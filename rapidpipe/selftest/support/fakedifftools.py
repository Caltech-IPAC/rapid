"""Fake external tools for the difference stage, and a synthetic input set.

``FakeToolRunner`` stands in for :class:`rapidpipe.science.difference.tools.ToolRunner`:
it recognises each command the stage issues (SExtractor ``sex``, SWarp
``swarp``, ``bkgest``, ``py_zogy.py``, and the SFFT shell command) and
writes plausible outputs with numpy and astropy, so the whole stage can
run with no C tools, no ZOGY and no SFFT installed. The outputs are not
the tools' science; they are shaped like it (same files, same headers,
same catalog columns), which is what the stage's own logic needs. Every
call is recorded for assertions.

``fake_psf_catalog`` stands in for
:func:`rapidpipe.science.difference.psfcat.psf_catalog` (photutils), and
:class:`FakeReferencePsf` for
:func:`rapidpipe.science.difference.refpsf.measure` (the synthetic epoch
has too few stars for the real measurement).

``write_epoch`` writes a small synthetic epoch's files -- a gzipped
two-HDU science image with a SIP WCS, a reference bundle on the same sky
with its catalog, the science-image PSF -- for the
stage's unit and database tests.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from astropy.io import fits

from rapidpipe.science.difference.psfcat import PsfCatalogResult
from rapidpipe.science.difference.refpsf import ReferencePsf, TooFewStars

REPO_ROOT = Path(__file__).resolve().parents[3]
#: The env var an operator can point at an arbitrary cdf/ checkout, read
#: by :func:`cdf_dir`. Not set by the pipeline image itself (which relies
#: on the /code/cdf fallback below) -- an escape hatch for a checkout
#: whose cdf/ isn't where REPO_ROOT lands, or a non-image test host.
CDF_DIR_ENV = "RAPID_CFG"


def cdf_dir() -> Path:
    """Resolve the SExtractor parameter directory the selftest fixture needs.

    In a source checkout (``pip install -e``, or the test suite, where
    this module's package still lives under the repository's own
    ``rapidpipe/``) ``REPO_ROOT / "cdf"`` is correct, as it always was.
    Inside the pipeline image, ``rapidpipe`` is installed into the conda
    environment's ``site-packages`` (``containers/rapid-pipeline/
    Containerfile``: ``pip install --no-deps /code``) and ``tests/`` is
    excluded from the image (``build.sh``'s archive filter) -- the
    repository's own ``cdf/`` never lands under ``REPO_ROOT`` there, so
    ``REPO_ROOT / "cdf"`` does not exist. The image copies the whole
    filtered source tree to ``/code`` (kept explicitly: "cdf/" -- the
    Containerfile's own comment), so ``/code/cdf`` is the fallback, the
    same directory the stage's own default ``cfg_path`` setting names
    (``rapidpipe/settings/foundation-thin.toml`` (``[difference.*]``)).

    Tried in order, first that exists wins: ``REPO_ROOT / "cdf"`` (a
    checkout), ``$RAPID_CFG`` (an explicit override, for a checkout or
    test host where neither of the other two applies), ``/code/cdf`` (the
    image default). Raises :class:`FileNotFoundError` naming all three
    candidates when none exists, rather than failing later and obscurely
    on a missing parameter file.
    """
    candidates = [REPO_ROOT / "cdf"]
    env_value = os.environ.get(CDF_DIR_ENV)
    if env_value:
        candidates.append(Path(env_value))
    candidates.append(Path("/code/cdf"))

    for candidate in candidates:
        if candidate.is_dir():
            return candidate

    tried = ", ".join(str(c) for c in candidates)
    raise FileNotFoundError(
        f"cdf_dir: no candidate cdf/ directory exists; tried: {tried} "
        f"(set {CDF_DIR_ENV} to override)")


#: Back-compatible module-level constant: the repository checkout's own
#: cdf/ directory. Still used by callers that only ever run from a
#: checkout; :func:`cdf_dir` is the image-safe resolution used by
#: ``write_epoch``.
CDF_DIR = REPO_ROOT / "cdf"

NAXIS = 64
PIXEL_SCALE_DEG = 0.11 / 3600.0
CRVAL = (269.4521, -28.7710)
EXPTIME = 140.0
ZPTMAG = 17.0
STARS = ((20.0, 22.0, 4000.0), (40.0, 18.0, 2500.0), (30.0, 44.0, 6000.0), (50.0, 50.0, 3000.0))
TRANSIENT = (12.0, 40.0, 3500.0)   # in the science image only


def _gaussian(n: int, x0: float, y0: float, sigma: float = 1.2) -> np.ndarray:
    yy, xx = np.mgrid[0:n, 0:n]
    return np.exp(-(((xx - x0) ** 2) + ((yy - y0) ** 2)) / (2 * sigma ** 2))


def wcs_header(naxis: int, *, sip: bool) -> fits.Header:
    h = fits.Header()
    h["CTYPE1"] = "RA---TAN-SIP" if sip else "RA---TAN"
    h["CTYPE2"] = "DEC--TAN-SIP" if sip else "DEC--TAN"
    h["CRVAL1"], h["CRVAL2"] = CRVAL
    h["CRPIX1"] = naxis / 2.0
    h["CRPIX2"] = naxis / 2.0
    h["CD1_1"] = -PIXEL_SCALE_DEG
    h["CD1_2"] = 0.0
    h["CD2_1"] = 0.0
    h["CD2_2"] = PIXEL_SCALE_DEG
    h["CUNIT1"] = "deg"
    h["CUNIT2"] = "deg"
    if sip:
        h["A_ORDER"] = 2
        h["A_2_0"] = 1.0e-7
        h["B_ORDER"] = 2
        h["B_0_2"] = 1.0e-7
    return h


def _sky(n: int, stars, rng, *, background: float, noise: float) -> np.ndarray:
    data = rng.normal(background, noise, size=(n, n))
    for x0, y0, flux in stars:
        data += flux * _gaussian(n, x0, y0) / (2 * np.pi * 1.2 ** 2)
    return data


SCIENCE_NAME = "r0034001004001029016_0001_wfi07_f184_cal_lite.fits.gz"
#: The files :func:`write_epoch` writes, relative to its directory.
EPOCH_FILES = {
    "science": f"l2/{SCIENCE_NAME}",
    "reference": "ref/awaicgen_output_mosaic_image.fits",
    "coverage": "ref/awaicgen_output_mosaic_cov_map.fits",
    "uncertainty": "ref/awaicgen_output_mosaic_uncert_image.fits",
    "catalog": "ref/awaicgen_output_mosaic_refimsexcat.txt",
    "science_psf": "psf/sciimage_psf_f184_sca07.fits",
}


def write_epoch(directory: Path, *, seed: int = 20260922, magzp: float | None = 17.0) -> dict[str, Path]:
    """Write one synthetic epoch's input files under ``directory``; return them by role."""
    import gzip

    rng = np.random.default_rng(seed)
    paths = {role: directory / name for role, name in EPOCH_FILES.items()}
    for path in paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)

    # Science: DN in HDU 1, 64x64, dev's socsims layout, gzipped.
    sci = _sky(NAXIS, STARS + (TRANSIENT,), rng, background=100.0 * EXPTIME / 10, noise=5.0)
    sci_header = wcs_header(NAXIS, sip=True)
    sci_header["EXPTIME"] = EXPTIME
    sci_header["ZPTMAG"] = ZPTMAG
    sci_header["SCA_NUM"] = 7
    raw = fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(data=sci.astype(np.float32),
                                                         header=sci_header)])
    raw.writeto(paths["science"].with_suffix(""))
    paths["science"].write_bytes(gzip.compress(paths["science"].with_suffix("").read_bytes(), mtime=0))
    paths["science"].with_suffix("").unlink()

    # Reference bundle on the 65x65 grid the reformatted science image has,
    # DN/s, the science stars without the transient.
    n = NAXIS + 1
    ref = _sky(n, tuple((x, y, f / EXPTIME) for x, y, f in STARS), rng, background=0.0, noise=0.02)
    ref_header = wcs_header(n, sip=False)
    if magzp is not None:
        ref_header["MAGZP"] = magzp
    fits.PrimaryHDU(data=ref.astype(np.float32), header=ref_header).writeto(paths["reference"])
    cov = np.full((n, n), 5.0, dtype=np.float32)
    cov[:, :3] = 0.0            # an uncovered strip the coverage mask must blank
    fits.PrimaryHDU(data=cov, header=ref_header).writeto(paths["coverage"])
    fits.PrimaryHDU(data=np.full((n, n), 0.02, dtype=np.float32),
                    header=ref_header).writeto(paths["uncertainty"])
    params = _params(str(cdf_dir() / "rapidSexParamsRefImage.inp"))
    paths["catalog"].write_text(
        "".join(f"#{i + 1:4d} {p}\n" for i, p in enumerate(params))
        + "".join(" ".join(_catalog_row(p, int(x), int(y), flux) for p in params) + "\n"
                  for x, y, flux in STARS))

    # The science PSF: a 9x9 Gaussian, labelled as dev's PSF generator labels it.
    fits.PrimaryHDU(data=(3.0 * _gaussian(9, 4, 4)).astype(np.float32),
                    header=fits.Header({"PSFKIND": "sciimage_psf"})).writeto(paths["science_psf"])
    return paths


# ----------------------------------------------------------------------
# The fake runner
# ----------------------------------------------------------------------


def _option(args: list[str], name: str) -> str:
    return args[args.index(name) + 1]


def _read(path: Path) -> tuple[np.ndarray, fits.Header]:
    with fits.open(path) as hdul:
        return np.array(hdul[0].data, dtype=np.float64), hdul[0].header.copy()


def _params(params_file: str) -> list[str]:
    return [line.strip() for line in Path(params_file).read_text().splitlines() if line.strip()]


def _peaks(data: np.ndarray, nsigma: float = 5.0) -> list[tuple[int, int, float]]:
    finite = np.isfinite(data)
    if not finite.any():
        return []
    med = np.nanmedian(data)
    sigma = np.nanstd(data[finite]) or 1.0
    peaks = []
    n0, n1 = data.shape
    for i in range(2, n0 - 2):
        for j in range(2, n1 - 2):
            v = data[i, j]
            if not np.isfinite(v) or v < med + nsigma * sigma:
                continue
            box = data[i - 1:i + 2, j - 1:j + 2]
            if v >= np.nanmax(box):
                peaks.append((j, i, float(np.nansum(data[i - 2:i + 3, j - 2:j + 3]))))
    return peaks


def _catalog_row(param: str, x: int, y: int, flux: float) -> str:
    base = param.split("(")[0]
    values = {
        "XWIN_IMAGE": x + 1.0, "YWIN_IMAGE": y + 1.0, "X_IMAGE": x + 1.0, "Y_IMAGE": y + 1.0,
        "FLUX_APER": flux, "MAG_APER": -2.5 * np.log10(abs(flux)) if flux else 99.0,
        "CLASS_STAR": 0.9, "ISOAREAF_IMAGE": 12.0, "AWIN_WORLD": 1.0e-5, "BWIN_WORLD": 1.0e-5,
        "FWHM_IMAGE": 2.4,
    }
    return f"{values.get(base, 0.0):.6f}"


@dataclass
class FakeToolRunner:
    """Recognises the difference stage's commands and fakes their outputs."""

    sfft_exit_code: int = 0
    calls: list[list[str]] = field(default_factory=list)
    shell_calls: list[str] = field(default_factory=list)

    def tools_called(self) -> list[str]:
        names = []
        for args in self.calls:
            if args[0].endswith("sex"):
                names.append("sextractor")
            elif args[0].endswith("swarp"):
                names.append("swarp")
            elif args[0].endswith("bkgest"):
                names.append("bkgest")
            elif len(args) > 1 and args[1].endswith("py_zogy.py"):
                names.append("zogy")
            else:
                names.append(args[0])
        return names + ["sfft"] * len(self.shell_calls)

    def run(self, args, *, cwd: Path) -> int:
        args = [str(a) for a in args]
        self.calls.append(args)
        cwd = Path(cwd)
        if args[0].endswith("sex"):
            self._sextractor(args, cwd)
        elif args[0].endswith("swarp"):
            self._swarp(args, cwd)
        elif args[0].endswith("bkgest"):
            self._bkgest(args, cwd)
        elif len(args) > 1 and args[1].endswith("py_zogy.py"):
            self._zogy(args, cwd)
        else:
            raise AssertionError(f"unexpected tool call {args!r}")
        return 0

    def run_shell(self, command: str, *, cwd: Path) -> int:
        self.shell_calls.append(command)
        if self.sfft_exit_code != 0:
            return self.sfft_exit_code
        # No activation: the whole command is the sfft call. With
        # activation, dev's "source ... && <sfft cmd> && deactivate".
        parts = command.split(" && ")
        sfft_call = parts[1] if len(parts) > 1 else parts[0]
        tokens = shlex.split(sfft_call)
        sci, ref = tokens[2].removeprefix("./"), tokens[3].removeprefix("./")
        sci_data, header = _read(Path(cwd) / sci)
        ref_data, _ = _read(Path(cwd) / ref)
        crossconv = "--crossconv" in tokens
        diff = "sfftdiffimage_dconv_masked.fits" if crossconv else "sfftdiffimage_masked.fits"
        fits.PrimaryHDU(data=(sci_data - ref_data).astype(np.float32), header=header).writeto(
            Path(cwd) / diff, overwrite=True)
        soln = "sfftsoln_cconv.fits" if crossconv else "sfftsoln.fits"
        fits.PrimaryHDU(data=np.ones((3, 3), dtype=np.float32)).writeto(Path(cwd) / soln, overwrite=True)
        psf_name = "sfftdiffpsf_dconv.fits" if crossconv else "sfftdiffpsf.fits"
        psf_data, _ = _read(Path(cwd) / tokens[tokens.index("--scipsf") + 1])
        fits.PrimaryHDU(data=psf_data.astype(np.float32)).writeto(Path(cwd) / psf_name, overwrite=True)
        return 0

    # -- the tools --

    def _sextractor(self, args, cwd: Path) -> None:
        images = args[1].split(",")
        detection = images[0]
        data, _ = _read(cwd / detection)
        params = _params(_option(args, "-PARAMETERS_NAME"))
        lines = [f"#{i + 1:4d} {p}\n" for i, p in enumerate(params)]
        for x, y, flux in _peaks(data):
            lines.append(" ".join(_catalog_row(p, x, y, flux) for p in params) + "\n")
        (cwd / _option(args, "-CATALOG_NAME")).write_text("".join(lines))

    def _swarp(self, args, cwd: Path) -> None:
        source, _ = _read(cwd / args[1])
        output = _option(args, "-IMAGEOUT_NAME")
        grid_header = fits.getheader(cwd / output.replace(".fits", ".head"))
        n1, n2 = grid_header["NAXIS1"], grid_header["NAXIS2"]
        resampled = np.zeros((n2, n1))
        m2, m1 = min(n2, source.shape[0]), min(n1, source.shape[1])
        resampled[:m2, :m1] = source[:m2, :m1]
        if _option(args, "-SUBTRACT_BACK") == "Y":
            resampled -= np.median(resampled)
        header = grid_header.copy()
        fits.PrimaryHDU(data=resampled.astype(np.float32), header=header).writeto(
            cwd / output, overwrite=True)
        fits.PrimaryHDU(data=np.ones((n2, n1), dtype=np.float32)).writeto(
            cwd / _option(args, "-WEIGHTOUT_NAME"), overwrite=True)

    def _bkgest(self, args, cwd: Path) -> None:
        data, header = _read(cwd / _option(args, "-i"))
        fits.PrimaryHDU(data=(data - np.nanmedian(data)).astype(np.float32), header=header).writeto(
            cwd / _option(args, "-o2"), overwrite=True)
        (cwd / _option(args, "-ot")).write_text(f"clippedmean {np.nanmedian(data)}\n")

    def _zogy(self, args, cwd: Path) -> None:
        (new, ref, new_psf, _ref_psf, new_sigma, ref_sigma,
         _sn, _sr, _dx, _dy, diff, diff_psf, scorr) = args[2:15]
        new_data, header = _read(cwd / new)
        ref_data, _ = _read(cwd / ref)
        sig_new, _ = _read(cwd / new_sigma)
        sig_ref, _ = _read(cwd / ref_sigma)
        d = new_data - ref_data
        fits.PrimaryHDU(data=d.astype(np.float32), header=header).writeto(cwd / diff, overwrite=True)
        s = d / np.sqrt(sig_new ** 2 + sig_ref ** 2)
        fits.PrimaryHDU(data=s.astype(np.float32), header=header).writeto(cwd / scorr, overwrite=True)
        psf_data, _ = _read(cwd / new_psf)
        fits.PrimaryHDU(data=psf_data.astype(np.float32)).writeto(cwd / diff_psf, overwrite=True)


def fake_sip_to_pv(header) -> None:
    """Stands in for the vendored SIP-to-PV converter (which needs sympy)."""
    for key in [k for k in header if k.startswith(("A_", "B_", "AP_", "BP_"))]:
        del header[key]
    header["CTYPE1"] = "RA---TPV"
    header["CTYPE2"] = "DEC--TPV"


@dataclass
class FakePsfCatalog:
    """Stands in for ``psfcat.psf_catalog``; ``fail`` lists labels to fail."""

    fail: tuple[str, ...] = ()
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, settings, **kwargs) -> PsfCatalogResult:
        self.calls.append(kwargs)
        if kwargs["label"] in self.fail:
            return PsfCatalogResult(produced=False, nsources=None)
        data, _ = _read(Path(kwargs["image"]))
        peaks = _peaks(data)
        nsources = len(peaks)
        Path(kwargs["catalog"]).write_text(f"# id x_fit y_fit\n# {nsources} sources\n")
        Path(kwargs["finder"]).write_text("# finder\n")
        if kwargs.get("parquet"):
            _write_psfcat_parquet(Path(kwargs["parquet"]), peaks)
        return PsfCatalogResult(produced=True, nsources=nsources)


@dataclass
class FakeReferencePsf:
    """Stands in for ``refpsf.measure``: a 61 x 61 Gaussian of ``sigma``, or ``stars`` too few.

    Records each call's image shape, usable count, science PSF shape and ``crowded``.
    """

    sigma: float = 1.1
    stars: int = 40
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, image, usable, science_psf, crowded=False) -> ReferencePsf:
        self.calls.append({"shape": image.shape, "usable": int(usable.sum()),
                           "science_psf_shape": science_psf.shape, "crowded": crowded})
        if self.stars < 10:
            raise TooFewStars(self.stars)
        stamp = _gaussian(61, 30, 30, self.sigma)
        return ReferencePsf(stamp=stamp / stamp.sum(), stars=self.stars, detections=2 * self.stars,
                            passed_cuts=self.stars, sigma_px=self.sigma, centroid_px=(0.0, 0.0),
                            star_sigma_px=self.sigma)


def _write_psfcat_parquet(path: Path, peaks) -> None:
    """The joined PSF-fit catalog's columns the source set maps, one row per peak."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    n = len(peaks)
    x = np.array([p[0] for p in peaks], dtype=float)
    y = np.array([p[1] for p in peaks], dtype=float)
    flux = np.array([p[2] for p in peaks], dtype=float)
    ones = np.ones(n)
    pq.write_table(pa.table({
        "id": np.arange(1, n + 1), "ra": CRVAL[0] - (x - NAXIS / 2) * PIXEL_SCALE_DEG,
        "dec": CRVAL[1] + (y - NAXIS / 2) * PIXEL_SCALE_DEG, "x_fit": x, "y_fit": y,
        "flux_fit": flux, "x_err": 0.1 * ones, "y_err": 0.1 * ones,
        "flux_err": np.sqrt(np.abs(flux)) + 1.0, "n_pixels_fit": 25 * ones.astype(int),
        "qfit": 0.1 * ones, "cfit": 0.0 * ones, "reduced_chi2": ones,
        "flags": np.zeros(n, dtype=int), "sharpness": 0.5 * ones, "roundness1": 0.0 * ones,
        "roundness2": 0.0 * ones, "n_pixels": 25 * ones.astype(int), "peak": flux / 10.0}), path)


def fake_toolkit():
    """Every external tool faked, for a test to put in place of ``difference.toolkit``."""
    from rapidpipe.stages.difference import Toolkit

    return Toolkit(runner=FakeToolRunner(), sip_to_pv=fake_sip_to_pv, psf_catalog=FakePsfCatalog(),
                   reference_psf=FakeReferencePsf())
