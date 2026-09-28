"""Behavioural, black-box tests of delivery discovery and batches (loop.md
§Discovery and batches): ``rapidpipe loop run|plan|show`` over an
``inbox`` spec, argv in, exit code / stdout and database state out, against
the CI PostgreSQL with Batch and S3 faked -- the same contract
``test_loop.py`` documents, extended with ``rapidpipe.launch.discovery``'s
paginated listing (``FakeS3.get_paginator``) and the ``loop_deliveries``
table (migration ``20260926-01-loop-batches.sql``).

``stream_world`` is ``test_loop.py``'s ``world`` fixture with an ``inbox``
spec (no ``[[dates]]``) in place of two fixed dates; deliveries are staged
into the inbox with :func:`_stage` rather than named in the spec.
"""

from __future__ import annotations

import datetime as _dt
import json
import random

import pytest

from rapidpipe.launch import walk as launch_walk
from rapidpipe.db.ids import new_ulid
from rapidpipe.exitcodes import ExitCode
from rapidpipe.launch import loop as launch_loop
from rapidpipe.runs import repository

from .conftest import FAKE_BUCKET, _delete_run_rows
from .test_loop import DIGEST, PROD_DEF, TABLE, TEMPLATE, UNIT, _FakeStages, _manifest, _member, _prefix

INBOX_NAME = "stream-inbox"


def _stage(fake_s3, inbox: str, date: str, exposure: str, *, name: str | None = None,
          detector: str = "1", version: str = "1", data: bytes = b"L2" * 8) -> tuple[str, str]:
    """Seed one delivery manifest at ``<inbox>/<date>/<name>/manifest.json``
    (``name`` defaults to ``<exposure>-sca01``); return ``(location, unit
    id)``. The delivery's *identity* is ``(exposure, detector, version)``, not
    its location: an explicit ``name`` stages a second location under the
    same or a different exposure -- same exposure/detector/version and the
    same ``data`` reproduces an identical re-delivery, same
    exposure/detector/version with different ``data`` a checksum conflict."""
    name = name or f"{exposure}-sca01"
    location = f"{inbox}/{date}/{name}"
    prefix = _prefix(location)
    unit_id = f"{name}/SCA01" if not name.endswith("-sca01") else f"{name[:-len('-sca01')]}/SCA01"
    manifest = _manifest("delivery", "delivery", unit_id, new_ulid(), [
        {"kind": "l2-image", "format_version": "delivered", "instance": new_ulid(),
         "key": {"exposure": exposure, "detector": detector, "version": version},
         "primary": "l2/image.fits",
         "members": [_member(fake_s3, prefix, "l2/image.fits", data)]}])
    fake_s3.seed(FAKE_BUCKET, f"{prefix}/manifest.json", json.dumps(manifest).encode())
    return location, unit_id


