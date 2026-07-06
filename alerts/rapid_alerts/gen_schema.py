"""
Generate the .avsc Avro schema files from the field registry in fields.py.

Replaces generate_schema.sh: the registry is the source of truth and the
.avsc files are build products (still committed, since downstream consumers
need them). --check verifies the committed files match the registry without
writing anything, so drift is caught mechanically.

Usage:
    python -m rapid_alerts.gen_schema              # write schema/01/00/*.avsc
    python -m rapid_alerts.gen_schema 01.01        # write a new version
    python -m rapid_alerts.gen_schema --check      # compare, don't write
"""

import argparse
import difflib
import json
import sys
from pathlib import Path

from .fields import RECORDS, VERSION, Status

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


def _is_nullable(avro_type):
    return isinstance(avro_type, list) and avro_type and avro_type[0] == "null"


def record_schema(record, version, namespace):
    """Build the Avro schema dict for one registry record."""
    fields = []
    for f in record.fields:
        if f.status is Status.NOT_USED:
            continue
        entry = {"name": f.name, "type": _resolve_type(f.avro, namespace)}
        if _is_nullable(f.avro):
            entry["default"] = None
        entry["doc"] = f.doc
        fields.append(entry)
    return {
        "namespace": namespace,
        "name": record.name,
        "doc": record.doc,
        "version": version,
        "type": "record",
        "fields": fields,
    }


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
                    fromfile=str(path), tofile="registry (fields.py)",
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
        description="Generate RAPID .avsc schema files from fields.py")
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
