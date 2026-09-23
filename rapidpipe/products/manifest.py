"""The completion manifest: dataclasses, JSON I/O, and validation.

A stage publishes this manifest only after its outputs are complete (stage
contract, "The manifest"). Its shape is fixed by the products page's "A
complete manifest" example, which is this module's authority:
https://roman-rapid.readthedocs.io/en/latest/system/products.html

The manifest records: its own schema version; the run, unit, stage and
attempt identifiers; a reference to the execution record kept by
``rapidpipe.runs``; the input manifest this attempt read and the upstream
product and database-result-set instances it consumed; and each output's
kind, format version, instance id, logical key, member files (with byte
size and SHA-256 per file), and registration metadata. A database result
set is an output with no members and no primary -- its members list is
always empty (products page, "Database result sets": "rows, not files").

This module imports only the standard library and ``rapidpipe.products.ids``:
no ``rapidpipe.runs``, ``rapidpipe.db`` or stage module.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any

#: The schema version this module reads and writes. Bumped when the
#: manifest's field set or semantics change.
SCHEMA_VERSION = "1"

#: The four units of work a stage may declare (stage contract, "Declaration").
UNIT_KINDS = ("exposure", "detector-image", "field", "processing-date")

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

_READ_CHUNK = 1024 * 1024


class ManifestError(ValueError):
    """A manifest failed validation."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ManifestError(message)


def _is_relative_and_contained(path: str) -> bool:
    """True if ``path`` is relative and never escapes its base via ``..``."""
    if not path or path.startswith("/"):
        return False
    pure = PurePosixPath(path)
    if pure.is_absolute():
        return False
    return ".." not in pure.parts


@dataclass(frozen=True)
class Member:
    """One file belonging to an output entry.

    ``role`` distinguishes members of a bundle (e.g. ``difference``,
    ``uncertainty``, ``significance``); ``path`` is relative to the
    attempt's output location and must not escape it.
    """

    role: str
    path: str
    bytes: int
    sha256: str

    def validate(self) -> None:
        _require(bool(self.role), "member missing required field 'role'")
        _require(bool(self.path), "member missing required field 'path'")
        _require(
            _is_relative_and_contained(self.path),
            f"member path {self.path!r} must be relative to the attempt's "
            "output location, with no leading '/' and no '..' segment")
        _require(
            isinstance(self.bytes, int) and not isinstance(self.bytes, bool)
            and self.bytes >= 0,
            f"member {self.path!r} has an invalid 'bytes' value: {self.bytes!r}")
        _require(
            bool(self.sha256) and _SHA256_RE.match(self.sha256) is not None,
            f"member {self.path!r} has an invalid 'sha256' value: "
            f"{self.sha256!r}; expected 'sha256:' followed by 64 hex digits")

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "path": self.path,
            "bytes": self.bytes,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Member":
        known = {f.name for f in fields(cls)}
        unknown = set(d) - known
        if unknown:
            raise ManifestError(f"member has unknown fields: {sorted(unknown)}")
        return cls(**d)


