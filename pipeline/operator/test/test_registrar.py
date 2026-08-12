"""`pipeline.operator.registrar`: the production registration callback.

PORTED FROM `pipeline/test/test_vpo_phases.py` (`ProductionRegistrarTests`,
`RegistrarConnectionTests`) when the monolith was retired (IR-2).
`production_registrar` and `registration_callback` moved to this module in
IR-1a / IR-2; these tests now import them directly rather than through
`virtualPipelineOperator`.

Not ported: the parts of `RegistrarConnectionTests` that exercised
`virtualPipelineOperator.registration_callback` bound into the monolith's
own `wait_for_submitted` -> register phase loop. That wiring had no
replacement to port to — `pipeline.operator.operator.Operator._register`
inlines the same factory-or-None conditional directly against its own
`_connection_factory`/`_registrar_factory`, which is exercised by
`pipeline/test/test_operator.py`. What remains live and worth testing here
is the callback itself and the connection-borrowing contract
`production_registrar` builds.
"""

import os
import unittest

from pipeline.operator import registrar as opregistrar


class FakeConnection:
    """A connection double that can ANSWER the DRAFT-048 catalog probe.

    Package R's wiring asks the catalog whether 048's tables exist before it
    builds an identity repository, so a bare `object()` no longer stands in
    for a connection here — it has no `cursor()`. The double answers rather
    than accepts: `present` decides the probe's verdict, so a test can put
    the wiring on either branch deliberately and neither branch is the
    accident of an unconfigured stub.

    The connection IDENTITY is what these tests are about, so instances are
    distinguishable by identity exactly as the `object()`s they replace were.
    """

    def __init__(self, present=True):
        self.present = present
        self.statements = []

    def cursor(self):
        return self._Cursor(self)

    class _Cursor:
        def __init__(self, conn):
            self._conn = conn

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, statement, params=None):
            self._conn.statements.append(statement)

        def fetchone(self):
            return (self._conn.present,)

        def close(self):
            pass


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

        self.assertIsNone(opregistrar.production_registrar())

    def test_the_default_is_not_a_dry_run(self):
        # Without the flag the registrar is real — which here means it
        # demands the records bucket rather than quietly returning None. A
        # None return is exactly the dry run this finding is about, so
        # "it returned None" is the one outcome that must not happen by
        # default.
        os.environ["RAPID_RECORDS_BUCKET"] = "roman-rapid-records"

        try:
            callback = opregistrar.production_registrar()
        except SystemExit:  # pragma: no cover - would be a different defect
            self.fail("a configured registrar must not exit")
        except ImportError:
            # boto3/psycopg2 absent off-image: the point is only that this
            # path does NOT return None.
            return

        self.assertIsNotNone(callback)

    def test_a_caller_supplied_s3_client_is_used_not_ambient(self):
        # The operator service passes its assumed-session client: records-
        # bucket read is an orchestrator-role grant, and an ambient client
        # reads as the instance role — AccessDenied per-attempt, deep
        # inside a pass (seen live: attempt 118, every pass). With a
        # client supplied, the factory must not touch boto3 at all, which
        # is also what lets this test run off-image.
        os.environ["RAPID_RECORDS_BUCKET"] = "roman-rapid-records"

        class _StubClient:
            pass

        factory = opregistrar.production_registrar(s3_client=_StubClient())

        self.assertIsNotNone(factory)

    def test_a_missing_records_bucket_is_refused_not_defaulted(self):
        # The registrar reads each attempt's terminal record from this
        # bucket. Guessing a name would fail per-attempt deep inside a pass;
        # refusing here is one message before any work is done.
        with self.assertRaises(SystemExit) as caught:
            opregistrar.production_registrar()

        self.assertEqual(64, caught.exception.code)


class RegistrationCallbackTests(unittest.TestCase):
    """`registration_callback`: the factory-or-None seam.

    A named seam rather than an inline conditional at each call site: the
    dry-run factory is None and must stay None (what makes `run_registration`
    pass `dry_run=True`), while a production factory has to be bound to the
    pass's own connection.
    """

    def test_a_none_factory_stays_none(self):
        self.assertIsNone(opregistrar.registration_callback(None, object()))

    def test_a_factory_is_bound_to_the_given_connection(self):
        conn = object()
        bound = []

        def factory(c):
            bound.append(c)
            return "callback-for-" + repr(c)

        result = opregistrar.registration_callback(factory, conn)

        self.assertEqual([conn], bound)
        self.assertEqual("callback-for-" + repr(conn), result)


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

        # `fallback_roles` is captured, not just absorbed by **kwargs: the
        # role-binding commit added it to `products.registrar`, and a double
        # that swallowed it silently would let the pre-binding replay path
        # regress without a test noticing. Tracking the real signature is
        # what makes this fake able to refuse a wrong call.
        #
        # `identity_repository` JOINS IT FOR THE SAME REASON, and the fake
        # earned its keep here: package R wired it into this factory, and this
        # double — modelling the real signature rather than **kwargs — refused
        # the new call with a TypeError the moment it appeared. That is the
        # double working. What R1 asserts about the value passed lives in
        # `pipeline/contract/test_live_registrar_identity.py`, on a real
        # connection; this file's subject is still the connection binding.
        def fake_registrar(dbh, store, fallback_roles=None,
                           identity_repository=None):
            captured["dbh"] = dbh
            captured["store"] = store
            captured["fallback_roles"] = fallback_roles
            captured["identity_repository"] = identity_repository
            return lambda *a, **k: None

        import pipeline.registration.products as products_mod
        real_registrar = products_mod.registrar
        products_mod.registrar = fake_registrar
        self.addCleanup(setattr, products_mod, "registrar", real_registrar)

        factory = opregistrar.production_registrar()
        self.assertIsNotNone(factory)

        pass_connection = FakeConnection()
        callback = opregistrar.registration_callback(factory, pass_connection)
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

        # An ordinary production pass is NOT the pre-binding replay, so the
        # fallback roles must be absent: passing the running release's
        # bindings here would let a record authored before bindings existed
        # register against them silently, which is the one case
        # `role_product` is supposed to refuse.
        self.assertIsNone(captured["fallback_roles"])

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
            lambda dbh, store, fallback_roles=None, identity_repository=None:
                captured.append(dbh) or (lambda *a, **k: None))
        self.addCleanup(setattr, products_mod, "registrar", real_registrar)

        factory = opregistrar.production_registrar()

        connections = [FakeConnection(), FakeConnection(), FakeConnection()]
        for conn in connections:
            opregistrar.registration_callback(factory, conn)

        for dbh in captured:
            dbh()

        self.assertEqual(connections, borrowed)

    def test_a_dry_run_factory_stays_none_through_the_seam(self):
        """`run_registration` passes `dry_run=register is None`, so binding a
        dry run to a connection must not manufacture a callback and turn a
        rehearsal into a production write."""
        os.environ["RAPID_VPO_DRY_RUN"] = "true"

        factory = opregistrar.production_registrar()

        self.assertIsNone(factory)
        self.assertIsNone(opregistrar.registration_callback(factory, object()))


if __name__ == "__main__":
    unittest.main()
