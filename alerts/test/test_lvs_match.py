"""NED-LVS cross-match tests.

Mirrors test_ned_match.py for the second NED product:
  - build_lvscat() / match_lvscat(): pure functions over in-memory LVS
    slices (conftest.make_lvs_table). Every LVS column must reach the
    LvsMatch record under its own name, nulls must become None, the two
    quality flags must stay bool.
  - End-to-end over the fake chip (conftest FakeDB) with an injected
    fake_ned_reader over an LVS table: the three lvsMatches states, the
    Avro round trip, and the join to nedMatches by prefName.
  - LvsReader (alerts/ned_reader.py), the production reader over the
    parquet that alerts.ned_catalog ingest-lvs writes, against a synthetic
    table in tmp_path: cone reads, "" -> None, one load per process, and
    the missing-table refusal.

The geometry (sep/pa, nearest-N, the per-chip cone) is NED's, tested in
test_ned_match.py, and is not repeated here.
"""

import io
import json
import math

import fastavro
import healpy as hp
import numpy as np
import pyarrow as pa
import pytest

from alerts import ned_catalog as nc
from alerts import ned_reader as nr
from alerts.produce import assemble_alert, load_schema, serialize_alert
from alerts.providers import (LVS_BOOL_COLUMNS, LVS_COLUMNS, LVS_MATCH_NMAX,
                              LVS_MATCH_RADIUS_ARCSEC, LVS_NUMERIC_COLUMNS,
                              LVS_STRING_COLUMNS, build_lvscat, match_lvscat)
from conftest import fake_ned_reader, make_lvs_table, make_ned_table


def ra_offset(dec, sep_arcsec):
    """Degrees of RA giving `sep_arcsec` of separation at declination dec."""
    return sep_arcsec / 3600.0 / math.cos(math.radians(dec))


# one galaxy with every LVS column set, for "does it all get through" tests
FULL_GALAXY = {
    "objname": "NGC 0001", "objtype": "G", "z_tech": "SPEC",
    "DistMpc_method": "zIndependent", "z_qual": False, "Diam_qual": True,
    "z": 0.0151, "z_unc": 0.0001,
    "DistMpc": 62.0, "DistMpc_unc": 4.0, "Diam": 95.5, "Diam_ba": 0.72,
    "Diam_pa": 112.0, "ebv": 0.035, "m_Ks": 9.83, "m_Ks_unc": 0.03,
    "m_W1": 10.41, "m_W1_unc": 0.02, "m_NUV": 15.9, "m_NUV_unc": 0.04,
    "SFR_hybrid": 2.4, "SFR_hybrid_unc": 0.5, "Mstar": 7.9e10, "Mstar_unc": 1.2e10,
}


# ---------------------------------------------------------------------------
# Building and matching
# ---------------------------------------------------------------------------

def test_build_lvscat_fills_optional_columns_and_requires_positions():
    table = make_lvs_table([{"ra": 150.0, "dec": -20.0}])
    del table["m_Ks"], table["z_tech"], table["Diam_qual"]     # a backend without them
    cat = build_lvscat(table)
    assert cat is not None and len(cat.coords) == 1
    assert np.isnan(cat.columns["m_Ks"]).all()
    assert list(cat.columns["z_tech"]) == [None]
    assert list(cat.columns["Diam_qual"]) == [False]

    del table["ra"]
    assert build_lvscat(table) is None                         # -> "not run"


def test_build_lvscat_empty_gives_none_coords():
    cat = build_lvscat(make_lvs_table([]))
    assert cat is not None and cat.coords is None
    assert match_lvscat(150.0, -20.0, cat) == [[]]             # [] not None


