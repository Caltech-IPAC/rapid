"""
File:    test_publisher_startup.py

Brief E's acceptance criterion 10: the entry points, and the publisher's
fail-closed startup without credentials.

**WHY FAIL-CLOSED STARTUP IS A TEST AND NOT A REVIEW COMMENT.** A publisher
that starts without broker configuration looks perfectly healthy — the process
is up, systemd is satisfied, the journal is quiet — while delivering nothing.
That is the 2026-08-04 Q7 finding's exact shape ("a pipeline that reports
published alerts while publishing nothing is worse than one that crashes"), and
the only way to know the refusal is real is to run the thing with the
configuration removed and watch it refuse.

**THE ENTRY POINTS ARE CHECKED AS DECLARATIONS HERE, NOT AS INSTALLED SCRIPTS.**
Whether the console scripts actually resolve after `pip install -e .` is checked
by the harness (`scripts/contract-brief-e-on-rapid-admin.sh`) and by the
workflow's own entry-point loop, both of which run against a real installation.
What this file asserts is the declaration side: that `pyproject.toml` names
every entry point the workflow loop tests, and that each named module:function
actually exists and is importable. The two halves catch different failures — a
declared-but-missing function, and an installed-but-broken script — and neither
subsumes the other.
"""

import importlib
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]

#: Every console script the distribution declares, as (name, module, function).
#: Five after brief E: B's three services, G's `rapidctl`, and E's publisher.
EXPECTED_ENTRY_POINTS = (
    ("rapid-reconciler", "pipeline.reconciler.main", "main"),
    ("rapid-operator", "pipeline.operator.service", "main"),
    ("rapid-job", "pipeline.entrypoints.job", "main"),
    ("rapidctl", "pipeline.operatorctl.main", "main"),
    ("rapid-publisher", "pipeline.publisher.service", "main"),
)


def _pyproject_scripts():
    """The `[project.scripts]` table, parsed from the file itself.

    Read and parsed rather than introspected from installed metadata: the
    question is what this SOURCE TREE declares, and reading installed metadata
    would answer a question about the environment instead — passing against a
    stale wheel built before the entry point was added.
    """
    import tomllib

    with open(ROOT / "pyproject.toml", "rb") as handle:
        return tomllib.load(handle).get("project", {}).get("scripts", {})


class EntryPointDeclarationTests(unittest.TestCase):
    """Every entry point is declared, and every declaration resolves."""

    def test_all_five_entry_points_are_declared(self):
        declared = _pyproject_scripts()
        for name, module, function in EXPECTED_ENTRY_POINTS:
            self.assertIn(name, declared,
                          f"{name} is not in [project.scripts]")
            self.assertEqual(declared[name], f"{module}:{function}")

    def test_the_workflow_loop_tests_every_declared_entry_point(self):
        """The CI loop and the declaration list must not drift apart.

        This is the gap brief E found and closed: `rapidctl` had been declared
        since brief G and the workflow's loop still named three commands, so a
        broken `rapidctl` shim would have shipped green. The assertion is
        deliberately about the LOOP rather than about `rapidctl` specifically —
        pinning the symptom would let the next entry point reintroduce it.
        """
        workflow = (ROOT / ".github/workflows/contract-tests.yml").read_text()
        for name, _module, _function in EXPECTED_ENTRY_POINTS:
            self.assertIn(
                name, workflow,
                f"the contract-tests workflow does not exercise {name}; a "
                f"declared console script that CI never runs can ship broken")

    def test_every_declared_target_is_importable_and_callable(self):
        """A declaration naming a function that does not exist is a broken shim.

        `pip install -e .` writes the console script from the string in
        `pyproject.toml` WITHOUT checking it resolves, so a typo produces a
        command that installs cleanly and fails at first run with an
        ImportError — found by an operator, at the worst moment.
        """
        for name, module_name, function in EXPECTED_ENTRY_POINTS:
            module = importlib.import_module(module_name)
            self.assertTrue(
                callable(getattr(module, function, None)),
                f"{name} declares {module_name}:{function}, which is not a "
                f"callable in that module")


