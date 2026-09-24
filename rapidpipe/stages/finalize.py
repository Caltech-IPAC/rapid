"""`finalize`: a difference attempt's products republished with a stamped header.

Ported from `origin/dev`'s post-processing pipeline (ppid 17,
``pipeline/awsBatchSubmitJobs_runSinglePostProcPipeline.py``), the
difference-image half: `dev` downloads the ZOGY difference image, adds
informational keywords to its primary header, rewrites it with astropy's
``CHECKSUM``/``DATASUM`` (``modules/utils/rapid_pipeline_subs.py``
``addKeywordsToFITSHeader``) and records the whole-file MD5 as
``diffimages.checksum``. `dev` overwrites the S3 object in place and
updates the database row; the rebuild never overwrites a published
instance (products page, "Identity"), so this stage writes a new instance
of the same kind in its own attempt location and `register` records it.
Chain (supervisor ruling, 2026-09-24): difference -> finalize ->
register(finalize output) -> load(finalize output); one `diffimages` row
per image, as `dev`. The raw instances are never registered, so
``inputs.products`` names the difference's own registered upstream (l2,
and the reference when it has an instance row), and the raw ids live in
``finalized_from``/``copied_from``, ``RPFINFRM`` and ``inputs.manifest``.
`dev`'s reference-image stamp is not ported: a reference is the
`reference` stage's product.

Inputs. ``--inputs`` is a difference attempt's output location: its
completion manifest (stage ``difference``). The ``difference-image``
entry of the ``[finalize] differencer`` setting and however many
``source-catalog`` entries are keyed to it (0..n) are republished; another
differencer's entry and its catalogs are dropped, and named in the
execution record's notes. The difference attempt's execution record is
read for provenance. Every member
read is verified (size and SHA-256) first.

Outputs. One ``difference-image`` entry: a new instance id, the same
logical key, the ``difference`` member rewritten with the stamp
(:mod:`rapidpipe.science.finalize.headers`), every other member copied
byte for byte, all members re-hashed; the registration block copied with
``md5`` recomputed over the stamped file and ``finalized_from`` (the input
instance) and ``revision`` (2) added. One ``source-catalog`` entry per
input catalog: a new instance id, key ``difference`` naming the finalized
instance, members copied byte for byte, the registration block copied
with ``copied_from`` added. Member paths are the input's, relative to this
attempt's output location.

This module may import ``rapidpipe.products``, ``rapidpipe.db``,
``rapidpipe.runs`` and ``rapidpipe.science``; never another stage,
``rapidpipe.launch`` or ``rapidpipe.cli``.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rapidpipe.db.ids import new_ulid
from rapidpipe.products.diffimage import (
    DIFFERENCERS,
    SOURCE_CATALOG_PROVENANCE_FIELD,
    validate_difference_entry,
    validate_source_catalog_entry,
)
from rapidpipe.products.manifest import Manifest, Member, OutputEntry, member_for_file
from rapidpipe.science.finalize import headers
from rapidpipe.science.spatial import tessellation_field
from rapidpipe.stages.contract import (
    InputRejected,
    StageContext,
    StageDeclaration,
    StageResult,
    UsageError,
    run_stage,
)

_SETTINGS_PATH = Path(__file__).resolve().parent.parent / "settings" / "finalize.toml"

DECLARATION = StageDeclaration(
    name="finalize",
    unit="detector-image",
    argument_schema={
        "description": (
            "rapidpipe stage finalize --run <run-id> --unit <unit-id> "
            "--attempt <attempt-id> --inputs <dir-or-s3-prefix> "
            "--outputs <dir-or-s3-prefix> [--settings <toml>] [--dry-run]. "
            "--inputs is a difference attempt's output location: its "
            "manifest.json (one difference-image entry and its source-catalog "
            "entries), member files and execution record."
        ),
    },
    settings_schema_path=str(_SETTINGS_PATH),
    consumes=("difference-image", "source-catalog"),
    produces=("difference-image", "source-catalog"),
    database_access="none",
    resource_defaults={"vcpus": 1, "memory_mib": 4096},
)

#: The output revision of a finalized instance (products page: the
#: manifest "records the input instance, the output revision").
REVISION = 2


# ----------------------------------------------------------------------
# Inputs
# ----------------------------------------------------------------------


def _check_settings(settings: dict[str, Any]) -> None:
    differencer = settings.get("finalize", {}).get("differencer")
    if differencer not in DIFFERENCERS:
        raise UsageError(
            f"[finalize] differencer must be one of {sorted(DIFFERENCERS)}, got {differencer!r}")
    pipelines = settings.get("pipelines")
    if not isinstance(pipelines, dict) or not pipelines:
        raise UsageError("[pipelines] must map each differencer to its pipelines row id")
    for name, value in pipelines.items():
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise UsageError(f"[pipelines] {name} must be a positive integer, got {value!r}")


def _sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _md5_of_file(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verified_member_path(inputs_dir: Path, member: Member) -> Path:
    path = inputs_dir / member.path
    if not path.exists():
        raise InputRejected(f"input member file not found: {path}")
    if path.stat().st_size != member.bytes:
        raise InputRejected(
            f"input member {member.path!r}: manifest declares {member.bytes} bytes, "
            f"file is {path.stat().st_size} bytes")
    if _sha256_of_file(path) != member.sha256.removeprefix("sha256:"):
        raise InputRejected(f"input member {member.path!r}: SHA-256 mismatch")
    return path


@dataclass
class _InputSet:
    manifest: Manifest
    difference: OutputEntry
    catalogs: list[OutputEntry]      # in input order
    dropped: list[dict[str, str]]    # other differencers' entries, not republished


def _catalog_entries(manifest: Manifest, difference: OutputEntry) -> list[OutputEntry]:
    """Every source-catalog entry keyed to the difference instance, in input order.

    However many the input carries (0..n) pass through; none is required
    (supervisor amendment, 2026-09-24).
    """
    return [e for e in manifest.outputs if e.kind == "source-catalog"
            and e.key.get("difference") == difference.instance]


def _read_input_set(context: StageContext) -> _InputSet:
    manifest = context.input_manifest
    if manifest.unit.kind != "detector-image":
        raise InputRejected(
            f"input manifest unit kind is {manifest.unit.kind!r}, expected 'detector-image'")
    if manifest.stage != "difference":
        raise InputRejected(
            f"input manifest is stage {manifest.stage!r}'s, expected 'difference'")

    differencer = context.settings["finalize"]["differencer"]
    differences = [e for e in manifest.outputs if e.kind == "difference-image"]
    selected = [e for e in differences if e.key.get("differencer") == differencer]
    if len(selected) != 1:
        raise InputRejected(
            f"input manifest has {len(selected)} difference-image entries for differencer "
            f"{differencer!r} ([finalize] differencer), expected exactly one")
    difference = selected[0]
    try:
        validate_difference_entry(difference.to_dict())
    except ValueError as exc:
        raise InputRejected(f"difference-image {difference.instance!r}: {exc}") from exc

    catalogs = _catalog_entries(manifest, difference)
    for entry in catalogs:
        try:
            validate_source_catalog_entry(entry.to_dict())
        except ValueError as exc:
            raise InputRejected(f"source-catalog {entry.instance!r}: {exc}") from exc

    # Another differencer's instance and its catalogs are dropped, and noted
    # (supervisor ruling, 2026-09-24); anything else is refused.
    others = {e.instance for e in differences if e is not difference}
    dropped = [e for e in manifest.outputs
               if e.instance in others
               or (e.kind == "source-catalog" and e.key.get("difference") in others)]
    republished = {difference.instance} | {e.instance for e in catalogs}
    extra = [f"{e.kind} {e.instance}" for e in manifest.outputs
             if e.instance not in republished and e not in dropped]
    if extra:
        raise InputRejected(
            f"input manifest carries entries finalize does not republish: {extra}")

    for entry in (difference, *catalogs):
        for member in entry.members:
            _verified_member_path(context.inputs_dir, member)
    return _InputSet(
        manifest=manifest, difference=difference, catalogs=catalogs,
        dropped=[{"kind": e.kind, "instance": e.instance,
                  "differencer": (e.key.get("differencer") if e.kind == "difference-image"
                                  else next(d.key.get("differencer") for d in differences
                                            if d.instance == e.key.get("difference")))}
                 for e in dropped])


def _difference_execution_record(context: StageContext) -> dict[str, Any]:
    """The difference attempt's execution record, or ``{}`` where absent."""
    path = context.inputs_dir / context.input_manifest.execution_record
    if not path.is_file():
        return {}
    try:
        record = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InputRejected(f"difference execution record {path} is unreadable: {exc}") from exc
    if not isinstance(record, dict):
        raise InputRejected(f"difference execution record {path} is not a JSON object")
    return record


