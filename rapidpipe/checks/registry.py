"""The check registry: named, versioned Python functions over one product
instance (supervisor step 6, 2026-09-24, R1).

A check is registered with :func:`check`::

    @check("difference-image-statistics", "1", kind="difference-image",
           params=("scalefacref_lo", ...))
    def difference_image_statistics(conn, instance_id, params):
        ...
        return CheckResult("passed", {...}, "every measurement within bounds")

and looked up by its reference ``name@version``. The function takes an
open ``psycopg2`` connection, the product instance id and the params the
check policy gives it, and returns a :class:`CheckResult`; it reads, never
writes. Recording the result (one ``checks`` row per run, including a
row with outcome ``failed`` when the function raises) is
:func:`rapidpipe.checks.runner.run_check`'s job, not the function's.

Versions are strings. A changed threshold is a policy change (a new policy
version); a changed measurement or rule is a new check version, registered
beside the old one, never an edit to it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

#: Outcomes a check may record (the ``checks.outcome`` CHECK constraint).
OUTCOMES = ("passed", "failed")


class CheckError(Exception):
    """Base class for check registry and policy errors."""


class UnknownCheck(CheckError):
    """No check is registered under the requested ``name@version``."""


@dataclass(frozen=True)
class CheckResult:
    """What a check function returns: an outcome, the detail to record
    (measurements, bounds, reasons) and a one-line summary."""

    outcome: str
    detail: dict[str, Any] = field(default_factory=dict)
    summary: str = ""

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of {OUTCOMES}, got {self.outcome!r}")


CheckFunction = Callable[[Any, str, dict[str, Any]], CheckResult]


@dataclass(frozen=True)
class RegisteredCheck:
    """One registered check: its identity, the product kind it applies to,
    the parameter names it takes, and the function."""

    name: str
    version: str
    kind: str
    params: tuple[str, ...]
    function: CheckFunction
    description: str = ""

    @property
    def ref(self) -> str:
        return check_ref(self.name, self.version)


_REGISTRY: dict[str, RegisteredCheck] = {}


def check_ref(name: str, version: str) -> str:
    """``name@version``, the key checks and policies are looked up by."""
    return f"{name}@{version}"


def parse_ref(ref: str) -> tuple[str, str]:
    """Split ``name@version``; :class:`ValueError` on any other shape."""
    name, sep, version = ref.rpartition("@")
    if not sep or not name or not version:
        raise ValueError(f"expected NAME@VERSION, got {ref!r}")
    return name, version


def check(name: str, version: str, *, kind: str, params: tuple[str, ...] = (),
          description: str = "") -> Callable[[CheckFunction], CheckFunction]:
    """Register the decorated function as check ``name@version``.

    ``kind`` is the product-instance kind it applies to; ``params`` the
    parameter names a policy must give it (a policy naming others, or
    missing one, is refused at load). Registering one reference twice is
    an error: a changed check is a new version.
    """
    if not isinstance(version, str) or not version:
        raise ValueError("a check version is a non-empty string")

    def decorate(function: CheckFunction) -> CheckFunction:
        ref = check_ref(name, version)
        if ref in _REGISTRY and _REGISTRY[ref].function is not function:
            raise CheckError(f"check {ref} is already registered")
        doc_lines = (function.__doc__ or "").strip().splitlines()
        _REGISTRY[ref] = RegisteredCheck(
            name, version, kind, tuple(params), function,
            description or (doc_lines[0] if doc_lines else ""))
        return function

    return decorate


def _load_builtins() -> None:
    # Importing the module registers its checks.
    from rapidpipe.checks import builtin  # noqa: F401


def get_check(ref: str) -> RegisteredCheck:
    """The check registered as ``ref`` (``name@version``), or :class:`UnknownCheck`."""
    _load_builtins()
    try:
        return _REGISTRY[ref]
    except KeyError:
        raise UnknownCheck(
            f"no check is registered as {ref!r} (registered: "
            f"{', '.join(sorted(_REGISTRY)) or 'none'})") from None


def registered_checks() -> list[RegisteredCheck]:
    """Every registered check, ordered by reference."""
    _load_builtins()
    return [_REGISTRY[ref] for ref in sorted(_REGISTRY)]
