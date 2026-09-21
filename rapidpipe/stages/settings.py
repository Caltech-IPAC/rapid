"""Stage settings: TOML load, recursive merge, canonical hash.

Per the stage contract's "Settings" section: each stage ships default
settings under ``settings/<name>.toml``. ``--settings`` supplies an overlay:
tables merge recursively, supplied scalar and array values replace
defaults. Unknown keys and invalid values fail with exit code 64 (raised
here as :class:`rapidpipe.stages.contract.UsageError` by the caller, since
that exception type lives in ``contract.py`` and this module must not
import back from it -- see the module-level note below).

This module imports only the standard library.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path
from typing import Any

#: Sentinel raised as a plain ValueError; ``rapidpipe.stages.contract`` wraps
#: it as UsageError so that settings resolution failures still map to exit
#: 64 without this module depending on the exception hierarchy that lives
#: downstream of it.
class SettingsError(ValueError):
    """A settings overlay was structurally invalid, or named an unknown key."""


def load_toml(path: str | Path) -> dict[str, Any]:
    """Load a TOML file as a plain dict."""
    with Path(path).open("rb") as fh:
        return tomllib.load(fh)


def merge_settings(defaults: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Merge ``overlay`` onto ``defaults`` per the contract's rules.

    Tables (nested dicts) merge recursively, key by key. A scalar or array
    value in the overlay replaces the default at that key outright -- an
    overlay array is never concatenated or element-merged with the default.
    A key present in the overlay but absent from defaults is an unknown key
    and raises :class:`SettingsError`; the caller maps that to exit 64.
    """
    return _merge(defaults, overlay, path=())


def _merge(defaults: dict[str, Any], overlay: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any]:
    result = dict(defaults)
    for key, value in overlay.items():
        if key not in defaults:
            dotted = ".".join((*path, key))
            raise SettingsError(f"unknown settings key: {dotted!r}")
        default_value = defaults[key]
        if isinstance(default_value, dict) and isinstance(value, dict):
            result[key] = _merge(default_value, value, path=(*path, key))
        elif isinstance(default_value, dict) != isinstance(value, dict):
            dotted = ".".join((*path, key))
            raise SettingsError(
                f"settings key {dotted!r} changes shape "
                f"(default is {'a table' if isinstance(default_value, dict) else 'a scalar/array'}, "
                f"overlay is {'a table' if isinstance(value, dict) else 'a scalar/array'})")
        else:
            result[key] = value
    return result


def resolve_settings(
    schema_path: str | Path | None,
    overlay_path: str | Path | None,
) -> dict[str, Any]:
    """Load the stage's defaults from ``schema_path`` and apply an overlay.

    ``schema_path`` is the stage's ``settings/<name>.toml`` defaults file;
    ``None`` means the stage declares no settings and an empty dict is used
    (an overlay is then only valid if it is also empty). ``overlay_path`` is
    the ``--settings`` argument, or ``None`` if it was not given.
    """
    defaults = load_toml(schema_path) if schema_path is not None else {}
    if overlay_path is None:
        return dict(defaults)
    overlay = load_toml(overlay_path)
    return merge_settings(defaults, overlay)


def canonical_hash(settings: dict[str, Any]) -> str:
    """A stable SHA-256 hex digest of a resolved settings dict.

    Keys are sorted recursively (via ``json.dumps(..., sort_keys=True)``) so
    the hash depends only on content, not on dict insertion order, and is
    stable across the recursive merge above producing new dicts.
    """
    canonical = json.dumps(settings, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
