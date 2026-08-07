"""The VPO's phase wiring: what it waits on, and whether it really registers.

Round-3 finding #3. The production operator could submit reference jobs, wait
for nothing, register nothing, and abort before science — and every one of
those was a separate defect that looked like working code. These are the
properties that make the loop actually run, tested at the seam rather than by
importing `__main__`.

**Why `sys.modules` is stubbed before import.** `virtualPipelineOperator`
imports boto3, psycopg2 (through `rapid_db`) and dateutil at module scope, and
none is installed off-image. The functions under test — `wait_for_submitted`
and `production_registrar` — touch none of that machinery, so the smallest
stand-ins that make the import boundary crossable are installed here. Same
pattern and same reasoning as `pipeline/entrypoints/test/test_job.py`.
"""

import os
import sys
import types
import unittest


def _stub(name):
    module = types.ModuleType(name)
    sys.modules[name] = module
    return module


def _install_third_party_stubs():
    if "boto3" not in sys.modules:
        boto3 = _stub("boto3")
        boto3.client = lambda *_a, **_k: object()
    if "psycopg2" not in sys.modules:
        psycopg2 = _stub("psycopg2")
        # A PACKAGE, not a module: `rapid_db_connect` does
        # `import psycopg2.extensions`, which fails with "not a package"
        # against a bare module stub however many attributes it carries.
        psycopg2.__path__ = []
        psycopg2.connect = lambda *_a, **_k: None
        psycopg2.DatabaseError = Exception
        extensions = _stub("psycopg2.extensions")
        extensions.ISOLATION_LEVEL_READ_COMMITTED = 1
        extras = _stub("psycopg2.extras")
        extras.RealDictCursor = object
        sql = _stub("psycopg2.sql")
        sql.Identifier = lambda *a: None
        sql.SQL = lambda *a: None
        psycopg2.extensions = extensions
        psycopg2.extras = extras
        psycopg2.sql = sql
    if "dateutil" not in sys.modules:
        dateutil = _stub("dateutil")
        tz = _stub("dateutil.tz")
        tz.gettz = lambda *_a, **_k: None
        tz.tzutc = lambda: None
        dateutil.tz = tz


def _install_module_scope_environment():
    """`virtualPipelineOperator` reads RAPID_SW/RAPID_WORK at MODULE SCOPE and
    calls `exit(64)` when either is unset — so merely importing it terminates
    the interpreter without them. Set here rather than "fixed" there: moving
    those checks into a function is a change to the operator's startup
    contract, which is the operations design's to make, not this test's.
    """
    os.environ.setdefault("RAPID_SW", os.getcwd())
    os.environ.setdefault("RAPID_WORK", os.path.join(os.getcwd(), "work"))
    os.environ.setdefault("STARTDATETIME", "2026-08-06T00:00:00Z")
    os.environ.setdefault("ENDDATETIME", "2026-08-07T00:00:00Z")


_install_third_party_stubs()
_install_module_scope_environment()

from pipeline import virtualPipelineOperator as vpo  # noqa: E402


def Submission(batch_id):
    """A REAL `submission.submit.Submission`, not a stand-in for one.

    The local stub this replaces defined `self.run_id = run_id` and was
    docstringed "just the attribute `wait_for_submitted` reads off a real
    submission" — which asserted the belief instead of checking it. The real
    class has never had a `run_id`; its run-scoped identity is `batch_id`. So
    the production code read `getattr(submission, "run_id", None)`, got None on
    every real submission, skipped every wait, and registered over jobs that
    were still running — while these tests passed, because the double granted
    the attribute the real object lacked.

    Constructing the real frozen dataclass is what makes these routing tests
    rather than three constants: a rename or a wrong attribute name now fails
    here (`AttributeError`/`TypeError`) instead of being blessed. The other
    fields are the minimum the constructor requires; the wait reads none of
    them. (Code standards: test doubles must be able to refuse.)
    """
    from submission.submit import Submission as RealSubmission
    return RealSubmission(batch_id=batch_id, job_id="job-abc",
                          job_name="rapid-test", array_size=2,
                          manifest_uri="s3://bucket/manifest.json",
                          manifest_checksum="0" * 64, manifest=None)


