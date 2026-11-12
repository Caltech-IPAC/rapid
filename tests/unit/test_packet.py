"""The packet stage's assembly (``write_set``) on a synthetic object set and two small images.

Contracts: every record is valid 00.07; a member detected after the
trigger is not a previous detection and is not counted in nDiaSources;
each index row's offset and length hold exactly its record; the three
cutouts are (2 half width + 1) pixels square with off-chip pixels zero;
a forced row a predecessor set holds is carried, not measured again,
while a pair it lacks is measured; a difference image's neighbours are
read under its own suffix, so a gzipped epoch gives the packets a plain
one gives, and each gzipped file is decompressed at most once per set,
whichever reader asks and however often. An unreadable stamp image of the trigger's epoch fails the
set, naming the file; the stamps are the trigger's alone, so a window
epoch's unreadable reference image does not. The forced window is the images covering the triggers'
objects, whichever field each image is centred on (the field rule,
runbook part 12 step 1): an image centred elsewhere that holds an object
is in it, one that holds none is not, the trigger's own always; the
query's reach admits every image of the detector that overlaps the
trigger's.
"""

from __future__ import annotations

import gzip
import io

import fastavro
import fastavro.validation
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from astropy.io import fits
from astropy.wcs import WCS

from astropy.time import Time

from rapidpipe.science.alerts import forced as fp
from rapidpipe.science.alerts import kona
from rapidpipe.science.alerts.assemble import load_schema
from rapidpipe.science.alerts.crossmatch import REFCAT_COLUMNS
from rapidpipe.science.alerts.records import Source
from rapidpipe.foundation import prefix
from rapidpipe.science.catalogs.readers import catalog_reader
from rapidpipe.science.statistics.sums import Sums
from rapidpipe.stages.packet import (
    CatalogInput, Read, WindowImage, _plain, _separation, alert_catalog, cap_triggers, covering,
    measure_forced, neighbour, reliability, window_reach, write_set, xsource_fields)
from tests.support import fakekete
from tests.support.catalogs import (
    ra_offset, write_gaia_store, write_hp6_store, write_lvs_store, write_ss_store)

N = 100                       # image side, pixels
RA0, DEC0 = 267.96, -28.72    # image centre
SETTINGS = {"stamp_half_width": 64, "prv_window_days": 365.25, "forced_window_days": 30.0,
            "max_alerts": 2000,
            "gaia_match": True, "ned_match": True, "lvs_match": True,
            "gaia_radius_arcsec": 1.0, "gaia_max_matches": 3,
            "ned_radius_arcsec": 10.0, "ned_max_matches": 3, "ned_host_selection": False,
            "lvs_radius_arcsec": 30.0, "lvs_max_matches": 3,
            "ss_match": True, "ss_cone_radius_deg": 0.15, "ss_radius_arcsec": 7.04, "ss_max_matches": 3,
            "refcat_radius_arcsec": 5.0, "refcat_max_matches": 3}
SOURCE_JOB = {1: 11, 2: 12, 3: 13}    # epoch -> its source set's job
MJD = {1: 61685.0, 2: 61686.0, 3: 61687.0}
SS_DAY = "20271007"                   # the ss day before epoch 2's date (20271008)


@pytest.fixture(autouse=True)
def fake_kete(monkeypatch):
    """The solar-system match runs on tests/support/fakekete.py, never the real kete."""
    monkeypatch.setattr(kona, "_kete", lambda: fakekete)


def write_l2(path, mjd: float) -> None:
    """An L2 file's primary header with the ephemeris cards the intake writes, and one image HDU."""
    header = fits.Header()
    for key, value in (("meta exposure mid_time", f"{Time(mjd, format='mjd').isot}"),
                       ("meta ephemeris time", mjd), ("meta ephemeris ephemeris_reference_frame", "?"),
                       ("meta ephemeris spatial_x", 1.0e8), ("meta ephemeris spatial_y", -1.0e8),
                       ("meta ephemeris spatial_z", 5.0e7), ("meta ephemeris velocity_x", 10.0),
                       ("meta ephemeris velocity_y", 20.0), ("meta ephemeris velocity_z", -5.0)):
        header[key] = value
    fits.HDUList([fits.PrimaryHDU(header=header), fits.ImageHDU(np.zeros((2, 2), np.float32))]).writeto(path)


