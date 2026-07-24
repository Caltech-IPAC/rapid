"""VARIANT 2 walker: Pydantic model -> Avro .avsc dict.

With registry-style declarations this is no longer a type walker at all:
the Avro type string on each ParamSpec is authoritative (the Python
annotation was *derived from it*, see rapid_pydantic), so building the
.avsc is the same resolve-and-collect that gen_schema.record_schema does
on the registry -- byte-identical output included.

Status plays no part here: IMPLEMENTED and STUB params are both in the
schema, and excluded params (registry NOT_USED) are commented out in the
record class, so they simply do not exist.
"""

from .v2_rapid_pydantic import is_nullable
from .v2_registry_style import VERSION


def namespace(version=VERSION):
    major, minor = version.split(".")
    return f"rapid.v{major}_{minor}"


def _resolve_type(avro_type, ns):
    """Expand "@record" references to namespace-qualified names.
    (Same rule as gen_schema._resolve_type.)"""
    if isinstance(avro_type, str):
        if avro_type.startswith("@"):
            return f"{ns}.{avro_type[1:]}"
        return avro_type
    if isinstance(avro_type, list):
        return [_resolve_type(t, ns) for t in avro_type]
    if isinstance(avro_type, dict):
        return {key: (_resolve_type(val, ns) if key == "items" else val)
                for key, val in avro_type.items()}
    raise TypeError(f"Unexpected avro type spec: {avro_type!r}")


def record_schema(model, version=VERSION):
    """Build the Avro schema dict for one Pydantic record class."""
    ns = namespace(version)
    fields = []
    for name, spec in model.__rapid_params__.items():
        entry = {"name": name, "type": _resolve_type(spec.avro, ns)}
        if is_nullable(spec.avro):
            entry["default"] = None
        entry["doc"] = spec.doc
        fields.append(entry)
    return {
        "namespace": ns,
        "name": model.avro_name,
        "doc": model.avro_doc,
        "version": version,
        "type": "record",
        "fields": fields,
    }
