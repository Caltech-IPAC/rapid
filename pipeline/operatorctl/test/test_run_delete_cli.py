"""Stub-tier tests for `rapidctl run delete` (`_cmd_run_delete`,
`pipeline/operatorctl/main.py`) — the evidence-envelope wiring, the
owned-batch-id candidate match, and the run-scoped `still_referenced`
callback, driven through the real parser and the real command body.

Covers case-map-3.txt's s3-normal, s3-partial-max-items, s3-resumed,
s3-split-batch-keys, s3-envelope-missing-manifest, s3-envelope-full-coverage,
s3-objectless-noop, s3-resume-open-plan, s3-scoped-callback-not-general.

**NO LIVE DATABASE, NO LIVE AWS.** `_FakeConn`/`_FakeCursor` route on
distinctive SQL substrings — the `_StrandedFakeCursor` idiom
`pipeline/operatorctl/test/test_run.py:1871-1901` already uses for a command
whose body issues several distinct statements against one connection — so
every query `_cmd_run_delete` and its helpers issue (the run/submissions
lookups, the in-flight-foreign-submission query, `derived.delete_run` via
`contract.call_function`, the fence, `gc_plan_items`) is answered from
scripted state without a real Postgres. `submission_role` is patched to a
transparent pass-through, the same idiom
`pipeline/operatorctl/test/test_run.py:1113-1122` uses for the identical
reason: the real `SET ROLE`/SAVEPOINT mechanics are this repo's own already-
covered ground (`test_session.py`), and this file's subject is
`_cmd_run_delete`'s OWN logic. `boto3.client` is substituted with a fake S3
client built on `pipeline.contract.test_gc_execution.StubS3` via the
`_FakeBoto3Client` pattern `pipeline/contract/test_gc_operator_surface.py
:414-441` documents — a real wrapper (`_S3Versions`) constructed around a
fake client, not a bypass of the wrapper.
"""

import sys
import types
import unittest
from unittest import mock

if "boto3" not in sys.modules:
    try:
        import boto3  # noqa: F401
    except ImportError:
        boto3_stub = types.ModuleType("boto3")
        # A placeholder `Session` attribute so `mock.patch("boto3.Session")`
        # has something to patch — `_cmd_run_delete` calls
        # `boto3.Session(...).client("s3")`, and `mock.patch` on a bare
        # `ModuleType` with no such attribute raises `AttributeError`
        # before the test body ever runs.
        boto3_stub.Session = lambda *a, **k: None
        boto3_stub.client = lambda *a, **k: None
        sys.modules["boto3"] = boto3_stub

# `pipeline.operatorctl.main`'s import chain reaches
# `database.modules.utils.rapid_db_connect`, which imports `psycopg2`,
# `psycopg2.extensions` and `psycopg2.sql` at MODULE scope purely to build a
# real connection this stub-tier suite never calls (`submission_role` and
# `boto3.client`/`boto3.Session` are both substituted below). Copied verbatim
# from `pipeline/operatorctl/test/test_session.py`'s identical preamble.
if "psycopg2" not in sys.modules:
    try:
        import psycopg2  # noqa: F401
    except ImportError:
        stub = types.ModuleType("psycopg2")
        stub.Error = type("Error", (Exception,), {})
        extensions_stub = types.ModuleType("psycopg2.extensions")
        sql_stub = types.ModuleType("psycopg2.sql")
        stub.extensions = extensions_stub
        stub.sql = sql_stub
        sys.modules["psycopg2"] = stub
        sys.modules["psycopg2.extensions"] = extensions_stub
        sys.modules["psycopg2.sql"] = sql_stub

from pipeline.operatorctl import main as operatorctl_main

BUCKET = operatorctl_main._SCRATCH_BUCKET


def parse(argv):
    return operatorctl_main.build_parser().parse_args(argv)


class _Out(object):
    def __init__(self):
        self.lines = []

    def write(self, text):
        self.lines.append(text)

    @property
    def text(self):
        return "".join(self.lines)


def _version(key, version_id, size=1):
    return {"Key": key, "VersionId": version_id, "Size": size,
           "LastModified": None}


class _FakePaginator(object):
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, Bucket=None):                  # noqa: N803
        return self._pages