def wcs() -> WCS:
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.crval = [RA0, DEC0]
    w.wcs.crpix = [N / 2 + 0.5, N / 2 + 0.5]
    w.wcs.cdelt = [-0.11 / 3600, 0.11 / 3600]
    return w


def gaussian(size: int, x: float, y: float, sigma: float = 1.2) -> np.ndarray:
    yy, xx = np.mgrid[0:size, 0:size]
    return np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * sigma ** 2))


def write_epoch(directory, stars: list[tuple[float, float, float]], gzipped: bool = False) -> None:
    """An epoch's job directory: difference, uncertainty, PSF, science and reference images."""
    directory.mkdir()
    header = wcs().to_header()
    image = sum((flux * gaussian(N, x - 1, y - 1) / (2 * np.pi * 1.2 ** 2) for x, y, flux in stars),
                np.zeros((N, N)))  # each star's total is `flux`
    rng = np.random.default_rng(1)
    for name, data in (("zogy_diffimage_masked.fits", image + rng.normal(0, 1, (N, N))),
                       ("zogy_diffimage_uncert_masked.fits", np.ones((N, N))),
                       ("bkg_subbed_science_image.fits", image + 5 + rng.normal(0, 1, (N, N))),
                       ("awaicgen_output_mosaic_image_resampled_gainmatched.fits",
                        np.full((N, N), 3.0))):
        fits.PrimaryHDU(data.astype(np.float32), header=header).writeto(directory / name)
    psf = gaussian(21, 10, 10)
    fits.PrimaryHDU(psf / psf.sum()).writeto(directory / "diffpsf.fits")
    if gzipped:   # as a difference job publishes them since the trim
        for path in list(directory.iterdir()):
            path.with_name(path.name + ".gz").write_bytes(gzip.compress(path.read_bytes()))
            path.unlink()


def write_refcat(path, columns=REFCAT_COLUMNS, rows: int = 1) -> None:
    """A SExtractor ASCII_HEAD catalog with ``columns``, every value 1."""
    head = "".join(f"# {i:3d} {name}\n" for i, name in enumerate(columns, 1))
    path.write_text(head + "".join(" ".join(["1"] * len(columns)) + "\n" for _ in range(rows)))


def corners() -> tuple[float, ...]:
    ra, dec = wcs().all_pix2world([1, N, N, 1], [1, 1, N, N], 1)
    return tuple(v for pair in zip(ra, dec) for v in pair)


def source(epoch: int, sid: int, x: float, y: float, aid: int) -> Source:
    ra, dec = wcs().all_pix2world([x], [y], 1)
    return Source(sid=sid, aid=aid, expid=epoch, sca=8, mjdobs=MJD[epoch], ra=float(ra[0]),
                  dec=float(dec[0]), xfit=x - 1, yfit=y - 1, band="W146", xerr=0.1, yerr=0.1,
                  fluxfit=500.0, fluxerr=10.0, flags=0, field=1, pid=100 + epoch, exptime=60.0)


