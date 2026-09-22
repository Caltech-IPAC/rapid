"""The `l2-image` registration block, as code.

The products page's "Registration metadata" section fixes the l2 image's
field list -- everything `register` needs to write `l2files` and
`l2filemeta` rows without reading the product's FITS file. `admit` builds
one of these and stores it as an `OutputEntry.registration` dict;
`register` (built next) reads it back with :meth:`L2ImageRegistration.from_dict`
rather than guessing the shape from `admit`'s source.

This module imports nothing from `rapidpipe` outside `rapidpipe.products`,
matching the products page's own dependency rule.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, fields
from typing import Any

_MD5_RE = re.compile(r"^[0-9a-f]{32}$")


class L2ImageRegistrationError(ValueError):
    """An l2-image registration block failed validation or decoding."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise L2ImageRegistrationError(message)


@dataclass(frozen=True)
class L2ImageRegistration:
    """Everything `register` needs to write one `l2files`/`l2filemeta` row pair.

    Field order and names match the products page's l2-image table:
    header values as delivered, the WCS as delivered (including the SIP
    polynomial, carried as sparse ``{"i_j": coefficient}`` dicts rather
    than one field per legacy `a_i_j` column, since the order varies),
    the sky centre and four corners `admit` derives from that WCS, the
    legacy MD5 checksum, which HDU the header came from, and the
    (never-registered) delivery instance this admission was built from.

    Optional fields (`pa_obsy`, `pa_fpa`, `zptmag`, `skymean`, the SIP
    block, `delivery_source`) are `None` or empty when the delivered
    header does not carry them -- the products page's rule that "nothing
    substitutes zero for an unavailable measurement" applies here, not
    just to `register`.
    """

    exposure_id: str
    detector: int
    delivered_version: str
    filter: str
    dateobs: str
    mjdobs: float
    exptime: float
    infobits: int
    status: int
    naxis1: int
    naxis2: int
    crval1: float
    crval2: float
    crpix1: float
    crpix2: float
    cd11: float
    cd12: float
    cd21: float
    cd22: float
    ctype1: str
    ctype2: str
    cunit1: str
    cunit2: str
    equinox: float
    a_order: int | None
    a: dict[str, float]
    b_order: int | None
    b: dict[str, float]
    ra_targ: float
    dec_targ: float
    pa_obsy: float | None
    pa_fpa: float | None
    zptmag: float | None
    skymean: float | None
    centre: dict[str, float]
    corners: list[list[float]]
    md5: str
    hdu: int
    delivery_instance: str
    delivery_source: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "L2ImageRegistration":
        known = {f.name for f in fields(cls)}
        unknown = set(d) - known
        if unknown:
            raise L2ImageRegistrationError(
                f"l2-image registration has unknown fields: {sorted(unknown)}")
        missing = known - set(d)
        if missing:
            raise L2ImageRegistrationError(
                f"l2-image registration is missing fields: {sorted(missing)}")
        return cls(**d)

    def validate(self) -> None:
        _require(bool(self.exposure_id), "exposure_id must be non-empty")
        _require(bool(self.delivered_version), "delivered_version must be non-empty")
        _require(bool(self.filter), "filter must be non-empty")
        _require(bool(self.dateobs), "dateobs must be non-empty")
        _require(self.exptime >= 0, f"exptime must be non-negative, got {self.exptime!r}")
        _require(
            self.status in (0, 1),
            f"status must be 0 or 1, got {self.status!r}")
        _require(
            self.md5 is not None and _MD5_RE.match(self.md5) is not None,
            f"md5 must be 32 lowercase hex characters, got {self.md5!r}")
        _require(
            isinstance(self.equinox, (int, float)) and not isinstance(self.equinox, bool),
            f"equinox must be numeric, got {self.equinox!r}")

        for label, ra, dec in (
            ("ra_targ/dec_targ", self.ra_targ, self.dec_targ),
            ("centre", self.centre.get("ra"), self.centre.get("dec")),
        ):
            _require(
                ra is not None and 0.0 <= ra < 360.0,
                f"{label} ra must be in [0, 360), got {ra!r}")
            _require(
                dec is not None and -90.0 <= dec <= 90.0,
                f"{label} dec must be in [-90, 90], got {dec!r}")

        _require(
            len(self.corners) == 4,
            f"corners must have exactly four [ra, dec] pairs, got {len(self.corners)}")
        for corner in self.corners:
            _require(
                len(corner) == 2,
                f"each corner must be a [ra, dec] pair, got {corner!r}")
            ra, dec = corner
            _require(
                0.0 <= ra < 360.0,
                f"corner ra must be in [0, 360), got {ra!r}")
            _require(
                -90.0 <= dec <= 90.0,
                f"corner dec must be in [-90, 90], got {dec!r}")
