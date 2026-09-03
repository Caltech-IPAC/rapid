"""Solar-system association (KONA) tests.

Three layers:
  - match_ss_predictions() geometry and selection: pure functions, no
    provider (sep/pa validated against astropy SkyCoord).
  - load_kona_predictions(): the nightly-predictions JSON loader used by
    `alerts.cli --kona-file`.
  - End-to-end over the fake chip (conftest FakeDB): assemble + serialize
    alerts for all three ssMatches states -- null (association not run),
    [] (ran and found nothing), populated (matches; candidate flag set) --
    through the real provider, SQL routing, and cutout path.

Live-database counterparts are in test_live_db.py.
"""

import io
import json
import math

import fastavro
import numpy as np
import pytest

from alerts.cli import load_kona_predictions
from alerts.produce import (assemble_alert, load_schema, serialize_alert)
from alerts.providers import (SS_CANDIDATE_SEP_ARCSEC, SS_MATCH_NMAX,
                              SS_MATCH_RADIUS_ARCSEC, match_ss_predictions)

# the fake chip's exposure (conftest.make_source_row)
CHIP_EXPID = 42


def ra_offset(dec, sep_arcsec):
    """Degrees of RA giving `sep_arcsec` of separation at declination dec."""
    return sep_arcsec / 3600.0 / math.cos(math.radians(dec))


# ---------------------------------------------------------------------------
# Geometry: sep/pa against astropy
# ---------------------------------------------------------------------------

def test_geometry_against_astropy():
    from astropy.coordinates import SkyCoord
    import astropy.units as u

    rng = np.random.default_rng(42)
    for _ in range(200):
        ra0 = rng.uniform(0, 360)
        dec0 = rng.uniform(-85, 85)
        dra = rng.uniform(-20, 20) / 3600.0 / np.cos(np.radians(dec0))
        ddec = rng.uniform(-20, 20) / 3600.0
        pra, pdec = ra0 + dra, float(np.clip(dec0 + ddec, -90, 90))

        got = match_ss_predictions(ra0, dec0, {"X": (pra, pdec, 20.0)},
                                   radius_arcsec=1e6)
        assert len(got) == 1
        c0 = SkyCoord(ra0 * u.deg, dec0 * u.deg)
        cp = SkyCoord(pra * u.deg, pdec * u.deg)
        assert got[0].sep == pytest.approx(c0.separation(cp).arcsec, abs=1e-6)
        want_pa = c0.position_angle(cp).deg % 360.0
        if got[0].sep > 1e-3:             # PA undefined at zero separation
            dpa = (got[0].pa - want_pa + 180.0) % 360.0 - 180.0
            assert abs(dpa) < 1e-6


def test_geometry_across_ra_wrap():
    """Separations must be correct across the RA 0/360 boundary."""
    got = match_ss_predictions(0.0001, 0.0,
                               {"X": (359.9999, 0.0, None)},
                               radius_arcsec=10.0)
    assert len(got) == 1
    assert got[0].sep == pytest.approx(0.72, abs=0.01)   # 0.0002 deg


# ---------------------------------------------------------------------------
# Selection: radius cut, nearest-N, ordering, input tolerance
# ---------------------------------------------------------------------------

def test_selection_and_ordering():
    ra0, dec0 = 150.0, -20.0
    off = lambda s: ra_offset(dec0, s)
    predictions = {
        "near1": (ra0 + off(1.0), dec0, 21.0),
        "near2": (ra0 + off(2.0), dec0, 22.0),
        "near3": (ra0 + off(3.0), dec0, None),   # no catalogued H
        "near4": (ra0 + off(4.0), dec0, 23.0),   # 4th nearest: over n_max
        "far":   (ra0 + 1.0, dec0, 15.0),        # ~1 deg: outside radius
        "old":   (ra0 + off(2.5), dec0),          # pre-vmag 2-tuple
    }
    got = match_ss_predictions(ra0, dec0, predictions)
    assert [m.designation for m in got] == ["near1", "near2", "old"]
    assert all(a.sep <= b.sep for a, b in zip(got, got[1:]))
    assert len(got) <= SS_MATCH_NMAX
    assert got[2].predvmag is None               # 2-tuple tolerated
    assert all(m.sep <= SS_MATCH_RADIUS_ARCSEC for m in got)


