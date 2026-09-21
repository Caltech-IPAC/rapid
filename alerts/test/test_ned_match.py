"""NED cross-match tests.

Three layers, mirroring test_ref_match.py:
  - select_host_candidates() / build_nedcat() / match_nedcat(): pure
    functions over in-memory NED slices (conftest.make_ned_table); sep/pa
    cross-checked against the KONA matcher's independent Vincenty
    implementation. The host-candidate selection is tested on its own --
    it is the main tunable of this cross-match and the thing most likely
    to be changed later.
  - bounding_cone(): the per-chip query cone must contain every possible
    match of every detection on the chip.
  - End-to-end over the fake chip (conftest FakeDB) with an injected
    fake_ned_reader: assemble + serialize alerts for all three nedMatches
    states -- null (matching not run), [] (ran and found nothing),
    populated -- through the real provider and its per-chip slice cache.
  - The astroquery adapter's column mapping, over a synthetic masked
    astropy Table with the web service's real column names (no network).

No test here touches NED itself; a live counterpart belongs in
test_live_db.py once the reader is wired into the CLI.
"""

import io
import math

import fastavro
import numpy as np
import pytest
from astropy.table import MaskedColumn, Table

from alerts import providers
from alerts.produce import assemble_alert, load_schema, serialize_alert
from alerts.providers import (NED_CONE_MAX_ARCSEC, NED_MATCH_NMAX,
                              NED_MATCH_RADIUS_ARCSEC, bounding_cone,
                              build_nedcat, chip_cone, match_nedcat,
                              match_ss_predictions, ned_table_to_columns,
                              select_host_candidates)
from conftest import fake_ned_reader, make_ned_table, make_source_row


@pytest.fixture(autouse=True)
def selection_on(monkeypatch):
    """Run this file with host-candidate selection ENABLED.

    The rule itself is what these tests specify (allowlist, redshift
    escape hatch, ordering after selection, the empty-vs-null contract),
    so they must not depend on the production default. That default was
    switched OFF on 2026-09-18 pending a rewrite against the local HATS
    copy of NED -- see the NED_SELECTION_ENABLED comment in providers.py;
    test_provider.py pins the default itself.
    """
    monkeypatch.setattr(providers, "NED_SELECTION_ENABLED", True)


def ra_offset(dec, sep_arcsec):
    """Degrees of RA giving `sep_arcsec` of separation at declination dec."""
    return sep_arcsec / 3600.0 / math.cos(math.radians(dec))


def make_nedcat(entries):
    return build_nedcat(make_ned_table(entries))


# ---------------------------------------------------------------------------
# Host-candidate selection (the tunable)
# ---------------------------------------------------------------------------

def test_selection_allowlist_and_redshift_escape_hatch():
    ptype = ["G", "QSO", "GPair", "IrS", "UvS", "RadioS", "*", None, ""]
    z = [np.nan] * 9
    keep = select_host_candidates(ptype, z)
    assert list(keep) == [True, True, True, False, False, False,
                          False, False, False]

    # the escape hatch: any type -- or no type -- with a redshift is kept
    z = [np.nan, np.nan, np.nan, 0.12, np.nan, np.nan, np.nan, 0.05, np.nan]
    keep = select_host_candidates(ptype, z)
    assert list(keep) == [True, True, True, True, False, False,
                          False, True, False]


def test_selection_knobs(monkeypatch):
    ptype = ["G", "IrS", None]
    z = [np.nan, 0.12, np.nan]

    monkeypatch.setattr(providers, "NED_KEEP_ANY_TYPE_WITH_REDSHIFT", False)
    assert list(select_host_candidates(ptype, z)) == [True, False, False]

    monkeypatch.setattr(providers, "NED_SELECTION_ENABLED", False)
    assert list(select_host_candidates(ptype, z)) == [True, True, True]


def test_selection_empty_input():
    assert select_host_candidates([], []).shape == (0,)


# ---------------------------------------------------------------------------
# Building the slice
# ---------------------------------------------------------------------------

