"""The reference stage under ``run(job)``, on three synthetic frames with the fake tools.

Contracts: a job's named ``l2files`` rows are coadded in ``mjdobs`` order
and the stage writes ``refimages``, ``refimimages`` and ``refimcatalogs``
with exactly their tables' columns from ``database/schema.sql``, all three
carrying the one ``rfid`` drawn for the job, the files under the attempt's
prefix and the header stamped with the job; frames past ``max_frames`` are
not coadded and are named in the manifest; too few frames, a frame in
another filter, an input outside the scope's filter, a refused
setting and a tool that writes nothing each fail the job before a manifest
is written, and a frame of another SCA is coadded; the mosaic catalog's ``MAG_ZEROPOINT`` is the
coadd's ``MAGZP``, the run's body unchanged; the row and the header carry the reference's grade; the run settings body's ``[reference]`` table is valid as it
ships. Under ``selection_type = "coverage"``: a reference whose estimate
clears the minimum is coadded with ``COVFRAC``/``COVDEPTH``/``COVTARG`` in
its header and the selected and skipped rids, estimate and measured fraction
in its manifest, a named frame that misses the reference skipped, the
frames in the loop's order (``mjdobs``, then distance from the reference's
centre, then key), not ``rid``'s; an estimate below the minimum less the
tolerance fails before awaicgen, and a measured fraction below the minimum
fails after it, each exit 65 with the fraction as the reason and no
manifest; ``"nframes"`` leaves the header without them; a body stored
before ``selection_type`` reads ``"nframes"``.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import re
import tomllib
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from astropy.io import fits
from astropy.wcs import WCS

import rapidpipe.stages.reference as reference
from rapidpipe.exitcodes import CommandExit
from rapidpipe.foundation.local import JobContext
from rapidpipe.science.reference import infobits
from tests.support import fakereftools
from tests.support.fakedifftools import cdf_dir
from tests.support.fakereftools import FRAMES, RTID, write_frames

REPO = Path(__file__).resolve().parents[2]
THIN = tomllib.loads((REPO / "rapidpipe/settings/foundation-thin.toml").read_text())
RFID = 4242
FID, SCA = 8, 1   # W146; the synthetic frames' SCA_NUM


def schema_columns(table: str) -> set[str]:
    body = re.search(rf"CREATE TABLE {table} \((.*?)\n\);", (REPO / "database/schema.sql").read_text(),
                     re.S).group(1)
    names = {line.split()[0] for line in body.splitlines()
             if line.strip() and not line.strip().startswith(("UNIQUE", "CHECK", "--"))}
    return names - {"job", "created"}


def settings(**tables) -> dict:
    body = copy.deepcopy(THIN["reference"])
    body["paths"]["cfg_path"] = str(cdf_dir())
    body["mosaic"].update(naxis1=128, naxis2=128)
    body["statistics"]["clip_correction_seed"] = 1
    for table, values in tables.items():
        body[table].update(values)
    return {"reference": body}


def fake_connect(rows: dict[int, tuple[int, str]], queries: list, corners: dict | None = None,
                 mjdobs: dict | None = None):
    """A connection answering the filter, each ``l2files`` row by source job, ``nextval`` and
    the named rows' corners by ``rid`` (``corners``), each with its corners' mean as its centre,
    its ``mjdobs`` (``mjdobs``, else its frame's) and its key."""
    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, statement, params=()):
            queries.append(statement)
            if "FROM filters" in statement:
                self.row = ("W146",) if params == (FID,) else None
            elif "rid = ANY" in statement:
                self.found = [(rid, *(v for pair in zip(*corners[rid]) for v in pair),
                               sum(corners[rid][0]) / 4, sum(corners[rid][1]) / 4,
                               (mjdobs or {}).get(rid, FRAMES[rid - 7000][4]), 100 + rid - 7000, SCA, FID)
                              for rid in params[0] if rid in (corners or {})]
            elif "FROM l2files" in statement:
                self.row = rows.get(params[0])
            else:
                assert "nextval" in statement
                self.row = (RFID,)

        def fetchone(self):
            return self.row

        def fetchall(self):
            return self.found

    class Connection:
        def cursor(self):
            return Cursor()

        def commit(self):
            pass

    return lambda: contextlib.nullcontext(Connection())


