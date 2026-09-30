"""Roman L2 metadata contract for KONA (modules/solarsystem/rapid_kona.py).

KONA builds the observer state from ``meta.ephemeris`` and the field of
view from ``meta.wcsinfo``, on the understanding -- from the Roman
datamodel (RAD) ``ephemeris`` schema -- that ``spatial_x/y/z`` and
``velocity_x/y/z`` are *barycentric* (solar system barycenter) vectors in
km and km/s. Read them as anything else (Earth-relative, as the code did
before 2026-09-29, or heliocentric) and every predicted asteroid position
is wrong by up to degrees while the code runs without complaint.

These tests pin that reading against real L2 files. Which files is a
command-line choice::

    pytest alerts/test/test_roman_ephemeris.py                      # pinned SOC sim
    pytest ... --roman-asdf /path/to/file.asdf --roman-asdf s3://b/k.asdf.gz

The default is the SOC-simulation L2 behind test_live_db.ROUNDTRIP_SID
(expid 84776, SCA 9), which needs AWS credentials for the sim bucket;
without them (or kete) the tests warn and skip, fail under
``--require-live``. Any Roman L2 with the same metadata layout as the SOC
sims can be checked the same way -- run this on every new simulation and
on the first flight files before trusting KONA output on them.

If a file fails here, do NOT loosen the tolerances: its ephemeris means
something else, and KONA's observer construction has to be revisited for
it (see the TODO in rapid_kona.kona()).
"""

import gzip
import math
import os
import warnings
from pathlib import Path

import pytest

asdf = pytest.importorskip("asdf")
kete = pytest.importorskip("kete")

# Default file: the L2 behind test_live_db.ROUNDTRIP_SID.
DEFAULT_SOURCE = ("s3://socsims-fakesrc-asdf-20260807/"
                  "r0034001004001033018_0001_wfi09_f146_cal_lite.asdf.gz")
AWS_ENV_VARS = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")

AU_KM = kete.constants.AU_KM

# Roman orbits Sun-Earth L2, ~1.5e6 km from Earth, on a halo of a few
# 1e5 km; the SOC sim places it ~1e3 km from Earth. Anything within a few
# million km is "at Earth" for this purpose; the readings we guard
# against are wrong by 1 AU (1.5e8 km).
OBSERVER_EARTH_MAX_KM = 4.0e6
# Relative velocity on the L2 halo is < 1 km/s; Earth-relative or
# heliocentric misreadings are wrong by ~30 km/s.
OBSERVER_EARTH_MAX_KM_S = 2.0
# A barycentric Roman vector has |r| ~ 1 AU; an Earth-relative one ~0.01.
DISTANCE_AU_RANGE = (0.97, 1.03)


def pytest_generate_tests(metafunc):
    """One test instance per --roman-asdf source (default: the pinned sim)."""
    if "asdf_source" in metafunc.fixturenames:
        sources = metafunc.config.getoption("--roman-asdf") or [DEFAULT_SOURCE]
        metafunc.parametrize("asdf_source", sources, scope="module",
                             ids=[Path(s).name for s in sources])


def _fetch(source, workdir):
    """Materialize `source` (local path or s3://bucket/key, maybe .gz) as
    an uncompressed .asdf under `workdir`; returns the path or raises."""
    if source.startswith("s3://"):
        missing = [v for v in AWS_ENV_VARS if not os.getenv(v)]
        if missing:
            raise RuntimeError("environment variables not set: "
                               + ", ".join(missing))
        import boto3
        bucket, key = source[5:].split("/", 1)
        raw = boto3.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()
        name = Path(key).name
    else:
        raw = Path(source).read_bytes()
        name = Path(source).name
    if name.endswith(".gz"):
        raw, name = gzip.decompress(raw), name[:-3]
    path = workdir / name
    path.write_bytes(raw)
    return path


@pytest.fixture(scope="module")
def sim_tree(request, asdf_source, tmp_path_factory):
    """The L2 ASDF tree for one source, fetched once per module."""
    try:
        path = _fetch(asdf_source, tmp_path_factory.mktemp("roman_asdf"))
    except Exception as exc:
        message = f"Roman ephemeris tests NOT run for {asdf_source}: {exc}"
        if request.config.getoption("--require-live"):
            pytest.fail(message)
        warnings.warn(message)
        pytest.skip(str(exc))
    # The roman/gwcs extensions may not be installed; unknown tags load as
    # plain dicts, which is all the metadata reads below need.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with asdf.open(path) as tree:
            yield tree


@pytest.fixture(scope="module")
def meta(sim_tree):
    return sim_tree["roman"]["meta"]


def observer_from_ephemeris(meta):
    """Build the heliocentric observer state the way rapid_kona.kona() does.

    Mirrors the km -> AU / km/s -> AU/day conversion, the frame choice,
    and the barycentric (NAIF 0) -> Sun (NAIF 10) re-centering; kept in
    step with kona() by hand since that function also fetches the MPC
    catalog and cannot run offline.
    """
    eph = meta["ephemeris"]
    t = kete.Time.from_iso(meta["exposure"]["mid_time"] + "Z")
    frame = (kete.Frames.Ecliptic
             if eph["ephemeris_reference_frame"] == "Ecliptic"
             else kete.Frames.Equatorial)
    vel_au_day = [eph[f"velocity_{a}"] / AU_KM * 86400.0 for a in "xyz"]
    # the header vector is at ephemeris.time; the observer must be at
    # mid_time, so slide the position along the velocity (as kona() does)
    dt_days = t.jd - kete.Time.from_mjd(eph["time"], scaling="utc").jd
    pos = kete.Vector([eph[f"spatial_{a}"] / AU_KM + v * dt_days
                       for a, v in zip("xyz", vel_au_day)], frame=frame)
    vel = kete.Vector(vel_au_day, frame=frame)
    return kete.State("Roman", t, pos, vel, center_id=0).change_center(10), t


