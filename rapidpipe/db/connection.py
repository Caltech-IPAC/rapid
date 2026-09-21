"""One connection path to the run-model tables: environment only, autocommit off.

Reads the standard ``PG*`` variables -- ``PGHOST``, ``PGPORT``, ``PGDATABASE``,
``PGUSER``, ``PGPASSWORD`` -- exactly as ``database/apply-migrations.sh``
does, and nothing else: no Secrets Manager lookup, no account-specific
configuration, no connection pooling. Those belong to deployment and are
out of scope for this PR; the smdc branch's
``database/modules/utils/rapid_db_connect.py`` is the reference for pooling,
credential fetch and dead-peer detection when that work lands.

This module provides persistence only: it does not import ``rapidpipe.runs``
or any stage module, matching ``rapidpipe.db``'s package contract.
"""

from __future__ import annotations

import contextlib
import os
from typing import Iterator

import psycopg2
import psycopg2.extensions

_REQUIRED_VARS = ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD")


class ConnectionConfigError(RuntimeError):
    """A required ``PG*`` environment variable is missing."""


def _read_env() -> dict[str, str]:
    missing = [name for name in _REQUIRED_VARS if not os.environ.get(name)]
    if missing:
        raise ConnectionConfigError(
            "missing required environment variable(s): "
            f"{', '.join(missing)}; rapidpipe.db.connection reads only "
            f"{', '.join(_REQUIRED_VARS)}")
    return {name: os.environ[name] for name in _REQUIRED_VARS}


@contextlib.contextmanager
def connect() -> Iterator[psycopg2.extensions.connection]:
    """Yield one connection with autocommit off.

    A plain context manager, not a pool: opens one connection from the
    ``PG*`` environment variables, yields it, and closes it on exit. The
    connection's autocommit is left at psycopg2's default (off), so every
    statement runs inside an implicit transaction that the caller must
    commit or roll back -- ``transaction()`` below is the common case of
    "one transaction per call", which every ``rapidpipe.runs.repository``
    function needs (each is documented as a single transaction).

    Does not swallow errors: connection failures and query errors raise;
    nothing here calls ``exit()`` or returns a sentinel in place of
    raising.
    """
    params = _read_env()
    conn = psycopg2.connect(
        host=params["PGHOST"],
        port=params["PGPORT"],
        dbname=params["PGDATABASE"],
        user=params["PGUSER"],
        password=params["PGPASSWORD"],
    )
    try:
        conn.autocommit = False
        yield conn
    finally:
        conn.close()


@contextlib.contextmanager
def transaction() -> Iterator[psycopg2.extensions.connection]:
    """Yield one connection inside one transaction: commit on success, rollback on error.

    Equivalent to ``with connect() as conn:`` followed by an explicit
    commit/rollback, spelled once here so every repository function opens
    with the same one-line pattern:

        with transaction() as conn:
            with conn.cursor() as cur:
                ...

    On an exception the transaction is rolled back and the exception
    re-raised unchanged; on normal exit it is committed.
    """
    with connect() as conn:
        try:
            yield conn
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