class WaitForSubmittedTests(unittest.TestCase):
    """The wait must key on the ids the rows were actually stamped with."""

    def setUp(self):
        self.waited = []

        def record(_conn, run_id, timeout):
            self.waited.append(run_id)
            return {"terminal": 1}

        self._real_wait_on = vpo._wait_on
        self._real_connection = vpo.connection
        vpo._wait_on = record

        # `wait_for_submitted` opens a connection per batch; the wait itself
        # is what is under test, so the connection is a no-op context manager.
        class NullConnection:
            def __init__(self, *_a, **_k):
                pass

            def __enter__(self):
                return object()

            def __exit__(self, *_exc):
                return False

        vpo.connection = NullConnection

    def tearDown(self):
        vpo._wait_on = self._real_wait_on
        vpo.connection = self._real_connection

    def test_every_batch_is_waited_for_by_its_own_run_id(self):
        """The defect that only appears from two batches on.

        `submit_gathered` re-scopes each batch to `<run_id>-<n>` wherever there
        is more than one, so a wait on the PARENT id matches no attempt row at
        all: `wait_for_completion` finds zero attempts and returns
        immediately. One batch would have worked and two would not.
        """
        submitted = [(Submission("vpo-2026-08-06-science-0"), [1, 2]),
                     (Submission("vpo-2026-08-06-science-1"), [3, 4])]

        vpo.wait_for_submitted(submitted)

        self.assertEqual(["vpo-2026-08-06-science-0",
                          "vpo-2026-08-06-science-1"], self.waited)

    def test_a_single_batch_is_waited_for_too(self):
        submitted = [(Submission("vpo-2026-08-06-refimage"), [1])]

        vpo.wait_for_submitted(submitted)

        self.assertEqual(["vpo-2026-08-06-refimage"], self.waited)

    def test_a_submission_with_no_batch_id_does_not_silently_pass(self):
        # It cannot be waited for, but it must not look like a completed wait
        # either: the batch stays a reconciliation case and the operator is
        # told so rather than proceeding as though it had finished.
        submitted = [(Submission(None), [1])]

        results = vpo.wait_for_submitted(submitted)

        self.assertEqual([], self.waited)
        self.assertEqual([], results)

    def test_nothing_submitted_waits_for_nothing(self):
        self.assertEqual([], vpo.wait_for_submitted([]))


