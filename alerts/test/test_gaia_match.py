"""Gaia cross-match tests.

Mirrors test_lvs_match.py for the third external catalog:
  - epoch_offset_years() / propagate_positions(): the proper-motion
    propagation, which is what makes Gaia different from NED.
  - build_gaiacat() / match_gaiacat(): pure functions over in-memory Gaia
    slices (conftest.make_gaia_table). Every Gaia column must reach the
    GaiaMatch record under its own name; NaN -> None; ints and bools keep
    their types; the variability string becomes a bool.
  - End-to-end over the fake chip (conftest FakeDB) with an injected
    fake_ned_reader over a Gaia table: the three gaiaMatches states, the
    Avro round trip (sourceId as a long), and a star that only matches
    once propagated to the chip's epoch.
  - GaiaReader (alerts/catalog_readers.py), the production reader over a
    synthetic LINCC-shaped copy in tmp_path: partition lookup through the
    ancestor walk, row-group selection and caching, the empty cone, the
    missing-partition rule, the ref_epoch hand-off, and the refusal.

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
import pyarrow.parquet as pq
import pytest

from alerts import catalog_readers as nr
from alerts import catalog_store as nc
from alerts.produce import assemble_alert, load_schema, serialize_alert
from alerts.providers import (GAIA_BOOL_COLUMNS, GAIA_COLUMNS, GAIA_DEFAULT_REF_EPOCH,
                              GAIA_INT_COLUMNS, GAIA_MATCH_NMAX,
                              GAIA_MATCH_RADIUS_ARCSEC, GAIA_NUMERIC_COLUMNS,
                              GAIA_PM_MAX_ARCSEC_PER_YEAR, GAIA_STRING_COLUMNS,
                              build_gaiacat, epoch_offset_years, match_gaiacat,
                              propagate_positions)
from conftest import fake_ned_reader, make_gaia_table


def ra_offset(dec, sep_arcsec):
    """Degrees of RA giving `sep_arcsec` of separation at declination dec."""
    return sep_arcsec / 3600.0 / math.cos(math.radians(dec))


# one star with every Gaia column set, for "does it all get through" tests
FULL_STAR = {
    "source_id": 4057125350474264448, "non_single_star": 1, "ipd_frac_multi_peak": 7,
    "duplicated_source": True, "in_qso_candidates": False, "in_galaxy_candidates": True,
    "phot_variable_flag": "VARIABLE",
    "pmra": -3.2, "pmdec": -5.9, "parallax": 0.41, "parallax_error": 0.05, "ruwe": 1.1,
    "phot_g_mean_mag": 17.9, "phot_bp_mean_mag": 19.1, "phot_rp_mean_mag": 16.8,
    "bp_rp": 2.3, "phot_g_mean_flux_over_error": 310.0,
    "classprob_dsc_combmod_star": 0.99, "classprob_dsc_combmod_galaxy": 0.005,
    "classprob_dsc_combmod_quasar": 0.005,
}


# ---------------------------------------------------------------------------
# Epoch propagation
# ---------------------------------------------------------------------------

def test_epoch_offset_years():
    assert epoch_offset_years(51544.5, 2000.0) == 0.0            # J2000.0 = MJD 51544.5
    assert epoch_offset_years(57388.5, 2016.0) == 0.0            # Gaia DR3's J2016.0
    assert epoch_offset_years(57388.5 + 365.25 * 11, 2016.0) == pytest.approx(11.0)
    assert epoch_offset_years(57388.5 - 365.25, 2016.0) == pytest.approx(-1.0)


def test_propagate_positions_is_linear_and_handles_cos_dec_nan_and_wrap():
    ra, dec = propagate_positions(np.array([10.0, 359.9999, 20.0]), np.array([60.0, 0.0, -30.0]),
                                  np.array([3600.0, 3600.0, np.nan]),
                                  np.array([-1800.0, 0.0, np.nan]), 10.0)
    # 1"/yr in RA*cos(dec) over 10 yr = 36" on the sky, i.e. 72" of RA at dec 60
    assert (ra[0] - 10.0) * 3600 == pytest.approx(72.0, abs=1e-6)
    assert (dec[0] - 60.0) * 3600 == pytest.approx(-18.0, abs=1e-6)
    assert 0.0 <= ra[1] < 0.02                                  # wrapped past 360
    assert ra[2] == 20.0 and dec[2] == -30.0                    # no pm: unmoved
    back_ra, back_dec = propagate_positions(ra[:1], dec[:1], np.array([3600.0]),
                                            np.array([-1800.0]), -10.0)
    assert back_dec[0] == pytest.approx(60.0, abs=1e-9)
    # not exactly reversible: the return trip divides by cos of the moved
    # declination (18" away), a ~1e-4 relative change on a 72" shift
    assert back_ra[0] == pytest.approx(10.0, abs=1e-5)


# ---------------------------------------------------------------------------
# Building and matching
# ---------------------------------------------------------------------------

def test_build_gaiacat_fills_optional_columns_and_requires_positions():
    table = make_gaia_table([{"ra": 150.0, "dec": -20.0}])
    del table["ruwe"], table["phot_variable_flag"], table["duplicated_source"]
    cat = build_gaiacat(table)
    assert cat is not None and len(cat.coords) == 1
    assert np.isnan(cat.columns["ruwe"]).all()
    assert list(cat.columns["phot_variable_flag"]) == [None]
    assert list(cat.columns["duplicated_source"]) == [False]

    del table["ra"]
    assert build_gaiacat(table) is None                        # -> "not run"


def test_build_gaiacat_empty_gives_none_coords():
    cat = build_gaiacat(make_gaia_table([]), dt_years=11.0)
    assert cat is not None and cat.coords is None
    assert match_gaiacat(150.0, -20.0, cat) == [[]]            # [] not None


def test_every_gaia_column_reaches_the_match():
    """GaiaMatch attributes are the column names; every one must carry the
    slice's value through, with the right type."""
    ra0, dec0 = 150.0, -20.0
    cat = build_gaiacat(make_gaia_table([{**FULL_STAR, "ra": ra0, "dec": dec0}]))
    (match,) = match_gaiacat(ra0, dec0, cat)[0]
    assert match.source_id == FULL_STAR["source_id"] and isinstance(match.source_id, int)
    for name in GAIA_INT_COLUMNS:
        assert getattr(match, name) == FULL_STAR[name]
        assert isinstance(getattr(match, name), int)
    for name in GAIA_BOOL_COLUMNS:
        assert getattr(match, name) is FULL_STAR[name]
    assert match.phot_variable_flag is True                     # "VARIABLE" -> True
    for name in GAIA_NUMERIC_COLUMNS:
        if name in ("ra", "dec"):
            continue
        assert getattr(match, name) == pytest.approx(FULL_STAR[name])
    assert set(GAIA_COLUMNS) == (set(GAIA_INT_COLUMNS) | set(GAIA_BOOL_COLUMNS)
                                 | set(GAIA_STRING_COLUMNS) | set(GAIA_NUMERIC_COLUMNS))