# ----------------------------------------------------------------------
# The transform
# ----------------------------------------------------------------------


def stamp_values(*, context: StageContext, inputs: _InputSet, instance: str,
                 date: str) -> headers.StampValues:
    """Gather the stamp for the finalized instance ``instance``."""
    difference = inputs.difference
    key = difference.key
    registration = difference.registration
    products = inputs.manifest.inputs.products
    record = _difference_execution_record(context)
    try:
        ppid = headers.ppid_for(key["differencer"], context.settings["pipelines"])
    except ValueError as exc:
        raise InputRejected(str(exc)) from exc
    centre = registration["centre"]
    return headers.StampValues(
        run=context.run_id,
        attempt=context.attempt_id,
        instance=instance,
        finalized_from=difference.instance,
        # `difference` names the reference in inputs.products only when the
        # rebuild registered it; the logical key always carries both ids.
        l2_instance=products.get("l2-image") or key["l2"],
        reference_instance=products.get("reference-image") or key["reference"],
        differencer=key["differencer"],
        settings_hash=key["settings_hash"],
        finalize_settings_hash="sha256:" + context.settings_hash,
        source_revision=headers.provenance_value(record.get("source_revision")),
        image_digest=headers.provenance_value(record.get("image_digest")),
        output_location=context.outputs_location,
        ppid=ppid,
        infobits=int(registration["catalog_outcome_bits"]),
        field=tessellation_field(float(centre["ra"]), float(centre["dec"])),
        diff_filename=Path(difference.primary).name,
        date=date,
    )


