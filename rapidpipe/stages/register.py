"""`register`: writes product rows from a manifest, without opening files.

Per the stage contract's "Declaration": "`register` records file products
from manifests"; per "The manifest": "Each product kind defines the
registration metadata its manifest entry must carry. `register` validates
that metadata and writes product rows without reading product contents."

It records six kinds. It registers the enclosing manifest's own
instances (`rapidpipe.runs.repository.register_manifest`), then writes
the legacy rows each kind has, all in one transaction:

- `l2-image` (`admit`'s manifest): `l2files`/`l2filemeta`
  (`rapidpipe.db.l2files.register_l2_image`);
- `difference-image` (`difference`'s manifest): `diffimages`/`diffimmeta`,
  one pair per registered differencer
  (`rapidpipe.db.diffimages.register_difference_image`);
- `psf` (designed in for `admit`'s manifest, which carries none today):
  one `psfs` row through `dev`'s ``addPSF`` (``rapidpipe.db.psfs.register_psf``);
- `source-catalog` (also `difference`'s): validated and accepted, nothing
  written beyond its instance row -- the products page's "Today's table"
  for this kind is "none until `load`".
- `alert-container` and `alert-set` (`alerts`'s manifest): validated and
  accepted, nothing written beyond their instance rows
  (``rapidpipe.products.alertcontainer``). The `alerts` stage registers both
  itself, in the transaction that writes their outbox rows, so registering
  its manifest again is a no-op replay.

It stays independently runnable from whatever
stage produced the manifest it reads (stage contract, "The manifest":
"whether it shares a Batch job with a transform changes nothing about
attempt identity, completion or retry safety").

This module may import ``rapidpipe.products``, ``rapidpipe.db`` and
``rapidpipe.runs``; never another stage, ``rapidpipe.launch`` or
``rapidpipe.cli`` (stage contract, dependency direction; see
``tests/unit/test_dependency_direction.py``).
"""

from __future__ import annotations

from rapidpipe.db import connection as _connection_module
from rapidpipe.db.connection import ConnectionUnavailable
from rapidpipe.db.diffimages import register_difference_image
from rapidpipe.db.l2files import register_l2_image
from rapidpipe.db.psfs import register_psf
from rapidpipe.products.alertcontainer import (
    validate_alert_container_entry,
    validate_alert_set_entry,
)
from rapidpipe.products.diffimage import (
    validate_difference_entry,
    validate_source_catalog_entry,
)
from rapidpipe.products.psf import validate_psf_entry
from rapidpipe.runs.repository import register_manifest
from rapidpipe.stages.contract import (
    InputRejected,
    StageContext,
    StageDeclaration,
    StageResult,
    TransientFailure,
    run_stage,
)

#: Product kinds this stage knows how to record. An output entry of any
#: other kind is InputRejected, naming the kind.
_KNOWN_KINDS = ("l2-image", "psf", "difference-image", "source-catalog",
                "alert-container", "alert-set")

DECLARATION = StageDeclaration(
    name="register",
    unit="detector-image",
    argument_schema={
        "description": (
            "rapidpipe stage register --run <run-id> --unit <unit-id> "
            "--attempt <attempt-id> --inputs <dir> --outputs <dir> "
            "[--settings <toml>] [--dry-run]. --inputs holds the "
            "producing attempt's completion manifest (admit's, naming "
            "l2-image and psf entries, or difference's, naming difference-image "
            "and source-catalog entries, or alerts's, naming alert-container "
            "and alert-set entries). <unit-id> is always "
            "<producing stage>/<producing unit id> (rapidpipe.products."
            "manifest.register_unit_id), derived from that same manifest's "
            "own `stage` and `unit.id` -- a register unit is identified by "
            "what it registers, so this stage's own invocation never "
            "chooses it; `rapidpipe run local`/`run submit` derive and "
            "pass it, refusing an explicit --unit for register."
        ),
    },
    settings_schema_path=None,
    consumes=("l2-image", "psf", "difference-image", "source-catalog",
              "alert-container", "alert-set"),
    produces=(),
    database_access="read-write",
    resource_defaults={"vcpus": 1, "memory_mib": 1024},
)