@dataclass(frozen=True)
class OutputEntry:
    """One product a stage wrote: a file (bundle) or a database result set.

    A file product has one or more ``members`` and a ``primary`` naming
    which member is the product's main file; a database result set has no
    members (an empty tuple) and no ``primary`` (products page, "Database
    result sets": completion lives on the result-set record, not a file).
    ``key`` is the logical key identifying what this output replaces or
    selects (products page, "Identity"); ``registration`` is the metadata
    ``register`` needs to write rows without reading the product.
    """

    kind: str
    format_version: str
    instance: str
    key: dict[str, Any]
    members: tuple[Member, ...] = field(default_factory=tuple)
    primary: str | None = None
    registration: dict[str, Any] = field(default_factory=dict)

    def is_result_set(self) -> bool:
        return not self.members

    def validate(self) -> None:
        for attr in ("kind", "format_version", "instance"):
            _require(
                bool(getattr(self, attr)),
                f"output entry missing required field {attr!r}")
        _require(
            isinstance(self.key, dict) and bool(self.key),
            f"output {self.instance!r} has an empty or invalid 'key'")

        for member in self.members:
            member.validate()

        member_paths = {m.path for m in self.members}
        _require(
            len(member_paths) == len(self.members),
            f"output {self.instance!r} has duplicate member paths")

        if self.members:
            _require(
                self.primary is not None,
                f"output {self.instance!r} has members but no 'primary'")
            _require(
                self.primary in member_paths,
                f"output {self.instance!r}: primary {self.primary!r} is "
                "not one of its member paths")
        else:
            _require(
                self.primary is None,
                f"output {self.instance!r} is a result set (no members) "
                "but declares a 'primary'")

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "kind": self.kind,
            "format_version": self.format_version,
            "instance": self.instance,
            "key": dict(self.key),
            "primary": self.primary,
            "members": [m.to_dict() for m in self.members],
            "registration": dict(self.registration),
        }
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "OutputEntry":
        d = dict(d)
        members = tuple(Member.from_dict(m) for m in d.pop("members", []))
        known = {f.name for f in fields(cls)} - {"members"}
        unknown = set(d) - known
        if unknown:
            raise ManifestError(f"output entry has unknown fields: {sorted(unknown)}")
        d.setdefault("primary", None)
        d.setdefault("registration", {})
        return cls(members=members, **d)


@dataclass(frozen=True)
class Unit:
    """The unit of work this manifest's attempt processed."""

    kind: str
    id: str

    def validate(self) -> None:
        _require(bool(self.id), "unit missing required field 'id'")
        _require(
            self.kind in UNIT_KINDS,
            f"unit has unknown kind {self.kind!r}; expected one of {UNIT_KINDS}")

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "id": self.id}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Unit":
        known = {f.name for f in fields(cls)}
        unknown = set(d) - known
        if unknown:
            raise ManifestError(f"unit has unknown fields: {sorted(unknown)}")
        return cls(**d)


@dataclass(frozen=True)
class Inputs:
    """What this attempt read: its input manifest, products, result sets.

    ``products`` maps a consumed product kind to the upstream instance id
    the stage read (products page, "Identity": downstream stages reference
    an instance id, never a bare logical key). ``result_sets`` lists the
    database result-set instance ids read, for the stages the stage
    contract names (``crossmatch``, ``statistics``, ``prune``); transform
    stages that declare no database access leave it empty.
    """

    manifest: str
    products: dict[str, str] = field(default_factory=dict)
    result_sets: tuple[str, ...] = field(default_factory=tuple)

    def validate(self) -> None:
        _require(bool(self.manifest), "inputs missing required field 'manifest'")
        for kind, instance in self.products.items():
            _require(
                bool(instance),
                f"inputs.products[{kind!r}] must be a non-empty instance id")

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest": self.manifest,
            "products": dict(self.products),
            "result_sets": list(self.result_sets),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Inputs":
        d = dict(d)
        result_sets = tuple(d.pop("result_sets", []))
        known = {f.name for f in fields(cls)} - {"result_sets"}
        unknown = set(d) - known
        if unknown:
            raise ManifestError(f"inputs has unknown fields: {sorted(unknown)}")
        d.setdefault("products", {})
        return cls(result_sets=result_sets, **d)