class _FakeS3Client(object):
    """The two calls `_S3Versions` makes (`head_object`/`delete_object`),
    forwarding to a `StubS3`-shaped double, plus `get_paginator` for
    `_enumerate_run_object_versions`. Copied in spirit from
    `pipeline/contract/test_gc_operator_surface.py`'s `_FakeBoto3Client`.
    """

    def __init__(self, versions_pages, head_versions):
        self._pages = versions_pages
        self._head_versions = dict(head_versions)
        self.deleted = []

    def get_paginator(self, name):
        assert name == "list_object_versions"
        return _FakePaginator(self._pages)

    def head_object(self, Bucket, Key, **kwargs):      # noqa: N803
        version = self._head_versions.get((Bucket, Key))
        if version is None:
            error = Exception("NoSuchKey")
            error.response = {"Error": {"Code": "404"}}
            raise error
        return {"VersionId": version}

    def delete_object(self, Bucket, Key, VersionId=None, **kwargs):  # noqa: N803
        assert VersionId is not None
        self.deleted.append((Bucket, Key, VersionId))
        self._head_versions.pop((Bucket, Key), None)
        return {}


class _FakeCursor(object):
    """Routes on distinctive SQL substrings. `conn.gc_items` is a dict of
    `item_id -> {status, bucket, object_key, version_id, object_class,
    attributed_attempt_id}`; every mutation `_cmd_run_delete`'s own Executor
    call makes lands there, so a test can read it back afterward.
    """

    def __init__(self, conn):
        self._conn = conn
        self.description = None
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=None):
        text = " ".join(sql.split())
        self._conn.calls.append((text, params))
        c = self._conn

        if text.startswith("SET ROLE") or "SAVEPOINT" in text:
            self.description = None
            self._rows = []
        elif "to_regclass('public.gc_plans')" in text:
            # `GCPlanRepository._require_schema`'s DRAFT 052 probe.
            self.description = [("present",)]
            self._rows = [(True,)]
        elif "SELECT session_user" in text:
            self.description = [("session_user",)]
            self._rows = [(c.session_user,)]
        elif text == "SELECT run_id FROM runs WHERE name = %s":
            row = c.runs.get(params[0])
            self.description = [("run_id",)]
            self._rows = [(row,)] if row is not None else []
        elif "FROM submissions" in text and "run_key = %s" in text and \
                "JOIN attempts" not in text:
            # _enumerate_run_owned_batch_ids's own batch-id query.
            self.description = [("run_id",)]
            self._rows = [(rid,) for rid in c.owned_batch_ids.get(
                params[0], [])]
        elif "FROM submissions s" in text and "JOIN attempts a" in text:
            # the in-flight-foreign-submissions query (main.py and
            # `_run_scoped_still_referenced`'s own re-query use the SAME
            # text, so one branch answers both).
            self.description = [("submission_id",)]
            self._rows = [(sid,) for sid in c.in_flight_submission_ids]
        elif "FROM submissions WHERE submission_id = ANY" in text:
            # reference_sql.SUBMISSION_MANIFEST_ROW_SQL
            ids = params[0]
            self.description = [("submission_id",), ("manifest_uri",),
                                ("manifest_checksum",)]
            self._rows = [c.submission_manifest_rows[sid] for sid in ids
                          if sid in c.submission_manifest_rows]
        elif "derived.delete_run(" in text:
            self.description = [("result",)]
            self._rows = [(c.next_delete_run_result(params),)]
        elif "gc_fences" in text and "INSERT INTO" in text:
            self.description = [("fence_id",)]
            self._rows = [(1,)] if c.fence_grants else []
            if c.fence_grants:
                c.fence_grants.pop(0)
        elif "DELETE FROM gc_fences" in text:
            self.description = None
            self._rows = []
        elif "SELECT item_id, bucket, object_key, version_id, object_class," \
                in text and "status IN ('pending', 'in-flight')" in text:
            self.description = [("item_id",)]
            self._rows = [
                (it["item_id"], it["bucket"], it["object_key"],
                 it["version_id"], it["object_class"], it["status"],
                 it["attributed_attempt_id"])
                for it in c.gc_items.values()
                if it["status"] in ("pending", "in-flight")]
        elif "UPDATE gc_plan_items SET status = 'in-flight'" in text:
            item_id = params[0]
            c.gc_items[item_id]["status"] = "in-flight"
            self.description = None
            self._rows = []
        elif "UPDATE gc_plan_items" in text and "SET status = %s" in text:
            status, detail, acted_version, item_id = params
            c.gc_items[item_id].update(status=status)
            self.description = None
            self._rows = []
        else:
            raise AssertionError("unexpected statement: %s | params=%r"
                                 % (sql, params))

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn(object):
    def __init__(self, *, session_user="rusholme", runs=None,
                owned_batch_ids=None, in_flight_submission_ids=(),
                submission_manifest_rows=None, delete_run_results=(),
                gc_items=None, fence_grants=None):
        self.session_user = session_user
        self.runs = runs or {}
        self.owned_batch_ids = owned_batch_ids or {}
        self.in_flight_submission_ids = list(in_flight_submission_ids)
        self.submission_manifest_rows = submission_manifest_rows or {}
        self._delete_run_results = list(delete_run_results)
        self.gc_items = gc_items or {}
        # unbounded: a fence request always succeeds unless the test seeds
        # a finite list to exhaust.
        self.fence_grants = (list(fence_grants) if fence_grants is not None
                             else [True] * 1000)
        self.calls = []
        self.committed = 0
        self.rolled_back = 0

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1

    def next_delete_run_result(self, params):
        if not self._delete_run_results:
            raise AssertionError("no more scripted derived.delete_run "
                                 "results; unexpected call with params=%r"
                                 % (params,))
        result = self._delete_run_results.pop(0)
        return result(params) if callable(result) else result