def build(tmp_path, reuse_first_epoch: bool, gzipped: bool = False, unreadable: str | None = None,
          broken_catalog: str | None = None):
    """Two objects: A with members in epochs 1, 2 and 3; B (near the edge) only in epoch 2.

    The packet set is epoch 2's; epoch 3's detection of A is after the trigger.
    ``unreadable`` names a file under the job directories (``e1/...``) overwritten with bytes
    that are not FITS. ``broken_catalog`` names a catalog whose copy could not be opened.
    """
    write_epoch(tmp_path / "e1", [(50, 50, 400.0)], gzipped)
    write_epoch(tmp_path / "e2", [(50, 50, 500.0), (20, 80, 300.0)], gzipped)
    if unreadable:
        (tmp_path / unreadable).write_bytes(b"not a FITS file")
    diff = "zogy_diffimage_masked.fits" + (".gz" if gzipped else "")

    def beside(e, name):
        return tmp_path / f"e{e}" / neighbour(name, diff)

    sci_psf = beside(1, "diffpsf.fits")
    (tmp_path / "l2").mkdir()                      # the L2 files, as the intake publishes them
    for e in (1, 2):
        write_l2(tmp_path / "l2" / f"e{e}.fits", MJD[e])
    images = [WindowImage(pid=100 + e, rid=200 + e, expid=e, sca=8, fid=8, band="W146",
                          mjdobs=MJD[e], corners=corners(), diff=tmp_path / f"e{e}" / diff,
                          diff_psf=beside(e, "diffpsf.fits"),
                          diff_uncert=beside(e, "zogy_diffimage_uncert_masked.fits"),
                          sci=beside(e, "bkg_subbed_science_image.fits"),
                          sci_psf=sci_psf, l2=(tmp_path / "l2" / f"e{e}.fits").as_uri()) for e in (1, 2)]
    a1, a2, a3 = (source(e, 1, 50, 50, aid=7) for e in (1, 2, 3))
    b2 = source(2, 2, 20, 80, aid=9)
    sums_a, sums_b = Sums(a1.ra, a1.dec), Sums(b2.ra, b2.dec)
    for member in (a1, a2):
        sums_a.add(member.ra, member.dec, member.fluxfit)
    sums_b.add(b2.ra, b2.dec, b2.fluxfit)
    objects = {7: (a1.ra, a1.dec, sums_a), 9: (b2.ra, b2.dec, sums_b)}
    members = {7: [(SOURCE_JOB[1], a1), (SOURCE_JOB[2], a2), (SOURCE_JOB[3], a3)],
               9: [(SOURCE_JOB[2], b2)]}
    reuse = {}
    if reuse_first_epoch:
        forced_id = fp.forced_measurement_id(201, 7, a1.ra, a1.dec)
        reuse[forced_id] = {"forced_id": forced_id, "aid": 7, "rid": 201, "pid": 101, "expid": 1,
                            "sca": 8, "fid": 8, "band": "W146", "mjdobs": MJD[1], "ra": a1.ra,
                            "dec": a1.dec, "x": 50.0, "y": 50.0, "psf_flux": 12345.0,
                            "psf_fluxerr": 1.0, "science_flux": 54321.0, "science_fluxerr": 1.0,
                            "flags": 0, "time_proc": 61000.0}
    gaia_ra, gaia_dec = wcs().all_pix2world([50.0], [50.0], 1)
    ra_a, dec_a = float(gaia_ra[0]), float(gaia_dec[0])
    (tmp_path / "gaia").mkdir()
    write_gaia_store(tmp_path / "gaia", [
        {"source_id": 42, "ra": ra_a, "dec": dec_a, "pmra": 0.0, "pmdec": 0.0},
        # a star 0.3 degrees away: in the mirror's one leaf, outside the chip's cone
        {"source_id": 43, "ra": ra_a + 0.3, "dec": dec_a + 0.1}])
    # B sits 4.7" from A towards +RA, +Dec (pixel x runs against RA). NED: a host 9" from A
    # towards -RA (within 10" of A, 12.7" from B), one 40" off; LVS: a galaxy 28" from A towards
    # -RA (within 30" of A, 31.5" from B)
    (tmp_path / "ned").mkdir()
    write_hp6_store(tmp_path / "ned", [
        {"ra": ra_a - ra_offset(dec_a, 9.0), "dec": dec_a, "prefname": "HOST-A", "ptype": "G", "z": 0.03},
        {"ra": ra_a - ra_offset(dec_a, 40.0), "dec": dec_a, "prefname": "TOO-FAR", "ptype": "G"}])
    write_lvs_store(tmp_path / "ned", [{"ra": ra_a - ra_offset(dec_a, 28.0), "dec": dec_a,
                                        "objname": "LVS-A", "DistMpc": 62.0}])
    # ss: a known object 0.3" from A (a candidate), one 20" off (outside the match radius, in the
    # cone), one 0.5 degrees away (outside the cone); the day before epoch 2's
    write_ss_store(tmp_path / "ss", SS_DAY, [("2024 AB", ra_a - ra_offset(dec_a, 0.3), dec_a, 18.0),
                                            ("433", ra_a + ra_offset(dec_a, 20.0), dec_a, 11.0),
                                            ("far", ra_a + 0.5, dec_a, 11.0)])
    catalogs = {name: CatalogInput(name, 3 + i, catalog_reader(name, (tmp_path / root).as_uri() + "/"))
                for i, (name, root) in enumerate((("gaia", "gaia"), ("ned", "ned"), ("lvs", "ned"),
                                                  ("ss", "ss")))}
    if broken_catalog:
        catalogs[broken_catalog] = CatalogInput(broken_catalog, catalogs[broken_catalog].job, None,
                                                "RuntimeError: the copy could not be opened")
    read = Read(key={"dataset": "D", "expid": 2, "sca": 8, "fid": 8},
                source_job=SOURCE_JOB[2], object_job={7: 5, 9: 5}, field_of={7: 1, 9: 1},
                flags_zero=2,
                triggers=[a2, b2], aid_of={1: 7, 2: 9}, objects=objects, members=members,
                images=images, trigger_image=images[1], reuse=reuse,
                catalogs=catalogs, refcat_path=None)
    out = tmp_path / "out"
    out.mkdir()
    summary = write_set(read, SETTINGS, out, out.as_uri() + "/", "D", sync_seed="test")
    with (out / summary["avro"]).open("rb") as stream:
        records = list(fastavro.reader(stream))
    return out, summary, {r["diaSourceId"]: r for r in records}