def _copy_member(inputs_dir: Path, outputs_dir: Path, member: Member) -> Member:
    destination = outputs_dir / member.path
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(inputs_dir / member.path, destination)
    return member_for_file(role=member.role, path=destination, relative_to=outputs_dir)


def finalized_difference_entry(*, inputs_dir: Path, outputs_dir: Path, entry: OutputEntry,
                               instance: str,
                               cards: list[tuple[str, Any, str]]) -> OutputEntry:
    """The finalized difference-image entry; writes its members under ``outputs_dir``."""
    members = []
    for member in entry.members:
        if member.path == entry.primary:
            destination = outputs_dir / member.path
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                headers.check_readable(inputs_dir / member.path)
            except Exception as exc:  # noqa: BLE001 -- astropy raises several types
                raise InputRejected(
                    f"difference member {member.path!r} is not a readable FITS file: {exc}"
                ) from exc
            headers.write_stamped(inputs_dir / member.path, destination, cards)
            members.append(member_for_file(
                role=member.role, path=destination, relative_to=outputs_dir))
        else:
            members.append(_copy_member(inputs_dir, outputs_dir, member))

    registration = dict(entry.registration)
    registration["md5"] = _md5_of_file(outputs_dir / entry.primary)
    registration["finalized_from"] = entry.instance
    registration["revision"] = REVISION
    return OutputEntry(
        kind=entry.kind, format_version=entry.format_version, instance=instance,
        key=dict(entry.key), members=tuple(members), primary=entry.primary,
        registration=registration)


def finalized_catalog_entry(*, inputs_dir: Path, outputs_dir: Path, entry: OutputEntry,
                            difference_instance: str) -> OutputEntry:
    """A source-catalog entry copied under a new instance keyed to ``difference_instance``."""
    members = tuple(_copy_member(inputs_dir, outputs_dir, m) for m in entry.members)
    registration = dict(entry.registration)
    registration[SOURCE_CATALOG_PROVENANCE_FIELD] = entry.instance
    return OutputEntry(
        kind=entry.kind, format_version=entry.format_version, instance=new_ulid(),
        key={**entry.key, "difference": difference_instance}, members=members,
        primary=entry.primary, registration=registration)


#: The upstream kinds finalize names in ``inputs.products``.
UPSTREAM_KINDS = ("l2-image", "reference-image")


def products_read(inputs: _InputSet) -> dict[str, str]:
    """The difference manifest's own registered upstream: its l2 and reference.

    Supervisor ruling (2026-09-24, option b): ``inputs.products`` names only
    instances that have rows for `register`'s dependency edges. The raw
    difference and catalog instances are never registered (chain
    difference -> finalize -> register), so they are recorded in
    ``finalized_from``/``copied_from``, ``RPFINFRM`` and ``inputs.manifest``
    instead. ``reference-image`` is named only when the difference manifest
    names it: a reference registered by `dev` has no instance row
    (``difference.py``'s ``products_read``).
    """
    upstream = inputs.manifest.inputs.products
    return {kind: upstream[kind] for kind in UPSTREAM_KINDS if upstream.get(kind)}


# ----------------------------------------------------------------------
# The body
# ----------------------------------------------------------------------


def _body(context: StageContext) -> StageResult:
    _check_settings(context.settings)
    headers.check_keywords()
    inputs = _read_input_set(context)

    instance = new_ulid()
    values = stamp_values(context=context, inputs=inputs, instance=instance,
                          date=headers.utc_date())
    cards = headers.stamp_cards(values)

    difference = finalized_difference_entry(
        inputs_dir=context.inputs_dir, outputs_dir=context.outputs_dir,
        entry=inputs.difference, instance=instance, cards=cards)
    try:
        validate_difference_entry(difference.to_dict())
    except ValueError as exc:
        raise InputRejected(f"finalized difference-image block: {exc}") from exc

    outputs = [difference]
    for entry in inputs.catalogs:
        outputs.append(finalized_catalog_entry(
            inputs_dir=context.inputs_dir, outputs_dir=context.outputs_dir,
            entry=entry, difference_instance=instance))

    context.logger.info(
        "finalize: %s -> %s (%s), %d catalogs", inputs.difference.instance, instance,
        values.diff_filename, len(inputs.catalogs))
    notes = {"dropped": inputs.dropped} if inputs.dropped else {}
    return StageResult(outputs=outputs, products_read=products_read(inputs),
                       execution_notes=notes)


def main(argv: list[str]) -> int:
    return run_stage(DECLARATION, _body, argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
