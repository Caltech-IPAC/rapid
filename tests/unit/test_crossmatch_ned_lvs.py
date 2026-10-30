"""The NED and NED-LVS matchers over in-memory slices (`dev`'s
``alerts/test/test_ned_match.py`` and ``test_lvs_match.py``, the
pure-function layers, ported 2026-10-30).

- select_host_candidates / build_nedcat / match_nedcat: the host-candidate
  selection (an argument here, `dev`'s module switch), the slice built with
  its rows aligned, tied positions as distinct matches, ordering, radius
  and cap, absent values as None.
- build_lvscat / match_lvscat: every LVS column through to the match with
  its type, absent values, the wider radius.
The geometry itself (sep/pa, nearest-N) is the reference-catalog match's,
tested with it.
"""

import numpy as np
import pytest

from rapidpipe.science.alerts.crossmatch import (
    LVS_BOOL_COLUMNS, LVS_COLUMNS, LVS_MATCH_NMAX, LVS_MATCH_RADIUS_ARCSEC, LVS_NUMERIC_COLUMNS,
    LVS_STRING_COLUMNS, NED_MATCH_NMAX, NED_MATCH_RADIUS_ARCSEC, build_lvscat, build_nedcat,
    match_lvscat, match_nedcat, match_ss_predictions, select_host_candidates)
from tests.support.catalogs import FULL_GALAXY, make_lvs_table, make_ned_table, ra_offset


def make_nedcat(entries, select_hosts=True):
    return build_nedcat(make_ned_table(entries), select_hosts=select_hosts)


# ---------------------------------------------------------------------------
# Host-candidate selection (the tunable)
# ---------------------------------------------------------------------------

def test_selection_allowlist_and_redshift_escape_hatch():
    ptype = ["G", "QSO", "GPair", "IrS", "UvS", "RadioS", "*", None, ""]
    z = [np.nan] * 9
    keep = select_host_candidates(ptype, z)
    assert list(keep) == [True, True, True, False, False, False, False, False, False]
    # the escape hatch: any type, or no type, with a redshift is kept
    z = [np.nan, np.nan, np.nan, 0.12, np.nan, np.nan, np.nan, 0.05, np.nan]
    keep = select_host_candidates(ptype, z)
    assert list(keep) == [True, True, True, True, False, False, False, True, False]


def test_selection_knobs():
    ptype = ["G", "IrS", None]
    z = [np.nan, 0.12, np.nan]
    assert list(select_host_candidates(ptype, z, keep_any_type_with_redshift=False)) == [True, False, False]
    assert list(select_host_candidates(ptype, z, enabled=False)) == [True, True, True]
    assert select_host_candidates([], []).shape == (0,)


# ---------------------------------------------------------------------------
# Building the NED slice
# ---------------------------------------------------------------------------

def test_build_nedcat_applies_selection_and_keeps_rows_aligned():
    cat = make_nedcat([
        {"ra": 150.0, "dec": -20.0, "prefname": "keepA", "ptype": "G"},
        {"ra": 150.1, "dec": -20.0, "prefname": "drop1", "ptype": "IrS"},
        {"ra": 150.2, "dec": -20.0, "prefname": "keepB", "ptype": "IrS", "z": 0.3, "zflag": "PSE?"},
        {"ra": 150.3, "dec": -20.0, "prefname": "drop2", "ptype": None},
        {"ra": 150.4, "dec": -20.0, "prefname": "keepC", "ptype": "QSO"},
    ])
    assert cat.n_input == 5
    assert list(cat.columns["prefname"]) == ["keepA", "keepB", "keepC"]
    assert cat.columns["z"][1] == pytest.approx(0.3)          # every column filtered by one mask
    assert cat.columns["zflag"][1] == "PSE?"
    assert len(cat.coords) == 3
    # the production default keeps every row
    assert len(make_nedcat([{"ra": 150.0, "dec": -20.0, "ptype": "IrS"}], select_hosts=False).coords) == 1


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
# Matching NED
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
        want, = match_ss_predictions(ra0, dec0, {"X": (pra, pdec, None)}, radius_arcsec=1e6)
        assert matches[0].sep == pytest.approx(want.sep, abs=1e-6)
        if want.sep > 1e-3:                # PA undefined at zero separation
            dpa = (matches[0].pa - want.pa + 180.0) % 360.0 - 180.0
            assert abs(dpa) < 1e-6


