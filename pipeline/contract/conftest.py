"""pytest wiring for the contract tier.

Everything here is thin on purpose: the fixture logic lives in
`pipeline.contract.fixture`, importable without pytest, so the same helpers
serve a pytest run in CI and any other runner on rapid-admin.

**AUTO-MARKING.** Every test collected under this directory is marked
`contract`, so no test author can forget the marker and accidentally add a
database-requiring test to the default (`-m 'not contract and not live'`)
selection. A tier whose membership depends on remembering to say so is a tier
that leaks.
"""

import pytest

from pipeline.contract import fixture


def pytest_collection_modifyitems(config, items):
    """Mark everything in this package `contract`."""
    for item in items:
        item.add_marker(pytest.mark.contract)


@pytest.fixture(scope="session")
def target():
    """The resolved libpq target, so a failure names WHERE it tried to connect.

    A connection failure whose message does not say which host was tried is
    the single most common way a location-parameterized suite wastes an
    operator's afternoon.
    """
    return fixture.connection_target()


@pytest.fixture(scope="session")
def _session_conn(target):
    """One connection per session, used to prepare shared fixture rows."""
    conn = fixture.connect()
    conn.autocommit = False
    fixture.ensure_definition(conn)
    yield conn
    conn.close()


@pytest.fixture
def conn(_session_conn):
    """A connection for one test, rolled back if the test left work open.

    NOT a transaction-per-test that rolls everything back: several of these
    tests need their writes VISIBLE to a second connection (the claim race,
    the watermark race), which a wrapping transaction would hide. Fixture
    honesty — unique run tags, no truncation — is what keeps the tier
    re-runnable instead, exactly as brief A's suite established.
    """
    yield _session_conn
    try:
        _session_conn.rollback()
    except Exception:  # noqa: BLE001 - teardown must not mask a test failure
        pass


@pytest.fixture
def second_conn():
    """An independent connection, for the genuinely-concurrent tests.

    Separate from `conn` and closed at the end of the test: two connections
    from one pool that turned out to be the same session would make every
    concurrency test pass vacuously.
    """
    other = fixture.connect()
    other.autocommit = False
    yield other
    other.close()
