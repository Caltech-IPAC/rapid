"""VARIANT 2 machinery -- registry-style declarations on top of pydantic.

``param()`` plus the RapidMeta metaclass let a schema record be declared
with the same layout as param_registry.py -- one param per statement, the
Avro type as a plain string, status/source/attr in their registry
positions -- while still producing a normal pydantic model underneath
(validation, aliases, from_attributes, ValidationError on missing or None
non-nullables).

How it works: pydantic requires every field to carry a type annotation, so
a plain assignment like ``ra = param("double", ...)`` is rejected by
BaseModel. RapidMeta -- a subclass of pydantic's own metaclass, the same
approach SQLModel takes -- rewrites the class namespace first: it derives
the Python annotation from the Avro type (long -> int, ["null", "float"]
-> Optional[float], "@diaSource" -> the DiaSource model, arrays ->
list[...]) and replaces the ParamSpec with an ordinary pydantic Field
carrying the alias/description/default. The Avro string is therefore the
single source of truth for both the wire schema and the Python type; the
width-marker problem (int-vs-long) disappears. The original specs are kept
on the class as ``__rapid_params__`` for the .avsc walker, the provenance
inventory, and source_check().

Record name comes from the class keyword and the doc from the docstring:

    class DiaSource(RapidRecord, name="diaSource"):
        \"\"\"RAPID alert schema: ...\"\"\"
        diaSourceId = param("long", "Unique identifier ...",
                        IMPLEMENTED, "sources.sid", attr="sid")

Trade-off vs. vanilla pydantic: static type checkers cannot see the
synthesized annotations, so IDE autocomplete / mypy on model instances is
weaker. Nothing is lost relative to the registry (plain runtime data
today), but it is a real difference from the Annotated style kept in
v1_annotated.py.
"""

import dataclasses
from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Any, Callable, Optional

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

ModelMetaclass = type(BaseModel)

_UNSET = object()


class Status(Enum):
    # No NOT_USED here: a param excluded from the schema is simply
    # commented out in the record class, with the reason in the comment.
    IMPLEMENTED = "implemented"
    STUB = "stub"


IMPLEMENTED = Status.IMPLEMENTED
STUB = Status.STUB


@dataclass(frozen=True)
class ParamSpec:
    """One declared param (registry Param minus the name, which comes from
    the attribute the spec is assigned to)."""

    avro: Any                     # Avro type (str, union list, or dict)
    doc: str
    status: Status
    source: Optional[str] = None  # implemented: where the value comes from
                                  # stub: what work would fill it in
    attr: Optional[str] = None    # attribute read from the provider record
                                  # (default: the param's own name)
    transform: Optional[Callable] = None  # value-level rework of the attr
                                          # (registry getter analogue; gets
                                          # the attribute value, not the
                                          # whole record)
    default: Any = _UNSET         # nullable params default to None


def param(avro, doc, status, source=None, *, attr=None, transform=None,
          default=_UNSET):
    return ParamSpec(avro, doc, status, source, attr, transform, default)


def is_nullable(avro_type):
    """True if the Avro type is a union whose first member is null.
    (Same rule as param_registry.is_nullable.)"""
    return (isinstance(avro_type, list) and bool(avro_type)
            and avro_type[0] == "null")


_PY_TYPES = {"long": int, "int": int, "float": float, "double": float,
             "boolean": bool, "string": str, "bytes": bytes}

# "@name" record references, filled in as RapidRecord subclasses are
# defined -- which is why records must be declared in dependency order,
# exactly like RECORDS in the registry.
_RECORDS = {}


def _python_type(avro):
    """Derive the Python annotation from an Avro type spec."""
    if isinstance(avro, str):
        if avro.startswith("@"):
            try:
                return _RECORDS[avro[1:]]
            except KeyError:
                raise TypeError(f"record {avro!r} referenced before it was "
                                f"defined (declare records in dependency "
                                f"order)") from None
        return _PY_TYPES[avro]
    if isinstance(avro, list):          # ["null", X] union
        inner = next(t for t in avro if t != "null")
        return Optional[_python_type(inner)]
    if isinstance(avro, dict):          # {"type": "array", "items": X}
        return list[_python_type(avro["items"])]
    raise TypeError(f"Unexpected avro type spec: {avro!r}")