@pytest.fixture()
def stream_world(db, fake_batch, fake_s3, batch_env, monkeypatch):
    """A complete release with a production job definition, the difference
    template and the sources table, whose spec discovers deliveries under a
    fresh ``inbox`` prefix instead of naming ``[[dates]]``."""
    tag = f"rebuild-v0.{random.randrange(10**6, 10**9)}"
    schedule = f"test-stream-{new_ulid()}"
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO releases (tag, source_revision, schema_version, image_digest, state, "
            "cut_by) VALUES (%s, %s, '20260926-01-loop-batches.sql', %s, 'complete', 'test')",
            (tag, "a" * 40, DIGEST))
        cur.execute(
            "INSERT INTO release_deployments (release, consumer, job_definition, deployed_by) "
            "VALUES (%s, 'rapid-pipeline-production', %s, 'test')", (tag, PROD_DEF))
        cur.execute("SELECT to_regclass(%s) IS NULL", (TABLE,))
        (made_table,) = cur.fetchone()
        cur.execute(f"CREATE TABLE IF NOT EXISTS {TABLE} (field integer, result_set text)")
    fake_batch.job_definitions[PROD_DEF] = "ACTIVE"

    conn = db.connection
    template_run = repository.create_run(
        conn, "scratch", "test", "stream template producer", ["difference"], "a" * 40,
        None, "20260926-01-loop-batches.sql", None, None, "prompt", "default", "rapid",
        1, False, None)
    repository.add_unit(conn, template_run, "difference", "detector-image", UNIT)
    template_attempt = repository.allocate_attempt(conn, template_run, "difference", UNIT)
    conn.commit()
    template_prefix = _prefix(TEMPLATE)
    template = _manifest(template_run, "difference", UNIT, template_attempt, [
        {"kind": "l2-image", "format_version": "1", "instance": new_ulid(),
         "key": {"unit": UNIT}, "primary": "l2/old.fits",
         "members": [_member(fake_s3, template_prefix, "l2/old.fits", b"OLD")]},
        {"kind": "reference-catalog", "format_version": "1", "instance": new_ulid(),
         "key": {"field": 101}, "primary": "ref/cat.txt",
         "members": [_member(fake_s3, template_prefix, "ref/cat.txt", b"REFCAT")]},
    ])
    fake_s3.seed(FAKE_BUCKET, f"{template_prefix}/manifest.json", json.dumps(template).encode())
    repository.register_manifest(conn, template, registering_attempt_id=template_attempt)
    conn.commit()

    inbox = f"s3://{FAKE_BUCKET}/{INBOX_NAME}/{schedule}"
    spec_key = f"control/loop/{schedule}-stream.toml"
    fake_s3.seed(FAKE_BUCKET, spec_key, f"""
[loop]
schedule = "{schedule}"
release = "{tag}"
kind = "production"
owner = "scheduler-test"
lane = "prompt"
max_attempts = 2
inbox = "{inbox}"
difference_template = "{TEMPLATE}"
""".encode())

    state = {"tag": tag, "digest": DIGEST, "schedule": schedule, "inbox": inbox,
             "spec": f"s3://{FAKE_BUCKET}/{spec_key}",
             "template": {o["kind"]: o["instance"] for o in template["outputs"]}}
    yield state

    with conn.cursor() as cur:
        cur.execute("SELECT run, promotion FROM loop_dates WHERE schedule = %s", (schedule,))
        rows = cur.fetchall()
        cur.execute("SELECT id FROM runs WHERE release = %s", (tag,))
        runs = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT id FROM promotions WHERE request_context->>'run' = ANY(%s)", (runs,))
        promotions = [r[0] for r in cur.fetchall()]
        cur.execute("DELETE FROM loop_deliveries WHERE schedule = %s", (schedule,))
        cur.execute("DELETE FROM loop_dates WHERE schedule = %s", (schedule,))
        cur.execute("DELETE FROM checks WHERE instance IN "
                    "(SELECT id FROM product_instances WHERE run = ANY(%s))", (runs,))
        cur.execute("DELETE FROM unit_inputs WHERE unit IN "
                    "(SELECT id FROM units WHERE run = ANY(%s))", (runs,))
        cur.execute(f"DELETE FROM {TABLE} WHERE result_set IN "
                    "(SELECT id FROM product_instances WHERE run = ANY(%s))", (runs,))
    _delete_run_rows(conn, runs + [r for r, _ in rows if r not in runs], promotions)
    _delete_run_rows(conn, [template_run], [])
    with conn.cursor() as cur:
        if made_table:
            cur.execute(f"DROP TABLE {TABLE}")
        else:
            cur.execute(f"DELETE FROM {TABLE} WHERE result_set NOT IN "
                        "(SELECT id FROM product_instances)")
        cur.execute("DELETE FROM release_deployments WHERE release = %s", (tag,))
        cur.execute("DELETE FROM releases WHERE tag = %s", (tag,))


def _delivery_rows(db, schedule):
    with db.cursor() as cur:
        cur.execute("SELECT processing_date::text, location, state, batch, reason "
                    "FROM loop_deliveries WHERE schedule = %s ORDER BY discovered_at, location",
                    (schedule,))
        return cur.fetchall()


