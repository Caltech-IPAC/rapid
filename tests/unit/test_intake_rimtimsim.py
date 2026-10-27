"""The intake's RimTimSim reader on a small synthetic lite FITS file and a faked PSF.

Contracts: the stem parses and names the file under the source root; the
FITS file is PRIMARY and SCI, its science image the file's DN (its NaN
kept), its WCS the file's, its keywords the file's own filter, SCA,
exposure time, start and ZPTMAG, the exposure number as ``OBSERVTN``; a
file whose own filter or SCA contradicts its name fails, and one not in DN,
without ``ZPTMAG``, or asked for a DQ mask is refused; under the dataset's
overlay the stage reads ``<root><stem>.fits.gz`` and declares rows keyed by
the exposure number with RimTimSim's ``program_id``.
A socsims lite file (``<SOC stem>_cal_lite.fits.gz``): the SOC stem parses
and names the file; the conversion keeps its DN, its WCS and inverse SIP,
its own centre, and writes the stem's observation identifiers; under its
overlay the stage reads it and keys the rows by the SOC ``expid``.
"""

from __future__ import annotations

import contextlib
import gzip
import json
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import pytest
import tomllib
from astropy.io import fits
from astropy.wcs import WCS

from rapidpipe.foundation.local import JobContext
from rapidpipe.science import intake_rimtimsim as rts
from rapidpipe.science.intake import ConversionError
from rapidpipe.stages import intake

STEM = "rimtimsim_WFI_F213_SCA02_000000411_lite"
N = 64
EXPTIME = 54.72
MJD = 61450.51327337697
ZPTMAG = 25.85726796291789
OVERLAY = Path(__file__).resolve().parents[2] / "rapidpipe/settings/datasets/rimtimsim-260622.toml"


def write_lite(path: Path, **cards) -> np.ndarray:
    """A lite file as dev's converter writes it, at ``path``; ``cards`` replace (``None`` drops) keywords.
    Returns its image."""
    header = fits.Header()
    header.update({"WCSAXES": 2, "CRPIX1": (N + 1) / 2, "CRPIX2": (N + 1) / 2,
                   "CD1_1": 2.6188e-05, "CD1_2": 1.4575e-05, "CD2_1": -1.5281e-05, "CD2_2": 2.5443e-05,
                   "CUNIT1": "deg", "CUNIT2": "deg", "CTYPE1": "RA---TAN-SIP", "CTYPE2": "DEC--TAN-SIP",
                   "CRVAL1": 268.4971455, "CRVAL2": -29.2047697, "RADESYS": "FK5", "EQUINOX": 2000.0,
                   "A_ORDER": 4, "B_ORDER": 4, "A_2_0": 0.0, "B_0_2": 0.0,
                   "FILTER": "K213", "DETECTOR": "SCA02", "TSTART": MJD + 2400000.5,
                   "DATE-OBS": "2027-02-14T12:19:06.820", "EXPOSURE": EXPTIME, "SOFTWARE": "rimtimsim_v2.0",
                   "MJD-OBS": MJD, "EXPTIME": EXPTIME, "SCA_NUM": 2, "ZPTMAG": ZPTMAG, "BUNIT": "DN"})
    for key, value in cards.items():
        if value is None:
            del header[key]
        else:
            header[key] = value
    rng = np.random.default_rng(0)
    data = rng.normal(900.0, 30.0, (N, N)).astype(">f4")
    data[3, 5] = np.nan
    path.parent.mkdir(parents=True, exist_ok=True)
    fits.HDUList([fits.PrimaryHDU(header=header), fits.ImageHDU(data, header)]).writeto(path)
    return data.astype(np.float32)


