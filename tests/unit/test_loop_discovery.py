"""Unit tests of delivery discovery and batches in the processing-date loop
(supervisor step 4 of the operations campaign, rulings R1-R7, R13, R14).

Database-free: :class:`_DB` interprets exactly the SQL ``rapidpipe.launch.loop``
and ``rapidpipe.launch.discovery`` send for ``loop_dates`` and
``loop_deliveries`` (with commit and rollback), and refuses any other
statement; the inbox is a fake S3 client whose paginator lists once; the
science seams of ``process_date`` are replaced as in ``test_loop.py``."""

from __future__ import annotations

import ast
import copy
import datetime as dt
import json
import re
from pathlib import Path

import pytest

from rapidpipe.launch import discovery, loop
from rapidpipe.products.manifest import Inputs, Manifest, Member, OutputEntry, Unit
from rapidpipe.runs import inputs, repository
from tests.unit.test_loop import _Storage, _entry, _manifest

REPO = Path(__file__).resolve().parents[2]

STREAM = """
[loop]
schedule = "ops4-stream"
release = "rebuild-v0.4"
owner = "scheduler"
lane = "prompt"
check_policy = "rebuild-trial@1"
max_attempts = 3
inbox = "s3://bkt/ops4/inbox/"
difference_template = "s3://bkt/control/20260923/inputs"
admit_settings = "s3://bkt/settings/admit-socsim.toml"
difference_settings = "s3://bkt/settings/difference.toml"
"""

LISTED = STREAM + """
[[dates]]
processing_date = 2027-10-01
[[dates.detector_images]]
delivery = "s3://bkt/control/delivery/r0034001002001001001-sca01"
difference_template = "s3://bkt/control/20260923/inputs"
"""

INBOX = "ops4/inbox"
D1, D2 = dt.date(2027, 11, 1), dt.date(2027, 11, 2)
SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64


def _loc(date, name):
    return f"s3://bkt/{INBOX}/{date}/{name}"


def _key(date, name):
    return f"{INBOX}/{date}/{name}/manifest.json"


def _delivery(exposure, *, version="1", sha=SHA_A, detector="1", stage="delivery",
              outputs=None, primary="img.fits.gz"):
    member = Member(role="image", path="img.fits.gz", bytes=10, sha256=sha)
    entry = OutputEntry(kind="l2-image", format_version="delivered",
                        instance=f"L2-{exposure}-v{version}",
                        key={"exposure": exposure, "detector": detector, "version": version},
                        members=(member,), primary=primary)
    return Manifest(run="-", unit=Unit(kind="detector-image", id=f"{exposure}/SCA01"),
                    stage=stage, attempt="-", execution_record="-",
                    inputs=Inputs(manifest="delivery"),
                    outputs=tuple(outputs if outputs is not None else (entry,)))


# ======================================================================
# Fakes
# ======================================================================

_ROW_ORDER = ("schedule", "processing_date", "run", "state", "started_at", "ended_at",
              "promotion", "record", "batch", "kind")


