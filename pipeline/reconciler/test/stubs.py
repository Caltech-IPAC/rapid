"""Stubbed boundaries for the reconciler suite: scheduler, S3 tagging, database.

The object stores are not stubbed here — `runtime.boundaries.InMemoryObjectStore`
already has real create-once semantics and real checksums, and a second fake
would be a second thing to keep honest.
"""

import datetime


def ms(moment):
    """A datetime as Batch reports it: epoch milliseconds."""
    return int(moment.timestamp() * 1000)


class FakeBatch:
    """A Batch client that returns prepared job descriptions.

    Records every `describe_jobs` call so a test can assert the batching
    behaviour — that 250 ids became three calls, not 250.
    """

    def __init__(self, jobs=None, chunk_limit=100):
        self.jobs = {job["jobId"]: job for job in (jobs or [])}
        self.calls = []
        self.chunk_limit = chunk_limit

    def describe_jobs(self, jobs):
        self.calls.append(list(jobs))
        if len(jobs) > self.chunk_limit:
            raise AssertionError(
                f"describe_jobs called with {len(jobs)} ids, over the "
                f"{self.chunk_limit} limit")
        return {"jobs": [self.jobs[i] for i in jobs if i in self.jobs]}


class FakeClientError(Exception):
    """A botocore ClientError's shape, as the retention code reads it.

    Carries `response["Error"]["Code"]`, which is how absence (NoSuchKey,
    NoSuchTagSet) is told from failure (AccessDenied, throttling, a network
    fault). That distinction is review finding #16: the code used to convert
    EVERY exception to "no retention tag", and a "no tag" answer lets a
    shortening rewrite through — so a transient read failure could replace a
    failure-class bundle's retention with the shorter success expiry.

    The stub raised a bare KeyError before, which is not a shape any
    classifier could act on.
    """

    def __init__(self, code, message="stubbed s3 error"):
        super().__init__(f"{code}: {message}")
        self.response = {"Error": {"Code": code, "Message": message}}


class FakeS3Tagging:
    """The tagging subset of an S3 client, with the real replace-whole-set rule."""

    def __init__(self, missing=(), unreadable=()):
        self.tags = {}
        self.missing = set(missing)
        #: Keys whose tag read FAILS, as distinct from having no tags.
        self.unreadable = set(unreadable)
        self.put_calls = []

    def get_object_tagging(self, Bucket, Key):  # noqa: N803 - boto3 casing
        if Key in self.unreadable:
            raise FakeClientError("AccessDenied", "tag read refused")
        if Key in self.missing:
            raise FakeClientError("NoSuchKey", f"no such object {Key}")
        stored = self.tags.get((Bucket, Key))
        if stored is None:
            return {"TagSet": []}
        return {"TagSet": [{"Key": k, "Value": v}
                           for k, v in sorted(stored.items())]}

    def put_object_tagging(self, Bucket, Key, Tagging):  # noqa: N803
        if Key in self.missing:
            raise FakeClientError("NoSuchKey", f"no such object {Key}")
        self.put_calls.append((Bucket, Key, Tagging))
        # Replace the whole set, exactly as S3 does.
        self.tags[(Bucket, Key)] = {
            tag["Key"]: tag["Value"] for tag in Tagging["TagSet"]}


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = None
        self._rows = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement, params=None):
        text = statement if isinstance(statement, str) \
            else _render_composed(statement)
        self.conn.statements.append((text, params))
        handler = self.conn.route(text, params)
        if handler is None:
            self.description = None
            self.rowcount = 1
            self._rows = []
            return
        rows, description = handler
        self._rows = list(rows)
        self.description = description
        self.rowcount = len(self._rows) if description else 1

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConnection:
    """A psycopg2-shaped connection that answers the reconciler's queries.

    Deliberately routes on statement text rather than parsing SQL: the point is
    to exercise the service's logic and its transaction discipline, not to
    reimplement PostgreSQL.
    """

    def __init__(self, rows=None, lease_granted=True):
        self.rows = {row["attempt_id"]: dict(row) for row in (rows or [])}
        self.statements = []
        self.commits = 0
        self.rollbacks = 0
        self.lease_granted = lease_granted
        self.closed_attempts = {}

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    # -- routing ---------------------------------------------------------

    def route(self, text, params):
        lowered = text.lower()

        if "pg_try_advisory_xact_lock" in lowered:
            return [(self.lease_granted,)], [("locked",)]
        if "pg_advisory_xact_lock" in lowered:
            return [(None,)], [("locked",)]

        if lowered.startswith("select") and "from attempts" in lowered:
            return self._select_attempts(text, params)

        if lowered.startswith("update attempts"):
            attempt_id = params[-1] if params else None
            self.closed_attempts[attempt_id] = (text, params)
            row = self.rows.get(attempt_id)
            if row is not None and "lifecycle_state = %s" in lowered:
                row["lifecycle_state"] = params[0]
            return None

        return None

    def _select_attempts(self, text, params):
        columns = _columns_of(text)
        lowered = text.lower()
        if "lifecycle_state = any" in lowered:
            wanted = set(params[0])
            matched = [row for row in self.rows.values()
                       if row.get("lifecycle_state") in wanted]
            # The bounded supersession requery (FixA, review finding #15)
            # carries two more predicates. Modelling them is the point: a stub
            # that returned every terminal row whatever the WHERE clause said
            # could not tell a bounded requery from an unbounded rescan of all
            # history, which is the thing the bound exists to prevent.
            if "scheduler_job_id is not null" in lowered:
                matched = [row for row in matched
                           if row.get("scheduler_job_id") is not None]
            if "ended_at is not null and ended_at >= %s" in lowered:
                horizon = params[1]
                matched = [row for row in matched
                           if row.get("ended_at") is not None
                           and row["ended_at"] >= horizon]
            matched.sort(key=lambda row: row["attempt_id"])
        else:
            attempt_id = params[0]
            row = self.rows.get(attempt_id)
            matched = [row] if row else []

        description = [(name,) for name in columns]
        return ([tuple(row.get(name) for name in columns) for row in matched],
                description)


