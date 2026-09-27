"""The stage-side read guard: may this run read the instances its input manifest names?

This implements the operations page's "Dependency eligibility" read
column (operations.md). Every stage invocation enters through
``rapidpipe.stages.contract.run_stage``: a
direct ``rapidpipe stage run <name> ...``, the local launcher's
``python -m rapidpipe.stages.<name> ...`` and the command a Batch job
runs (``rapidpipe stage <name> ...``). ``run_stage`` calls
:func:`assert_inputs_readable` once the input manifest (and only the
manifest) is fetched and parsed, before any member is fetched and before
``validate_inputs``, the ``--dry-run`` return or the stage body. So the
guard cannot be bypassed by choosing a path; the launcher-side binding
fence (``rapidpipe.runs.inputs``) stays as an earlier, cheaper refusal.

The read rule, for every ``product_instances`` row, file product and
result set alike: the instance is retained; a result set is complete;
and, when it belongs to another run, its custody is ``candidate`` or
``current`` and it was produced by its unit's selected attempt (a unit
with no selected attempt refuses it as unselected). An instance id that
names no registered instance is not a product of any run and is
readable, with one exception: a file-product entry whose id is
unregistered but whose members match a registered instance's members
(same path, against ``product_members.path`` or
``product_instances.primary_location``, and same SHA-256) is judged as
that instance, so a fresh id over another run's scratch files is refused.
Requiring the SHA-256 as well as the path keeps two runs that merely
share a relative layout (``l2/<name>.fits``) from matching each other.
Each member is judged on its own: a member whose bytes
match several instances is readable if any of them is, and one member
that matches only unreadable instances refuses the entry, whatever its
other members match.

This module sits in ``rapidpipe.runs`` and may not import
``rapidpipe.stages`` (the fixed dependency direction,
``tests/unit/test_dependency_direction.py``), so it raises its own three
exceptions, each carrying the exit code the stage contract gives it, and
``run_stage`` maps them onto its own family: :class:`InputNotReadable`
to ``InputRejected`` (65), :class:`ReadGuardUnavailable` to
``TransientFailure`` (75) and :class:`ReadGuardNotConfigured` to
``UsageError`` (64).
"""

from __future__ import annotations

import importlib
import logging
import os
from typing import Any, Callable

from rapidpipe.db import connection as _connection_module
from rapidpipe.db.objects import Unreadable, assert_readable_instance
from rapidpipe.exitcodes import ExitCode
from rapidpipe.products.manifest import Manifest, OutputEntry
from rapidpipe.runs.inputs import manifest_instances

logger = logging.getLogger(__name__)

#: The custody states in which another run's instance may be read: a
#: production run's output (runs.md §Rules).
FOREIGN_READABLE_CUSTODY: tuple[str, ...] = ("candidate", "current")


class ReadGuardError(Exception):
    """Base class of the guard's three outcomes other than "readable"."""

    exit_code: ExitCode


class InputNotReadable(ReadGuardError, ValueError):
    """A named instance may not be read by this run. Exit 65."""

    exit_code = ExitCode.INPUT_REJECTED


class ReadGuardUnavailable(ReadGuardError):
    """The database could not be reached, or the connection was lost. Exit 75."""

    exit_code = ExitCode.TRANSIENT_FAILURE


class ReadGuardNotConfigured(ReadGuardError):
    """No database is configured for this process. Exit 64 (configuration)."""

    exit_code = ExitCode.USAGE


#: Names a ``module:factory`` whose call returns the guard's connection
#: context manager in place of the database, honoured only for a selftest
#: fixture run: the factory's module must be under :data:`SELFTEST_SUPPORT`
#: and :data:`SELFTEST_ENV` must be ``1``, which only
#: ``rapidpipe.selftest.runner`` sets on its subprocesses.
#: Anything else is a configuration error (exit 64), never a fallback.
DATABASE_ENV = "RAPIDPIPE_READGUARD_DATABASE"