def convert(tmp_path: Path, dq_mask_bits: int = 0, **cards) -> tuple[Path, np.ndarray, Path]:
    src = tmp_path / f"{STEM}.fits.gz"
    data = write_lite(src, **cards)
    out = tmp_path / f"{STEM}_cal.fits"
    rts.lite_to_fits(src, out, sip_degree=5, sip_max_pix_error=0.1, nominal_zptmag={}, swname="test",
                     swvers="test", created="2026-10-24T00:00:00Z", dq_mask_bits=dq_mask_bits)
    return out, data, src


def test_the_stem_parses_and_names_the_file():
    assert rts.parse_stem(STEM) == {"filter": "F213", "sca": 2, "exposure": 411}
    assert rts.source_key(STEM) == f"{STEM}.fits.gz"
    with pytest.raises(ValueError):
        rts.parse_stem("roman_tds_H158_10032_wfi08")


def test_the_fits_file_is_primary_and_sci_with_the_files_own_pixels_wcs_and_keywords(tmp_path):
    path, data, src = convert(tmp_path)
    with fits.open(path) as hdul, fits.open(src) as lite:
        assert [h.name for h in hdul] == ["PRIMARY", "SCI"]
        primary, sci = hdul[0].header, hdul["SCI"]
        assert (primary["FILTER"], primary["SCA_NUM"], primary["OBSERVTN"]) == ("K213", 2, 411)
        assert primary["EXPTIME"] == EXPTIME and primary["MJD-OBS"] == pytest.approx(MJD, abs=1e-9)
        assert primary["ZPTMAG"] == ZPTMAG and sci.header["BUNIT"] == "DN"
        np.testing.assert_allclose(sci.data, data, rtol=1e-6)
        assert np.isnan(sci.data[3, 5])
        x, y = np.array([0.0, N - 1.0, 7.3]), np.array([0.0, N - 1.0, 20.1])
        np.testing.assert_allclose(WCS(sci.header).all_pix2world(x, y, 0),
                                   WCS(lite[1].header).all_pix2world(x, y, 0), atol=1e-12)


@pytest.mark.parametrize("cards", [{"FILTER": "Z087"}, {"SCA_NUM": 3}])
def test_a_file_whose_own_filter_or_sca_contradicts_its_name_fails(tmp_path, cards):
    with pytest.raises(ValueError, match="not its name's"):
        convert(tmp_path, **cards)


@pytest.mark.parametrize("cards,bits", [({"BUNIT": "e/s"}, 0), ({"ZPTMAG": None}, 0), ({}, 1)])
def test_a_file_not_in_dn_without_zptmag_or_asked_for_a_dq_mask_is_refused(tmp_path, cards, bits):
    with pytest.raises(ConversionError):
        convert(tmp_path, dq_mask_bits=bits, **cards)


def fake_connect(psf_filename: str, fid: int = 4, sca: int = 2):
    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, statement, params):
            assert "FROM psfs" in statement and params[1:] == (fid, sca)  # K213, SCA 2 by default

        def fetchone(self):
            return (psf_filename,)

    class Connection:
        def cursor(self):
            return Cursor()

    return lambda: contextlib.nullcontext(Connection())