def test_absent_values_become_none_and_flags_keep_types():
    ra0, dec0 = 150.0, -20.0
    cat = build_gaiacat(make_gaia_table([{"ra": ra0, "dec": dec0}]))
    (match,) = match_gaiacat(ra0, dec0, cat)[0]
    assert match.pmra is None and match.phot_g_mean_mag is None and match.ruwe is None
    assert match.phot_variable_flag is False                    # "NOT_AVAILABLE" -> False
    assert match.non_single_star == 0 and match.ipd_frac_multi_peak == 0
    assert match.duplicated_source is False and isinstance(match.duplicated_source, bool)


def test_ordering_radius_and_cap():
    """Nearest 3 within GAIA_MATCH_RADIUS_ARCSEC (1", much tighter than NED's)."""
    ra0, dec0 = 150.0, -20.0
    off = lambda s: ra_offset(dec0, s)
    cat = build_gaiacat(make_gaia_table([
        {"ra": ra0 + off(0.8), "dec": dec0, "source_id": 80},
        {"ra": ra0 + off(0.2), "dec": dec0, "source_id": 20},
        {"ra": ra0 + off(0.9), "dec": dec0, "source_id": 90},
        {"ra": ra0 + off(0.5), "dec": dec0, "source_id": 50},
        {"ra": ra0 + off(GAIA_MATCH_RADIUS_ARCSEC + 0.3), "dec": dec0, "source_id": 999},
    ]))
    (matches,) = match_gaiacat(ra0, dec0, cat)
    assert [m.source_id for m in matches] == [20, 50, 80]
    assert len(matches) == GAIA_MATCH_NMAX
    assert all(m.sep <= GAIA_MATCH_RADIUS_ARCSEC for m in matches)
    assert matches[0].sep == pytest.approx(0.2, abs=0.01)
    assert matches[0].pa == pytest.approx(90.0, abs=0.1)        # due East
    assert GAIA_MATCH_RADIUS_ARCSEC <= 1.0