#: Set to ``1`` by ``rapidpipe.selftest.runner`` on a fixture subprocess.
SELFTEST_ENV = "RAPIDPIPE_SELFTEST"

#: The package a selftest registry factory must live in.
SELFTEST_SUPPORT = "rapidpipe.selftest.support."

#: The line every use of a selftest registry logs.
SELFTEST_WARNING = "read guard: selftest registry in use, no product custody enforced"


def _selftest_registry(override: str):
    module_name, _, factory_name = override.partition(":")
    if os.environ.get(SELFTEST_ENV) != "1":
        raise ReadGuardNotConfigured(
            f"{DATABASE_ENV} is set, but this is not a selftest fixture run "
            f"({SELFTEST_ENV} is not 1); it is honoured only there")
    if not module_name.startswith(SELFTEST_SUPPORT) or not factory_name:
        raise ReadGuardNotConfigured(
            f"{DATABASE_ENV}={override!r} does not name a factory under "
            f"{SELFTEST_SUPPORT.rstrip('.')}")
    try:
        factory = getattr(importlib.import_module(module_name), factory_name)
    except (ImportError, AttributeError, ValueError) as exc:
        raise ReadGuardNotConfigured(
            f"{DATABASE_ENV}={override!r} does not name a factory: {exc}") from exc
    logger.warning(SELFTEST_WARNING)
    return factory()


def connect(*args, **kwargs):
    """Module-level indirection to ``rapidpipe.db.connection.connect``, for tests.

    ``RAPIDPIPE_READGUARD_DATABASE`` replaces the database only in a
    selftest fixture run (see :data:`DATABASE_ENV`); set anywhere else, or
    naming anything else, it is a configuration error (exit 64).
    """
    override = os.environ.get(DATABASE_ENV)
    if override is not None:
        return _selftest_registry(override)
    return _connection_module.connect(*args, **kwargs)


# ----------------------------------------------------------------------
# The read rule: rapidpipe.db.objects.assert_readable_instance (imported
# above).
# ----------------------------------------------------------------------

_INSTANCE_SQL = """
    SELECT pi.kind, pi.run, pi.custody, pi.deletion_state,
           rs.instance IS NOT NULL, COALESCE(rs.complete, false),
           COALESCE(u.selected_attempt = pi.producing_attempt, false)
    FROM product_instances pi
    LEFT JOIN result_sets rs ON rs.instance = pi.id
    LEFT JOIN attempts a ON a.id = pi.producing_attempt
    LEFT JOIN units u ON u.id = a.unit
    WHERE pi.id = %s
"""


def _describe(cur, instance: str) -> dict[str, Any] | None:
    """The fields the refusal message needs, or ``None`` when unregistered."""
    cur.execute(_INSTANCE_SQL, (instance,))
    row = cur.fetchone()
    if row is None:
        return None
    kind, owner, custody, deletion_state, is_result_set, complete, selected = row
    return {"kind": kind, "run": owner, "custody": custody,
            "deletion_state": deletion_state, "result_set": bool(is_result_set),
            "complete": bool(complete), "selected": bool(selected)}


# ----------------------------------------------------------------------
# The guard
# ----------------------------------------------------------------------

_MEMBER_MATCH_SQL = """
    SELECT DISTINCT pi.id
    FROM product_members pm
    JOIN product_instances pi ON pi.id = pm.instance
    WHERE pm.sha256 = %s AND (pm.path = %s OR pi.primary_location = %s)
    ORDER BY pi.id
"""


def _member_matches(cur, member) -> list[str]:
    """Registered instances one of whose members is ``member``'s file (A5):
    the same SHA-256 at the same path, as a member path or a primary location."""
    cur.execute(_MEMBER_MATCH_SQL, (member.sha256, member.path, member.path))
    return [instance for (instance,) in cur.fetchall()]


