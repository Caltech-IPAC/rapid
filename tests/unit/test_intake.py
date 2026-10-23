"""The intake stage on a small synthetic SOC L2 ASDF file and a faked PSF.

Contracts: the FITS file is `dev`'s layout, its science header carrying
the keywords the difference stage reads and its data in DN; the SIP header
reproduces the gWCS at the corners and the centre to within the fit's own
bound, ``sip_max_pix_error`` (0.1 px: ``to_fits_sip`` raises when a
fixed-degree fit misses it, and a corner is the domain's edge, where a
polynomial fit is worst); the ``exposures`` and ``l2files`` rows have
exactly their schema columns and the key the file name gives; the
``exposures`` row is declared only when the job expects its key; the
manifest names the file's provenance; a file whose own identifiers
contradict its name fails before a manifest; the file name parses, and the
expid packs, as the stage's docstring says; a file whose own ePSF
reference is not the one its science PSF names (``EPSFREF``) fails before
a manifest, and one whose PSF names the same reference publishes.
"""

from __future__ import annotations

import contextlib
import gzip
import json
import re
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import pytest
from astropy.io import fits
from astropy.wcs import WCS

from rapidpipe.foundation.local import JobContext
from rapidpipe.stages import intake

REPO = Path(__file__).resolve().parents[2]
STEM = "r0034201001001004001_0001_wfi08_f146"
N = 64
EXPTIME = 294.1
EPSF = "roman_wfi_epsf_0276.asdf"
SETTINGS = {"intake": {"source_root": None, "unsigned": False, "sip_degree": 5,
                       "sip_max_pix_error": 0.1, "gzip_level": 1, "program_id": -1,
                       "limmag_nsigma": 5.0, "limmag_clip_sigma": 3.0, "limmag_maxiters": 10,
                       "nominal_zptmag": {"F146": 27.5}}}


def schema_columns(table: str) -> set[str]:
    body = re.search(rf"CREATE TABLE {table} \((.*?)\n\);", (REPO / "database/schema.sql").read_text(),
                     re.S).group(1)
    names = {line.split()[0] for line in body.splitlines()
             if line.strip() and not line.strip().startswith(("UNIQUE", "CHECK", "--"))}
    return names - {"job", "created", "rid"}


def distorted_gwcs(ra: float = 9.67, dec: float = -45.2):
    """A TAN gWCS with a quadratic distortion of about two pixels at the corners."""
    from astropy import coordinates, units as u
    from astropy.modeling import models
    from gwcs import coordinate_frames as cf, wcs

    centre = models.Shift(-(N - 1) / 2) & models.Shift(-(N - 1) / 2)
    distortion = models.Mapping((0, 1, 0, 1)) | (
        models.Polynomial2D(2, c1_0=1.0, c2_0=1e-3, c1_1=5e-4)
        & models.Polynomial2D(2, c0_1=1.0, c0_2=-1e-3, c1_1=5e-4))
    scale = models.Scale(0.11 / 3600) & models.Scale(0.11 / 3600)
    sky = models.Pix2Sky_TAN() | models.RotateNative2Celestial(ra, dec, 180)
    detector = cf.Frame2D(name="detector", axes_order=(0, 1), unit=(u.pix, u.pix))
    world = cf.CelestialFrame(reference_frame=coordinates.ICRS(), name="world",
                              unit=(u.deg, u.deg))
    return wcs.WCS(forward_transform=centre | distortion | scale | sky,
                   input_frame=detector, output_frame=world)


def write_asdf(path: Path, stem: str = STEM) -> None:
    """A small WfiImage: every array the converter reads, the meta it needs."""
    from roman_datamodels import datamodels

    ids = intake.parse_stem(stem)
    model = datamodels.ImageModel.create_fake_data(shape=(N, N))
    rng = np.random.default_rng(0)
    model.data = rng.normal(1.0, 0.05, (N, N)).astype(np.float32)
    model.err = np.full((N, N), 0.05, dtype=np.float16)
    model.var_poisson = np.full((N, N), 0.0025, dtype=np.float16)
    meta = model.meta
    meta.wcs = distorted_gwcs()
    meta.instrument.detector = f"WFI{ids['sca']:02d}"
    meta.instrument.optical_element = ids["filter"]
    meta.exposure.exposure_time = EXPTIME
    from astropy.time import Time
    meta.exposure.start_time = Time("2027-07-01T15:28:47.000")
    meta.exposure.end_time = Time("2027-07-01T15:33:41.110")
    for name in ("program", "execution_plan", "pass", "segment", "observation", "visit", "exposure"):
        setattr(meta.observation, name, ids[name])
    meta.pointing.target_ra, meta.pointing.target_dec = 9.67, -45.2
    meta.photometry.conversion_megajanskys = 0.24
    meta.photometry.pixel_area = 2.72e-13
    meta.wcs_fit_results = {"status": "SUCCESS", "fitgeom": "rshift", "nmatches": 630,
                            "rmse": 0.17, "mae": 0.13}
    meta.calibration_software_version = "0.23.0"
    meta.ref_file.epsf = f"crds://{EPSF}"
    path.parent.mkdir(parents=True, exist_ok=True)
    model.save(path)


