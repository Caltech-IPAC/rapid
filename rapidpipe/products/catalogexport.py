"""The `catalog-export` registration block, as code.

The products page's "File products" table: a `catalog-export` is a field
product, a HATS (Hierarchical Adaptive Tiling Scheme) catalog directory
made by `export` from named result sets; "Today's table": none, exported.
Its field list, fixed with the `export` port (supervisor step 8,
2026-09-24, ruling R12), then corrected (supervisor step 8, 2026-09-24,
ruling R13 -- ``export.py`` keyed a multi-source-set export by
``source_sets[0]`` alone, so [A,B] and [A,C] shared a key and [B,A]
differed from [A,B]):

- key: ``field`` (the unit), ``export_type`` (``"sources"`` today; the
  light-curve catalog is the next port), ``selection`` (the full 64-hex
  SHA-256 digest over the sorted, distinct ``source_sets`` instance ids,
  newline-joined -- :func:`selection_digest`, mirroring the reference-image
  selection digest's sorted-newline-joined-ids rule,
  :mod:`rapidpipe.science.reference.identity`; unlike that digest, the
  settings hash is not folded in here, since it is already the key's own
  field) and ``settings_hash``; sorting makes ``selection`` a function of
  the source-set selection as a set, so [A,B] and [B,A] are one logical
  key and [A,B] and [A,C] are different ones;
- members: every file under the HATS catalog directory, with one of three
  roles -- ``hats`` for the catalog's root ``properties`` and
  ``hats.properties`` files, ``partition`` for each ``Npix=*.parquet``
  leaf, ``metadata`` for everything else hats-import writes
  (``partition_info.csv``, ``skymap*.fits``, ``point_map.fits``,
  ``dataset/_metadata``, ``dataset/_common_metadata``); the primary is the
  root ``properties`` file (``hats.properties`` where a hats version writes
  only that);
- registration: ``row_count``, ``export_type``, ``hats_version``,
  ``source_sets`` (one or more instance ids, manifest order; the key's
  ``selection`` is their digest), ``healpix_order`` (the highest partition
  order written), ``partition_count`` (the number of ``partition``
  members) and ``md5`` (of the primary member).

`register` validates a `catalog-export` entry and writes nothing beyond its
instance row, like `source-catalog` and `alert-container`.

This module imports nothing from `rapidpipe` outside `rapidpipe.products`.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping

CATALOG_EXPORT_KIND = "catalog-export"

KEY_FIELDS = ("field", "export_type", "selection", "settings_hash")
ROLES = ("hats", "partition", "metadata")
REGISTRATION_FIELDS = ("row_count", "export_type", "hats_version", "source_sets",
                       "healpix_order", "partition_count", "md5")
EXPORT_TYPES = ("sources",)

#: The root files a HATS catalog's ``hats`` role names.
PROPERTIES_NAMES = ("properties", "hats.properties")

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_MD5_RE = re.compile(r"^[0-9a-f]{32}$")
_PARTITION_RE = re.compile(r"(^|/)Norder=[0-9]+/Dir=[0-9]+/Npix=[0-9]+\.parquet$")


class CatalogExportRegistrationError(ValueError):
    """A catalog-export entry failed validation."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CatalogExportRegistrationError(message)


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def selection_digest(source_sets: Iterable[str]) -> str:
    """The key's ``selection``: SHA-256 hex over the sorted, distinct ``source_sets`` ids.

    Mirrors :func:`rapidpipe.science.reference.identity.selection_digest`'s
    sorted, newline-joined-ids formula (ruling R13); it does not fold in a
    settings hash, since ``settings_hash`` is already the catalog-export
    key's own field. Order-independent (``[A, B]`` and ``[B, A]`` digest
    the same) but membership-sensitive (``[A, B]`` and ``[A, C]`` differ).
    """
    ids = sorted(set(str(s) for s in source_sets))
    return hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()


def role_for(relative_path: str) -> str:
    """The member role of a file at ``relative_path`` inside a HATS catalog directory."""
    pure = PurePosixPath(relative_path)
    if len(pure.parts) == 1 and pure.name in PROPERTIES_NAMES:
        return "hats"
    if _PARTITION_RE.search(relative_path):
        return "partition"
    return "metadata"


def validate_catalog_export_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Check one `catalog-export` manifest entry (as a dict); return its registration block."""
    _require(entry.get("kind") == CATALOG_EXPORT_KIND,
             f"entry kind is {entry.get('kind')!r}, expected {CATALOG_EXPORT_KIND!r}")
    key = entry.get("key") or {}
    _require(set(key) == set(KEY_FIELDS),
             f"catalog-export key must name exactly {list(KEY_FIELDS)}, got {sorted(key)}")
    _require(_is_count(key["field"]), f"key field must be a non-negative integer rtid, "
             f"got {key['field']!r}")
    _require(key["export_type"] in EXPORT_TYPES,
             f"key export_type must be one of {list(EXPORT_TYPES)}, got {key['export_type']!r}")
    _require(isinstance(key["selection"], str) and _HEX64_RE.match(key["selection"]) is not None,
             "key selection must be a SHA-256 hex digest")
    _require(isinstance(key["settings_hash"], str)
             and _HEX64_RE.match(key["settings_hash"]) is not None,
             "key settings_hash must be a SHA-256 hex digest")

    members = entry.get("members") or []
    roles = [m.get("role") for m in members]
    _require(all(r in ROLES for r in roles),
             f"catalog-export member roles must be among {list(ROLES)}, got {sorted(set(roles))}")
    _require(roles.count("hats") >= 1 and roles.count("partition") >= 1,
             "a catalog-export has at least one 'hats' and one 'partition' member")
    primary = next((m for m in members if m.get("path") == entry.get("primary")), None)
    _require(primary is not None and primary.get("role") == "hats"
             and PurePosixPath(primary["path"]).name in PROPERTIES_NAMES,
             "the primary member of a catalog-export is its 'hats' properties file")

    registration = dict(entry.get("registration") or {})
    _require(set(registration) == set(REGISTRATION_FIELDS),
             f"catalog-export registration must name exactly {sorted(REGISTRATION_FIELDS)}, "
             f"got {sorted(registration)}")
    _require(_is_count(registration["row_count"]) and registration["row_count"] > 0,
             f"row_count must be a positive integer, got {registration['row_count']!r}")
    _require(registration["export_type"] == key["export_type"],
             "registration export_type differs from the key's")
    _require(isinstance(registration["hats_version"], str) and bool(registration["hats_version"]),
             "hats_version must be a non-empty string")
    source_sets = registration["source_sets"]
    _require(isinstance(source_sets, list) and len(source_sets) >= 1
             and all(isinstance(s, str) and s for s in source_sets)
             and len(set(source_sets)) == len(source_sets),
             f"source_sets must be a list of distinct instance ids, got {source_sets!r}")
    _require(key["selection"] == selection_digest(source_sets),
             "the key's selection must be the sorted source_sets digest")
    _require(_is_count(registration["healpix_order"]),
             f"healpix_order must be a non-negative integer, got {registration['healpix_order']!r}")
    _require(registration["partition_count"] == roles.count("partition"),
             f"partition_count {registration['partition_count']!r} differs from the "
             f"{roles.count('partition')} partition members")
    _require(isinstance(registration["md5"], str) and _MD5_RE.match(registration["md5"]) is not None,
             "md5 must be 32 lowercase hex digits")
    return registration
