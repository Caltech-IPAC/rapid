"""The `alert-container` and `alert-set` registration blocks, as code.

The products page's "File products" table: an `alert-container` is a
detector-image product, logical key (difference instance, alert schema
version), an Avro object container plus a JSON summary, made by `alerts`,
recorded in the alert outbox. Its field list, fixed with the `alerts` stage
(lead's ruling, 2026-09-24):

- key: ``difference`` (the difference instance the alerts were cut from)
  and ``schema_version`` (the Avro schema, ``"00.04"``);
- members: role ``container`` (the primary, the ``.avro`` file) and role
  ``summary`` (`dev`'s per-chip summary JSON), both required;
- registration: ``alert_count``, ``dropped_count``, ``schema_version``,
  ``difference``, ``source_set``, ``association_sets`` (one or more: an
  image can span fields; one per field) and ``statistics_sets`` (zero or
  more), the last two lists of instance ids, and ``association_set`` and
  ``statistics_set``, the first of each list (``statistics_set`` null when
  the list is empty), kept for the products page's singular shape.

The `alerts` stage writes the container's outbox rows and registers both
of its outputs itself, in its own transaction. `register` validates an
`alert-container` entry and writes nothing beyond its instance row, like
`source-catalog`, so registering the alerts manifest again is a no-op
replay. The `alert-set` beside it is the database result set of the
outbox rows (no members); `register` validates and accepts it the same way.

This module imports nothing from `rapidpipe` outside `rapidpipe.products`.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

ALERT_CONTAINER_KIND = "alert-container"
ALERT_SET_KIND = "alert-set"

KEY_FIELDS = ("difference", "schema_version")
ROLES = ("container", "summary")
REGISTRATION_FIELDS = ("alert_count", "dropped_count", "schema_version", "difference",
                       "source_set", "association_sets", "statistics_sets",
                       "association_set", "statistics_set")

_VERSION_RE = re.compile(r"^[0-9]{2}\.[0-9]{2}$")


class AlertContainerRegistrationError(ValueError):
    """An alert-container or alert-set entry failed validation."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AlertContainerRegistrationError(message)


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _check_key(kind: str, key: Mapping[str, Any]) -> None:
    _require(set(key) == set(KEY_FIELDS),
             f"{kind} key must name exactly {list(KEY_FIELDS)}, got {sorted(key)}")
    _require(isinstance(key["difference"], str) and bool(key["difference"]),
             f"{kind} key difference must be a non-empty instance id")
    _require(isinstance(key["schema_version"], str)
             and _VERSION_RE.match(key["schema_version"]) is not None,
             f"{kind} key schema_version must look like '00.04', got {key['schema_version']!r}")


def validate_alert_container_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Check one `alert-container` manifest entry (as a dict); return its registration block."""
    _require(entry.get("kind") == ALERT_CONTAINER_KIND,
             f"entry kind is {entry.get('kind')!r}, expected {ALERT_CONTAINER_KIND!r}")
    key = entry.get("key") or {}
    _check_key(ALERT_CONTAINER_KIND, key)

    members = entry.get("members") or []
    roles = [m.get("role") for m in members]
    _require(sorted(roles) == sorted(ROLES),
             f"an alert-container has exactly the roles {list(ROLES)}, got {roles}")
    container = next(m for m in members if m.get("role") == "container")
    _require(container.get("path") == entry.get("primary"),
             "the primary member of an alert-container is its 'container' role")

    registration = dict(entry.get("registration") or {})
    _require(set(registration) == set(REGISTRATION_FIELDS),
             f"alert-container registration must name exactly {sorted(REGISTRATION_FIELDS)}, "
             f"got {sorted(registration)}")
    for name in ("alert_count", "dropped_count"):
        _require(_is_count(registration[name]),
                 f"{name} must be a non-negative integer, got {registration[name]!r}")
    _require(registration["schema_version"] == key["schema_version"],
             "registration schema_version differs from the key's")
    _require(registration["difference"] == key["difference"],
             "registration difference differs from the key's")
    _require(isinstance(registration["source_set"], str) and bool(registration["source_set"]),
             "source_set must be a non-empty instance id")
    for name, minimum in (("association_sets", 1), ("statistics_sets", 0)):
        ids = registration[name]
        _require(isinstance(ids, list) and len(ids) >= minimum
                 and all(isinstance(i, str) and i for i in ids),
                 f"{name} must be a list of at least {minimum} instance ids, got {ids!r}")
    _require(registration["association_set"] == registration["association_sets"][0],
             "association_set must be the first of association_sets")
    _require(registration["statistics_set"]
             == (registration["statistics_sets"][0] if registration["statistics_sets"] else None),
             "statistics_set must be the first of statistics_sets, or null")
    return registration


def validate_alert_set_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Check one `alert-set` manifest entry: a result set of outbox rows, no members."""
    _require(entry.get("kind") == ALERT_SET_KIND,
             f"entry kind is {entry.get('kind')!r}, expected {ALERT_SET_KIND!r}")
    _check_key(ALERT_SET_KIND, entry.get("key") or {})
    _require(not entry.get("members") and entry.get("primary") is None,
             "an alert-set is a result set: no members, no primary")
    registration = dict(entry.get("registration") or {})
    _require(set(registration) == {"row_count", "table"},
             f"alert-set registration must name exactly ['row_count', 'table'], "
             f"got {sorted(registration)}")
    _require(_is_count(registration["row_count"]),
             f"row_count must be a non-negative integer, got {registration['row_count']!r}")
    _require(registration["table"] == "alert_outbox",
             f"alert-set table must be 'alert_outbox', got {registration['table']!r}")
    return registration
