"""`export`: a declared stub for a field's HATS catalog export.

Not ported in this build (supervisor step 8, 2026-09-24, ruling R9): the
declaration below, ``settings/export.toml`` and this module's validation
are real, but the science is not. A valid invocation always exits **69**
("declared, not implemented in this build") once its arguments, settings
and input manifest have all checked out.

`dev`'s ancestors are two standalone scripts (``origin/dev``, no ``ppid``
row: `dev` never wired either into the AWS Batch dispatch table):
``pipeline/generateSourceHATSCatalog.py`` (272 lines, a HATS catalog of
the ``sources`` table) and ``pipeline/generateLightCurveHATSCatalog.py``
(426 lines, a HATS light-curve catalog joining ``AstroObjects``,
``Merges`` and ``Sources`` into one light curve per object). Both build
their catalog from a dumped file with ``hats_import``; their
``[HATS_CATALOGS]`` parameter names are preserved as comments in
``settings/export.toml``.

Where the port would go: a ``rapidpipe.science.export`` package, called
from this module's ``_body`` in place of the
:class:`~rapidpipe.stages.contract.NotImplementedInBuild` it raises today,
with the database read this stage declares (``database_access = "read"``)
behind it -- ``alerts.py``'s ``open_database``/``result_set_kinds``
pattern is the model for telling the named result sets apart by kind
before reading their rows.

Inputs. ``--inputs`` is an input-set manifest (stage ``input-set``, unit
``field``): its ``inputs.result_sets`` names, by instance id, one or more
of an ``association-set``, ``statistics-set`` or ``source-set`` -- the
result sets ``[export] catalog_type`` selects the rows to dump from. This
build never opens a database connection to tell them apart by kind (that
belongs to the port); it only checks that the input manifest names at
least one.

Outputs (not written by this build): one ``catalog-export`` file product
(a HATS bundle), per the products page.

``--dry-run`` also runs this input-set validation (via ``run_stage``'s
``validate_inputs`` hook), rejecting an input manifest naming no result
sets with exit 65 -- unlike most stages, whose ``body`` (and so its own
shape checks) never runs under ``--dry-run`` at all, since the science
this stage would otherwise run does not exist to skip.

This module may import ``rapidpipe.products``, ``rapidpipe.db``,
``rapidpipe.runs`` and ``rapidpipe.science``; never another stage,
``rapidpipe.launch`` or ``rapidpipe.cli``.
"""

from __future__ import annotations

import sys
from pathlib import Path

from rapidpipe.products.manifest import Manifest
from rapidpipe.stages.contract import (
    ExitCode,
    InputRejected,
    NotImplementedInBuild,
    StageContext,
    StageDeclaration,
    StageResult,
    UsageError,
    run_stage,
)

_SETTINGS_PATH = Path(__file__).resolve().parent.parent / "settings" / "export.toml"

#: The input manifest's stage: a composed input set, not one producer's manifest.
INPUT_SET_STAGE = "input-set"

#: The three result-set kinds `[export] catalog_type` may draw rows from
#: (alerts.py's RESULT_SET_KINDS convention); this stage cannot tell them
#: apart itself in this build (no database connection is opened), so it
#: only checks that the input manifest names at least one.
RESULT_SET_KINDS = ("association-set", "statistics-set", "source-set")

CATALOG_TYPES = ("sources", "light-curves")

#: hats-import's own ceiling on a healpix order (dev's `highest_healpix_order`).
MAX_HEALPIX_ORDER = 11

DECLARATION = StageDeclaration(
    name="export",
    unit="field",
    argument_schema={
        "description": (
            "rapidpipe stage export --run <run-id> --unit <unit-id> "
            "--attempt <attempt-id> --inputs <dir> --outputs <dir> "
            "[--settings <toml>] [--dry-run]. --inputs holds an input-set "
            "manifest (stage input-set) naming, in inputs.result_sets, one "
            "or more of an association-set, statistics-set or source-set "
            "instance. Declared, not implemented in this build: a valid "
            "invocation exits 69."
        ),
    },
    settings_schema_path=str(_SETTINGS_PATH),
    consumes=("association-set", "statistics-set", "source-set"),
    produces=("catalog-export",),
    database_access="read",
    resource_defaults={"vcpus": 1, "memory_mib": 4096},
    supported_exit_codes=(
        ExitCode.SUCCESS,
        ExitCode.USAGE,
        ExitCode.INPUT_REJECTED,
        ExitCode.NOT_IMPLEMENTED,
        ExitCode.STAGE_ERROR,
        ExitCode.TRANSIENT_FAILURE,
    ),
)