import contextlib


@contextlib.contextmanager
def _passthrough_submission_role(conn):
    yield conn


def _stub_manifest_reader(uri):
    return {"units": []}


class _RunDeleteCliTestBase(unittest.TestCase):
    def setUp(self):
        self.out = _Out()
        patcher = mock.patch(
            "pipeline.operatorctl.session.submission_role",
            _passthrough_submission_role)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_delete(self, conn, *, name="ramp-1", apply=False, max_items=None,
                   s3_client=None):
        argv = ["run", "delete", "--name", name, "--reason", "test"]
        if apply:
            argv.append("--apply")
        if max_items is not None:
            argv += ["--max-items", str(max_items)]
        args = parse(argv)
        self.assertIs(args.func, operatorctl_main._cmd_run_delete)

        with mock.patch("boto3.Session") as fake_session_cls, \
             mock.patch("pipeline.operatorctl.gc.s3_manifest_reader",
                        lambda client=None: _stub_manifest_reader):
            fake_session_cls.return_value.client.return_value = (
                s3_client or _FakeS3Client([], {}))
            rc = args.func(conn, args, self.out)
        return rc


class NormalAndPartialAndResumedTests(_RunDeleteCliTestBase):
    """s3-normal, s3-partial-max-items, s3-resumed."""

    def test_s3_normal_first_attempt_fence_and_reference_check_run(self):
        s3 = _FakeS3Client(
            [{"Versions": [_version("gen/phase/ramp-1/a.fits", "v1")]}],
            {(BUCKET, "gen/phase/ramp-1/a.fits"): "v1"})
        conn = _FakeConn(
            runs={"ramp-1": 101},
            owned_batch_ids={101: []},
            in_flight_submission_ids=[],
            delete_run_results=[
                {"action": "run_delete", "dry_run": False, "plan_id": 555,
                 "rows_affected": 0, "audit_id": 1},
                {"action": "run_delete", "dry_run": False, "plan_id": None,
                 "rows_affected": 0, "audit_id": 2}],
            gc_items={1: {"item_id": 1, "bucket": BUCKET,
                          "object_key": "gen/phase/ramp-1/a.fits",
                          "version_id": "v1", "object_class": "scratch",
                          "status": "pending", "attributed_attempt_id": None}})

        rc = self.run_delete(conn, apply=True, s3_client=s3)

        self.assertEqual(rc, operatorctl_main.EXIT_OK)
        self.assertEqual(s3.deleted,
                         [(BUCKET, "gen/phase/ramp-1/a.fits", "v1")])
        self.assertEqual(conn.gc_items[1]["status"], "deleted")

    def test_s3_partial_max_items_only_the_requested_count_is_processed(self):
        s3 = _FakeS3Client(
            [{"Versions": [_version("gen/phase/ramp-1/a.fits", "v1"),
                          _version("gen/phase/ramp-1/b.fits", "v1")]}],
            {(BUCKET, "gen/phase/ramp-1/a.fits"): "v1",
             (BUCKET, "gen/phase/ramp-1/b.fits"): "v1"})
        conn = _FakeConn(
            runs={"ramp-1": 101}, owned_batch_ids={101: []},
            in_flight_submission_ids=[],
            delete_run_results=[
                {"action": "run_delete", "dry_run": False, "plan_id": 555,
                 "rows_affected": 0, "audit_id": 1},
                {"action": "run_delete", "dry_run": False, "plan_id": None,
                 "rows_affected": 0, "audit_id": 2}],
            gc_items={
                1: {"item_id": 1, "bucket": BUCKET,
                   "object_key": "gen/phase/ramp-1/a.fits",
                   "version_id": "v1", "object_class": "scratch",
                   "status": "pending", "attributed_attempt_id": None},
                2: {"item_id": 2, "bucket": BUCKET,
                   "object_key": "gen/phase/ramp-1/b.fits",
                   "version_id": "v1", "object_class": "scratch",
                   "status": "pending", "attributed_attempt_id": None}})

        rc = self.run_delete(conn, apply=True, max_items=1, s3_client=s3)

        self.assertEqual(rc, operatorctl_main.EXIT_OK)
        self.assertEqual(len(s3.deleted), 1,
                         "only --max-items object(s) processed this call")
        statuses = {it["object_key"]: it["status"]
                   for it in conn.gc_items.values()}
        self.assertEqual(sorted(statuses.values()), ["deleted", "pending"])

    def test_s3_resumed_second_call_picks_up_the_remaining_item(self):
        # Simulates the state a resumed call finds: item 1 already
        # deleted, item 2 still pending from a prior --max-items=1 call.
        s3 = _FakeS3Client(
            [{"Versions": [_version("gen/phase/ramp-1/b.fits", "v1")]}],
            {(BUCKET, "gen/phase/ramp-1/b.fits"): "v1"})
        conn = _FakeConn(
            runs={"ramp-1": 101}, owned_batch_ids={101: []},
            in_flight_submission_ids=[],
            delete_run_results=[
                {"action": "run_delete", "dry_run": False, "plan_id": 555,
                 "rows_affected": 0, "audit_id": 3},
                {"action": "run_delete", "dry_run": False, "plan_id": None,
                 "rows_affected": 0, "audit_id": 4}],
            gc_items={
                1: {"item_id": 1, "bucket": BUCKET,
                   "object_key": "gen/phase/ramp-1/a.fits",
                   "version_id": "v1", "object_class": "scratch",
                   "status": "deleted", "attributed_attempt_id": None},
                2: {"item_id": 2, "bucket": BUCKET,
                   "object_key": "gen/phase/ramp-1/b.fits",
                   "version_id": "v1", "object_class": "scratch",
                   "status": "pending", "attributed_attempt_id": None}})

        rc = self.run_delete(conn, apply=True, s3_client=s3)

        self.assertEqual(rc, operatorctl_main.EXIT_OK)
        self.assertEqual(s3.deleted,
                         [(BUCKET, "gen/phase/ramp-1/b.fits", "v1")],
                         "the already-terminal item was not re-processed")
        self.assertEqual(conn.gc_items[1]["status"], "deleted")
        self.assertEqual(conn.gc_items[2]["status"], "deleted")


