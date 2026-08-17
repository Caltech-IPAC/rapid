"""Section C of the test plan: schema registry consistency, alert
assembly semantics, and Avro serialization (migrated from the old
test_rapid_alerts.py script; same coverage, one test per concern).

These run against a minimal hand-rolled AlertDataProvider rather than the
fake database -- assembly semantics don't care where records come from,
and the simpler provider keeps failures here pointing at produce.py, not
at fixture plumbing.

TODO (test plan, not yet implemented):
  C13 load_schema staleness detection: stale latest.txt / drifted file /
      missing file each raise the clear RuntimeError; explicit old
      version still loads without the check
  C14 value-domain round-trip: NaN policy in nullable floats, very
      large/negative fluxes, isdiffpos=False -> isNegative=True
  C15 size guard: an alert with three real-size clips and a fat prv
      history stays well under Kafka's configured message.max.bytes
"""

import io
import time

import fastavro
import pytest

from alerts.gen_schema import generate
from alerts.param_registry import RECORDS, VERSION, Status
from alerts.produce import (assemble_alert, build_dia_source,
                                  build_dia_forced_source, load_schema,
                                  serialize_alert)
from alerts.providers import (Cutouts, ForcedPhot, ObjectRecord,
                                    RefMatch, Source, SSMatch)


def make_detection(sid, mjd, aid=None):
    return Source(
        sid=sid, expid=42, sca=7, mjdobs=mjd, ra=150.1, dec=2.2,
        xfit=101.5, yfit=202.5, band="F158", aid=aid,
        xerr=0.01, yerr=0.02, fluxfit=1234.5, fluxerr=56.7,
        flags=0, field=3, hp6=123, hp9=4567, pid=99, isdiffpos=True,
        qfit=0.1, cfit=0.05, redchi=1.2, npixfit=25,
        sharpness=0.4, roundness1=0.1, roundness2=-0.05, peak=321.0,
    )


class MinimalProvider:
    """Hand-rolled records; no database, no files, no cutout machinery.

    Duck-typed: assemble_alert() only calls these get_* methods, so this
    needs no provider base class."""

    def get_detection(self, sid):
        return make_detection(sid, mjd=60500.5)

    def get_object_for_source(self, detection):
        return ObjectRecord(aid=777, ra0=150.1, dec0=2.2,
                            stdevra=1.5e-05, stdevdec=1.2e-05, nsources=3)

    def get_prv_detections(self, detection, obj, window_days=365.25):
        return [make_detection(1001, mjd=60480.5, aid=obj.aid),
                make_detection(1002, mjd=60490.5, aid=obj.aid)]

    def get_forced_photometry(self, detection, obj):
        return []

    def get_ss_matches(self, detection):
        # mirror the real provider's contract: return the match list and
        # set the candidate flag on the detection as a side effect
        detection.is_ss_candidate = True
        return [SSMatch(designation="2005 QP87", ra=150.1001, dec=2.2,
                        sep=0.36, pa=90.0, predvmag=21.7)]

    def get_ref_matches(self, detection):
        # (star matches, galaxy matches), mirroring the real provider
        star = RefMatch(source_id="42", ra=150.10005, dec=2.2, sep=0.4,
                        pa=90.0, class_star=0.97, flags=0,
                        mag_auto=18.5, mag_err_auto=0.02, elong=1.05,
                        fwhm=0.31, half_light_radius=0.15, kron_radius=3.5)
        galaxy = RefMatch(source_id="77", ra=150.1004, dec=2.2, sep=1.5,
                          pa=270.0, class_star=0.03, flags=2,
                          mag_auto=21.2, mag_err_auto=0.1, elong=1.6,
                          fwhm=0.8, half_light_radius=0.6, kron_radius=4.1)
        return [star], [galaxy]

    def get_cutouts(self, detection):
        return Cutouts(difference=b"FAKE_DIFF", science=b"FAKE_SCI",
                       template=None)


@pytest.fixture(scope="module")
def alert():
    return assemble_alert(MinimalProvider(), 9999)


def test_committed_avsc_files_match_registry():
    assert generate(check=True), ".avsc files differ from param_registry.py"