def test_every_lvs_column_reaches_the_match():
    """LvsMatch attributes are the column names (objname -> prefname); every
    one must carry the slice's value through, with the right type."""
    ra0, dec0 = 150.0, -20.0
    cat = build_lvscat(make_lvs_table([{**FULL_GALAXY, "ra": ra0, "dec": dec0}]))
    (match,), = [match_lvscat(ra0, dec0, cat)[0]]
    assert match.prefname == "NGC 0001"
    for name in LVS_STRING_COLUMNS:
        if name == "objname":
            continue
        assert getattr(match, name) == FULL_GALAXY[name]
    for name in LVS_BOOL_COLUMNS:
        assert getattr(match, name) is FULL_GALAXY[name]
    for name in LVS_NUMERIC_COLUMNS:
        if name in ("ra", "dec"):
            continue
        assert getattr(match, name) == pytest.approx(FULL_GALAXY[name])
    assert set(LVS_COLUMNS) == (set(LVS_STRING_COLUMNS) | set(LVS_BOOL_COLUMNS)
                                | set(LVS_NUMERIC_COLUMNS))


def test_absent_values_become_none_and_flags_stay_bool():
    ra0, dec0 = 150.0, -20.0
    cat = build_lvscat(make_lvs_table([
        {"ra": ra0, "dec": dec0, "z_tech": "", "z_qual": True},     # "" -> None
    ]))
    (match,) = match_lvscat(ra0, dec0, cat)[0]
    assert match.z is None and match.Diam is None and match.Mstar is None
    assert match.z_tech is None and match.DistMpc_method is None
    assert match.z_qual is True and match.Diam_qual is False
    assert isinstance(match.z_qual, bool)


def test_ordering_radius_and_cap():
    """Nearest 3 within LVS_MATCH_RADIUS_ARCSEC (30", wider than NED's 10")."""
    ra0, dec0 = 150.0, -20.0
    off = lambda s: ra_offset(dec0, s)
    cat = build_lvscat(make_lvs_table([
        {"ra": ra0 + off(20.0), "dec": dec0, "objname": "AT-20"},
        {"ra": ra0 + off(5.0), "dec": dec0, "objname": "AT-5"},
        {"ra": ra0 + off(28.0), "dec": dec0, "objname": "AT-28"},
        {"ra": ra0 + off(10.0), "dec": dec0, "objname": "AT-10"},
        {"ra": ra0 + off(LVS_MATCH_RADIUS_ARCSEC + 5.0), "dec": dec0, "objname": "OUT"},
    ]))
    (matches,) = match_lvscat(ra0, dec0, cat)
    assert [m.prefname for m in matches] == ["AT-5", "AT-10", "AT-20"]
    assert len(matches) == LVS_MATCH_NMAX
    assert all(m.sep <= LVS_MATCH_RADIUS_ARCSEC for m in matches)
    assert matches[0].sep == pytest.approx(5.0, abs=0.01)
    assert matches[0].pa == pytest.approx(90.0, abs=0.1)         # due East
    assert LVS_MATCH_RADIUS_ARCSEC > 10.0


def test_batch_equals_single():
    rng = np.random.default_rng(23)
    dec0 = -20.0
    entries = [{"ra": 150.0 + ra_offset(dec0, rng.uniform(-40, 40)),
                "dec": dec0 + rng.uniform(-40, 40) / 3600.0} for _ in range(30)]
    cat = build_lvscat(make_lvs_table(entries))
    src_ra = [150.0, 150.003, 149.997]
    src_dec = [dec0, dec0 + 2e-3, dec0 - 2e-3]
    batch = match_lvscat(src_ra, src_dec, cat)
    for i, (ra, dec) in enumerate(zip(src_ra, src_dec)):
        (single,) = match_lvscat(ra, dec, cat)
        assert [m.prefname for m in batch[i]] == [m.prefname for m in single]
        assert [m.sep for m in batch[i]] == pytest.approx([m.sep for m in single], abs=1e-9)


# ---------------------------------------------------------------------------
# End to end over the fake chip (real provider, injected reader)
# ---------------------------------------------------------------------------

@pytest.fixture()
def trigger(make_provider):
    """The fake chip's sid 9001 sky position."""
    detection = make_provider().get_detection(9001)
    return detection.ra, detection.dec


