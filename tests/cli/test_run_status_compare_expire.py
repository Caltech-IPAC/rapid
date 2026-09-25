"""Behavioural, black-box tests of ``rapidpipe run status``, ``run
compare`` and ``run expire``, plus the cleanup role ``run delete`` and
``run expire`` both honour -- argv in, exit code / stdout / stderr and
database state out, against a real PostgreSQL with Batch and S3 faked
(see ``tests/cli/test_run_lifecycle.py``'s module docstring for the
shared setup and registration correction this module reuses).
"""

from __future__ import annotations

import pytest

from rapidpipe.db.ids import new_ulid
from rapidpipe.runs import cleanup
from tests.unit.fakes3 import FakeClientError

from .conftest import FAKE_BUCKET
from .test_run_lifecycle import (
    _create_run,
    _kv,
    _register_candidate,
    _seed_manifest,
    _submit,
    _submit_and_complete,
)

_STATUS_HEADER = "stage\tunit\tstate\tselected_attempt\tlast_attempt\tlast_job\tdisposition"


# ======================================================================
# run status
# ======================================================================

def test_status_exit_codes_and_one_row_per_unit(cli, db, fake_batch, fake_s3, batch_env):
    run_id = _create_run(cli, db, kind="scratch", purpose="status-1")
    submitted = _submit(cli, run_id, "admit", "cli-status-001/SCA07")
    assert submitted.rc == 0, submitted.err
    # RUNNING (one of _UNRESOLVED_BATCH_STATUSES): a submitted-but-
    # unresolved attempt. A job FakeBatch was never told a status for
    # resolves as 'killed' on the first reconcile (launch.batch.reconcile's
    # catch-all for an unexpected/empty status), not as still-running.
    fake_batch.set_status(_kv(submitted.out, "job"), "RUNNING")

    still_running = cli("run", "status", run_id)
    assert still_running.rc == 2
    lines = still_running.out.splitlines()
    assert lines[0] == _STATUS_HEADER
    assert len(lines) == 2
    assert lines[1].split("\t")[:2] == ["admit", "cli-status-001/SCA07"]

    # SUCCEEDED with a valid manifest: complete, exit 0.
    job_id = _kv(submitted.out, "job")
    attempt_id = _kv(submitted.out, "attempt")
    output_location = _kv(submitted.out, "outputs")
    fake_batch.set_status(job_id, "SUCCEEDED")
    _seed_manifest(fake_s3, output_location, run_id=run_id, stage="admit",
                    unit_id="cli-status-001/SCA07", attempt_id=attempt_id)
    done = cli("run", "status", run_id)
    assert done.rc == 0
    assert done.out.splitlines()[1].split("\t")[2] == "complete"

    # A second unit's job FAILED: exit 1, one row per unit.
    second = _submit(cli, run_id, "admit", "cli-status-002/SCA07")
    assert second.rc == 0, second.err
    second_job = _kv(second.out, "job")
    fake_batch.set_status(second_job, "FAILED", container_exit_code=1)
    failed = cli("run", "status", run_id)
    assert failed.rc == 1
    rows = failed.out.splitlines()[1:]
    assert len(rows) == 2
    states = {row.split("\t")[1]: row.split("\t")[2] for row in rows}
    assert states == {"cli-status-001/SCA07": "complete", "cli-status-002/SCA07": "failed"}


def test_status_watch_on_a_run_with_no_units_prints_once_and_exits_2(cli, db):
    run_id = _create_run(cli, db, kind="scratch", purpose="status-empty")
    result = cli("run", "status", run_id, "--watch", "--interval", "1")
    assert result.rc == 2
    assert result.out.count(_STATUS_HEADER) == 1


# ======================================================================
# run compare
# ======================================================================

def test_compare_same_dispositions_and_instance_counts(cli, db, fake_batch, fake_s3, batch_env):
    run_a = _create_run(cli, db, kind="production", purpose="compare-a")
    run_b = _create_run(cli, db, kind="production", purpose="compare-b")
    attempt_a, _job_a, _out_a = _submit_and_complete(
        cli, fake_batch, fake_s3, run_a, unit_id="cli-cmp-001/SCA07")
    attempt_b, _job_b, _out_b = _submit_and_complete(
        cli, fake_batch, fake_s3, run_b, unit_id="cli-cmp-001/SCA07")

    key = {"k": new_ulid()}
    _register_candidate(db, run_a, "admit", attempt_a, key=key)
    _register_candidate(db, run_b, "admit", attempt_b, key=key)

    result = cli("run", "compare", run_a, run_b)
    assert result.rc == 0, result.err
    assert result.out.splitlines()[-1] == "same"