def test_records_are_valid_and_history_is_bounded_by_the_trigger(tmp_path):
    out, summary, records = build(tmp_path, reuse_first_epoch=False)
    schema = load_schema()
    for record in records.values():
        assert fastavro.validation.validate(record, schema, raise_errors=True)
    a = records[1]
    assert [p["midpointMjd"] for p in a["prvDiaSources"]] == [MJD[1]]
    assert a["diaObject"]["nDiaSources"] == 2
    assert records[2]["prvDiaSources"] is None and records[2]["diaObject"]["nDiaSources"] == 1
    # the F146 statistics over A's two detections up to the trigger (epoch 3's excluded), B's one;
    # the mean position from the object's sums
    obj = a["diaObject"]
    assert (obj["F146PsfFluxNdata"], obj["F146PsfFluxMean"], obj["F146PsfFluxSigma"]) == (2, 500.0, 0.0)
    assert obj["F146PsfFluxMaxSlope"] == 0.0 and obj["F146PsfFluxErrMean"] == 10.0
    assert obj["F184PsfFluxNdata"] is None and obj["F184PsfFluxMean"] is None
    assert obj["meanRa"] == pytest.approx(obj["ra0"], abs=1e-9) and obj["meanDec"] == pytest.approx(obj["dec0"], abs=1e-9)
    assert records[2]["diaObject"]["F146PsfFluxNdata"] == 1 and records[2]["diaObject"]["F146PsfFluxMean"] == 500.0
    assert [m["sourceId"] for m in a["gaiaMatches"]] == [42] and records[2]["gaiaMatches"] == []
    assert [m["prefName"] for m in a["nedMatches"]] == ["HOST-A"] and records[2]["nedMatches"] == []
    assert a["nedMatches"][0]["z"] == pytest.approx(0.03) and a["nedMatches"][0]["sep"] == pytest.approx(9.0, abs=0.02)
    assert [m["prefName"] for m in a["lvsMatches"]] == ["LVS-A"] and records[2]["lvsMatches"] == []
    assert a["lvsMatches"][0]["distMpc"] == pytest.approx(62.0)
    assert a["refStarMatches"] is None
    # the solar-system match: the day before the trigger's date, the predictions in the chip's cone
    assert [(m["designation"], m["predVMag"]) for m in a["ssMatches"]] == [("2024 AB", pytest.approx(23.0))]
    assert a["ssMatches"][0]["sep"] == pytest.approx(0.3, abs=0.01) and a["diaSource"]["isSSCandidate"] is True
    # B, 4.7" from A, sees the same object within the 7" radius but is no candidate (0.5")
    b = records[2]
    assert [m["designation"] for m in b["ssMatches"]] == ["2024 AB"] and b["ssMatches"][0]["sep"] > 4.0
    assert b["diaSource"]["isSSCandidate"] is False
    ss = summary["catalogs"]["ss"]
    assert (ss["release"], ss["n_objects"], ss["n_in_cone"]) == (SS_DAY, 3, 2)
    assert ss["mid_time"].startswith("2027-10-08T00:00:00")
    external = pq.read_table(out / "external.parquet").to_pylist()
    assert [(e["sid"], e["catid"], e["catalog_job"]) for e in external] == [
        (1, "42", 3), (1, "HOST-A", 4), (1, "LVS-A", 5), (1, "2024 AB", 6), (2, "2024 AB", 6)]
    assert (summary["gaia_matches"], summary["ned_matches"], summary["lvs_matches"]) == (1, 1, 1)
    assert summary["catalogs_failed"] == []
    # each copy's read is recorded: the chip's cone, the rows it held, and for Gaia the row groups
    gaia = summary["catalogs"]["gaia"]
    assert gaia["release"] == "gaia_test" and gaia["ref_epoch"] == 2016.0
    assert gaia["rows"] == 1 and gaia["reads"] and all(len(r) == 3 for r in gaia["reads"])
    assert abs(gaia["cone"]["ra"] - RA0) < 1e-3 and abs(gaia["cone"]["dec"] - DEC0) < 1e-3
    # the half-diagonal (7.8" for 100 px at 0.11"), the 1" radius and 1" slack, plus 11"/yr over
    # the 11.8 years from 2016.0
    assert 132.0 < gaia["cone"]["radius_arcsec"] < 147.0
    ned, lvs = summary["catalogs"]["ned"], summary["catalogs"]["lvs"]
    assert ned["release"] == "NED_TEST" and ned["rows"] == 1 and ned["n_reads"] >= 1      # TOO-FAR is outside the cone
    assert lvs["release"] == "LVS_TEST" and lvs["rows"] == 1 and lvs["n_reads"] == 1
    assert 7.0 + 10.0 < ned["cone"]["radius_arcsec"] < 20.0 and 7.0 + 30.0 < lvs["cone"]["radius_arcsec"] < 40.0