def test_the_stage_under_the_overlay_reads_the_lite_file_and_keys_the_rows_by_exposure(tmp_path):
    source = tmp_path / "source"
    write_lite(source / rts.source_key(STEM))
    yy, xx = np.mgrid[:9, :9]
    psf = np.exp(-((xx - 4) ** 2 + (yy - 4) ** 2) / 4.0).astype(np.float32)
    fits.PrimaryHDU(psf / psf.sum()).writeto(source / "psf.fits")
    prefix = tmp_path / "outputs"
    prefix.mkdir()
    settings = {"intake": {"source_root": source.as_uri() + "/", "unsigned": False, "sip_degree": 5,
                           "sip_max_pix_error": 0.1, "gzip_level": 1, "limmag_nsigma": 5.0,
                           "limmag_clip_sigma": 3.0, "limmag_maxiters": 10, "nominal_zptmag": {},
                           "dq_mask_bits": 0, "rederive_wcs": False, "crpix_shift": [0.0, 0.0],
                           **tomllib.loads(OVERLAY.read_text())["intake"]}}
    job = JobContext(job=1, run=1, stage="intake", scope={"dataset": "rts1", "file": STEM},
                     survey="GBTDS", dataset="rts1", settings=settings,
                     inputs=[{"kind": "input", "product": "psfs", "key": {}, "source_job": 9,
                              "binding": None},
                             *({"kind": "expects", "product": p, "key": {}, "source_job": None,
                                "binding": None} for p in ("l2files", "exposures"))],
                     attempt=1, prefix=prefix.as_uri() + "/",
                     connect=fake_connect((source / "psf.fits").as_uri()))
    intake.run(job)
    manifest = json.loads((prefix / "manifest.json").read_text())
    assert manifest["source"].endswith(f"/{STEM}.fits.gz")
    (exposure,) = pq.read_table(prefix / "exposures.parquet").to_pylist()
    (l2file,) = pq.read_table(prefix / "l2files.parquet").to_pylist()
    assert (exposure["expid"], exposure["program_id"]) == (411, -3)
    assert (l2file["expid"], l2file["sca"], l2file["fid"], l2file["zptmag"]) == (411, 2, 4, ZPTMAG)
    with gzip.open(prefix / f"{STEM}_cal.fits.gz") as stream, fits.open(stream) as hdul:
        assert hdul["SCI"].header["FILTER"] == "K213"


SS_STEM = "r0034001005001003061_0001_wfi08_f146"
SS_OVERLAY = OVERLAY.parent / "gbtds-r00340-fakesrc.toml"


def write_socsims(path: Path) -> np.ndarray:
    """A socsims lite file as dev's converter wrote it: PRIMARY and SCI_ORIG, both with the keywords."""
    header = fits.Header()
    header.update({"WCSAXES": 2, "CRPIX1": (N + 1) / 2, "CRPIX2": (N + 1) / 2,
                   "CD1_1": -2.6331e-05, "CD1_2": 1.5240e-05, "CD2_1": 1.5688e-05, "CD2_2": 2.5932e-05,
                   "CUNIT1": "deg", "CUNIT2": "deg", "CTYPE1": "RA---TAN-SIP", "CTYPE2": "DEC--TAN-SIP",
                   "CRVAL1": 267.5565, "CRVAL2": -29.8512, "RADESYS": "ICRS",
                   "A_ORDER": 5, "B_ORDER": 5, "A_2_0": -3.06e-07, "B_0_2": 1.94e-06,
                   "AP_ORDER": 3, "BP_ORDER": 3, "AP_2_0": 3.04e-07, "BP_0_2": -1.94e-06,
                   "TARGAPER": "WFI_CEN", "TARGRA": 267.603154, "TARGDEC": -29.916446,
                   "EXPTIME": 66.41188, "DATE-OBS": "2027-10-07T23:52:00.000", "MJD-OBS": 61685.99444,
                   "SCA_NUM": 8, "FILTER": "W146", "ZPTMAG": 26.8266, "BUNIT": "DN"})
    data = np.random.default_rng(1).normal(1281.0, 40.0, (N, N)).astype(">f4")
    path.parent.mkdir(parents=True, exist_ok=True)
    fits.HDUList([fits.PrimaryHDU(header=header), fits.ImageHDU(data, header, name="SCI_ORIG")]).writeto(path)
    return data.astype(np.float32)


def test_a_socsims_stem_parses_as_the_soc_stem_and_names_the_lite_file():
    assert rts.parse_stem(SS_STEM) == {"program": 340, "execution_plan": 1, "pass": 5, "segment": 1,
                                       "observation": 3, "visit": 61, "exposure": 1, "sca": 8,
                                       "filter": "F146"}
    assert rts.source_key(SS_STEM) == f"{SS_STEM}_cal_lite.fits.gz"


