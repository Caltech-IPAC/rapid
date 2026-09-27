"""The stage-side read guard: may this run read the instances its input manifest names?

Supervisor step 6, 2026-09-26, rulings R5 and R6 and amendments A5 and A6
(operations page, "Dependency eligibility", the read column). Every stage
invocation enters through ``rapidpipe.stages.contract.run_stage``: a
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
readable, with one exception (A5): a file-product entry whose id is
unregistered but whose members match a registered instance's members
(same path, against ``product_members.path`` or
``product_instances.primary_location``, and same SHA-256) is judged as
that instance, so a fresh id over another run's scratch files is refused.
Requiring the SHA-256 as well as the path keeps two runs that merely
share a relative layout (``l2/<name>.fits``) from matching each other;
when the bytes match several instances the entry is readable if any of
them is.

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
import json
import logging
import os
from typing import Any, Callable

from rapidpipe.db import connection as _connection_module
from rapidpipe.exitcodes import ExitCode
from rapidpipe.products.manifest import Manifest, OutputEntry
from rapidpipe.runs.inputs import manifest_instances

logger = logging.getLogger(__name__)

#: The custody states in which another run's instance may be read: a
#: production run's output (R5).
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
#: context manager in place of the database, as ``RAPIDPIPE_LOAD_DATABASE``
#: does for the load stage. Only the selftest sets it
#: (``rapidpipe.selftest.support.fakereadguarddb``, an empty registry).
DATABASE_ENV = "RAPIDPIPE_READGUARD_DATABASE"


def connect(*args, **kwargs):
    """Module-level indirection to ``rapidpipe.db.connection.connect``, for tests.

    When ``RAPIDPIPE_READGUARD_DATABASE`` names a factory, that factory's
    connection is used instead; a name that is not a factory is a
    configuration error (exit 64).
    """
    override = os.environ.get(DATABASE_ENV)
    if override:
        module_name, _, factory_name = override.partition(":")
        try:
            factory = getattr(importlib.import_module(module_name), factory_name)
        except (ImportError, AttributeError, ValueError) as exc:
            raise ReadGuardNotConfigured(
                f"{DATABASE_ENV}={override!r} does not name a factory: {exc}") from exc
        return factory()
    return _connection_module.connect(*args, **kwargs)


# ----------------------------------------------------------------------
# The read rule. A private copy of rapidpipe.db.objects.assert_readable_instance
# (step 6 WP-A, R5; origin/ops6-eligibility f544ea10), semantically identical
# (same refusals, reasons and wording), until that function is on this
# branch's base. Switching is this one line, replacing the block below:
#   from rapidpipe.db.objects import assert_readable_instance
# ----------------------------------------------------------------------

_READABLE_SQL = """
    SELECT pi.kind, pi.run, pi.custody, pi.deletion_state, rs.complete, rs.row_count,
           pi.logical_key::text,
           COALESCE(u.selected_attempt = pi.producing_attempt, false)
    FROM product_instances pi
    LEFT JOIN result_sets rs ON rs.instance = pi.id
    LEFT JOIN attempts a ON a.id = pi.producing_attempt
    LEFT JOIN units u ON u.id = a.unit
    WHERE pi.id = %s
"""


class Unreadable(ValueError):
    """A read-rule refusal (:func:`assert_readable_instance`); ``reason`` is
    one of ``unknown``, ``kind``, ``deleted``, ``incomplete``, ``scratch``,
    ``unselected``."""

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def assert_readable_instance(
    cur, instance: str, run_id: str, *, kind: str | None = None,
) -> dict[str, Any]:
    """Refuse (:class:`Unreadable`, a ValueError) a product instance ``run_id``
    may not read; else describe it.

    The read column of the dependency-eligibility table (supervisor step 6,
    2026-09-26, R5), for any ``product_instances`` row, file product or
    result set: retained, complete when a result set, and, when it belongs
    to another run, custody ``candidate`` or ``current`` and produced by its
    unit's selected attempt (a unit with no selected attempt counts as
    unselected). An instance of ``run_id`` itself is readable whatever its
    custody or attempt. With ``kind``, an instance of any other kind is
    refused. An id naming no instance is refused (``unknown``); the guard
    below never hands it one.

    Returns ``{kind, run, custody, row_count, key, result_set}``.
    """
    cur.execute(_READABLE_SQL, (instance,))
    row = cur.fetchone()
    if row is None:
        raise Unreadable(f"no product instance {instance!r}", "unknown")
    (found_kind, owner, custody, deletion_state, complete, row_count, key_text,
     selected) = row
    if kind is not None and found_kind != kind:
        raise Unreadable(f"{instance!r} is a {found_kind}, not a {kind}", "kind")
    # ``complete`` is NULL exactly when the instance has no result_sets row:
    # the instance is a file product.
    is_result_set = complete is not None
    if is_result_set:
        if not complete or deletion_state != "retained":
            raise Unreadable(
                f"{found_kind} {instance!r} is not complete and retained "
                f"(complete={complete}, deletion_state={deletion_state})",
                "deleted" if deletion_state != "retained" else "incomplete")
    elif deletion_state != "retained":
        raise Unreadable(
            f"{found_kind} {instance!r} is not retained (deletion_state={deletion_state})",
            "deleted")
    if owner != run_id:
        what = "result set" if is_result_set else "product"
        if custody not in FOREIGN_READABLE_CUSTODY:
            raise Unreadable(
                f"{found_kind} {instance!r} belongs to run {owner!r} with custody {custody!r}: "
                f"another run's scratch {what} is not readable by run {run_id!r}", "scratch")
        if not selected:
            raise Unreadable(
                f"{found_kind} {instance!r} of run {owner!r} was produced by an attempt that "
                f"is not its unit's selected attempt (or its unit has none): not readable "
                f"by run {run_id!r}", "unselected")
    key = json.loads(key_text) if isinstance(key_text, str) else (key_text or {})
    return {"kind": found_kind, "run": owner, "custody": custody, "row_count": row_count,
            "key": key if isinstance(key, dict) else {}, "result_set": is_result_set}


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


def _member_matches(cur, entry: OutputEntry) -> list[str]:
    """Registered instances whose members carry one of ``entry``'s files (A5)."""
    matches: list[str] = []
    for member in entry.members:
        cur.execute(_MEMBER_MATCH_SQL, (member.sha256, member.path, member.path))
        for (instance,) in cur.fetchall():
            if instance not in matches:
                matches.append(instance)
    return matches


def _refusal(name: str, instance: str, found: dict[str, Any] | None, run_id: str,
             reason: str) -> InputNotReadable:
    via = "" if name == instance else f" (its members are those of registered instance {instance})"
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
        matches = _member_matches(cur, entry)
        refusals = []
        for match in matches:
            match_found, match_reason = _judge(cur, match, run_id)
            if match_reason is None:
                refusals = []
                break
            refusals.append((match, match_found, match_reason))
        if refusals:
            match, match_found, match_reason = refusals[0]
            raise _refusal(name, match, match_found, run_id, match_reason)


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
    "assert_inputs_readable",
    "assert_readable_instance",
    "connect",
]