@pytest.fixture()
def chip_lvs_table(trigger):
    """An LVS slice around sid 9001: a full galaxy at 3", a sparse one at
    12", one just outside the radius, and a far one."""
    ra, dec = trigger
    off = lambda s: ra_offset(dec, s)
    return make_lvs_table([
        {**FULL_GALAXY, "ra": ra + off(3.0), "dec": dec},
        {"ra": ra + off(12.0), "dec": dec, "objname": "SPARSE", "z_qual": True},
        {"ra": ra + off(LVS_MATCH_RADIUS_ARCSEC + 2.0), "dec": dec, "objname": "EDGE"},
        {"ra": ra + 1.0, "dec": dec, "objname": "FAR"},
    ])


def test_e2e_not_run_without_reader(make_provider):
    alert = assemble_alert(make_provider(), 9001)
    assert alert["lvsMatches"] is None


def test_e2e_unavailable_slice_is_null_not_empty(make_provider, chip_lvs_table, caplog):
    reader = fake_ned_reader(chip_lvs_table, coverage=False)
    with caplog.at_level("WARNING", logger="alerts.providers"):
        alert = assemble_alert(make_provider(lvs_reader=reader), 9001)
    assert alert["lvsMatches"] is None
    assert "NED-LVS matching not run" in caplog.text


def test_e2e_ran_clean_is_empty_not_null(make_provider, trigger):
    ra, dec = trigger
    table = make_lvs_table([{"ra": ra + 1.0, "dec": dec, "objname": "FAR"}])
    alert = assemble_alert(make_provider(lvs_reader=fake_ned_reader(table)), 9001)
    assert alert["lvsMatches"] == []


def test_e2e_matched_and_serialized(make_provider, chip_lvs_table):
    alert = assemble_alert(make_provider(lvs_reader=fake_ned_reader(chip_lvs_table)), 9001)

    assert [m["prefName"] for m in alert["lvsMatches"]] == ["NGC 0001", "SPARSE"]
    first, sparse = alert["lvsMatches"]
    assert first["sep"] == pytest.approx(3.0, abs=0.01)
    assert first["distMpc"] == pytest.approx(62.0)
    assert first["distMethod"] == "zIndependent"
    assert first["diam"] == pytest.approx(95.5) and first["diamQual"] is True
    assert first["magKs"] == pytest.approx(9.83) and first["magNUV"] == pytest.approx(15.9)
    assert first["sfr"] == pytest.approx(2.4) and first["mStar"] == pytest.approx(7.9e10)
    assert sparse["zQual"] is True and sparse["diamQual"] is False
    assert sparse["z"] is None and sparse["diam"] is None and sparse["objType"] == "G"

    schema = load_schema()
    decoded = fastavro.schemaless_reader(
        io.BytesIO(serialize_alert(alert, schema=schema)), schema)
    assert [m["prefName"] for m in decoded["lvsMatches"]] == ["NGC 0001", "SPARSE"]
    assert decoded["lvsMatches"][0]["mStar"] == pytest.approx(7.9e10)
    assert decoded["lvsMatches"][1]["distMpc"] is None
    assert decoded["lvsMatches"][1]["zQual"] is True


def test_e2e_joins_to_ned_matches_by_prefname(make_provider, trigger, monkeypatch):
    """The same galaxy seen by both readers carries the same prefName in
    both arrays -- the join key a consumer uses to attach LVS's distance
    and size to a NED match."""
    from alerts import providers
    monkeypatch.setattr(providers, "NED_SELECTION_ENABLED", False)
    ra, dec = trigger
    pos = {"ra": ra + ra_offset(dec, 2.0), "dec": dec}
    ned = fake_ned_reader(make_ned_table([{**pos, "prefname": "NGC 0001", "ptype": "G"}]))
    lvs = fake_ned_reader(make_lvs_table([{**FULL_GALAXY, **pos}]))
    alert = assemble_alert(make_provider(ned_reader=ned, lvs_reader=lvs), 9001)
    assert alert["nedMatches"][0]["prefName"] == "NGC 0001"
    assert alert["lvsMatches"][0]["prefName"] == alert["nedMatches"][0]["prefName"]
    assert alert["lvsMatches"][0]["sep"] == pytest.approx(alert["nedMatches"][0]["sep"], abs=1e-6)