#: The message NotImplementedInBuild carries; fixed by the supervisor
#: ruling and checked verbatim by the selftest fixture and unit tests.
STUB_MESSAGE = "stage `export` is declared but not implemented in this build"


# ----------------------------------------------------------------------
# Settings and inputs
# ----------------------------------------------------------------------


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _check_settings(settings: dict) -> None:
    export = settings.get("export")
    if not isinstance(export, dict) or export.get("catalog_type") not in CATALOG_TYPES:
        raise UsageError(
            f"[export] catalog_type must be one of {list(CATALOG_TYPES)}, "
            f"got {(export or {}).get('catalog_type')!r}")
    hats = settings.get("hats")
    if not isinstance(hats, dict):
        raise UsageError("[hats] table is required")
    if not isinstance(hats.get("format_version"), str) or not hats["format_version"]:
        raise UsageError(
            f"[hats] format_version must be a non-empty string, got {hats.get('format_version')!r}")
    lowest, highest = hats.get("lowest_healpix_order"), hats.get("highest_healpix_order")
    for name, value in (("lowest_healpix_order", lowest), ("highest_healpix_order", highest)):
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= MAX_HEALPIX_ORDER:
            raise UsageError(
                f"[hats] {name} must be an integer in [0, {MAX_HEALPIX_ORDER}], got {value!r}")
    if isinstance(lowest, int) and isinstance(highest, int) and lowest > highest:
        raise UsageError(
            f"[hats] lowest_healpix_order ({lowest}) must not exceed "
            f"highest_healpix_order ({highest})")
    if not _positive_int(hats.get("n_workers")):
        raise UsageError(f"[hats] n_workers must be a positive integer, got {hats.get('n_workers')!r}")
    for key in ("ra_col", "dec_col", "join_column"):
        if not isinstance(hats.get(key), str) or not hats[key]:
            raise UsageError(f"[hats] {key} must be a non-empty string, got {hats.get(key)!r}")
    if not isinstance(hats.get("columns"), list):
        raise UsageError(f"[hats] columns must be a list, got {hats.get('columns')!r}")


def _named_result_sets(manifest: Manifest) -> tuple[str, ...]:
    if manifest.unit.kind != "field":
        raise InputRejected(
            f"input manifest unit kind is {manifest.unit.kind!r}, expected 'field'")
    if manifest.stage != INPUT_SET_STAGE:
        raise InputRejected(
            f"input manifest is stage {manifest.stage!r}'s, expected {INPUT_SET_STAGE!r}")

    named = manifest.inputs.result_sets
    if not named:
        raise InputRejected(
            f"input manifest's inputs.result_sets names no result sets, expected at "
            f"least one ({', '.join(RESULT_SET_KINDS)})")
    if len(set(named)) != len(named):
        raise InputRejected("input manifest's inputs.result_sets names a result set twice")
    for instance in named:
        if not instance:
            raise InputRejected("input manifest's inputs.result_sets names an empty instance id")
    return named


# ----------------------------------------------------------------------
# The body
# ----------------------------------------------------------------------


def _validate_inputs(context: StageContext) -> None:
    """``run_stage``'s pre-dry-run hook: the same named-result-set check
    ``_body`` runs, so ``--dry-run`` rejects an input manifest naming no
    result sets (exit 65) instead of validating only the generic manifest
    shape, as most other stages' dry-run does."""
    _named_result_sets(context.input_manifest)


def _body(context: StageContext) -> StageResult:
    _check_settings(context.settings)
    named = _named_result_sets(context.input_manifest)
    context.logger.info(
        "export: catalog_type=%s, %d named result sets -- declared, not implemented "
        "in this build", context.settings["export"]["catalog_type"], len(named))
    raise NotImplementedInBuild(STUB_MESSAGE)


def main(argv: list[str]) -> int:
    return run_stage(DECLARATION, _body, argv, validate_inputs=_validate_inputs)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