@pytest.fixture()
def frames(tmp_path) -> list[Path]:
    return write_frames(tmp_path / "l2")


@pytest.fixture(autouse=True)
def fakes(monkeypatch, tmp_path):
    monkeypatch.setattr(reference, "toolkit", fakereftools.fake_toolkit)
    monkeypatch.setenv("RAPIDPIPE_WORK", str(tmp_path / "work"))


def frame_corners(path: Path) -> tuple[list[float], list[float]]:
    """A frame's four corners from its own WCS, in order around it, as its ``l2files`` row carries them."""
    header = fits.getheader(path, 1)
    n1, n2 = header["NAXIS1"], header["NAXIS2"]
    sky = WCS(header).pixel_to_world([-0.5, n1 - 0.5, n1 - 0.5, -0.5], [-0.5, -0.5, n2 - 0.5, n2 - 0.5])
    return list(sky.ra.deg), list(sky.dec.deg)


def run_reference(tmp_path: Path, frames: list[Path], *, order=(2, 0, 1), body=None,
                  key_override=None, corners=None, mjdobs=None) -> Path:
    """Run a job naming ``frames`` (as source jobs 10, 11, ... in ``order``); return its prefix."""
    prefix = tmp_path / "prefix"
    prefix.mkdir(exist_ok=True)
    inputs, rows = [], {}
    for n, index in enumerate(order):
        source_job = 10 + n
        key = {"dataset": "D", "expid": 100 + index, "sca": SCA, "fid": FID, **(key_override or {})}
        inputs.append({"kind": "input", "product": "l2files", "key": key,
                       "source_job": source_job, "binding": None})
        rows[source_job] = (7000 + index, frames[index].as_uri())
    job = JobContext(job=5, run=3, stage="reference", scope={"field": RTID, "fid": FID, "sca": SCA},
                     survey="S", dataset="D", settings=body or settings(), inputs=inputs,
                     attempt=1, prefix=prefix.as_uri() + "/",
                     connect=fake_connect(rows, [], corners or {7000 + i: frame_corners(f) for i, f in enumerate(frames)},
                                          mjdobs))
    reference.run(job)
    return prefix


def read(prefix: Path, table: str) -> list[dict]:
    return pq.read_table(prefix / f"{table}.parquet").to_pylist()


def test_a_job_writes_its_three_tables_with_one_rfid_and_its_files(tmp_path, frames):
    prefix = run_reference(tmp_path, frames)
    manifest = json.loads((prefix / "manifest.json").read_text())
    assert [o["table"] for o in manifest["outputs"]] == ["refimages", "refimimages", "refimcatalogs"]
    for table in ("refimages", "refimimages", "refimcatalogs"):
        assert set(pq.read_schema(prefix / f"{table}.parquet").names) == (
            schema_columns(table) - {"rfcatid"})
    (ref,), (cat,) = read(prefix, "refimages"), read(prefix, "refimcatalogs")
    assert (ref["rfid"], ref["dataset"], ref["field"], ref["fid"], ref["nframes"]) == (
        RFID, "D", RTID, FID, 3)
    assert sorted(r["rid"] for r in read(prefix, "refimimages")) == [7000, 7001, 7002]
    assert {r["rfid"] for r in read(prefix, "refimimages")} == {cat["rfid"]} == {RFID}
    assert cat["cattype"] == reference.CATALOG_TYPE == 1
    image = Path(ref["filename"].removeprefix("file://"))
    assert ref["filename"].startswith(prefix.as_uri() + "/") and image.is_file()
    assert ref["checksum"] == hashlib.md5(image.read_bytes()).hexdigest()
    for name in ("awaicgen_output_mosaic_cov_map.fits", "awaicgen_output_mosaic_uncert_image.fits"):
        assert (image.parent / name).is_file()
    assert Path(cat["filename"].removeprefix("file://")).is_file()
    header = fits.getheader(image)
    assert (header["RPRUN"], header["RPJOB"], header["RPATTMPT"], header["RPSTAGE"]) == (
        3, 5, 1, "reference")
    assert [header[f"INFIL00{i}"] for i in (1, 2, 3)] == [f[1] for f in FRAMES]  # mjdobs order
    assert manifest["reference"]["rfid"] == RFID and manifest["reference"]["not_coadded"] == []
    assert manifest["reference"]["nsexcatsources"] == 4
    # The fake catalog's four sources are under t08 = 10: bit 8, fatal, status 0, on the row
    # and in the header alike.
    assert ref["infobits"] & 2**8 and ref["status"] == 0
    assert (header["INFOBITS"], header["STATUS"]) == (ref["infobits"], ref["status"])
    assert header["BIT08"] == 8 and "BIT09" not in header
    metrics = manifest["metrics"]
    assert {"gmin", "gmax", "coverage_lt1", "coverage_lt2", "coverage_lt3",
            "awaicgen_seconds"} <= set(metrics)
    assert all(isinstance(v, (int, float)) for v in metrics.values())
    assert not any((tmp_path / "work").iterdir())