def fake_connect(psf_filename: str):
    """A connection factory whose one query, the PSF lookup, returns ``psf_filename``."""
    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, statement, params):
            assert "FROM psfs" in statement and params[1:] == (8, 8)

        def fetchone(self):
            return (psf_filename,)

    class Connection:
        def cursor(self):
            return Cursor()

    return lambda: contextlib.nullcontext(Connection())


@pytest.fixture()
def source(tmp_path: Path) -> Path:
    src = tmp_path / "source"
    write_asdf(src / f"{STEM}_cal.asdf")
    yy, xx = np.mgrid[:9, :9]
    psf = np.exp(-((xx - 4) ** 2 + (yy - 4) ** 2) / 4.0).astype(np.float32)
    fits.PrimaryHDU(psf / psf.sum()).writeto(src / "psf.fits")
    return src


def run_intake(tmp_path: Path, source: Path, stem: str = STEM, *,
               expects_exposure: bool = True) -> Path:
    prefix = tmp_path / "outputs"
    prefix.mkdir(parents=True)
    settings = {"intake": {**SETTINGS["intake"], "source_root": source.as_uri() + "/"}}
    expects = [{"kind": "expects", "product": "l2files", "key": {}, "source_job": None,
                "binding": None}]
    if expects_exposure:
        expects.append({"kind": "expects", "product": "exposures", "key": {}, "source_job": None,
                        "binding": None})
    job = JobContext(job=1, run=1, stage="intake", scope={"dataset": "r00342", "file": stem},
                     survey="HLWAS", dataset="r00342", settings=settings,
                     inputs=[{"kind": "input", "product": "psfs", "key": {}, "source_job": 9,
                              "binding": None}, *expects],
                     attempt=1, prefix=prefix.as_uri() + "/",
                     connect=fake_connect((source / "psf.fits").as_uri()))
    intake.run(job)
    return prefix


def science_header(prefix: Path) -> tuple[fits.Header, np.ndarray, list[str]]:
    with gzip.open(prefix / f"{STEM}_cal.fits.gz") as stream, fits.open(stream) as hdul:
        return hdul["SCI"].header.copy(), hdul["SCI"].data.copy(), [h.name for h in hdul]


def test_the_fits_file_has_devs_layout_and_the_keywords_the_difference_stage_reads(tmp_path, source):
    header, data, names = science_header(run_intake(tmp_path, source))
    assert names[:2] == ["PRIMARY", "SCI"] and {"ERR", "DQ", "VAR_POISSON"} <= set(names)
    for keyword in ("FILTER", "EXPTIME", "DATE-OBS", "MJD-OBS", "SCA_NUM", "ZPTMAG", "TARGRA",
                    "TARGDEC", "CRVAL1", "CRPIX1", "CD1_1", "CD2_2", "A_ORDER", "B_ORDER"):
        assert keyword in header, keyword
    assert header["FILTER"] == "W146" and header["SCA_NUM"] == 8 and header["BUNIT"] == "DN"
    assert all(f"{p}_{i}_{j}" in header for p in ("A", "B", "AP", "BP") for i in range(6) for j in range(6))
    assert np.isclose(np.median(data), EXPTIME, rtol=0.01)  # DN/s of about 1 times EXPTIME


def test_the_sip_header_reproduces_the_gwcs_within_the_fits_own_bound(tmp_path, source):
    header, _, _ = science_header(run_intake(tmp_path, source))
    gwcs = distorted_gwcs()
    x = np.array([0, N - 1, N - 1, 0, (N - 1) / 2])
    y = np.array([0, 0, N - 1, N - 1, (N - 1) / 2])
    ra_s, dec_s = WCS(header).all_pix2world(x, y, 0)
    ra_g, dec_g = gwcs(x, y)
    offset_px = np.hypot((ra_s - ra_g) * np.cos(np.radians(dec_g)), dec_s - dec_g) * 3600 / 0.11
    assert offset_px.max() < SETTINGS["intake"]["sip_max_pix_error"]


