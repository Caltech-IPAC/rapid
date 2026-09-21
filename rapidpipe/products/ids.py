"""Product identifiers and the four unit-of-work kinds.

The stage contract names four units of work: an exposure, a detector image,
a field, and a processing date. This module gives that list a stable type
and a small identifier wrapper for product and unit identities. It imports
only the standard library.
"""

from __future__ import annotations

from enum import Enum


class UnitKind(str, Enum):
    """The four units of work a stage may declare."""

    EXPOSURE = "exposure"
    DETECTOR_IMAGE = "detector-image"
    FIELD = "field"
    PROCESSING_DATE = "processing-date"


class Identifier(str):
    """A product or unit identifier.

    A thin string subclass rather than a bare ``str`` alias: it lets type
    signatures in ``manifest.py`` and callers elsewhere say ``Identifier``
    instead of ``str`` for values that name a run, unit, stage, attempt or
    product, without imposing a parsed structure this PR does not define.
    Identifier vocabulary (exposure, detector image, field, processing date)
    is specification work tracked separately; this type only marks the role.
    """

    __slots__ = ()

    def __new__(cls, value: str) -> "Identifier":
        if not value:
            raise ValueError("Identifier must be a non-empty string")
        return super().__new__(cls, value)
