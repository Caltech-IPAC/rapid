"""`photometry`: a declared stub for forced photometry over a field.

Not ported in this build (supervisor step 8, 2026-09-24, ruling R9): the
declaration below, ``settings/photometry.toml`` and this module's
validation are real, but the science is not. A valid invocation always
exits **69** ("declared, not implemented in this build") once its
arguments, settings and input manifest have all checked out -- the same
rigor a real stage applies before doing its work, just without the work.

`dev`'s ancestor is ``pipeline/forcedPhotometryForField.py``
(``origin/dev``, 1507 lines): forced photometry for a CSV of
``reqid,ra,dec`` sky positions within one field, fit against the
SFFT difference-image PSF at each epoch, falling back to the
corresponding reference-image PSF when the difference-image PSF is not
available. Its documented exit/warning codes (52, 54-58, 60-62, 63, 255,
...) are preserved as comments in ``settings/photometry.toml``; this
build raises none of them.

Where the port would go: a ``rapidpipe.science.photometry`` package
(mirroring ``rapidpipe.science.finalize``/``rapidpipe.science.alerts``),
called from this module's ``_body`` in place of the
:class:`~rapidpipe.stages.contract.NotImplementedInBuild` it raises today;
the settings and input-manifest validation below would carry over
largely unchanged.

Inputs. ``--inputs`` is an input-set manifest (stage ``input-set``, unit
``field``): its outputs list one or more ``difference-image`` entries
(the epochs to fit) and one or more ``psf`` entries (a standalone role
``psf`` member each -- `dev`'s difference-image-then-reference-image PSF
choice is a fixed input here, not resolved by this stage); its
``inputs.result_sets`` names exactly one object set -- a
``statistics-set`` or ``association-set`` instance -- naming the objects
to fit forced photometry for. ``database_access`` is "none": nothing here
is read from a database, including to tell the named object set's kind
apart; a real port would need to (as ``alerts`` does for its own result
sets).

Outputs (not written by this build): one ``light-curve`` result set per
request, per the products page.

This module may import ``rapidpipe.products``, ``rapidpipe.db``,
``rapidpipe.runs`` and ``rapidpipe.science``; never another stage,
``rapidpipe.launch`` or ``rapidpipe.cli``.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from rapidpipe.products.diffimage import validate_difference_entry
from rapidpipe.products.manifest import Manifest, Member, OutputEntry
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

_SETTINGS_PATH = Path(__file__).resolve().parent.parent / "settings" / "photometry.toml"

#: The input manifest's stage: a composed input set, not one producer's manifest.
INPUT_SET_STAGE = "input-set"

#: The two result-set kinds the named object set may be (alerts.py's
#: RESULT_SET_KINDS convention); this stage cannot tell them apart itself
#: (database_access "none"), so it only checks that exactly one is named.
OBJECT_SET_KINDS = ("statistics-set", "association-set")

#: The PSF source choices `dev`'s forced photometry falls back through.
PSF_SOURCES = ("difference", "reference", "difference-then-reference")

DECLARATION = StageDeclaration(
    name="photometry",
    unit="field",
    argument_schema={
        "description": (
            "rapidpipe stage photometry --run <run-id> --unit <unit-id> "
            "--attempt <attempt-id> --inputs <dir> --outputs <dir> "
            "[--settings <toml>] [--dry-run]. --inputs holds an input-set "
            "manifest (stage input-set) naming one or more difference-image "
            "entries, one or more psf entries, and in inputs.result_sets "
            "exactly one object set (a statistics-set or association-set "
            "instance) to fit forced photometry for. Declared, not "
            "implemented in this build: a valid invocation exits 69."
        ),
    },
    settings_schema_path=str(_SETTINGS_PATH),
    consumes=("difference-image", "psf", "statistics-set", "association-set"),
    produces=("light-curve",),
    database_access="none",
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
STUB_MESSAGE = "stage `photometry` is declared but not implemented in this build"


# ----------------------------------------------------------------------
# Settings and inputs
# ----------------------------------------------------------------------


def _positive_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _check_settings(settings: dict) -> None:
    photometry = settings.get("photometry")
    if not isinstance(photometry, dict):
        raise UsageError("[photometry] table is required")
    if photometry.get("psf_source") not in PSF_SOURCES:
        raise UsageError(
            f"[photometry] psf_source must be one of {list(PSF_SOURCES)}, "
            f"got {photometry.get('psf_source')!r}")
    for key in ("stampsz", "stampupsamplefac", "minnumrats"):
        value = photometry.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise UsageError(f"[photometry] {key} must be a positive integer, got {value!r}")
    for key in ("apdiam", "corrunc", "snrforcor", "refmatchrad", "minimum_percent_overlap_area"):
        if not _positive_number(photometry.get(key)):
            raise UsageError(
                f"[photometry] {key} must be a positive number, got {photometry.get(key)!r}")
    frac = photometry.get("maxbadpixfrac")
    if not isinstance(frac, (int, float)) or isinstance(frac, bool) or not 0.0 <= frac <= 1.0:
        raise UsageError(
            f"[photometry] maxbadpixfrac must be a number in [0, 1], got {frac!r}")
    if not isinstance(photometry.get("applyflxcorr"), bool):
        raise UsageError("[photometry] applyflxcorr must be true or false")


def _entries(manifest: Manifest, kind: str) -> list[OutputEntry]:
    return [e for e in manifest.outputs if e.kind == kind]


def _member(entry: OutputEntry, role: str) -> Member:
    members = [m for m in entry.members if m.role == role]
    if len(members) != 1:
        raise InputRejected(
            f"{entry.kind} entry {entry.instance!r} has {len(members)} members "
            f"with role {role!r}, expected exactly one")
    return members[0]


@dataclass(frozen=True)
class _InputSet:
    manifest: Manifest
    differences: tuple[OutputEntry, ...]
    psfs: tuple[OutputEntry, ...]
    object_set: str


def _read_input_set(manifest: Manifest) -> _InputSet:
    if manifest.unit.kind != "field":
        raise InputRejected(
            f"input manifest unit kind is {manifest.unit.kind!r}, expected 'field'")
    if manifest.stage != INPUT_SET_STAGE:
        raise InputRejected(
            f"input manifest is stage {manifest.stage!r}'s, expected {INPUT_SET_STAGE!r}")

    differences = _entries(manifest, "difference-image")
    if not differences:
        raise InputRejected("input manifest carries no difference-image entries")
    for entry in differences:
        try:
            validate_difference_entry(entry.to_dict())
        except ValueError as exc:
            raise InputRejected(f"difference-image {entry.instance!r}: {exc}") from exc

    psfs = _entries(manifest, "psf")
    if not psfs:
        raise InputRejected("input manifest carries no psf entries")
    for entry in psfs:
        _member(entry, "psf")

    named = manifest.inputs.result_sets
    if len(named) != 1:
        raise InputRejected(
            f"input manifest's inputs.result_sets names {len(named)} result sets, "
            f"expected exactly one (a {' or '.join(OBJECT_SET_KINDS)} instance)")
    object_set = named[0]
    if not object_set:
        raise InputRejected("input manifest's named object-set instance id is empty")

    return _InputSet(manifest=manifest, differences=tuple(differences),
                     psfs=tuple(psfs), object_set=object_set)


# ----------------------------------------------------------------------
# The body
# ----------------------------------------------------------------------


def _body(context: StageContext) -> StageResult:
    _check_settings(context.settings)
    inputs = _read_input_set(context.input_manifest)
    context.logger.info(
        "photometry: %d difference-image entries, %d psf entries, object set %s -- "
        "declared, not implemented in this build", len(inputs.differences),
        len(inputs.psfs), inputs.object_set)
    raise NotImplementedInBuild(STUB_MESSAGE)


def main(argv: list[str]) -> int:
    return run_stage(DECLARATION, _body, argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
