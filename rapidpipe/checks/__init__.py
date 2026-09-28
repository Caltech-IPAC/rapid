"""Candidate checks and check policies.

- :mod:`rapidpipe.checks.registry` -- the ``@check(name, version)``
  registry of check functions.
- :mod:`rapidpipe.checks.builtin` -- the two shipped checks,
  ``difference-image-statistics@1`` and ``catalog-counts-vs-reference@1``.
- :mod:`rapidpipe.checks.policy` -- versioned TOML policies shipped in
  ``policies/`` (``rebuild-trial@1``).

Running and recording checks, and automatic promotion (designed in,
off), are in :mod:`rapidpipe.runs.checking`, above this package: checks
never import ``rapidpipe.runs``
(``tests/unit/test_dependency_direction.py``). The promotion gate itself
is in :func:`rapidpipe.runs.repository.promote`.
"""

from rapidpipe.checks.policy import (
    DEFAULT_POLICY,
    Policy,
    PolicyCheck,
    PolicyError,
    load_policy,
    policy_permits_auto_promote,
    policy_permits_promotion,
    shipped_policies,
)
from rapidpipe.checks.registry import (
    CheckError,
    CheckResult,
    UnknownCheck,
    check,
    get_check,
    registered_checks,
)

__all__ = [
    "DEFAULT_POLICY", "Policy", "PolicyCheck", "PolicyError", "load_policy",
    "policy_permits_auto_promote", "policy_permits_promotion", "shipped_policies",
    "CheckError", "CheckResult", "UnknownCheck", "check", "get_check", "registered_checks",
]
