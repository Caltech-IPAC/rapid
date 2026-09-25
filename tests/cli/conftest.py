"""Fixtures for the black-box ``rapidpipe`` CLI behavioural suite.

Every test here drives the CLI only through ``rapidpipe.cli.main.main``
(argv in, exit code / stdout / stderr out) and checks database state
afterward -- never repository functions directly, except where a test
needs to set up a registered product instance that only a real stage
process (never exercised here) would otherwise create; see
``tests/cli/README.md``.

Separate from ``tests/db`` and ``tests/unit`` so the plain unit-test job
(``.github/workflows/unit-tests.yml``) stays database-free: nothing here
is collected or imported unless ``PGHOST`` is set, mirroring
``tests/db/conftest.py``.

The CLI commits its own work (each subcommand opens a connection, does
its writes, commits, and closes -- see ``rapidpipe.cli.main``'s
``_run_model_command`` and friends), so this suite cannot use
``tests/db``'s never-committed-transaction isolation. Instead, ``cli``
runs each command against a real connection to the CI database via a
monkeypatched ``rapidpipe.cli.main.connect``, and the ``db`` fixture
deletes every row the test created at teardown, in FK order (see
``_delete_run_rows`` below; the pattern follows
``tests/db/test_launch.py``'s ``_cleanup_exposure`` -- a plain
autocommit connection, DELETE by id, and nothing else touched).
"""

from __future__ import annotations

import contextlib
import io
import os
from dataclasses import dataclass, field
from typing import Any, Callable

import pytest

from rapidpipe.cli import main as cli_main
from rapidpipe.launch import batch as launch_batch
from rapidpipe.products import storage
from rapidpipe.runs import cleanup as runs_cleanup
from tests.unit.fakebatch import FakeBatch
from tests.unit.fakes3 import FakeS3
from tests.unit.fakes3 import FakeVersionedS3

PG_VARS = ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD")

#: The bucket every env var below and every FakeS3/FakeVersionedS3 seed
#: in this suite agree on; not a real bucket, never touched over the
#: network -- both fakes are pure in-memory stand-ins.
FAKE_BUCKET = "fake-bucket"


def _pg_configured() -> bool:
    return bool(os.environ.get("PGHOST"))


@dataclass
class RunResult:
    rc: int
    out: str
    err: str


@dataclass
class _DB:
    """A live psycopg2 connection plus the ids this test wants cleaned
    up at teardown, in FK order."""

    connection: Any
    run_ids: list[str] = field(default_factory=list)
    promotion_ids: list[str] = field(default_factory=list)

    def track_run(self, run_id: str) -> str:
        self.run_ids.append(run_id)
        return run_id

    def track_promotion(self, promotion_id: str) -> str:
        self.promotion_ids.append(promotion_id)
        return promotion_id

    def cursor(self):
        return self.connection.cursor()

    def commit(self):
        self.connection.commit()

    def rollback(self):
        self.connection.rollback()


def _delete_run_rows(connection, run_ids: list[str], promotion_ids: list[str]) -> None:
    if not run_ids and not promotion_ids:
        return
    with connection.cursor() as cur:
        if promotion_ids:
            cur.execute(
                "DELETE FROM promotion_changes WHERE promotion = ANY(%s)",
                (promotion_ids,))
            cur.execute("DELETE FROM promotions WHERE id = ANY(%s)", (promotion_ids,))
        if run_ids:
            # Break the units.selected_attempt <-> attempts FK cycle
            # before either side is deleted.
            cur.execute(
                "UPDATE units SET selected_attempt = NULL WHERE run = ANY(%s)",
                (run_ids,))
            # unit_inputs (bound at submission, R4) and dependencies
            # reference both units and instances; they go first.
            cur.execute(
                "DELETE FROM unit_inputs WHERE unit IN "
                "(SELECT id FROM units WHERE run = ANY(%s)) OR producer_instance IN "
                "(SELECT id FROM product_instances WHERE run = ANY(%s))",
                (run_ids, run_ids))
            cur.execute(
                "DELETE FROM dependencies WHERE consumer_instance IN "
                "(SELECT id FROM product_instances WHERE run = ANY(%s)) OR producer_instance IN "
                "(SELECT id FROM product_instances WHERE run = ANY(%s))",
                (run_ids, run_ids))
            cur.execute(
                "DELETE FROM product_members WHERE instance IN "
                "(SELECT id FROM product_instances WHERE run = ANY(%s))",
                (run_ids,))
            cur.execute(
                "DELETE FROM result_sets WHERE instance IN "
                "(SELECT id FROM product_instances WHERE run = ANY(%s))",
                (run_ids,))
            cur.execute(
                "DELETE FROM product_instances WHERE run = ANY(%s)", (run_ids,))
            cur.execute(
                "DELETE FROM execution_records WHERE attempt IN "
                "(SELECT id FROM attempts WHERE run = ANY(%s))",
                (run_ids,))
            cur.execute("DELETE FROM attempts WHERE run = ANY(%s)", (run_ids,))
            cur.execute("DELETE FROM units WHERE run = ANY(%s)", (run_ids,))
            cur.execute("DELETE FROM runs WHERE id = ANY(%s)", (run_ids,))
    connection.commit()


