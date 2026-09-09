"""Regression test for numpy-scalar psycopg2 adapters in ``rapid_db.py``.

**Why this exists.** ``database/sims/db_register_socsim_files.py`` admitted
~9,911 objects and then failed every single per-file registration with
``can't adapt type 'numpy.int64'`` -- ``register_l2filemeta``'s ``hp6``/
``hp9`` come from ``healpy.ang2pix`` and are numpy scalars, and psycopg2 has
no adapter for them by default (unlike ``numpy.float64``, which happens to
subclass the builtin ``float`` and adapts by accident). The fix registers
adapters for the numpy scalar types at import of ``rapid_db``.

**Why this is a stub-tier test.** It only needs to confirm the module
imports cleanly and the registration function is idempotent under the stub
psycopg2 installed here (which has a bare ``psycopg2.extensions`` with no
``register_adapter``/``AsIs`` -- the guard this test exercises). Whether the
adapters actually make psycopg2 accept a numpy scalar is asserted against
the REAL driver in ``test_rapid_db_numpy_adapters_contract.py``, marked
``contract`` so it does not run here. Same stub-installation pattern and
reasoning as ``test_rapid_db_borrowing.py``.
"""

import sys
import types
import unittest


def _stub(name):
    module = types.ModuleType(name)
    sys.modules[name] = module
    return module


def _install_third_party_stubs():
    if "psycopg2" not in sys.modules:
        psycopg2 = _stub("psycopg2")
        # A PACKAGE, not a module: `rapid_db` does `import psycopg2.sql as sql`,
        # which fails with "not a package" against a bare module stub.
        psycopg2.__path__ = []
        psycopg2.connect = lambda *_a, **_k: None
        psycopg2.DatabaseError = Exception
        psycopg2.OperationalError = Exception
        psycopg2.InterfaceError = Exception
        # Deliberately bare: no register_adapter, no AsIs. This is the
        # shape of psycopg2.extensions the real stub tier installs
        # elsewhere in the suite, and it is exactly the shape that must
        # not raise on import.
        extensions = _stub("psycopg2.extensions")
        extensions.ISOLATION_LEVEL_AUTOCOMMIT = 0
        sql = _stub("psycopg2.sql")
        sql.Identifier = lambda *a: None
        sql.SQL = lambda *a: None
        psycopg2.extensions = extensions
        psycopg2.sql = sql


_install_third_party_stubs()

from database.modules.utils import rapid_db


class NumpyAdapterRegistrationTests(unittest.TestCase):
    def test_module_imports_under_bare_extensions_stub(self):
        # Import already happened above; reaching here at all is the
        # assertion that `_register_numpy_adapters()` did not raise against
        # a `psycopg2.extensions` with no `register_adapter`/`AsIs`.
        self.assertTrue(hasattr(rapid_db, "_register_numpy_adapters"))

    def test_registration_is_idempotent(self):
        # Calling it again (e.g. a second import in another test process,
        # or a caller re-invoking it) must not raise.
        rapid_db._register_numpy_adapters()
        rapid_db._register_numpy_adapters()


if __name__ == "__main__":
    unittest.main()
