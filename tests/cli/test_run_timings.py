"""Behavioural, black-box tests of ``rapidpipe run timings``: argv in,
exit code / stdout / database state out, against a real PostgreSQL with
Batch and S3 faked (see ``tests/cli/test_run_lifecycle.py``'s module
docstring for the shared setup this module reuses).
"""

from __future__ import annotations

import json

from .conftest import FAKE_BUCKET
from .test_run_lifecycle import _create_run, _kv, _seed_manifest, _submit

_TIMINGS_HEADER = (
    "stage\tunit\tattempt\tdisposition\tqueue_s\texec_s\t"
    "fetch_s\tbody_s\tpublish_s\treconcile_lag_s\tover_30m"
)


def _seed_exec_record(fake_s3, output_location, *, attempt_id, fetch_s=None, body_s=None):
    """Seed exec/<attempt>.json with a "timing" key, the way
    ``rapidpipe.stages.contract.run_stage`` writes one (direction/
    logging-timing) -- so ``reconcile`` has something to copy into
    ``scheduler_metadata["stage"]`` (direction/run-timings)."""
    bucket = FAKE_BUCKET
    assert output_location.startswith(f"s3://{bucket}/")
    prefix = output_location[len(f"s3://{bucket}/"):]
    record = {
        "settings_hash": "sha256:" + "0" * 64,
        "source_revision": None,
        "image_digest": None,
        "release": None,
        "timing": {"started": "2023-11-14T22:13:30Z", "fetch_s": fetch_s, "body_s": body_s},
    }
    fake_s3.seed(bucket, f"{prefix}/exec/{attempt_id}.json", json.dumps(record).encode())


def _submit_and_complete_with_batch_timestamps(
        cli, fake_batch, fake_s3, run_id, *, unit_id, stage="admit",
        created_at, started_at, stopped_at, fetch_s=None, body_s=None):
    """Like ``test_run_lifecycle._submit_and_complete``, but the FakeBatch
    job also carries Batch's own ``createdAt``/``startedAt``/``stoppedAt``
    (epoch milliseconds), so ``reconcile`` has something to copy into
    ``scheduler_metadata`` (direction/run-timings); when ``fetch_s``/
    ``body_s`` are given, the attempt's own exec/<attempt>.json also
    carries a "timing" key, the way a real stage invocation would."""
    submitted = _submit(cli, run_id, stage, unit_id)
    assert submitted.rc == 0, submitted.err
    attempt_id = _kv(submitted.out, "attempt")
    job_id = _kv(submitted.out, "job")
    output_location = _kv(submitted.out, "outputs")

    fake_batch.set_status(
        job_id, "SUCCEEDED", created_at=created_at, started_at=started_at,
        stopped_at=stopped_at, job_queue="fake-queue", log_stream="fake-stream")
    _seed_manifest(fake_s3, output_location, run_id=run_id, stage=stage,
                    unit_id=unit_id, attempt_id=attempt_id)
    if fetch_s is not None or body_s is not None:
        _seed_exec_record(fake_s3, output_location, attempt_id=attempt_id,
                          fetch_s=fetch_s, body_s=body_s)

    reconciled = cli("run", "reconcile", run_id)
    assert reconciled.rc == 0, reconciled.err
    return attempt_id, job_id, output_location


def test_timings_derives_queue_and_exec_seconds_from_batch_metadata(
        cli, db, fake_batch, fake_s3, batch_env):
    run_id = _create_run(cli, db, kind="scratch", purpose="timings-1")
    attempt_id, _job_id, _location = _submit_and_complete_with_batch_timestamps(
        cli, fake_batch, fake_s3, run_id, unit_id="cli-timings-001/SCA07",
        created_at=1_700_000_000_000, started_at=1_700_000_010_000,
        stopped_at=1_700_000_070_000, fetch_s=2.5, body_s=54.0)

    result = cli("run", "timings", run_id)
    assert result.rc == 0, result.err
    lines = result.out.splitlines()
    assert lines[0] == _TIMINGS_HEADER
    row = lines[1].split("\t")
    assert row[0] == "admit"
    assert row[1] == "cli-timings-001/SCA07"
    assert row[2] == attempt_id
    assert row[3] == "succeeded"
    assert row[4] == "10.0"  # queue_s
    assert row[5] == "60.0"  # exec_s
    assert row[6] == "2.5"  # fetch_s: from the stage's own exec record
    assert row[7] == "54.0"  # body_s: likewise
    assert row[8] == "-"  # publish_s: never reaches scheduler_metadata
    assert row[10] == "false"

    # The per-stage summary follows.
    blank_index = lines.index("")
    assert lines[blank_index + 1] == (
        "stage\tcount\tmedian_exec_s\tp90_exec_s\tmax_exec_s\tover_30m_count")
    summary_row = lines[blank_index + 2].split("\t")
    assert summary_row[0] == "admit"
    assert summary_row[1] == "1"


def test_timings_json_output(cli, db, fake_batch, fake_s3, batch_env):
    run_id = _create_run(cli, db, kind="scratch", purpose="timings-json")
    _submit_and_complete_with_batch_timestamps(
        cli, fake_batch, fake_s3, run_id, unit_id="cli-timings-002/SCA07",
        created_at=1_700_000_000_000, started_at=1_700_000_005_000,
        stopped_at=1_700_000_020_000)

    result = cli("run", "timings", run_id, "--json")
    assert result.rc == 0, result.err
    payload = json.loads(result.out)
    assert len(payload["attempts"]) == 1
    attempt = payload["attempts"][0]
    assert attempt["queue_s"] == 5.0
    assert attempt["exec_s"] == 15.0
    assert attempt["fetch_s"] is None
    assert payload["stages"][0]["count"] == 1


def test_timings_stage_filter(cli, db, fake_batch, fake_s3, batch_env):
    run_id = _create_run(cli, db, kind="scratch", purpose="timings-stage",
                         stages="admit,register")
    _submit_and_complete_with_batch_timestamps(
        cli, fake_batch, fake_s3, run_id, unit_id="cli-timings-003/SCA07",
        created_at=1_700_000_000_000, started_at=1_700_000_001_000,
        stopped_at=1_700_000_002_000)

    result = cli("run", "timings", run_id, "--stage", "register")
    assert result.rc == 0, result.err
    assert result.out.splitlines() == [_TIMINGS_HEADER]  # no register attempts


def test_timings_attempt_with_no_batch_metadata_prints_dashes(cli, db):
    # A local run (or an attempt older than this feature): no Batch job,
    # so no scheduler_metadata at all, but the attempt row itself exists
    # (rapidpipe.runs.local writes execution_records with an empty
    # scheduler_metadata default, same as any attempt reconcile never saw).
    run_id = _create_run(cli, db, kind="scratch", purpose="timings-no-batch")
    result = cli("run", "timings", run_id)
    assert result.rc == 0, result.err
    # No attempts at all yet for this run: header only, no rows, no
    # per-stage summary (nothing to summarise).
    assert result.out.splitlines() == [_TIMINGS_HEADER]


def test_timings_unknown_run_exits_64(cli, db):
    result = cli("run", "timings", "NOSUCHRUN")
    assert result.rc == 64
    assert "no such run" in result.err