def test_a_catalog_that_cannot_be_opened_ships_its_field_null_and_is_recorded(tmp_path):
    out, summary, records = build(tmp_path, reuse_first_epoch=False, broken_catalog="ned")
    schema = load_schema()
    for record in records.values():
        assert fastavro.validation.validate(record, schema, raise_errors=True)
        assert record["nedMatches"] is None                       # did not run, not "nothing found"
    assert [m["sourceId"] for m in records[1]["gaiaMatches"]] == [42]
    assert [m["prefName"] for m in records[1]["lvsMatches"]] == ["LVS-A"]
    assert summary["catalogs_failed"] == ["ned"] and summary["ned_matches"] == 0
    assert summary["catalogs"]["ned"] == {"error": "RuntimeError: the copy could not be opened"}
    external = pq.read_table(out / "external.parquet").to_pylist()
    assert [e["catid"] for e in external] == ["42", "LVS-A", "2024 AB", "2024 AB"]


def test_index_rows_locate_their_records(tmp_path):
    out, summary, _ = build(tmp_path, reuse_first_epoch=False)
    index = pq.read_table(out / "packets.parquet").to_pylist()
    data = (out / summary["avro"]).read_bytes()
    with (out / summary["avro"]).open("rb") as stream:
        header_end = next(fastavro.block_reader(stream)).offset
    assert len(index) == 2
    for row in index:
        chunk = data[:header_end] + data[row["byte_offset"]:row["byte_offset"] + row["byte_length"]]
        (record,) = list(fastavro.reader(io.BytesIO(chunk)))
        assert record["diaSourceId"] == row["sid"]
        assert record["diaObject"]["diaObjectId"] == row["aid"]
        assert row["filename"] == out.as_uri() + "/" + summary["avro"]


def test_cutouts_are_full_size_with_off_chip_pixels_zero(tmp_path):
    _, _, records = build(tmp_path, reuse_first_epoch=False)
    for record in records.values():
        for name in ("cutoutDifference", "cutoutScience", "cutoutReference"):
            stamp = fits.getdata(io.BytesIO(record[name]))
            assert stamp.shape == (129, 129)
    # B sits at pixel (20, 80) of a 100-pixel image: the stamp's columns left
    # of the chip and rows above it are off-chip; the reference is 3.0 on-chip.
    reference = fits.getdata(io.BytesIO(records[2]["cutoutReference"]))
    assert np.all(reference[:, :64 - 19] == 0.0) and np.all(reference[64 + 21:, :] == 0.0)
    assert np.all(reference[64 - 19:64 + 21, 64 - 19:] == 3.0)


