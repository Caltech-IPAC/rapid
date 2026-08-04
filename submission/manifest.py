"""
File:    manifest.py

The submission manifest: the binding from array index to SCA identity.

Why a manifest exists at all. Batch gives an array child exactly one
thing to identify itself with — ``AWS_BATCH_JOB_ARRAY_INDEX``, an integer
in ``[0, size)``. The work the child must do is a per-SCA processing unit.
Something has to carry the mapping between the two, and design/compute.md
§ Submission names it: "The child's array index binds to its SCA identity
through the submission manifest the orchestrator builds to size the
array."

Two properties follow from that sentence and are enforced here.

The manifest sizes the array, so the manifest is authoritative: the array
size is ``len(manifest)``, never a separately-tracked number that could
drift from it.

The binding is immutable once submitted. A retried child keeps its
array-index identity (design/compute.md § Retry), so the same index must
resolve to the same SCA on the retry as on the first attempt. The
manifest is therefore built once, written once, and read — never rebuilt
from a re-query of ready work, which could return a different ordering.
"""

import dataclasses
import hashlib
import json
from typing import Any, Iterable, Iterator

# Batch's hard ceiling on array children (design/compute.md § Submission).
MAX_ARRAY_SIZE = 10000

# An array job needs at least two children; Batch rejects size 1. A
# one-unit batch is submitted as a plain (non-array) job instead — see
# submit.py, which is where that distinction is acted on.
MIN_ARRAY_SIZE = 2


@dataclasses.dataclass(frozen=True)
class ProcessingUnit:
    """One per-SCA unit of work — what a single array child processes.

    Frozen because a unit's identity is fixed the moment it enters a
    manifest: the retry contract binds an array index to an SCA identity
    for the life of the batch, and a mutable record would let that
    binding be edited out from under a retried child.

    Attributes
    ----------
    exposure : int
        Roman exposure identifier.
    sca : int
        Sensor chip assembly number within the exposure.
    fields : dict
        Any additional per-unit values the job needs (field id, filter,
        product paths). Kept open rather than enumerated: the manifest's
        job is the index binding, not a schema for pipeline inputs.
    """

    exposure: int
    sca: int
    fields: dict[str, Any] = dataclasses.field(default_factory=dict)

    @property
    def key(self) -> str:
        """Stable string identity for this unit, for dedup and logging."""
        return f"{self.exposure}/{self.sca}"

    def to_dict(self) -> dict[str, Any]:
        return {"exposure": self.exposure, "sca": self.sca,
                **({"fields": self.fields} if self.fields else {})}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ProcessingUnit":
        return cls(exposure=int(raw["exposure"]), sca=int(raw["sca"]),
                   fields=raw.get("fields", {}))


class Manifest:
    """An ordered index -> ProcessingUnit binding for one submission.

    Construction fixes the order; the index of a unit is its position.
    """

    SCHEMA_VERSION = 1

    def __init__(self, units: Iterable[ProcessingUnit],
                 batch_id: str | None = None):
        self.units: tuple[ProcessingUnit, ...] = tuple(units)
        self.batch_id = batch_id
        if not self.units:
            raise ValueError("a manifest needs at least one processing unit")
        if len(self.units) > MAX_ARRAY_SIZE:
            raise ValueError(
                f"{len(self.units)} units exceeds Batch's {MAX_ARRAY_SIZE}-child "
                "array ceiling; the batcher must cut smaller batches")
        duplicates = self._duplicate_keys()
        if duplicates:
            # Two children processing the same SCA would write the same
            # products from two attempts with different identities.
            raise ValueError(
                "duplicate processing units in one manifest: "
                + ", ".join(sorted(duplicates)))

    def _duplicate_keys(self) -> set[str]:
        seen: set[str] = set()
        dupes: set[str] = set()
        for unit in self.units:
            if unit.key in seen:
                dupes.add(unit.key)
            seen.add(unit.key)
        return dupes

    def __len__(self) -> int:
        return len(self.units)

    def __iter__(self) -> Iterator[ProcessingUnit]:
        return iter(self.units)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Manifest):
            return NotImplemented
        return self.units == other.units and self.batch_id == other.batch_id

    @property
    def array_size(self) -> int:
        """The array size this manifest implies.

        The manifest sizes the array — this property is the only place
        the number comes from.
        """
        return len(self.units)

    @property
    def is_array(self) -> bool:
        """Whether this batch submits as an array job.

        Batch rejects an array of size 1, so a single-unit batch goes out
        as a plain job. The manifest is still built and still binds index
        0 to that unit, so the startup path is identical either way.
        """
        return len(self.units) >= MIN_ARRAY_SIZE

    def unit_for_index(self, index: int) -> ProcessingUnit:
        """Resolve one array child's own processing unit.

        Parameters
        ----------
        index : int
            The child's ``AWS_BATCH_JOB_ARRAY_INDEX``.

        Raises
        ------
        IndexError
            If the index falls outside the manifest. This is a real fault
            — a child of an array sized by a different manifest — and
            surfaces rather than silently processing the wrong SCA.
        """
        if not 0 <= index < len(self.units):
            raise IndexError(
                f"array index {index} is outside manifest of "
                f"{len(self.units)} units; the job's array was sized by a "
                "different manifest")
        return self.units[index]

    def to_dict(self) -> dict[str, Any]:
        """Serializable form, written for the jobs to read back."""
        return {
            "schema_version": self.SCHEMA_VERSION,
            "batch_id": self.batch_id,
            "array_size": self.array_size,
            "units": [unit.to_dict() for unit in self.units],
        }

    def to_json(self, indent: int | None = None) -> str:
        # sort_keys so the serialized form is byte-stable: the manifest is
        # checksummed and compared across the submit/startup boundary.
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Manifest":
        version = raw.get("schema_version")
        if version != cls.SCHEMA_VERSION:
            raise ValueError(
                f"manifest schema_version {version!r} is not "
                f"{cls.SCHEMA_VERSION}; refusing to guess the layout")
        manifest = cls((ProcessingUnit.from_dict(u) for u in raw["units"]),
                       batch_id=raw.get("batch_id"))
        # The recorded size is checked against the reconstructed one: a
        # mismatch means the array was sized from something other than
        # the units actually listed.
        recorded = raw.get("array_size")
        if recorded is not None and recorded != manifest.array_size:
            raise ValueError(
                f"manifest records array_size {recorded} but carries "
                f"{manifest.array_size} units")
        return manifest

    @classmethod
    def from_json(cls, text: str) -> "Manifest":
        return cls.from_dict(json.loads(text))

    def checksum(self) -> str:
        """Content hash of the manifest, for submit/startup agreement.

        Lets a starting job prove it read the manifest the submitter
        wrote, rather than a truncated or superseded copy.
        """
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()
