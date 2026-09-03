"""Reference-catalog cross-match tests.

Three layers, mirroring test_ss_match.py:
  - load_refcat() / match_refcat() geometry and selection: pure functions
    over synthetic SExtractor catalogs (conftest.write_sextractor_refcat);
    sep/pa cross-checked against the KONA matcher's independent Vincenty
    implementation.
  - The star/galaxy partition and its subset -> original-row index
    mapping (the classic off-by-one-level bug: KD-tree indices are
    positions within a class subset, not catalog rows).
  - End-to-end over the fake chip (conftest FakeDB): assemble + serialize
    alerts for all three refStarMatches/refGalaxyMatches states -- null
    (matching not run), [] (ran and found nothing), populated -- through
    the real provider, SQL routing (pid -> rfid -> refimcatalogs), and
    staging path.

Live-database counterparts are in test_live_db.py.
"""

import io
import math

import fastavro
import numpy as np
import pytest

from alerts.produce import assemble_alert, load_schema, serialize_alert
from alerts.providers import (REF_MATCH_NMAX, REF_MATCH_RADIUS_ARCSEC,
                              REFCAT_PIXEL_SCALE_ARCSEC,
                              REFCAT_STAR_MIN_CLASS, load_refcat,
                              match_refcat, match_ss_predictions)
from conftest import write_sextractor_refcat

STAR = REFCAT_STAR_MIN_CLASS + 0.05      # safely star-classified
GALAXY = REFCAT_STAR_MIN_CLASS - 0.5     # safely galaxy-classified


def ra_offset(dec, sep_arcsec):
    """Degrees of RA giving `sep_arcsec` of separation at declination dec."""
    return sep_arcsec / 3600.0 / math.cos(math.radians(dec))


def make_refcat(tmp_path, entries, name="refcat.txt"):
    return load_refcat(write_sextractor_refcat(tmp_path / name, entries))


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def test_load_refcat_parses_and_partitions(tmp_path):
    cat = make_refcat(tmp_path, [
        {"ra": 150.0, "dec": -20.0, "class_star": STAR},
        {"ra": 150.1, "dec": -20.0, "class_star": GALAXY},
        {"ra": 150.2, "dec": -20.0, "class_star": GALAXY},
    ])
    assert len(cat.rows["star"]) == 1
    assert len(cat.rows["galaxy"]) == 2
    # subset -> original-row mapping points back into the full catalog
    assert list(cat.rows["star"]) == [0]
    assert list(cat.rows["galaxy"]) == [1, 2]
    assert cat.columns["NUMBER"][0] == 1.0   # NUMBER assigned in file order


def test_load_refcat_unreadable_and_missing_columns(tmp_path):
    assert load_refcat(str(tmp_path / "nope.txt")) is None

    bad = tmp_path / "bad.txt"
    bad.write_text("#   1 NUMBER\n#   2 ALPHAWIN_J2000\n1 150.0\n")
    assert load_refcat(str(bad)) is None     # missing expected columns


def test_load_refcat_empty_class_gives_none_coords(tmp_path):
    cat = make_refcat(tmp_path, [
        {"ra": 150.0, "dec": -20.0, "class_star": GALAXY},
    ])
    assert cat.coords["star"] is None
    assert cat.rows["star"].size == 0


# ---------------------------------------------------------------------------
# Geometry: sep/pa against the KONA matcher's independent implementation
# ---------------------------------------------------------------------------

def test_geometry_against_ss_matcher(tmp_path):
    rng = np.random.default_rng(7)
    for _ in range(25):
        ra0 = rng.uniform(0, 360)
        dec0 = rng.uniform(-85, 85)
        dra = rng.uniform(-4, 4) / 3600.0 / np.cos(np.radians(dec0))
        ddec = rng.uniform(-4, 4) / 3600.0
        pra, pdec = (ra0 + dra) % 360.0, float(np.clip(dec0 + ddec, -90, 90))

        cat = make_refcat(tmp_path, [
            {"ra": pra, "dec": pdec, "class_star": GALAXY}])
        (stars, galaxies), = match_refcat(ra0, dec0, cat,
                                          radius_arcsec=1e6)
        assert stars == []
        assert len(galaxies) == 1
        want, = match_ss_predictions(ra0, dec0, {"X": (pra, pdec, None)},
                                     radius_arcsec=1e6)
        assert galaxies[0].sep == pytest.approx(want.sep, abs=1e-6)
        if want.sep > 1e-3:                # PA undefined at zero separation
            dpa = (galaxies[0].pa - want.pa + 180.0) % 360.0 - 180.0
            assert abs(dpa) < 1e-6


# ---------------------------------------------------------------------------
# Selection: radius mask, nearest-N ordering, per-class capping
# ---------------------------------------------------------------------------

