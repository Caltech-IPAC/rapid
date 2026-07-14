"""
Generate the .avsc Avro schema files from the registry in param_registry.py.

Replaces generate_schema.sh: the registry is the source of truth and the
.avsc files are build products (still committed, since downstream consumers
need them). --check verifies the committed files match the registry without
writing anything, so drift is caught mechanically.

Note: the generated files contain a "fields" array -- that token is Avro's,
even though this codebase calls schema fields "params".

Usage:
    python -m rapid_alerts.gen_schema              # write current VERSION
    python -m rapid_alerts.gen_schema 01.02        # write a new version
    python -m rapid_alerts.gen_schema --check      # compare, don't write
"""

import argparse
import difflib
import json
import sys
from pathlib import Path

from .param_registry import RECORDS, VERSION, Status, is_nullable

SCHEMA_ROOT = Path(__file__).resolve().parent.parent / "schema"


def _resolve_type(avro_type, namespace):
    """Expand "@record" references to namespace-qualified names."""
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


def record_schema(record, version, namespace):
    """Build the Avro schema dict for one registry record."""
    avro_fields = []  # "fields" is the Avro spec's name for what we call params
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


def schema_problems(version=VERSION, schema_root=SCHEMA_ROOT):
    """Compare the on-disk .avsc files for a version against the registry.

    Returns a list of problem strings, empty when everything is in sync.
    produce.load_schema() calls this so that stale files fail at load time
    with a clear message instead of a cryptic fastavro error (or a silently
    mis-filled alert) at serialization time. --check prints full diffs and
    stays the tool for humans; this is the cheap programmatic answer.
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


def generate(version=VERSION, schema_root=SCHEMA_ROOT, check=False):
    """Write (or with check=True, verify) the .avsc files for a version.

    Returns True if all files are up to date / were written successfully.
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


def main(argv=None):
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