def test_forced_rows_a_predecessor_holds_are_reused_not_measured(tmp_path):
    out, summary, records = build(tmp_path, reuse_first_epoch=True)
    forced = {(r["aid"], r["rid"]): r for r in pq.read_table(out / "forced.parquet").to_pylist()}
    assert set(forced) == {(7, 201), (7, 202), (9, 201), (9, 202)}
    assert forced[(7, 201)]["psf_flux"] == 12345.0 and forced[(7, 201)]["time_proc"] == 61000.0
    assert summary["forced_reused"] == 1 and summary["forced_measured"] == 3
    assert (summary["packets"], summary["packets_snr_gt20"]) == (2, 2)  # both triggers at S/N 50
    # the measured epoch-2 flux of A is near its injected 500 (unit-sum PSF, unit noise)
    assert abs(forced[(7, 202)]["psf_flux"] - 500.0) < 50.0
    carried = {f["midpointMjd"]: f["psfFlux"] for f in records[1]["prvDiaForcedSources"]}
    # the record's psfFlux is an Avro float
    assert carried == {MJD[1]: 12345.0, MJD[2]: pytest.approx(forced[(7, 202)]["psf_flux"], rel=1e-6)}


def test_the_cap_keeps_the_highest_absolute_snr_triggers_in_sid_order():
    """dev's _cut_to_max_sources by |S/N|: a strong negative source outranks a weak positive one,
    a missing S/N ranks last, order by sid is kept, -1 keeps everything and 0 keeps none."""
    def trigger(sid, fluxfit, fluxerr):
        return Source(sid=sid, aid=sid, expid=2, sca=8, mjdobs=MJD[2], ra=RA0, dec=DEC0, xfit=1.0,
                      yfit=1.0, band="W146", xerr=0.1, yerr=0.1, fluxfit=fluxfit, fluxerr=fluxerr,
                      flags=0, field=1, pid=102, exptime=60.0)
    triggers = [trigger(1, 50.0, 10.0),      # S/N 5
                trigger(2, -900.0, 10.0),    # S/N -90: a negative source, the strongest
                trigger(3, 300.0, 10.0),     # S/N 30
                trigger(4, 100.0, 0.0)]      # no S/N
    kept, dropped = cap_triggers(triggers, 2)
    assert [s.sid for s in kept] == [2, 3] and dropped == 2
    kept, dropped = cap_triggers(triggers, 3)
    assert [s.sid for s in kept] == [1, 2, 3] and dropped == 1
    assert cap_triggers(triggers, -1) == (triggers, 0)
    assert cap_triggers(triggers, 4) == (triggers, 0)
    assert cap_triggers(triggers, 0) == ([], 4)


def test_an_image_without_a_science_psf_is_measured_on_its_difference_only(tmp_path):
    """dev 01f89299: no psfs row for the image's filter and SCA skips the science product; the
    difference is still measured and the row is kept, its science fluxes null."""
    write_epoch(tmp_path / "e1", [(50, 50, 400.0)])
    ra, dec = (float(v[0]) for v in wcs().all_pix2world([50.0], [50.0], 1))
    image = WindowImage(pid=101, rid=201, expid=1, sca=8, fid=8, band="W146", mjdobs=MJD[1],
                        corners=corners(), diff=tmp_path / "e1" / "zogy_diffimage_masked.fits",
                        diff_psf=tmp_path / "e1" / "diffpsf.fits",
                        diff_uncert=tmp_path / "e1" / "zogy_diffimage_uncert_masked.fits",
                        sci=tmp_path / "e1" / "bkg_subbed_science_image.fits", sci_psf=None)
    rows, measured = measure_forced([image], [fp.Position(7, ra, dec)], {}, 61000.0)
    (row,) = rows
    assert measured == 1 and row["rid"] == 201 and abs(row["psf_flux"] - 400.0) < 50.0
    assert row["science_flux"] is None and row["science_fluxerr"] is None
    assert row["x"] == pytest.approx(50.0, abs=1e-6) and row["flags"] == 0   # dev's one-based x


def test_a_gzipped_epoch_gives_the_packets_a_plain_one_gives(tmp_path):
    assert neighbour("diffpsf.fits", "s3://b/p/zogy_diffimage_masked.fits.gz") == "diffpsf.fits.gz"
    assert neighbour("diffpsf.fits", "s3://b/p/zogy_diffimage_masked.fits") == "diffpsf.fits"
    (tmp_path / "plain").mkdir(), (tmp_path / "gzipped").mkdir()
    _, _, plain = build(tmp_path / "plain", reuse_first_epoch=False)
    _, _, gzipped = build(tmp_path / "gzipped", reuse_first_epoch=False, gzipped=True)
    assert plain.keys() == gzipped.keys()
    for sid, record in plain.items():
        for name in ("cutoutDifference", "cutoutScience", "cutoutReference"):
            assert gzipped[sid][name] == record[name]
        assert [(f["psfFlux"], f["scienceFlux"]) for f in gzipped[sid]["prvDiaForcedSources"]] == [
            (f["psfFlux"], f["scienceFlux"]) for f in record["prvDiaForcedSources"]]