class PublisherFailClosedTests(unittest.TestCase):
    """The publisher refuses to start without what it needs.

    `main()` is called directly with the environment stripped rather than the
    console script being launched: the assertion is about the exit CODE and the
    refusal, and a subprocess would add a process boundary that hides which
    check refused.
    """

    def _main_without(self, monkeypatched_env):
        import os

        from pipeline.publisher import service

        saved = {}
        for key in monkeypatched_env:
            saved[key] = os.environ.pop(key, None)
        try:
            return service.main()
        finally:
            for key, value in saved.items():
                if value is not None:
                    os.environ[key] = value

    def test_it_exits_start_failed_without_configuration(self):
        """No parameter tree, no region, no credentials: EXIT_START_FAILED.

        Every path out of `main()`'s try block lands on the same code, which is
        the point — systemd's `Restart=always` should retry a start failure,
        and an operator reading the journal should see one vocabulary for "this
        service could not start" regardless of which resource was missing.
        """
        from pipeline.runtime import service_kernel

        code = self._main_without(
            ["AWS_REGION", "AWS_DEFAULT_REGION", "RAPID_PUBLISHER_ROLE_ARN",
             "DBSERVER", "DBPORT", "DBNAME", "DBUSER", "DBPASS",
             "RAPID_DB_SECRET_ID"])

        self.assertEqual(code, service_kernel.EXIT_START_FAILED)

    def test_it_does_not_raise_out_of_main(self):
        """A start failure is an EXIT CODE, never an unhandled traceback.

        The distinction matters to the journal: an exit code with a logged
        explanation is a diagnosable start failure, while a traceback escaping
        `main()` is an unhandled crash that reads as a bug in the service
        rather than as a missing deployment step.
        """
        try:
            self._main_without(["AWS_REGION", "AWS_DEFAULT_REGION"])
        except Exception as exc:                            # noqa: BLE001
            self.fail(f"main() raised instead of returning an exit code: "
                      f"{exc!r}")


class TopicGuardTests(unittest.TestCase):
    """The internal-topic guard, now the publisher's (brief E2).

    The guard MOVED from the alert-production job with the send. Its values are
    out of scope (the brief says so), but the predicate's behaviour is not: a
    guard that admitted everything would be indistinguishable from no guard,
    and the mission stream must not be reachable by reconfiguration.
    """

    def test_internal_topics_are_admitted_and_public_ones_are_not(self):
        from pipeline.publisher.service import _topic_guard

        guard = _topic_guard(("rapid.internal.", "rapid.test."))

        self.assertTrue(guard("rapid.internal.alerts.v1"))
        self.assertTrue(guard("rapid.test.alerts"))
        self.assertFalse(guard("roman.alerts.public"))
        self.assertFalse(guard(""))
        self.assertFalse(guard(None))


class _StubCursor:
    """A cursor that answers the preflight probes and records what it was asked.

    It ANSWERS RATHER THAN ACCEPTS: a double that returned a bland truthy value
    for every statement could not tell a preflight that ran from one that did
    not, which is the stub-blind failure this file's whole purpose is to avoid.
    The schema-contract probes get a satisfying answer so the schema half
    passes and the APPLICATION half is what the test is left standing on.
    """

    def __init__(self, recorder):
        self._recorder = recorder
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement, params=None):
        self._recorder.append(statement)
        lowered = " ".join(statement.lower().split())
        if "schema_migrations" in lowered:
            # Every migration this build requires, reported as applied.
            self._rows = [(name,) for name in _all_required_migrations()]
        elif "information_schema.tables" in lowered:
            # The publisher's narrower probe: both its tables are present.
            self._rows = [(2,)]
        elif "to_regclass" in lowered:
            # DRAFT 051's `admission_releases` is ABSENT, which the
            # application check treats as the pre-rollout path and skips —
            # so this stub exercises the environment half without needing a
            # database, exactly as `test_application_contract.py` does.
            self._rows = [(False,)]
        else:
            self._rows = []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass


class _StubConnection:
    """A connection handing out `_StubCursor`s. Records every statement."""

    def __init__(self):
        self.statements = []

    def cursor(self):
        return _StubCursor(self.statements)

    def commit(self):
        pass

    def rollback(self):
        pass


def _all_required_migrations():
    """Every filename `verify_schema_contract` requires, from its own list.

    Read from `REQUIRED_MIGRATIONS` rather than hard-coded, so the stub cannot
    drift out of date and start failing the SCHEMA half — which would make
    these tests pass for the wrong reason, the exact stub-blindness they exist
    to avoid. The entries are `(filename, why)` pairs; the applier records
    filenames, so that is what the stub returns.
    """
    from pipeline.intent.schema_contract import REQUIRED_MIGRATIONS

    return tuple(name for name, _why in REQUIRED_MIGRATIONS)


