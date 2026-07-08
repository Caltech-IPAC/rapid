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

import numpy as np

from rapid_alerts.param_registry import RECORDS, VERSION, Status
from rapid_alerts.gen_schema import generate
from rapid_alerts.produce import (assemble_alert, build_dia_source,
                                  build_dia_forced_source, load_schema,
                                  produce_chip, serialize_alert)
from rapid_alerts.providers import (AlertDataProvider, Source,
                                    ObjectRecord, ForcedPhot, Cutouts,
                                    extract_stamp)


def make_detection(sid, mjd, aid=None):
    return Source(
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

    def iter_sources(self, pid):
        for sid in (9001, 9002, 9003):
            yield make_detection(sid, mjd=60500.5)


class FakeKafkaProducer:
    """Counts messages and flushes (stand-in for confluent_kafka.Producer)."""

    def __init__(self):
        self.messages = []
        self.flushes = 0

    def produce(self, topic, value, callback=None):
        self.messages.append((topic, value, callback))

    def flush(self):
        self.flushes += 1


def main():
    # 1. Committed .avsc files must match the registry
    assert generate(check=True), ".avsc files differ from param_registry.py"

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
    for f in by_name["diaSource"].params:
        if f.status is Status.STUB:
            assert alert["diaSource"][f.name] is None, f.name

    # 4. Status enforcement: an IMPLEMENTED non-nullable field yielding None
    # must raise, not serialize silently
    bad = make_detection(9999, mjd=60500.5)
    bad.sid = None                      # diaSourceId is non-nullable
    try:
        build_dia_source(bad)
        raise AssertionError("None in non-nullable IMPLEMENTED field "
                             "did not raise")
    except ValueError as e:
        assert "diaSourceId" in str(e)

    # 5. Status enforcement: STUB params stay null even with a getter staged
    fp = ForcedPhot(forced_id=1, aid=777, expid=42, sca=7, ra=150.1, dec=2.2,
                    mjdobs=60500.5, time_processed=60500.6, flux=123.4)
    assert all(v is None for v in build_dia_forced_source(fp).values())

    # 6. Provider boundary: strict from_row rejects rows with missing columns
    try:
        Source.from_row({"sid": 1, "expid": 42}, strict=True)
        raise AssertionError("strict from_row accepted an incomplete row")
    except KeyError as e:
        assert "fluxfit" in str(e)

    # 7. Serialize and read back
    schema = load_schema()
    blob = serialize_alert(alert, schema=schema)
    decoded = fastavro.schemaless_reader(io.BytesIO(blob), schema)
    assert decoded["diaSourceId"] == 9999
    assert decoded["diaObject"]["diaObjectId"] == 777
    assert len(decoded["prvDiaSources"]) == 2
    assert decoded["cutoutDifference"] == b"FAKE_DIFF"

    # 8. Cutout stamps: extract_stamp round-trips through FITS bytes and
    # refuses positions too close to the chip edge
    image = np.arange(300 * 300, dtype=np.float32).reshape(300, 300)
    stamp_bytes = extract_stamp(image, 150.0, 150.0)
    from astropy.io import fits
    with fits.open(io.BytesIO(stamp_bytes)) as hdul:
        stamp = hdul[0].data
    assert stamp.shape == (129, 129)
    assert stamp[64, 64] == image[149, 149]  # center pixel (1-based coords)
    assert extract_stamp(image, 5.0, 150.0) is None  # off-edge -> no stamp
    assert extract_stamp(None, 150.0, 150.0) is None

    # 9. Batch flow: produce_chip serializes every source on the chip and
    # flushes Kafka exactly once
    fake_producer = FakeKafkaProducer()
    count = produce_chip(FakeProvider(), pid=99, producer=fake_producer,
                         schema=schema)
    assert count == 3
    assert len(fake_producer.messages) == 3
    assert fake_producer.flushes == 1
    # batch messages go through publish_alert(flush=False), so each still
    # carries the delivery-report callback
    assert all(callback is not None for _, _, callback in fake_producer.messages)

    print(f"OK: alert assembled, serialized ({len(blob)} bytes), "
          "round-tripped through the Avro schema, and batch-produced "
          f"{count} alerts for a fake chip")


if __name__ == "__main__":
    main()