class ProductionRegistrarTests(unittest.TestCase):
    """Production must default to production."""

    def setUp(self):
        self._saved = {name: os.environ.get(name)
                       for name in ("RAPID_VPO_DRY_RUN",
                                    "RAPID_RECORDS_BUCKET")}
        for name in self._saved:
            os.environ.pop(name, None)

    def tearDown(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_a_dry_run_must_be_asked_for_by_name(self):
        """Omitting the callback used to BE the dry run.

        `run_registration` passes `dry_run=register is None`, so the whole
        production path was a rehearsal that reported its decisions as
        registrations. The rehearsal now needs an explicit flag, and the
        default is the real thing.
        """
        os.environ["RAPID_VPO_DRY_RUN"] = "true"

        self.assertIsNone(vpo.production_registrar())

    def test_the_default_is_not_a_dry_run(self):
        # Without the flag the registrar is real — which here means it
        # demands the records bucket rather than quietly returning None. A
        # None return is exactly the dry run this finding is about, so
        # "it returned None" is the one outcome that must not happen by
        # default.
        os.environ["RAPID_RECORDS_BUCKET"] = "roman-rapid-records"

        try:
            callback = vpo.production_registrar()
        except SystemExit:  # pragma: no cover - would be a different defect
            self.fail("a configured registrar must not exit")
        except ImportError:
            # boto3/psycopg2 absent off-image: the point is only that this
            # path does NOT return None.
            return

        self.assertIsNotNone(callback)

    def test_a_missing_records_bucket_is_refused_not_defaulted(self):
        # The registrar reads each attempt's terminal record from this
        # bucket. Guessing a name would fail per-attempt deep inside a pass;
        # refusing here is one message before any work is done.
        with self.assertRaises(SystemExit) as caught:
            vpo.production_registrar()

        self.assertEqual(64, caught.exception.code)


class SubmissionEnvRoutingTests(unittest.TestCase):
    """Round-4 finding #1: each phase submits to ITS OWN queue and definition.

    `submission_env` took `job_type` and ignored it, returning one singular
    `RAPID_JOB_QUEUE`/`RAPID_JOB_DEFINITION` pair to all three phases. The
    route matrix does not allow that — reference-image runs on the BULK class
    and science and post-process on PROMPT — so whichever pair was
    configured, at least one phase was submitted to a queue whose job
    definition names the other class, and `validate_route` rejects it at the
    entrypoint before any processing.

    Asserted at the SUBMIT-CALL BOUNDARY: what `submission_env` resolves is
    what the three call sites pass straight into `submit_gathered` as `queue=`
    and `job_definition=`, so nothing has to be submitted to AWS to know which
    queue a phase would reach.
    """

    #: The tree as it really stands (verified live, 2026-08-06:
    #: `aws ssm get-parameters-by-path --path /rapid/pipeline/batch`).
    TREE = {
        "batch/queue-bulk": "rapid-queue-bulk",
        "batch/queue-prompt": "rapid-queue-prompt",
        "batch/job-definition-bulk": "rapid-pipeline-bulk",
        "batch/job-definition-science": "rapid-pipeline-science",
    }

    #: The ACTIVE revision each family really resolves to. DELIBERATELY
    #: DIFFERENT per family and deliberately not 7: the defect this replaces
    #: declared one process-wide revision for both, so a test where the two
    #: families share a number could not tell a resolved revision from a
    #: declared one.
    REVISIONS = {
        "rapid-pipeline-bulk": 11,
        "rapid-pipeline-science": 14,
    }

    ACCOUNT_ARN = "arn:aws:batch:us-east-1:ACCOUNT:job-definition/{}:{}"

    class FakeBatch:
        """`describe_job_definitions`, exact-match on family, ascending.

        Mirrors the two properties the resolver depends on: the name filter
        is exact, and Batch returns revisions oldest-first so the last is the
        ACTIVE one a bare-name submission would have reached.
        """

        def __init__(self, revisions, arn_template):
            self.revisions = revisions
            self.arn_template = arn_template
            self.calls = []

        def describe_job_definitions(self, jobDefinitionName=None,
                                     status=None):
            self.calls.append((jobDefinitionName, status))
            revision = self.revisions.get(jobDefinitionName)
            if revision is None:
                return {"jobDefinitions": []}
            # Two revisions, ascending, so "last is ACTIVE" is exercised
            # rather than accidentally satisfied by a single-element list.
            return {"jobDefinitions": [
                {"jobDefinitionName": jobDefinitionName,
                 "jobDefinitionArn": self.arn_template.format(
                     jobDefinitionName, revision - 1),
                 "revision": revision - 1,
                 "containerProperties": {"image": "repo@sha256:" + "1" * 64}},
                {"jobDefinitionName": jobDefinitionName,
                 "jobDefinitionArn": self.arn_template.format(
                     jobDefinitionName, revision),
                 "revision": revision,
                 "containerProperties": {"image": "repo@sha256:" + "2" * 64}},
            ]}

    def setUp(self):
        self._saved = {name: os.environ.get(name)
                       for name in ("RAPID_JOB_DEFINITION_REV",
                                    "RAPID_IMAGE_DIGEST",
                                    "RAPID_RELEASE_IDENTITY",
                                    "RAPID_MANIFEST_BUCKET",
                                    "RAPID_JOB_QUEUE",
                                    "RAPID_JOB_DEFINITION")}
        # The revision is RESOLVED from Batch now, never declared. Left set to
        # a WRONG value on purpose: if anything still reads it, the assertions
        # below on the resolved revisions fail loudly instead of passing by
        # coincidence.
        os.environ["RAPID_JOB_DEFINITION_REV"] = "7"
        os.environ["RAPID_IMAGE_DIGEST"] = "sha256:" + "0" * 64
        os.environ["RAPID_RELEASE_IDENTITY"] = "w8-test"
        os.environ["RAPID_MANIFEST_BUCKET"] = "rapid-manifests"
        # The env vars this used to read are deliberately left UNSET: a
        # binding resolved from them rather than from the tree would now be
        # a silent regression, so the test would rather fail loudly.
        os.environ.pop("RAPID_JOB_QUEUE", None)
        os.environ.pop("RAPID_JOB_DEFINITION", None)

    def tearDown(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def _resolve(self, job_type, batch_client=None):
        if batch_client is None:
            batch_client = self.FakeBatch(self.REVISIONS, self.ACCOUNT_ARN)
        # Both clients are injected: resolving a binding must not require AWS
        # credentials or a region, and building a real S3 client here made
        # these tests fail under `unittest discover` (no region in scope)
        # while passing when the module was run alone.
        return vpo.submission_env(job_type, parameters=dict(self.TREE),
                                  batch_client=batch_client,
                                  s3_client=object())

    # -- one test per phase, as the direction asks -------------------------

    def test_reference_image_is_submitted_to_the_bulk_class(self):
        context = self._resolve(vpo.routes.JOB_TYPE_REFERENCE_IMAGE)

        self.assertEqual("rapid-queue-bulk", context["queue"])
        self.assertEqual(
            self.ACCOUNT_ARN.format("rapid-pipeline-bulk", 11),
            context["job_definition"])
        self.assertEqual(vpo.routes.CLASS_BULK, context["workload_class"])

    def test_science_is_submitted_to_the_prompt_class(self):
        context = self._resolve(vpo.routes.JOB_TYPE_SCIENCE)

        self.assertEqual("rapid-queue-prompt", context["queue"])
        self.assertEqual(
            self.ACCOUNT_ARN.format("rapid-pipeline-science", 14),
            context["job_definition"])
        self.assertEqual(vpo.routes.CLASS_PROMPT, context["workload_class"])

    def test_post_process_is_submitted_to_the_prompt_class(self):
        context = self._resolve(vpo.routes.JOB_TYPE_POST_PROCESS)

        self.assertEqual("rapid-queue-prompt", context["queue"])
        self.assertEqual(
            self.ACCOUNT_ARN.format("rapid-pipeline-science", 14),
            context["job_definition"])
        self.assertEqual(vpo.routes.CLASS_PROMPT, context["workload_class"])

    # -- and the property that makes those three a routing test ------------

    def test_the_three_phases_do_not_share_one_binding(self):
        """The defect stated directly: reference and science must differ.

        Each assertion above would still pass if `submission_env` returned a
        constant that happened to match — this is the one that cannot.
        """
        reference = self._resolve(vpo.routes.JOB_TYPE_REFERENCE_IMAGE)
        science = self._resolve(vpo.routes.JOB_TYPE_SCIENCE)

        self.assertNotEqual(reference["queue"], science["queue"])
        self.assertNotEqual(reference["job_definition"],
                            science["job_definition"])

    def test_the_binding_recorded_is_the_definition_submitted_to(self):
        """Submitted ARN == recorded ARN == a VERSIONED ARN (round-5).

        The equality alone is not the property. This test used to assert only
        that the two matched, which a bare family name satisfies trivially —
        both sides were `rapid-pipeline-science`, equal and both unpinned,
        while Batch resolved the revision at submission and nothing recorded
        which one it picked. So the versioned-ness is asserted here too, and
        the revision is checked against the one the family really resolves to
        rather than against the environment's declaration.
        """
        expected = {
            vpo.routes.JOB_TYPE_REFERENCE_IMAGE: ("rapid-pipeline-bulk", 11),
            vpo.routes.JOB_TYPE_SCIENCE: ("rapid-pipeline-science", 14),
            vpo.routes.JOB_TYPE_POST_PROCESS: ("rapid-pipeline-science", 14),
        }
        for job_type, (family, revision) in expected.items():
            with self.subTest(job_type=job_type):
                context = self._resolve(job_type)
                binding = context["binding"]

                submitted = context["job_definition"]
                self.assertEqual(submitted, binding.job_definition_arn)

                # ...and it is a versioned ARN, not a family name.
                self.assertEqual(self.ACCOUNT_ARN.format(family, revision),
                                 submitted)
                self.assertTrue(submitted.rpartition(":")[2].isdigit(),
                                "submitted definition is not revision-pinned: "
                                + submitted)
                self.assertEqual(revision, binding.job_definition_rev)

                # The stale env var says 7. Nothing may have read it.
                self.assertNotEqual(7, binding.job_definition_rev)

    def test_the_two_families_resolve_to_their_own_revisions(self):
        """One process-wide revision cannot describe two families.

        The defect stated as a property: bulk and science revise
        independently, so a binding whose revision came from the environment
        was wrong for at least one of them whatever the value.
        """
        reference = self._resolve(vpo.routes.JOB_TYPE_REFERENCE_IMAGE)
        science = self._resolve(vpo.routes.JOB_TYPE_SCIENCE)

        self.assertNotEqual(reference["binding"].job_definition_rev,
                            science["binding"].job_definition_rev)
        self.assertEqual(11, reference["binding"].job_definition_rev)
        self.assertEqual(14, science["binding"].job_definition_rev)

    def test_the_family_is_selected_by_exact_name_and_active_status(self):
        """The describe call is filtered, not scanned and matched here."""
        batch = self.FakeBatch(self.REVISIONS, self.ACCOUNT_ARN)
        self._resolve(vpo.routes.JOB_TYPE_SCIENCE, batch_client=batch)

        self.assertEqual([("rapid-pipeline-science", "ACTIVE")], batch.calls)

    def test_the_recorded_identity_is_what_the_reconciler_will_observe(self):
        """The binding's identity must equal Batch's own report (#11).

        This is what the fix is FOR. `definition_identity` synthesizes
        `<arn>:<rev>` when the recorded ARN carries no revision, so a bare
        name plus an environment revision produced an identity that disagreed
        with the real job — and the reconciler recorded drift on attempts
        that ran under exactly the definition they were submitted to.
        """
        from observability.attempts import ExecutionBinding

        context = self._resolve(vpo.routes.JOB_TYPE_SCIENCE)
        submission = context["binding"]

        binding = ExecutionBinding(
            job_definition_arn=submission.job_definition_arn,
            image_digest=submission.image_digest,
            manifest_checksum="c" * 64,
            job_definition_rev=submission.job_definition_rev,
            release_identity=submission.release_identity)

        # What Batch reports for the job it actually ran.
        observed = self.ACCOUNT_ARN.format("rapid-pipeline-science", 14)
        self.assertEqual(observed, binding.definition_identity)

    def test_an_ambiguous_family_is_refused(self):
        """Two definitions behind one name: refuse, do not choose."""
        class Ambiguous:
            def describe_job_definitions(self, jobDefinitionName=None,
                                         status=None):
                return {"jobDefinitions": [
                    {"jobDefinitionName": "rapid-pipeline-science",
                     "jobDefinitionArn": "arn:...science:1", "revision": 1,
                     "containerProperties": {}},
                    {"jobDefinitionName": "rapid-pipeline-science-old",
                     "jobDefinitionArn": "arn:...science-old:2", "revision": 2,
                     "containerProperties": {}},
                ]}

        with self.assertRaises(RuntimeError) as caught:
            self._resolve(vpo.routes.JOB_TYPE_SCIENCE,
                          batch_client=Ambiguous())
        self.assertIn("more than one", str(caught.exception))

    def test_a_family_with_no_active_revision_is_refused(self):
        """Better to fail here than to submit under a definition that is
        not there."""
        class Empty:
            def describe_job_definitions(self, jobDefinitionName=None,
                                         status=None):
                return {"jobDefinitions": []}

        with self.assertRaises(RuntimeError) as caught:
            self._resolve(vpo.routes.JOB_TYPE_SCIENCE, batch_client=Empty())
        self.assertIn("no ACTIVE revision", str(caught.exception))

    def test_every_resolved_queue_is_the_one_the_entrypoint_will_check(self):
        """`validate_route` re-derives the queue from the SAME tree key.

        Submitting to a queue the entrypoint's own check would reject is the
        failure mode this finding describes, so the two are compared here
        rather than assumed to agree.
        """
        for job_type in (vpo.routes.JOB_TYPE_REFERENCE_IMAGE,
                         vpo.routes.JOB_TYPE_SCIENCE,
                         vpo.routes.JOB_TYPE_POST_PROCESS):
            with self.subTest(job_type=job_type):
                context = self._resolve(job_type)
                route = vpo.routes.validate_route(
                    job_type, context["workload_class"],
                    queue_name=context["queue"],
                    queue_names=dict(self.TREE))
                self.assertEqual(job_type, route.job_type)

    def test_a_tree_without_this_route_s_keys_is_refused(self):
        """Guessing would submit to whatever the last phase used — which is
        the defect. One clear message, before any submission."""
        with self.assertRaises(SystemExit) as caught:
            vpo.submission_env(vpo.routes.JOB_TYPE_REFERENCE_IMAGE,
                               parameters={"batch/queue-prompt": "q"})

        self.assertEqual(64, caught.exception.code)


class RegistrarConnectionTests(unittest.TestCase):
    """Round-4 finding #2: the callback borrows the pass's OWN connection.

    `production_registrar` returned a callback built over
    `registrar(rapid_db.RAPIDDB, store)` — the class, as a factory — so the
    registrar opened a SECOND, autocommitting connection while
    `run_registration(regconn, ...)` advanced the watermark on the first. Two
    connections cannot be one transaction: product rows became durable before
    the watermark was attempted, and a crash between them left rows written
    with the attempt still a candidate.

    THIS TEST FAILS ON TWO CONNECTIONS. What it inspects is the database
    handle the registrar would build — whether it is `RAPIDDB.borrowing(conn)`
    over the connection the pass holds, or a fresh one of the registrar's own.
    """

    def setUp(self):
        self._saved = {name: os.environ.get(name)
                       for name in ("RAPID_VPO_DRY_RUN",
                                    "RAPID_RECORDS_BUCKET")}
        os.environ.pop("RAPID_VPO_DRY_RUN", None)
        os.environ["RAPID_RECORDS_BUCKET"] = "roman-rapid-records"

    def tearDown(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_the_factory_binds_the_handle_to_the_connection_it_is_given(self):
        from database.modules.utils import rapid_db

        borrowed = []
        opened = []

        class FakeHandle:
            pass

        def fake_borrowing(conn):
            borrowed.append(conn)
            return FakeHandle()

        def fake_construct(*args, **kwargs):
            opened.append((args, kwargs))
            return FakeHandle()

        real_borrowing = rapid_db.RAPIDDB.borrowing
        rapid_db.RAPIDDB.borrowing = staticmethod(fake_borrowing)
        self.addCleanup(setattr, rapid_db.RAPIDDB, "borrowing", real_borrowing)

        captured = {}

        def fake_registrar(dbh, store):
            captured["dbh"] = dbh
            captured["store"] = store
            return lambda *a, **k: None

        import pipeline.registration.products as products_mod
        real_registrar = products_mod.registrar
        products_mod.registrar = fake_registrar
        self.addCleanup(setattr, products_mod, "registrar", real_registrar)

        factory = vpo.production_registrar()
        self.assertIsNotNone(factory)

        pass_connection = object()
        callback = vpo.registration_callback(factory, pass_connection)
        self.assertIsNotNone(callback)

        # The registrar takes its handle as a CALLABLE; invoking it is what
        # would open a connection, so that is where the split shows.
        handle = captured["dbh"]()
        self.assertIsInstance(handle, FakeHandle)

        # THE ASSERTION THE OLD SHAPE FAILS. `registrar(rapid_db.RAPIDDB,
        # store)` would have called the CLASS here — opening a second,
        # autocommitting connection and recording nothing in `borrowed`.
        self.assertEqual([pass_connection], borrowed,
                         "the registrar must borrow the pass's connection, "
                         "not open one of its own")

    def test_each_phase_binds_to_its_own_connection(self):
        """Three phases, three registration connections, three bindings.

        One callback built once could only ever borrow one of them, which is
        why the factory is a factory. A regression to a single shared callback
        shows up here as the same connection borrowed three times.
        """
        from database.modules.utils import rapid_db

        borrowed = []

        real_borrowing = rapid_db.RAPIDDB.borrowing
        rapid_db.RAPIDDB.borrowing = staticmethod(
            lambda conn: borrowed.append(conn) or object())
        self.addCleanup(setattr, rapid_db.RAPIDDB, "borrowing", real_borrowing)

        captured = []

        import pipeline.registration.products as products_mod
        real_registrar = products_mod.registrar
        products_mod.registrar = (
            lambda dbh, store: captured.append(dbh) or (lambda *a, **k: None))
        self.addCleanup(setattr, products_mod, "registrar", real_registrar)

        factory = vpo.production_registrar()

        connections = [object(), object(), object()]
        for conn in connections:
            vpo.registration_callback(factory, conn)

        for dbh in captured:
            dbh()

        self.assertEqual(connections, borrowed)

    def test_a_dry_run_factory_stays_none_through_the_seam(self):
        """`run_registration` passes `dry_run=register is None`, so binding a
        dry run to a connection must not manufacture a callback and turn a
        rehearsal into a production write."""
        os.environ["RAPID_VPO_DRY_RUN"] = "true"

        factory = vpo.production_registrar()

        self.assertIsNone(factory)
        self.assertIsNone(vpo.registration_callback(factory, object()))


class MjdWindowTests(unittest.TestCase):
    """The window's two values are bound into the readiness query."""

    def test_the_window_is_built_in_floats_not_numpy_scalars(self):
        """astropy returns `numpy.float64`, and psycopg2 has no adapter for
        it — so it reprs the value, which under NumPy 2 is
        `np.float64(61679.0)`. Pasted into SQL that reads as a
        schema-qualified name and Postgres fails with `schema "np" does not
        exist`, aborting the transaction. Gathering then reports zero ready
        pairs, which looks exactly like a night with no data. Asserting the
        exact type, not just the value: `np.float64` compares equal to a
        float, so an equality check would pass while the bug was live."""
        start, end = vpo.mjd_window("2027-10-01 00:00:00",
                                    "2027-10-08 00:00:00")

        self.assertIs(type(start), float)
        self.assertIs(type(end), float)
        self.assertAlmostEqual(61679.0, start, places=6)
        self.assertAlmostEqual(61686.0, end, places=6)

    def test_the_window_survives_being_formatted_into_a_repr(self):
        """The failure mode was a repr, so pin the repr itself: a bare
        number, with no `np.` qualifier anywhere in it."""
        for value in vpo.mjd_window("2027-10-01 00:00:00",
                                    "2027-10-08 00:00:00"):
            self.assertNotIn("np.", repr(value))


if __name__ == "__main__":
    unittest.main()