def test_build_nedcat_applies_selection_and_keeps_rows_aligned():
    cat = make_nedcat([
        {"ra": 150.0, "dec": -20.0, "prefname": "keepA", "ptype": "G"},
        {"ra": 150.1, "dec": -20.0, "prefname": "drop1", "ptype": "IrS"},
        {"ra": 150.2, "dec": -20.0, "prefname": "keepB", "ptype": "IrS",
         "z": 0.3, "zflag": "PSE?"},
        {"ra": 150.3, "dec": -20.0, "prefname": "drop2", "ptype": None},
        {"ra": 150.4, "dec": -20.0, "prefname": "keepC", "ptype": "QSO"},
    ])
    assert cat.n_input == 5
    assert list(cat.columns["prefname"]) == ["keepA", "keepB", "keepC"]
    # every column was filtered by the same mask: keepB's z rode along
    assert cat.columns["z"][1] == pytest.approx(0.3)
    assert cat.columns["zflag"][1] == "PSE?"
    assert len(cat.coords) == 3


def test_build_nedcat_fills_optional_columns_and_requires_positions():
    table = make_ned_table([{"ra": 150.0, "dec": -20.0}])
    del table["zunc"], table["zflag"]              # a backend without them
    cat = build_nedcat(table)
    assert cat is not None
    assert np.isnan(cat.columns["zunc"]).all()
    assert list(cat.columns["zflag"]) == [None]

    del table["ra"]
    assert build_nedcat(table) is None             # -> "not run"


def test_build_nedcat_nothing_selected_gives_none_coords():
    cat = make_nedcat([{"ra": 150.0, "dec": -20.0, "ptype": "IrS"}])
    assert cat is not None and cat.coords is None
    assert match_nedcat(150.0, -20.0, cat) == [[]]  # [] not None

    empty = build_nedcat(make_ned_table([]))
    assert empty is not None and empty.coords is None


# ---------------------------------------------------------------------------
# Geometry: sep/pa against the KONA matcher's independent implementation
# ---------------------------------------------------------------------------

def test_geometry_against_ss_matcher():
    rng = np.random.default_rng(7)
    for _ in range(25):
        ra0 = rng.uniform(0, 360)
        dec0 = rng.uniform(-85, 85)
        dra = rng.uniform(-4, 4) / 3600.0 / np.cos(np.radians(dec0))
        ddec = rng.uniform(-4, 4) / 3600.0
        pra, pdec = (ra0 + dra) % 360.0, float(np.clip(dec0 + ddec, -90, 90))

        cat = make_nedcat([{"ra": pra, "dec": pdec}])
        matches, = match_nedcat(ra0, dec0, cat, radius_arcsec=1e6)
        assert len(matches) == 1
        want, = match_ss_predictions(ra0, dec0, {"X": (pra, pdec, None)},
                                     radius_arcsec=1e6)
        assert matches[0].sep == pytest.approx(want.sep, abs=1e-6)
        if want.sep > 1e-3:                # PA undefined at zero separation
            dpa = (matches[0].pa - want.pa + 180.0) % 360.0 - 180.0
            assert abs(dpa) < 1e-6


# ---------------------------------------------------------------------------
# Selection: radius mask, nearest-N ordering, capping
# ---------------------------------------------------------------------------

def test_selection_ordering_and_radius():
    ra0, dec0 = 150.0, -20.0
    off = lambda s: ra_offset(dec0, s)
    cat = make_nedcat([
        # galaxies at 2..8": the 4th nearest is over n_max
        {"ra": ra0 + off(2.0), "dec": dec0, "prefname": "g2"},
        {"ra": ra0 + off(4.0), "dec": dec0, "prefname": "g4"},
        {"ra": ra0 + off(6.0), "dec": dec0, "prefname": "g6"},
        {"ra": ra0 + off(8.0), "dec": dec0, "prefname": "g8"},
        # inside the radius but not a host candidate: must not appear
        {"ra": ra0 + off(1.0), "dec": dec0, "prefname": "irs1", "ptype": "IrS"},
        # a host candidate just outside the radius
        {"ra": ra0 + off(NED_MATCH_RADIUS_ARCSEC + 1.0), "dec": dec0,
         "prefname": "far"},
    ])
    matches, = match_nedcat(ra0, dec0, cat)

    assert [m.prefname for m in matches] == ["g2", "g4", "g6"]
    assert all(a.sep <= b.sep for a, b in zip(matches, matches[1:]))
    assert len(matches) == NED_MATCH_NMAX
    assert all(m.sep <= NED_MATCH_RADIUS_ARCSEC for m in matches)
    assert matches[0].sep == pytest.approx(2.0, abs=0.01)
    assert matches[0].pa == pytest.approx(90.0, abs=0.1)   # due East