def test_propagation_brings_a_moving_star_into_the_match():
    """A star 5" away at the catalog epoch with the proper motion to cover
    that in 11 years matches only when the slice is propagated."""
    ra0, dec0, dt = 150.0, -20.0, 11.0
    pmra, pmdec = 400.0, -150.0                                 # mas/yr
    shift_ra = pmra * dt / 3.6e6 / math.cos(math.radians(dec0))
    shift_dec = pmdec * dt / 3.6e6
    entries = [{"ra": ra0 - shift_ra, "dec": dec0 - shift_dec, "pmra": pmra, "pmdec": pmdec,
                "source_id": 1}]
    assert match_gaiacat(ra0, dec0, build_gaiacat(make_gaia_table(entries))) == [[]]
    (match,) = match_gaiacat(ra0, dec0, build_gaiacat(make_gaia_table(entries), dt))[0]
    assert match.source_id == 1 and match.sep < 0.01
    assert match.ra == pytest.approx(ra0, abs=1e-6) and match.dec == pytest.approx(dec0, abs=1e-6)


def test_batch_equals_single():
    rng = np.random.default_rng(29)
    dec0 = -20.0
    entries = [{"ra": 150.0 + ra_offset(dec0, rng.uniform(-3, 3)),
                "dec": dec0 + rng.uniform(-3, 3) / 3600.0} for _ in range(40)]
    cat = build_gaiacat(make_gaia_table(entries))
    src_ra = [150.0, 150.0 + ra_offset(dec0, 0.7), 150.0 - ra_offset(dec0, 1.1)]
    src_dec = [dec0, dec0 + 0.4 / 3600, dec0 - 0.6 / 3600]
    batch = match_gaiacat(src_ra, src_dec, cat)
    for i, (ra, dec) in enumerate(zip(src_ra, src_dec)):
        (single,) = match_gaiacat(ra, dec, cat)
        assert [m.source_id for m in batch[i]] == [m.source_id for m in single]
        assert [m.sep for m in batch[i]] == pytest.approx([m.sep for m in single], abs=1e-9)


# ---------------------------------------------------------------------------
# End to end over the fake chip (real provider, injected reader)
# ---------------------------------------------------------------------------

@pytest.fixture()
def trigger(make_provider):
    """The fake chip's sid 9001: sky position and epoch."""
    detection = make_provider().get_detection(9001)
    return detection.ra, detection.dec, detection.mjdobs


@pytest.fixture()
def chip_gaia_table(trigger):
    """A Gaia slice around sid 9001: a full star whose catalog-epoch
    position is off by its proper motion so it lands 0.3" away once
    propagated to the chip's epoch, a sparse static star at 0.7", one just
    outside the radius, and a far one."""
    ra, dec, mjd = trigger
    dt = epoch_offset_years(mjd, GAIA_DEFAULT_REF_EPOCH)
    off = lambda s: ra_offset(dec, s)
    moving = {**FULL_STAR, "pmra": 250.0, "pmdec": -400.0}
    moving["ra"] = ra + off(0.3) - moving["pmra"] * dt / 3.6e6 / math.cos(math.radians(dec))
    moving["dec"] = dec - moving["pmdec"] * dt / 3.6e6
    return make_gaia_table([
        moving,
        {"ra": ra + off(0.7), "dec": dec, "source_id": 7},
        {"ra": ra + off(GAIA_MATCH_RADIUS_ARCSEC + 0.4), "dec": dec, "source_id": 14},
        {"ra": ra + 1.0, "dec": dec, "source_id": 999},
    ])


def test_e2e_not_run_without_reader(make_provider):
    alert = assemble_alert(make_provider(), 9001)
    assert alert["gaiaMatches"] is None


def test_e2e_unavailable_slice_is_null_not_empty(make_provider, chip_gaia_table, caplog):
    reader = fake_ned_reader(chip_gaia_table, coverage=False)
    with caplog.at_level("WARNING", logger="alerts.providers"):
        alert = assemble_alert(make_provider(gaia_reader=reader), 9001)
    assert alert["gaiaMatches"] is None
    assert "Gaia matching not run" in caplog.text


def test_e2e_ran_clean_is_empty_not_null(make_provider, trigger):
    ra, dec, _ = trigger
    table = make_gaia_table([{"ra": ra + 1.0, "dec": dec, "source_id": 999}])
    alert = assemble_alert(make_provider(gaia_reader=fake_ned_reader(table)), 9001)
    assert alert["gaiaMatches"] == []