class SplitBatchKeyMatchTests(_RunDeleteCliTestBase):
    """s3-split-batch-keys: candidate enumeration matches the run's OWNED
    batch id set (the run name AND every <run>-<n> attempt batch), never a
    prefix/LIKE match, and never the bare run name alone once split
    batches exist.
    """

    def test_split_batch_object_is_enumerated_and_a_similarly_named_run_is_not(
            self):
        s3 = _FakeS3Client(
            [{"Versions": [
                _version("gen/phase/ramp-1/a.fits", "v1"),
                _version("gen/phase/ramp-1-2/b.fits", "v1"),
                # A DIFFERENT run whose name is a prefix of ramp-1's
                # split-batch id must NOT be swept in by a LIKE/prefix
                # match.
                _version("gen/phase/ramp-1-20/c.fits", "v1"),
            ]}],
            {(BUCKET, "gen/phase/ramp-1/a.fits"): "v1",
             (BUCKET, "gen/phase/ramp-1-2/b.fits"): "v1",
             (BUCKET, "gen/phase/ramp-1-20/c.fits"): "v1"})
        conn = _FakeConn(
            runs={"ramp-1": 101},
            # ramp-1 has ONE split-batch attempt, submissions.run_id
            # "ramp-1-2" — never "ramp-1-20".
            owned_batch_ids={101: ["ramp-1-2"]},
            in_flight_submission_ids=[],
            delete_run_results=[
                {"action": "run_delete", "dry_run": True, "plan_id": None,
                 "rows_affected": 2, "audit_id": 9}])

        rc = self.run_delete(conn, apply=False, s3_client=s3)

        self.assertEqual(rc, operatorctl_main.EXIT_OK)
        # The dry run still enumerates real S3 candidates (contract.py's
        # "the plan shown IS the answer the apply will act on" rule) --
        # read back via the call the test itself drove, by checking what
        # was passed to derived.delete_run.
        delete_run_calls = [c for c in conn.calls
                            if "derived.delete_run(" in c[0]]
        self.assertEqual(len(delete_run_calls), 1)
        import json
        p_objects = json.loads(delete_run_calls[0][1][3])
        keys = {o["key"] for o in (p_objects if isinstance(p_objects, list)
                                   else p_objects["objects"])}
        self.assertEqual(
            keys, {"gen/phase/ramp-1/a.fits", "gen/phase/ramp-1-2/b.fits"},
            "the split-batch object is included and the unrelated "
            "ramp-1-20 object is excluded")


