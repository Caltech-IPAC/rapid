"""An empty product registry for the stage read guard in a selftest.

``rapidpipe selftest`` and ``make stage-<name>`` run a stage as a
subprocess on a packaged fixture with no database: every other database
the stage touches is a fake named by its own ``RAPIDPIPE_<STAGE>_DATABASE``
variable. The read guard inside ``run_stage``
(``rapidpipe.runs.readguard``; supervisor step 6, 2026-09-26, R6) needs a
database whenever the input manifest names an instance, and a fixture's
input manifest names instance ids minted for the fixture, which no run
registered. :func:`empty_registry` is the factory
``RAPIDPIPE_READGUARD_DATABASE`` names for those runs: a connection whose
every query finds no row, which is exactly what a real database answers
for those ids (unregistered, so readable). It is never a way to admit a
registered instance: it knows none. The guard honours it only when
``RAPIDPIPE_SELFTEST=1`` marks the run as a selftest (set by
``rapidpipe.selftest.runner`` alone) and logs a WARNING each time;
anywhere else the variable is a configuration error (exit 64).

Packaged under ``rapidpipe.selftest.support`` so the pipeline image can
import it (``tests/`` is excluded at build time).
"""

from __future__ import annotations

import contextlib
from typing import Any, Iterator

#: The factory ``rapidpipe.runs.readguard.DATABASE_ENV`` names for a selftest.
FACTORY = "rapidpipe.selftest.support.fakereadguarddb:empty_registry"


class _EmptyCursor:
    def __enter__(self) -> "_EmptyCursor":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        return None

    def fetchone(self) -> None:
        return None

    def fetchall(self) -> list:
        return []


class _EmptyConnection:
    def cursor(self) -> _EmptyCursor:
        return _EmptyCursor()

    def rollback(self) -> None:
        return None


@contextlib.contextmanager
def empty_registry() -> Iterator[_EmptyConnection]:
    yield _EmptyConnection()