def test_a_file_url_epoch_is_left_as_published(tmp_path, monkeypatch):
    """A ``file://`` product is the published file itself: the set reads it and leaves it."""
    monkeypatch.setattr(prefix, "_cache", tmp_path / "cache")
    build(tmp_path, reuse_first_epoch=False, gzipped=True)
    for epoch in ("e1", "e2"):
        assert all(path.suffix == ".gz" for path in (tmp_path / epoch).iterdir())


def test_a_staged_download_is_decompressed_beside_itself_and_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(prefix, "_cache", tmp_path / "cache")
    staged = tmp_path / "cache" / "bucket" / "diffpsf.fits.gz"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(gzip.compress(b"plain"))
    assert _plain(staged).read_bytes() == b"plain"
    assert sorted(p.name for p in staged.parent.iterdir()) == ["diffpsf.fits"]


def test_each_gzipped_file_is_decompressed_once_per_set(tmp_path, monkeypatch):
    """Both readers and every role: the trigger's difference and science images feed forced and
    the cutouts, and epoch 1's PSF is also every epoch's science PSF (a repeated request)."""
    opened = []
    init = gzip.GzipFile.__init__

    def counting(self, filename=None, mode=None, *args, **kwargs):
        fileobj = kwargs.get("fileobj") or (args[1] if len(args) > 1 else None)
        if not (mode or getattr(fileobj, "mode", "rb")).startswith("w"):
            opened.append(str(filename or fileobj.name))
        init(self, filename, mode, *args, **kwargs)

    monkeypatch.setattr(gzip.GzipFile, "__init__", counting)
    build(tmp_path, reuse_first_epoch=False, gzipped=True)
    roles = ["zogy_diffimage_masked", "zogy_diffimage_uncert_masked", "diffpsf",
             "bkg_subbed_science_image"]
    read = {f"e{e}/{role}.fits.gz" for e in (1, 2) for role in roles} | {f"e2/{REFERENCE_STAMP}.gz"}
    assert sorted(str(tmp_path / name) for name in read) == sorted(opened)


def _image(job: int, ra0: float, dec0: float, half: float = 0.06) -> dict:
    """A window candidate: a square footprint ``2 half`` degrees on a side centred at (ra0, dec0)."""
    c = half / np.cos(np.radians(dec0))
    corners = [(ra0 - c, dec0 - half), (ra0 + c, dec0 - half), (ra0 + c, dec0 + half),
               (ra0 - c, dec0 + half)]
    row = {"job": job, "ra0": ra0, "dec0": dec0}
    for n, (ra, dec) in enumerate(corners, start=1):
        row[f"ra{n}"], row[f"dec{n}"] = ra, dec
    return row


def test_the_window_is_the_images_covering_an_object_whichever_field_they_are_centred_on():
    own = _image(1, RA0, DEC0)
    east = _image(2, RA0 + 0.1 / np.cos(np.radians(DEC0)), DEC0)   # overlaps own's east half
    far = _image(3, RA0 - 0.1 / np.cos(np.radians(DEC0)), DEC0)    # overlaps own's west half
    obj = (RA0 + 0.05 / np.cos(np.radians(DEC0)), DEC0)            # in own and east, not far
    assert [d["job"] for d in covering([own, east, far], [obj], own=1)] == [1, 2]
    assert [d["job"] for d in covering([own, east, far], [], own=1)] == [1]


def test_the_reach_admits_every_overlapping_image_of_the_detector():
    """The farthest overlap is corner on corner: centres just under two half-diagonals apart."""
    own = _image(1, RA0, DEC0)
    c = 0.95 * 0.12 / np.cos(np.radians(DEC0))
    corner_on_corner = _image(2, RA0 + c, DEC0 + 0.95 * 0.12)
    assert covering([corner_on_corner], [(RA0 + 0.058 / np.cos(np.radians(DEC0)), DEC0 + 0.058)], own=1)
    assert _separation(RA0, DEC0, corner_on_corner["ra0"], corner_on_corner["dec0"]) <= window_reach(own)


REFERENCE_STAMP = "awaicgen_output_mosaic_image_resampled_gainmatched.fits"