def _date_rows(db, schedule):
    with db.cursor() as cur:
        cur.execute("SELECT processing_date::text, batch, run, state, record FROM loop_dates "
                    "WHERE schedule = %s ORDER BY processing_date, batch", (schedule,))
        return cur.fetchall()


def _counts(db, schedule, tag):
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM loop_dates WHERE schedule = %s", (schedule,))
        (dates,) = cur.fetchone()
        cur.execute("SELECT count(*) FROM loop_deliveries WHERE schedule = %s", (schedule,))
        (deliveries,) = cur.fetchone()
        cur.execute("SELECT count(*) FROM runs WHERE release = %s", (tag,))
        (runs,) = cur.fetchone()
    return dates, deliveries, runs


def test_discovery_end_to_end_two_dates_duplicate_conflict_and_a_new_batch(
        cli, db, fake_batch, fake_s3, stream_world, monkeypatch):
    schedule, inbox, tag = stream_world["schedule"], stream_world["inbox"], stream_world["tag"]
    stages = _FakeStages(db, fake_batch, fake_s3, execution_record={
        "image_digest": stream_world["digest"], "release": tag})
    stages.install(monkeypatch)

    # Two dates staged: both runs (and their loop_dates/loop_deliveries rows)
    # must exist before the walk fake is first called (frozen membership,
    # batches committed before any is processed). ``stages.install`` above
    # already wrapped the true ``launch_walk._reconcile``; wrap that wrapper once
    # more (not a second ``stages.install`` -- that would double-apply the
    # stage fakes) to record the count at the first call only.
    date_count_at_first_walk = []
    after_stages = launch_walk._reconcile

    def counting_reconcile(conn, run_id):
        if not date_count_at_first_walk:
            with db.cursor() as cur:
                cur.execute("SELECT count(*) FROM loop_dates WHERE schedule = %s", (schedule,))
                date_count_at_first_walk.append(cur.fetchone()[0])
        return after_stages(conn, run_id)

    monkeypatch.setattr(launch_walk, "_reconcile", counting_reconcile)

    locA, _ = _stage(fake_s3, inbox, "2027-11-01", "exp001", data=b"A" * 16)
    locB, _ = _stage(fake_s3, inbox, "2027-11-02", "exp002", data=b"B" * 16)

    result = cli("loop", "run", "--spec", stream_world["spec"], "--interval", "1")
    assert result.rc == 0, result.err + result.out
    assert date_count_at_first_walk == [2]  # both batches committed before any walk

    dates = _date_rows(db, schedule)
    assert [(d, b, s) for d, b, _, s, _ in dates] == [
        ("2027-11-01", 1, "complete"), ("2027-11-02", 1, "complete")]
    run1, run2 = dates[0][2], dates[1][2]
    assert run1 != run2

    deliveries = _delivery_rows(db, schedule)
    assert [(d, loc, s, b) for d, loc, s, b, _ in deliveries] == [
        ("2027-11-01", locA, "batched", 1), ("2027-11-02", locB, "batched", 1)]

    # A duplicate (same identity+checksum as A, new location) and a checksum
    # conflict (B's identity, different bytes) discovered together, the
    # disposition-only commit -- no new run, no new loop_dates row.
    locD, _ = _stage(fake_s3, inbox, "2027-11-02", "exp001", name="again-exp001-sca01",
                     data=b"A" * 16)  # same identity+checksum as A, new location
    locE, _ = _stage(fake_s3, inbox, "2027-11-02", "exp002", name="bad-exp002-sca01",
                     data=b"E" * 16)  # B's identity, different bytes, new location
    before = _counts(db, schedule, tag)
    rejected = cli("loop", "run", "--spec", stream_world["spec"])
    assert rejected.rc == 0, rejected.err + rejected.out
    after = _counts(db, schedule, tag)
    assert after == (before[0], before[1] + 2, before[2])  # +2 deliveries, no new date/run
    rows_by_loc = {loc: (s, r) for _, loc, s, _, r in _delivery_rows(db, schedule)}
    assert rows_by_loc[locD] == ("refused", "identical re-delivery")
    assert rows_by_loc[locE] == ("quarantined", "checksum conflict")

    # A genuinely new delivery for 2027-11-02: forms batch 2 of that date,
    # based on batch 1 of the SAME date (a later batch extends its
    # predecessor before any earlier date), not on 2027-11-01's run.
    locC, _ = _stage(fake_s3, inbox, "2027-11-02", "exp003", data=b"C" * 16)
    again = cli("loop", "run", "--spec", stream_world["spec"])
    assert again.rc == 0, again.err + again.out
    dates2 = _date_rows(db, schedule)
    assert [(d, b, s) for d, b, _, s, _ in dates2] == [
        ("2027-11-01", 1, "complete"), ("2027-11-02", 1, "complete"),
        ("2027-11-02", 2, "complete")]
    run3, record3 = dates2[2][2], dates2[2][4]
    assert run3 not in (run1, run2)
    for base in record3.get("bases", {}).values():
        if base is not None:
            assert base["run"] == run2  # 2027-11-02 batch 1's run, not 2027-11-01's

    shown = cli("loop", "show", schedule)
    assert shown.rc == 0, shown.err
    assert "batch=2" in shown.out
    assert locD in shown.out and "identical re-delivery" in shown.out
    assert locE in shown.out and "checksum conflict" in shown.out

    # Nothing left to discover: every location is now recorded. Exit 0, no
    # writes ("a firing that resumes nothing and discovers nothing").
    before_empty = _counts(db, schedule, tag)
    empty = cli("loop", "run", "--spec", stream_world["spec"])
    assert empty.rc == 0, empty.err + empty.out
    assert "nothing to discover" in empty.out
    assert _counts(db, schedule, tag) == before_empty


