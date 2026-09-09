"""Contract-tier check: real psycopg2 actually adapts numpy scalars after
importing ``rapid_db``.

Marked ``contract`` (not because it needs a database -- it needs nothing but
the real ``psycopg2`` package, already present in this venv as
psycopg2-binary) so the default `-m 'not contract and not live'` selection
deselects it: importing `rapid_db` in the same process as
``test_rapid_db_numpy_adapters.py`` would register adapters against the
REAL `psycopg2.extensions` (that file only runs isolated, with a stub
`psycopg2` installed first), and this file's whole point is to exercise the
real driver, so it must never share a process with the stub test. Run
explicitly: `pytest -m contract database/modules/utils/test/test_rapid_db_numpy_adapters_contract.py`.

Before the fix in `rapid_db.py`, the `numpy.int64` assertion below raised
`psycopg2.ProgrammingError: can't adapt type 'numpy.int64'` -- reproduced by
hand:

    >>> import psycopg2, numpy
    >>> psycopg2.extensions.adapt(numpy.int64(7)).getquoted()
    Traceback (most recent call last):
        ...
    psycopg2.ProgrammingError: can't adapt type 'numpy.int64'

This is the exact failure `database/sims/db_register_socsim_files.py` hit
7,384 times registering g0005 (`register_l2filemeta`'s `hp6`/`hp9`, from
`healpy.ang2pix`, are `numpy.int64`).
"""

import numpy
import psycopg2
import pytest

from database.modules.utils import rapid_db  # noqa: F401  (import registers the adapters)

pytestmark = pytest.mark.contract


def test_int64_adapts_as_plain_integer():
    assert psycopg2.extensions.adapt(numpy.int64(7)).getquoted() == b"7"


def test_float32_adapts_as_plain_float():
    assert psycopg2.extensions.adapt(numpy.float32(1.5)).getquoted() == b"1.5"


def test_bool_adapts_as_plain_bool():
    assert psycopg2.extensions.adapt(numpy.bool_(True)).getquoted() == b"true"