def test_selection_ordering_and_radius(tmp_path):
    ra0, dec0 = 150.0, -20.0
    off = lambda s: ra_offset(dec0, s)
    cat = make_refcat(tmp_path, [
        # galaxies at 1..4": the 4th nearest is over n_max
        {"ra": ra0 + off(1.0), "dec": dec0, "class_star": GALAXY, "mag": 21.0},
        {"ra": ra0 + off(2.0), "dec": dec0, "class_star": GALAXY, "mag": 22.0},
        {"ra": ra0 + off(3.0), "dec": dec0, "class_star": GALAXY, "mag": 23.0},
        {"ra": ra0 + off(4.0), "dec": dec0, "class_star": GALAXY, "mag": 24.0},
        # one star inside the radius, one far outside
        {"ra": ra0 + off(2.5), "dec": dec0, "class_star": STAR, "mag": 18.0},
        {"ra": ra0 + off(30.0), "dec": dec0, "class_star": STAR, "mag": 17.0},
    ])
    (stars, galaxies), = match_refcat(ra0, dec0, cat)

    assert [m.mag_auto for m in galaxies] == [21.0, 22.0, 23.0]
    assert all(a.sep <= b.sep for a, b in zip(galaxies, galaxies[1:]))
    assert len(galaxies) == REF_MATCH_NMAX
    assert all(m.sep <= REF_MATCH_RADIUS_ARCSEC for m in galaxies)
    assert [m.mag_auto for m in stars] == [18.0]
    assert stars[0].sep == pytest.approx(2.5, abs=0.01)
    assert stars[0].pa == pytest.approx(90.0, abs=0.1)   # due East


def test_index_mapping_with_interleaved_classes(tmp_path):
    """KD-tree indices are subset positions; the matched rows must carry
    the *original* catalog row's photometry. Interleave classes so a
    subset-for-row confusion cannot produce the right answer."""
    ra0, dec0 = 150.0, 0.0
    off = lambda s: ra_offset(dec0, s)
    entries = []
    for k in range(6):     # star, galaxy, star, galaxy, ... at 1..6"
        entries.append({"ra": ra0 + off(1.0 + k), "dec": dec0,
                        "class_star": STAR if k % 2 == 0 else GALAXY,
                        "mag": 20.0 + k})
    cat = make_refcat(tmp_path, entries)
    (stars, galaxies), = match_refcat(ra0, dec0, cat, radius_arcsec=10.0)

    # nearest stars are rows 0, 2, 4 (mags 20, 22, 24); NUMBER is 1-based
    assert [m.mag_auto for m in stars] == [20.0, 22.0, 24.0]
    assert [m.source_id for m in stars] == ["1", "3", "5"]
    assert [m.mag_auto for m in galaxies] == [21.0, 23.0, 25.0]
    assert [m.source_id for m in galaxies] == ["2", "4", "6"]


def test_fewer_catalog_rows_than_nmax(tmp_path):
    ra0, dec0 = 150.0, -20.0
    cat = make_refcat(tmp_path, [
        {"ra": ra0 + ra_offset(dec0, 1.0), "dec": dec0, "class_star": STAR},
    ])
    (stars, galaxies), = match_refcat(ra0, dec0, cat)
    assert len(stars) == 1                 # nthneighbor capped, no error
    assert galaxies == []


def test_batch_equals_single(tmp_path):
    rng = np.random.default_rng(11)
    dec0 = -20.0
    entries = [{"ra": 150.0 + ra_offset(dec0, rng.uniform(-6, 6)),
                "dec": dec0 + rng.uniform(-6, 6) / 3600.0,
                "class_star": STAR if i % 3 == 0 else GALAXY}
               for i in range(20)]
    cat = make_refcat(tmp_path, entries)
    src_ra = [150.0, 150.001, 149.999]
    src_dec = [dec0, dec0 + 5e-4, dec0 - 5e-4]

    batch = match_refcat(src_ra, src_dec, cat)
    for i, (ra, dec) in enumerate(zip(src_ra, src_dec)):
        single, = match_refcat(ra, dec, cat)
        for got, want in zip(batch[i], single):
            assert [m.source_id for m in got] == [m.source_id for m in want]
            assert [m.sep for m in got] == pytest.approx(
                [m.sep for m in want], abs=1e-9)


def test_mag_sentinel_becomes_null(tmp_path):
    ra0, dec0 = 150.0, -20.0
    cat = make_refcat(tmp_path, [
        {"ra": ra0, "dec": dec0, "class_star": GALAXY,
         "mag": 99.0, "mag_err": 99.0},
    ])
    (_, galaxies), = match_refcat(ra0, dec0, cat)
    assert galaxies[0].mag_auto is None
    assert galaxies[0].mag_err_auto is None


