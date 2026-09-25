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
  - Hp6NedReader (alerts/ned_reader.py), the production reader over the
    local order-6 copy of NED, against synthetic pixel files in tmp_path;
    see the section at the end of this file.

No test here touches NED or S3; the live copy is exercised by hand with
the CLI (--ned-source) and in test_live_db.py.
"""

import io
import math

import fastavro
import numpy as np
import pytest

from alerts import providers
from alerts.produce import assemble_alert, load_schema, serialize_alert
from alerts.providers import (NED_CONE_MAX_ARCSEC, NED_MATCH_NMAX,
                              NED_MATCH_RADIUS_ARCSEC, bounding_cone,
                              build_nedcat, chip_cone, match_nedcat,
                              match_ss_predictions, select_host_candidates)
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
# Tied positions: rows at the same coordinates must come back as distinct
# matches. The nth-neighbour loop this replaced returned one row for every
# rank (3C 273's absorption-line systems, live 2026-09-23).
# ---------------------------------------------------------------------------

def test_coincident_rows_are_distinct_matches():
    ra0, dec0 = 10.0, 20.0
    cat = make_nedcat([
        {"ra": ra0, "dec": dec0, "prefname": "ABS-1", "ptype": "AbLS", "z": 0.1},
        {"ra": ra0, "dec": dec0, "prefname": "ABS-2", "ptype": "AbLS", "z": 0.2},
        {"ra": ra0, "dec": dec0, "prefname": "ABS-3", "ptype": "AbLS", "z": 0.3},
        {"ra": ra0 + ra_offset(dec0, 3.0), "dec": dec0, "prefname": "NEAR", "ptype": "G"},
    ])
    [matches] = match_nedcat(ra0, dec0, cat, n_max=3)
    assert [m.prefname for m in matches] == ["ABS-1", "ABS-2", "ABS-3"]   # distinct, index order
    assert all(m.sep == pytest.approx(0.0, abs=1e-6) for m in matches)
    [matches] = match_nedcat(ra0, dec0, cat, n_max=4)
    assert [m.prefname for m in matches] == ["ABS-1", "ABS-2", "ABS-3", "NEAR"]
    assert matches[-1].sep == pytest.approx(3.0, abs=0.01)


def test_matcher_agrees_with_nth_neighbour_loop_when_untied():
    # property check against the algorithm this replaced: identical
    # matches (indices, order, sep, pa) on random inputs without ties
    from astropy import units as u
    from astropy.coordinates import SkyCoord
    rng = np.random.default_rng(3)
    ra0, dec0 = 210.0, -35.0
    ncat, nsrc = 400, 120
    cat_ra = ra0 + rng.uniform(-60, 60, ncat) / 3600 / np.cos(np.radians(dec0))
    cat_dec = dec0 + rng.uniform(-60, 60, ncat) / 3600
    cat = make_nedcat([{"ra": r, "dec": d, "prefname": f"C{i}", "ptype": "G"}
                       for i, (r, d) in enumerate(zip(cat_ra, cat_dec))])
    src_ra = ra0 + rng.uniform(-60, 60, nsrc) / 3600 / np.cos(np.radians(dec0))
    src_dec = dec0 + rng.uniform(-60, 60, nsrc) / 3600
    got = match_nedcat(src_ra, src_dec, cat, radius_arcsec=12.0, n_max=3)

    src = SkyCoord(src_ra * u.deg, src_dec * u.deg)
    want = [[] for _ in range(nsrc)]
    for n in (1, 2, 3):                        # the old loop
        idx, sep2d, _ = src.match_to_catalog_sky(cat.coords, nthneighbor=n)
        pa = src.position_angle(cat.coords[idx]).deg % 360.0
        for i in np.flatnonzero(sep2d.arcsec <= 12.0):
            want[i].append((cat.columns["prefname"][idx[i]], sep2d.arcsec[i], pa[i]))
    assert sum(len(w) for w in want) > 100     # a meaningful comparison
    for g, w in zip(got, want):
        assert [m.prefname for m in g] == [name for name, _, _ in w]
        assert [m.sep for m in g] == pytest.approx([s for _, s, _ in w], abs=1e-6)
        assert [m.pa for m in g] == pytest.approx([p for _, _, p in w], abs=1e-6)


def test_sep_pa_matches_astropy_everywhere():
    # the matcher's numpy separation / position angle against astropy's
    # SkyCoord, over the whole sphere and the regimes that bite: tiny
    # offsets at all declinations, the poles, the RA 0/360 seam
    from astropy import units as u
    from astropy.coordinates import SkyCoord
    from alerts.providers import _sep_pa
    rng = np.random.default_rng(42)
    n = 20000

    def check(ra1, dec1, ra2, dec2, sep_tol, pa_tol):
        a = SkyCoord(ra1 * u.deg, dec1 * u.deg)
        b = SkyCoord(ra2 * u.deg, dec2 * u.deg)
        want_sep = a.separation(b).arcsec
        want_pa = a.position_angle(b).deg % 360.0
        sep, pa = _sep_pa(np.radians(ra1), np.radians(dec1),
                          np.radians(ra2), np.radians(dec2))
        assert np.max(np.abs(sep - want_sep)) < sep_tol
        dpa = np.abs((pa - want_pa + 180.0) % 360.0 - 180.0)   # wrap-aware
        assert np.max(np.where(want_sep < 1e-6, 0.0, dpa)) < pa_tol

    ra1 = rng.uniform(0, 360, n)
    dec1 = np.degrees(np.arcsin(rng.uniform(-1, 1, n)))
    check(ra1, dec1, rng.uniform(0, 360, n),
          np.degrees(np.arcsin(rng.uniform(-1, 1, n))), 1e-8, 1e-9)   # whole sphere
    sep = 10 ** rng.uniform(-3, np.log10(60), n) / 3600                 # 0.001"-60"
    theta = rng.uniform(0, 2 * np.pi, n)
    dec2 = np.clip(dec1 + sep * np.cos(theta), -89.999, 89.999)
    ra2 = (ra1 + sep * np.sin(theta) / np.cos(np.radians(dec2))) % 360
    check(ra1, dec1, ra2, dec2, 1e-8, 1e-4)                             # PA ill-conditioned at 1 mas
    decp = rng.uniform(88, 89.999, n)
    check(ra1, decp, rng.uniform(0, 360, n), np.clip(decp + rng.uniform(-.01, .01, n), -90, 89.9999), 1e-8, 1e-9)
    check(ra1, -decp, rng.uniform(0, 360, n), -np.clip(decp + rng.uniform(-.01, .01, n), -90, 89.9999), 1e-8, 1e-9)
    decw = rng.uniform(-80, 80, n)
    check(rng.uniform(359.99, 360, n), decw, rng.uniform(0, 0.01, n),
          decw + rng.uniform(-.01, .01, n), 1e-8, 1e-6)                # RA seam
    # cardinal directions at the equator: exact quadrant PAs
    z = np.zeros(4)
    sep, pa = _sep_pa(np.radians(z), np.radians(z),
                      np.radians(np.array([0, 1 / 3600, 0, 360 - 1 / 3600])),
                      np.radians(np.array([1 / 3600, 0, -1 / 3600, 0])))
    assert pa == pytest.approx([0.0, 90.0, 180.0, 270.0], abs=1e-9)
    assert sep == pytest.approx([1.0] * 4, abs=1e-9)


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
# Hp6NedReader (alerts/ned_reader.py): the NedSliceReader over the local
# order-6 copy of NED that alerts.ned_catalog builds. Synthetic pixel files
# in tmp_path; the reader contract, pixel-edge cones, the cache, and one
# full alert over the fake chip whose nedMatches come from the store.
# (Selection is ON in this file, so store objects carry a type or a z.)
# ---------------------------------------------------------------------------

import json

import healpy as hp
import pyarrow as pa
import pyarrow.parquet as pq

from alerts import ned_catalog as nc
from alerts import ned_reader as nr
from alerts.providers import NED_COLUMNS, build_nedcat, match_nedcat

HP6_NSIDE = 2 ** nc.HP6_ORDER


def write_hp6_store(root, objects, complete=True):
    """Lay out `objects` (dicts: ra, dec, prefname, optional ptype/z/zunc/
    zflag) as order-6 pixel files under `root`, plus a manifest. Returns
    the pixels written."""
    by_pixel = {}
    for obj in objects:
        pix = int(hp.ang2pix(HP6_NSIDE, obj["ra"], obj["dec"], nest=True, lonlat=True))
        by_pixel.setdefault(pix, []).append(obj)
    for pix, objs in by_pixel.items():
        path = root / nc.hp6_file(pix)
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.table({
            "prefname": [o["prefname"] for o in objs],
            "ra": [o["ra"] for o in objs], "dec": [o["dec"] for o in objs],
            "ptype": pa.array([o.get("ptype") for o in objs], pa.string()),
            "z": [o.get("z", np.nan) for o in objs],
            "zunc": [o.get("zunc", np.nan) for o in objs],
            "zflag": pa.array([o.get("zflag") for o in objs], pa.string()),
        }), path)
    (root / nc.MANIFEST_NAME).write_text(json.dumps(
        {"release": "NED_TEST", "hp6": {"complete": complete}}))
    return sorted(by_pixel)


def test_hp6_table_to_columns_contract():
    t = pa.table({"prefname": ["A", "B"], "ra": [1.0, 2.0], "dec": [3.0, 4.0],
                  "ptype": pa.array(["G", ""], pa.string()), "z": [0.1, None],
                  "zunc": [None, None], "zflag": pa.array([None, "SLS"], pa.string())})
    cols = nr.table_to_columns(t)
    assert set(cols) == set(NED_COLUMNS)
    assert list(cols["ptype"]) == ["G", None]            # "" -> None
    assert list(cols["zflag"]) == [None, "SLS"]
    assert cols["z"][0] == 0.1 and np.isnan(cols["z"][1]) and np.isnan(cols["zunc"]).all()


def test_hp6_cone_returns_only_rows_within_radius(tmp_path):
    ra0, dec0 = 150.0, -20.0
    write_hp6_store(tmp_path, [
        {"ra": ra0, "dec": dec0, "prefname": "AT-CENTRE", "ptype": "G", "z": 0.02},
        {"ra": ra0 + ra_offset(dec0, 6.0), "dec": dec0, "prefname": "AT-6AS", "ptype": ""},
        {"ra": ra0, "dec": dec0 + 15.0 / 3600, "prefname": "AT-15AS"},
        {"ra": ra0 + 1.0, "dec": dec0, "prefname": "FAR"},
    ])
    reader = nr.Hp6NedReader(str(tmp_path))
    assert reader.complete and reader.release == "NED_TEST"
    got = reader(ra0, dec0, 10.0)
    assert sorted(got["prefname"]) == ["AT-6AS", "AT-CENTRE"]
    assert list(got["ptype"][np.argsort(got["prefname"])]) == [None, "G"]   # "" -> None
    assert reader(ra0, dec0, 20.0)["prefname"].tolist().count("AT-15AS") == 1


def test_hp6_cone_crossing_pixel_edges_sees_every_neighbour(tmp_path):
    # centre the cone on a pixel corner: the disc overlaps several pixels,
    # and objects 3" away in each of them must all come back
    pix = int(hp.ang2pix(HP6_NSIDE, 40.0, 10.0, nest=True, lonlat=True))
    corner = hp.boundaries(HP6_NSIDE, pix, step=1, nest=True)[:, 0]
    ra0, dec0 = (float(np.asarray(v).item()) for v in hp.vec2ang(corner, lonlat=True))
    objs = [{"ra": ra0 + ra_offset(dec0, dra), "dec": dec0 + ddec / 3600,
             "prefname": f"N{i}", "ptype": "G"}
            for i, (dra, ddec) in enumerate([(3, 0), (-3, 0), (0, 3), (0, -3)])]
    pixels = write_hp6_store(tmp_path, objs)
    assert len(pixels) >= 2                       # the objects do straddle pixels
    reader = nr.Hp6NedReader(str(tmp_path))
    assert sorted(reader(ra0, dec0, 5.0)["prefname"]) == ["N0", "N1", "N2", "N3"]
    assert set(reader.pixels_for_cone(ra0, dec0, 5.0)) >= set(pixels)


def test_hp6_missing_pixel_is_empty_when_complete_and_none_when_not(tmp_path):
    # X sits at a pixel centre, so a 10" cone around it touches its pixel only
    xra, xdec = (float(v) for v in hp.pix2ang(HP6_NSIDE, 1234, nest=True, lonlat=True))
    write_hp6_store(tmp_path, [{"ra": xra, "dec": xdec, "prefname": "X"}], complete=True)
    reader = nr.Hp6NedReader(str(tmp_path))
    empty = reader(180.0, 45.0, 10.0)             # no file anywhere near
    assert empty is not None and len(empty["prefname"]) == 0
    assert set(empty) == set(NED_COLUMNS)
    (tmp_path / nc.MANIFEST_NAME).unlink()        # no manifest: coverage unknown
    reader = nr.Hp6NedReader(str(tmp_path))
    assert not reader.complete
    assert reader(180.0, 45.0, 10.0) is None      # a touched pixel has no file
    assert reader(xra, xdec, 10.0)["prefname"].tolist() == ["X"]   # present pixel works


def test_hp6_pixel_cache_reads_each_file_once(tmp_path):
    write_hp6_store(tmp_path, [{"ra": 10.0, "dec": 10.0, "prefname": "A"}])
    reader = nr.Hp6NedReader(str(tmp_path), cache_pixels=2)
    reader(10.0, 10.0, 10.0); n = reader.n_reads
    reader(10.0, 10.0 + 1 / 3600, 10.0)
    assert reader.n_reads == n >= 1               # second cone served from cache


def test_hp6_alert_over_fake_chip_matches_local_store(make_provider, trigger_positions,
                                                      tmp_path):
    ra, dec = trigger_positions[9001]
    write_hp6_store(tmp_path, [
        {"ra": ra + ra_offset(dec, 2.0), "dec": dec, "prefname": "HOST-A",
         "ptype": "G", "z": 0.0312, "zunc": 0.0001, "zflag": "SLS"},
        {"ra": ra, "dec": dec + 6.0 / 3600, "prefname": "HOST-B", "ptype": "",
         "z": 0.05},                              # untyped but has z: passes selection
        {"ra": ra, "dec": dec + 40.0 / 3600, "prefname": "TOO-FAR", "ptype": "G"},
    ])
    provider = make_provider(ned_reader=nr.Hp6NedReader(str(tmp_path)))
    alert = assemble_alert(provider, 9001)
    names = [m["prefName"] for m in alert["nedMatches"]]
    assert names == ["HOST-A", "HOST-B"]          # nearest first, TOO-FAR excluded
    first = alert["nedMatches"][0]
    assert first["sep"] == pytest.approx(2.0, abs=0.02)
    assert first["type"] == "G" and first["z"] == pytest.approx(0.0312)
    assert first["zUnc"] == pytest.approx(0.0001) and first["zFlag"] == "SLS"
    assert alert["nedMatches"][1]["type"] is None      # "" arrived as null
    schema = load_schema()
    decoded = fastavro.schemaless_reader(io.BytesIO(serialize_alert(alert, schema=schema)), schema)
    assert [m["prefName"] for m in decoded["nedMatches"]] == names


def test_hp6_reader_feeds_the_matcher_directly(tmp_path):
    ra0, dec0 = 185.7288, 15.8225                 # M100's position
    write_hp6_store(tmp_path, [{"ra": ra0, "dec": dec0, "prefname": "NGC 4321",
                                "ptype": "G", "z": 0.00524}])
    reader = nr.Hp6NedReader(str(tmp_path))
    cat = build_nedcat(reader(ra0, dec0, NED_MATCH_RADIUS_ARCSEC + 1.0))
    [matches] = match_nedcat(ra0, dec0, cat)
    assert [m.prefname for m in matches] == ["NGC 4321"]
    assert matches[0].sep == pytest.approx(0.0, abs=1e-3)