def test_assembled_alert_semantics(alert):
    assert alert["diaSourceId"] == 9999
    assert alert["schemaVersion"] == VERSION
    assert alert["diaSource"]["isNegative"] is False   # isdiffpos inverted
    assert alert["diaSource"]["psfNdata"] == 25
    assert alert["diaSource"]["diaObjectId"] == 777
    assert abs(alert["diaSource"]["snr"] - 1234.5 / 56.7) < 1e-6
    assert alert["diaObject"]["nDiaSources"] == 3
    # first/last/validity MJDs are computed from the detection history
    assert alert["diaObject"]["firstDiaSourceMjd"] == 60480.5
    assert alert["diaObject"]["lastDiaSourceMjd"] == 60500.5
    assert alert["diaObject"]["validityStartMjd"] == 60500.5
    assert len(alert["prvDiaSources"]) == 2
    assert alert["prvDiaForcedSources"] is None
    assert alert["cutoutDifference"] == b"FAKE_DIFF"
    assert alert["cutoutReference"] is None
    # solar-system association: matched path
    assert alert["diaSource"]["isSSCandidate"] is True
    assert alert["ssMatches"][0]["designation"] == "2005 QP87"
    assert alert["ssMatches"][0]["predVMag"] == pytest.approx(21.7)
    # prv sources were never associated -> their flag stays null
    assert all(p["isSSCandidate"] is None for p in alert["prvDiaSources"])
    # reference-catalog cross-match: matched path
    assert alert["refStarMatches"][0]["sourceId"] == "42"
    assert alert["refStarMatches"][0]["classStar"] == pytest.approx(0.97)
    assert alert["refGalaxyMatches"][0]["magAuto"] == pytest.approx(21.2)


def test_time_processed_stamped_at_assembly():
    """timeProcessedMjd is the assembly-time UTC MJD, bracket-checked.

    The bracket converts the Unix clock to MJD independently of the
    astropy path produce.py uses (Unix epoch 1970-01-01 = MJD 40587),
    so a wrong epoch or time scale in the stamping would fail here."""
    def unix_now_mjd():
        return time.time() / 86400.0 + 40587.0

    before = unix_now_mjd()
    alert = assemble_alert(MinimalProvider(), 9999)
    after = unix_now_mjd()

    assert before <= alert["diaSource"]["timeProcessedMjd"] <= after


def test_ref_match_not_run_stays_null():
    """refStarMatches/refGalaxyMatches = None must mean "not run"."""
    provider = MinimalProvider()
    provider.get_ref_matches = lambda detection: None
    alert = assemble_alert(provider, 9999)
    assert alert["refStarMatches"] is None
    assert alert["refGalaxyMatches"] is None


def test_ref_match_ran_clean_is_empty_not_null():
    provider = MinimalProvider()
    provider.get_ref_matches = lambda detection: ([], [])
    alert = assemble_alert(provider, 9999)
    assert alert["refStarMatches"] == []
    assert alert["refGalaxyMatches"] == []


def test_ss_association_not_run_stays_null():
    """ssMatches=None must mean "not run", never "ran and found nothing"."""
    provider = MinimalProvider()
    provider.get_ss_matches = lambda detection: None
    alert = assemble_alert(provider, 9999)
    assert alert["ssMatches"] is None
    assert alert["diaSource"]["isSSCandidate"] is None


def test_ss_association_ran_clean_is_empty_not_null():
    provider = MinimalProvider()

    def ran_clean(detection):
        detection.is_ss_candidate = False
        return []
    provider.get_ss_matches = ran_clean
    alert = assemble_alert(provider, 9999)
    assert alert["ssMatches"] == []
    assert alert["diaSource"]["isSSCandidate"] is False


def test_stub_params_stay_null(alert):
    by_name = {r.name: r for r in RECORDS}
    for param in by_name["diaSource"].params:
        if param.status is Status.STUB:
            assert alert["diaSource"][param.name] is None, param.name


def test_non_nullable_implemented_param_with_none_raises():
    bad = make_detection(9999, mjd=60500.5)
    bad.sid = None                        # diaSourceId is non-nullable
    with pytest.raises(ValueError, match="diaSourceId"):
        build_dia_source(bad)


def test_stub_params_null_even_with_value_staged():
    fp = ForcedPhot(forced_id=1, aid=777, expid=42, sca=7, ra=150.1,
                    dec=2.2, mjdobs=60500.5, time_proc=60500.6,
                    flux=123.4)
    assert all(v is None for v in build_dia_forced_source(fp).values())


def test_strict_from_row_rejects_incomplete_rows():
    with pytest.raises(KeyError, match="fluxfit"):
        Source.from_row({"sid": 1, "expid": 42}, strict=True)


def test_avro_round_trip(alert):
    schema = load_schema()
    blob = serialize_alert(alert, schema=schema)
    decoded = fastavro.schemaless_reader(io.BytesIO(blob), schema)
    assert decoded["diaSourceId"] == 9999
    assert decoded["diaObject"]["diaObjectId"] == 777
    assert len(decoded["prvDiaSources"]) == 2
    assert decoded["cutoutDifference"] == b"FAKE_DIFF"