@pytest.fixture()
def db():
    if not _pg_configured():
        pytest.skip("PGHOST is not set; CLI behavioural tests are skipped")
    import psycopg2

    connection = psycopg2.connect(
        host=os.environ["PGHOST"],
        port=os.environ.get("PGPORT", "5432"),
        dbname=os.environ["PGDATABASE"],
        user=os.environ["PGUSER"],
        password=os.environ.get("PGPASSWORD", ""),
    )
    connection.autocommit = True
    record = _DB(connection)
    try:
        yield record
    finally:
        try:
            _delete_run_rows(connection, record.run_ids, record.promotion_ids)
        finally:
            connection.close()


@pytest.fixture()
def cli_connect(monkeypatch):
    """Monkeypatch ``rapidpipe.cli.main.connect`` to open a fresh
    connection to the same CI database every call, mirroring the real
    ``rapidpipe.db.connection.connect`` context-manager shape (open,
    yield, close) but skipping its retry/backoff/parameter-tree
    machinery -- this suite only ever runs against a database that is
    already up. The CLI commits or rolls back itself before the ``with``
    block exits."""
    if not _pg_configured():
        pytest.skip("PGHOST is not set; CLI behavioural tests are skipped")

    @contextlib.contextmanager
    def _connect(**_kwargs):
        import psycopg2

        connection = psycopg2.connect(
            host=os.environ["PGHOST"],
            port=os.environ.get("PGPORT", "5432"),
            dbname=os.environ["PGDATABASE"],
            user=os.environ["PGUSER"],
            password=os.environ.get("PGPASSWORD", ""),
        )
        connection.autocommit = False
        try:
            yield connection
        finally:
            connection.close()

    monkeypatch.setattr(cli_main, "connect", _connect)


@pytest.fixture()
def cli(cli_connect) -> Callable[..., RunResult]:
    """``run(*argv) -> RunResult(rc, out, err)``: calls
    ``rapidpipe.cli.main.main`` in-process with stdout/stderr captured.
    A ``SystemExit`` (argparse's ``--help``, a bad argument, or
    ``--version``) is caught and its code returned rather than
    propagated."""

    def run(*argv: str) -> RunResult:
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = cli_main.main(list(argv))
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        return RunResult(rc=rc if rc is not None else 0, out=out.getvalue(), err=err.getvalue())

    return run


@pytest.fixture()
def fake_batch(monkeypatch) -> FakeBatch:
    """The one :class:`FakeBatch` instance every CLI call in this test
    sees: ``rapidpipe.launch.batch.batch_client`` is the seam both
    ``submit_unit`` and ``cancel`` fall back to when no explicit
    ``client`` is passed -- and the CLI never passes one -- so this must
    return the SAME instance every call, not a fresh one, for state
    (submitted jobs, statuses) to carry across CLI invocations within a
    test."""
    fake = FakeBatch()
    monkeypatch.setattr(launch_batch, "batch_client", lambda: fake)
    return fake


@pytest.fixture()
def fake_s3(monkeypatch) -> FakeS3:
    """The one :class:`FakeS3` instance ``rapidpipe.products.storage.s3_client``
    returns -- the seam ``reconcile``'s manifest/execution-record fetch
    falls back to (via ``fetch_object``'s ``client=None`` default) when
    the CLI calls ``run reconcile`` with no explicit ``s3_client``."""
    fake = FakeS3()
    monkeypatch.setattr(storage, "s3_client", lambda: fake)
    return fake


@pytest.fixture()
def fake_versioned_s3(monkeypatch) -> FakeVersionedS3:
    """The one :class:`FakeVersionedS3` instance
    ``rapidpipe.runs.cleanup._default_s3_client`` returns -- the seam
    ``run delete`` falls back to when the CLI calls ``cleanup.delete_run``
    with no explicit ``s3_client``. Deliberately independent of
    ``fake_s3``/``storage.s3_client``: ``delete_run`` needs versioned
    operations (``list_object_versions``/``delete_objects``) that
    :class:`FakeS3` does not implement, so this seam is patched
    separately rather than making one fake serve both shapes."""
    fake = FakeVersionedS3()
    monkeypatch.setattr(runs_cleanup, "_default_s3_client", lambda: fake)
    return fake


@pytest.fixture()
def batch_env(monkeypatch):
    """Every ``RAPIDPIPE_*`` deployment variable ``rapidpipe.launch.batch``
    reads (README, "Running on Batch"), pointed at the fake queue/
    definitions and at ``s3://fake-bucket/...`` prefixes the FakeS3/
    FakeVersionedS3 fixtures serve."""
    monkeypatch.setenv("RAPIDPIPE_BATCH_JOB_QUEUE", "test-queue")
    monkeypatch.setenv("RAPIDPIPE_BATCH_JOB_DEFINITION_SCRATCH", "test-def-scratch")
    monkeypatch.setenv("RAPIDPIPE_BATCH_JOB_DEFINITION_PRODUCTION", "test-def-production")
    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT_SCRATCH", f"s3://{FAKE_BUCKET}/scratch")
    monkeypatch.setenv("RAPIDPIPE_OUTPUTS_ROOT_PRODUCTION", f"s3://{FAKE_BUCKET}/production")
    monkeypatch.setenv("RAPIDPIPE_SCRATCH_BUCKET", FAKE_BUCKET)
    monkeypatch.delenv("RAPIDPIPE_IMAGE_DIGEST", raising=False)