def test_frames_past_max_frames_are_not_coadded_and_are_named(tmp_path, frames):
    prefix = run_reference(tmp_path, frames, body=settings(selection={"max_frames": 2}))
    assert sorted(r["rid"] for r in read(prefix, "refimimages")) == [7000, 7001]
    assert json.loads((prefix / "manifest.json").read_text())["reference"]["not_coadded"] == [7002]


@pytest.mark.parametrize("case", ["too few", "another filter"])
def test_bad_inputs_fail_before_a_manifest(tmp_path, frames, case):
    kwargs = {"too few": {"order": (0,)}}.get(case, {})
    if case == "another filter":
        with fits.open(frames[1]) as hdul:
            hdul = fits.HDUList([h.copy() for h in hdul])
        hdul[1].header["FILTER"] = "F184"
        hdul.writeto(frames[1], overwrite=True)
    with pytest.raises(ValueError):
        run_reference(tmp_path, frames, **kwargs)
    assert not (tmp_path / "prefix" / "manifest.json").exists()


def test_an_input_outside_the_scope_is_rejected_65_with_its_reason(tmp_path, frames):
    with pytest.raises(CommandExit, match="is not the scope's fid") as stopped:
        run_reference(tmp_path, frames, key_override={"fid": FID + 1})
    assert stopped.value.code == 65
    assert not (tmp_path / "prefix" / "manifest.json").exists()


def test_a_frame_of_another_sca_is_coadded(tmp_path, frames):
    prefix = run_reference(tmp_path, frames, key_override={"sca": SCA + 1})
    assert sorted(r["rid"] for r in read(prefix, "refimimages")) == [7000, 7001, 7002]


@pytest.mark.parametrize("tables", [
    {"psfcat": {"enabled": True}},
    {"fake_sources": {"inject_fake_sources_flag": True}},
    {"selection": {"min_frames": 5, "max_frames": 4}},
    {"mosaic": {"ra_center": 10.0}},
    {"mosaic": {"cdelt1": -0.0001}},
    {"awaicgen": {"awaicgen_output_mosaic_image_file": "mosaic.fits"}},
    {"awaicgen": {"use_nominal_zptmag": "true"}},
])
def test_refused_settings_fail_before_a_manifest(tmp_path, frames, tables):
    with pytest.raises(ValueError):
        run_reference(tmp_path, frames, body=settings(**tables))
    assert not (tmp_path / "prefix" / "manifest.json").exists()


def test_a_reference_records_that_its_inputs_were_scaled_by_their_own_zptmag(tmp_path, frames):
    prefix = run_reference(tmp_path, frames)
    (ref,) = read(prefix, "refimages")
    assert fits.getheader(Path(ref["filename"].removeprefix("file://")))["ZPSOURCE"] == "header"
    assert json.loads((prefix / "manifest.json").read_text())["reference"]["zptmag_source"] == "header"