def test_e2e_matched_propagated_and_serialized(make_provider, chip_gaia_table, trigger):
    ra, dec, mjd = trigger
    assert abs(epoch_offset_years(mjd, GAIA_DEFAULT_REF_EPOCH)) > 1.0   # propagation matters
    alert = assemble_alert(make_provider(gaia_reader=fake_ned_reader(chip_gaia_table)), 9001)

    assert [m["sourceId"] for m in alert["gaiaMatches"]] == [FULL_STAR["source_id"], 7]
    moving, sparse = alert["gaiaMatches"]
    assert moving["sep"] == pytest.approx(0.3, abs=0.01)        # at the chip's epoch
    assert moving["ra"] != pytest.approx(float(chip_gaia_table["ra"][0]), abs=1e-7)
    assert moving["variable"] is True and moving["inGalaxyCandidates"] is True
    assert moving["magG"] == pytest.approx(17.9) and moving["pmDec"] == pytest.approx(-400.0)
    assert sparse["sep"] == pytest.approx(0.7, abs=0.01)
    assert sparse["magG"] is None and sparse["variable"] is False and sparse["nonSingleStar"] == 0

    schema = load_schema()
    decoded = fastavro.schemaless_reader(
        io.BytesIO(serialize_alert(alert, schema=schema)), schema)
    assert [m["sourceId"] for m in decoded["gaiaMatches"]] == [FULL_STAR["source_id"], 7]
    assert decoded["gaiaMatches"][0]["probStar"] == pytest.approx(0.99)
    assert decoded["gaiaMatches"][1]["pmRA"] is None


def test_e2e_batch_flow_matches_all_sources_with_one_padded_cone(make_provider, chip_gaia_table,
                                                                 trigger):
    log = []
    provider = make_provider(gaia_reader=fake_ned_reader(chip_gaia_table, log=log))
    sources = list(provider.iter_sources(99))
    assert len(sources) == 3 and len(log) == 1            # one slice per chip
    dt = epoch_offset_years(trigger[2], GAIA_DEFAULT_REF_EPOCH)
    assert log[0][2] >= GAIA_PM_MAX_ARCSEC_PER_YEAR * abs(dt)   # the cone carries the pm pad
    fresh = make_provider(gaia_reader=fake_ned_reader(chip_gaia_table))
    for source in sources:
        batch = provider.get_gaia_matches(source)
        single = fresh.get_gaia_matches(fresh.get_detection(source.sid))
        assert batch is not None and single is not None
        assert [m.source_id for m in batch] == [m.source_id for m in single]


# ---------------------------------------------------------------------------
# GaiaReader (alerts/catalog_readers.py) over a synthetic LINCC-shaped copy
# ---------------------------------------------------------------------------

def write_gaia_store(root, entries, partition_order=3, complete=True, ref_epoch=2016.0,
                     rows_per_group=2, extra_partitions=()):
    """Write the copy that alerts.catalog_store ingest-gaia would, from
    make_gaia_table entries: one partition at `partition_order` holding
    every row (sorted by `_healpix_29`, small row groups so the cone
    read's row-group selection is exercised), the metadata files and the
    ingest record. `extra_partitions` are listed but not written."""
    table = make_gaia_table(entries)
    key = hp.ang2pix(2 ** nr.HP29_ORDER, table["ra"], table["dec"], nest=True, lonlat=True)
    order = np.argsort(key)
    arrow = pa.table({nr.GAIA_KEY: pa.array(key[order]),
                      **{k: pa.array(v[order]) for k, v in table.items()}})
    npix = int(key[0] >> (2 * (nr.HP29_ORDER - partition_order)))
    assert np.all(key >> (2 * (nr.HP29_ORDER - partition_order)) == npix), "one partition"
    store = nc.Store(str(root))
    rel = nc.gaia_leaf_rel(partition_order, npix)
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(arrow, root / rel, row_group_size=rows_per_group)
    leaves = [(partition_order, npix), *extra_partitions]
    store.write_text("partition_info.csv", "Norder,Npix\n" + "".join(f"{o},{p}\n" for o, p in leaves))
    store.write_text("hats.properties", "obs_collection=gaia_test\nhats_npix_suffix=.parquet\n")
    store.write_text(nc.GAIA_INFO, json.dumps({"release": "gaia_test", "complete": complete,
                                               "ref_epoch": ref_epoch}))
    return (partition_order, npix)