def test_fewer_rows_than_nmax():
    ra0, dec0 = 150.0, -20.0
    cat = make_nedcat([{"ra": ra0 + ra_offset(dec0, 1.0), "dec": dec0}])
    matches, = match_nedcat(ra0, dec0, cat)
    assert len(matches) == 1                 # nthneighbor capped, no error


def test_batch_equals_single():
    rng = np.random.default_rng(11)
    dec0 = -20.0
    entries = [{"ra": 150.0 + ra_offset(dec0, rng.uniform(-6, 6)),
                "dec": dec0 + rng.uniform(-6, 6) / 3600.0,
                "ptype": "G" if i % 3 else "IrS", "z": 0.1 if i % 5 == 0
                else np.nan}
               for i in range(20)]
    cat = make_nedcat(entries)
    src_ra = [150.0, 150.001, 149.999]
    src_dec = [dec0, dec0 + 5e-4, dec0 - 5e-4]

    batch = match_nedcat(src_ra, src_dec, cat)
    for i, (ra, dec) in enumerate(zip(src_ra, src_dec)):
        single, = match_nedcat(ra, dec, cat)
        assert [m.prefname for m in batch[i]] == [m.prefname for m in single]
        assert [m.sep for m in batch[i]] == pytest.approx(
            [m.sep for m in single], abs=1e-9)


def test_absent_values_become_none_not_nan_or_empty():
    ra0, dec0 = 150.0, -20.0
    cat = make_nedcat([
        {"ra": ra0, "dec": dec0, "ptype": "G", "zflag": ""},   # no z
        {"ra": ra0 + ra_offset(dec0, 1.0), "dec": dec0, "ptype": "G",
         "z": 0.0123, "zunc": 0.0004, "zflag": "PSE?"},
    ])
    nothing, has_z = match_nedcat(ra0, dec0, cat)[0]
    assert nothing.z is None and nothing.zunc is None
    assert nothing.zflag is None                 # "" -> None
    assert nothing.ptype == "G"
    assert has_z.z == pytest.approx(0.0123)
    assert has_z.zunc == pytest.approx(0.0004)
    assert has_z.zflag == "PSE?"


def test_prefname_preserved_verbatim():
    name = "2MASX J10002400+0212000"
    cat = make_nedcat([{"ra": 150.0, "dec": -20.0, "prefname": name}])
    matches, = match_nedcat(150.0, -20.0, cat)
    assert matches[0].prefname == name


# ---------------------------------------------------------------------------
# The per-chip query cone
# ---------------------------------------------------------------------------

def test_bounding_cone_contains_every_position_plus_pad():
    rng = np.random.default_rng(3)
    for _ in range(20):
        dec0 = rng.uniform(-80, 80)
        ra0 = rng.uniform(0, 360)
        # a ~7.5' chip's worth of scatter
        ra = (ra0 + rng.uniform(-4, 4, 30) / 60.0 / np.cos(np.radians(dec0)))
        dec = dec0 + rng.uniform(-4, 4, 30) / 60.0
        cra, cdec, radius = bounding_cone(ra, dec, pad_arcsec=11.0)

        want, = zip(*[match_ss_predictions(
            cra, cdec, {"P": (r, d, None)}, radius_arcsec=1e9)
            for r, d in zip(ra, dec)])
        seps = np.array([w.sep for w in want])
        assert seps.max() + 11.0 == pytest.approx(radius, abs=1e-6)
        assert (seps + 11.0 <= radius + 1e-6).all()


def test_bounding_cone_single_position_is_just_the_pad():
    cra, cdec, radius = bounding_cone(150.0, -20.0, pad_arcsec=11.0)
    assert cra == pytest.approx(150.0)
    assert cdec == pytest.approx(-20.0)
    assert radius == pytest.approx(11.0, abs=1e-9)


def test_bounding_cone_handles_ra_wrap():
    cra, cdec, radius = bounding_cone([359.9, 0.1], [0.0, 0.0], pad_arcsec=0)
    assert cra == pytest.approx(0.0, abs=1e-9) or cra == pytest.approx(360.0)
    assert radius == pytest.approx(0.1 * 3600.0, rel=1e-6)


