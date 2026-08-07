"""The reconciler's startup wiring: what it passes to `connect`, not what
it exports.

There was no test module for `main.py` while its startup work was writing
`os.environ` — the behaviour was a side effect, and side effects on a
process-global are awkward to assert. O1 turned that work into two
functions that RETURN values, which is what makes it testable, so the
properties that used to live only in comments are pinned here.

Two of them are load-bearing and were both wrong at some point in this
module's life:

- The endpoint's precedence. An explicitly-set variable beats the tree,
  because an operator debugging against a replica sets DBSERVER in the
  unit and restarts; a tree-first order connects them to production while
  they believe otherwise, and says nothing.
- The credential never reaches the environment. The service resolves it
  under its own role — the host's role cannot read the secret (W8,
  AccessDenied, live) — and passes it as a value.
"""

import json
import os
import sys
import types
import unittest
from unittest import mock

# psycopg2 is not installed everywhere this suite runs, and `main` imports
# the connection helper lazily but `Endpoint`/`Credentials` come from the
# same module. A stand-in satisfies the import without the driver; the
# tests below never open a connection.
if "psycopg2" not in sys.modules:  # pragma: no cover - environment-dependent
    try:
        import psycopg2  # noqa: F401
    except ImportError:
        stub = types.ModuleType("psycopg2")
        stub.OperationalError = type("OperationalError", (Exception,), {})
        stub.extensions = types.ModuleType("psycopg2.extensions")
        sql = types.ModuleType("psycopg2.sql")
        sql.SQL = sql.Identifier = lambda *a, **k: None
        stub.sql = sql
        sys.modules["psycopg2"] = stub
        sys.modules["psycopg2.extensions"] = stub.extensions
        sys.modules["psycopg2.sql"] = sql

from pipeline.reconciler import main  # noqa: E402

TREE = {
    "db/server": "tree.internal",
    "db/port": "6432",
    "db/name": "rapidopsdb",
}


class DatabaseEndpointTests(unittest.TestCase):
    def test_the_tree_supplies_the_endpoint_when_nothing_is_set(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            endpoint = main._database_endpoint(dict(TREE))
        self.assertEqual(endpoint.host, "tree.internal")
        self.assertEqual(endpoint.port, "6432")
        self.assertEqual(endpoint.dbname, "rapidopsdb")

    def test_an_explicitly_set_variable_beats_the_tree(self):
        # THE SILENT FAILURE THIS PINS: with the order inverted, an
        # operator who set DBSERVER to a replica and restarted the service
        # would have been connected to the tree's production endpoint,
        # with no log line saying so.
        with mock.patch.dict(os.environ, {"DBSERVER": "replica.internal"},
                             clear=True):
            endpoint = main._database_endpoint(dict(TREE))
        self.assertEqual(endpoint.host, "replica.internal")
        # The fields the operator did NOT override still come from the tree.
        self.assertEqual(endpoint.port, "6432")
        self.assertEqual(endpoint.dbname, "rapidopsdb")

    def test_each_field_is_overridden_independently(self):
        with mock.patch.dict(os.environ, {"DBPORT": "5432"}, clear=True):
            endpoint = main._database_endpoint(dict(TREE))
        self.assertEqual(endpoint.host, "tree.internal")
        self.assertEqual(endpoint.port, "5432")

    def test_a_field_absent_from_both_is_named(self):
        tree = dict(TREE)
        del tree["db/name"]
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(Exception) as caught:
                main._database_endpoint(tree)
        self.assertIn("dbname", str(caught.exception))

    def test_the_endpoint_is_returned_not_exported(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            main._database_endpoint(dict(TREE))
            for name in ("DBSERVER", "DBPORT", "DBNAME"):
                self.assertNotIn(name, os.environ)


class DatabaseCredentialTests(unittest.TestCase):
    def _session(self, secret):
        client = mock.MagicMock()
        client.get_secret_value.return_value = {"SecretString": secret}
        session = mock.MagicMock()
        session.client.return_value = client
        return session

    def test_the_credential_is_returned_and_never_exported(self):
        session = self._session(
            json.dumps({"username": "rapid_orch", "password": "s3cret"}))
        with mock.patch.dict(
                os.environ, {"RAPID_DB_SECRET_ID": "rapid/db/orch"},
                clear=True):
            credentials = main._database_credentials(session)
            # The property the whole change exists to establish.
            self.assertNotIn("DBPASS", os.environ)
            self.assertNotIn("DBUSER", os.environ)
        self.assertEqual(credentials.user, "rapid_orch")
        self.assertEqual(credentials.password, "s3cret")

    def test_the_secret_is_read_under_the_passed_session(self):
        # NOT the ambient chain: the host's instance role cannot read the
        # orchestrator secret (W8, AccessDenied, live). The session handed
        # in is already chained into RAPID_RECONCILER_ROLE_ARN.
        session = self._session(
            json.dumps({"username": "rapid_orch", "password": "s3cret"}))
        with mock.patch.dict(
                os.environ, {"RAPID_DB_SECRET_ID": "rapid/db/orch"},
                clear=True):
            main._database_credentials(session)
        session.client.assert_called_once_with("secretsmanager")

    def test_no_secret_id_leaves_the_credential_to_the_boundary_read(self):
        session = self._session("{}")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(main._database_credentials(session))
        session.client.assert_not_called()

    def test_an_operator_supplied_dbuser_wins(self):
        session = self._session("{}")
        with mock.patch.dict(
                os.environ,
                {"RAPID_DB_SECRET_ID": "rapid/db/orch", "DBUSER": "someone"},
                clear=True):
            self.assertIsNone(main._database_credentials(session))
        session.client.assert_not_called()

    def test_the_password_is_not_in_the_repr(self):
        session = self._session(
            json.dumps({"username": "rapid_orch", "password": "hunter2"}))
        with mock.patch.dict(
                os.environ, {"RAPID_DB_SECRET_ID": "rapid/db/orch"},
                clear=True):
            credentials = main._database_credentials(session)
        self.assertNotIn("hunter2", repr(credentials))


if __name__ == "__main__":
    unittest.main()
