"""
Registry-driven record builders.

Each builder walks the field list in fields.py: implemented fields call
their getter against the normalized record, stub fields come out null.
Because the same registry generates the .avsc files, a field cannot exist
in the schema without being accounted for here (and vice versa).
"""

from .fields import DIA_SOURCE_FIELDS, DIA_FORCED_SOURCE_FIELDS, DIA_OBJECT_FIELDS


def build_record(field_list, data):
    """Build a schema-conforming dict by applying each field's getter."""
    return {
        f.name: (f.getter(data) if f.getter is not None else None)
        for f in field_list
    }


def build_dia_source(detection):
    """diaSource dict from a records.Detection."""
    return build_record(DIA_SOURCE_FIELDS, detection)


def build_dia_object(obj):
    """diaObject dict from a records.ObjectRecord."""
    return build_record(DIA_OBJECT_FIELDS, obj)


def build_dia_forced_source(forced_phot):
    """diaForcedSource dict from a records.ForcedPhot."""
    return build_record(DIA_FORCED_SOURCE_FIELDS, forced_phot)
