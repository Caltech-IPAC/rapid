"""Check policies: named, versioned TOML files shipped in the package
(supervisor step 6, 2026-09-24, R3, with the plan-review amendment A1).

A policy lives at ``rapidpipe/checks/policies/<name>@<version>.toml`` and
is loaded by its reference ``name@version``::

    name = "rebuild-trial"
    version = "1"
    description = "..."
    approval = "trial"            # none | trial | lead
    approved_by = "..."           # who approved it; required unless approval = "none"
    auto_promote = false

    [[checks]]
    name = "difference-image-statistics"
    version = "1"
    kind = "difference-image"
    required = true
    [checks.params]
    rms_max = 1.0
    ...

Policies are immutable once landed: a change is a new version (a new
file). There is no policy table in the database; a promotion records the
policy's reference and the ``checks`` row ids it relied on (R3).

Approval (A1): a policy with ``approval = "none"`` admits no promotion;
``trial`` or ``lead`` admits manual promotion; automatic promotion needs
``approval = "lead"``, ``approved_by`` set and ``auto_promote = true``
(:func:`policy_permits_auto_promote`, R5). No shipped policy permits it.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from rapidpipe.checks.registry import CheckError, UnknownCheck, check_ref, get_check, parse_ref

#: The policy a promotion is validated under when neither ``--check-policy``
#: nor the run's ``check_policy_ref`` names one (R4).
DEFAULT_POLICY = "rebuild-trial@1"

APPROVALS = ("none", "trial", "lead")

#: Test seam: policies not shipped in the package, keyed ``name@version``,
#: consulted after the shipped files. Only tests put anything here (a
#: fixture policy that permits automatic promotion, R5); production code
#: never writes it, so a policy cannot be introduced at run time.
_FIXTURE_POLICIES: dict[str, "Policy"] = {}


class PolicyError(CheckError):
    """A policy is unknown or malformed."""


@dataclass(frozen=True)
class PolicyCheck:
    """One ``[[checks]]`` row of a policy."""

    name: str
    version: str
    kind: str
    required: bool
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def ref(self) -> str:
        return check_ref(self.name, self.version)

    def params_json(self) -> str:
        """The params as canonical JSON, the form recorded in a check row's
        ``detail.params`` and matched by the promotion gate (A2)."""
        return json.dumps(self.params, sort_keys=True)


@dataclass(frozen=True)
class Policy:
    name: str
    version: str
    approval: str
    approved_by: str | None
    auto_promote: bool
    checks: tuple[PolicyCheck, ...]
    description: str = ""

    @property
    def ref(self) -> str:
        return check_ref(self.name, self.version)

    def checks_for_kind(self, kind: str) -> list[PolicyCheck]:
        return [c for c in self.checks if c.kind == kind]

    def find_check(self, ref: str) -> PolicyCheck | None:
        return next((c for c in self.checks if c.ref == ref), None)


def policy_permits_promotion(policy: Policy) -> bool:
    """True when the policy is approved (trial or lead, with ``approved_by``
    recorded) and so may admit a manual promotion (A1)."""
    return policy.approval in ("trial", "lead") and bool(policy.approved_by)


def policy_permits_auto_promote(policy: Policy) -> bool:
    """True only for a lead-approved policy with ``auto_promote = true``
    (R5, A1). No shipped policy satisfies this."""
    return policy.approval == "lead" and bool(policy.approved_by) and policy.auto_promote


def _policy_files() -> dict[str, Any]:
    root = resources.files("rapidpipe.checks").joinpath("policies")
    found = {}
    for entry in root.iterdir():
        if entry.name.endswith(".toml"):
            found[entry.name[: -len(".toml")]] = entry
    return found


def shipped_policies() -> list[str]:
    """References of every policy file shipped in the package, sorted."""
    return sorted(_policy_files())


def parse_policy(data: dict[str, Any], *, source: str, expected_ref: str | None = None) -> Policy:
    """Validate a policy document's fields and build a :class:`Policy`.

    Refuses (:class:`PolicyError`) an unknown field, a name/version that
    do not match ``expected_ref`` (the file name), an unknown approval, an
    approved policy with no ``approved_by``, a check that is not
    registered, whose kind differs from the registered kind, whose params
    do not name exactly the check's parameters, or that appears twice.
    """
    def fail(message: str) -> PolicyError:
        return PolicyError(f"check policy {source}: {message}")

    known = {"name", "version", "description", "approval", "approved_by",
             "auto_promote", "checks"}
    unknown = sorted(set(data) - known)
    if unknown:
        raise fail(f"unknown fields {unknown}")
    try:
        name, version = str(data["name"]), str(data["version"])
    except KeyError as exc:
        raise fail(f"missing field {exc.args[0]!r}") from None
    ref = check_ref(name, version)
    if expected_ref is not None and ref != expected_ref:
        raise fail(f"declares {ref!r}, but its file is named {expected_ref!r}")
    approval = data.get("approval", "none")
    if approval not in APPROVALS:
        raise fail(f"approval must be one of {APPROVALS}, got {approval!r}")
    approved_by = data.get("approved_by") or None
    if approval != "none" and not approved_by:
        raise fail(f"approval {approval!r} needs approved_by")
    auto_promote = data.get("auto_promote", False)
    if not isinstance(auto_promote, bool):
        raise fail("auto_promote must be true or false")

    checks: list[PolicyCheck] = []
    for row in data.get("checks", []):
        extra = sorted(set(row) - {"name", "version", "kind", "required", "params"})
        if extra:
            raise fail(f"unknown check fields {extra}")
        try:
            pc = PolicyCheck(str(row["name"]), str(row["version"]), str(row["kind"]),
                             bool(row.get("required", True)), dict(row.get("params", {})))
        except KeyError as exc:
            raise fail(f"a check is missing {exc.args[0]!r}") from None
        try:
            registered = get_check(pc.ref)
        except UnknownCheck as exc:
            raise fail(str(exc)) from None
        if registered.kind != pc.kind:
            raise fail(f"check {pc.ref} applies to kind {registered.kind!r}, "
                       f"not {pc.kind!r}")
        if set(pc.params) != set(registered.params):
            raise fail(f"check {pc.ref} params {sorted(pc.params)} must be exactly "
                       f"{sorted(registered.params)}")
        if any(c.ref == pc.ref for c in checks):
            raise fail(f"check {pc.ref} appears twice")
        checks.append(pc)

    return Policy(name, version, approval, approved_by, auto_promote, tuple(checks),
                  str(data.get("description", "")))


def load_policy_file(path: str | Path) -> Policy:
    """Load and validate a policy from a TOML file (no name check)."""
    path = Path(path)
    with path.open("rb") as handle:
        return parse_policy(tomllib.load(handle), source=str(path))


def load_policy(ref: str) -> Policy:
    """The shipped policy ``ref`` (``name@version``), validated.

    :class:`PolicyError` when no such policy ships (or it is malformed).
    """
    try:
        parse_ref(ref)
    except ValueError as exc:
        raise PolicyError(f"check policy {ref!r}: {exc}") from None
    files = _policy_files()
    if ref in files:
        with files[ref].open("rb") as handle:
            return parse_policy(tomllib.load(handle), source=ref, expected_ref=ref)
    if ref in _FIXTURE_POLICIES:
        return _FIXTURE_POLICIES[ref]
    raise PolicyError(
        f"check policy {ref!r} does not exist (shipped: "
        f"{', '.join(shipped_policies()) or 'none'})")
