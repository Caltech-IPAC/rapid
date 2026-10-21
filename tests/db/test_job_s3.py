"""``job local`` and ``job run`` under an ``s3://`` outputs root, on the in-memory S3 stand-in.

Contracts: a job under an ``s3://`` root uploads its attempt to
``<root>/<run>/<stage>/<job>/<attempt>/``, the publisher loads the rows
from the bucket and the live run binds the key it expects; when an earlier
attempt's files are already under the job's prefix, the next attempt takes
the next number and leaves them as they were.
"""

from __future__ import annotations

import contextlib
import json

import pytest

from rapidpipe.cli import main as cli
from rapidpipe.foundation import create_job, prefix, run_job
from tests.db.foundation_helpers import new_run
from tests.db.test_job_local import KEY, exposures_rows, trivial_stage
from tests.unit.fakes3 import FakeS3

ROOT = "s3://products/foundation"


@pytest.fixture()
def s3(monkeypatch, tmp_path):
    fake = FakeS3()
    monkeypatch.setattr("rapidpipe.products.storage.s3_client", lambda: fake)
    monkeypatch.setattr(prefix, "_cache", tmp_path / "cache")
    return fake


def test_job_local_uploads_publishes_from_the_bucket_and_binds(connect, s3, monkeypatch, capsys):
    conn = connect()
    run = new_run(conn, live=True)
    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT", ROOT)
    monkeypatch.setattr(cli, "_stage_run", lambda stage: trivial_stage)
    code = cli.main(["job", "local", "--run", str(run), "--stage", "trivial", "--scope", "{}",
                     "--expects", json.dumps([{"product": "exposures", "key": KEY, "binding": None}])])
    result = json.loads(capsys.readouterr().out)
    assert code == 0 and result["published"] and result["group"] is not None
    attempt = f"foundation/{run}/trivial/{result['job']}/1/"
    uploads = [key for op, key in s3.calls if op == "upload_file"]
    assert uploads == [attempt + "exposures.parquet", attempt + "manifest.json"]
    with conn.cursor() as cur:
        cur.execute("SELECT job, run FROM exposures_official WHERE expid = 7")
        assert cur.fetchall() == [(result["job"], run)]
        cur.execute("SELECT manifest->>'prefix' FROM jobs WHERE id = %s", (result["job"],))
        assert cur.fetchone()[0] == f"s3://products/{attempt}"
    conn.commit()


def test_job_run_takes_the_attempt_after_the_ones_already_in_the_bucket(connect, s3):
    conn = connect()
    run = new_run(conn, live=True)
    job = create_job(conn, run, "trivial", {})
    s3.seed("products", f"foundation/{run}/trivial/{job}/1/exposures.parquet", b"attempt one")
    result = run_job(lambda: contextlib.closing(connect()), job, ROOT, trivial_stage)
    assert (result["attempt"], result["published"]) == (2, True)
    assert exposures_rows(conn, job) == (1, ("published", 2))
    assert s3._objects[("products", f"foundation/{run}/trivial/{job}/1/exposures.parquet")] == b"attempt one"