class RapidMeta(ModelMetaclass):
    """Turns param() assignments into annotated pydantic fields."""

    def __new__(mcs, cls_name, bases, namespace, name=None, **kwargs):
        specs = {attr_name: value for attr_name, value in namespace.items()
                 if isinstance(value, ParamSpec)}
        if specs:
            annotations = dict(namespace.get("__annotations__", {}))
            for field_name, spec in specs.items():
                py = _python_type(spec.avro)
                if spec.transform is not None:
                    py = Annotated[py, BeforeValidator(spec.transform)]
                field_kwargs = {"description": spec.doc}
                if spec.attr:
                    field_kwargs["validation_alias"] = spec.attr
                if spec.default is not _UNSET:
                    field_kwargs["default"] = spec.default
                elif is_nullable(spec.avro):
                    field_kwargs["default"] = None
                annotations[field_name] = py
                namespace[field_name] = Field(**field_kwargs)
            namespace["__annotations__"] = annotations
        namespace["__rapid_params__"] = specs  # declaration order preserved
        cls = super().__new__(mcs, cls_name, bases, namespace, **kwargs)
        if name is not None:
            cls.avro_name = name
            cls.avro_doc = (cls.__doc__ or "").strip()
            _RECORDS[name] = cls
        return cls

    def __init__(cls, cls_name, bases, namespace, name=None, **kwargs):
        super().__init__(cls_name, bases, namespace, **kwargs)


class RapidRecord(BaseModel, metaclass=RapidMeta):
    """Base class for schema records: shared config plus the two registry
    behaviors pydantic does not give us by itself (stub enforcement and
    the import-time source check)."""

    # populate_by_name must stay False (the pydantic default): transformed
    # params (isNegative) rely on alias-only input to apply their transform
    # exactly once. Enabling it would let field-name input through, and a
    # model rebuilt from its own dump would silently double-transform.
    model_config = ConfigDict(from_attributes=True)

    def __init_subclass__(cls, name=None, **kwargs):
        # RapidMeta consumes `name` before type.__new__ forwards class
        # keywords here, so at runtime it never arrives; declaring it
        # anyway is what tells static checkers the keyword is legal.
        super().__init_subclass__(**kwargs)

    @model_validator(mode="after")
    def _null_stub_fields(self):
        # produce.build_record's rule: a STUB param serializes as null even
        # if its attr/transform is already staged (diaForcedSource stages
        # all of them). Without this, a staged attr would leak a real
        # provider value into the packet.
        for field_name, spec in type(self).__rapid_params__.items():
            if spec.status is STUB and getattr(self, field_name) is not None:
                setattr(self, field_name, None)
        return self

    @classmethod
    def source_check(cls, data_cls):
        """Import-time guard, ported from produce._validate_registry():
        every IMPLEMENTED param must read an attribute or property that
        exists on ``data_cls`` (the provider record this model is built
        from). Returns a list of problem strings, empty when clean.

        This matters most for *nullable* implemented params: a typo'd attr
        there would not fail validation -- the field would silently default
        to None on every alert. Run this at import so the typo is caught
        before any alert is built.
        """
        available = {f.name for f in dataclasses.fields(data_cls)}
        available |= {attr_name for attr_name, value in vars(data_cls).items()
                      if isinstance(value, property)}
        problems = []
        for field_name, spec in cls.__rapid_params__.items():
            if spec.status is not Status.IMPLEMENTED:
                continue
            attr = spec.attr or field_name
            if attr not in available:
                problems.append(
                    f"{cls.avro_name}.{field_name} reads {data_cls.__name__}."
                    f"{attr}, which does not exist")
        return problems
