"""The `difference-image` registration block, as code.

The products page's "Registration metadata" section fixes the difference
image's field list -- everything `register` needs to write `diffimages`
and `diffimmeta` rows without reading the product's FITS files -- and its
"File products" section fixes the bundle's roles, declared per
differencer. `difference` builds one :class:`DifferenceImageRegistration`
per registered differencer and stores it as an `OutputEntry.registration`
dict; `register` reads it back with
:meth:`DifferenceImageRegistration.from_dict` and checks the entry as a
whole with :func:`validate_difference_entry`, mirroring
`rapidpipe.products.l2image` for the l2 image.

Rulings this module encodes (lead, 2026-09-22, recorded on the products
page):

- Bundle roles are declared per differencer. `difference` and
  `uncertainty` always; `significance` where the differencer produces one
  (ZOGY yes, SFFT no); `psf` optional for any differencer, `kernel`
  optional for SFFT. A declared, required role missing from a present
  instance fails registration.
- The detection image is a per-differencer setting recorded with the
  instance: `significance` for ZOGY, `difference` for SFFT.
- `catalog_outcome_bits` is `dev`'s six-bit mask (one bit per differencer
  and sign, set when no Photutils catalog was produced), registered as
  `diffimages.infobitssci`. The block also carries `infobits_science`,
  the l2 image's own quality bits, which is not registered.

This module imports nothing from `rapidpipe` outside `rapidpipe.products`,
matching the products page's own dependency rule.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, fields
from typing import Any, Iterable, Mapping

_MD5_RE = re.compile(r"^[0-9a-f]{32}$")
_SETTINGS_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class DifferenceImageRegistrationError(ValueError):
    """A difference-image registration block or entry failed validation."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DifferenceImageRegistrationError(message)


@dataclass(frozen=True)
class DifferencerRoles:
    """The bundle roles one differencer declares.

    ``required`` roles must be present in every registered instance of
    this differencer; ``optional`` roles may be absent. A member whose
    role is in neither list is not part of this differencer's bundle.
    ``default_detection_role`` is the member the catalog stage detects on
    unless the stage setting says otherwise.
    """

    required: tuple[str, ...]
    optional: tuple[str, ...]
    default_detection_role: str

    @property
    def declared(self) -> tuple[str, ...]:
        return self.required + self.optional


#: Per-differencer bundle roles (products page, "File products"). The
#: naive subtraction is not here: it is an optional diagnostic file, never
#: a registered instance.
DIFFERENCERS: dict[str, DifferencerRoles] = {
    "zogy": DifferencerRoles(
        required=("difference", "uncertainty", "significance"),
        optional=("psf",),
        default_detection_role="significance",
    ),
    "sfft": DifferencerRoles(
        required=("difference", "uncertainty"),
        optional=("psf", "kernel"),
        default_detection_role="difference",
    ),
}

#: The source-catalog families the difference stage writes, both retained
#: (lead, 2026-09-22), and the two signs of each.
CATALOG_TYPES = ("sextractor", "photutils")
SIGNS = ("positive", "negative")

#: `dev`'s catalog-outcome bits, kept (products page, "`infobitssci`'s six
#: bits"): one bit per differencer and sign, set when no Photutils catalog
#: was produced for that difference image and sign.
CATALOG_OUTCOME_BITS: dict[tuple[str, str], int] = {
    ("zogy", "positive"): 0,
    ("zogy", "negative"): 1,
    ("sfft", "positive"): 2,
    ("sfft", "negative"): 3,
    ("naive", "positive"): 4,
    ("naive", "negative"): 5,
}

_MAX_CATALOG_OUTCOME_BITS = (1 << len(CATALOG_OUTCOME_BITS)) - 1


def catalog_outcome_bit(differencer: str, sign: str) -> int:
    """The mask value (``2**bit``) for no Photutils catalog on (differencer, sign)."""
    try:
        return 1 << CATALOG_OUTCOME_BITS[(differencer, sign)]
    except KeyError as exc:
        raise DifferenceImageRegistrationError(
            f"no catalog-outcome bit for differencer {differencer!r}, "
            f"sign {sign!r}") from exc


def roles_for(differencer: str) -> DifferencerRoles:
    try:
        return DIFFERENCERS[differencer]
    except KeyError as exc:
        raise DifferenceImageRegistrationError(
            f"unknown differencer {differencer!r}; known: {sorted(DIFFERENCERS)}"
        ) from exc


def check_bundle_roles(differencer: str, member_roles: Iterable[str]) -> None:
    """Refuse a bundle whose roles do not match the differencer's declaration.

    A required role missing is an error (products page: "A role a
    differencer declares but does not deliver fails registration"); so is
    a role the differencer does not declare at all, or a role that appears
    twice.
    """
    declaration = roles_for(differencer)
    roles = list(member_roles)
    duplicates = sorted({r for r in roles if roles.count(r) > 1})
    _require(not duplicates, f"{differencer} bundle repeats roles {duplicates}")
    missing = [r for r in declaration.required if r not in roles]
    _require(
        not missing,
        f"{differencer} bundle is missing declared role(s) {missing}")
    unknown = sorted(set(roles) - set(declaration.declared))
    _require(
        not unknown,
        f"{differencer} bundle has role(s) {unknown} it does not declare; "
        f"declared: {list(declaration.declared)}")