def _refusal(name: str, instance: str, found: dict[str, Any] | None, run_id: str,
             reason: str, *, member: str | None = None) -> InputNotReadable:
    via = ("" if name == instance else
           f" (its member {member} is a file of registered instance {instance})")
    if found is None:
        return InputNotReadable(
            f"input {name}{via} is not readable by run {run_id}: {reason}")
    return InputNotReadable(
        f"input {name}{via} is not readable by run {run_id}: kind {found['kind']}, "
        f"custody {found['custody']}, owning run {found['run']}: {reason}")


def _judge(cur, instance: str, run_id: str) -> tuple[dict[str, Any] | None, str | None]:
    """``(description, reason)``: ``reason`` is ``None`` when readable.

    An unregistered id is decided here (readable, then matched by its
    members) and never handed to the rule, so the rule may refuse or
    accept an unknown id without changing the guard's answer.
    """
    found = _describe(cur, instance)
    if found is None:
        return None, None
    try:
        assert_readable_instance(cur, instance, run_id)
    except ValueError as exc:
        return found, str(exc)
    return found, None


def _check_all(cur, manifest: Manifest, names: list[str], run_id: str) -> None:
    entries = {entry.instance: entry for entry in manifest.outputs}
    for name in names:
        found, reason = _judge(cur, name, run_id)
        if reason is not None:
            raise _refusal(name, name, found, run_id, reason)
        if found is not None:
            continue
        entry = entries.get(name)
        if entry is None or not entry.members:
            continue  # an unregistered result-set id reads nothing: readable
        # Authorised per member: every member whose bytes
        # are a registered product's must be those of a readable one. A
        # readable match for one member never authorises another member.
        for member in entry.members:
            refusals = []
            for match in _member_matches(cur, member):
                match_found, match_reason = _judge(cur, match, run_id)
                if match_reason is None:
                    refusals = []
                    break
                refusals.append((match, match_found, match_reason))
            if refusals:
                match, match_found, match_reason = refusals[0]
                raise _refusal(name, match, match_found, run_id, match_reason,
                               member=member.path)


def assert_inputs_readable(
    manifest: Manifest, run_id: str, *, connect: Callable[[], Any] | None = None,
) -> None:
    """Refuse the stage invocation when ``run_id`` may not read an input ``manifest`` names.

    Takes :func:`rapidpipe.runs.inputs.manifest_instances` (output entries'
    instances and ``inputs.result_sets``). With none named, returns
    without connecting. Otherwise opens one connection (``connect``, else
    this module's :func:`connect`), applies the read rule to every named
    instance in one read-only transaction that is always rolled back, and
    raises :class:`InputNotReadable` naming the first refused instance,
    its kind, custody, owning run and the reason. A missing database
    configuration raises :class:`ReadGuardNotConfigured`; a database that
    cannot be reached, or a connection lost mid-check, raises
    :class:`ReadGuardUnavailable`.
    """
    names = manifest_instances(manifest)
    if not names:
        return
    opener = connect if connect is not None else globals()["connect"]
    try:
        with opener() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("SET TRANSACTION READ ONLY")
                    _check_all(cur, manifest, names, run_id)
            finally:
                conn.rollback()
    except _connection_module.ConnectionConfigError as exc:
        raise ReadGuardNotConfigured(
            f"the input read guard needs the database and none is configured: {exc}") from exc
    except _connection_module.ConnectionUnavailable as exc:
        raise ReadGuardUnavailable(f"could not connect to the database: {exc}") from exc
    except _connection_module.psycopg2.OperationalError as exc:
        raise ReadGuardUnavailable(
            f"database connection lost during the input read guard: {exc}") from exc
    logger.info("read guard: run=%s may read the %d instance(s) its inputs name",
                run_id, len(names))


__all__ = [
    "FOREIGN_READABLE_CUSTODY",
    "InputNotReadable",
    "ReadGuardError",
    "ReadGuardNotConfigured",
    "ReadGuardUnavailable",
    "Unreadable",
    "DATABASE_ENV",
    "SELFTEST_ENV",
    "assert_inputs_readable",
    "assert_readable_instance",
    "connect",
]