class _DB:
    """``loop_dates`` and ``loop_deliveries`` in memory, transactional."""

    def __init__(self, *, lock_held=False):
        self.state = {"dates": [], "deliveries": []}
        self._committed = copy.deepcopy(self.state)
        self.executed: list[tuple[str, tuple]] = []
        self.commits = 0
        self.rollbacks = 0
        self.lock_held = lock_held
        self._clock = 0

    # -- transactions --------------------------------------------------
    def commit(self):
        self.commits += 1
        self._committed = copy.deepcopy(self.state)

    def rollback(self):
        self.rollbacks += 1
        self.state = copy.deepcopy(self._committed)

    @property
    def dates(self):
        return self.state["dates"]

    @property
    def deliveries(self):
        return self.state["deliveries"]

    def writes(self):
        return [sql for sql, _ in self.executed if sql.split()[0] in ("INSERT", "UPDATE")]

    def seed_row(self, date, batch, run, state, record=None):
        self.dates.append({"schedule": "ops4-stream", "processing_date": date, "run": run,
                           "state": state, "started_at": None, "ended_at": None,
                           "promotion": None, "record": record or {"run": run},
                           "batch": batch, "kind": "batch"})
        self.commit()

    def seed_delivery(self, location, date, exposure, version, sha, state, batch=None,
                      reason=None):
        self._clock += 1
        self.deliveries.append({
            "schedule": "ops4-stream", "location": location, "processing_date": date,
            "exposure": exposure, "detector": "1", "version": version, "checksum": sha,
            "delivery_instance": f"L2-{exposure}-v{version}", "unit": "u", "state": state,
            "reason": reason, "batch": batch, "discovered_at": self._clock})
        self.commit()

    def row(self, date, batch):
        return next(r for r in self.dates if (r["processing_date"], r["batch"]) == (date, batch))

    # -- the cursor ----------------------------------------------------
    def cursor(self):
        return _Cursor(self)

    def run_sql(self, sql, params):
        sql = " ".join(sql.split())
        self.executed.append((sql, tuple(params or ())))
        p = list(params or ())
        if "pg_try_advisory_lock" in sql:
            return [(not self.lock_held,)]
        if "pg_advisory_unlock" in sql:
            return [(True,)]
        if sql.startswith("SELECT schedule, processing_date, run, state") and \
                "FROM loop_dates" in sql:
            rows = [r for r in self.dates if r["schedule"] == p[0]]
            if sql.endswith("AND processing_date = %s AND batch = %s"):
                rows = [r for r in rows if (r["processing_date"], r["batch"]) == (p[1], p[2])]
            elif "(processing_date, batch) < (%s, %s)" in sql:
                assert "state = 'complete'" in sql
                assert sql.endswith("ORDER BY processing_date DESC, batch DESC")
                rows = sorted((r for r in rows if r["state"] == "complete"
                               and (r["processing_date"], r["batch"]) < (p[1], p[2])),
                              key=lambda r: (r["processing_date"], r["batch"]), reverse=True)
            else:
                assert sql.endswith("WHERE schedule = %s ORDER BY processing_date, batch")
                rows = sorted(rows, key=lambda r: (r["processing_date"], r["batch"]))
            return [tuple(copy.deepcopy(r[c]) for c in _ROW_ORDER) for r in rows]
        if sql.startswith("SELECT COALESCE(MAX(batch), 0) + 1 FROM loop_dates"):
            batches = [r["batch"] for r in self.dates
                       if (r["schedule"], r["processing_date"]) == (p[0], p[1])]
            return [(max(batches, default=0) + 1,)]
        if sql.startswith("INSERT INTO loop_dates"):
            schedule, date, batch, run, record = p
            if any((r["schedule"], r["processing_date"], r["batch"]) == (schedule, date, batch)
                   for r in self.dates):
                raise AssertionError(f"duplicate loop_dates key {(schedule, date, batch)}")
            self.dates.append({"schedule": schedule, "processing_date": date, "run": run,
                               "state": "open", "started_at": "t", "ended_at": None,
                               "promotion": None, "record": json.loads(record),
                               "batch": batch, "kind": "batch"})
            return []
        if sql.startswith("UPDATE loop_dates SET run = %s, state = 'open'"):
            run, record, schedule, date, batch = p
            for r in self.dates:
                if (r["schedule"], r["processing_date"], r["batch"], r["state"]) == (
                        schedule, date, batch, "failed"):
                    r.update(run=run, state="open", ended_at=None, promotion=None,
                             record=json.loads(record))
            return []
        if sql.startswith("UPDATE loop_dates SET state = %s"):
            state, promotion, record, schedule, date, batch = p
            for r in self.dates:
                if (r["schedule"], r["processing_date"], r["batch"]) == (schedule, date, batch):
                    r.update(state=state, promotion=promotion, ended_at="t",
                             record=json.loads(record))
            return []
        if sql.startswith("SELECT location FROM loop_deliveries"):
            rows = [d for d in self.deliveries if d["schedule"] == p[0]]
            if "AND processing_date = %s AND batch = %s AND state = 'batched'" in sql:
                rows = sorted((d for d in rows if (d["processing_date"], d["batch"], d["state"])
                               == (p[1], p[2], "batched")), key=lambda d: d["location"])
            else:
                assert sql == "SELECT location FROM loop_deliveries WHERE schedule = %s"
            return [(d["location"],) for d in rows]
        if sql.startswith("SELECT exposure, detector, version, checksum, delivery_instance"):
            return [(d["exposure"], d["detector"], d["version"], d["checksum"],
                     d["delivery_instance"]) for d in sorted(
                         (d for d in self.deliveries
                          if d["schedule"] == p[0] and d["state"] == "batched"),
                         key=lambda d: (d["discovered_at"], d["location"]))]
        if sql.startswith("INSERT INTO loop_deliveries"):
            names = ("schedule", "location", "processing_date", "exposure", "detector",
                     "version", "checksum", "delivery_instance", "unit", "state", "reason",
                     "batch")
            row = dict(zip(names, p))
            if any((d["schedule"], d["location"]) == (row["schedule"], row["location"])
                   for d in self.deliveries):
                raise AssertionError(f"duplicate loop_deliveries key {row['location']}")
            self._clock += 1
            row["discovered_at"] = self._clock
            self.deliveries.append(row)
            return []
        if sql.startswith("SELECT processing_date, state, location, exposure"):
            rows = sorted((d for d in self.deliveries if d["schedule"] == p[0]),
                          key=lambda d: (d["processing_date"], d["discovered_at"],
                                         d["location"]))
            return [(d["processing_date"], d["state"], d["location"], d["exposure"],
                     d["detector"], d["version"], d["reason"], d["batch"]) for d in rows]
        raise AssertionError(f"unexpected SQL: {sql}")


class _Cursor:
    def __init__(self, db):
        self.db, self.rows = db, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.rows = self.db.run_sql(sql, params)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class _Paginator:
    def __init__(self, s3):
        self.s3 = s3

    def paginate(self, *, Bucket, Prefix):
        self.s3.listings.append((Bucket, Prefix))
        keys = sorted(k for b, k in self.s3.keys if b == Bucket and k.startswith(Prefix))
        # Two pages, so the discovery follows pagination.
        half = len(keys) // 2
        return iter([{"Contents": [{"Key": k} for k in keys[:half]]},
                     {"Contents": [{"Key": k} for k in keys[half:]]}])


class _S3:
    """``get_paginator("list_objects_v2")`` over a key set; one listing per call."""

    def __init__(self, keys=()):
        self.keys = [("bkt", k) for k in keys]
        self.listings: list[tuple[str, str]] = []

    def add(self, key):
        self.keys.append(("bkt", key))

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return _Paginator(self)


class _Inbox(_Storage):
    """Storage whose delivery manifests are counted as they are read."""

    def __init__(self, manifests=None):
        super().__init__(manifests)
        self.reads: list[str] = []

    def read_manifest(self, location):
        self.reads.append(location)
        value = super().read_manifest(location)
        if isinstance(value, Exception):
            raise value
        return value


def _stage(s3, storage, date, name, manifest):
    s3.add(_key(date, name))
    storage.manifests[_loc(date, name)] = manifest


class _Tools:
    def __init__(self, s3, storage):
        self.created: list[dict] = []
        self.lines: list[str] = []
        self.tools = loop.LoopTools(walk=None, create_run=self.create_run, storage=storage,
                                    inputs_root=lambda run: f"s3://bkt/scratch/runs/{run}/inputs",
                                    out=self.lines.append, s3_client=s3)

    def create_run(self, conn, **kwargs):
        self.created.append(kwargs)
        return f"RUN{len(self.created)}"


def _spec(text=STREAM):
    return loop.parse_spec(text, "s3://bkt/ops4/loop/ops4-stream.toml")


def _no_resume_state(monkeypatch):
    monkeypatch.setattr(loop, "reopenable", lambda conn, row: False)


