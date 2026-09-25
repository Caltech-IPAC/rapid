"""The `reference-image` and `reference-catalog` registration blocks, as code.

The products page's "File products" table: a `reference-image` is a field
product, logical key (field, filter, recipe, version), a bundle of three
FITS files (image, coverage, uncertainty) made by `reference`, landing in
`refimages`, `refimmeta` and `refimimages`; a `reference-catalog` is a
field product, logical key (reference instance, catalog type), one text
file made by `reference`, landing in `refimcatalogs`. The two field lists
were fixed by the supervisor's step-8 ruling R6 (2026-09-24), as the
difference image's was:

- `reference-image`: ``md5`` of the primary member, ``status``,
  ``infobits``, ``field`` (the Roman tessellation index), ``filter``,
  ``ra_center``/``dec_center`` (the mosaic centre, from which `register`
  derives ``hp6``/``hp9``), ``constituents`` (the ordered `l2-image`
  instance ids coadded), and the measurements `dev` writes to `refimmeta`
  under `dev`'s own names (``nframes`` ... ``npucatsources``), plus the
  frame-range and zero-point values the header carries and
  ``settings_hash``. The SExtractor count keeps `dev`'s block spelling
  ``nsexcatsources`` (``registerCompletedJobsInDB.py``'s
  ``[REF_IMAGE] nsexcatsources``), written to the column
  ``refimmeta.nsxcatsources``; ``npucatsources`` is null when the
  Photutils reference catalog is off (``[psfcat] enabled = false``, the
  default), and `refimmeta` stores that null
  (20260924-09-refimmeta-npucatsources-nullable.sql).
- ``filter`` may use either spelling of a filter name, the RAPID one the
  `filters` table seeds (``W146``) or the Roman one (``F146``);
  :func:`rapid_filter_name` normalises it, as `dev`'s
  ``roman_to_rapid_filter_names`` does.
- `reference-catalog`: ``md5``, ``status``, ``catalog_type`` (``sextractor``
  or ``psf``; the legacy ``refimcatalogs.cattype`` 1 or 2) and
  ``source_count``.

`dev` lineage: `dev` builds the same values in
``awsBatchSubmitJobs_runSingleReferenceImagePipeline.py`` and writes them
from ``registerCompletedJobsInDB.py`` through ``addRefImage``,
``registerRefImImage``, ``registerRefImCatalog`` and ``registerRefImMeta``.
Unlike `dev`, which reads the frame list back from a CSV it uploaded,
the constituents travel in the manifest as instance ids and `register`
resolves each to its `l2files` row.

The key's ``version`` is the selection digest (ruling R5), not
`refimages.version`: the legacy per-(field, fid, ppid) counter is
allocated at registration by ``addRefImage`` (products page, "legacy
version columns").

This module imports nothing from `rapidpipe` outside `rapidpipe.products`,
matching the products page's own dependency rule.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, fields
from typing import Any, Mapping

from rapidpipe.products.filters import ROMAN_TO_RAPID_FILTER_NAMES, rapid_filter_name

_MD5_RE = re.compile(r"^[0-9a-f]{32}$")
#: A ULID as rapidpipe.db.ids writes one (the rapid_ulid domain's pattern);
#: repeated here because products must not import db.
_ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_SETTINGS_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

REFERENCE_IMAGE_KEY_FIELDS = ("field", "filter", "recipe", "version")
REFERENCE_CATALOG_KEY_FIELDS = ("reference", "catalog_type")

#: The recipes a reference image may be built with. `awaicgen` is `dev`'s
#: only reference recipe (ruling R3); `register` maps it to its
#: `pipelines` row (``rapidpipe.db.refimages.REFERENCE_RECIPE_PPIDS``).
REFERENCE_RECIPES = ("awaicgen",)

#: Bundle roles of a reference image (ruling R5): the primary member is
#: ``image``; all three are required and no other role is declared.
REFERENCE_IMAGE_ROLES = ("image", "coverage", "uncertainty")

# ROMAN_TO_RAPID_FILTER_NAMES and rapid_filter_name (imported above from
# rapidpipe.products.filters): `dev`'s `roman_to_rapid_filter_names`
# (`modules/utils/rapid_pipeline_subs.py`), Roman filter designations to
# the RAPID names that FITS FILTER headers and the `filters` table carry;
# F184 is spelled the same either way. The `reference` stage carries the
# same map (`rapidpipe.science.reference.prep`); the single copy lives in
# `rapidpipe.products.filters` (step 8, WP-E) and is re-exported here
# under its original name, since this module used to define it.

#: `dev`'s ``refimcatalogs.cattype`` values: 1 for the SExtractor catalog,
#: 2 for the Photutils PSF-fit catalog (``registerCompletedJobsInDB.py``).
REFERENCE_CATALOG_CATTYPES: dict[str, int] = {"sextractor": 1, "psf": 2}


class ReferenceRegistrationError(ValueError):
    """A reference-image or reference-catalog block or entry failed validation."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ReferenceRegistrationError(message)