def connect(*args, **kwargs):
    """Module-level indirection to ``rapidpipe.db.connection.connect``.

    A thin wrapper, not a re-export, so tests can monkeypatch
    ``rapidpipe.stages.register.connect`` without reaching into
    ``rapidpipe.db.connection`` and affecting other callers (design
    brief: "a module-level name `connect` so tests can monkeypatch it").
    """
    return _connection_module.connect(*args, **kwargs)


def _reject_unknown_kinds(entries) -> None:
    for entry in entries:
        if entry.kind not in _KNOWN_KINDS:
            raise InputRejected(
                f"register does not know how to record output kind {entry.kind!r}; "
                f"known kinds: {_KNOWN_KINDS}")


def _body(context: StageContext) -> StageResult:
    manifest = context.input_manifest
    _reject_unknown_kinds(manifest.outputs)
    # The difference and psf kinds' blocks are checked before any connection is
    # opened, so a malformed manifest is refused without touching the
    # database.
    for entry in manifest.outputs:
        try:
            if entry.kind == "difference-image":
                validate_difference_entry(entry.to_dict())
            elif entry.kind == "source-catalog":
                validate_source_catalog_entry(entry.to_dict())
            elif entry.kind == "psf":
                validate_psf_entry(entry.to_dict())
            elif entry.kind == "alert-container":
                validate_alert_container_entry(entry.to_dict())
            elif entry.kind == "alert-set":
                validate_alert_set_entry(entry.to_dict())
        except ValueError as exc:
            raise InputRejected(f"{entry.kind} {entry.instance!r}: {exc}") from exc

    manifest_dict = manifest.to_dict()
    products_read: dict[str, str] = {}

    try:
        with connect() as conn:
            try:
                with conn.cursor():
                    pass  # establish the connection is usable before writing.
                register_manifest(
                    conn, manifest_dict, registering_attempt_id=context.attempt_id)

                for entry in manifest.outputs:
                    if entry.kind == "difference-image":
                        register_difference_image(
                            conn,
                            entry=entry.to_dict(),
                            run_id=manifest.run,
                            attempt_id=context.attempt_id,
                            output_location=context.inputs_location,
                        )
                        products_read["difference-image"] = entry.instance
                        continue
                    if entry.kind == "psf":
                        register_psf(
                            conn,
                            entry=entry.to_dict(),
                            run_id=manifest.run,
                            attempt_id=context.attempt_id,
                            output_location=context.inputs_location,
                        )
                        products_read["psf"] = entry.instance
                        continue
                    if entry.kind != "l2-image":
                        continue
                    register_l2_image(
                        conn,
                        entry=entry.to_dict(),
                        run_id=manifest.run,
                        attempt_id=context.attempt_id,
                        # The original --inputs argument, not
                        # context.inputs_dir: for an S3 input that is a
                        # local temp directory run_stage fetched into, and
                        # register must record the location, not the path
                        # (stage contract, "Invocation": "--inputs names a
                        # local directory or S3 prefix").
                        output_location=context.inputs_location,
                    )
                    products_read["l2-image"] = entry.instance

                conn.commit()
            except BaseException:
                conn.rollback()
                raise
    except ConnectionUnavailable as exc:
        raise TransientFailure(f"could not connect to the database: {exc}") from exc
    except _connection_module.psycopg2.OperationalError as exc:
        raise TransientFailure(
            f"database connection lost mid-transaction: {exc}") from exc
    except ValueError as exc:
        raise InputRejected(str(exc)) from exc

    return StageResult(outputs=[], products_read=products_read, result_sets_read=())


def main(argv: list[str]) -> int:
    return run_stage(DECLARATION, _body, argv)


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