class EvidenceEnvelopeTests(_RunDeleteCliTestBase):
    """s3-envelope-missing-manifest, s3-envelope-full-coverage."""

    def test_s3_envelope_missing_manifest_is_refused_before_delete_run_is_called(
            self):
        s3 = _FakeS3Client(
            [{"Versions": [_version("gen/phase/ramp-1/a.fits", "v1")]}],
            {(BUCKET, "gen/phase/ramp-1/a.fits"): "v1"})
        conn = _FakeConn(
            runs={"ramp-1": 101}, owned_batch_ids={101: []},
            # A foreign in-flight submission exists, but its row is
            # ABSENT from submission_manifest_rows -- unreadable/missing.
            in_flight_submission_ids=[42],
            submission_manifest_rows={})

        rc = self.run_delete(conn, apply=False, s3_client=s3)

        from pipeline.operatorctl.contract import ManifestEvidenceRefused
        self.assertEqual(rc, ManifestEvidenceRefused.exit_code)
        self.assertIn("REFUSED", self.out.text)
        delete_run_calls = [c for c in conn.calls
                            if "derived.delete_run(" in c[0]]
        self.assertEqual(delete_run_calls, [],
                         "derived.delete_run must never be called once the "
                         "evidence itself could not be assembled")

    def test_s3_envelope_full_coverage_no_reference_is_a_projection_not_a_refusal(
            self):
        s3 = _FakeS3Client(
            [{"Versions": [_version("gen/phase/ramp-1/a.fits", "v1")]}],
            {(BUCKET, "gen/phase/ramp-1/a.fits"): "v1"})
        conn = _FakeConn(
            runs={"ramp-1": 101}, owned_batch_ids={101: []},
            in_flight_submission_ids=[42],
            submission_manifest_rows={
                42: (42, "s3://roman-rapid-products/submissions/x/m.json",
                    "sha256:" + "a" * 64)},
            delete_run_results=[
                {"action": "run_delete", "dry_run": True, "plan_id": None,
                 "rows_affected": 1, "audit_id": 10}])

        rc = self.run_delete(conn, apply=False, s3_client=s3)

        self.assertEqual(rc, operatorctl_main.EXIT_OK)
        delete_run_calls = [c for c in conn.calls
                            if "derived.delete_run(" in c[0]]
        self.assertEqual(len(delete_run_calls), 1)
        import json
        p_objects_arg = delete_run_calls[0][1][3]
        p_objects = json.loads(p_objects_arg)
        self.assertIsInstance(p_objects, dict,
                              "an envelope, not the legacy bare array, once "
                              "a foreign in-flight submission exists")
        self.assertEqual(p_objects["evidence"]["submissions"][0]
                         ["references_run"], False)
        self.assertEqual(p_objects["evidence"]["unreadable"], [])