def test_under_the_override_the_reference_reads_the_intake_table_and_grades_by_the_headers(
        tmp_path, frames):
    """Commissioning 8a4a172d: nominal scaling is recorded; zptmag_rms (bit 10) keeps the headers' spread."""
    body = {**settings(awaicgen={"use_nominal_zptmag": True}),
            "intake": {"nominal_zptmag": {"F146": 27.01}}}
    prefix = run_reference(tmp_path, frames, body=body)
    (ref,) = read(prefix, "refimages")
    manifest = json.loads((prefix / "manifest.json").read_text())
    assert fits.getheader(Path(ref["filename"].removeprefix("file://")))["ZPSOURCE"] == "nominal"
    assert manifest["reference"]["zptmag_source"] == "nominal"
    assert manifest["reference"]["quality"]["zptmag_rms"] == pytest.approx(
        infobits.compute_robust_spread([f[3] for f in FRAMES]))
    assert manifest["reference"]["quality"]["zptmag_rms"] > 0


def test_under_the_override_a_filter_missing_from_the_table_fails_the_job(tmp_path, frames):
    body = {**settings(awaicgen={"use_nominal_zptmag": True}),
            "intake": {"nominal_zptmag": {"F184": 25.34}}}
    with pytest.raises(KeyError, match="no nominal ZPTMAG"):
        run_reference(tmp_path, frames, body=body)
    assert not (tmp_path / "prefix" / "manifest.json").exists()


def test_awaicgen_writing_nothing_fails_the_job(tmp_path, frames, monkeypatch):
    monkeypatch.setattr(fakereftools.FakeReferenceToolRunner, "_awaicgen",
                        lambda self, args, cwd: None)
    with pytest.raises(RuntimeError, match="awaicgen"):
        run_reference(tmp_path, frames)
    assert not (tmp_path / "prefix" / "manifest.json").exists()


def test_the_mosaic_catalog_takes_the_coadds_zero_point_and_the_body_keeps_its_own(
        tmp_path, frames, monkeypatch):
    runner = fakereftools.FakeReferenceToolRunner()
    monkeypatch.setattr(reference, "toolkit", lambda: reference.Toolkit(runner=runner))
    body = settings()
    prefix = run_reference(tmp_path, frames, body=body)
    (ref,) = read(prefix, "refimages")
    magzp = fits.getheader(Path(ref["filename"].removeprefix("file://")))["MAGZP"]
    (sex,) = [a for a in runner.calls if a[0].endswith("sex")]
    assert float(sex[sex.index("-MAG_ZEROPOINT") + 1]) == magzp == pytest.approx(26.8630336165)
    assert body["reference"]["sextractor_refimage"]["sextractor_mag_zeropoint"] == "0.0"


def test_the_thin_settings_body_reference_table_is_valid():
    checked = reference.check_settings(THIN["reference"])
    assert (checked.min_frames, checked.naxis1) == (2, 9100)
    assert checked.infobits["fatalinfobits"] == [2, 5, 7, 8, 16]
    assert checked.use_nominal_zptmag is False
    stored = copy.deepcopy(THIN["reference"])
    del stored["awaicgen"]["use_nominal_zptmag"]
    assert reference.check_settings(stored).use_nominal_zptmag is False  # a body stored before the flag


def test_a_body_stored_before_the_coverage_selection_reads_nframes_and_needs_no_coverage_key():
    stored = copy.deepcopy(THIN["reference"])
    for key in ("selection_type", "min_coverage_depth", "min_coverage_fraction", "max_coverage_depth",
                "max_coverage_fraction", "coverage_estimate_tolerance"):
        del stored["selection"][key]
    checked = reference.check_settings(stored)
    assert (checked.selection_type, checked.min_coverage_fraction) == ("nframes", None)


def by_coverage(**selection) -> dict:
    return settings(selection={"selection_type": "coverage", **selection})


