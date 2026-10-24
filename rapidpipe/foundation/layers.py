"""A run's settings body, resolved at ``run create`` from three layers.

Foundation plan v9, Part 1 (amended 2026-10-23): the layers, low to high,
are the base (the shipped ``foundation-thin.toml``, dev's values and
behaviour, or another full body named by ``--settings``), one dataset
overlay (a TOML holding only the keys that differ for that dataset, each
with the measurement that earned it in a comment) and the run's own
``--set TABLE.key=value`` overrides, applied in order. Tables merge key by
key; a value in a higher layer replaces the lower's outright. A key the
base does not have is refused, naming it, so a misspelt overlay key fails
the command instead of being ignored by every stage; the only exceptions
are :data:`OPTIONAL_KEYS`, which the base documents as absent by default.

:func:`resolve` returns the resolved body, which the run stores and every
stage reads, and the record of how it was built, which the run stores
beside it: the body's hash and each layer's name and SHA-256. Nothing here
runs at execution time.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path
from typing import Any, Sequence

#: Keys the base documents as absent by default and a higher layer may add:
#: the source root is a location (never in the repository) and the reference
#: bound differs per run (foundation-thin.toml's comments on both).
OPTIONAL_KEYS = frozenset({
    ("intake", "source_root"),
    ("reference", "selection", "before_mjd"),
})


class LayerError(ValueError):
    """A layer could not be read or parsed, or named a key the base does not have."""


def body_hash(body: dict[str, Any]) -> str:
    """``sha256:`` and the hex digest of the body's canonical JSON (keys sorted)."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_set(text: str) -> tuple[tuple[str, ...], Any]:
    """``TABLE.key=value`` as (the dotted path, the TOML-typed value).

    The value is a TOML value: ``61591.4056`` is a float, ``true`` a
    boolean, ``"s3://..."`` a string (a bare word is refused, not guessed).
    """
    dotted, sep, raw = text.partition("=")
    path = tuple(part.strip() for part in dotted.split("."))
    if not sep or len(path) < 2 or not all(path):
        raise LayerError(f"--set {text!r}: expected TABLE.key=value")
    try:
        value = tomllib.loads(f"v = {raw}")["v"]
    except tomllib.TOMLDecodeError as exc:
        raise LayerError(f"--set {text!r}: the value is not a TOML value "
                         f"(a string is quoted): {exc}") from exc
    return path, value


def _merge(lower: dict[str, Any], upper: dict[str, Any], base: dict[str, Any] | None,
           path: tuple[str, ...], layer: str) -> dict[str, Any]:
    result = dict(lower)
    for key, value in upper.items():
        here = (*path, key)
        known = base is not None and key in base
        if not known and here not in OPTIONAL_KEYS:
            raise LayerError(f"{layer}: unknown settings key {'.'.join(here)!r} "
                             f"(the base has no such key)")
        below = result.get(key)
        if isinstance(below, dict) != isinstance(value, dict) and key in result:
            raise LayerError(f"{layer}: settings key {'.'.join(here)!r} changes shape "
                             f"(a table in one layer, a value in the other)")
        if isinstance(value, dict):
            result[key] = _merge(below or {}, value, base[key] if known else None, here, layer)
        else:
            result[key] = value
    return result


def _read_toml(path: Path, what: str) -> tuple[dict[str, Any], str]:
    try:
        data = path.read_bytes()
        return tomllib.loads(data.decode("utf-8")), hashlib.sha256(data).hexdigest()
    except (OSError, ValueError) as exc:
        raise LayerError(f"cannot read the {what} file {path}: {exc}") from exc


def resolve(base: Path, overlay: Path | None = None,
            sets: Sequence[str] = ()) -> tuple[dict[str, Any], dict[str, Any]]:
    """The resolved body and its layers record, from ``base``, ``overlay`` and ``sets``.

    ``base`` is TOML, or JSON when its name ends in ``.json`` (a stored
    body). The record is ``{"settings_hash": ..., "layers": [...]}``, one
    entry per layer given: ``{"layer": "base" | "overlay", "name", "sha256"}``
    for the files, ``{"layer": "set", "sets": [...], "sha256"}`` for the
    overrides in order, hashed as their newline-joined text.
    """
    if base.suffix == ".json":
        try:
            data = base.read_bytes()
            body, digest = json.loads(data), hashlib.sha256(data).hexdigest()
        except (OSError, ValueError) as exc:
            raise LayerError(f"cannot read the settings file {base}: {exc}") from exc
    else:
        body, digest = _read_toml(base, "settings")
    if not isinstance(body, dict):
        raise LayerError(f"the settings file {base} is not a table")
    layers: list[dict[str, Any]] = [{"layer": "base", "name": base.name, "sha256": digest}]
    resolved = body
    if overlay is not None:
        upper, digest = _read_toml(overlay, "overlay")
        resolved = _merge(resolved, upper, body, (), f"overlay {overlay.name}")
        layers.append({"layer": "overlay", "name": overlay.name, "sha256": digest})
    if sets:
        for text in sets:
            path, value = parse_set(text)
            nested: Any = value
            for key in reversed(path):
                nested = {key: nested}
            resolved = _merge(resolved, nested, body, (), f"--set {text!r}")
        layers.append({"layer": "set", "sets": list(sets),
                       "sha256": hashlib.sha256("\n".join(sets).encode("utf-8")).hexdigest()})
    return resolved, {"settings_hash": body_hash(resolved), "layers": layers}
