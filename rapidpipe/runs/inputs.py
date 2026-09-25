"""Bind a unit's inputs from its input-set manifest before an attempt is
allocated (supervisor step 9, 2026-09-25, R4).

The runs page ("Units"): "Inputs are bound in unit_inputs before
execution and retained for retries." Both launch paths --
``rapidpipe.launch.batch.submit_unit`` and
``rapidpipe.runs.local.run_stage_locally`` -- read the manifest at
``--inputs`` (a local directory or an ``s3://`` prefix holding
``manifest.json``), collect every instance it names (each output entry's
``instance`` and every ``inputs.result_sets`` id), and bind those that
are registered product instances through
:func:`~rapidpipe.runs.repository.bind_unit_inputs`. Names that are not
registered (a delivery manifest's, a dev-era template's) bind nothing and
are logged. The deletion guard
(:func:`~rapidpipe.runs.repository.mark_run_deleting`) then sees the
consumer from submission, not only once its outputs register their
dependency edges.

A manifest that cannot be read refuses the submission with
:class:`InputsRefused` (the CLI exits 65, the stage contract's
``INPUT_REJECTED``): the guard's basis is the binding, so no attempt is
allocated without one. A network-shaped storage failure (connection,
timeout, throttling) propagates unchanged: the CLI maps it to 75.

This module imports ``rapidpipe.products`` and
``rapidpipe.runs.repository`` only, never a stage module (the package's
fixed dependency direction).
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any, Iterable

from rapidpipe.products.manifest import Manifest, ManifestError
from rapidpipe.products.storage import LocationError, fetch_object, join, parse_location
from rapidpipe.runs.repository import bind_unit_inputs

logger = logging.getLogger(__name__)

#: Class-name suffixes of network-shaped storage failures, matched by
#: name (not ``isinstance``) so a test stand-in and the real botocore
#: class are treated alike. Mirrors ``rapidpipe.stages.contract``'s list;
#: duplicated because ``rapidpipe.runs`` never imports a stage module.
_TRANSIENT_EXCEPTION_NAMES = (
    "EndpointConnectionError",
    "ConnectionError",
    "ConnectTimeoutError",
    "ReadTimeoutError",
    "ThrottlingException",
    "RequestTimeout",
    "RequestTimeoutException",
)

#: The stage contract's INPUT_REJECTED exit code, which the CLI returns
#: for :class:`InputsRefused`.
INPUTS_REFUSED_EXIT = 65


class InputsRefused(Exception):
    """The input-set manifest at ``--inputs`` is absent or unreadable, so
    the unit's inputs cannot be bound and nothing is submitted. The CLI
    exits 65 (:data:`INPUTS_REFUSED_EXIT`)."""

    exit_code = INPUTS_REFUSED_EXIT


def _is_transient(exc: BaseException) -> bool:
    name = type(exc).__name__
    return any(name.endswith(suffix) for suffix in _TRANSIENT_EXCEPTION_NAMES)


def read_input_manifest(inputs_location: str, *, s3_client: Any = None) -> Manifest:
    """Read and validate ``<inputs_location>/manifest.json``.

    Raises :class:`InputsRefused` when it is absent, not valid JSON, fails
    validation, or cannot be fetched for a non-network reason (e.g.
    AccessDenied). A network-shaped failure propagates unchanged.
    """
    shown = f"{inputs_location.rstrip('/')}/manifest.json"
    try:
        location = parse_location(inputs_location)
    except (LocationError, ValueError) as exc:
        raise InputsRefused(f"--inputs {inputs_location!r} is not a location: {exc}") from exc
    with tempfile.TemporaryDirectory(prefix="rapidpipe-bind-") as tmp:
        try:
            path = fetch_object(location, "manifest.json", Path(tmp) / "manifest.json",
                                client=s3_client)
        except Exception as exc:  # noqa: BLE001 - classified below
            if _is_transient(exc):
                raise
            raise InputsRefused(
                f"input manifest {join(location, 'manifest.json')} could not be read "
                f"({type(exc).__name__}: {exc}); refusing to submit") from exc
        if not Path(path).is_file():
            raise InputsRefused(f"input manifest not found: {shown}; refusing to submit")
        try:
            return Manifest.read(path)
        except (ManifestError, OSError, ValueError, KeyError, TypeError) as exc:
            raise InputsRefused(
                f"input manifest {shown} is not a valid manifest: {exc}; "
                "refusing to submit") from exc


def manifest_instances(manifest: Manifest) -> list[str]:
    """Every instance id ``manifest`` names as an input for the unit that
    reads it: its output entries' ``instance`` fields and its
    ``inputs.result_sets``, de-duplicated, in order."""
    names = [o.instance for o in manifest.outputs] + list(manifest.inputs.result_sets)
    return list(dict.fromkeys(n for n in names if n))


def read_input_instances(inputs_location: str, *, s3_client: Any = None) -> list[str]:
    """:func:`manifest_instances` of the manifest at ``inputs_location``
    (:func:`read_input_manifest`'s refusals apply). Called before anything
    is written, so a refusal leaves no unit and no attempt behind."""
    return manifest_instances(read_input_manifest(inputs_location, s3_client=s3_client))


def _registered(conn, names: list[str]) -> set[str]:
    if not names:
        return set()
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM product_instances WHERE id = ANY(%s)", (names,))
        return {row[0] for row in cur.fetchall()}


def bind_registered_inputs(
    conn, run_id: str, stage: str, unit_id: str, names: Iterable[str],
) -> tuple[list[str], list[str]]:
    """Bind those of ``names`` that are registered product instances to
    the unit; return ``(bound, skipped)``. Does not commit. Idempotent per
    (unit, instance), so a retry or a re-submission rebinds nothing new.
    With nothing registered, makes no ``unit_inputs`` write at all."""
    names = list(dict.fromkeys(names))
    found = _registered(conn, names)
    bound = [n for n in names if n in found]
    skipped = [n for n in names if n not in found]
    if bound:
        bind_unit_inputs(conn, run_id, stage, unit_id, bound)
    if skipped:
        logger.info(
            "unit (run=%s, stage=%s, unit=%s): %d input name(s) are not registered "
            "product instances and bind nothing: %s",
            run_id, stage, unit_id, len(skipped), ", ".join(skipped))
    return bound, skipped
