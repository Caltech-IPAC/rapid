"""The `psf` registration block, as code.

The products page's "File products" table: a `psf` is a detector-image
product, logical key (filter, detector, version), a FITS file, made by
`admit`, landing in `psfs`. Its field list was not fixed on the products
page; this block carries exactly what `dev`'s ``add_psf``
(``database/modules/utils/rapid_db.py``, the ``addPSF`` stored function)
takes that registration cannot derive:

- ``md5``: the file's MD5, for the legacy ``psfs.checksum`` column
  (products page, "Legacy checksum columns keep their MD5").
- ``status``: `dev`'s ``psfs.status`` -- 1 when the maker verified the
  file (the DATASUM/CHECKSUM keywords, as `admit` sets ``l2files.status``),
  else 0.

The filter and detector come from the logical key; ``fid`` is looked up in
`filters`, ``sca`` is the detector; ``version`` is allocated as ``addPSF``
allocates it and ``vbest`` is 0 (never current at registration). Nothing
on `rebuild` emits a `psf` entry yet -- `admit` does not today, and the
difference stage's input-set `psf` entries are keyed ``applies_to`` and
consumed, never registered -- so this block is designed in and unused.

This module imports nothing from `rapidpipe` outside `rapidpipe.products`,
matching the products page's own dependency rule.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, fields
from typing import Any, Mapping

_MD5_RE = re.compile(r"^[0-9a-f]{32}$")

PSF_KEY_FIELDS = ("filter", "detector", "version")


class PsfRegistrationError(ValueError):
    """A psf registration block or entry failed validation."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PsfRegistrationError(message)


@dataclass(frozen=True)
class PsfRegistration:
    """Everything `register` needs, beyond the key, to write one `psfs` row."""

    md5: str
    status: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "PsfRegistration":
        known = {f.name for f in fields(cls)}
        unknown = set(d) - known
        if unknown:
            raise PsfRegistrationError(f"psf registration has unknown fields: {sorted(unknown)}")
        missing = known - set(d)
        if missing:
            raise PsfRegistrationError(f"psf registration is missing fields: {sorted(missing)}")
        return cls(**d)

    def validate(self) -> None:
        _require(isinstance(self.md5, str) and _MD5_RE.match(self.md5) is not None,
                 f"md5 must be 32 lowercase hex characters, got {self.md5!r}")
        _require(self.status in (0, 1) and not isinstance(self.status, bool),
                 f"status must be 0 or 1, got {self.status!r}")


def psf_detector(key: Mapping[str, Any]) -> int:
    """The key's detector as an SCA number (a digit string or an integer, 1..18)."""
    detector = key.get("detector")
    if isinstance(detector, str) and detector.isdigit():
        detector = int(detector)
    _require(isinstance(detector, int) and not isinstance(detector, bool)
             and 1 <= detector <= 18,
             f"psf key detector must be an SCA number 1..18, got {key.get('detector')!r}")
    return detector


def validate_psf_entry(entry: Mapping[str, Any]) -> PsfRegistration:
    """Check one `psf` manifest entry (as a dict); return its block.

    The key names a filter, a detector and a version; the entry has one
    member, role ``psf``, which is its primary.
    """
    _require(entry.get("kind") == "psf", f"entry kind is {entry.get('kind')!r}, expected 'psf'")
    key = entry.get("key") or {}
    _require(set(key) == set(PSF_KEY_FIELDS),
             f"psf key must name exactly {list(PSF_KEY_FIELDS)}, got {sorted(key)}")
    _require(isinstance(key["filter"], str) and bool(key["filter"]),
             "psf key filter must be a non-empty string")
    psf_detector(key)
    _require(isinstance(key["version"], str) and bool(key["version"]),
             "psf key version must be a non-empty string")
    members = entry.get("members") or []
    _require(len(members) == 1 and members[0].get("role") == "psf"
             and members[0].get("path") == entry.get("primary"),
             "a psf entry has exactly one member, role 'psf', and it is the primary")
    registration = PsfRegistration.from_dict(entry.get("registration") or {})
    registration.validate()
    return registration