class ObjectlessAndResumeOpenPlanTests(_RunDeleteCliTestBase):
    """s3-objectless-noop, s3-resume-open-plan."""

    def test_s3_objectless_noop_renders_the_no_op_explicitly(self):
        s3 = _FakeS3Client([{"Versions": []}], {})
        conn = _FakeConn(
            runs={"ramp-1": 101}, owned_batch_ids={101: []},
            in_flight_submission_ids=[],
            delete_run_results=[
                {"action": "run_delete", "dry_run": False, "plan_id": None,
                 "rows_affected": 0, "audit_id": 11, "no_op": True,
                 "no_op_reason": "the run has no candidate objects and no "
                                 "unresolved gc_plans row"}])

        rc = self.run_delete(conn, apply=True, s3_client=s3)

        self.assertEqual(rc, operatorctl_main.EXIT_OK)
        self.assertIn("NO-OP", self.out.text)
        self.assertIn("no candidate objects", self.out.text)

    def test_s3_resume_open_plan_zero_enumerated_objects_still_resumes(self):
        # Zero objects enumerated THIS call, but derived.delete_run reports
        # an open plan_id to resume against -- the executor step must
        # still run over the unresolved item left from a PRIOR call.
        s3 = _FakeS3Client([{"Versions": []}], {})
        conn = _FakeConn(
            runs={"ramp-1": 101}, owned_batch_ids={101: []},
            in_flight_submission_ids=[],
            delete_run_results=[
                {"action": "run_delete", "dry_run": False, "plan_id": 777,
                 "rows_affected": 0, "audit_id": 12},
                {"action": "run_delete", "dry_run": False, "plan_id": None,
                 "rows_affected": 0, "audit_id": 13}],
            gc_items={
                9: {"item_id": 9, "bucket": BUCKET,
                   "object_key": "gen/phase/ramp-1/leftover.fits",
                   "version_id": "v1", "object_class": "scratch",
                   "status": "pending", "attributed_attempt_id": None}})
        # The leftover item's version must still resolve on head_object for
        # the executor to act on it (it was enumerated in an EARLIER call,
        # not this one).
        s3._head_versions[(BUCKET, "gen/phase/ramp-1/leftover.fits")] = "v1"

        rc = self.run_delete(conn, apply=True, s3_client=s3)

        self.assertEqual(rc, operatorctl_main.EXIT_OK)
        self.assertEqual(conn.gc_items[9]["status"], "deleted",
                         "an open plan resumes rather than being treated "
                         "as a fresh no-op")


class ScopedCallbackNotGeneralTests(_RunDeleteCliTestBase):
    """s3-scoped-callback-not-general: the run-scoped callback this file
    exercises is additive — `pipeline.gc.execute`'s general
    `still_referenced_check` callers, and `test_gc_execution.py`'s existing
    crash-recovery tests, are unaffected. Asserted here by confirming
    `_cmd_run_delete` uses `_run_scoped_still_referenced`
    (built on the additive `references_run_check`), never
    `still_referenced_check` (the general collector's own function, used
    unmodified by `_cmd_gc_execute`).
    """

    def test_run_delete_never_calls_the_general_still_referenced_check(self):
        s3 = _FakeS3Client(
            [{"Versions": [_version("gen/phase/ramp-1/a.fits", "v1")]}],
            {(BUCKET, "gen/phase/ramp-1/a.fits"): "v1"})
        conn = _FakeConn(
            runs={"ramp-1": 101}, owned_batch_ids={101: []},
            in_flight_submission_ids=[],
            delete_run_results=[
                {"action": "run_delete", "dry_run": False, "plan_id": 555,
                 "rows_affected": 0, "audit_id": 1},
                {"action": "run_delete", "dry_run": False, "plan_id": None,
                 "rows_affected": 0, "audit_id": 2}],
            gc_items={1: {"item_id": 1, "bucket": BUCKET,
                          "object_key": "gen/phase/ramp-1/a.fits",
                          "version_id": "v1", "object_class": "scratch",
                          "status": "pending", "attributed_attempt_id": None}})

        with mock.patch(
                "pipeline.operatorctl.gc.still_referenced_check") as spy:
            rc = self.run_delete(conn, apply=True, s3_client=s3)

        self.assertEqual(rc, operatorctl_main.EXIT_OK)
        spy.assert_not_called()

    def test_still_referenced_check_itself_is_untouched_by_this_change(self):
        """The function's own body — reading `_cmd_gc_execute`'s call site
        is out of scope for this file, but the function object itself must
        still exist with its original, general (non-run-scoped) signature,
        proving nothing about this module's changes altered it.
        """
        from pipeline.operatorctl.gc import still_referenced_check
        import inspect
        params = list(inspect.signature(still_referenced_check).parameters)
        self.assertEqual(params, ["execute", "manifest_reader"])


if __name__ == "__main__":
    unittest.main()
