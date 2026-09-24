"""Tests for rapidpipe.science.alerts: the schema, stamps, container byte ranges, assembly.

No database, no network: the pure functions ported from `dev`'s
``alerts/produce.py`` and ``alerts/providers.py``.
"""

from __future__ import annotations

import io
import json

import fastavro
import numpy as np
import pytest
from astropy.io import fits

from rapidpipe.science.alerts import assemble, crossmatch, cutouts
from rapidpipe.science.alerts.param_registry import ALERT_PARAMS, NOT_USED, VERSION
from rapidpipe.science.alerts.records import Cutouts, ObjectRecord, Source


def _source(sid, **overrides):
    row = {"sid": sid, "expid": 1234, "sca": 7, "mjdobs": 61273.125, "ra": 269.45,
           "dec": -28.77, "xfit": 10.0, "yfit": 12.0, "band": "F184", "xerr": 0.01,
           "yerr": 0.02, "fluxfit": 100.0, "fluxerr": 2.0, "flags": 0, "field": 5321,
           "hp6": 1, "hp9": 2, "pid": 4242, "isdiffpos": True, "qfit": 0.1, "cfit": 0.1,
           "redchi": 1.0, "npixfit": 25, "sharpness": 0.5, "roundness1": 0.0,
           "roundness2": 0.0, "peak": 12.0, "exptime": 139.8}
    row.update(overrides)
    return Source.from_row(row, strict=True)


def _object_row(object_id, sid, **overrides):
    row = {"sid": sid, "merges_aid": object_id, "aid": object_id, "ra0": 269.45, "dec0": -28.77,
           "stdevra": None, "stdevdec": None, "nsources": 1}
    row.update(overrides)
    return row


def test_packaged_schema_matches_the_registry():
    assert assemble.schema_problems() == []
    assert assemble.SCHEMA_VERSION == VERSION == "00.04"
    schema = assemble.load_schema()
    assert schema["name"] == "rapid.v00_04.alert"


def test_schema_problems_names_a_drifted_file(tmp_path):
    root = tmp_path / "schema"
    for path in assemble.schema_paths():
        target = root / path.relative_to(assemble.SCHEMA_ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(path.read_text())
    alert = root / "00" / "04" / "rapid.v00_04.alert.avsc"
    data = json.loads(alert.read_text())
    data["fields"] = data["fields"][:-1]
    alert.write_text(json.dumps(data))
    assert any("alert.avsc" in p for p in assemble.schema_problems(root))
    with pytest.raises(RuntimeError, match="out of sync"):
        assemble.load_schema(schema_root=root)


def test_extract_stamp_is_devs_geometry_with_shifted_crpix():
    image = np.arange(200 * 200, dtype=np.float32).reshape(200, 200)
    header = fits.Header()
    header["CTYPE1"], header["CTYPE2"] = "RA---TAN", "DEC--TAN"
    header["CRPIX1"], header["CRPIX2"] = 100.5, 100.5
    header["CRVAL1"], header["CRVAL2"] = 269.45, -28.77
    header["OBJECT"] = "not copied"
    stamp = cutouts.extract_stamp(image, 101.0, 101.0, header=header)
    with fits.open(io.BytesIO(stamp)) as hdus:
        data, out = hdus[0].data, hdus[0].header
    assert data.shape == (129, 129) and data.dtype == np.dtype(">f4")
    # 1-based (101, 101) is numpy [100, 100], the stamp's centre
    assert data[64, 64] == image[100, 100]
    assert out["CRPIX1"] == pytest.approx(100.5 - (100 - 64))
    assert out["CRPIX2"] == pytest.approx(100.5 - (100 - 64))
    assert "OBJECT" not in out


def test_extract_stamp_fills_off_chip_pixels_and_refuses_no_overlap():
    image = np.ones((50, 50), dtype=np.float32)
    stamp = cutouts.extract_stamp(image, 1.0, 1.0)
    with fits.open(io.BytesIO(stamp)) as hdus:
        data = hdus[0].data
    assert data.shape == (129, 129)
    assert data[0, 0] == cutouts.STAMP_FILL_VALUE and data[64, 64] == 1.0
    assert cutouts.extract_stamp(image, 500.0, 500.0) is None
    assert cutouts.extract_stamp(None, 1.0, 1.0) is None


def test_load_fits_image_reads_the_first_image_hdu(tmp_path):
    path = tmp_path / "x.fits"
    fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(np.zeros((3, 4), dtype=np.float32))]).writeto(path)
    pixels, header = cutouts.load_fits_image(path)
    assert pixels.shape == (3, 4) and header is not None
    assert cutouts.load_fits_image(tmp_path / "missing.fits") == (None, None)


def _alert(sid=1, *, prv=(), cutout=None):
    source = _source(sid)
    obj = ObjectRecord.from_row(_object_row(900, sid), strict=True)
    ss = crossmatch.associate_ss(source, None)
    return assemble.assemble_alert(source, obj, list(prv), ss_matches=ss, ref_matches=None,
                                   ned_matches=None, cutouts=Cutouts(difference=cutout),
                                   time_proc=61274.0)


