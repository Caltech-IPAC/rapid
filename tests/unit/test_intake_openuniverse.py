"""The intake's OpenUniverse reader on a small synthetic ``simple_model`` file and a faked PSF.

Contracts: the stem parses and names the file's path under the source root;
the FITS file is the SOC reader's layout, its science image the file's
counts in DN, its keywords the file's own filter, SCA, exposure time and
start, the pointing as ``OBSERVTN`` and the zero point from ``[intake.
nominal_zptmag]``, and the truth tables follow the images; the SIP header is
zero-based, so a sky position the file's one-based gWCS puts at ``(x, y)``
lands at ``(x - 1, y - 1)``; a file whose own filter or detector contradicts
its name fails; under ``[intake] reader = "openuniverse"`` the stage reads
``<filter>/<pointing>/<stem>_simple.asdf`` and declares rows whose ``expid``
is the pointing; an unknown reader is refused before anything is read.
"""

from __future__ import annotations

import contextlib
import gzip
import json
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import pytest
from astropy.io import fits
from astropy.table import Table
from astropy.time import Time
from astropy.wcs import WCS

from rapidpipe.foundation.local import JobContext
from rapidpipe.science import intake_openuniverse as ou
from rapidpipe.stages import intake

STEM = "roman_tds_H158_10032_wfi08"
N = 64
EXPTIME = 302.275
MJD = 62130.091
SETTINGS = {"intake": {"source_root": None, "unsigned": False, "reader": "openuniverse",
                       "sip_degree": 5, "sip_max_pix_error": 0.1, "gzip_level": 1,
                       "program_id": -2, "limmag_nsigma": 5.0, "limmag_clip_sigma": 3.0,
                       "limmag_maxiters": 10, "nominal_zptmag": {"F158": 26.5113},
                       "dq_mask_bits": 0}}


def one_based_gwcs(ra: float = 9.66, dec: float = -43.95):
    """A TAN gWCS taking GalSim's one-based pixels, its reference pixel at the image centre."""
    from astropy import coordinates, units as u
    from astropy.modeling import models
    from gwcs import coordinate_frames as cf, wcs

    centre = models.Shift(-(N + 1) / 2) & models.Shift(-(N + 1) / 2)
    scale = models.Scale(0.11 / 3600) & models.Scale(0.11 / 3600)
    sky = models.Pix2Sky_TAN() | models.RotateNative2Celestial(ra, dec, 180)
    detector = cf.Frame2D(name="detector", axes_order=(0, 1), unit=(u.pix, u.pix))
    world = cf.CelestialFrame(reference_frame=coordinates.FK5(), name="world", unit=(u.deg, u.deg))
    return wcs.WCS(forward_transform=centre | scale | sky, input_frame=detector, output_frame=world)


def write_simple(path: Path, *, filter_name: str = "H158", detector: str = "SCA08") -> np.ndarray:
    """The file at ``path``; returns its counts."""
    import asdf

    rng = np.random.default_rng(0)
    data = rng.poisson(150.0, (N, N)).astype(np.float64)
    transients = Table({"id": [30045752, 50146549], "ra": [9.6601, 9.6599], "dec": [-43.95, -43.9502],
                        "peak_mjd": [62046.9, 62130.0], "peak_mag_F": [26.1, 24.9]})
    tree = {"roman": {
        "meta": {"optical_element": filter_name, "detector": detector, "exp_time": EXPTIME,
                 "mjd_obs": Time(MJD, format="mjd"), "ra_pointing": 9.57837, "dec_pointing": -43.6181,
                 "gain": 1.0, "zptmag": 17.69, "sky_mean": 147.0},
        "data": data, "err": data.astype(np.float32), "dq": np.zeros((N, N), dtype=np.uint32),
        "wcs": one_based_gwcs(), "catalogs": {"transients": transients}}}
    path.parent.mkdir(parents=True, exist_ok=True)
    asdf.AsdfFile(tree).write_to(path)
    return data


def convert(tmp_path: Path, **kwargs) -> tuple[Path, np.ndarray]:
    src = tmp_path / f"{STEM}_simple.asdf"
    counts = write_simple(src, **kwargs)
    out = tmp_path / f"{STEM}_cal.fits"
    ou.simple_model_to_fits(src, out, sip_degree=5, sip_max_pix_error=0.1,
                            nominal_zptmag=SETTINGS["intake"]["nominal_zptmag"], swname="test",
                            swvers="test", created="2026-10-24T00:00:00Z")
    return out, counts


def test_the_stem_parses_and_names_the_files_path():
    assert ou.parse_stem(STEM) == {"survey": "tds", "filter": "H158", "pointing": 10032, "sca": 8}
    assert ou.source_key(STEM) == f"H158/10032/{STEM}_simple.asdf"
    with pytest.raises(ValueError):
        ou.parse_stem("r0034201001001004001_0001_wfi08_f146")


