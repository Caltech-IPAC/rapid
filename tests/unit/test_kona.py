"""Tests for rapidpipe.science.alerts.kona: the solar-system predictions, with a fake kete.

The real kete fetches orbits and kernels from the network and integrates
n-body; ``tests/support/fakekete.py`` stands in, so what these check is the
port's own work: the ephemeris read from an L2 header, the observer's
state, the day chosen, the day's files, and the predictions in a cone.
"""

from __future__ import annotations

import gzip
import json
import math
import os

import pytest
from astropy.io import fits

from rapidpipe.science.alerts import kona
from rapidpipe.stages import import_products
from tests.support import fakekete
from tests.support.catalogs import write_ss_store

#: An L2 primary header's ephemeris cards, as the intake writes the ASDF meta tree.
EPHEMERIS = {"meta exposure mid_time": "2027-10-07T12:00:27.360",
             "meta ephemeris time": 61685.5, "meta ephemeris ephemeris_reference_frame": "?",
             "meta ephemeris spatial_x": 1.0e8, "meta ephemeris spatial_y": -1.0e8,
             "meta ephemeris spatial_z": 5.0e7, "meta ephemeris velocity_x": 10.0,
             "meta ephemeris velocity_y": 20.0, "meta ephemeris velocity_z": -5.0}


@pytest.fixture(autouse=True)
def fake_kete(monkeypatch):
    monkeypatch.setattr(kona, "_kete", lambda: fakekete)


def l2_header(cards=EPHEMERIS) -> fits.Header:
    header = fits.Header()
    header["SIMPLE"] = True
    header["BITPIX"] = 8
    header["NAXIS"] = 0
    for key, value in cards.items():
        header[key] = value
    return header


def test_the_ephemeris_is_read_from_the_hierarch_cards_and_a_missing_one_is_named():
    eph = kona.ephemeris_from_header(l2_header())
    assert eph.mid_time == "2027-10-07T12:00:27.360" and eph.time == 61685.5 and eph.frame == "?"
    assert eph.position == (1.0e8, -1.0e8, 5.0e7) and eph.velocity == (10.0, 20.0, -5.0)
    short = {k: v for k, v in EPHEMERIS.items() if k != "meta ephemeris velocity_z"}
    with pytest.raises(ValueError, match="meta ephemeris velocity_z"):
        kona.ephemeris_from_header(l2_header(short))


def test_the_primary_header_is_read_from_a_plain_or_gzipped_file_without_the_data(tmp_path):
    """Only the header's blocks are read: a gzipped L2 streams up to its END card."""
    hdus = fits.HDUList([fits.PrimaryHDU(header=l2_header()),
                         fits.ImageHDU(data=[[1.0] * 100] * 100, name="SCI")])
    plain = tmp_path / "l2.fits"
    hdus.writeto(plain)
    (tmp_path / "l2.fits.gz").write_bytes(gzip.compress(plain.read_bytes()))
    for name in ("l2.fits", "l2.fits.gz"):
        header = kona.l2_primary_header((tmp_path / name).as_uri())
        assert header["meta ephemeris spatial_x"] == 1.0e8 and "SCI" not in repr(header)
    with pytest.raises(ValueError, match="neither"):
        kona.l2_primary_header("http://nowhere/l2.fits")


def test_the_observer_is_the_barycentric_state_in_au_slid_to_the_mid_time_and_recentred():
    """dev 0e7113b7: km to AU, km/s to AU/day, the position moved along the velocity from the
    ephemeris time to the mid-time, built on the barycentre and re-centred on the Sun."""
    eph = kona.ephemeris_from_header(l2_header())
    observer, image_time = kona.observer_state(eph)
    au = fakekete.AU_KM
    eph_jd = 2400000.5 + 61685.5
    dt = image_time.jd - eph_jd
    assert 0.0 < dt < 1.0 / 24.0                                    # 27 s after the ephemeris epoch
    vel = [v / au * 86400.0 for v in (10.0, 20.0, -5.0)]
    assert observer.vel.xyz == pytest.approx(vel)
    assert observer.pos.xyz == pytest.approx([p / au + v * dt for p, v in zip((1.0e8, -1.0e8, 5.0e7), vel)])
    assert observer.center_id == 10 and observer.pos.frame == fakekete.Frames.Equatorial
    ecliptic = kona.Ephemeris("2027-10-07T12:00:27.360Z", 61685.5, "Ecliptic", (1.0, 0.0, 0.0), (0.0, 0.0, 0.0))
    assert kona.observer_state(ecliptic)[0].pos.frame == fakekete.Frames.Ecliptic


