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

    Also answers `list_jobs` (no paginator, so `find_job_by_name` falls back
    to the single-call path deliberately) for `batch_describer`'s callers —
    package S's re-query path. Keyed by (jobName, jobQueue, jobStatus)
    against `named_jobs`, so a test can place a job in exactly ONE of the
    states `JOB_SEARCH_STATES` loops over, matching real Batch (a job has
    one status) rather than answering every state identically, which would
    manufacture the "N jobs share this name" collision warning for every
    call. REFUSAL-CAPABLE: `list_jobs_raises`, set by a test, makes every
    `list_jobs` call raise instead of answering — the shape criteria 3 and 11
    need (a describe that raises must not be mistaken for a negative).
    """

    def __init__(self, jobs=None, chunk_limit=100, named_jobs=None,
                list_jobs_raises=None):
        self.jobs = {job["jobId"]: job for job in (jobs or [])}
        self.calls = []
        self.chunk_limit = chunk_limit
        #: `{(job_name, job_queue): (job_id, job_status)}` — a job "found" by
        #: name search, independent of `self.jobs` (which is keyed by id,
        #: for `describe_jobs`). A name with no entry here is not found, in
        #: any state `find_job_by_name` searches. `job_status` defaults to
        #: "RUNNING" when the entry is a bare id (the common case: a test
        #: only cares that the job WAS found, not which live state).
        self.named_jobs = {
            key: value if isinstance(value, tuple) else (value, "RUNNING")
            for key, value in dict(named_jobs or {}).items()}
        self.list_jobs_calls = []
        #: An exception instance (or class) to raise on every `list_jobs`
        #: call — an unreachable Batch, not a negative answer.
        self.list_jobs_raises = list_jobs_raises

    def describe_jobs(self, jobs):
        self.calls.append(list(jobs))
        if len(jobs) > self.chunk_limit:
            raise AssertionError(
                f"describe_jobs called with {len(jobs)} ids, over the "
                f"{self.chunk_limit} limit")
        return {"jobs": [self.jobs[i] for i in jobs if i in self.jobs]}

    def list_jobs(self, jobQueue, jobStatus, filters):  # noqa: N803
        self.list_jobs_calls.append((jobQueue, jobStatus, filters))
        if self.list_jobs_raises is not None:
            raise self.list_jobs_raises
        job_name = filters[0]["values"][0]
        entry = self.named_jobs.get((job_name, jobQueue))
        if entry is None or entry[1] != jobStatus:
            return {"jobSummaryList": []}
        job_id, _ = entry
        return {"jobSummaryList": [
            {"jobName": job_name, "jobId": job_id, "createdAt": 0}]}


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


#: The ONLY value `FakeConnection.route` may return to mean "this statement
#: is recognized and its effect is intentionally not modelled as data —
#: report it as ordinary void success." Distinct from Python's `None`
#: precisely so that Python's `None` stays available to a branch as an
#: ordinary "nothing found" value without being read as this sentinel by
#: accident, and so `route` returning nothing at all (falling off the end,
#: or an `if`/`elif` chain with no matching branch and no explicit
#: `return`) is a `None` that reaches `FakeCursor.execute` and is NOT this
#: sentinel — see `execute` below for what that now does.
VOID_SUCCESS = object()


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
        if handler is VOID_SUCCESS:
            self.description = None
            self.rowcount = 1
            self._rows = []
            return
        if handler is None:
            # UNRECOGNIZED SQL RAISES, LOUDLY (round-4/wave-E finding #10).
            # This used to be indistinguishable from `VOID_SUCCESS` above —
            # both were plain `None` — so a statement `route` genuinely did
            # not recognize silently became "1 row affected" instead of a
            # test failure. That let a caller believe a write had happened
            # (a CAS matched, a transition landed) when the stub had done
            # nothing at all, which is the opposite of what a stub testing
            # transaction discipline and CAS semantics is for. A test that
            # needs a new statement shape modelled gets a clear failure
            # naming the exact text, not a false green.
            raise AssertionError(
                "FakeConnection.route did not recognize this statement — "
                "add a route for it (or return stubs.VOID_SUCCESS if its "
                "effect is genuinely not worth modelling as data): "
                f"{text!r} params={params!r}")
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

    def __init__(self, rows=None, lease_granted=True,
                 submissions_available=False, submissions=None,
                 route_raises=None):
        self.rows = {row["attempt_id"]: dict(row) for row in (rows or [])}
        self.statements = []
        self.commits = 0
        self.rollbacks = 0
        self.lease_granted = lease_granted
        self.closed_attempts = {}
        #: `write_heartbeat`'s inserts into `reconciler_runs`, as
        #: `(rows_classified, poll_seconds, reconciler_host)` — recorded
        #: rather than routed to `VOID_SUCCESS` unconditionally so a test can
        #: assert on `poll_seconds` actually landing (wave-E finding #2:
        #: `ReconcilerService.poll_seconds` used to never be assigned, so
        #: every heartbeat row silently recorded NULL regardless of the real
        #: interval). Previously unrouted entirely — this statement fell
        #: through `route()` to the old blanket "unrecognized SQL is fake
        #: success" fallback (wave-E finding #10), so a test could not have
        #: asserted on it even if one had tried.
        self.heartbeats = []
        #: `submission.protocol.is_available` — False models a database
        #: without DRAFT 044, which is every existing (pre-package-S) test's
        #: assumption. A package-S test that needs `submissions` opts in.
        self.submissions_available = submissions_available
        #: `submissions` rows keyed by `submission_id`, in the shape
        #: `submission.protocol`'s own SQL constants read: `state`,
        #: `job_name`, `job_queue`, `resolution_deadline`.
        self.submissions = {row["submission_id"]: dict(row)
                            for row in (submissions or [])}
        #: REFUSAL-CAPABLE, mirroring `FakeBatch.list_jobs_raises`: a test
        #: can make one chosen query branch fail instead of answering.
        #: `{branch: exception}`, branch one of "select_attempts",
        #: "submission_for_attempt", "select_open_submissions",
        #: "update_submission" — the same four `route()` dispatches to below.
        #: Without this, a test wanting a database-read failure had no
        #: sanctioned way to get one and had to monkeypatch `conn.route`
        #: directly in the test body (the gap the verifier found in
        #: criterion 11's original test).
        self.route_raises = dict(route_raises or {})

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

        if lowered.startswith("select resolve_attempt("):
            return self._resolve_attempt(params)

        if lowered.startswith("insert into reconciler_runs"):
            # `write_heartbeat` — recorded rather than a bare `VOID_SUCCESS`
            # so `poll_seconds` threading (wave-E finding #2) is actually
            # assertable; see `self.heartbeats`'s own comment.
            self.heartbeats.append(tuple(params))
            return VOID_SUCCESS

        # `submission.protocol.is_available` — this fake models a database
        # with no `submissions` table by default (every existing reconciler
        # test predates rule 7 package S), so the probe must answer FALSE
        # rather than falling through to the unmatched-statement branch
        # below, which returns a truthy rowcount and would make every test
        # here silently believe DRAFT 044 is applied. `self.submissions_
        # available` lets a package-S test opt in.
        if "information_schema.tables" in lowered and "submissions" in lowered:
            return ([(1,)], [("?column?",)]) if self.submissions_available \
                else ([], [("?column?",)])

        if lowered.startswith("select") and "from attempts" in lowered:
            self._maybe_raise("select_attempts")
            return self._select_attempts(text, params)

        if "from submissions" in lowered and "join attempts" in lowered:
            self._maybe_raise("submission_for_attempt")
            return self._select_submission_for_attempt(text, params)

        if lowered.startswith("select") and "from submissions" in lowered:
            self._maybe_raise("select_open_submissions")
            return self._select_open_submissions()

        if lowered.startswith("update submissions"):
            self._maybe_raise("update_submission")
            return self._update_submission(text, params)

        if lowered.startswith("update attempts") and "work_unit_id = %s" in lowered \
                and "where attempt_id = %s and work_unit_id is null" in lowered:
            # `_inherit_work_unit` (finding 18): the guarded FK-inheritance
            # UPDATE. Modelled with the real guard, not folded into the
            # generic branch below, because the guard's whole POINT is that
            # a row which already carries a work_unit_id must not be
            # overwritten — a stub that always "wrote" it could not tell
            # that case from a legitimate first inheritance. Rowcount
            # reported the same way `_update_submission`'s CAS does: rows
            # present (len 1) when the guard matched, empty when it did not.
            work_unit_id, attempt_id = params
            row = self.rows.get(attempt_id)
            description = [("rowcount",)]
            if row is None or row.get("work_unit_id") is not None:
                return [], description
            row["work_unit_id"] = work_unit_id
            return [(1,)], description

        if lowered.startswith("update attempts"):
            attempt_id = params[-1] if params else None
            self.closed_attempts[attempt_id] = (text, params)
            row = self.rows.get(attempt_id)
            if row is not None and "lifecycle_state = %s" in lowered:
                row["lifecycle_state"] = params[0]
            # `submission_outcome_at_closure`'s write-once stamp
            # (`ReconcilerService._stamp_submission_outcome`, wave-E finding
            # #1) lands here too — a plain `UPDATE attempts SET
            # submission_outcome_at_closure = COALESCE(...)` with no
            # `lifecycle_state` clause — and is genuinely fine to report as
            # void success like every other `attempts` UPDATE this branch
            # already covers; modelling migration 081's COALESCE semantics
            # as data would need `self.rows` to track the column, which no
            # existing reconciler test asserts on.
            return VOID_SUCCESS

        if "derived.transition_work_unit" in lowered:
            # C1 (campaign ruling R5, migration 077): `WorkUnitWriter.
            # transition_unit` now issues this ONE call — the CAS, the
            # work-unit advisory lock, and the `unit_events` append all live
            # behind it — rather than a raw `UPDATE work_units` followed by a
            # separate `INSERT INTO unit_events`. This branch is the CAS
            # itself: `work_unit_id, from_state, to_state, writer,
            # blocked_reason, reason, detail, lock` (the eight positional
            # params `transition_unit` passes). No `self.rows` here models
            # `work_units` as data (this stub only models `attempts`), so —
            # matching this module's own stated convention for statements it
            # does not model as data — it reports success unconditionally,
            # `[(None,)]` (the function returns void), rather than
            # attempting a CAS this stub has nothing to check against.
            return [(None,)], [("transition_work_unit",)]

        return None

    def _maybe_raise(self, branch):
        """Raise this branch's declared exception, if a test set one."""
        exc = self.route_raises.get(branch)
        if exc is not None:
            raise exc

    def _resolve_attempt(self, params):
        """`AttemptWriter.resolve_attempt`'s DB function, claim-or-create.

        Modelled well enough for the reconciler suite's purposes: a row
        already carrying this (run_id, logical_job_id, scheduler_job_id,
        scheduler_attempt_index) is returned unchanged (the claim half);
        otherwise a NEW row is created and added to `self.rows` (the create
        half), with a fresh id one past the highest existing one — mirroring
        the real function's identity-resolving INSERT closely enough that
        `_inherit_work_unit` (finding 18) has an actual new row to write
        onto, rather than the placeholder rowcount the unmatched-statement
        fallback used to stand in for an attempt id.
        """
        (run_id, logical_job_id, scheduler_job_id, application_attempt_index,
         scheduler_attempt_index, created_at, submitted_at, exposure_id, sca,
         sky_tile, _schema_version) = params

        for attempt_id, row in self.rows.items():
            if (row.get("run_id") == run_id
                    and row.get("logical_job_id") == logical_job_id
                    and row.get("scheduler_job_id") == scheduler_job_id
                    and row.get("scheduler_attempt_index")
                    == scheduler_attempt_index):
                return [(attempt_id,)], [("resolve_attempt",)]

        new_id = max(self.rows, default=0) + 1
        self.rows[new_id] = {
            "attempt_id": new_id, "run_id": run_id,
            "logical_job_id": logical_job_id,
            "scheduler_job_id": scheduler_job_id,
            "lifecycle_state": "submitted",
            "application_attempt_index": application_attempt_index,
            "scheduler_attempt_index": scheduler_attempt_index,
            "exposure_id": exposure_id, "sca": sca, "sky_tile": sky_tile,
            "submitted_at": submitted_at, "started_at": None, "ended_at": None,
            "work_unit_id": None,
        }
        return [(new_id,)], [("resolve_attempt",)]

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
        elif "work_unit_id = %s and attempt_id <> %s" in lowered:
            # THE ATTEMPT-SERIES CENSUS (rule 4): the closure path asks its
            # work unit's OTHER attempts whether any was accepted and how many
            # scheduler-visible losses the series absorbed. Modelled as the
            # real predicate rather than falling through to the by-attempt_id
            # lookup below — where `params[0]` is a work_unit_id, that
            # fallback would silently return whichever attempt happened to
            # share that number, and a stub that answers the wrong question
            # confidently is worse than one that cannot answer.
            work_unit_id, excluded = params[0], params[1]
            matched = [row for row in self.rows.values()
                       if row.get("work_unit_id") == work_unit_id
                       and row.get("attempt_id") != excluded]
            matched.sort(key=lambda row: row["attempt_id"])
        else:
            attempt_id = params[0]
            row = self.rows.get(attempt_id)
            matched = [row] if row else []

        description = [(name,) for name in columns]
        return ([tuple(row.get(name) for name in columns) for row in matched],
                description)

    def _select_submission_for_attempt(self, text, params):
        """`submission.protocol.submission_for_attempt`'s join, modelled
        directly over `self.rows`/`self.submissions` rather than a real
        JOIN: the attempt row already carries `submission_id`."""
        attempt_id = params[0]
        row = self.rows.get(attempt_id)
        submission = self.submissions.get(row.get("submission_id")) \
            if row else None
        description = [("submission_id",), ("state",), ("job_name",),
                       ("job_queue",), ("resolution_deadline",)]
        if submission is None:
            return [], description
        values = (submission.get("submission_id"), submission.get("state"),
                  submission.get("job_name"), submission.get("job_queue"),
                  submission.get("resolution_deadline"))
        return [values], description

    def _select_open_submissions(self):
        """`submission.protocol.open_submissions` — `state IN ('calling',
        'unknown')`, oldest first, matching `_OPEN_SQL` exactly."""
        columns = ("submission_id", "run_id", "job_type", "job_name",
                  "job_queue", "job_definition", "state", "call_started_at",
                  "resolution_deadline", "ambiguity_detail")
        matched = [row for row in self.submissions.values()
                  if row.get("state") in ("calling", "unknown")]
        matched.sort(key=lambda row: row["submission_id"])
        description = [(name,) for name in columns]
        return ([tuple(row.get(name) for name in columns) for row in matched],
                description)

    def _update_submission(self, text, params):
        """`mark_found`/`mark_lost`/etc — the CAS `WHERE ... AND state = ...`
        matched as a rowcount, exactly as psycopg2 reports an UPDATE."""
        submission_id = params[-1]
        submission = self.submissions.get(submission_id)
        lowered = text.lower()
        if submission is None:
            return [], [("rowcount",)]  # no such row: CAS matches nothing
        if "set state = 'found'" in lowered:
            expected = ("calling", "unknown")
            new_state = "found"
        elif "set state = 'lost'" in lowered:
            expected = ("unknown",)
            new_state = "lost"
        elif "set state = 'unknown'" in lowered:
            expected = ("calling",)
            new_state = "unknown"
        elif "set state = 'bound'" in lowered:
            expected = ("calling",)
            new_state = "bound"
        elif "set state = 'calling'" in lowered:
            expected = ("prepared",)
            new_state = "calling"
        else:
            return None
        if submission.get("state") not in expected:
            return [], [("rowcount",)]  # zero rows: the CAS did not match
        submission["state"] = new_state
        if new_state == "found":
            submission["scheduler_job_id"] = params[0]
        return [(1,)], [("rowcount",)]


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