def test_the_rows_have_their_schema_columns_and_the_key_the_name_gives(tmp_path, source):
    prefix = run_intake(tmp_path, source)
    (exposure,) = pq.read_table(prefix / "exposures.parquet").to_pylist()
    (l2file,) = pq.read_table(prefix / "l2files.parquet").to_pylist()
    assert set(exposure) == schema_columns("exposures")
    assert set(l2file) == schema_columns("l2files")
    expid = 1 * 10**6 + 4 * 10**4 + 1 * 10**2 + 1
    assert (exposure["dataset"], exposure["expid"], exposure["program_id"]) == ("r00342", expid, -1)
    assert (l2file["dataset"], l2file["expid"], l2file["sca"], l2file["fid"]) == ("r00342", expid, 8, 8)
    assert l2file["filename"] == (prefix.as_uri() + "/") + f"{STEM}_cal.fits.gz"
    assert np.isfinite(l2file["limmag"]) and l2file["overlapfields"] and l2file["field"] > 0


def test_the_manifest_names_the_provenance(tmp_path, source):
    manifest = json.loads((run_intake(tmp_path, source) / "manifest.json").read_text())
    assert [o["table"] for o in manifest["outputs"]] == ["exposures", "l2files"]
    assert manifest["source"].endswith(f"{STEM}_cal.asdf")
    provenance = manifest["provenance"]
    assert (provenance["program"], provenance["pass"], provenance["observation"], provenance["visit"],
            provenance["exposure"]) == (342, 1, 4, 1, 1)
    assert provenance["romancal"] == "0.23.0"
    assert provenance["wcs_fit_results"]["status"] == "SUCCESS"
    assert provenance["wcs_fit_results"]["rmse"] == pytest.approx(0.17)


def test_the_exposures_row_is_declared_only_when_the_job_expects_it(tmp_path, source):
    expected = json.loads((run_intake(tmp_path / "a", source) / "manifest.json").read_text())
    assert [o["table"] for o in expected["outputs"]] == ["exposures", "l2files"]
    sibling = run_intake(tmp_path / "b", source, expects_exposure=False)
    assert [o["table"] for o in json.loads((sibling / "manifest.json").read_text())["outputs"]] \
        == ["l2files"]
    assert not (sibling / "exposures.parquet").exists()


def test_a_file_whose_identifiers_contradict_its_name_fails_before_a_manifest(tmp_path, source):
    other = "r0034201002001004001_0001_wfi08_f146"  # pass 2 by name
    (source / f"{STEM}_cal.asdf").rename(source / f"{other}_cal.asdf")  # pass 1 inside
    with pytest.raises(ValueError, match="identifiers"):
        run_intake(tmp_path, source, stem=other)
    assert not (tmp_path / "outputs" / "manifest.json").exists()


def test_the_stem_parses_and_the_expid_packs():
    ids = intake.parse_stem("r0034201013001014018_0003_wfi18_f213")
    assert ids == {"program": 342, "execution_plan": 1, "pass": 13, "segment": 1, "observation": 14,
                   "visit": 18, "exposure": 3, "sca": 18, "filter": "F213"}
    assert intake.expid(ids) == 13_141_803
    with pytest.raises(ValueError):
        intake.parse_stem("r00342_0001_wfi08_f146")
    with pytest.raises(ValueError):
        intake.expid({**ids, "visit": 100})


def _name_epsf(source: Path, name: str) -> None:
    with fits.open(source / "psf.fits", mode="update") as hdul:
        hdul[0].header["EPSFREF"] = name


def test_a_psf_from_the_files_own_epsf_reference_is_accepted(tmp_path, source):
    _name_epsf(source, EPSF)
    assert (run_intake(tmp_path, source) / "manifest.json").exists()


def test_a_psf_from_another_epsf_reference_refuses_the_file_before_a_manifest(tmp_path, source):
    _name_epsf(source, "roman_wfi_epsf_0999.asdf")
    with pytest.raises(ValueError, match="roman_wfi_epsf_0999.asdf"):
        run_intake(tmp_path, source)
    assert not (tmp_path / "outputs" / "manifest.json").exists()