def test_days_and_epochs():
    assert kona.day_of(61685.5) == "20271007" and kona.day_of(61685.0) == "20271007"
    assert kona.noon_jd("20271007") == pytest.approx(2400000.5 + 61685.5)
    days = ["20271001", "20271005", "20271007"]
    assert kona.newest_day(days, 61685.9) == "20271007"             # the day itself
    assert kona.newest_day(days, 61684.2) == "20271005"             # a gap: the newest before
    assert kona.newest_day(days, 61670.0) is None                  # before every day


def test_predict_gives_the_objects_in_the_cone_with_their_hg_magnitude():
    day_states = [fakekete.State(d, None, fakekete.Vector((1, 0, 0), ra=ra, dec=dec), fakekete.Vector((0, 0, 0)))
                  for d, ra, dec in (("2024 AB", 10.0, 5.0), ("433", 10.1, 5.05), ("far", 40.0, 5.0))]
    hg = {"2024 AB": (18.0, math.nan), "433": (math.nan, 0.2)}
    eph = kona.ephemeris_from_header(l2_header())
    found = kona.predict(day_states, hg, eph, 10.0, 5.0, radius_deg=0.15)
    assert found == {"2024 AB": (10.0, 5.0, 23.0), "433": (10.1, 5.05, None)}   # no H: no magnitude


def test_the_reader_lists_the_days_picks_one_by_date_and_loads_its_files(tmp_path):
    root = tmp_path / "ss"
    write_ss_store(root, "20271005", [("433", 10.0, 5.0, 11.0)])
    write_ss_store(root, "20271007", [("433", 10.2, 5.0, 11.0), ("2024 AB", 12.0, 5.0, 18.0)], epoch_jd=2461685.5)
    (root / "notaday").mkdir()
    (root / "notaday" / kona.SS_INFO).write_text("{}")
    reader = kona.SsReader(root.as_uri())
    assert reader.days == ["20271005", "20271007"] and reader.release == "rolling"
    assert reader.day_for(61685.9) == "20271007" and reader.day_for(61684.0) == "20271005"
    with pytest.raises(ValueError, match="no ss day at or before 20270901"):
        reader.day_for(61649.0)
    states, hg, record = reader.load("20271007")
    assert [s.desig for s in states] == ["433", "2024 AB"] and hg["2024 AB"][0] == 18.0
    assert math.isnan(hg["433"][1]) and record["epoch_jd"] == 2461685.5 and reader.n_reads == 1
    assert kona.read_days(str(root)) == ["20271005", "20271007"]
    with pytest.raises(ValueError, match="no day"):
        kona.SsReader((tmp_path / "empty").as_uri())


def test_ingest_day_writes_the_states_the_hg_table_and_the_record(tmp_path, monkeypatch):
    class Orbits:
        desig = ["433", "2024 AB", "-146 P1"]
        H = [11.0, float("nan"), float("nan")]
        G = [0.15, float("nan"), float("nan")]
        ra = [10.0, 12.0, 14.0]
        dec = [5.0, 5.0, 5.0]
        jd = [fakekete.EPOCH_JD, fakekete.EPOCH_JD, 1667909.5]   # an ancient comet: outside the kernels
    monkeypatch.setattr(fakekete.horizons, "fetch_known_orbit_data", staticmethod(lambda: Orbits()))
    with pytest.raises(ValueError, match="yyyymmdd"):
        kona.ingest_day(str(tmp_path / "ss"), "2027-10-07")
    record = kona.ingest_day(str(tmp_path / "ss"), "20271007", cache_dir=str(tmp_path / "kete"))
    assert record["n_objects"] == 2 and record["epoch_jd"] == kona.noon_jd("20271007") and record["complete"]
    assert record["n_dropped_epoch"] == 1
    assert json.loads((tmp_path / "ss" / "20271007" / kona.SS_INFO).read_text()) == record
    assert (tmp_path / "kete").is_dir()
    reader = kona.SsReader(str(tmp_path / "ss"))
    states, hg, _ = reader.load("20271007")
    assert [s.desig for s in states] == ["433", "2024 AB"] and hg["433"] == (11.0, 0.15)
    # a day already written is left as it is (the bucket seals a release create-once)
    Orbits.desig = ["433"]
    assert kona.ingest_day(str(tmp_path / "ss"), "20271007") == record
    assert [s.desig for s in reader.load("20271007")[0]] == ["433", "2024 AB"]


