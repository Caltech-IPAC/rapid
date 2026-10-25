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
a manifest, and one whose PSF names the same reference publishes; the DQ
mask (``[intake] dq_mask_bits``, default 0, `dev`'s behaviour: nothing
masked) sets ``SCI``, ``ERR`` and ``VAR_*`` NaN where ``DQ`` has a masked
bit, leaves ``DQ`` as it was, carries ``DQMASK`` and ``NDQMASK`` on each
masked HDU and counts the file's own NaN, the masked pixels and their union
in the manifest; the HLWAS overlay's bits are #238's names without
SATURATED; ``[intake] crpix_shift`` (default ``[0, 0]``) is added to
``CRPIX1`` and ``CRPIX2`` and recorded in the manifest, and the HLWAS overlay
carries the WCS ledger's measured translation; ``[intake] rederive_wcs``
(default false, `dev`'s behaviour) fetches nothing and keeps the file's
gWCS, and on fetches the distortion reference the file names and writes
the SIP fit of the re-derived gWCS, the manifest naming the reference and
the shift at the centre; the GBTDS overlay turns it on and HLWAS's does not;
``[intake] gaia_proper_motion`` on, a Gaia star that moved by its proper
motion from the copy's ``ref_epoch`` to the frame's mid-exposure reads
offset zero, and off (the shipped value; flight's overlay turns it on) the
offset is the copy's positions' as they are.
"""

from __future__ import annotations

import contextlib
import gzip
import json
import re
import sys
import types
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import tomllib
from astropy.io import fits
from astropy.wcs import WCS

from rapidpipe.foundation.local import JobContext
from rapidpipe.stages import intake

REPO = Path(__file__).resolve().parents[2]
STEM = "r0034201001001004001_0001_wfi08_f146"
N = 64
EXPTIME = 294.1
EPSF = "roman_wfi_epsf_0276.asdf"
SETTINGS = {"intake": {"source_root": None, "unsigned": False, "reader": "soc-l2", "sip_degree": 5,
                       "sip_max_pix_error": 0.1, "gzip_level": 1, "program_id": -1,
                       "limmag_nsigma": 5.0, "limmag_clip_sigma": 3.0, "limmag_maxiters": 10,
                       "nominal_zptmag": {"F146": 27.5}, "dq_mask_bits": 0,
                       "crpix_shift": [0.0, 0.0], "rederive_wcs": False},
            "psf": {"crds_server": "https://crds.invalid"}}
DISTORTION = "roman_wfi_distortion_0003.asdf"
#: Pixels the synthetic file flags, each (y, x, DQ value); (1, 1) is also NaN in the file itself.
FLAGGED = [(1, 1, 0x1), (2, 3, 0x2000000), (5, 7, 0x2 | 0x400), (9, 9, 0x2), (11, 4, 0x4)]
NATIVE_NAN = [(1, 1), (20, 20)]


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
    model.dq = np.zeros((N, N), dtype=np.uint32)
    for y, x, bits in FLAGGED:
        model.dq[y, x] = bits
    for y, x in NATIVE_NAN:
        model.data[y, x] = np.nan
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
    meta.ref_file.distortion = f"crds://{DISTORTION}"
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
               expects_exposure: bool = True, dq_mask_bits: int = 0,
               crpix_shift: tuple[float, float] = (0.0, 0.0),
               rederive_wcs: bool = False) -> Path:
    prefix = tmp_path / "outputs"
    prefix.mkdir(parents=True)
    settings = {**SETTINGS, "intake": {**SETTINGS["intake"], "source_root": source.as_uri() + "/",
                                       "dq_mask_bits": dq_mask_bits, "crpix_shift": list(crpix_shift),
                                       "rederive_wcs": rederive_wcs}}
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
    assert np.isclose(np.nanmedian(data), EXPTIME, rtol=0.01)  # DN/s of about 1 times EXPTIME


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


def hdus(prefix: Path) -> dict[str, fits.ImageHDU]:
    with gzip.open(prefix / f"{STEM}_cal.fits.gz") as stream, fits.open(stream) as hdul:
        return {h.name: h.copy() for h in hdul[1:] if h.is_image}


def test_dq_mask_bits_defaults_to_devs_behaviour_nothing_masked(tmp_path, source):
    shipped = tomllib.loads((REPO / "rapidpipe/settings/foundation-thin.toml").read_text())
    assert shipped["intake"]["dq_mask_bits"] == 0
    prefix = run_intake(tmp_path, source)
    images = hdus(prefix)
    assert all("DQMASK" not in h.header for h in images.values())
    assert np.isnan(images["SCI"].data).sum() == len(NATIVE_NAN)
    manifest = json.loads((prefix / "manifest.json").read_text())
    assert manifest["dq"] == {"mask_bits": "0x0", "masked": 0, "nan_input": 2, "nan_output": 2}
    assert manifest["metrics"] == {"masked_fraction": 0.0}


def test_the_mask_sets_sci_err_and_var_nan_keeps_dq_and_counts_the_union(tmp_path, source):
    bits = 0x1 | 0x400 | 0x2000000  # DO_NOT_USE, DEAD, UNRELIABLE_FLAT
    prefix = run_intake(tmp_path, source, dq_mask_bits=bits)
    images = hdus(prefix)
    masked = {(y, x) for y, x, flags in FLAGGED if flags & bits}
    assert masked == {(1, 1), (2, 3), (5, 7)}
    for name in ("SCI", "ERR", "VAR_POISSON"):
        data, header = images[name].data, images[name].header
        assert all(np.isnan(data[y, x]) for y, x in masked), name
        assert header["DQMASK"] == bits and header["NDQMASK"] == len(masked), name
    assert np.isfinite(images["SCI"].data[9, 9]) and np.isfinite(images["ERR"].data[11, 4])
    assert "DQMASK" not in images["DQ"].header
    assert {(y, x, int(images["DQ"].data[y, x])) for y, x, _ in FLAGGED} == set(FLAGGED)
    union = masked | set(NATIVE_NAN)
    assert {tuple(p) for p in np.argwhere(np.isnan(images["SCI"].data))} == union
    manifest = json.loads((prefix / "manifest.json").read_text())
    assert manifest["dq"] == {"mask_bits": hex(bits), "masked": 3, "nan_input": 2,
                              "nan_output": len(union)}
    assert manifest["metrics"]["masked_fraction"] == pytest.approx(3 / N**2)


def test_the_hlwas_mask_is_238s_named_bits_without_saturated():
    from roman_datamodels.dqflags import pixel

    overlay = tomllib.loads((REPO / "rapidpipe/settings/datasets/hlwas-r00342.toml").read_text())
    names = ("DO_NOT_USE", "DEAD", "HOT", "NONLINEAR", "NO_LIN_CORR", "UNRELIABLE_FLAT")
    named = 0
    for name in names:
        named |= int(pixel[name])
    assert overlay["intake"]["dq_mask_bits"] == named
    assert named | int(pixel.SATURATED) == 0x2110C03  # #238's default, dqMaskSubs.py
    assert not named & int(pixel.SATURATED) and not named & int(pixel.JUMP_DET)


def test_crpix_shift_defaults_to_nothing_and_translates_the_wcs_by_the_setting(tmp_path, source):
    shipped = tomllib.loads((REPO / "rapidpipe/settings/foundation-thin.toml").read_text())
    assert shipped["intake"]["crpix_shift"] == [0.0, 0.0]
    still, _, _ = science_header(run_intake(tmp_path / "still", source))
    prefix = run_intake(tmp_path / "moved", source, crpix_shift=(0.25, -0.5))
    moved, _, _ = science_header(prefix)
    assert moved["CRPIX1"] == pytest.approx(still["CRPIX1"] + 0.25)
    assert moved["CRPIX2"] == pytest.approx(still["CRPIX2"] - 0.5)
    world = WCS(still).all_pix2world([[10.0, 20.0]], 0)
    assert WCS(moved).all_world2pix(world, 0)[0] == pytest.approx([10.25, 19.5], abs=1e-4)
    assert json.loads((prefix / "manifest.json").read_text())["crpix_shift"] == [0.25, -0.5]


def test_the_hlwas_overlay_carries_the_ledgers_translation():
    overlay = tomllib.loads((REPO / "rapidpipe/settings/datasets/hlwas-r00342.toml").read_text())
    assert overlay["intake"]["crpix_shift"] == [0.12, -0.99]


#: The re-derived gWCS the faked romancal assigns: the file's moved this many pixels in declination.
SHIFT_PX = 3.0


@pytest.fixture()
def fake_romancal(monkeypatch, tmp_path):
    """romancal's ``load_wcs`` faked to assign a moved gWCS, and the CRDS fetch to a local file."""
    calls = []

    def load_wcs(model, reference_files):
        calls.append(reference_files)
        model.meta["wcs"] = distorted_gwcs(dec=-45.2 + SHIFT_PX * 0.11 / 3600)
        return model

    module = types.ModuleType("romancal.assign_wcs.assign_wcs")
    module.load_wcs = load_wcs
    for name in ("romancal", "romancal.assign_wcs"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "romancal.assign_wcs.assign_wcs", module)

    def fetch_reference(server, name, directory):
        calls.append((server, name))
        (directory / name).write_bytes(b"distortion")
        return directory / name

    monkeypatch.setattr(intake, "fetch_reference", fetch_reference)
    return calls


def test_rederive_wcs_defaults_to_devs_behaviour_the_files_own_gwcs(tmp_path, source, fake_romancal):
    prefix = run_intake(tmp_path, source)
    assert fake_romancal == []
    manifest = json.loads((prefix / "manifest.json").read_text())
    assert manifest["wcs"] == {"rederived": False}
    assert "wcs_shift_dx_px" not in manifest["metrics"]


def test_rederive_wcs_fits_the_gwcs_rederived_from_the_files_own_distortion_reference(
        tmp_path, source, fake_romancal):
    prefix = run_intake(tmp_path, source, rederive_wcs=True)
    assert fake_romancal[0] == ("https://crds.invalid", DISTORTION)
    assert Path(fake_romancal[1]["distortion"]).name == DISTORTION
    header, _, _ = science_header(prefix)
    moved = distorted_gwcs(dec=-45.2 + SHIFT_PX * 0.11 / 3600)
    centre = (N - 1) / 2
    ra_s, dec_s = WCS(header).all_pix2world([centre], [centre], 0)
    ra_g, dec_g = moved(centre, centre)
    assert np.hypot((ra_s[0] - ra_g) * np.cos(np.radians(dec_g)), dec_s[0] - dec_g) * 3600 / 0.11 \
        < SETTINGS["intake"]["sip_max_pix_error"]
    manifest = json.loads((prefix / "manifest.json").read_text())
    assert manifest["wcs"]["rederived"] and manifest["wcs"]["distortion"] == DISTORTION
    shift = np.hypot(manifest["metrics"]["wcs_shift_dx_px"], manifest["metrics"]["wcs_shift_dy_px"])
    assert shift == pytest.approx(SHIFT_PX, abs=0.05)


def test_the_gbtds_overlay_rederives_the_wcs_and_hlwas_does_not():
    overlays = REPO / "rapidpipe/settings/datasets"
    gbtds = tomllib.loads((overlays / "gbtds-r00340.toml").read_text())
    hlwas = tomllib.loads((overlays / "hlwas-r00342.toml").read_text())
    assert gbtds["intake"]["rederive_wcs"] is True
    assert "rederive_wcs" not in hlwas.get("intake", {})


PM_MAS_YR = (-15.0, 12.0)      # (pmra, pmdec): about 1.4 and 1.1 px over ten years


def gaia_frame(tmp_path: Path, proper_motion: bool):
    """A frame at 2026.0 whose stars sit on its WCS, and a 2016.0 copy that moved by ``PM_MAS_YR``."""
    from rapidpipe.science.alerts.crossmatch import propagate_positions
    from tests.unit.test_astrometry import frame, grid, tan_wcs

    w, stars = tan_wcs(), grid()
    ra, dec = w.all_pix2world(stars[:, 0], stars[:, 1], 0)
    pmra, pmdec = (np.full(len(stars), v) for v in PM_MAS_YR)
    ra16, dec16 = propagate_positions(ra, dec, -pmra, -pmdec, 10.0)
    path = tmp_path / "gaia.parquet"
    pq.write_table(pa.table({"ra": ra16, "dec": dec16, "pmra": pmra, "pmdec": pmdec,
                             "ref_epoch": np.full(len(stars), 2016.0)}), path)

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, statement, params):
            assert "FROM catalogs" in statement

        def fetchone(self):
            return (path.as_uri(),)

    class Connection:
        def cursor(self):
            return Cursor()

    job = types.SimpleNamespace(
        settings={"intake": {"gaia_proper_motion": proper_motion}},
        inputs=[{"kind": "input", "product": "catalogs", "source_job": 3,
                 "key": {"dataset": "flight", "name": "gaia", "version": 1}}],
        connect=lambda: contextlib.nullcontext(Connection()))
    mid = 51544.5 + 26.0 * 365.25                                 # 2026.0
    header = fits.Header({"MJD-OBS": mid - 30.0 / 86400.0, "EXPTIME": 60.0})
    return job, frame(stars), w, header, (ra16, dec16)


def test_with_proper_motion_a_star_moved_by_it_reads_offset_zero(tmp_path):
    job, image, w, header, _ = gaia_frame(tmp_path, proper_motion=True)
    measured = intake._gaia_metrics(job, image, w, header)
    assert measured["n_gaia_stars"] > 0
    assert measured["gaia_dx_px"] == pytest.approx(0.0, abs=0.03)
    assert measured["gaia_dy_px"] == pytest.approx(0.0, abs=0.03)


def test_without_proper_motion_the_offset_is_the_copys_positions_as_they_are(tmp_path):
    from rapidpipe.science.astrometry import gaia_offset

    job, image, w, header, (ra16, dec16) = gaia_frame(tmp_path, proper_motion=False)
    measured = intake._gaia_metrics(job, image, w, header)
    assert measured == gaia_offset(image, w, ra16, dec16)
    assert abs(measured["gaia_dy_px"]) > 0.5


def test_the_shipped_setting_reads_the_copy_as_it_is_and_flight_propagates():
    shipped = tomllib.loads((REPO / "rapidpipe/settings/foundation-thin.toml").read_text())
    flight = tomllib.loads((REPO / "rapidpipe/settings/datasets/flight-1049.toml").read_text())
    assert shipped["intake"]["gaia_proper_motion"] is False
    assert flight["intake"]["gaia_proper_motion"] is True


class _Nodes:
    """A datamodel-shaped mapping whose ``unreadable`` nodes raise on access."""

    def __init__(self, nodes: dict, unreadable: set):
        self.nodes, self.unreadable = nodes, unreadable

    def keys(self):
        return list(self.nodes)

    def __getitem__(self, key):
        if key in self.unreadable:
            raise OSError(f"block of {key} is corrupt")
        return self.nodes[key]


def test_an_unreadable_masked_node_fails_intake_and_an_unknown_extra_is_skipped():
    from rapidpipe.science.intake import asdf_image_nodes, asdf_table_nodes

    image = np.zeros((4, 4), dtype=np.float32)
    nodes = {"meta": {}, "data": image, "err": image, "extra": image}
    for read in (asdf_image_nodes, asdf_table_nodes):
        with pytest.raises(OSError, match="err"):
            read(_Nodes(nodes, {"err"}))
    assert [name for name, _ in asdf_image_nodes(_Nodes(nodes, {"extra"}))] == ["data", "err"]
    assert asdf_table_nodes(_Nodes(nodes, {"extra"})) == []