def _chip_scatter(rng, ra0, dec0, n=30, half_arcmin=3.5):
    """n positions scattered over a ~7' chip around (ra0, dec0)."""
    ra = ra0 + rng.uniform(-half_arcmin, half_arcmin, n) / 60.0 \
        / np.cos(np.radians(dec0))
    dec = dec0 + rng.uniform(-half_arcmin, half_arcmin, n) / 60.0
    return ra, dec


def test_chip_cone_is_bounding_cone_when_the_chip_is_sane():
    rng = np.random.default_rng(5)
    ra, dec = _chip_scatter(rng, 268.09, -29.87)
    want = bounding_cone(ra, dec, pad_arcsec=11.0)
    cra, cdec, radius, inliers = chip_cone(ra, dec, pad_arcsec=11.0)
    assert (cra, cdec, radius) == pytest.approx(want)
    assert inliers.all()
    assert radius < NED_CONE_MAX_ARCSEC


def test_chip_cone_excludes_a_far_outlier_and_stays_chip_sized():
    """The pid 338173 case: thousands of on-chip detections plus a couple
    whose fitted positions are degrees away. The cone must not follow them."""
    rng = np.random.default_rng(6)
    ra, dec = _chip_scatter(rng, 268.09, -29.87, n=2000)
    ra = np.append(ra, [268.44, 267.96])          # 1.6 deg and 0.5 deg off
    dec = np.append(dec, [-28.26, -29.93])
    cra, cdec, radius, inliers = chip_cone(ra, dec, pad_arcsec=11.0)

    assert list(inliers[-2:]) == [False, False]
    assert inliers[:-2].all()
    assert radius <= NED_CONE_MAX_ARCSEC
    # and the cone is the ordinary one for the on-chip detections
    want = bounding_cone(ra[:-2], dec[:-2], pad_arcsec=11.0)
    assert (cra, cdec, radius) == pytest.approx(want)


def test_chip_cone_small_n_uses_a_centre_the_outlier_cannot_drag():
    """Three real detections and one far one: a mean centre would sit far
    enough toward the outlier to misclassify the real three as well."""
    ra = np.array([268.090, 268.091, 268.089, 268.44])
    dec = np.array([-29.870, -29.871, -29.869, -28.26])
    cra, cdec, radius, inliers = chip_cone(ra, dec, pad_arcsec=11.0)
    assert list(inliers) == [True, True, True, False]
    assert radius <= NED_CONE_MAX_ARCSEC


def test_chip_cone_nothing_survives_gives_all_false():
    """Two detections a degree apart: neither is 'the chip'; no query."""
    _, _, _, inliers = chip_cone([268.0, 269.0], [-29.0, -29.0],
                                 pad_arcsec=11.0)
    assert not inliers.any()


# ---------------------------------------------------------------------------
# End to end over the fake chip (real provider, injected reader)
# ---------------------------------------------------------------------------

@pytest.fixture()
def trigger_positions(make_provider):
    """The fake chip's three sky positions, keyed by sid."""
    provider = make_provider()
    return {sid: (d.ra, d.dec) for sid in (9001, 9002, 9003)
            for d in [provider.get_detection(sid)]}


@pytest.fixture()
def chip_ned_table(trigger_positions):
    """A NED slice around sid 9001: galaxies at 2" and 5" (nearest
    first), an IrS entry at 1" that must be filtered out, a galaxy just
    outside the radius, and a far one."""
    ra, dec = trigger_positions[9001]
    off = lambda s: ra_offset(dec, s)
    return make_ned_table([
        {"ra": ra + off(2.0), "dec": dec, "prefname": "HOST-A", "ptype": "G",
         "z": 0.0312, "zflag": "SLS"},
        {"ra": ra + off(5.0), "dec": dec, "prefname": "HOST-B", "ptype": "G"},
        {"ra": ra + off(1.0), "dec": dec, "prefname": "WISEA-X",
         "ptype": "IrS"},
        {"ra": ra + off(NED_MATCH_RADIUS_ARCSEC + 2.0), "dec": dec,
         "prefname": "EDGE", "ptype": "G"},
        {"ra": ra + 1.0, "dec": dec, "prefname": "FAR", "ptype": "G"},
    ])


def test_e2e_not_run_without_reader(make_provider):
    """No reader (the default) -> nedMatches stays null."""
    alert = assemble_alert(make_provider(), 9001)
    assert alert["nedMatches"] is None