def test_the_orbit_table_converts_on_either_kete_line():
    """kete 2.0.0 moved mpc.table_to_states to conversion.table_to_states; the port takes whichever
    the installed kete has (requirements.txt pins 3.3.0 until IPAC's 1.x installs on 3.14)."""
    class Orbits:
        desig, ra, dec = ["433"], [10.0], [5.0]

    class OldKete:
        mpc = fakekete.mpc
    assert [s.desig for s in kona._table_to_states(fakekete, Orbits())] == ["433"]
    assert [s.desig for s in kona._table_to_states(OldKete, Orbits())] == ["433"]


def test_ingest_day_sets_the_cache_directory_before_importing_kete(tmp_path, monkeypatch):
    """kete fetches its core kernels at import, into KETE_CACHE_DIR: the ingest sets it first."""
    seen = {}

    def importer():
        seen["cache"] = os.environ.get("KETE_CACHE_DIR")
        return fakekete
    monkeypatch.setattr(kona, "_kete", importer)
    monkeypatch.setattr(fakekete.horizons, "fetch_known_orbit_data",
                        staticmethod(lambda: type("O", (), {"desig": [], "H": [], "G": [], "ra": [], "dec": []})()))
    kona.ingest_day(str(tmp_path / "ss"), "20271007", cache_dir=str(tmp_path / "kete"))
    assert seen["cache"] == str(tmp_path / "kete")


def test_a_days_files_are_conditional_creates_as_the_sealed_bucket_requires(tmp_path, monkeypatch):
    """The catalogs bucket denies an unconditional put once sealed: every file of a day goes up
    with If-None-Match: *, and a local store refuses to overwrite one."""
    from rapidpipe.science.catalogs.store import Store

    class Client:
        puts: list[dict] = []

        def put_object(self, **kwargs):
            self.puts.append(kwargs)
    monkeypatch.setattr(Store, "_bucket_region", lambda self: "us-east-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    store = Store("s3://bucket/ss")
    store._client = Client()
    store.put_bytes("20271007/ss.json", b"{}", create_once=True)
    store.put_bytes("plain", b"x")
    assert [p.get("IfNoneMatch") for p in Client.puts] == ["*", None]
    assert Client.puts[0]["Key"] == "ss/20271007/ss.json"
    local = Store(str(tmp_path))
    local.put_bytes("a", b"1", create_once=True)
    with pytest.raises(FileExistsError):
        local.put_bytes("a", b"2", create_once=True)
    # the ingest writes its three files that way
    calls = []
    monkeypatch.setattr(Store, "put_bytes", lambda self, rel, data, create_once=False: calls.append((rel, create_once)))
    monkeypatch.setattr(fakekete.horizons, "fetch_known_orbit_data",
                        staticmethod(lambda: type("O", (), {"desig": [], "H": [], "G": [], "ra": [], "dec": []})()))
    kona.ingest_day(str(tmp_path / "ss"), "20271008")
    assert calls == [(f"20271008/{kona.STATES_NAME}", True), (f"20271008/{kona.HG_NAME}", True),
                     (f"20271008/{kona.SS_INFO}", True)]


def test_the_import_stage_registers_an_ss_root_by_its_days_and_refuses_an_empty_one(tmp_path):
    root = tmp_path / "ss"
    write_ss_store(root, "20271005", [("433", 10.0, 5.0, 11.0)])
    write_ss_store(root, "20271007", [("433", 10.2, 5.0, 11.0)])
    location, record = import_products._mirror("ss", str(root))
    assert location == root.as_uri() + "/"
    assert record == {"release": "rolling", "days": 2, "oldest": "20271005", "newest": "20271007",
                      "complete": True}
    (tmp_path / "empty").mkdir()
    with pytest.raises(ValueError, match="not an ss root"):
        import_products._mirror("ss", str(tmp_path / "empty"))
