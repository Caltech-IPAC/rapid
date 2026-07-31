"""
File    : gen_schema.py
Author  : Emily Everetts
Date    : 07/2026

Generate the .avsc Avro schema files from the registry in param_registry.py.
Catches drift between schema and data loads automatically.

Usage:
    python -m alerts.gen_schema              # write current VERSION (from registry)
    python -m alerts.gen_schema 01.02        # write a new version
    python -m alerts.gen_schema --check      # compare, don't write
"""

import argparse
import difflib
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

# Support both `python -m alerts.gen_schema` (module) and
# `python gen_schema.py` (script)
if __package__:
    from .param_registry import RECORDS, VERSION, Record, Status, is_nullable
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from alerts.param_registry import (RECORDS, VERSION, Record, Status,
                                       is_nullable)

# Annotation-only: imported here (not in the dual runtime import above)
# because Pyright only treats AvroType as a type alias while the symbol has
# a single declaration -- one name imported on both `if __package__` branches
# gets two and stops working in type expressions. (Classes such as Record
# are not affected; only aliases are.)
if TYPE_CHECKING:
    from .param_registry import AvroType

SCHEMA_ROOT = Path(__file__).resolve().parent / "schema"

def _resolve_type(avro_type: "AvroType", namespace: str) -> "AvroType":
    """Expand ``"@record"`` references to namespace-qualified names.

    Parameters
    ----------
    avro_type : str or list or dict
        Version-independent Avro type spec from the registry: a type name
        (possibly ``"@record"``-style), a union list, or an array dict.
    namespace : str
        Schema namespace, e.g. ``"rapid.v01_01"``.

    Returns
    -------
    str or list or dict
        The same structure with every ``"@name"`` replaced by
        ``"<namespace>.<name>"``.

    Raises
    ------
    TypeError
        If `avro_type` is not a str, list, or dict.
    """
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


def record_schema(record: Record, version: str,
                  namespace: str) -> dict[str, Any]:
    """
    Build the Avro schema dict for one registry record.
    Note: Params with status NOT_USED are excluded.

    Parameters
    ----------
    record : param_registry.Record
        The record declaration to translate.
    version : str
        Schema version string written into the schema, e.g. ``"01.01"``.
    namespace : str
        Schema namespace, e.g. ``"rapid.v01_01"``.

    Returns
    -------
    dict
        The record's Avro schema, ready to be JSON-serialized as an
        ``.avsc`` file.
    """
    # Avro schema have 'fields', which we call params
    avro_fields = []
    # Loop through
    for p in record.params:
        if p.status is Status.NOT_USED:
            continue
        entry = {"name": p.name, "type": _resolve_type(p.avro, namespace)}
        if is_nullable(p.avro):
            entry["default"] = None
        entry["doc"] = p.doc
        avro_fields.append(entry)
    return {
        "namespace": namespace,
        "name": record.name,
        "doc": record.doc,
        "version": version,
        "type": "record",
        "fields": avro_fields,
    }


def schema_problems(version: str = VERSION,
                    schema_root: str | Path = SCHEMA_ROOT) -> list[str]:
    """Compare the on-disk .avsc files for a version against the registry.

    produce.load_schema() calls this so that stale files fail at load time
    with a clear message instead of a cryptic fastavro error (or a silently
    mis-filled alert) at serialization time. ``--check`` prints full diffs
    and stays the tool for humans; this is the cheap programmatic answer.

    Parameters
    ----------
    version : str, optional
        Schema version to check. Defaults to the registry VERSION.
    schema_root : str or pathlib.Path, optional
        Directory holding ``<major>/<minor>/*.avsc`` and ``latest.txt``.

    Returns
    -------
    list of str
        One human-readable problem per missing or differing file (and for
        a ``latest.txt`` that points at a different version); empty when
        everything is in sync.
    """
    major, minor = version.split(".")
    namespace = f"rapid.v{major}_{minor}"
    out_dir = Path(schema_root) / major / minor

    problems = []
    for record in RECORDS:
        path = out_dir / f"{namespace}.{record.name}.avsc"
        if not path.exists():
            problems.append(f"{path.name} is missing")
        elif json.loads(path.read_text()) != record_schema(record, version,
                                                           namespace):
            problems.append(f"{path.name} differs from the registry")

    latest = Path(schema_root) / "latest.txt"
    if latest.exists():
        pointed = latest.read_text().strip()
        if pointed != version:
            problems.append(f"latest.txt points at {pointed}, but the "
                            f"registry VERSION is {version}")
    return problems


def generate(version: str = VERSION,
             schema_root: str | Path = SCHEMA_ROOT,
             check: bool = False) -> bool:
    """Write (or with ``check=True``, verify) the .avsc files for a version.

    Parameters
    ----------
    version : str, optional
        Schema version to write or check. Defaults to the registry VERSION.
    schema_root : str or pathlib.Path, optional
        Directory holding ``<major>/<minor>/*.avsc`` and ``latest.txt``.
    check : bool, optional
        If True, compare the existing files against the registry and print
        per-file results (with diffs) instead of writing anything. If False
        (the default), write the .avsc files and update ``latest.txt``.

    Returns
    -------
    bool
        True if all files are up to date (check mode) or were written
        (write mode); False if check mode found missing/differing files.
    """
    major, minor = version.split(".")
    namespace = f"rapid.v{major}_{minor}"
    out_dir = Path(schema_root) / major / minor

    ok = True
    for record in RECORDS:
        schema = record_schema(record, version, namespace)
        path = out_dir / f"{namespace}.{record.name}.avsc"
        if check:
            if not path.exists():
                print(f"MISSING  {path}")
                ok = False
                continue
            existing = json.loads(path.read_text())
            if existing == schema:
                print(f"ok       {path.name}")
            else:
                ok = False
                print(f"DIFFERS  {path.name}")
                diff = difflib.unified_diff(
                    json.dumps(existing, indent=2).splitlines(),
                    json.dumps(schema, indent=2).splitlines(),
                    fromfile=str(path), tofile="registry (param_registry.py)",
                    lineterm="")
                for line in diff:
                    print(f"    {line}")
        else:
            out_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(schema, indent=4) + "\n")
            print(f"wrote    {path}")

    if not check:
        latest = Path(schema_root) / "latest.txt"
        latest.write_text(version + "\n")
        print(f"wrote    {latest}")
    return ok


def main(argv: list[str] | None = None) -> int:
    """Run schema generation (or --check) from the command line.

    Parameters
    ----------
    argv : list of str, optional
        Command-line arguments. None (the default) means ``sys.argv[1:]``.

    Returns
    -------
    int
        Process exit status: 0 on success, 1 if ``--check`` found problems.
    """
    parser = argparse.ArgumentParser(
        description="Generate RAPID .avsc schema files from param_registry.py")
    parser.add_argument("version", nargs="?", default=VERSION,
                        help="schema version <major>.<minor>, zero-padded "
                             "(default: %(default)s)")
    parser.add_argument("--schema-root", default=str(SCHEMA_ROOT),
                        help="directory holding <major>/<minor>/*.avsc "
                             "(default: %(default)s)")
    parser.add_argument("--check", action="store_true",
                        help="compare against existing files; write nothing")
    args = parser.parse_args(argv)

    if not args.version.replace(".", "").isdigit() or args.version.count(".") != 1:
        parser.error(f"version {args.version!r} must be <major>.<minor>")

    ok = generate(args.version, Path(args.schema_root), check=args.check)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