def test_discovery_an_empty_inbox_exits_0_with_no_writes(
        cli, db, fake_batch, fake_s3, stream_world):
    before = _counts(db, stream_world["schedule"], stream_world["tag"])
    result = cli("loop", "run", "--spec", stream_world["spec"])
    assert result.rc == 0, result.err + result.out
    assert "nothing to discover" in result.out
    assert _counts(db, stream_world["schedule"], stream_world["tag"]) == before == (0, 0, 0)


def test_discovery_crash_after_batches_committed_resumes_the_same_run(
        cli, db, fake_batch, fake_s3, stream_world, monkeypatch):
    schedule, inbox, tag = (stream_world["schedule"], stream_world["inbox"],
                            stream_world["tag"])
    _stage(fake_s3, inbox, "2027-11-01", "exp001")
    _stage(fake_s3, inbox, "2027-11-02", "exp002")

    class _Crash(RuntimeError):
        pass

    real_walk_unit = launch_walk.walk_unit

    def crashing_walk(*args, **kwargs):
        raise _Crash("simulated crash: process killed after the batches were committed")

    monkeypatch.setattr(launch_walk, "walk_unit", crashing_walk)
    # rapidpipe.cli.main's unclassified-error boundary catches any plain
    # exception escaping a command and exits 70 (ExitCode.STAGE_ERROR) --
    # the in-process stand-in for the process actually being killed; what
    # matters here is what is left in the database, not this exit code.
    crashed = cli("loop", "run", "--spec", stream_world["spec"])
    assert crashed.rc == int(ExitCode.STAGE_ERROR), crashed.err + crashed.out

    # The batches were committed (their own transaction) before the walk
    # fake ever ran.
    dates = _date_rows(db, schedule)
    assert [(d, b, s) for d, b, _, s, _ in dates] == [
        ("2027-11-01", 1, "open"), ("2027-11-02", 1, "open")]
    run1, run2 = dates[0][2], dates[1][2]
    before = _counts(db, schedule, tag)

    monkeypatch.setattr(launch_walk, "walk_unit", real_walk_unit)  # restore, not undo()
    _FakeStages(db, fake_batch, fake_s3, execution_record={
        "image_digest": stream_world["digest"], "release": tag}).install(monkeypatch)
    resumed = cli("loop", "run", "--spec", stream_world["spec"])
    assert resumed.rc == 0, resumed.err + resumed.out

    after = _counts(db, schedule, tag)
    assert after == (before[0], before[1], before[2])  # no new loop_dates row, no new run
    dates2 = _date_rows(db, schedule)
    assert [(d, b, r, s) for d, b, r, s, _ in dates2] == [
        ("2027-11-01", 1, run1, "complete"), ("2027-11-02", 1, run2, "complete")]