def test_e2e_batch_flow_matches_all_sources(make_provider, chip_lvs_table):
    log = []
    provider = make_provider(lvs_reader=fake_ned_reader(chip_lvs_table, log=log))
    sources = list(provider.iter_sources(99))
    assert len(sources) == 3 and len(log) == 1            # one slice per chip
    fresh = make_provider(lvs_reader=fake_ned_reader(chip_lvs_table))
    for source in sources:
        batch = provider.get_lvs_matches(source)
        single = fresh.get_lvs_matches(fresh.get_detection(source.sid))
        assert batch is not None and single is not None
        assert [m.prefname for m in batch] == [m.prefname for m in single]


# ---------------------------------------------------------------------------
# LvsReader (alerts/ned_reader.py) over a synthetic lvs/nedlvs.parquet
# ---------------------------------------------------------------------------

def write_lvs_store(root, entries, release="LVS_TEST"):
    """Write the parquet + info that alerts.ned_catalog ingest-lvs would,
    from make_lvs_table entries (plus the `_hp6` column the reader uses)."""
    table = make_lvs_table(entries)
    hp6 = hp.ang2pix(2 ** nc.HP6_ORDER, table["ra"], table["dec"], nest=True, lonlat=True)
    arrow = pa.table({**{k: pa.array(v) for k, v in table.items()},
                      nc.LVS_HP6_COLUMN: pa.array(hp6.astype("int32"))})
    store = nc.Store(str(root))
    store.write_table(arrow, nc.LVS_PARQUET)
    store.write_text(nc.LVS_INFO, json.dumps({"release": release, "rows": len(entries)}))


def test_lvs_reader_cone_and_contract(tmp_path):
    ra0, dec0 = 150.0, -20.0
    write_lvs_store(tmp_path, [
        {**FULL_GALAXY, "ra": ra0, "dec": dec0},
        {"ra": ra0 + ra_offset(dec0, 25.0), "dec": dec0, "objname": "AT-25",
         "z_tech": "", "z_qual": True},
        {"ra": ra0 + ra_offset(dec0, 45.0), "dec": dec0, "objname": "AT-45"},
        {"ra": ra0 + 1.0, "dec": dec0, "objname": "FAR"},
    ])
    reader = nr.LvsReader(str(tmp_path))
    assert reader.release == "LVS_TEST" and reader.n_reads == 0    # lazy

    got = reader(ra0, dec0, 30.0)
    assert set(got) == set(LVS_COLUMNS)
    assert sorted(got["objname"]) == ["AT-25", "NGC 0001"]
    at25 = list(got["objname"]).index("AT-25")
    assert got["z_tech"][at25] is None and got["z_qual"][at25] is np.True_
    assert got["Mstar"][list(got["objname"]).index("NGC 0001")] == pytest.approx(7.9e10)
    assert np.isnan(got["Mstar"][at25])

    assert sorted(reader(ra0, dec0, 50.0)["objname"]) == ["AT-25", "AT-45", "NGC 0001"]
    assert reader(ra0 + 0.5, dec0, 10.0)["objname"].size == 0      # empty, not None
    assert reader.n_reads == 1                                     # one load, reused


def test_lvs_reader_feeds_the_matcher(tmp_path):
    ra0, dec0 = 150.0, -20.0
    write_lvs_store(tmp_path, [{**FULL_GALAXY, "ra": ra0 + ra_offset(dec0, 4.0), "dec": dec0}])
    cat = build_lvscat(nr.LvsReader(str(tmp_path))(ra0, dec0, 40.0))
    (match,) = match_lvscat(ra0, dec0, cat)[0]
    assert match.prefname == "NGC 0001" and match.sep == pytest.approx(4.0, abs=0.01)
    assert match.Diam_qual is True and match.DistMpc == pytest.approx(62.0)


def test_lvs_reader_refuses_a_missing_table(tmp_path):
    with pytest.raises(FileNotFoundError, match="ingest-lvs"):
        nr.LvsReader(str(tmp_path))