def test_a_reference_selected_by_coverage_records_its_coverage_in_its_header_and_manifest(tmp_path, frames):
    # The 96-pixel frames cover about 60% of the 128-pixel mosaic once.
    prefix = run_reference(tmp_path, frames, body=by_coverage(min_coverage_fraction=0.3))
    manifest = json.loads((prefix / "manifest.json").read_text())
    covered = manifest["reference"]["coverage"]
    assert covered["decision"] == "build"
    assert covered["selected"] == [7000, 7001, 7002] and covered["skipped"] == []
    assert 0.35 <= covered["estimate"] < 1.0 and 0.3 <= covered["measured"] < 1.0
    (ref,) = read(prefix, "refimages")
    assert set(ref) == schema_columns("refimages")
    header = fits.getheader(Path(ref["filename"].removeprefix("file://")))
    assert (header["COVFRAC"], header["COVDEPTH"], header["COVTARG"]) == (
        pytest.approx(covered["measured"]), 1, 0.3)
    assert "metrics" in manifest


def test_a_named_frame_that_misses_the_reference_is_skipped(tmp_path, frames):
    corners = {7000 + i: frame_corners(f) for i, f in enumerate(frames)}
    corners[7001] = ([c + 1.0 for c in corners[7001][0]], corners[7001][1])
    prefix = run_reference(tmp_path, frames, body=by_coverage(min_coverage_fraction=0.3), corners=corners)
    covered = json.loads((prefix / "manifest.json").read_text())["reference"]["coverage"]
    assert (covered["selected"], covered["skipped"]) == ([7000, 7002], [7001])
    assert sorted(r["rid"] for r in read(prefix, "refimimages")) == [7000, 7002]


def test_under_coverage_the_coadd_takes_the_loops_order_not_rids(tmp_path, frames, monkeypatch):
    """Frames tied on ``mjdobs`` coadd nearest the reference's centre first, as the loop chose them."""
    from rapidpipe.products.coverage import angular_distance
    from rapidpipe.science.spatial import field_center

    corners = {7000 + i: frame_corners(f) for i, f in enumerate(frames)}
    corners[7000] = ([c + 0.002 for c in corners[7000][0]], corners[7000][1])     # the farthest
    centre = field_center(RTID)
    distance = {rid: angular_distance(sum(ras) / 4, sum(decs) / 4, *centre) for rid, (ras, decs) in corners.items()}
    prefix = run_reference(tmp_path, frames, body=by_coverage(min_coverage_fraction=0.3), corners=corners,
                           mjdobs={rid: 61679.0 for rid in corners})
    covered = json.loads((prefix / "manifest.json").read_text())["reference"]["coverage"]
    assert covered["selected"] == sorted(corners, key=distance.get) and covered["selected"][-1] == 7000


@pytest.mark.parametrize("selection, step", [({"min_coverage_fraction": 0.95}, "estimated"),
                                             ({"min_coverage_fraction": 0.95, "coverage_estimate_tolerance": 0.5},
                                              "measured")])
def test_a_coverage_short_of_the_minimum_fails_the_job_with_exit_65_and_no_manifest(tmp_path, frames, monkeypatch,
                                                                                    selection, step):
    """Short by the estimate, before awaicgen; short by the coverage map, after it."""
    kit = fakereftools.fake_toolkit()
    monkeypatch.setattr(reference, "toolkit", lambda: kit)
    with pytest.raises(CommandExit) as raised:
        run_reference(tmp_path, frames, body=by_coverage(**selection))
    assert raised.value.code == 65 and str(raised.value).startswith(f"{step} coverage ")
    assert str(raised.value).endswith("< 0.95")
    assert not (tmp_path / "prefix" / "manifest.json").exists()
    assert (kit.runner.calls == []) == (step == "estimated")


def test_nframes_leaves_the_header_without_coverage(tmp_path, frames):
    prefix = run_reference(tmp_path, frames)
    (ref,) = read(prefix, "refimages")
    assert "COVFRAC" not in fits.getheader(Path(ref["filename"].removeprefix("file://")))
    assert "coverage" not in json.loads((prefix / "manifest.json").read_text())["reference"]


@pytest.mark.parametrize("selection", [{"selection_type": "first"}, {"min_coverage_fraction": 0.0},
                                       {"max_coverage_depth": 0.5}, {"coverage_estimate_tolerance": -0.1}])
def test_refused_coverage_settings_fail_before_a_manifest(tmp_path, frames, selection):
    with pytest.raises(ValueError):
        run_reference(tmp_path, frames, body=by_coverage(**selection))
    assert not (tmp_path / "prefix" / "manifest.json").exists()