def _is_number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class DifferenceImageRegistration:
    """Everything `register` needs to write one `diffimages`/`diffimmeta` pair.

    Field names follow the products page's difference-image table and its
    complete-manifest example:

    - ``centre``/``corners``: the image centre and four corners (RA, Dec),
      in `dev`'s corner order (bottom-left, bottom-right, top-right,
      top-left). `dev` registers the science image's own centre and
      corners here, correct because the reference is resampled onto the
      science grid; the difference stage carries the l2 instance's values.
    - ``catalog_outcome_bits``: the six-bit mask, registered as
      ``infobitssci``.
    - ``infobits_science``: the l2 image's quality bits; carried, not
      registered.
    - ``infobits_reference``: the reference instance's info bits,
      registered as ``infobitsref``.
    - ``source_counts``: ``{catalog_type: {sign: count}}``. A count is
      ``None`` where that catalog was not produced (the mask says why),
      never zero in its place.
    - ``registration_residual``: ``x_rms``/``y_rms`` are ZOGY's astrometric
      uncertainty inputs (the stage setting, 0.0 by default, reproducing
      `dev`); ``x_median``/``y_median`` are the measured median offsets
      the stage applied to the reference.
    - ``reference_scale_factor``: `dev`'s ``scalefacref``.
    - ``detection_role``: the member the catalogs were detected on.
    - ``md5``: the legacy MD5 of the primary member (products page,
      "Legacy checksum columns keep their MD5").
    - ``reference_rfid``: the legacy `refimages.rfid` of the reference, for
      a reference registered by `dev` with no instance row; ``None`` when
      the reference instance is registered by the rebuild.
    """

    detection_role: str
    centre: dict[str, float]
    corners: list[list[float]]
    catalog_outcome_bits: int
    infobits_science: int
    infobits_reference: int
    source_counts: dict[str, dict[str, int | None]]
    registration_residual: dict[str, float]
    reference_scale_factor: float
    md5: str
    reference_rfid: int | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "DifferenceImageRegistration":
        known = {f.name for f in fields(cls)}
        unknown = set(d) - known
        if unknown:
            raise DifferenceImageRegistrationError(
                f"difference-image registration has unknown fields: {sorted(unknown)}")
        missing = known - set(d)
        if missing:
            raise DifferenceImageRegistrationError(
                f"difference-image registration is missing fields: {sorted(missing)}")
        return cls(**d)

    def validate(self, differencer: str) -> None:
        declaration = roles_for(differencer)
        _require(
            self.detection_role in declaration.declared,
            f"detection_role {self.detection_role!r} is not a {differencer} role")

        ra, dec = self.centre.get("ra"), self.centre.get("dec")
        _require(
            _is_number(ra) and 0.0 <= ra < 360.0,
            f"centre ra must be in [0, 360), got {ra!r}")
        _require(
            _is_number(dec) and -90.0 <= dec <= 90.0,
            f"centre dec must be in [-90, 90], got {dec!r}")
        _require(
            len(self.corners) == 4,
            f"corners must have exactly four [ra, dec] pairs, got {len(self.corners)}")
        for corner in self.corners:
            _require(len(corner) == 2, f"each corner must be [ra, dec], got {corner!r}")
            ra, dec = corner
            _require(
                _is_number(ra) and 0.0 <= ra < 360.0,
                f"corner ra must be in [0, 360), got {ra!r}")
            _require(
                _is_number(dec) and -90.0 <= dec <= 90.0,
                f"corner dec must be in [-90, 90], got {dec!r}")

        _require(
            _is_int(self.catalog_outcome_bits)
            and 0 <= self.catalog_outcome_bits <= _MAX_CATALOG_OUTCOME_BITS,
            f"catalog_outcome_bits must be an integer in [0, "
            f"{_MAX_CATALOG_OUTCOME_BITS}], got {self.catalog_outcome_bits!r}")
        _require(
            _is_int(self.infobits_science) and self.infobits_science >= 0,
            f"infobits_science must be a non-negative integer, got {self.infobits_science!r}")
        _require(
            _is_int(self.infobits_reference) and self.infobits_reference >= 0,
            f"infobits_reference must be a non-negative integer, "
            f"got {self.infobits_reference!r}")

        _require(
            set(self.source_counts) == set(CATALOG_TYPES),
            f"source_counts must name exactly {list(CATALOG_TYPES)}, "
            f"got {sorted(self.source_counts)}")
        for catalog_type in CATALOG_TYPES:
            counts = self.source_counts[catalog_type]
            _require(
                isinstance(counts, dict) and set(counts) == set(SIGNS),
                f"source_counts[{catalog_type!r}] must name exactly {list(SIGNS)}")
            for sign in SIGNS:
                count = counts[sign]
                _require(
                    count is None or (_is_int(count) and count >= 0),
                    f"source_counts[{catalog_type!r}][{sign!r}] must be a "
                    f"non-negative integer or null, got {count!r}")
        # The SExtractor positive count fills the NOT NULL legacy column
        # diffimmeta.nsexcatsources; it is always measured.
        _require(
            self.source_counts["sextractor"]["positive"] is not None,
            "source_counts['sextractor']['positive'] is required "
            "(diffimmeta.nsexcatsources)")

        residual_keys = ("x_rms", "y_rms", "x_median", "y_median")
        _require(
            set(self.registration_residual) == set(residual_keys),
            f"registration_residual must name exactly {list(residual_keys)}")
        for key in residual_keys:
            _require(
                _is_number(self.registration_residual[key]),
                f"registration_residual[{key!r}] must be a finite number, "
                f"got {self.registration_residual[key]!r}")

        _require(
            _is_number(self.reference_scale_factor),
            f"reference_scale_factor must be a finite number, "
            f"got {self.reference_scale_factor!r}")
        _require(
            isinstance(self.md5, str) and _MD5_RE.match(self.md5) is not None,
            f"md5 must be 32 lowercase hex characters, got {self.md5!r}")
        _require(
            self.reference_rfid is None
            or (_is_int(self.reference_rfid) and self.reference_rfid > 0),
            f"reference_rfid must be a positive integer or null, got {self.reference_rfid!r}")


