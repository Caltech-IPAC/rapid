"""The ``module:factory`` seams a subprocess reads from its environment.

A stage run as a subprocess cannot be monkeypatched, so its database and
its external tools are replaceable through one variable each:
``RAPIDPIPE_<STAGE>_DATABASE`` and ``RAPIDPIPE_<STAGE>_TOOLKIT``, set by
the stage's own fixture and unset in every deployment. The read guard's
``RAPIDPIPE_READGUARD_DATABASE`` has the same shape. Each variable names a
``module:factory``; :func:`load_factory` resolves it.

This module is stdlib-only and imports nothing from ``rapidpipe``: stages
and ``rapidpipe.runs`` both use it, and ``rapidpipe.runs`` may not import
``rapidpipe.stages``, so the caller passes the exception a bad value
raises (a stage's ``UsageError``, the read guard's
``ReadGuardNotConfigured``; exit 64 either way).
"""

from __future__ import annotations

import importlib
import os
from typing import Callable

__all__ = ["database_env", "load_factory", "toolkit_env"]


def database_env(stage: str) -> str:
    """The variable naming ``stage``'s replacement database: ``RAPIDPIPE_<STAGE>_DATABASE``."""
    return f"RAPIDPIPE_{stage.upper()}_DATABASE"


def toolkit_env(stage: str) -> str:
    """The variable naming ``stage``'s replacement tools: ``RAPIDPIPE_<STAGE>_TOOLKIT``."""
    return f"RAPIDPIPE_{stage.upper()}_TOOLKIT"


def load_factory(env_name: str, error: type[Exception]) -> Callable | None:
    """The callable ``$env_name`` names as ``module:factory``, or ``None`` when unset or empty.

    A value that does not resolve (no module name, no such module, no such
    attribute on it) raises ``error``. A module that exists but fails while
    importing, a missing dependency of it included, propagates unchanged.
    """
    value = os.environ.get(env_name)
    if not value:
        return None
    module_name, _, factory_name = value.partition(":")
    if not module_name:
        raise error(f"{env_name}={value!r} does not name a factory: no module name")
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        named = exc.name is not None and (
            module_name == exc.name or module_name.startswith(exc.name + "."))
        if not named:
            raise
        raise error(f"{env_name}={value!r} does not name a factory: {exc}") from exc
    try:
        return getattr(module, factory_name)
    except AttributeError as exc:
        raise error(f"{env_name}={value!r} does not name a factory: {exc}") from exc