def test_assemble_alert_is_devs_packet():
    earlier = _source(2, mjdobs=61200.0, aid=900)
    alert = _alert(prv=[earlier], cutout=b"FITS")
    assert set(alert) == {p.name for p in ALERT_PARAMS if p.status is not NOT_USED}
    assert alert["schemaVersion"] == "00.04"
    assert alert["diaSource"]["diaObjectId"] == 900
    assert alert["diaSource"]["isNegative"] is False
    assert alert["diaSource"]["isSSCandidate"] is None
    assert alert["diaObject"]["firstDiaSourceMjd"] == 61200.0
    assert alert["diaObject"]["validityStartMjd"] == 61273.125
    assert len(alert["prvDiaSources"]) == 1
    assert alert["ssMatches"] is None and alert["refStarMatches"] is None
    decoded = fastavro.schemaless_reader(
        io.BytesIO(assemble.serialize_alert(alert, assemble.load_schema())),
        assemble.load_schema())
    assert decoded["diaSourceId"] == 1 and decoded["cutoutDifference"] == b"FITS"


def test_container_byte_ranges_decode_one_record_each():
    buf = io.BytesIO()
    container = assemble.AlertContainer(buf, assemble.load_schema())
    ranges = [container.write(_alert(sid)) for sid in (1, 2, 3)]
    raw = buf.getvalue()
    reader = fastavro.reader(io.BytesIO(raw))
    assert reader.codec == "deflate"
    assert [r["diaSourceId"] for r in reader] == [1, 2, 3]
    header_end = ranges[0][0]
    for sid, (offset, length) in zip((1, 2, 3), ranges):
        one = list(fastavro.reader(io.BytesIO(raw[:header_end] + raw[offset:offset + length])))
        assert [r["diaSourceId"] for r in one] == [sid]


def test_an_empty_container_is_a_readable_header():
    buf = io.BytesIO()
    assemble.AlertContainer(buf, assemble.load_schema())
    assert list(fastavro.reader(io.BytesIO(buf.getvalue()))) == []


def test_index_associations_keeps_the_lowest_aid_and_the_orphans():
    rows = [_object_row(5, 1), _object_row(7, 1), _object_row(8, 2, aid=None)]
    history = [{**{c: getattr(_source(3, mjdobs=61000.0), c) for c in
                   ("sid", "expid", "sca", "mjdobs", "ra", "dec", "xfit", "yfit", "band", "xerr",
                    "yerr", "fluxfit", "fluxerr", "flags", "field", "hp6", "hp9", "pid",
                    "isdiffpos", "qfit", "cfit", "redchi", "npixfit", "sharpness",
                    "roundness1", "roundness2", "peak", "exptime")}, "object_aid": 5}]
    indexed = assemble.index_associations(rows, history)
    assert indexed.objects_by_sid[1]["aid"] == 5
    assert indexed.orphans_by_sid == {2: [8]}
    assert [s.sid for s in indexed.history_by_aid[5]] == [3]


def test_batch_produce_drops_orphans_and_unassociated_with_reasons():
    sources = [_source(1), _source(2), _source(3)]
    indexed = assemble.index_associations(
        [_object_row(5, 1), _object_row(8, 2, aid=None)], [])
    stats = assemble.BatchStats(pid=4242)
    stats.record_flagged(9, 4)
    buf = io.BytesIO()
    schema = assemble.load_schema()
    written = assemble.batch_produce(
        sources, indexed, container=assemble.AlertContainer(buf, schema), stats=stats,
        schema=schema, window_days=365.25,
        difference_image=(np.zeros((50, 50), dtype=np.float32), None))
    assert [w.sid for w in written] == [1]
    assert written[0].record_index == 0 and written[0].aid == 5
    assert [(d["sid"], d["reason"]) for d in stats.dropped] == [
        (9, "flagged"), (2, "orphan"), (3, "unassociated")]
    assert stats.dropped_count == 3 and stats.n_failed == 2 and stats.n_flagged == 1
    assert stats.cutouts_present["cutoutDifference"] == 1


def test_match_ss_predictions_and_associate_ss():
    source = _source(1)
    predictions = {"2026 AB": (269.45, -28.77 + 0.1 / 3600, 18.0),
                   "far": (270.0, -28.0, None)}
    matches = crossmatch.associate_ss(source, predictions)
    assert [m.designation for m in matches] == ["2026 AB"]
    assert source.is_ss_candidate is True
    assert crossmatch.associate_ss(source, None) is None and source.is_ss_candidate is None


def test_chip_matches_are_not_run_without_a_catalog_or_reader():
    sources = [_source(1)]
    assert crossmatch.chip_ref_matches(sources, None) == {}
    assert crossmatch.chip_ned_matches(sources, None) == {}


def test_chip_ned_matches_with_a_stub_reader():
    sources = [_source(1)]

    def reader(ra, dec, radius):
        return {"prefname": np.array(["NGC 1"], dtype=object), "ra": np.array([269.45]),
                "dec": np.array([-28.77 + 2.0 / 3600])}

    matches = crossmatch.chip_ned_matches(sources, reader)
    assert [m.prefname for m in matches[1]] == ["NGC 1"]

    def broken(ra, dec, radius):
        raise OSError("NED down")

    assert crossmatch.chip_ned_matches(sources, broken) == {}
