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


class Submission:
    """Just the attribute `wait_for_submitted` reads off a real submission."""

    def __init__(self, run_id):
        self.run_id = run_id


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

    def test_a_submission_with_no_run_id_does_not_silently_pass(self):
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


if __name__ == "__main__":
    unittest.main()
