"""``rapidpipe.launch``: a job submitted to Batch as ``job run``, or an old-contract stage run locally.

A Spot submission names its queue and one attempt (nothing retries on
Spot); a submission is recorded on the row's ``attempts``. The job environment stands for every SSM read a job would make (the
database tree, the ``[kafka]`` endpoint), is read once per process, and is
what the job's ``publish_kafka.endpoint`` then reads instead of SSM.
"""

from __future__ import annotations

import json

import pytest

from rapidpipe import launch


class _Batch:
    def __init__(self):
        self.submitted = []

    def submit_job(self, **request):
        self.submitted.append(request)
        return {"jobId": "batch-1"}


@pytest.fixture()
def batch_env(monkeypatch):
    monkeypatch.setenv("RAPIDPIPE_BATCH_JOB_QUEUE", "queue")
    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT", "s3://products/foundation")
    monkeypatch.delenv("RAPID_DB_SECRET_ID", raising=False)
    monkeypatch.delenv("RAPIDPIPE_BATCH_JOB_NAME_PREFIX", raising=False)


def test_submit_runs_job_run_for_the_job_with_the_outputs_root(batch_env):
    batch = _Batch()
    assert launch.submit(7, "difference", job_definition="definition:3", client=batch) == "batch-1"
    assert batch.submitted == [{
        "jobName": "rapid-difference-7", "jobQueue": "queue", "jobDefinition": "definition:3",
        "containerOverrides": {
            "command": ["job", "run", "--job", "7"],
            "environment": [{"name": "RAPIDPIPE_OUTPUTS_ROOT", "value": "s3://products/foundation"}]}}]


class _Conn:
    """Records the statements a submission runs and whether it committed."""

    def __init__(self):
        self.statements, self.committed = [], False

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params):
        self.statements.append((sql, params))

    def commit(self):
        self.committed = True

    def rollback(self):
        pass


def test_submit_records_the_batch_job_id_and_its_submission_on_the_job_row(batch_env):
    conn = _Conn()
    launch.submit(7, "difference", job_definition="definition", conn=conn, client=_Batch())
    sql, params = conn.statements[0]
    assert sql.startswith("UPDATE jobs SET batch_job_id = %s") and params == ("batch-1", 7)
    sql, params = conn.statements[1]
    assert "attempts" in sql and params[0] == "batch-1" and params[-1] == 7
    assert json.loads(params[2])["queue"] == "queue" and json.loads(params[2])["why"] == "first"
    assert conn.committed


class _ThrottledBatch:
    def submit_job(self, **request):
        raise RuntimeError("TooManyRequestsException: the queue's rate")


def test_a_raising_batch_request_leaves_the_row_failed_with_the_reason_and_propagates(batch_env):
    conn = _Conn()
    with pytest.raises(RuntimeError, match="TooManyRequestsException"):
        launch.submit(7, "difference", job_definition="definition", conn=conn, client=_ThrottledBatch())
    ((sql, params),) = conn.statements
    assert "SET state = 'failed'" in sql and "state <> 'published'" in sql
    assert params == ("RuntimeError: TooManyRequestsException: the queue's rate", 7) and conn.committed


def test_a_failing_id_write_after_an_accepted_request_does_not_mark_the_row_failed(batch_env):
    class Unwritable(_Conn):
        def execute(self, sql, params):
            self.statements.append((sql, params))
            raise RuntimeError("the database went away")

    conn = Unwritable()
    with pytest.raises(RuntimeError, match="went away"):
        launch.submit(7, "difference", job_definition="definition", conn=conn, client=_Batch())
    assert not any("SET state = 'failed'" in sql for sql, _ in conn.statements)


def test_a_spot_submission_names_its_queue_and_one_attempt_and_a_prompt_one_neither(batch_env):
    batch = _Batch()
    launch.submit(7, "intake", job_definition="definition", client=batch, queue="spot-q", one_attempt=True)
    launch.submit(8, "intake", job_definition="definition", client=batch)
    assert batch.submitted[0]["jobQueue"] == "spot-q" and batch.submitted[0]["retryStrategy"] == {"attempts": 1}
    assert batch.submitted[1]["jobQueue"] == "queue" and "retryStrategy" not in batch.submitted[1]
    assert launch.spot({"batch": {"spot": {"queue": "spot-q", "backlog": 2}}})["backlog"] == 2
    assert launch.spot({"batch": {}}) is None and launch.spot({}) is None