def test_a_socsims_file_keeps_its_dn_wcs_and_centre_and_writes_the_stems_identifiers(tmp_path):
    src = tmp_path / rts.source_key(SS_STEM)
    data = write_socsims(src)
    out = tmp_path / f"{SS_STEM}_cal.fits"
    rts.lite_to_fits(src, out, sip_degree=5, sip_max_pix_error=0.1, nominal_zptmag={}, swname="test",
                     swvers="test", created="2026-10-27T00:00:00Z")
    with fits.open(out) as hdul, fits.open(src) as lite:
        primary, sci = hdul[0].header, hdul["SCI"]
        assert [h.name for h in hdul] == ["PRIMARY", "SCI"]
        assert (primary["FILTER"], primary["SCA_NUM"], primary["ZPTMAG"]) == ("W146", 8, 26.8266)
        assert (primary["PASS"], primary["OBSERVTN"], primary["VISIT"], primary["EXPOSURE"]) == (5, 3, 61, 1)
        assert (primary["TARGRA"], primary["TARGDEC"]) == (267.603154, -29.916446)
        np.testing.assert_allclose(sci.data, data, rtol=1e-6)
        assert sci.header["AP_2_0"] == 3.04e-07 and sci.header["A_2_0"] == -3.06e-07
        x, y = np.array([0.0, N - 1.0, 7.3]), np.array([0.0, N - 1.0, 20.1])
        np.testing.assert_allclose(WCS(sci.header).all_pix2world(x, y, 0),
                                   WCS(lite[1].header).all_pix2world(x, y, 0), atol=1e-12)


def test_the_stage_under_the_socsims_overlay_keys_the_rows_by_the_soc_expid(tmp_path):
    source = tmp_path / "source"
    write_socsims(source / rts.source_key(SS_STEM))
    yy, xx = np.mgrid[:9, :9]
    psf = np.exp(-((xx - 4) ** 2 + (yy - 4) ** 2) / 4.0).astype(np.float32)
    fits.PrimaryHDU(psf / psf.sum()).writeto(source / "psf.fits")
    prefix = tmp_path / "outputs"
    prefix.mkdir()
    settings = {"intake": {"source_root": source.as_uri() + "/", "unsigned": False, "sip_degree": 5,
                           "sip_max_pix_error": 0.1, "gzip_level": 1, "limmag_nsigma": 5.0,
                           "limmag_clip_sigma": 3.0, "limmag_maxiters": 10, "nominal_zptmag": {},
                           "dq_mask_bits": 0, "rederive_wcs": False, "crpix_shift": [0.0, 0.0],
                           "program_id": -1, **tomllib.loads(SS_OVERLAY.read_text())["intake"]}}
    job = JobContext(job=1, run=1, stage="intake", scope={"dataset": "ss1", "file": SS_STEM},
                     survey="GBTDS", dataset="ss1", settings=settings,
                     inputs=[{"kind": "input", "product": "psfs", "key": {}, "source_job": 9,
                              "binding": None},
                             *({"kind": "expects", "product": p, "key": {}, "source_job": None,
                                "binding": None} for p in ("l2files", "exposures"))],
                     attempt=1, prefix=prefix.as_uri() + "/",
                     connect=fake_connect((source / "psf.fits").as_uri(), fid=8, sca=8))
    intake.run(job)
    manifest = json.loads((prefix / "manifest.json").read_text())
    assert manifest["source"].endswith(f"/{SS_STEM}_cal_lite.fits.gz")
    (exposure,) = pq.read_table(prefix / "exposures.parquet").to_pylist()
    (l2file,) = pq.read_table(prefix / "l2files.parquet").to_pylist()
    assert (exposure["expid"], exposure["program_id"], exposure["visit"]) == (5036101, -1, 61)
    assert (l2file["expid"], l2file["sca"], l2file["fid"], l2file["zptmag"]) == (5036101, 8, 8, 26.8266)