def _is_number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _from_dict(cls, d: Mapping[str, Any], label: str):
    known = {f.name for f in fields(cls)}
    unknown = set(d) - known
    if unknown:
        raise ReferenceRegistrationError(
            f"{label} registration has unknown fields: {sorted(unknown)}")
    missing = known - set(d)
    if missing:
        raise ReferenceRegistrationError(
            f"{label} registration is missing fields: {sorted(missing)}")
    return cls(**d)


#: The block's real-valued measurements (`refimmeta`'s ``real``/``double``
#: columns, plus the header values carried alongside). Each must be a
#: finite number.
_FLOAT_FIELDS = (
    "mjdobs_min", "mjdobs_max", "jd_start", "jd_end", "total_exptime",
    "zero_point", "cov5percent", "medncov", "medpixunc", "clmean",
    "clstddev", "gmedian", "datascale", "gmin", "gmax", "fwhmmedpix",
    "fwhmminpix", "fwhmmaxpix",
)
#: The block's counts (`refimmeta`'s ``integer``/``smallint`` columns).
_COUNT_FIELDS = ("npixnan", "clnoutliers", "nsexcatsources")


@dataclass(frozen=True)
class ReferenceImageRegistration:
    """Everything `register` needs to write the `refimages`, `refimmeta`
    and `refimimages` rows of one reference image (ruling R6)."""

    md5: str
    status: int
    infobits: int
    field: int
    filter: str
    ra_center: float
    dec_center: float
    constituents: list[str]
    nframes: int
    mjdobs_min: float
    mjdobs_max: float
    jd_start: float
    jd_end: float
    total_exptime: float
    zero_point: float
    cov5percent: float
    medncov: float
    medpixunc: float
    npixnan: int
    clmean: float
    clstddev: float
    clnoutliers: int
    gmedian: float
    datascale: float
    gmin: float
    gmax: float
    fwhmmedpix: float
    fwhmminpix: float
    fwhmmaxpix: float
    nsexcatsources: int
    npucatsources: int | None
    settings_hash: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "ReferenceImageRegistration":
        return _from_dict(cls, d, "reference-image")

    def validate(self) -> None:
        _require(isinstance(self.md5, str) and _MD5_RE.match(self.md5) is not None,
                 f"md5 must be 32 lowercase hex characters, got {self.md5!r}")
        _require(self.status in (0, 1) and _is_int(self.status),
                 f"status must be 0 or 1, got {self.status!r}")
        _require(_is_int(self.infobits) and self.infobits >= 0,
                 f"infobits must be a non-negative integer, got {self.infobits!r}")
        _require(_is_int(self.field) and self.field > 0,
                 f"field must be a positive integer (a tessellation index), "
                 f"got {self.field!r}")
        _require(isinstance(self.filter, str) and bool(self.filter),
                 f"filter must be a non-empty string, got {self.filter!r}")
        _require(_is_number(self.ra_center) and 0.0 <= self.ra_center < 360.0,
                 f"ra_center must be in [0, 360), got {self.ra_center!r}")
        _require(_is_number(self.dec_center) and -90.0 <= self.dec_center <= 90.0,
                 f"dec_center must be in [-90, 90], got {self.dec_center!r}")

        _require(isinstance(self.constituents, list) and bool(self.constituents),
                 "constituents must be a non-empty list of l2-image instance ids")
        for constituent in self.constituents:
            _require(isinstance(constituent, str) and _ULID_RE.match(constituent) is not None,
                     f"constituent must be an instance ULID, got {constituent!r}")
        repeated = sorted({c for c in self.constituents if self.constituents.count(c) > 1})
        _require(not repeated, f"constituents repeat instance ids {repeated}")
        _require(_is_int(self.nframes) and self.nframes == len(self.constituents),
                 f"nframes must equal the number of constituents "
                 f"({len(self.constituents)}), got {self.nframes!r}")

        for name in _FLOAT_FIELDS:
            value = getattr(self, name)
            _require(_is_number(value), f"{name} must be a finite number, got {value!r}")
        for name in _COUNT_FIELDS:
            value = getattr(self, name)
            _require(_is_int(value) and value >= 0,
                     f"{name} must be a non-negative integer, got {value!r}")
        _require(self.npucatsources is None
                 or (_is_int(self.npucatsources) and self.npucatsources >= 0),
                 f"npucatsources must be a non-negative integer or null, "
                 f"got {self.npucatsources!r}")

        _require(self.mjdobs_min <= self.mjdobs_max,
                 f"mjdobs_min {self.mjdobs_min!r} is after mjdobs_max {self.mjdobs_max!r}")
        _require(self.jd_start <= self.jd_end,
                 f"jd_start {self.jd_start!r} is after jd_end {self.jd_end!r}")
        _require(self.total_exptime >= 0.0,
                 f"total_exptime must be non-negative, got {self.total_exptime!r}")
        _require(0.0 <= self.cov5percent <= 100.0,
                 f"cov5percent must be a percentage in [0, 100], got {self.cov5percent!r}")
        _require(isinstance(self.settings_hash, str)
                 and _SETTINGS_HASH_RE.match(self.settings_hash) is not None,
                 f"settings_hash must be 'sha256:' and 64 hex characters, "
                 f"got {self.settings_hash!r}")