_DIFFERENCE_KEY_FIELDS = ("l2", "reference", "differencer", "settings_hash")


def validate_difference_entry(entry: Mapping[str, Any]) -> DifferenceImageRegistration:
    """Check one `difference-image` manifest entry (as a dict); return its block.

    Checks the logical key's four fields, the bundle's roles against the
    differencer's declaration, that the primary member is the
    ``difference`` role, and the registration block itself.
    """
    _require(entry.get("kind") == "difference-image",
             f"entry kind is {entry.get('kind')!r}, expected 'difference-image'")
    key = entry.get("key") or {}
    for key_field in _DIFFERENCE_KEY_FIELDS:
        _require(
            isinstance(key.get(key_field), str) and bool(key.get(key_field)),
            f"difference-image key is missing string field {key_field!r}")
    _require(
        _SETTINGS_HASH_RE.match(key["settings_hash"]) is not None,
        f"settings_hash must be 'sha256:' and 64 hex characters, "
        f"got {key['settings_hash']!r}")
    differencer = key["differencer"]
    members = entry.get("members") or []
    check_bundle_roles(differencer, [m.get("role") for m in members])
    primary = next((m for m in members if m.get("path") == entry.get("primary")), None)
    _require(
        primary is not None and primary.get("role") == "difference",
        "the primary member of a difference-image bundle must be its "
        "'difference' role")
    registration = DifferenceImageRegistration.from_dict(entry.get("registration") or {})
    registration.validate(differencer)
    _require(
        registration.detection_role in {m.get("role") for m in members},
        f"detection_role {registration.detection_role!r} is not a member of "
        "this bundle")
    return registration


_SOURCE_CATALOG_KEY_FIELDS = ("difference", "catalog_type", "sign")


def validate_source_catalog_entry(entry: Mapping[str, Any]) -> None:
    """Check one `source-catalog` manifest entry (as a dict).

    The products page records no rows for this kind ("none until `load`");
    `register` accepts a valid entry and writes nothing for it beyond its
    instance row.
    """
    _require(entry.get("kind") == "source-catalog",
             f"entry kind is {entry.get('kind')!r}, expected 'source-catalog'")
    key = entry.get("key") or {}
    for key_field in _SOURCE_CATALOG_KEY_FIELDS:
        _require(
            isinstance(key.get(key_field), str) and bool(key.get(key_field)),
            f"source-catalog key is missing string field {key_field!r}")
    _require(key["catalog_type"] in CATALOG_TYPES,
             f"unknown catalog_type {key['catalog_type']!r}; known: {list(CATALOG_TYPES)}")
    _require(key["sign"] in SIGNS, f"unknown sign {key['sign']!r}; known: {list(SIGNS)}")
    members = entry.get("members") or []
    primary = next((m for m in members if m.get("path") == entry.get("primary")), None)
    _require(
        primary is not None and primary.get("role") == "catalog",
        "the primary member of a source-catalog entry must be its 'catalog' role")
    registration = entry.get("registration") or {}
    _require(set(registration) == {"source_count"},
             f"source-catalog registration must name exactly ['source_count'], "
             f"got {sorted(registration)}")
    count = registration["source_count"]
    _require(_is_int(count) and count >= 0,
             f"source_count must be a non-negative integer, got {count!r}")
