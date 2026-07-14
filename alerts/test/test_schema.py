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

import fastavro
import pytest

from rapid_alerts.gen_schema import generate
from rapid_alerts.param_registry import RECORDS, VERSION, Status
from rapid_alerts.produce import (assemble_alert, build_dia_source,
                                  build_dia_forced_source, load_schema,
                                  serialize_alert)
from rapid_alerts.providers import (AlertDataProvider, Cutouts, ForcedPhot,
                                    ObjectRecord, Source)


def make_detection(sid, mjd, aid=None):
    return Source(
        sid=sid, expid=42, sca=7, mjdobs=mjd, ra=150.1, dec=2.2,
        xfit=101.5, yfit=202.5, band="F158", aid=aid,
        xerr=0.01, yerr=0.02, fluxfit=1234.5, fluxerr=56.7,
        flags=0, field=3, hp6=123, hp9=4567, pid=99, isdiffpos=True,
        qfit=0.1, cfit=0.05, redchi=1.2, npixfit=25,
        sharpness=0.4, roundness1=0.1, roundness2=-0.05, peak=321.0,
    )


class MinimalProvider(AlertDataProvider):
    """Hand-rolled records; no database, no files, no cutout machinery."""

    def get_detection(self, sid):
        return make_detection(sid, mjd=60500.5)

    def get_object_for_source(self, detection):
        return ObjectRecord(aid=777, ra0=150.1, dec0=2.2, nsources=3)

    def get_prv_detections(self, detection, obj, window_days=365.25):
        return [make_detection(1001, mjd=60480.5, aid=obj.aid),
                make_detection(1002, mjd=60490.5, aid=obj.aid)]

    def get_forced_photometry(self, detection, obj):
        return []

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
    assert alert["diaSource"]["npixfit"] == 25
    assert alert["diaSource"]["diaObjectId"] == 777
    assert abs(alert["diaSource"]["snr"] - 1234.5 / 56.7) < 1e-6
    assert alert["diaObject"]["nDiaSources"] == 3
    # first/last/validity MJDs are computed from the detection history
    assert alert["diaObject"]["firstDiaSourceMjdTai"] == 60480.5
    assert alert["diaObject"]["lastDiaSourceMjdTai"] == 60500.5
    assert alert["diaObject"]["validityStartMjdTai"] == 60500.5
    assert len(alert["prvDiaSources"]) == 2
    assert alert["prvDiaForcedSources"] is None
    assert alert["cutoutDifference"] == b"FAKE_DIFF"
    assert alert["cutoutTemplate"] is None


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
                    dec=2.2, mjdobs=60500.5, time_processed=60500.6,
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
