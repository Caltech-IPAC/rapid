"""The psf stage on a synthetic L2 file and a synthetic ePSF reference whose model is a known Gaussian.

Contracts: the job declares one ``psfs`` row, kind ``science``, with its
FITS file and ``PSFKIND`` (the reference PSF is the difference job's
measurement, not this stage's); the stamp is
``size x size``, holds the Gaussian's flux to within 0.5% (the reference
grid is cut at five sigma and spline-interpolated) and its centroid sits at
the stamp's centre within 0.01 pixel, for odd and even ``size``; every
header and the manifest carry the reference's name; a reference for
another detector or filter fails before a manifest. With ``epsf_cache``,
the reference is read from the cache by its name, and from CRDS only when
the cache lacks it, the manifest saying which. The fetch is faked.

With ``[intake] reader = "openuniverse"``, the stem's filter and SCA name
the stamp galsim.roman draws: no file is read and nothing is fetched, the
row's fid and file name are the filter's, and the header and manifest name
galsim; galsim's own stamp (when galsim imports) is ``size x size`` with
unit sum. The draw is faked in the plumbing test.

With ``[intake] reader = "rimtimsim"``, the reference ``[psf.epsf]`` names
for the stem's filter is fetched and reduced, no file read, the row the
stem's filter and SCA's; an empty name refuses.
"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
from pathlib import Path

import asdf
import numpy as np
import pyarrow.parquet as pq
import pytest
from astropy.io import fits

from rapidpipe.foundation.local import JobContext
from rapidpipe.stages import psf

REPO = Path(__file__).resolve().parents[2]
STEM = "r0034201001001004001_0001_wfi08_f146"
EPSF = "roman_wfi_epsf_0276.asdf"
OVERSAMPLE = 4
SIGMA_PX = 1.0


def schema_columns(table: str) -> set[str]:
    body = re.search(rf"CREATE TABLE {table} \((.*?)\n\);", (REPO / "database/schema.sql").read_text(),
                     re.S).group(1)
    names = {line.split()[0] for line in body.splitlines()
             if line.strip() and not line.strip().startswith(("UNIQUE", "CHECK", "--"))}
    return names - {"job", "created", "psfid"}


def write_reference(path: Path, detector: str = "WFI08", optical_element: str = "F146") -> None:
    """A 3x3 grid of one Gaussian, ``SIGMA_PX`` detector pixels, as flux per oversampled sample."""
    n = 41                                          # five sigma each side at 4x oversampling
    yy, xx = np.indices((n, n)) - (n - 1) / 2
    image = np.exp(-(xx ** 2 + yy ** 2) / (2 * (SIGMA_PX * OVERSAMPLE) ** 2))
    image /= image.sum()
    grid = np.broadcast_to(image, (3, 3, 9, n, n)).astype(np.float32)
    meta = {"instrument": {"detector": detector, "optical_element": optical_element},
            "oversample": OVERSAMPLE, "spectral_type": ["A0V", "G2V", "M5V"],
            "pixel_x": [4.0, 2047.5, 4091.0] * 3, "pixel_y": [4.0] * 3 + [2047.5] * 3 + [4091.0] * 3}
    asdf.AsdfFile({"roman": {"meta": meta, "psf": grid}}).write_to(path)


@pytest.fixture()
def source(tmp_path: Path) -> Path:
    src = tmp_path / "source"
    src.mkdir()
    meta = {"ref_file": {"epsf": f"crds://{EPSF}", "crds": {"context": "roman_0046.pmap"}},
            "instrument": {"detector": "WFI08", "optical_element": "F146"}}
    asdf.AsdfFile({"roman": {"meta": meta}}).write_to(src / f"{STEM}_cal.asdf")
    write_reference(src / EPSF)
    return src


def run_psf(tmp_path: Path, source: Path, monkeypatch, size: int = 15, epsf_cache: str = "",
            from_crds: bool = True) -> Path:
    fetched = []

    def fake_fetch(url: str, target: Path) -> Path:
        fetched.append(url)
        shutil.copyfile(source / url.rsplit("/", 1)[-1], target)
        return target

    monkeypatch.setattr(psf, "fetch", fake_fetch)
    prefix = tmp_path / f"outputs-{size}"
    prefix.mkdir(parents=True)
    settings = {"intake": {"source_root": source.as_uri() + "/", "unsigned": False},
                "psf": {"crds_server": "https://crds.example/", "size": size, "epsf_cache": epsf_cache}}
    job = JobContext(job=1, run=1, stage="psf", scope={"dataset": "r00342", "file": STEM},
                     survey="HLWAS", dataset="r00342", settings=settings, inputs=[], attempt=1,
                     prefix=prefix.as_uri() + "/", connect=lambda: contextlib.nullcontext(None))
    psf.run(job)
    assert fetched == ([f"https://crds.example/unchecked_get/references/roman/{EPSF}"] if from_crds else [])
    return prefix


def test_one_science_row_and_file_are_declared(tmp_path, source, monkeypatch):
    prefix = run_psf(tmp_path, source, monkeypatch)
    manifest = json.loads((prefix / "manifest.json").read_text())
    assert [o["table"] for o in manifest["outputs"]] == ["psfs"]
    rows = pq.read_table(prefix / "psfs.parquet").to_pylist()
    assert all(set(row) == schema_columns("psfs") for row in rows)
    assert [(r["kind"], r["dataset"], r["fid"], r["sca"]) for r in rows] == [
        ("science", "r00342", 8, 8)]
    assert rows[0]["filename"] == prefix.as_uri() + "/sciimage_psf_f146_sca08.fits"
    assert fits.getheader(prefix / "sciimage_psf_f146_sca08.fits")["PSFKIND"] == "sciimage_psf"
    assert not (prefix / "refimage_psf_f146_sca08.fits").exists()


@pytest.mark.parametrize("size", [15, 16])
def test_the_stamp_holds_the_flux_centred_on_the_stamp(tmp_path, source, monkeypatch, size):
    prefix = run_psf(tmp_path, source, monkeypatch, size)
    stamp = fits.getdata(prefix / "sciimage_psf_f146_sca08.fits").astype(float)
    assert stamp.shape == (size, size) and stamp.sum() == pytest.approx(1.0, abs=1e-6)
    held = json.loads((prefix / "manifest.json").read_text())["psf"]["flux_held"]
    assert held == pytest.approx(1.0, abs=5e-3)
    rows, cols = np.indices(stamp.shape)
    centre = (size - 1) / 2
    assert abs((stamp * cols).sum() - centre) < 0.01 and abs((stamp * rows).sum() - centre) < 0.01


def test_the_headers_and_manifest_carry_the_reference_name(tmp_path, source, monkeypatch):
    prefix = run_psf(tmp_path, source, monkeypatch)
    header = fits.getheader(prefix / "sciimage_psf_f146_sca08.fits")
    assert header["EPSFREF"] == EPSF and header["CRDSCTX"] == "roman_0046.pmap"
    recorded = json.loads((prefix / "manifest.json").read_text())["psf"]
    assert (recorded["epsf"], recorded["context"]) == (EPSF, "roman_0046.pmap")


@pytest.mark.parametrize("detector, optical_element", [("WFI09", "F146"), ("WFI08", "F158")])
def test_a_reference_for_another_detector_or_filter_refuses(tmp_path, source, monkeypatch,
                                                            detector, optical_element):
    write_reference(source / EPSF, detector, optical_element)
    with pytest.raises(ValueError, match="not the file's WFI08 F146"):
        run_psf(tmp_path, source, monkeypatch)
    assert not (tmp_path / "outputs-15" / "manifest.json").exists()


def test_the_epsf_cache_is_read_first_and_crds_only_for_a_name_it_lacks(tmp_path, source, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    shutil.copyfile(source / EPSF, cache / EPSF)
    prefix = run_psf(tmp_path / "hit", source, monkeypatch, epsf_cache=cache.as_uri(), from_crds=False)
    assert json.loads((prefix / "manifest.json").read_text())["psf"]["fetched_from"] == f"{cache.as_uri()}/{EPSF}"
    (cache / EPSF).unlink()
    prefix = run_psf(tmp_path / "miss", source, monkeypatch, epsf_cache=cache.as_uri())
    assert json.loads((prefix / "manifest.json").read_text())["psf"]["fetched_from"].startswith("https://crds")


OU_STEM = "roman_tds_H158_1012_wfi10"


def test_an_openuniverse_stem_names_the_galsim_stamp_and_reads_nothing(tmp_path, monkeypatch):
    drawn = []

    def fake_stamp(filter_name, sca, size):
        drawn.append((filter_name, sca, size))
        stamp = np.zeros((size, size))
        stamp[size // 2, size // 2] = 1.0
        return stamp, {"source": "galsim.roman 0.0 getPSF", "wavelength_nm": 1579.1, "size": size}

    monkeypatch.setattr(psf, "galsim_stamp", fake_stamp)
    monkeypatch.setattr(psf, "fetch", lambda url, target: pytest.fail("fetched " + url))
    settings = {"intake": {"source_root": (tmp_path / "absent").as_uri() + "/", "unsigned": True,
                           "reader": "openuniverse"},
                "psf": {"crds_server": "https://crds.example/", "size": 15, "epsf_cache": ""}}
    job = JobContext(job=1, run=1, stage="psf", scope={"dataset": "ou-tds2", "file": OU_STEM},
                     survey="HLTDS", dataset="ou-tds2", settings=settings, inputs=[], attempt=1,
                     prefix=tmp_path.as_uri() + "/", connect=lambda: contextlib.nullcontext(None))
    psf.run(job)
    assert drawn == [("H158", 10, 15)]
    rows = pq.read_table(tmp_path / "psfs.parquet").to_pylist()
    assert [(r["kind"], r["fid"], r["sca"]) for r in rows] == [("science", psf.FIDS["H158"], 10)]
    header = fits.getheader(tmp_path / "sciimage_psf_f158_sca10.fits")
    assert header["PSFSRC"].startswith("galsim.roman") and header["FILTER"] == "F158"
    assert json.loads((tmp_path / "manifest.json").read_text())["psf"]["source"].startswith("galsim.roman")


def test_galsims_stamp_is_the_size_asked_with_unit_sum():
    pytest.importorskip("galsim")
    stamp, recorded = psf.galsim_stamp("H158", 10, 15)
    assert stamp.shape == (15, 15) and stamp.sum() == pytest.approx(1.0, abs=1e-9)
    assert 0.5 < recorded["flux_held"] < 1.0 and recorded["wavelength_nm"] > 1000


RTS_STEM = "rimtimsim_WFI_F213_SCA02_000000411_lite"


def test_a_rimtimsim_stem_reduces_the_reference_psf_epsf_names_and_reads_no_file(tmp_path, monkeypatch):
    src = tmp_path / "source"
    src.mkdir()
    write_reference(src / EPSF, detector="WFI02", optical_element="F213")
    fetched = []

    def fake_fetch(url: str, target: Path) -> Path:
        fetched.append(url)
        shutil.copyfile(src / url.rsplit("/", 1)[-1], target)
        return target

    monkeypatch.setattr(psf, "fetch", fake_fetch)
    settings = {"intake": {"source_root": (tmp_path / "absent").as_uri() + "/", "unsigned": False,
                           "reader": "rimtimsim"},
                "psf": {"crds_server": "https://crds.example/", "size": 15, "epsf_cache": "",
                        "epsf": {"F087": "", "F213": EPSF}}}
    job = JobContext(job=1, run=1, stage="psf", scope={"dataset": "rts1", "file": RTS_STEM},
                     survey="GBTDS", dataset="rts1", settings=settings, inputs=[], attempt=1,
                     prefix=tmp_path.as_uri() + "/", connect=lambda: contextlib.nullcontext(None))
    psf.run(job)
    assert fetched == [f"https://crds.example/unchecked_get/references/roman/{EPSF}"]
    (row,) = pq.read_table(tmp_path / "psfs.parquet").to_pylist()
    assert (row["fid"], row["sca"], row["kind"]) == (psf.FIDS["K213"], 2, "science")
    assert fits.getheader(tmp_path / "sciimage_psf_f213_sca02.fits")["EPSFREF"] == EPSF
    job.settings["psf"]["epsf"]["F213"] = ""
    with pytest.raises(ValueError, match="names no ePSF"):
        psf.run(job)
