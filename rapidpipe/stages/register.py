"""`register`: writes product rows from a manifest, without opening files.

Per the stage contract's "Declaration": "`register` records file products
from manifests"; per "The manifest": "Each product kind defines the
registration metadata its manifest entry must carry. `register` validates
that metadata and writes product rows without reading product contents."

Today this stage knows how to record one kind, `l2-image`: it registers
the enclosing manifest's own instances (`rapidpipe.runs.repository.
register_manifest`) and then writes the `l2files`/`l2filemeta` rows for
each `l2-image` output entry (`rapidpipe.db.l2files.register_l2_image`),
all in one transaction. It stays independently runnable from whatever
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
from rapidpipe.db.l2files import register_l2_image
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
#: other kind is InputRejected, naming the kind (design brief: "every
#: output entry must be of a kind this stage knows how to record, which
#: today is l2-image only").
_KNOWN_KINDS = ("l2-image",)

DECLARATION = StageDeclaration(
    name="register",
    unit="detector-image",
    argument_schema={
        "description": (
            "rapidpipe stage register --run <run-id> --unit <unit-id> "
            "--attempt <attempt-id> --inputs <dir> --outputs <dir> "
            "[--settings <toml>] [--dry-run]. --inputs holds the "
            "producing attempt's completion manifest (currently admit's), "
            "naming one or more l2-image output entries."
        ),
    },
    settings_schema_path=None,
    consumes=("l2-image",),
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
                    if entry.kind != "l2-image":
                        continue
                    register_l2_image(
                        conn,
                        entry=entry.to_dict(),
                        run_id=manifest.run,
                        attempt_id=context.attempt_id,
                        output_location=str(context.inputs_dir),
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