def test_submit_asks_for_the_stages_memory_and_the_definitions_stands_without_a_key(batch_env):
    settings = {"batch": {"memory_mib": {"intake": 2048}}}
    batch = _Batch()
    launch.submit(7, "intake", job_definition="definition", client=batch,
                  memory_mib=launch.memory_mib(settings, "intake"))
    launch.submit(8, "associate", job_definition="definition", client=batch,
                  memory_mib=launch.memory_mib(settings, "associate"))
    assert batch.submitted[0]["containerOverrides"]["resourceRequirements"] == \
        [{"type": "MEMORY", "value": "2048"}]
    assert "resourceRequirements" not in batch.submitted[1]["containerOverrides"]
    assert launch.memory_mib({}, "intake") is None


def test_submit_passes_the_database_secret_when_the_submitter_names_one(batch_env, monkeypatch):
    monkeypatch.setenv("RAPID_DB_SECRET_ID", "secret-name")
    batch = _Batch()
    launch.submit(7, "difference", job_definition="definition", client=batch)
    assert {"name": "RAPID_DB_SECRET_ID", "value": "secret-name"} in \
        batch.submitted[0]["containerOverrides"]["environment"]


def test_submit_refuses_without_a_queue(batch_env, monkeypatch):
    monkeypatch.delenv("RAPIDPIPE_BATCH_JOB_QUEUE")
    batch = _Batch()
    with pytest.raises(launch.LaunchError, match="RAPIDPIPE_BATCH_JOB_QUEUE"):
        launch.submit(7, "difference", job_definition="definition", client=batch)
    assert batch.submitted == []


def test_submit_refuses_an_outputs_root_a_batch_job_cannot_write(batch_env, monkeypatch):
    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT", "file:///home/someone/products")
    batch = _Batch()
    with pytest.raises(launch.LaunchError, match="s3://"):
        launch.submit(7, "difference", job_definition="definition", client=batch)
    assert batch.submitted == []


class _Ssm:
    """A fake SSM client: the database tree under ``/rapid/x`` and the three Kafka parameters."""

    def __init__(self):
        self.calls = 0

    def get_parameters_by_path(self, **kw):
        self.calls += 1
        return {"Parameters": [{"Name": f"/rapid/x/{k}", "Value": v} for k, v in (
            ("db/server", "db.example"), ("db/port", "6432"), ("db/name", "rapid"), ("db/secret-id", "s-1"))]}

    def get_parameter(self, Name):
        self.calls += 1
        return {"Parameter": {"Value": {"/k/b": "b1:9098,b2:9098", "/k/t": "alerts", "/k/m": "4194304"}[Name]}}


KAFKA = {"bootstrap_servers_parameter": "/k/b", "topic_parameter": "/k/t", "max_request_bytes_parameter": "/k/m"}


def test_the_job_environment_stands_for_every_ssm_read_and_is_read_once(monkeypatch, batch_env):
    from rapidpipe.foundation import publish_kafka

    ssm = _Ssm()
    monkeypatch.setenv("RAPID_PARAMETER_PATH", "/rapid/x")
    monkeypatch.delenv("PGHOST", raising=False)
    monkeypatch.setattr(launch, "_environments", {})
    env = launch.job_environment({"kafka": KAFKA}, ssm)
    assert env == {"PGHOST": "db.example", "PGPORT": "6432", "PGDATABASE": "rapid", "RAPID_DB_SECRET_ID": "s-1",
                   "RAPIDPIPE_KAFKA_BOOTSTRAP_SERVERS": "b1:9098,b2:9098", "RAPIDPIPE_KAFKA_TOPIC": "alerts",
                   "RAPIDPIPE_KAFKA_MAX_REQUEST_BYTES": "4194304"}
    calls = ssm.calls
    assert launch.job_environment({"kafka": KAFKA}, ssm) == env and ssm.calls == calls
    batch = _Batch()
    launch.submit(7, "packet", job_definition="d:1", client=batch, environment_extra=env)
    passed = {e["name"]: e["value"] for e in batch.submitted[0]["containerOverrides"]["environment"]}
    assert passed == {**env, "RAPIDPIPE_OUTPUTS_ROOT": "s3://products/foundation"}
    # In the job: the endpoint comes from the environment; an SSM client would fail if called.
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert publish_kafka.endpoint(KAFKA, ssm=object()) == {
        "bootstrap_servers": ["b1:9098", "b2:9098"], "topic": "alerts", "max_request_bytes": 4194304}