@dataclass(frozen=True)
class ReferenceCatalogRegistration:
    """Everything `register` needs to write one `refimcatalogs` row (ruling R6)."""

    md5: str
    status: int
    catalog_type: str
    source_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "ReferenceCatalogRegistration":
        return _from_dict(cls, d, "reference-catalog")

    def validate(self) -> None:
        _require(isinstance(self.md5, str) and _MD5_RE.match(self.md5) is not None,
                 f"md5 must be 32 lowercase hex characters, got {self.md5!r}")
        _require(self.status in (0, 1) and _is_int(self.status),
                 f"status must be 0 or 1, got {self.status!r}")
        _require(self.catalog_type in REFERENCE_CATALOG_CATTYPES,
                 f"unknown catalog_type {self.catalog_type!r}; "
                 f"known: {list(REFERENCE_CATALOG_CATTYPES)}")
        _require(_is_int(self.source_count) and self.source_count >= 0,
                 f"source_count must be a non-negative integer, got {self.source_count!r}")


def _primary(entry: Mapping[str, Any]) -> Mapping[str, Any] | None:
    members = entry.get("members") or []
    return next((m for m in members if m.get("path") == entry.get("primary")), None)


def validate_reference_image_entry(entry: Mapping[str, Any]) -> ReferenceImageRegistration:
    """Check one `reference-image` manifest entry (as a dict); return its block.

    Checks the logical key's four fields and its recipe, the bundle's three
    roles with ``image`` primary, the block itself, and that the block's
    field and filter are the key's.
    """
    _require(entry.get("kind") == "reference-image",
             f"entry kind is {entry.get('kind')!r}, expected 'reference-image'")
    key = entry.get("key") or {}
    _require(set(key) == set(REFERENCE_IMAGE_KEY_FIELDS),
             f"reference-image key must name exactly {list(REFERENCE_IMAGE_KEY_FIELDS)}, "
             f"got {sorted(key)}")
    for key_field in REFERENCE_IMAGE_KEY_FIELDS:
        _require(isinstance(key[key_field], str) and bool(key[key_field]),
                 f"reference-image key field {key_field!r} must be a non-empty string")
    _require(key["recipe"] in REFERENCE_RECIPES,
             f"unknown reference recipe {key['recipe']!r}; known: {list(REFERENCE_RECIPES)}")

    members = entry.get("members") or []
    roles = [m.get("role") for m in members]
    _require(sorted(roles) == sorted(REFERENCE_IMAGE_ROLES),
             f"a reference-image bundle has exactly the roles "
             f"{list(REFERENCE_IMAGE_ROLES)}, got {roles}")
    primary = _primary(entry)
    _require(primary is not None and primary.get("role") == "image",
             "the primary member of a reference-image bundle must be its 'image' role")

    registration = ReferenceImageRegistration.from_dict(entry.get("registration") or {})
    registration.validate()
    _require(key["field"] == str(registration.field),
             f"registration field {registration.field!r} is not the key's field "
             f"{key['field']!r}")
    _require(rapid_filter_name(key["filter"]) == rapid_filter_name(registration.filter),
             f"registration filter {registration.filter!r} is not the key's filter "
             f"{key['filter']!r}")
    return registration


def validate_reference_catalog_entry(entry: Mapping[str, Any]) -> ReferenceCatalogRegistration:
    """Check one `reference-catalog` manifest entry (as a dict); return its block.

    The key names the reference-image instance and the catalog type; the
    entry has one member, role ``catalog``, which is its primary; the
    block's ``catalog_type`` is the key's.
    """
    _require(entry.get("kind") == "reference-catalog",
             f"entry kind is {entry.get('kind')!r}, expected 'reference-catalog'")
    key = entry.get("key") or {}
    _require(set(key) == set(REFERENCE_CATALOG_KEY_FIELDS),
             f"reference-catalog key must name exactly {list(REFERENCE_CATALOG_KEY_FIELDS)}, "
             f"got {sorted(key)}")
    _require(isinstance(key["reference"], str) and _ULID_RE.match(key["reference"]) is not None,
             f"reference-catalog key reference must be an instance ULID, "
             f"got {key['reference']!r}")
    _require(key["catalog_type"] in REFERENCE_CATALOG_CATTYPES,
             f"unknown catalog_type {key['catalog_type']!r}; "
             f"known: {list(REFERENCE_CATALOG_CATTYPES)}")
    members = entry.get("members") or []
    _require(len(members) == 1 and members[0].get("role") == "catalog"
             and members[0].get("path") == entry.get("primary"),
             "a reference-catalog entry has exactly one member, role 'catalog', "
             "and it is the primary")
    registration = ReferenceCatalogRegistration.from_dict(entry.get("registration") or {})
    registration.validate()
    _require(registration.catalog_type == key["catalog_type"],
             f"registration catalog_type {registration.catalog_type!r} is not the key's "
             f"{key['catalog_type']!r}")
    return registration