def test_pixel_sizes_converted_to_arcsec(tmp_path):
    ra0, dec0 = 150.0, -20.0
    cat = make_refcat(tmp_path, [
        {"ra": ra0, "dec": dec0, "class_star": GALAXY,
         "fwhm_pix": 3.0, "flux_radius_pix": 2.0},
    ])
    (_, galaxies), = match_refcat(ra0, dec0, cat)
    assert galaxies[0].fwhm == pytest.approx(3.0 * REFCAT_PIXEL_SCALE_ARCSEC)
    assert galaxies[0].half_light_radius == pytest.approx(
        2.0 * REFCAT_PIXEL_SCALE_ARCSEC)


# ---------------------------------------------------------------------------
# End to end over the fake chip (real provider, SQL routing, staging)
# ---------------------------------------------------------------------------

@pytest.fixture()
def trigger_position(make_provider):
    """The fake chip's sid 9001 sky position (from the TPV WCS)."""
    detection = make_provider().get_detection(9001)
    return detection.ra, detection.dec


@pytest.fixture()
def chip_refcat(chip_data, tmp_path, trigger_position):
    """Register a catalog around sid 9001: one star at 1", galaxies at
    2" and 3", and both classes far outside the radius."""
    ra, dec = trigger_position
    off = lambda s: ra_offset(dec, s)
    path = write_sextractor_refcat(tmp_path / "refimsexcat.txt", [
        {"ra": ra + off(1.0), "dec": dec, "class_star": STAR, "mag": 18.5},
        {"ra": ra + off(2.0), "dec": dec, "class_star": GALAXY, "mag": 21.0},
        {"ra": ra + off(3.0), "dec": dec, "class_star": GALAXY, "mag": 22.0},
        {"ra": ra + off(40.0), "dec": dec, "class_star": STAR},
        {"ra": ra + off(50.0), "dec": dec, "class_star": GALAXY},
    ])
    chip_data.refcat_filename = path
    return path


def test_e2e_not_run_without_catalog(make_provider):
    """No refimcatalogs row (the ChipData default) -> arrays stay null."""
    alert = assemble_alert(make_provider(), 9001)
    assert alert["refStarMatches"] is None
    assert alert["refGalaxyMatches"] is None


def test_e2e_disabled_provider_stays_null(make_provider, chip_refcat):
    alert = assemble_alert(make_provider(refcat=False), 9001)
    assert alert["refStarMatches"] is None
    assert alert["refGalaxyMatches"] is None


def test_e2e_ran_clean_is_empty_not_null(make_provider, chip_data,
                                         trigger_position, tmp_path):
    ra, dec = trigger_position
    chip_data.refcat_filename = write_sextractor_refcat(
        tmp_path / "far.txt",
        [{"ra": ra + 1.0, "dec": dec, "class_star": GALAXY}])
    alert = assemble_alert(make_provider(), 9001)
    assert alert["refStarMatches"] == []
    assert alert["refGalaxyMatches"] == []


def test_e2e_matched_and_serialized(make_provider, chip_refcat):
    alert = assemble_alert(make_provider(), 9001)

    assert [m["magAuto"] for m in alert["refStarMatches"]] == [18.5]
    assert alert["refStarMatches"][0]["sep"] == pytest.approx(1.0, abs=0.01)
    assert [m["magAuto"] for m in alert["refGalaxyMatches"]] == [21.0, 22.0]
    assert all(m["classStar"] < REFCAT_STAR_MIN_CLASS
               for m in alert["refGalaxyMatches"])

    schema = load_schema()
    decoded = fastavro.schemaless_reader(
        io.BytesIO(serialize_alert(alert, schema=schema)), schema)
    assert decoded["refStarMatches"][0]["sourceId"] == \
        alert["refStarMatches"][0]["sourceId"]
    assert decoded["refGalaxyMatches"][1]["magAuto"] == pytest.approx(22.0)


def test_e2e_batch_flow_matches_all_sources(make_provider, chip_refcat):
    """iter_sources() matches the whole chip in one pass; every source
    answers from the prefetch, and the per-source results agree with the
    single-alert flow from an independent provider."""
    provider = make_provider()
    sources = list(provider.iter_sources(99))
    assert len(sources) == 3

    fresh = make_provider()                # single-alert flow, no prefetch
    for source in sources:
        batch = provider.get_ref_matches(source)
        single = fresh.get_ref_matches(fresh.get_detection(source.sid))
        assert batch is not None and single is not None
        for got, want in zip(batch, single):
            assert [m.source_id for m in got] == [m.source_id for m in want]