def _km(vec_a, vec_b):
    return math.sqrt(sum((float(a) - float(b)) ** 2
                         for a, b in zip(vec_a, vec_b))) * AU_KM


# ---------------------------------------------------------------------------
# Metadata layout: the keys KONA reads, under the names it uses
# ---------------------------------------------------------------------------

def test_kona_metadata_keys_present(meta):
    """Every key kona() dereferences exists. Guards the 'pointng' class
    of bug: a misspelled key raises KeyError on the first file."""
    assert "pointing" in meta and "pointng" not in meta
    assert {"ra_v1", "dec_v1"} <= set(meta["pointing"])
    assert {"ra_ref", "dec_ref", "s_region"} <= set(meta["wcsinfo"])
    assert {"mid_time", "start_time", "end_time"} <= set(meta["exposure"])
    eph = meta["ephemeris"]
    for key in ("spatial_x", "spatial_y", "spatial_z",
                "velocity_x", "velocity_y", "velocity_z",
                "time", "ephemeris_reference_frame"):
        assert key in eph, key


def test_wcsinfo_ref_is_on_this_sca(meta):
    """wcsinfo.ra_ref/dec_ref is the per-SCA reference point kona() now
    centers its cone on; it must lie inside the SCA's own s_region and
    away from the V1 boresight (the WFI is off-axis)."""
    ra_ref, dec_ref = meta["wcsinfo"]["ra_ref"], meta["wcsinfo"]["dec_ref"]
    ra_v1, dec_v1 = meta["pointing"]["ra_v1"], meta["pointing"]["dec_v1"]
    # kete's angle_between returns degrees
    v1_sep_deg = kete.Vector.from_ra_dec(ra_ref, dec_ref).angle_between(
        kete.Vector.from_ra_dec(ra_v1, dec_v1))
    # V1 is ~0.5 deg from the array center and up to ~0.75 deg from an
    # edge SCA. If this ever comes out < 0.2 deg, V1 *is* the chip center
    # and the cone choice in kona() should be revisited.
    assert 0.2 < v1_sep_deg < 1.0, v1_sep_deg
    # inside the chip's own footprint (POLYGON ICRS ra dec ra dec ...)
    coords = [float(x) for x in meta["wcsinfo"]["s_region"].split()[2:]]
    ras, decs = coords[0::2], coords[1::2]
    assert min(ras) <= ra_ref <= max(ras)
    assert min(decs) <= dec_ref <= max(decs)


# ---------------------------------------------------------------------------
# Ephemeris meaning: barycentric km / km/s, per the RAD schema
# ---------------------------------------------------------------------------

def test_ephemeris_vector_is_one_au_long(meta):
    """A barycentric Roman position is ~1 AU long. An Earth-relative one
    (what the pre-2026-09-29 code assumed) would be ~0.01 AU."""
    eph = meta["ephemeris"]
    r_au = math.sqrt(sum(eph[f"spatial_{a}"] ** 2 for a in "xyz")) / AU_KM
    assert DISTANCE_AU_RANGE[0] < r_au < DISTANCE_AU_RANGE[1], r_au


def test_ephemeris_is_barycentric(meta):
    """Read as barycentric and re-centered on the Sun, the observer sits
    at Earth (L2 distance at most) and moves with it. This is the reading
    rapid_kona.kona() relies on."""
    obs, t = observer_from_ephemeris(meta)
    earth = kete.spice.get_state("Earth", t.jd).as_equatorial
    obs_eq = obs.as_equatorial
    assert obs.center_id == 10
    d_km = _km(obs_eq.pos, earth.pos)
    v_km_s = _km(obs_eq.vel, earth.vel) / 86400.0
    assert d_km < OBSERVER_EARTH_MAX_KM, f"{d_km:,.0f} km from Earth"
    assert v_km_s < OBSERVER_EARTH_MAX_KM_S, f"{v_km_s:.2f} km/s vs Earth"


def test_ephemeris_is_not_earth_relative(meta):
    """The reading the old code used -- ephemeris + Earth's heliocentric
    state -- must be grossly wrong on this file, so that this suite
    would have caught the bug. If this ever passes, the file's ephemeris
    is Earth-relative and kona() needs the old construction back."""
    eph = meta["ephemeris"]
    t = kete.Time.from_iso(meta["exposure"]["mid_time"] + "Z")
    earth = kete.spice.get_state("Earth", t.jd).as_equatorial
    summed = [float(e) + eph[f"spatial_{a}"] / AU_KM
              for e, a in zip(earth.pos, "xyz")]
    d_km = _km(summed, earth.pos)
    assert d_km > 0.5 * AU_KM, f"only {d_km:,.0f} km off: Earth-relative?"


def test_ephemeris_time_is_within_the_exposure(meta):
    """ephemeris.time should fall within the exposure. The SOC sim stamps
    it at the start, which is why the observer lands ~1e3 km (33 s x
    30 km/s) from Earth's mid-time position rather than on it.

    ephemeris.time is a UTC MJD per the RAD schema; kete.Time.mjd is
    TDB (69 s later), so the comparison is done in UTC with astropy."""
    from astropy.time import Time

    eph_mjd = meta["ephemeris"]["time"]
    start = Time(meta["exposure"]["start_time"], scale="utc").mjd
    end = Time(meta["exposure"]["end_time"], scale="utc").mjd
    assert start - 1e-6 <= eph_mjd <= end + 1e-6, (eph_mjd, start, end)