def test_selection_ordering_and_radius():
    ra0, dec0 = 150.0, -20.0
    off = lambda s: ra_offset(dec0, s)   # noqa: E731
    cat = make_nedcat([
        {"ra": ra0 + off(2.0), "dec": dec0, "prefname": "g2"},
        {"ra": ra0 + off(4.0), "dec": dec0, "prefname": "g4"},
        {"ra": ra0 + off(6.0), "dec": dec0, "prefname": "g6"},
        {"ra": ra0 + off(8.0), "dec": dec0, "prefname": "g8"},
        {"ra": ra0 + off(1.0), "dec": dec0, "prefname": "irs1", "ptype": "IrS"},   # not a candidate
        {"ra": ra0 + off(NED_MATCH_RADIUS_ARCSEC + 1.0), "dec": dec0, "prefname": "far"},
    ])
    matches, = match_nedcat(ra0, dec0, cat)
    assert [m.prefname for m in matches] == ["g2", "g4", "g6"]
    assert all(a.sep <= b.sep for a, b in zip(matches, matches[1:]))
    assert len(matches) == NED_MATCH_NMAX
    assert all(m.sep <= NED_MATCH_RADIUS_ARCSEC for m in matches)
    assert matches[0].sep == pytest.approx(2.0, abs=0.01)
    assert matches[0].pa == pytest.approx(90.0, abs=0.1)   # due East


def test_batch_equals_single_ned():
    rng = np.random.default_rng(11)
    dec0 = -20.0
    entries = [{"ra": 150.0 + ra_offset(dec0, rng.uniform(-6, 6)),
                "dec": dec0 + rng.uniform(-6, 6) / 3600.0,
                "ptype": "G" if i % 3 else "IrS", "z": 0.1 if i % 5 == 0 else np.nan}
               for i in range(20)]
    cat = make_nedcat(entries)
    src_ra = [150.0, 150.001, 149.999]
    src_dec = [dec0, dec0 + 5e-4, dec0 - 5e-4]
    batch = match_nedcat(src_ra, src_dec, cat)
    for i, (ra, dec) in enumerate(zip(src_ra, src_dec)):
        single, = match_nedcat(ra, dec, cat)
        assert [m.prefname for m in batch[i]] == [m.prefname for m in single]
        assert [m.sep for m in batch[i]] == pytest.approx([m.sep for m in single], abs=1e-9)


def test_absent_ned_values_become_none_not_nan_or_empty():
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


# ---------------------------------------------------------------------------
# NED-LVS
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
    ra0, dec0 = 150.0, -20.0
    cat = build_lvscat(make_lvs_table([{**FULL_GALAXY, "ra": ra0, "dec": dec0}]))
    (match,) = match_lvscat(ra0, dec0, cat)[0]
    assert match.prefname == "NGC 0001"
    for name in LVS_STRING_COLUMNS:
        if name != "objname":
            assert getattr(match, name) == FULL_GALAXY[name]
    for name in LVS_BOOL_COLUMNS:
        assert getattr(match, name) is FULL_GALAXY[name]
    for name in LVS_NUMERIC_COLUMNS:
        if name not in ("ra", "dec"):
            assert getattr(match, name) == pytest.approx(FULL_GALAXY[name])
    assert set(LVS_COLUMNS) == set(LVS_STRING_COLUMNS) | set(LVS_BOOL_COLUMNS) | set(LVS_NUMERIC_COLUMNS)


def test_absent_lvs_values_become_none_and_flags_stay_bool():
    ra0, dec0 = 150.0, -20.0
    cat = build_lvscat(make_lvs_table([{"ra": ra0, "dec": dec0, "z_tech": "", "z_qual": True}]))
    (match,) = match_lvscat(ra0, dec0, cat)[0]
    assert match.z is None and match.Diam is None and match.Mstar is None
    assert match.z_tech is None and match.DistMpc_method is None
    assert match.z_qual is True and match.Diam_qual is False
    assert isinstance(match.z_qual, bool)


def test_lvs_ordering_radius_and_cap():
    ra0, dec0 = 150.0, -20.0
    off = lambda s: ra_offset(dec0, s)   # noqa: E731
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
    assert LVS_MATCH_RADIUS_ARCSEC > NED_MATCH_RADIUS_ARCSEC


def test_batch_equals_single_lvs():
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