@dataclass(frozen=True)
class Manifest:
    """A stage's record of what it read and what it wrote.

    Matches the products page's "A complete manifest" example field for
    field: ``schema_version``, ``run``, ``unit``, ``stage``, ``attempt``,
    ``execution_record``, ``inputs``, ``outputs``.
    """

    run: str
    unit: Unit
    stage: str
    attempt: str
    execution_record: str
    inputs: Inputs
    outputs: tuple[OutputEntry, ...] = field(default_factory=tuple)
    schema_version: str = SCHEMA_VERSION

    def validate(self) -> None:
        required = {
            "run": self.run,
            "stage": self.stage,
            "attempt": self.attempt,
            "execution_record": self.execution_record,
        }
        for name, value in required.items():
            _require(bool(value), f"manifest missing required field {name!r}")
        _require(
            self.schema_version == SCHEMA_VERSION,
            f"unsupported manifest schema_version {self.schema_version!r}; "
            f"expected {SCHEMA_VERSION!r}")

        self.unit.validate()
        self.inputs.validate()

        seen_instances: set[str] = set()
        for output in self.outputs:
            output.validate()
            _require(
                output.instance not in seen_instances,
                f"duplicate output instance {output.instance!r}")
            seen_instances.add(output.instance)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run": self.run,
            "unit": self.unit.to_dict(),
            "stage": self.stage,
            "attempt": self.attempt,
            "execution_record": self.execution_record,
            "inputs": self.inputs.to_dict(),
            "outputs": [o.to_dict() for o in self.outputs],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Manifest":
        d = dict(d)
        unit = Unit.from_dict(d.pop("unit"))
        inputs = Inputs.from_dict(d.pop("inputs"))
        outputs = tuple(OutputEntry.from_dict(o) for o in d.pop("outputs", []))
        known = {f.name for f in fields(cls)} - {"unit", "inputs", "outputs"}
        unknown = set(d) - known
        if unknown:
            raise ManifestError(f"manifest has unknown fields: {sorted(unknown)}")
        d.setdefault("schema_version", SCHEMA_VERSION)
        return cls(unit=unit, inputs=inputs, outputs=outputs, **d)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"

    def write(self, path: str | Path) -> None:
        """Validate, then write this manifest as JSON to ``path``, atomically.

        Writes to a temp file in the same directory and renames it into
        place, so a reader never observes a partially written manifest.
        """
        self.validate()
        path = Path(path)
        tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
        tmp_path.write_text(self.to_json())
        os.replace(tmp_path, path)

    @classmethod
    def read(cls, path: str | Path) -> "Manifest":
        """Read and validate a manifest from ``path``."""
        try:
            raw = json.loads(Path(path).read_text())
        except FileNotFoundError:
            raise
        except json.JSONDecodeError as exc:
            raise ManifestError(f"{path}: not valid JSON: {exc}") from exc
        manifest = cls.from_dict(raw)
        manifest.validate()
        return manifest


#: Backwards-compatible alias: the products page and the stage contract
#: call this type "the manifest"; ``CompletionManifest`` was this module's
#: working name before the shape was pinned to the products page.
CompletionManifest = Manifest


def register_unit_id(manifest: "Manifest") -> str:
    """The unit id a ``register`` unit recording ``manifest`` must use.

    A `register` unit is identified by what it registers (Ben, 2026-09-23):
    ``<producing stage>/<producing unit id>``, e.g.
    ``admit/r0034001002001001001/SCA01`` after `admit` and
    ``difference/r0034001002001001001/SCA01`` after `difference`, derived
    from the manifest register reads -- ``manifest.stage`` (the producing
    stage) and ``manifest.unit.id`` (the producing unit) name both.
    `register` is one stage that follows every producer, so this is the
    single place that derivation happens; no occurrence counter, no new
    column, and no hand-keyed suffix (the old ``<unit>/difference``
    pattern a caller used to add by hand for a second `register` in one
    run) is needed -- two different producing stages already yield two
    distinct derived ids for the same nominal unit.
    """
    return f"{manifest.stage}/{manifest.unit.id}"


def hash_file(path: str | Path) -> tuple[int, str]:
    """Return ``(byte_size, sha256_hex)`` for the file at ``path``.

    Reads in fixed-size chunks so an arbitrarily large product file is
    hashed without loading it whole into memory. ``sha256_hex`` has no
    ``sha256:`` prefix; use :func:`member_for_file` to build a
    manifest-ready :class:`Member`.
    """
    p = Path(path)
    digest = hashlib.sha256()
    size = 0
    with p.open("rb") as fh:
        while True:
            chunk = fh.read(_READ_CHUNK)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def member_for_file(role: str, path: str | Path, *, relative_to: str | Path) -> Member:
    """Build a :class:`Member` for a local file, computing its size and hash.

    ``path`` is the file to hash; the member's ``path`` field is recorded
    relative to ``relative_to`` (the attempt's output location), matching
    what :meth:`Member.validate` requires.
    """
    byte_size, sha256_hex = hash_file(path)
    rel = os.path.relpath(str(path), start=str(relative_to))
    rel = rel.replace(os.sep, "/")
    return Member(role=role, path=rel, bytes=byte_size, sha256=f"sha256:{sha256_hex}")