def _recording_process(monkeypatch, db, *, result=0, finish=True, during=None):
    """Replace ``process_date``: record the batch, then complete its row."""
    calls = []

    def process(conn, spec, day, tools, *, interval, timeout):
        calls.append({"date": day.processing_date, "batch": day.batch,
                      "deliveries": day.deliveries,
                      "units": [i.unit for i in day.detector_images],
                      "images": day.detector_images,
                      "rows_committed": len(db._committed["dates"])})
        if during:
            during(day)
        if finish and result == 0:
            row = loop.loop_row(conn, spec.schedule, day.processing_date, day.batch)
            loop._update_row(conn, spec.schedule, day.processing_date, day.batch,
                             state="complete", promotion=None, record=row.record)
            conn.commit()
        return result

    monkeypatch.setattr(loop, "process_date", process)
    _no_resume_state(monkeypatch)
    return calls


# ======================================================================
# The spec (R1)
# ======================================================================

def test_an_inbox_spec_needs_no_dates_and_carries_the_stream_inputs():
    spec = _spec()
    assert spec.dates == ()
    assert (spec.inbox, spec.difference_template, spec.admit_settings,
            spec.difference_settings) == (
        "s3://bkt/ops4/inbox", "s3://bkt/control/20260923/inputs",
        "s3://bkt/settings/admit-socsim.toml", "s3://bkt/settings/difference.toml")
    both = _spec(LISTED)
    assert both.inbox and [d.processing_date for d in both.dates] == [dt.date(2027, 10, 1)]
    assert both.dates[0].batch == 1 and both.dates[0].deliveries == ()


@pytest.mark.parametrize("mutate, message", [
    (lambda s: s.replace('difference_template = "s3://bkt/control/20260923/inputs"\n', ""),
     "'difference_template' is required with 'inbox'"),
    (lambda s: s.replace('inbox = "s3://bkt/ops4/inbox/"\n', ""), "no \\[\\[dates\\]\\]"),
    (lambda s: s.replace('"s3://bkt/ops4/inbox/"', '"/local/inbox"'), "s3://bucket/prefix"),
    (lambda s: s.replace('"s3://bkt/ops4/inbox/"', '"s3://bkt"'), "s3://bucket/prefix"),
    (lambda s: s.replace('"s3://bkt/ops4/inbox/"', '""'), "non-empty string"),
])
def test_an_inbox_spec_is_refused_when_malformed(mutate, message):
    with pytest.raises(loop.LoopSpecError, match=message):
        _spec(mutate(STREAM))


# ======================================================================
# Discovery and classification (R3)
# ======================================================================

def test_discovery_lists_once_ignores_other_keys_and_skips_recorded_locations():
    db, s3, storage = _DB(), _S3(), _Inbox()
    _stage(s3, storage, D1, "r1-sca01", _delivery("r1"))
    for other in (f"{INBOX}/README.txt", f"{INBOX}/{D1}/r1-sca01/img.fits.gz",
                  f"{INBOX}/{D1}/r1-sca01/sub/manifest.json", f"{INBOX}/2027-13-45/x/manifest.json",
                  f"{INBOX}/{D1}/manifest.json"):
        s3.add(other)
    recorded = _loc(D1, "old-sca01")
    s3.add(_key(D1, "old-sca01"))
    storage.manifests[recorded] = AssertionError("a recorded location is never read")
    db.seed_delivery(recorded, D1, "r0", "1", SHA_A, "quarantined", reason="malformed")
    found = discovery.discover(db, "ops4-stream", "s3://bkt/ops4/inbox", storage, s3,
                               loop.detector_unit_id)
    assert s3.listings == [("bkt", "ops4/inbox/")]
    assert (found.ignored, found.recorded) == (5, 1)
    assert storage.reads == [_loc(D1, "r1-sca01")]
    (d,) = found.deliveries
    assert (d.location, d.processing_date, d.unit, d.state, d.reason) == (
        _loc(D1, "r1-sca01"), D1, "r1/SCA01", "batched", None)
    assert (d.identity.exposure, d.identity.detector, d.identity.version,
            d.identity.checksum, d.identity.instance) == ("r1", "1", "1", SHA_A, "L2-r1-v1")
    assert db.writes() == []


def test_classification_follows_every_rule_in_key_order():
    db, s3, storage = _DB(), _S3(), _Inbox()
    db.seed_delivery(_loc(D1, "a-sca01"), D1, "rA", "1", SHA_A, "batched", batch=1)
    staged = {
        "b01-sca01": _delivery("rB"),                           # new: batched
        "b02-sca01": _delivery("rA"),                           # identical re-delivery
        "b03-sca01": _delivery("rA", sha=SHA_B),                # checksum conflict
        "b04-sca01": _delivery("rA", version="2"),              # corrected: deferred
        "b05-sca01": _delivery("rB"),                           # identical to b01 (this firing)
        "b06-sca01": _delivery("rB", sha=SHA_B),                # conflicts with b01
        "b07-sca01": _delivery("rB", version="2"),              # corrects b01
        "b08-sca01": _delivery("rC", stage="admit"),            # not a delivery
        "b09-sca01": _delivery("rC", outputs=()),               # no l2-image entry
        "b10-sca01": _delivery("rC", outputs=[_entry("l2-image", "X"), _entry("l2-image", "Y")]),
        "b11-sca01": _delivery("rC", version=""),               # missing version
        "b12-sca01": _delivery("rC", primary=None),             # no primary member
        "b13-sca01": ValueError("not valid JSON"),              # unreadable
        "b14-sca01": _delivery("rC"),                           # batched after the malformed
    }
    for name, manifest in staged.items():
        _stage(s3, storage, D2, name, manifest)
    found = discovery.discover(db, "ops4-stream", "s3://bkt/ops4/inbox", storage, s3,
                               loop.detector_unit_id)
    assert [(d.location.rsplit("/", 1)[1], d.state, d.reason) for d in found.deliveries] == [
        ("b01-sca01", "batched", None),
        ("b02-sca01", "refused", "identical re-delivery"),
        ("b03-sca01", "quarantined", "checksum conflict"),
        ("b04-sca01", "deferred", "corrected delivery awaits a correction run"),
        ("b05-sca01", "refused", "identical re-delivery"),
        ("b06-sca01", "quarantined", "checksum conflict"),
        ("b07-sca01", "deferred", "corrected delivery awaits a correction run"),
        ("b08-sca01", "quarantined", "malformed"),
        ("b09-sca01", "quarantined", "malformed"),
        ("b10-sca01", "quarantined", "malformed"),
        ("b11-sca01", "quarantined", "malformed"),
        ("b12-sca01", "quarantined", "malformed"),
        ("b13-sca01", "quarantined", "malformed"),
        ("b14-sca01", "batched", None),
    ]
    assert storage.reads == sorted(_loc(D2, n) for n in staged)
    assert found.summary() == ("14 new deliveries (2 batched, 2 refused, 8 quarantined, "
                               "2 deferred); 0 already recorded, 0 other keys ignored")
    assert db.writes() == []


