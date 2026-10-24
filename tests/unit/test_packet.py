"""The packet stage's assembly (``write_set``) on a synthetic object set and two small images.

Contracts: every record is valid 00.06; a member detected after the
trigger is not a previous detection and is not counted in nDiaSources;
each index row's offset and length hold exactly its record; the three
cutouts are (2 half width + 1) pixels square with off-chip pixels zero;
a forced row a predecessor set holds is carried, not measured again,
while a pair it lacks is measured; a difference image's neighbours are
read under its own suffix, so a gzipped epoch gives the packets a plain
one gives. An unreadable stamp image of the trigger's epoch fails the
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

from rapidpipe.science.alerts import forced as fp
from rapidpipe.science.alerts.assemble import load_schema
from rapidpipe.science.alerts.crossmatch import GAIA_COLUMNS, REFCAT_COLUMNS
from rapidpipe.science.alerts.records import Source
from rapidpipe.science.statistics.sums import Sums
from rapidpipe.stages.packet import (
    Read, WindowImage, _separation, covering, neighbour, window_reach, write_set)

N = 100                       # image side, pixels
RA0, DEC0 = 267.96, -28.72    # image centre
SETTINGS = {"stamp_half_width": 64, "prv_window_days": 365.25, "forced_window_days": 30.0,
            "gaia_radius_arcsec": 1.0, "gaia_max_matches": 3,
            "refcat_radius_arcsec": 5.0, "refcat_max_matches": 3}
SOURCE_JOB = {1: 11, 2: 12, 3: 13}    # epoch -> its source set's job
MJD = {1: 61685.0, 2: 61686.0, 3: 61687.0}


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


def build(tmp_path, reuse_first_epoch: bool, gzipped: bool = False, unreadable: str | None = None):
    """Two objects: A with members in epochs 1, 2 and 3; B (near the edge) only in epoch 2.

    The packet set is epoch 2's; epoch 3's detection of A is after the trigger.
    ``unreadable`` names a file under the job directories (``e1/...``) overwritten with bytes
    that are not FITS.
    """
    write_epoch(tmp_path / "e1", [(50, 50, 400.0)], gzipped)
    write_epoch(tmp_path / "e2", [(50, 50, 500.0), (20, 80, 300.0)], gzipped)
    if unreadable:
        (tmp_path / unreadable).write_bytes(b"not a FITS file")
    diff = "zogy_diffimage_masked.fits" + (".gz" if gzipped else "")

    def beside(e, name):
        return tmp_path / f"e{e}" / neighbour(name, diff)

    sci_psf = beside(1, "diffpsf.fits")
    images = [WindowImage(pid=100 + e, rid=200 + e, expid=e, sca=8, fid=8, band="W146",
                          mjdobs=MJD[e], corners=corners(), diff=tmp_path / f"e{e}" / diff,
                          diff_psf=beside(e, "diffpsf.fits"),
                          diff_uncert=beside(e, "zogy_diffimage_uncert_masked.fits"),
                          sci=beside(e, "bkg_subbed_science_image.fits"),
                          sci_psf=sci_psf) for e in (1, 2)]
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
    gaia = {name: [0] for name in GAIA_COLUMNS}
    gaia.update(source_id=[42], ra=[float(gaia_ra[0])], dec=[float(gaia_dec[0])],
                pmra=[0.0], pmdec=[0.0], phot_variable_flag=["NOT_AVAILABLE"],
                ref_epoch=[2016.0])
    for name in ("duplicated_source", "in_qso_candidates", "in_galaxy_candidates"):
        gaia[name] = [False]
    pq.write_table(pa.table(gaia), tmp_path / "gaia.parquet")
    read = Read(key={"dataset": "D", "expid": 2, "sca": 8, "fid": 8},
                source_job=SOURCE_JOB[2], object_job={7: 5, 9: 5}, flags_zero=2,
                triggers=[a2, b2], aid_of={1: 7, 2: 9}, objects=objects, members=members,
                images=images, trigger_image=images[1], reuse=reuse,
                gaia_job=3, gaia_path=tmp_path / "gaia.parquet", refcat_path=None)
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
    assert [m["sourceId"] for m in a["gaiaMatches"]] == [42] and records[2]["gaiaMatches"] == []
    assert a["refStarMatches"] is None and a["nedMatches"] is None and a["lvsMatches"] is None
    external = pq.read_table(out / "external.parquet").to_pylist()
    assert [(e["sid"], e["catid"], e["catalog_job"]) for e in external] == [(1, "42", 3)]


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