def test_e2e_unavailable_slice_is_null_not_empty(make_provider,
                                                 chip_ned_table, caplog):
    reader = fake_ned_reader(chip_ned_table, coverage=False)
    with caplog.at_level("WARNING", logger="alerts.providers"):
        alert = assemble_alert(make_provider(ned_reader=reader), 9001)
    assert alert["nedMatches"] is None
    assert "not run" in caplog.text


def test_e2e_reader_exception_is_null_not_empty(make_provider, caplog):
    def boom(ra, dec, radius):
        raise ConnectionError("NED is down")
    with caplog.at_level("WARNING", logger="alerts.providers"):
        alert = assemble_alert(make_provider(ned_reader=boom), 9001)
    assert alert["nedMatches"] is None
    assert "NED query failed" in caplog.text


def test_e2e_ran_clean_is_empty_not_null(make_provider, trigger_positions):
    ra, dec = trigger_positions[9001]
    # only non-candidates nearby, and a candidate far away
    table = make_ned_table([
        {"ra": ra + ra_offset(dec, 1.0), "dec": dec, "ptype": "IrS"},
        {"ra": ra + 1.0, "dec": dec, "ptype": "G"},
    ])
    alert = assemble_alert(make_provider(ned_reader=fake_ned_reader(table)),
                           9001)
    assert alert["nedMatches"] == []


def test_e2e_matched_and_serialized(make_provider, chip_ned_table):
    reader = fake_ned_reader(chip_ned_table)
    alert = assemble_alert(make_provider(ned_reader=reader), 9001)

    assert [m["prefName"] for m in alert["nedMatches"]] == ["HOST-A", "HOST-B"]
    assert alert["nedMatches"][0]["sep"] == pytest.approx(2.0, abs=0.01)
    assert alert["nedMatches"][0]["z"] == pytest.approx(0.0312)
    assert alert["nedMatches"][0]["zFlag"] == "SLS"
    assert alert["nedMatches"][0]["type"] == "G"
    assert alert["nedMatches"][1]["z"] is None
    assert alert["nedMatches"][1]["zUnc"] is None    # never on this path

    schema = load_schema()
    decoded = fastavro.schemaless_reader(
        io.BytesIO(serialize_alert(alert, schema=schema)), schema)
    assert [m["prefName"] for m in decoded["nedMatches"]] == ["HOST-A", "HOST-B"]
    assert decoded["nedMatches"][0]["z"] == pytest.approx(0.0312)
    assert decoded["nedMatches"][1]["z"] is None


def test_e2e_batch_flow_matches_all_sources(make_provider, chip_ned_table):
    """iter_sources() fetches one slice and matches the whole chip in one
    pass; every source answers from the prefetch, and the per-source
    results agree with the single-alert flow from an independent provider."""
    log = []
    provider = make_provider(ned_reader=fake_ned_reader(chip_ned_table,
                                                        log=log))
    sources = list(provider.iter_sources(99))
    assert len(sources) == 3
    assert len(log) == 1                       # one slice for the chip

    fresh = make_provider(ned_reader=fake_ned_reader(chip_ned_table))
    for source in sources:
        batch = provider.get_ned_matches(source)
        single = fresh.get_ned_matches(fresh.get_detection(source.sid))
        assert batch is not None and single is not None
        assert [m.prefname for m in batch] == [m.prefname for m in single]


def test_e2e_chip_cone_covers_every_source(make_provider, trigger_positions):
    """A candidate host just inside the match radius of EACH chip source
    must be found in the batch flow -- the chip's single query cone has
    to reach every detection's neighbourhood, not just the centroid's."""
    entries = []
    for sid, (ra, dec) in trigger_positions.items():
        entries.append({"ra": ra + ra_offset(dec, NED_MATCH_RADIUS_ARCSEC - 0.1),
                        "dec": dec, "prefname": f"HOST-{sid}", "ptype": "G"})
    log = []
    reader = fake_ned_reader(make_ned_table(entries), log=log)
    provider = make_provider(ned_reader=reader)
    for source in provider.iter_sources(99):
        matches = provider.get_ned_matches(source)
        # its own host must be there. Not *only* its own: the fake chip is
        # ~33" across, so a neighbour's host 9.9" away can also fall inside
        # this source's 10" -- and reporting it, nearest first, is correct.
        assert f"HOST-{source.sid}" in [m.prefname for m in matches]
        assert all(a.sep <= b.sep for a, b in zip(matches, matches[1:]))
    # and the cone really was one chip-sized request, not per-source
    assert len(log) == 1
    _, _, radius = log[0]
    assert radius > NED_MATCH_RADIUS_ARCSEC