def test_compare_different_when_one_run_has_an_extra_unit(cli, db, fake_batch, fake_s3, batch_env):
    run_a = _create_run(cli, db, kind="scratch", purpose="compare-c")
    run_b = _create_run(cli, db, kind="scratch", purpose="compare-d")
    _submit_and_complete(cli, fake_batch, fake_s3, run_a, unit_id="cli-cmp-010/SCA07")
    _submit_and_complete(cli, fake_batch, fake_s3, run_b, unit_id="cli-cmp-010/SCA07")
    _submit_and_complete(cli, fake_batch, fake_s3, run_b, unit_id="cli-cmp-011/SCA07")

    result = cli("run", "compare", run_a, run_b)
    assert result.rc == 1
    assert result.out.splitlines()[-1] == "different"


def test_compare_unknown_run_exits_64(cli, db):
    run_id = _create_run(cli, db, kind="scratch", purpose="compare-e")
    result = cli("run", "compare", run_id, new_ulid())
    assert result.rc == 64
    assert "no such run" in result.err


# ======================================================================
# run expire
# ======================================================================

def test_expire_deletes_unpinned_expired_scratch_runs_and_spares_pinned(
        cli, db, fake_versioned_s3, batch_env, monkeypatch):
    monkeypatch.delenv("RAPIDPIPE_CLEANUP_ROLE_ARN", raising=False)
    unpinned = _create_run(cli, db, kind="scratch", purpose="expire-unpinned")
    pinned = _create_run(cli, db, kind="scratch", purpose="expire-pinned")
    pin = cli("run", "pin", pinned)
    assert pin.rc == 0, pin.err

    # A scratch run otherwise defaults to expiring 14 days from now
    # (runs.repository.create_run) and no CLI flag backdates it; --now
    # only widens run expire's own candidate SELECT, while the actual
    # deletion gate (runs.repository.mark_run_deleting, amendment A6)
    # re-checks 'expires_at < now()' under the row lock using the
    # database's real now(), not --now. Both runs are backdated here so
    # the pinned one is genuinely expired too -- proving its own
    # exemption (it is filtered out of the candidate query by 'NOT
    # pinned' before the gate even runs), not merely that it has not
    # expired yet.
    with db.cursor() as cur:
        cur.execute(
            "UPDATE runs SET expires_at = now() - interval '1 hour' WHERE id = ANY(%s)",
            ([unpinned, pinned],))

    result = cli("run", "expire", "--now", "2100-01-01T00:00:00+00:00")
    assert result.rc == 0, result.err
    assert f"run_id: {unpinned}" in result.out
    assert f"run_id: {pinned}" not in result.out
    assert result.out.splitlines()[-1] == "expired=1 refused=0"

    with db.cursor() as cur:
        cur.execute("SELECT state FROM runs WHERE id = %s", (unpinned,))
        assert cur.fetchone()[0] == "deleted"
        cur.execute("SELECT state FROM runs WHERE id = %s", (pinned,))
        assert cur.fetchone()[0] == "open"


# ======================================================================
# run delete: the cleanup role (RAPIDPIPE_CLEANUP_ROLE_ARN)
# ======================================================================

class _FakeSTS:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    def assume_role(self, *, RoleArn: str, RoleSessionName: str):
        self.calls.append((RoleArn, RoleSessionName))
        if self.fail:
            raise FakeClientError("AccessDenied", "not allowed")
        return {"Credentials": {"AccessKeyId": "AK", "SecretAccessKey": "SK",
                                "SessionToken": "ST"}}


def test_delete_with_the_cleanup_role_assumes_it_and_uses_the_client(
        cli, db, fake_versioned_s3, batch_env, monkeypatch):
    sts = _FakeSTS()

    def _client(service, **_kwargs):
        # One real bucket would serve both the sts and s3 clients built
        # from the assumed role; fake_versioned_s3 stands in for the s3
        # one, the same instance runs.cleanup._default_s3_client would
        # otherwise return, since this run has nothing to clean up.
        return sts if service == "sts" else fake_versioned_s3

    monkeypatch.setattr(cleanup, "_boto3_client", _client)
    monkeypatch.setenv("RAPIDPIPE_CLEANUP_ROLE_ARN", "arn:example:role/rapid-cleanup")

    run_id = _create_run(cli, db, kind="scratch", purpose="delete-role")
    result = cli("run", "delete", run_id)
    assert result.rc == 0, result.err

    assert len(sts.calls) == 1
    arn, session_name = sts.calls[0]
    assert arn == "arn:example:role/rapid-cleanup"
    assert session_name.startswith("rapidpipe-cleanup-")


def test_delete_cleanup_role_assume_failure_exits_75(cli, monkeypatch):
    sts = _FakeSTS(fail=True)
    monkeypatch.setattr(cleanup, "_boto3_client", lambda service, **_kw: sts)
    monkeypatch.setenv("RAPIDPIPE_CLEANUP_ROLE_ARN", "arn:example:role/rapid-cleanup")

    # cleanup_s3_client() is called, and can fail, before the run itself
    # is even looked up (test_cli_runctl.py's own
    # test_expire_and_delete_role_failure_exit_75 does the same with a
    # fictional run id).
    result = cli("run", "delete", "NOPE")
    assert result.rc == 75
    assert "could not assume the cleanup role" in result.err
    assert len(sts.calls) == 1