def test_a_storage_error_that_is_not_a_bad_manifest_propagates():
    db, s3, storage = _DB(), _S3(), _Inbox()
    _stage(s3, storage, D1, "r1-sca01", ConnectionError("S3 unreachable"))
    with pytest.raises(ConnectionError):
        discovery.discover(db, "ops4-stream", "s3://bkt/ops4/inbox", storage, s3,
                           loop.detector_unit_id)


def test_an_invalid_manifest_exit_64_from_the_cli_storage_is_malformed():
    class _Exit(Exception):
        code = 64

    db, s3, storage = _DB(), _S3(), _Inbox()
    _stage(s3, storage, D1, "r1-sca01", _Exit("invalid manifest"))
    (d,) = discovery.discover(db, "ops4-stream", "s3://bkt/ops4/inbox", storage, s3,
                              loop.detector_unit_id).deliveries
    assert (d.state, d.reason, d.identity) == ("quarantined", "malformed", None)


def test_an_attribute_or_index_error_from_manifest_validation_is_malformed_not_fatal():
    # P1 (Codex 4-2): AttributeError/IndexError used to propagate and abort the
    # whole firing before any classification committed; one bad object then
    # blocked the inbox forever. They are malformed like ValueError etc.
    db, s3, storage = _DB(), _S3(), _Inbox()
    _stage(s3, storage, D1, "bad-attr-sca01", AttributeError("'int' object has no attribute 'get'"))
    _stage(s3, storage, D1, "bad-index-sca01", IndexError("list index out of range"))
    _stage(s3, storage, D1, "good-sca01", _delivery("r1"))
    found = discovery.discover(db, "ops4-stream", "s3://bkt/ops4/inbox", storage, s3,
                               loop.detector_unit_id)
    assert [(d.location.rsplit("/", 1)[1], d.state, d.reason, d.identity)
            for d in found.deliveries] == [
        ("bad-attr-sca01", "quarantined", "malformed", None),
        ("bad-index-sca01", "quarantined", "malformed", None),
        ("good-sca01", "batched", None, found.deliveries[2].identity)]
    assert found.deliveries[2].identity is not None


# ======================================================================
# Batches (R2, R6, R13)
# ======================================================================

def test_two_dates_become_two_batches_oldest_first_all_committed_before_any_walk(monkeypatch):
    db, s3, storage = _DB(), _S3(), _Inbox()
    _stage(s3, storage, D2, "r2-sca01", _delivery("r2"))
    _stage(s3, storage, D1, "r1-sca01", _delivery("r1"))
    t = _Tools(s3, storage)
    calls = _recording_process(monkeypatch, db)
    assert loop.run_loop(db, _spec(), t.tools) == 0
    assert [c["purpose"] for c in t.created] == [
        "processing date 2027-11-01 batch 1 (schedule ops4-stream)",
        "processing date 2027-11-02 batch 1 (schedule ops4-stream)"]
    assert all(c["release"] == "rebuild-v0.4" and c["max_attempts"] == 3 for c in t.created)
    assert [(c["date"], c["batch"], c["deliveries"]) for c in calls] == [
        (D1, 1, (_loc(D1, "r1-sca01"),)), (D2, 1, (_loc(D2, "r2-sca01"),))]
    assert [c["rows_committed"] for c in calls] == [2, 2]   # frozen before any walk
    image = calls[0]["images"][0]
    assert image == loop.DetectorImage(
        delivery=_loc(D1, "r1-sca01"), admit_settings="s3://bkt/settings/admit-socsim.toml",
        difference_template="s3://bkt/control/20260923/inputs",
        difference_settings="s3://bkt/settings/difference.toml", unit="r1/SCA01")
    assert [(r["processing_date"], r["batch"], r["run"], r["kind"]) for r in db.dates] == [
        (D1, 1, "RUN1", "batch"), (D2, 1, "RUN2", "batch")]
    assert [(d["location"], d["state"], d["batch"]) for d in db.deliveries] == [
        (_loc(D1, "r1-sca01"), "batched", 1), (_loc(D2, "r2-sca01"), "batched", 1)]
    # One transaction per batch: run, row and its deliveries, then commit.
    inserts = [sql.split(" (")[0] for sql in db.writes() if sql.startswith("INSERT")]
    assert inserts == ["INSERT INTO loop_dates", "INSERT INTO loop_deliveries"] * 2
    assert "schedule ops4-stream: s3://bkt/ops4/inbox: 2 new deliveries (2 batched, " \
           "0 refused, 0 quarantined, 0 deferred); 0 already recorded, 0 other keys " \
           "ignored" in t.lines


def test_the_batch_record_names_its_batch_and_deliveries(monkeypatch):
    db, s3, storage = _DB(), _S3(), _Inbox()
    _stage(s3, storage, D1, "r1-sca01", _delivery("r1"))
    t = _Tools(s3, storage)
    _recording_process(monkeypatch, db, finish=False)
    assert loop.run_loop(db, _spec(), t.tools) == 0
    assert db.row(D1, 1)["record"] == {
        "spec": "s3://bkt/ops4/loop/ops4-stream.toml", "release": "rebuild-v0.4",
        "run": "RUN1", "batch": 1, "deliveries": [_loc(D1, "r1-sca01")]}