def _render_composed(statement):
    """Render a `psycopg2.sql.Composed` without a live connection.

    `Composed.as_string()` needs a real connection or cursor to quote
    identifiers — it calls into libpq — and `FakeConnection` is not one, so
    passing it raised `TypeError: argument 2 must be a connection or a
    cursor`. That made every test whose path reaches `lease.reread_attempt`
    fail on import of the SQL, not on anything the test was about.

    (Found by FixA, 2026-08-06, and PRE-EXISTING: the same failure reproduces
    on the unmodified branch. The reconciler suite was never run by W5's
    in-image runner, which is how a red suite stayed invisible — FixA's runner
    adds it.)

    Identifiers here are internal column names from a module-level tuple, so
    rendering them with plain double quotes is faithful to what psycopg2 would
    produce and needs no connection.
    """
    from psycopg2 import sql

    if isinstance(statement, sql.Identifier):
        return ".".join('"' + part.replace('"', '""') + '"'
                        for part in statement.strings)
    if isinstance(statement, sql.SQL):
        return statement.string
    if isinstance(statement, sql.Composed):
        return "".join(_render_composed(item) for item in statement.seq)
    return str(statement)


def _columns_of(text):
    """The column list of a rendered SELECT, or the known set for `*`."""
    head = text.split(" FROM ")[0]
    head = head[len("SELECT "):].strip()
    if head == "*":
        from pipeline.reconciler.service import _OPEN_COLUMNS
        return list(_OPEN_COLUMNS)
    return [part.strip().strip('"') for part in head.split(",")]


def utc(*args):
    return datetime.datetime(*args, tzinfo=datetime.timezone.utc)


def attempt_row(attempt_id=1, **overrides):
    """A submitted attempt row with the fields the reconciler reads."""
    row = {
        "attempt_id": attempt_id,
        "run_id": "run-1",
        "logical_job_id": "90000/1",
        "scheduler_job_id": "job-abc",
        "lifecycle_state": "submitted",
        # A pre-created row has NO application-observed index: the submission
        # layer cannot know it, and the runtime writes it only when it claims
        # the row from inside a running container. Its presence is therefore
        # evidence the attempt ran, which is why the DDL forbids it in
        # terminal_without_start — tests that need a claimed row set it.
        "application_attempt_index": None,
        "scheduler_attempt_index": None,
        "exposure_id": 90000,
        "sca": 1,
        "sky_tile": None,
        "submitted_at": utc(2026, 8, 6, 10, 0, 0),
        "started_at": None,
        "ended_at": None,
        "rapid_outcome": None,
        "product_disposition": None,
        "application_intended_exit": None,
        "error_category": None,
        "terminal_record_key": None,
        "terminal_record_sequence": None,
        "terminal_record_checksum": None,
        "binding_job_definition_arn": "arn:aws:batch:us-east-1:1:job-definition/rapid-pipeline-science:10",
        "binding_job_definition_rev": 10,
        "binding_image_digest": "sha256:abc",
        "binding_release_identity": "rel-1",
        "binding_manifest_checksum": "chk-1",
    }
    row.update(overrides)
    return row


def batch_job(job_id="job-abc", status="SUCCEEDED", started=None, stopped=None,
              exit_code=0, attempts=None, status_reason=None, reason=None):
    job = {
        "jobId": job_id,
        "status": status,
        "createdAt": ms(utc(2026, 8, 6, 10, 0, 0)),
        "container": {"exitCode": exit_code, "reason": reason,
                      "logStreamName": f"science/default/{job_id}"},
    }
    if started is not None:
        job["startedAt"] = ms(started)
    if stopped is not None:
        job["stoppedAt"] = ms(stopped)
    if status_reason is not None:
        job["statusReason"] = status_reason
    if attempts is not None:
        job["attempts"] = attempts
    return job