def test_gaia_reader_cone_row_groups_and_cache(tmp_path):
    ra0, dec0 = 150.0, -20.0
    off = lambda s: ra_offset(dec0, s)
    # four near rows, then a crowd 0.3 deg away: in a different order-6
    # pixel, so its keys are all below or all above the near rows' and
    # fill row groups the cone read must skip
    crowd = [{"ra": ra0 + 0.3 + i * 1e-4, "dec": dec0 + 0.3, "source_id": 10_000 + i}
             for i in range(40)]
    partition = write_gaia_store(tmp_path, [
        {**FULL_STAR, "ra": ra0, "dec": dec0},
        {"ra": ra0 + off(0.6), "dec": dec0, "source_id": 6},
        {"ra": ra0 + off(2.0), "dec": dec0, "source_id": 20},
        {"ra": ra0 + off(60.0), "dec": dec0, "source_id": 600},
        *crowd,
    ])
    reader = nr.GaiaReader(str(tmp_path))
    n_groups = reader._footer(partition).metadata.num_row_groups
    assert n_groups >= 20
    assert reader.release == "gaia_test" and reader.complete and reader.ref_epoch == 2016.0
    assert reader.partitions == {partition} and reader.n_reads == 0

    # the ancestor walk: an order-29 key in the cone maps to the order-3 partition
    lo, _ = nr.key_ranges_for_cone(ra0, dec0, 1.0)[0]
    assert reader.partition_for_key(lo) == partition

    got = reader(ra0, dec0, 1.0)
    assert set(got) == set(GAIA_COLUMNS)
    assert sorted(got["source_id"]) == [6, FULL_STAR["source_id"]]
    full = list(got["source_id"]).index(FULL_STAR["source_id"])
    assert got["phot_variable_flag"][full] == "VARIABLE"
    assert got["duplicated_source"][full] == True and got["non_single_star"][full] == 1   # noqa: E712
    assert got["phot_g_mean_mag"][full] == pytest.approx(17.9)
    assert np.isnan(got["phot_g_mean_mag"][list(got["source_id"]).index(6)])
    first_reads = reader.n_reads
    assert 0 < first_reads <= 3                                  # not the whole partition

    assert sorted(reader(ra0, dec0, 1.0)["source_id"]) == [6, FULL_STAR["source_id"]]
    assert reader.n_reads == first_reads                         # served from the cache
    assert sorted(reader(ra0, dec0, 70.0)["source_id"]) == [6, 20, 600, FULL_STAR["source_id"]]
    assert reader(ra0 + 0.1, dec0 + 0.1, 5.0)["source_id"].size == 0   # empty, not None
    assert reader(ra0 + 20.0, dec0, 5.0)["source_id"].size == 0        # no partition at all


def test_gaia_reader_missing_partition_rule(tmp_path):
    ra0, dec0 = 150.0, -20.0
    write_gaia_store(tmp_path, [{"ra": ra0, "dec": dec0}], complete=False,
                     extra_partitions=[(6, 0)])        # listed, never written
    reader = nr.GaiaReader(str(tmp_path))
    assert reader(ra0, dec0, 1.0)["source_id"].size == 1
    ra_missing, dec_missing = hp.pix2ang(2 ** 6, 0, nest=True, lonlat=True)
    assert reader(float(ra_missing), float(dec_missing), 1.0) is None      # incomplete copy
    (tmp_path / nc.GAIA_INFO).write_text(json.dumps({"complete": True, "ref_epoch": 2016.0}))
    assert nr.GaiaReader(str(tmp_path))(float(ra_missing), float(dec_missing), 1.0)["source_id"].size == 0


def test_gaia_reader_feeds_the_provider_with_its_epoch(tmp_path, make_provider):
    detection = make_provider().get_detection(9001)
    write_gaia_store(tmp_path, [{**FULL_STAR, "ra": detection.ra + ra_offset(detection.dec, 0.4),
                                 "dec": detection.dec, "pmra": 0.0, "pmdec": 0.0}],
                     ref_epoch=2017.5)
    reader = nr.GaiaReader(str(tmp_path))
    provider = make_provider(gaia_reader=reader)
    assert provider.gaia_ref_epoch == 2017.5                      # taken from the copy
    (match,) = provider.get_gaia_matches(detection)
    assert match.source_id == FULL_STAR["source_id"] and match.sep == pytest.approx(0.4, abs=0.01)


def test_gaia_reader_refuses_a_missing_copy(tmp_path):
    with pytest.raises(FileNotFoundError, match="ingest-gaia"):
        nr.GaiaReader(str(tmp_path))