def test_a_later_arrival_for_a_date_is_its_next_batch_and_extends_the_first(monkeypatch):
    db, s3, storage = _DB(), _S3(), _Inbox()
    _stage(s3, storage, D1, "r1-sca01", _delivery("r1"))
    _stage(s3, storage, D2, "r2-sca01", _delivery("r2"))
    t = _Tools(s3, storage)
    calls = _recording_process(monkeypatch, db)
    assert loop.run_loop(db, _spec(), t.tools) == 0
    _stage(s3, storage, D2, "r3-sca01", _delivery("r3"))
    assert loop.run_loop(db, _spec(), t.tools) == 0
    assert [(c["date"], c["batch"]) for c in calls] == [(D1, 1), (D2, 1), (D2, 2)]
    assert t.created[-1]["purpose"] == "processing date 2027-11-02 batch 2 (schedule ops4-stream)"
    assert db.row(D2, 2)["record"]["deliveries"] == [_loc(D2, "r3-sca01")]
    previous = loop.previous_complete_rows(db, "ops4-stream", D2, 2)
    assert [(r.processing_date, r.batch) for r in previous] == [(D2, 1), (D1, 1)]
    assert [(r.processing_date, r.batch) for r in
            loop.previous_complete_rows(db, "ops4-stream", D2, 1)] == [(D1, 1)]
    assert [(r.processing_date, r.batch, r.kind) for r in loop.loop_rows(db, "ops4-stream")] == [
        (D1, 1, "batch"), (D2, 1, "batch"), (D2, 2, "batch")]


def test_membership_is_frozen_at_the_listing(monkeypatch):
    db, s3, storage = _DB(), _S3(), _Inbox()
    _stage(s3, storage, D1, "r1-sca01", _delivery("r1"))
    t = _Tools(s3, storage)

    def arrive(day):
        if day.batch == 1 and day.processing_date == D1 and len(s3.keys) == 1:
            _stage(s3, storage, D1, "late-sca01", _delivery("r9"))

    calls = _recording_process(monkeypatch, db, during=arrive)
    assert loop.run_loop(db, _spec(), t.tools) == 0
    assert [(c["date"], c["batch"], c["deliveries"]) for c in calls] == [
        (D1, 1, (_loc(D1, "r1-sca01"),))]
    assert _loc(D1, "late-sca01") not in storage.reads
    assert len(s3.listings) == 1
    # The next firing forms the late delivery's batch.
    assert loop.run_loop(db, _spec(), t.tools) == 0
    assert [(c["date"], c["batch"], c["deliveries"]) for c in calls][1:] == [
        (D1, 2, (_loc(D1, "late-sca01"),))]


def test_an_empty_firing_writes_nothing_and_exits_0(monkeypatch):
    db, s3, storage = _DB(), _S3([f"{INBOX}/README.txt"]), _Inbox()
    t = _Tools(s3, storage)
    calls = _recording_process(monkeypatch, db)
    assert loop.run_loop(db, _spec(), t.tools) == 0
    assert t.lines == ["schedule ops4-stream: nothing to discover"]
    assert db.writes() == [] and t.created == [] and calls == []
    assert db.dates == [] and db.deliveries == []


def test_an_already_recorded_inbox_is_nothing_to_discover(monkeypatch):
    db, s3, storage = _DB(), _S3(), _Inbox()
    _stage(s3, storage, D1, "r1-sca01", _delivery("r1"))
    t = _Tools(s3, storage)
    _recording_process(monkeypatch, db)
    assert loop.run_loop(db, _spec(), t.tools) == 0
    before = len(db.writes())
    t.lines.clear()
    storage.reads.clear()
    assert loop.run_loop(db, _spec(), t.tools) == 0
    assert t.lines == ["schedule ops4-stream: nothing to discover"]
    assert len(db.writes()) == before and storage.reads == [] and len(t.created) == 1


def test_a_firing_of_only_rejections_records_them_and_creates_no_run(monkeypatch):
    # R13: a duplicate and a conflict of an admitted delivery.
    db, s3, storage = _DB(), _S3(), _Inbox()
    db.seed_row(D1, 1, "RUN0", "complete")
    db.seed_delivery(_loc(D1, "r1-sca01"), D1, "r1", "1", SHA_A, "batched", batch=1)
    s3.add(_key(D1, "r1-sca01"))
    _stage(s3, storage, D2, "again-r1-sca01", _delivery("r1"))
    _stage(s3, storage, D2, "bad-r1-sca01", _delivery("r1", sha=SHA_B))
    t = _Tools(s3, storage)
    calls = _recording_process(monkeypatch, db)
    commits = db.commits
    assert loop.run_loop(db, _spec(), t.tools) == 0
    assert t.created == [] and calls == []
    assert [sql.split(" (")[0] for sql in db.writes()] == ["INSERT INTO loop_deliveries"] * 2
    assert [(d["location"], d["state"], d["reason"], d["batch"]) for d in db.deliveries[1:]] == [
        (_loc(D2, "again-r1-sca01"), "refused", "identical re-delivery", None),
        (_loc(D2, "bad-r1-sca01"), "quarantined", "checksum conflict", None)]
    assert [(r["processing_date"], r["batch"]) for r in db.dates] == [(D1, 1)]
    # The lock's commit, the one disposition transaction, the unlock's commit.
    assert db.commits == commits + 3


def test_rejections_commit_before_the_first_batch(monkeypatch):
    db, s3, storage = _DB(), _S3(), _Inbox()
    _stage(s3, storage, D1, "a-sca01", _delivery("r1"))
    _stage(s3, storage, D1, "b-sca01", _delivery("r1"))
    t = _Tools(s3, storage)
    seen = {}

    def create_run(conn, **kwargs):
        seen["committed"] = [(d["location"], d["state"]) for d in db._committed["deliveries"]]
        return "RUN1"

    t.tools.create_run = create_run
    _recording_process(monkeypatch, db)
    assert loop.run_loop(db, _spec(), t.tools) == 0
    assert seen["committed"] == [(_loc(D1, "b-sca01"), "refused")]
    assert [(d["location"], d["state"], d["batch"]) for d in db.deliveries] == [
        (_loc(D1, "b-sca01"), "refused", None), (_loc(D1, "a-sca01"), "batched", 1)]