class ApplicationPreflightTests(unittest.TestCase):
    """ALL FIVE entry points preflight the APPLICATION half (rule 18).

    Rule 18 says "Services and payloads preflight the **application/schema**
    contract at startup" — one contract, two halves. The schema half has been
    checked at four of the five entry points since brief B and at the fifth
    since brief H. The APPLICATION half was wired into `rapidctl` ALONE, by
    package H, which recorded the scope as deliberate and explicitly did NOT
    extend it ("This is NOT extended to the other four").

    That left the four that matter most unchecked: `rapidctl` is an operator
    tool run by a human who can read a traceback, while the other four are the
    deployed service and payload processes whose results get attributed to a
    release. A process that cannot say which release it is cannot have its
    products attributed, cannot be reconciled against an ExecutionBinding, and
    cannot be rolled back from — and it would have started anyway.

    **WHAT MAKES THESE TESTS ABLE TO FAIL.** Each drives the entry point's OWN
    preflight function with an environment that has no release identity, and
    asserts `ApplicationContractUnmet`. Delete the `verify_application_contract`
    call from any one of them and its test fails, because the stub connection
    satisfies the schema half — so nothing else in that function raises.
    """

    def setUp(self):
        import os

        self._saved = {}
        from pipeline.intent.application_contract import (IMAGE_DIGEST_ENV,
                                                          RELEASE_ENV)
        for key in (RELEASE_ENV, IMAGE_DIGEST_ENV):
            self._saved[key] = os.environ.pop(key, None)

    def tearDown(self):
        import os

        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _assert_refuses(self, call):
        from pipeline.intent.application_contract import (
            ApplicationContractUnmet)

        with self.assertRaises(ApplicationContractUnmet):
            call()

    def test_the_reconciler_preflights_the_application_contract(self):
        from pipeline.reconciler.main import _preflight_schema

        self._assert_refuses(lambda: _preflight_schema(_StubConnection()))

    def test_the_publisher_preflights_the_application_contract(self):
        from pipeline.publisher.service import _preflight_schema

        self._assert_refuses(lambda: _preflight_schema(_StubConnection()))

    def test_the_job_payload_preflights_the_application_contract(self):
        """The payload's preflight is inside its `_database` context manager.

        Entered rather than called: the check runs on the connection that
        manager opens, so the contract is only observable by entering it.
        `connection` is patched to yield the stub — the assertion is about the
        preflight, and a real connection would make this a database test for a
        check that reads no table.
        """
        import contextlib

        from database.modules.utils import rapid_db_connect
        from pipeline.entrypoints import job

        @contextlib.contextmanager
        def fake_connection(*args, **kwargs):
            yield _StubConnection()

        real = rapid_db_connect.connection
        rapid_db_connect.connection = fake_connection
        self.addCleanup(setattr, rapid_db_connect, "connection", real)

        class _Route:
            db_lane = "transaction"

        class _JobEnv:
            scheduler_job_id = "job-preflight-test"

        def enter():
            with job._database(_Route(), _JobEnv(), None, None):
                pass

        self._assert_refuses(enter)

    def test_rapidctl_preflights_the_application_contract(self):
        """The entry point package H wired, asserted alongside the other four.

        Included so the property is stated for all five in one place: a later
        change that removed rapidctl's call would otherwise be caught only by
        H's own tests, and the rule is about the set.
        """
        from pipeline.operatorctl.main import _preflight

        self._assert_refuses(lambda: _preflight(_StubConnection()))

    def test_the_operator_preflights_the_application_contract(self):
        """The operator's checks sit behind a session/endpoint signature.

        `connection` is patched as for the payload, and `database_credentials`
        with it — the function resolves a credential before it connects, and
        that resolution is not what is under test.
        """
        import contextlib

        from database.modules.utils import rapid_db_connect
        from pipeline.operator import service
        from pipeline.runtime import service_kernel

        @contextlib.contextmanager
        def fake_connection(*args, **kwargs):
            yield _StubConnection()

        real_conn = rapid_db_connect.connection
        rapid_db_connect.connection = fake_connection
        self.addCleanup(setattr, rapid_db_connect, "connection", real_conn)

        real_creds = service_kernel.database_credentials
        service_kernel.database_credentials = lambda *a, **k: None
        self.addCleanup(setattr, service_kernel, "database_credentials",
                        real_creds)

        self._assert_refuses(
            lambda: service._verify_work_streams(object(), object()))


if __name__ == "__main__":
    unittest.main()
