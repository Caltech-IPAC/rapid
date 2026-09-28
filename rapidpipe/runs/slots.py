"""Promotion selectors and frozen promotion plans (runs.md §Rules).

Every product instance carries a ``slot``, the part of its identity a
consumer selects on; at most one instance is current per (kind, slot) and
promotion replaces by slot. The slot and the identity are
derived by the database from the provenance key (``logical_key``), in
``product_identity_derive()`` of migration 20260926-02, and nowhere in
Python. This module holds only the plain-data shapes the repository and
the CLI exchange:

- a *selector*, one change's target: ``{"slot": {...}}``;
- a *plan entry*, one slot a promotion would change:
  ``{"kind", "slot", "before", "after"}``, the JSON ``rapidpipe run
  promote-plan`` prints and ``rapidpipe run promote --plan`` reads.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Sequence

#: ``{"slot": {...}}``.
Selector = dict[str, dict[str, Any]]

#: ``{"kind": str, "slot": dict, "before": str | None, "after": str}``.
PlanEntry = dict[str, Any]

SELECTOR_KINDS = ("slot",)


def canonical_json(value: Any) -> str:
    """One text per JSON value: sorted keys, no spaces. Two slots are the
    same slot exactly when their canonical texts are equal."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def selector_parts(selector: Any) -> tuple[str, dict[str, Any]]:
    """``(by, value)`` of a selector; ``ValueError`` when it is not a
    one-key mapping ``{"slot": <non-empty object>}``."""
    if not isinstance(selector, dict) or len(selector) != 1:
        raise ValueError(f"selector {selector!r} is not {{'slot': {{...}}}}")
    ((by, value),) = selector.items()
    if by not in SELECTOR_KINDS or not isinstance(value, dict):
        raise ValueError(f"selector {selector!r} is not {{'slot': {{...}}}}")
    if not value:
        raise ValueError("an empty slot selects nothing")
    return by, value


def plan_entries(
    changes: Iterable[tuple[str, dict[str, Any], str | None, str | None]],
) -> list[PlanEntry]:
    """Plan entries from ``(kind, slot, before, after)`` changes, sorted by
    kind then canonical slot."""
    entries = [{"kind": kind, "slot": slot, "before": before, "after": after}
               for kind, slot, before, after in changes]
    return sorted(entries, key=lambda e: (e["kind"], canonical_json(e["slot"])))


def plan_json(entries: Sequence[PlanEntry]) -> str:
    """The text ``rapidpipe run promote-plan`` prints: a JSON list."""
    return json.dumps(list(entries), sort_keys=True, indent=2)


def plan_by_slot(entries: Any) -> dict[tuple[str, str], tuple[str | None, str | None]]:
    """``{(kind, canonical slot): (before, after)}`` of a plan;
    ``ValueError`` naming the first malformed entry or repeated slot."""
    if not isinstance(entries, list):
        raise ValueError("a plan is a JSON list of {kind, slot, before, after}")
    out: dict[tuple[str, str], tuple[str | None, str | None]] = {}
    for position, entry in enumerate(entries):
        if (not isinstance(entry, dict) or set(entry) != {"kind", "slot", "before", "after"}
                or not isinstance(entry["kind"], str)
                or not isinstance(entry["slot"], dict) or not entry["slot"]
                or not (entry["before"] is None or isinstance(entry["before"], str))
                or not isinstance(entry["after"], str)):
            raise ValueError(f"entry {position} is not {{kind, slot, before, after}} with a "
                             "non-empty slot object and instance-id strings")
        key = (entry["kind"], canonical_json(entry["slot"]))
        if key in out:
            raise ValueError(f"entry {position} repeats kind={key[0]!r} slot={key[1]}")
        out[key] = (entry["before"], entry["after"])
    return out