def test_a_unit_id_collision_is_quarantined_naming_the_earlier_delivery_and_the_batch_has_one_image(
        monkeypatch):
    # P2 (Codex 4-2): "image-sca01" and "image_sca01" both derive unit
    # "image/SCA01". This used to raise LoopError with nothing recorded, so
    # neither delivery was ever batched. Now the later one (key order) is a
    # durable quarantine naming the earlier, and the earlier batches alone.
    db, s3, storage = _DB(), _S3(), _Inbox()
    _stage(s3, storage, D1, "image-sca01", _delivery("eA"))
    _stage(s3, storage, D1, "image_sca01", _delivery("eB"))
    t = _Tools(s3, storage)
    calls = _recording_process(monkeypatch, db)
    assert loop.run_loop(db, _spec(), t.tools) == 0
    assert [(c["date"], c["batch"], c["deliveries"], c["units"]) for c in calls] == [
        (D1, 1, (_loc(D1, "image-sca01"),), ["image/SCA01"])]
    assert [(d["location"], d["state"], d["reason"], d["batch"]) for d in db.deliveries] == [
        (_loc(D1, "image_sca01"), "quarantined",
         f"unit id collision with {_loc(D1, 'image-sca01')}", None),
        (_loc(D1, "image-sca01"), "batched", None, 1)]
    assert (f"delivery {_loc(D1, 'image_sca01')} eB/1/v1 state=quarantined reason=unit id "
            f"collision with {_loc(D1, 'image-sca01')}") in t.lines


def test_resolve_unit_collisions_leaves_deliveries_without_a_collision_unchanged():
    same_unit_other_date = discovery.Delivery(
        location=_loc(D2, "b-sca01"), processing_date=D2, unit="b/SCA01", identity=None,
        state=discovery.BATCHED)
    batched = discovery.Delivery(
        location=_loc(D1, "a-sca01"), processing_date=D1, unit="a/SCA01", identity=None,
        state=discovery.BATCHED)
    already_rejected = discovery.Delivery(
        location=_loc(D1, "c-sca01"), processing_date=D1, unit="a/SCA01", identity=None,
        state=discovery.QUARANTINED, reason="malformed")
    deliveries = [batched, already_rejected, same_unit_other_date]
    resolved = discovery.resolve_unit_collisions(deliveries, loop.detector_unit_id)
    assert resolved == deliveries


def test_a_held_lock_exits_75_before_any_discovery(monkeypatch):
    db, s3, storage = _DB(lock_held=True), _S3(), _Inbox()
    _stage(s3, storage, D1, "r1-sca01", _delivery("r1"))
    t = _Tools(s3, storage)
    calls = _recording_process(monkeypatch, db)
    assert loop.run_loop(db, _spec(), t.tools) == 75
    assert t.lines == ["another loop holds schedule ops4-stream"]
    assert s3.listings == [] and storage.reads == [] and calls == [] and t.created == []
    assert [sql for sql, _ in db.executed] == [
        "SELECT pg_try_advisory_lock(hashtext('rapidpipe.loop:' || %s))"]


def test_a_failed_batch_stops_the_firing_before_discovery(monkeypatch):
    db, s3, storage = _DB(), _S3(), _Inbox()
    db.seed_row(D1, 1, "RUN1", "failed")
    db.seed_delivery(_loc(D1, "r1-sca01"), D1, "r1", "1", SHA_A, "batched", batch=1)
    _stage(s3, storage, D2, "r2-sca01", _delivery("r2"))
    t = _Tools(s3, storage)
    calls = _recording_process(monkeypatch, db)
    assert loop.run_loop(db, _spec(), t.tools) == 1
    assert s3.listings == [] and calls == [] and db.writes() == []
    assert t.lines[-1] == ("date=2027-11-01: failed; stopping before later dates "
                           "(--retry-failed re-runs its failed units)")


def test_open_batches_resume_before_discovery_in_date_and_batch_order(monkeypatch):
    db, s3, storage = _DB(), _S3(), _Inbox()
    db.seed_row(D2, 1, "RUN1", "complete")
    db.seed_row(D2, 2, "RUN2", "open")
    db.seed_row(D1, 1, "RUN3", "open")
    for date, batch, name in ((D2, 1, "a-sca01"), (D2, 2, "b-sca01"), (D1, 1, "c-sca01")):
        db.seed_delivery(_loc(date, name), date, name, "1", SHA_A, "batched", batch=batch)
        s3.add(_key(date, name))
    t = _Tools(s3, storage)
    calls = _recording_process(monkeypatch, db)
    assert loop.run_loop(db, _spec(), t.tools) == 0
    assert [(c["date"], c["batch"], c["deliveries"]) for c in calls] == [
        (D1, 1, (_loc(D1, "c-sca01"),)), (D2, 2, (_loc(D2, "b-sca01"),))]
    assert calls[0]["units"] == ["c/SCA01"]
    assert t.created == [] and storage.reads == []
    assert t.lines[-1] == "schedule ops4-stream: nothing to discover"


def test_a_date_filter_on_an_inbox_spec_runs_only_listed_dates(monkeypatch):
    db, s3, storage = _DB(), _S3(), _Inbox()
    _stage(s3, storage, D1, "r1-sca01", _delivery("r1"))
    t = _Tools(s3, storage)
    calls = []
    monkeypatch.setattr(loop, "process_date", lambda conn, spec, day, tools, **kw: (
        calls.append((day.processing_date, day.batch, day.deliveries)) or 0))
    _no_resume_state(monkeypatch)
    assert loop.run_loop(db, _spec(LISTED), t.tools, dates=[dt.date(2027, 10, 1)]) == 0
    assert calls == [(dt.date(2027, 10, 1), 1, ())] and s3.listings == []


