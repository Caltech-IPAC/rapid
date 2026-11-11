# ported-from: alerts/gen_schema.py @ 883e3876
"""Write the packaged ``.avsc`` Avro schema files from ``param_registry.py``.

`dev`'s ``alerts/gen_schema.py``, unchanged in what it writes: one
``rapid.v<major>_<minor>.<record>.avsc`` per registry record under
``schema/<major>/<minor>/``, each a record of the registry's non-NOT_USED
params in declaration order, nullable ones defaulting to null, ``@name``
type references qualified by the namespace. The files are never edited
by hand; :func:`rapidpipe.science.alerts.assemble.load_schema` refuses a
production set that differs from the registry.

Two departures from `dev`: ``latest.txt`` is not written (the version is
``param_registry.VERSION`` alone), and a parse failure exits 64 through
:class:`rapidpipe.exitcodes.ArgumentParser`.

Usage::

    python -m rapidpipe.science.alerts.gen_schema            # write VERSION
    python -m rapidpipe.science.alerts.gen_schema 01.02      # write a new version
    python -m rapidpipe.science.alerts.gen_schema --check    # compare, write nothing
"""

from __future__ import annotations

import difflib
import json
import sys
from pathlib import Path
from typing import Any

from rapidpipe.exitcodes import ArgumentParser, ExitCode
from rapidpipe.science.alerts.param_registry import (
    RECORDS, VERSION, AvroType, Record, Status, is_nullable)

#: The packaged ``.avsc`` files: ``schema/<major>/<minor>/rapid.v<major>_<minor>.<record>.avsc``.
SCHEMA_ROOT = Path(__file__).resolve().parent / "schema"


def namespace_for(version: str) -> str:
    """The Avro namespace of a schema version: ``rapid.v<major>_<minor>``."""
    major, minor = version.split(".")
    return f"rapid.v{major}_{minor}"


def schema_path(record: Record, version: str, schema_root: str | Path = SCHEMA_ROOT) -> Path:
    """Where one record's ``.avsc`` file lives for a schema version."""
    major, minor = version.split(".")
    return Path(schema_root) / major / minor / f"{namespace_for(version)}.{record.name}.avsc"


def _resolve_type(avro_type: AvroType, namespace: str) -> AvroType:
    """``avro_type`` with every ``"@name"`` reference replaced by ``"<namespace>.<name>"``."""
    if isinstance(avro_type, str):
        if avro_type.startswith("@"):
            return f"{namespace}.{avro_type[1:]}"
        return avro_type
    if isinstance(avro_type, list):
        return [_resolve_type(t, namespace) for t in avro_type]
    if isinstance(avro_type, dict):
        return {key: (_resolve_type(val, namespace) if key == "items" else val)
                for key, val in avro_type.items()}
    raise TypeError(f"Unexpected avro type spec: {avro_type!r}")


def record_schema(record: Record, version: str, namespace: str | None = None) -> dict[str, Any]:
    """One registry record's Avro schema dict, ready to serialize as its ``.avsc``.

    Params with status NOT_USED are left out. ``namespace`` defaults to
    the version's own.
    """
    namespace = namespace or namespace_for(version)
    fields = []
    for p in record.params:
        if p.status is Status.NOT_USED:
            continue
        entry: dict[str, Any] = {"name": p.name, "type": _resolve_type(p.avro, namespace)}
        if is_nullable(p.avro):
            entry["default"] = None
        entry["doc"] = p.doc
        fields.append(entry)
    return {
        "namespace": namespace,
        "name": record.name,
        "doc": record.doc,
        "version": version,
        "type": "record",
        "fields": fields,
    }


def schema_text(schema: dict[str, Any]) -> str:
    """The bytes written for a schema dict (`dev`'s ``json.dumps(indent=4)`` plus a newline)."""
    return json.dumps(schema, indent=4) + "\n"


def schema_problems(version: str = VERSION, schema_root: str | Path = SCHEMA_ROOT) -> list[str]:
    """Where the on-disk ``.avsc`` files for a version differ from the registry.

    One line per missing or differing file; empty when every file equals
    what :func:`record_schema` would write. ``load_schema`` calls this so
    a stale set fails at load time with a clear message.
    """
    problems = []
    for record in RECORDS:
        path = schema_path(record, version, schema_root)
        if not path.exists():
            problems.append(f"{path.name} is missing")
        elif json.loads(path.read_text()) != record_schema(record, version):
            problems.append(f"{path.name} differs from the registry")
    return problems


def generate(version: str = VERSION, schema_root: str | Path = SCHEMA_ROOT,
             check: bool = False, out=sys.stdout) -> bool:
    """Write (or with ``check=True``, verify) the ``.avsc`` files for a version.

    In check mode prints ``ok``, ``MISSING`` or ``DIFFERS`` per file, with
    a unified diff against the registry for a differing one, and returns
    whether every file is up to date. In write mode writes each file and
    returns True.
    """
    ok = True
    for record in RECORDS:
        schema = record_schema(record, version)
        path = schema_path(record, version, schema_root)
        if check:
            if not path.exists():
                print(f"MISSING  {path}", file=out)
                ok = False
                continue
            existing = json.loads(path.read_text())
            if existing == schema:
                print(f"ok       {path.name}", file=out)
            else:
                ok = False
                print(f"DIFFERS  {path.name}", file=out)
                diff = difflib.unified_diff(
                    json.dumps(existing, indent=2).splitlines(),
                    json.dumps(schema, indent=2).splitlines(),
                    fromfile=str(path), tofile="registry (param_registry.py)",
                    lineterm="")
                for line in diff:
                    print(f"    {line}", file=out)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(schema_text(schema))
            print(f"wrote    {path}", file=out)
    return ok


def main(argv: list[str] | None = None) -> int:
    """Write the ``.avsc`` files, or ``--check`` them: 0 when in sync or written, 1 otherwise."""
    parser = ArgumentParser(
        prog="gen_schema",
        description="Write the RAPID alert .avsc schema files from param_registry.py")
    parser.add_argument("version", nargs="?", default=VERSION,
                        help="schema version <major>.<minor>, zero-padded (default: %(default)s)")
    parser.add_argument("--schema-root", default=str(SCHEMA_ROOT),
                        help="directory holding <major>/<minor>/*.avsc (default: %(default)s)")
    parser.add_argument("--check", action="store_true",
                        help="compare against the existing files; write nothing")
    args = parser.parse_args(argv)

    if args.version.count(".") != 1 or not args.version.replace(".", "").isdigit():
        parser.error(f"version {args.version!r} must be <major>.<minor>")

    ok = generate(args.version, Path(args.schema_root), check=args.check)
    return int(ExitCode.SUCCESS if ok else ExitCode.FAILURE)


if __name__ == "__main__":
    sys.exit(main())