def test_empty_predictions_give_empty_list():
    assert match_ss_predictions(150.0, -20.0, {}) == []


# ---------------------------------------------------------------------------
# The nightly-file loader (alerts.cli --kona-file)
# ---------------------------------------------------------------------------

def test_load_kona_predictions(tmp_path):
    path = tmp_path / "kona.json"
    path.write_text(json.dumps({
        "42": {"2005 QP87": [150.1, -20.2, 21.7],
               "2010 AB1": [150.2, -20.3, None]},
        "43": {},
    }))
    loaded = load_kona_predictions(path)
    assert set(loaded) == {42, 43}                # JSON keys -> int expids
    assert loaded[42]["2005 QP87"] == [150.1, -20.2, 21.7]
    assert loaded[43] == {}                       # ran, found nothing
    assert loaded.get(99) is None                 # absent expid -> not run


# ---------------------------------------------------------------------------
# End to end over the fake chip (real provider, SQL routing, cutouts)
# ---------------------------------------------------------------------------

@pytest.fixture()
def trigger_position(make_provider):
    """The fake chip's sid 9001 sky position (from the TPV WCS)."""
    detection = make_provider().get_detection(9001)
    return detection.ra, detection.dec


def test_e2e_association_not_run(make_provider):
    alert = assemble_alert(make_provider(), 9001)
    assert alert["ssMatches"] is None
    assert alert["diaSource"]["isSSCandidate"] is None


def test_e2e_no_kona_data_for_exposure(make_provider):
    # a lookup is configured but has nothing for this exposure
    provider = make_provider(kona_lookup={999999: {}}.get)
    alert = assemble_alert(provider, 9001)
    assert alert["ssMatches"] is None
    assert alert["diaSource"]["isSSCandidate"] is None


def test_e2e_ran_clean(make_provider, trigger_position):
    ra, dec = trigger_position
    far = {CHIP_EXPID: {"far away": (ra + 1.0, dec, 15.0)}}
    alert = assemble_alert(make_provider(kona_lookup=far.get), 9001)
    assert alert["ssMatches"] == []
    assert alert["diaSource"]["isSSCandidate"] is False


def test_e2e_matched_and_serialized(make_provider, trigger_position):
    ra, dec = trigger_position
    sep_in = 0.5 * SS_CANDIDATE_SEP_ARCSEC
    sep_out = 0.5 * (SS_CANDIDATE_SEP_ARCSEC + SS_MATCH_RADIUS_ARCSEC)
    predictions = {CHIP_EXPID: {
        "2005 QP87": (ra + ra_offset(dec, sep_in), dec, 21.7),
        "2010 AB1": (ra + ra_offset(dec, sep_out), dec, 24.1),
    }}
    alert = assemble_alert(make_provider(kona_lookup=predictions.get), 9001)

    assert [m["designation"] for m in alert["ssMatches"]] == \
        ["2005 QP87", "2010 AB1"]
    assert alert["ssMatches"][0]["sep"] == pytest.approx(sep_in, abs=0.01)
    assert alert["ssMatches"][0]["predVMag"] == pytest.approx(21.7)
    assert alert["diaSource"]["isSSCandidate"] is True
    # prv sources of the object were never associated -> flag stays null
    assert all(p["isSSCandidate"] is None for p in alert["prvDiaSources"])

    schema = load_schema()
    decoded = fastavro.schemaless_reader(
        io.BytesIO(serialize_alert(alert, schema=schema)), schema)
    assert decoded["ssMatches"][0]["designation"] == "2005 QP87"
    assert decoded["diaSource"]["isSSCandidate"] is True