# ======================================================================
# Crash and restart (R6)
# ======================================================================

def _science(monkeypatch, storage):
    """``process_date``'s science seams (as ``test_loop._world``), leaving the
    ``loop_dates`` reads and writes real against :class:`_DB`."""
    source = _entry("source-set", "S1", {"difference": "DI1"}, table="sources_20271101_01")
    diff = OutputEntry(kind="difference-image", format_version="1", instance="DI1",
                       key={"u": 1}, members=(), registration={})
    outputs = {
        "load": [source], "finalize": [diff],
        "crossmatch": [_entry("association-set", "AS1", {"field": 5})],
        "statistics": [_entry("statistics-set", "ST1")],
        "prune": [_entry("pruned-set", "PS1", {"base": "AS1"})],
        "alerts": [_entry("alert-container", "AC1")]}
    for stage, o in outputs.items():
        storage.manifests[f"s3://bkt/out/{stage}"] = _manifest(o)
    storage.manifests["s3://bkt/control/20260923/inputs"] = _manifest([])
    monkeypatch.setattr(loop, "selected_output", lambda conn, run, stage, unit: f"s3://bkt/out/{stage}")
    monkeypatch.setattr(loop, "source_set_fields", lambda conn, table, instance: [5])
    monkeypatch.setattr(loop, "readable_result_set", lambda *a: None)
    monkeypatch.setattr(loop, "base_entry", lambda conn, st, prev, f: None)
    monkeypatch.setattr(loop, "run_promotion", lambda conn, run: None)
    monkeypatch.setattr(loop, "run_state", lambda conn, run: "open")
    monkeypatch.setattr(loop, "run_lineage", lambda conn, run: ((run,), list(loop.SELECTED_STAGES)))
    monkeypatch.setattr(loop, "unit_state", lambda conn, run, stage, unit: ("complete", False))
    monkeypatch.setattr(loop, "jobless_attempts", lambda conn, run: [])
    monkeypatch.setattr(loop, "unit_records", lambda conn, run: [])
    monkeypatch.setattr(loop, "reopenable", lambda conn, row: False)
    monkeypatch.setattr(inputs, "_registered", lambda conn, ids: set())
    monkeypatch.setattr(repository, "add_unit", lambda *a, **k: None)
    monkeypatch.setattr(inputs, "bind_unit_inputs", lambda *a, **k: None)
    monkeypatch.setattr(repository, "finish_run", lambda conn, run: None)
    monkeypatch.setattr(loop, "_promote", lambda conn, run, spec, date, batch=1: (
        "P1", "P1", "check policy rebuild-trial@1", []))


def test_a_crash_after_the_batches_commit_resumes_the_same_runs(monkeypatch):
    db, s3, storage = _DB(), _S3(), _Inbox()
    _stage(s3, storage, D1, "r1-sca01", _delivery("r1"))
    _stage(s3, storage, D2, "r2-sca01", _delivery("r2"))
    _science(monkeypatch, storage)
    t = _Tools(s3, storage)
    walked: list[tuple[str, str]] = []
    crash = {"armed": True}

    def walk(conn, *, run_id, unit_id, positions, inputs, **kw):
        if crash.pop("armed", False):
            raise RuntimeError("the instance was terminated")
        assert kw["continue_hint"] == "rapidpipe loop run --spec s3://bkt/ops4/loop/ops4-stream.toml"
        walked.append((run_id, unit_id))
        return 0

    t.tools.walk = walk
    with pytest.raises(RuntimeError, match="terminated"):
        loop.run_loop(db, _spec(), t.tools)
    assert [(r["processing_date"], r["batch"], r["run"], r["state"]) for r in db.dates] == [
        (D1, 1, "RUN1", "open"), (D2, 1, "RUN2", "open")]
    assert len(db.deliveries) == 2 and len(t.created) == 2
    inserts_before = [sql for sql in db.writes() if sql.startswith("INSERT")]

    t.lines.clear()
    assert loop.run_loop(db, _spec(), t.tools) == 0
    assert len(t.created) == 2                    # create_run not called again
    assert [sql for sql in db.writes() if sql.startswith("INSERT")] == inserts_before
    assert [(r["processing_date"], r["batch"], r["run"], r["state"]) for r in db.dates] == [
        (D1, 1, "RUN1", "complete"), (D2, 1, "RUN2", "complete")]
    assert len(db.deliveries) == 2
    assert {run for run, _ in walked} == {"RUN1", "RUN2"}
    assert t.lines[0] == "date=2027-11-01 run=RUN1 resumed"
    assert t.lines[-1] == "schedule ops4-stream: nothing to discover"
    assert db.row(D1, 1)["record"]["deliveries"] == [_loc(D1, "r1-sca01")]


# ======================================================================
# Batch-qualified reads and writes (R14)
# ======================================================================

def _execute_sql(tree: ast.AST) -> list[str]:
    """The text of every ``<cursor>.execute(<sql>, ...)`` first argument."""
    texts = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "execute" and node.args):
            arg = node.args[0]
            parts = []
            for sub in ast.walk(arg):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    parts.append(sub.value)
            texts.append(" ".join(parts))
    return texts


def test_every_loop_dates_statement_with_a_date_predicate_names_the_batch():
    tree = ast.parse((REPO / "rapidpipe/launch/loop.py").read_text())
    statements = [s for s in _execute_sql(tree) if "loop_dates" in s]
    predicate = re.compile(r"processing_date\s*(=|<|>|IN\b)|\(processing_date, batch\)")
    dated = [s for s in statements if predicate.search(s)]
    # loop_row, previous_complete_rows, next_batch, repoint_row, _update_row.
    assert len(dated) == 5
    for sql in dated:
        assert re.search(r"\bbatch\b", sql), sql
    inserts = [s for s in statements if s.lstrip().startswith("INSERT INTO loop_dates")]
    assert inserts and all("batch" in s for s in inserts)