def test_the_fits_file_is_the_soc_layout_with_the_files_own_keywords(tmp_path):
    path, counts = convert(tmp_path)
    with fits.open(path) as hdul:
        names = [h.name for h in hdul]
        primary, sci = hdul[0].header, hdul["SCI"]
        assert names[:2] == ["PRIMARY", "SCI"] and "TRANSIENTS" in names
        assert names.index("TRANSIENTS") > max(names.index(n) for n in ("SCI", "ERR", "DQ"))
        assert len(hdul["TRANSIENTS"].data) == 2
        assert (primary["FILTER"], primary["SCA_NUM"], primary["OBSERVTN"]) == ("H158", 8, 10032)
        assert primary["EXPTIME"] == EXPTIME and primary["MJD-OBS"] == pytest.approx(MJD)
        assert primary["ZPTMAG"] == 26.5113
        assert sci.header["BUNIT"] == "DN"
        np.testing.assert_allclose(sci.data, counts, rtol=1e-6)


def test_the_sip_header_is_zero_based(tmp_path):
    with fits.open(convert(tmp_path)[0]) as hdul:
        sip = WCS(hdul["SCI"].header)
    x = np.array([1.0, N, N, 1.0, (N + 1) / 2])  # one-based, the file's gWCS's own pixels
    y = np.array([1.0, 1.0, N, N, (N + 1) / 2])
    ra, dec = one_based_gwcs()(x, y)
    x0, y0 = sip.all_world2pix(ra, dec, 0)
    np.testing.assert_allclose(x0, x - 1, atol=0.01)
    np.testing.assert_allclose(y0, y - 1, atol=0.01)


@pytest.mark.parametrize("contradiction", [{"filter_name": "J129"}, {"detector": "SCA09"}])
def test_a_file_whose_own_filter_or_detector_contradicts_its_name_fails(tmp_path, contradiction):
    with pytest.raises(ValueError, match="not its name's"):
        convert(tmp_path, **contradiction)


def fake_connect(psf_filename: str):
    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, statement, params):
            assert "FROM psfs" in statement and params[1:] == (2, 8)  # H158, SCA 8

        def fetchone(self):
            return (psf_filename,)

    class Connection:
        def cursor(self):
            return Cursor()

    return lambda: contextlib.nullcontext(Connection())


def run_intake(tmp_path: Path, reader: str = "openuniverse") -> Path:
    source = tmp_path / "source"
    write_simple(source / ou.source_key(STEM))
    yy, xx = np.mgrid[:9, :9]
    psf = np.exp(-((xx - 4) ** 2 + (yy - 4) ** 2) / 4.0).astype(np.float32)
    fits.PrimaryHDU(psf / psf.sum()).writeto(source / "psf.fits")
    prefix = tmp_path / "outputs"
    prefix.mkdir()
    settings = {"intake": {**SETTINGS["intake"], "source_root": source.as_uri() + "/", "reader": reader}}
    job = JobContext(job=1, run=1, stage="intake", scope={"dataset": "ou-tds1", "file": STEM},
                     survey="HLTDS", dataset="ou-tds1", settings=settings,
                     inputs=[{"kind": "input", "product": "psfs", "key": {}, "source_job": 9,
                              "binding": None},
                             *({"kind": "expects", "product": p, "key": {}, "source_job": None,
                                "binding": None} for p in ("l2files", "exposures"))],
                     attempt=1, prefix=prefix.as_uri() + "/",
                     connect=fake_connect((source / "psf.fits").as_uri()))
    intake.run(job)
    return prefix


def test_the_stage_reads_the_openuniverse_path_and_keys_the_rows_by_pointing(tmp_path):
    prefix = run_intake(tmp_path)
    manifest = json.loads((prefix / "manifest.json").read_text())
    assert manifest["source"].endswith(f"/H158/10032/{STEM}_simple.asdf")
    (exposure,) = pq.read_table(prefix / "exposures.parquet").to_pylist()
    (l2file,) = pq.read_table(prefix / "l2files.parquet").to_pylist()
    assert (exposure["expid"], exposure["program_id"]) == (10032, -2)
    assert (l2file["dataset"], l2file["expid"], l2file["sca"], l2file["fid"]) == ("ou-tds1", 10032, 8, 2)
    with gzip.open(prefix / f"{STEM}_cal.fits.gz") as stream, fits.open(stream) as hdul:
        assert hdul["SCI"].header["FILTER"] == "H158"


def test_an_unknown_reader_is_refused_before_anything_is_read(tmp_path):
    with pytest.raises(ValueError, match="reader"):
        run_intake(tmp_path, reader="fits")
    assert not (tmp_path / "outputs" / "manifest.json").exists()
