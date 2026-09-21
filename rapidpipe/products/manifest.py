"""The completion manifest: dataclasses, JSON I/O, and validation.

A stage publishes this manifest only after its outputs are complete (stage
contract, "The manifest"). It records: the manifest's own schema version;
the run, unit, stage and attempt identifiers; a reference to the execution
record kept by ``rapidpipe.runs``; a reference to the input manifest the
stage read and to any database result sets it read; and each output's
identity, kind, format version and location, with byte size and SHA-256 for
file outputs.

This module imports only the standard library and ``rapidpipe.products.ids``:
no ``rapidpipe.runs``, ``rapidpipe.db`` or stage module.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

#: The schema version this module reads and writes. Bumped when the
#: manifest's field set or semantics change.
SCHEMA_VERSION = 1

_READ_CHUNK = 1024 * 1024


class ManifestError(ValueError):
    """A manifest failed validation."""


@dataclass(frozen=True)
class OutputEntry:
    """One product a stage wrote.

    ``byte_size`` and ``sha256`` are required for file outputs (``location``
    a path or S3 key) and absent for a database result set (``location``
    identifies the result set some other way, e.g. a table/run-scoped name);
    ``validate`` enforces that pairing.
    """

    identity: str
    kind: str
    format_version: str
    location: str
    byte_size: int | None = None
    sha256: str | None = None

    def is_file(self) -> bool:
        return self.byte_size is not None or self.sha256 is not None

    def validate(self) -> None:
        for attr in ("identity", "kind", "format_version", "location"):
            if not getattr(self, attr):
                raise ManifestError(f"output entry missing required field {attr!r}")
        if self.is_file():
            if self.byte_size is None or self.byte_size < 0:
                raise ManifestError(
                    f"output {self.identity!r} has sha256 but no valid byte_size")
            if not self.sha256:
                raise ManifestError(
                    f"output {self.identity!r} has byte_size but no sha256")

    def to_dict(self) -> dict[str, Any]:
        d = {
            "identity": self.identity,
            "kind": self.kind,
            "format_version": self.format_version,
            "location": self.location,
        }
        if self.byte_size is not None:
            d["byte_size"] = self.byte_size
        if self.sha256 is not None:
            d["sha256"] = self.sha256
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "OutputEntry":
        known = {f.name for f in fields(cls)}
        unknown = set(d) - known
        if unknown:
            raise ManifestError(f"output entry has unknown fields: {sorted(unknown)}")
        return cls(**d)


@dataclass(frozen=True)
class CompletionManifest:
    """A stage's record of what it read and what it wrote.

    ``db_result_sets_read`` lists the named, completed database result sets
    (per the contract, for ``crossmatch``, ``statistics`` and ``prune``)
    this attempt read; transform stages that declare no database access
    leave it empty.
    """

    run_id: str
    unit_id: str
    stage: str
    attempt_id: str
    execution_record_ref: str
    input_manifest_ref: str
    outputs: tuple[OutputEntry, ...] = field(default_factory=tuple)
    db_result_sets_read: tuple[str, ...] = field(default_factory=tuple)
    schema_version: int = SCHEMA_VERSION

    def validate(self) -> None:
        required = {
            "run_id": self.run_id,
            "unit_id": self.unit_id,
            "stage": self.stage,
            "attempt_id": self.attempt_id,
            "execution_record_ref": self.execution_record_ref,
            "input_manifest_ref": self.input_manifest_ref,
        }
        for name, value in required.items():
            if not value:
                raise ManifestError(f"manifest missing required field {name!r}")
        if self.schema_version != SCHEMA_VERSION:
            raise ManifestError(
                f"unsupported manifest schema_version {self.schema_version!r}; "
                f"expected {SCHEMA_VERSION!r}")

        seen_identities: set[str] = set()
        for output in self.outputs:
            output.validate()
            if output.identity in seen_identities:
                raise ManifestError(
                    f"duplicate output identity {output.identity!r}")
            seen_identities.add(output.identity)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "unit_id": self.unit_id,
            "stage": self.stage,
            "attempt_id": self.attempt_id,
            "execution_record_ref": self.execution_record_ref,
            "input_manifest_ref": self.input_manifest_ref,
            "db_result_sets_read": list(self.db_result_sets_read),
            "outputs": [o.to_dict() for o in self.outputs],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CompletionManifest":
        d = dict(d)
        outputs = tuple(OutputEntry.from_dict(o) for o in d.pop("outputs", []))
        db_result_sets_read = tuple(d.pop("db_result_sets_read", []))
        known = {f.name for f in fields(cls)} - {"outputs", "db_result_sets_read"}
        unknown = set(d) - known
        if unknown:
            raise ManifestError(f"manifest has unknown fields: {sorted(unknown)}")
        return cls(outputs=outputs, db_result_sets_read=db_result_sets_read, **d)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"

    def write(self, path: str | Path) -> None:
        """Validate, then write this manifest as JSON to ``path``."""
        self.validate()
        Path(path).write_text(self.to_json())

    @classmethod
    def read(cls, path: str | Path) -> "CompletionManifest":
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


def hash_file(path: str | Path) -> tuple[int, str]:
    """Return ``(byte_size, sha256_hex)`` for the file at ``path``.

    Reads in fixed-size chunks so an arbitrarily large product file is
    hashed without loading it whole into memory.
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


def output_entry_for_file(
    identity: str,
    kind: str,
    format_version: str,
    path: str | Path,
) -> OutputEntry:
    """Build an :class:`OutputEntry` for a local file, computing its hash."""
    byte_size, sha256 = hash_file(path)
    return OutputEntry(
        identity=identity,
        kind=kind,
        format_version=format_version,
        location=str(path),
        byte_size=byte_size,
        sha256=sha256,
    )