def test_finishing_or_failing_batch_1_leaves_batch_2_untouched(monkeypatch):
    db = _DB()
    db.seed_row(D1, 1, "RUN1", "open")
    db.seed_row(D1, 2, "RUN2", "open", record={"run": "RUN2", "keep": True})
    monkeypatch.setattr(loop, "_promote", lambda conn, run, spec, date, batch=1: (
        "P1", "P1", "gate", []))
    monkeypatch.setattr(loop, "run_state", lambda conn, run: "finished")
    monkeypatch.setattr(loop, "unit_records", lambda conn, run: [])
    spec = _spec()
    untouched = copy.deepcopy(db.row(D1, 2))
    assert loop._finish_row(db, spec, D1, 1, "RUN1", {"run": "RUN1"}, lambda line: None) == 0
    assert db.row(D1, 1)["state"] == "complete" and db.row(D1, 2) == untouched
    db.row(D1, 1)["state"] = "open"
    assert loop._fail_row(db, spec, D1, 1, "RUN1", {"run": "RUN1"}, lambda line: None,
                          failure="x") == 1
    assert db.row(D1, 1)["state"] == "failed" and db.row(D1, 2) == untouched


def test_retry_failed_repoints_only_the_batch_being_retried(monkeypatch):
    db = _DB()
    db.seed_row(D1, 1, "RUN1", "failed", record={"run": "RUN1", "failure": "f1"})
    db.seed_row(D1, 2, "RUN2", "failed", record={"run": "RUN2", "failure": "f2"})
    t = _Tools(_S3(), _Inbox())
    t.tools.create_seeded_run = lambda conn, seed: f"{seed}-retry"
    row2 = loop.loop_row(db, "ops4-stream", D1, 2)
    assert row2.batch == 2
    assert loop.retry_date(db, _spec(), row2, t.tools) == 0
    assert (db.row(D1, 2)["run"], db.row(D1, 2)["state"]) == ("RUN2-retry", "open")
    assert (db.row(D1, 1)["run"], db.row(D1, 1)["state"]) == ("RUN1", "failed")
    assert t.lines == ["date=2027-11-01 batch=2 run=RUN2-retry reopened (--retry-failed, "
                       "seeded from RUN2)"]


def test_reopen_repoints_only_its_own_batch(monkeypatch):
    db = _DB()
    db.seed_row(D1, 1, "RUN1", "failed", record={"run": "RUN1", "failure": "f1"})
    db.seed_row(D1, 2, "RUN2", "failed", record={"run": "RUN2", "failure": "f2"})
    t = _Tools(_S3(), _Inbox())
    loop.reopen_date(db, _spec(), loop.loop_row(db, "ops4-stream", D1, 1), t.tools)
    assert db.row(D1, 1)["state"] == "open" and db.row(D1, 2)["state"] == "failed"


# ======================================================================
# plan and show (R5)
# ======================================================================

def test_plan_on_an_inbox_spec_classifies_without_writing(monkeypatch):
    db, s3, storage = _DB(), _S3(), _Inbox()
    db.seed_row(D1, 1, "RUN1", "open")
    db.seed_delivery(_loc(D1, "r1-sca01"), D1, "r1", "1", SHA_A, "batched", batch=1)
    s3.add(_key(D1, "r1-sca01"))
    _stage(s3, storage, D1, "r2-sca01", _delivery("r2"))
    _stage(s3, storage, D1, "again-r1-sca01", _delivery("r1"))
    _no_resume_state(monkeypatch)
    t = _Tools(s3, storage)
    lines = loop.plan(db, _spec(), t.tools)
    assert db.writes() == [] and t.created == []
    assert [(line["processing_date"], line["action"], line.get("batch")) for line in lines] == [
        ("2027-11-01", "resume", 1), ("2027-11-01", "refused", None),
        ("2027-11-01", "batched", 2)]
    assert t.lines[0].startswith("date=2027-11-01 action=resume run=RUN1 units=r1/SCA01 ")
    assert t.lines[2] == (f"date=2027-11-01 delivery={_loc(D1, 'again-r1-sca01')} r1/1/v1 "
                          "action=refused reason=identical re-delivery")
    assert t.lines[3] == (f"date=2027-11-01 delivery={_loc(D1, 'r2-sca01')} r2/1/v1 "
                          "action=batched batch=2 unit=r2/SCA01")


def test_dry_run_on_an_inbox_spec_is_the_plan(monkeypatch):
    db, s3, storage = _DB(), _S3(), _Inbox()
    _stage(s3, storage, D1, "r1-sca01", _delivery("r1"))
    t = _Tools(s3, storage)
    assert loop.run_loop(db, _spec(), t.tools, dry_run=True) == 0
    assert db.writes() == [] and t.created == []
    assert t.lines[-1].endswith("action=batched batch=1 unit=r1/SCA01")


def test_show_prints_batches_then_deliveries():
    db = _DB()
    db.seed_row(D1, 1, "RUN1", "complete")
    db.seed_row(D1, 2, "RUN2", "open")
    db.seed_delivery(_loc(D1, "a-sca01"), D1, "r1", "1", SHA_A, "batched", batch=1)
    db.seed_delivery(_loc(D1, "b-sca01"), D1, "r1", "1", SHA_B, "quarantined",
                     reason="checksum conflict")
    lines = []
    assert loop.show(db, "ops4-stream", as_json=False, out=lines.append) == 0
    assert [line.split("\t")[:4] for line in lines[:2]] == [
        ["2027-11-01", "batch=1", "complete", "run=RUN1"],
        ["2027-11-01", "batch=2", "open", "run=RUN2"]]
    assert lines[2:] == [
        f"2027-11-01\tbatched\t{_loc(D1, 'a-sca01')}\tr1/1/v1\tbatch=1",
        f"2027-11-01\tquarantined\t{_loc(D1, 'b-sca01')}\tr1/1/v1\tchecksum conflict"]
    empty = []
    loop.show(_DB(), "none", as_json=False, out=empty.append)
    assert empty == ["schedule none: no processing dates recorded"]