def test_discovery_exits_75_while_another_loop_holds_the_schedule(
        cli, db, fake_batch, fake_s3, stream_world, monkeypatch):
    schedule, inbox, tag = (stream_world["schedule"], stream_world["inbox"],
                            stream_world["tag"])
    _FakeStages(db, fake_batch, fake_s3).install(monkeypatch)
    _stage(fake_s3, inbox, "2027-11-01", "exp001")
    before = _counts(db, schedule, tag)
    with db.cursor() as cur:  # db's connection is autocommit: a session lock
        cur.execute("SELECT pg_advisory_lock(hashtext('rapidpipe.loop:' || %s))", (schedule,))
    try:
        result = cli("loop", "run", "--spec", stream_world["spec"])
    finally:
        with db.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(hashtext('rapidpipe.loop:' || %s))",
                        (schedule,))
    assert result.rc == 75
    assert f"another loop holds schedule {schedule}" in result.out
    # No discovery happened at all: the delivery staged above is untouched.
    assert _counts(db, schedule, tag) == before == (0, 0, 0)


def test_sibling_isolation_finishing_or_failing_one_batch_leaves_the_other_open(
        db, stream_world):
    """``_fail_row`` (and, by the same ``_update_row`` call, ``_finish_row``)
    is batch-qualified -- with two open batches of one date, acting on batch 1
    leaves batch 2's row untouched."""
    conn = db.connection
    schedule, tag = stream_world["schedule"], stream_world["tag"]
    spec = launch_loop.load_spec(stream_world["spec"], s3_client=None)

    def _run(purpose):
        return repository.create_run(
            conn, "production", "scheduler-test", purpose, list(launch_loop.SELECTED_STAGES),
            "a" * 40, None, "20260926-01-loop-batches.sql", None, spec.location, "prompt",
            "default", "rapid", 2, False, None, release=tag)

    run1 = _run("processing date 2027-11-05 batch 1 (schedule test)")
    run2 = _run("processing date 2027-11-05 batch 2 (schedule test)")
    date = _dt.date(2027, 11, 5)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO loop_dates (schedule, processing_date, batch, run, state, "
                    "record) VALUES (%s, %s, 1, %s, 'open', '{}'::jsonb)",
                    (schedule, date, run1))
        cur.execute("INSERT INTO loop_dates (schedule, processing_date, batch, run, state, "
                    "record) VALUES (%s, %s, 2, %s, 'open', '{}'::jsonb)",
                    (schedule, date, run2))
    conn.commit()

    out_lines = []
    code = launch_loop._fail_row(conn, spec, date, 1, run1, {}, out_lines.append,
                                 failure="simulated failure of batch 1 only")
    assert code == launch_loop.EXIT_FAILED

    with conn.cursor() as cur:
        cur.execute("SELECT batch, state, record FROM loop_dates WHERE schedule = %s "
                    "AND processing_date = %s ORDER BY batch", (schedule, date))
        rows = cur.fetchall()
    assert rows[0][0:2] == (1, "failed")
    assert rows[0][2]["failure"] == "simulated failure of batch 1 only"
    # Batch 2's row is exactly as inserted: open, empty record, untouched.
    assert rows[1] == (2, "open", {})
