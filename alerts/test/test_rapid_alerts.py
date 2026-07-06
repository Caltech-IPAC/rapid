#!/usr/bin/env python
"""
End-to-end smoke test for the rapid_alerts package -- no database needed.

Feeds a fake provider through assemble -> serialize -> deserialize and
checks the round trip, plus registry/.avsc consistency.

Run:
    python alerts/test/test_rapid_alerts.py
"""

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fastavro

from rapid_alerts.assemble import assemble_alert
from rapid_alerts.fields import RECORDS, VERSION, Status
from rapid_alerts.gen_schema import generate
from rapid_alerts.providers.base import AlertDataProvider
from rapid_alerts.records import Detection, ObjectRecord, Cutouts
from rapid_alerts.serialize import load_schema, serialize_alert


def make_detection(sid, mjd, aid=None):
    return Detection(
        sid=sid, expid=42, sca=7, mjdobs=mjd, ra=150.1, dec=2.2,
        xfit=101.5, yfit=202.5, band="F158", aid=aid,
        xerr=0.01, yerr=0.02, fluxfit=1234.5, fluxerr=56.7,
        flags=0, field=3, hp6=123, hp9=4567, pid=99, isdiffpos=True,
        qfit=0.1, cfit=0.05, redchi=1.2, npixfit=25,
        sharpness=0.4, roundness1=0.1, roundness2=-0.05, peak=321.0,
    )


class FakeProvider(AlertDataProvider):
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


def main():
    # 1. Committed .avsc files must match the registry
    assert generate(check=True), ".avsc files differ from fields.py registry"

    # 2. Assemble from the fake provider
    alert = assemble_alert(FakeProvider(), 9999)
    assert alert["diaSourceId"] == 9999
    assert alert["schemaVersion"] == VERSION
    assert alert["diaSource"]["isNegative"] is False  # isdiffpos=True inverted
    assert alert["diaSource"]["npixfit"] == 25
    assert alert["diaSource"]["diaObjectId"] == 777
    assert abs(alert["diaSource"]["snr"] - 1234.5 / 56.7) < 1e-6
    assert alert["diaObject"]["nDiaSources"] == 3
    assert alert["diaObject"]["firstDiaSourceMjdTai"] == 60480.5
    assert alert["diaObject"]["lastDiaSourceMjdTai"] == 60500.5
    assert alert["diaObject"]["validityStartMjdTai"] == 60500.5
    assert len(alert["prvDiaSources"]) == 2
    assert alert["prvDiaForcedSources"] is None
    assert alert["cutoutDifference"] == b"FAKE_DIFF"
    assert alert["cutoutTemplate"] is None

    # 3. Every stub field must be null in the built records
    by_name = {r.name: r for r in RECORDS}
    for f in by_name["diaSource"].fields:
        if f.status is Status.STUB:
            assert alert["diaSource"][f.name] is None, f.name

    # 4. Serialize and read back
    schema = load_schema()
    blob = serialize_alert(alert, schema=schema)
    decoded = fastavro.schemaless_reader(io.BytesIO(blob), schema)
    assert decoded["diaSourceId"] == 9999
    assert decoded["diaObject"]["diaObjectId"] == 777
    assert len(decoded["prvDiaSources"]) == 2
    assert decoded["cutoutDifference"] == b"FAKE_DIFF"

    print(f"OK: alert assembled, serialized ({len(blob)} bytes), "
          "and round-tripped through the Avro schema")


if __name__ == "__main__":
    main()