def test_an_unreadable_stamp_image_of_the_trigger_fails_the_set(tmp_path):
    with pytest.raises(ValueError, match=f"template image .*e2/{REFERENCE_STAMP}"):
        build(tmp_path, reuse_first_epoch=False, unreadable=f"e2/{REFERENCE_STAMP}")


def test_an_unreadable_reference_image_of_a_window_epoch_does_not(tmp_path):
    _, summary, _ = build(tmp_path, reuse_first_epoch=False, unreadable=f"e1/{REFERENCE_STAMP}")
    assert summary["packets"] == 2


def test_reliability_is_the_threshold_and_the_models_version_or_the_set_one():
    rubrat = {"model_prefix": "", "model_key": "rubrat/m-2026"}
    run = {"packet": {"rb_threshold": 0.5134, "rb_version": ""}, "difference": {"rubrat": rubrat}}
    assert reliability(run) == (0.5134, "m-2026")
    rubrat["model_prefix"] = "s3://elsewhere/models/m-other/"
    assert reliability(run) == (0.5134, "m-other")
    run["packet"]["rb_version"] = "named"
    assert reliability(run) == (0.5134, "named")


def _xsource_row(**columns) -> dict:
    """An xsource row as the packet stage reads it, fitted unless ``columns`` say otherwise."""
    return {"source_job": 11, "sid": 3, "x": 50.0, "y": 40.0, "fluxap1": 380.0, "fluxerrap1": 12.0,
            "fluxfit": 400.0, "snrfit": 40.0, "flagsfit": 4, "redchi": 1.25, **columns}


@pytest.mark.parametrize("unfitted", [{"fluxfit": None}, {"snrfit": None}, {"snrfit": 0.0},
                                      {"fluxfit": float("nan")}, {"snrfit": float("inf")}])
def test_an_unfitted_xsource_takes_the_3px_aperture_and_flags_0(unfitted):
    fields = xsource_fields(_xsource_row(**unfitted))
    assert (fields["fluxfit"], fields["fluxerr"], fields["flags"]) == (380.0, 12.0, 0)
    assert (fields["apflux"], fields["apfluxerr"]) == (380.0, 12.0)


def test_a_fitted_xsource_carries_its_fit_and_zero_based_pixels():
    fields = xsource_fields(_xsource_row())
    assert (fields["fluxfit"], fields["fluxerr"], fields["flags"]) == (400.0, 10.0, 4)
    assert (fields["apflux"], fields["apfluxerr"]) == (380.0, 12.0)
    assert (fields["xfit"], fields["yfit"], fields["redchi"]) == (49.0, 39.0, 1.25)
    source = Source.from_row({**fields, "expid": 1, "sca": 8, "mjdobs": MJD[1], "ra": RA0,
                              "dec": DEC0, "band": "F184"})
    assert source.snr == 40.0                      # [packet] max_alerts ranks by the fit's S/N


def test_alert_catalog_is_xsources_and_refuses_the_retired_sources_path():
    """A body without the key, stored before it, reads the xsources like every other; one naming
    the retired sources path is refused by name, never read against the dropped table."""
    assert alert_catalog({"packet": {}}) == "xsources"
    assert alert_catalog({"packet": {"alert_catalog": "xsources"}}) == "xsources"
    with pytest.raises(ValueError, match="retired with the sources table"):
        alert_catalog({"packet": {"alert_catalog": "sources"}})
    with pytest.raises(ValueError, match="alert_catalog"):
        alert_catalog({"packet": {"alert_catalog": "objects"}})


def test_the_base_settings_carry_no_alert_catalog_key():
    import tomllib
    from pathlib import Path

    base = Path(__file__).resolve().parents[2] / "rapidpipe" / "settings" / "foundation-thin.toml"
    body = tomllib.loads(base.read_text())
    assert "alert_catalog" not in body["packet"] and alert_catalog(body) == "xsources"


def test_the_alert_catalog_reads_the_xsources_tables():
    from rapidpipe.stages.packet import ALERT_CATALOG, MEMBERSHIPS_SQL, ORIGINS_SQL, TRIGGER_SQL

    x = ALERT_CATALOG
    assert "FROM xmemberships_of(" in x.sql(TRIGGER_SQL) and "xsid AS sid" in x.sql(TRIGGER_SQL)
    assert "(NULL::xobjects," in x.sql(ORIGINS_SQL)
    assert "(NULL::xmemberships," in x.sql(MEMBERSHIPS_SQL)