def test_e2e_off_chip_detection_is_null_and_cone_stays_chip_sized(
        make_provider, chip_data, tpv_header, trigger_positions, caplog):
    """A detection fitted far off the array -- as PhotUtils off-image fits
    are (pid 338173: xfit=22834, yfit=27838 on a 4088-pixel chip) -- must
    not stretch the chip's NED cone to degrees. It is excluded and reported
    null; the real detections still match; one warning names the count."""
    outlier = make_source_row(9004, 22834.8, 27837.6, 60500.5, tpv_header)
    chip_data.sources.append(outlier)
    chip_data.merges[9004] = 1111
    chip_data.objects[1111] = dict(chip_data.objects[777], aid=1111)

    entries = [{"ra": ra + ra_offset(dec, 2.0), "dec": dec,
                "prefname": f"HOST-{sid}", "ptype": "G"}
               for sid, (ra, dec) in trigger_positions.items()]
    log = []
    reader = fake_ned_reader(make_ned_table(entries), log=log)
    provider = make_provider(ned_reader=reader)
    with caplog.at_level("WARNING", logger="alerts.providers"):
        sources = list(provider.iter_sources(99))
    assert len(sources) == 4

    by_sid = {s.sid: provider.get_ned_matches(s) for s in sources}
    assert by_sid[9004] is None                       # not run -- never []
    for sid in (9001, 9002, 9003):
        assert f"HOST-{sid}" in [m.prefname for m in by_sid[sid]]
    (_, _, radius), = log
    assert radius <= NED_CONE_MAX_ARCSEC
    assert "1 of 4 detections" in caplog.text


def test_e2e_slice_cached_per_chip_including_failure(make_provider,
                                                     chip_ned_table):
    """A failed fetch is cached for the chip: three sources, one attempt."""
    calls = []
    def flaky(ra, dec, radius):
        calls.append(1)
        return None
    provider = make_provider(ned_reader=flaky)
    for source in provider.iter_sources(99):
        assert provider.get_ned_matches(source) is None
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# The astroquery adapter (column mapping only; no network)
# ---------------------------------------------------------------------------

def test_ned_table_to_columns_maps_names_and_masks():
    # the web service's real column names, with masked cells where NED
    # has no value -- exactly what astroquery hands back
    table = Table({
        "No.": [1, 2, 3],
        "Object Name": ["2MASX J1", "WISEA J2", "SDSS J3"],
        "RA": [150.0, 150.1, 150.2],
        "DEC": [-20.0, -20.0, -20.0],
        "Type": MaskedColumn(["G", "IrS", "G"], mask=[False, False, True]),
        "Redshift": MaskedColumn([0.0312, 0.0, 0.5],
                                 mask=[False, True, False]),
        "Redshift Flag": MaskedColumn(["SLS", "", "PSE?"],
                                      mask=[False, True, False]),
        "Magnitude and Filter": ["15.2g", "", "18.1r"],
    })
    cols = ned_table_to_columns(table)

    assert set(cols) == set(providers.NED_COLUMNS)
    assert list(cols["prefname"]) == ["2MASX J1", "WISEA J2", "SDSS J3"]
    assert cols["ra"].tolist() == pytest.approx([150.0, 150.1, 150.2])
    assert list(cols["ptype"]) == ["G", "IrS", None]         # masked -> None
    assert cols["z"][0] == pytest.approx(0.0312)
    assert np.isnan(cols["z"][1])                            # masked -> NaN
    assert list(cols["zflag"]) == ["SLS", None, "PSE?"]
    assert np.isnan(cols["zunc"]).all()                      # not served

    # and the result feeds straight into the matcher
    cat = build_nedcat(cols)
    assert list(cat.columns["prefname"]) == ["2MASX J1", "SDSS J3"]


def test_ned_table_to_columns_empty_table():
    cols = ned_table_to_columns(Table({"Object Name": [], "RA": [],
                                       "DEC": [], "Type": [],
                                       "Redshift": [], "Redshift Flag": []}))
    assert all(v.size == 0 for v in cols.values())
    assert build_nedcat(cols).coords is None
