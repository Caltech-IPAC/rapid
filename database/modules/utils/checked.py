"""
File:    checked.py

The checked-call adapter over a `RAPIDDB` handle (integration review
2026-08, composite ruling 10).

`design/database.md`'s connection-path rule is "errors raised, never
swallowed into flags or process exits from library code" — but
`RAPIDDB` predates it and reports a failed query by setting
`self.exit_code` and returning `None` (or an empty result), not by
raising. Every caller that wants the contract has therefore had to
check `getattr(handle, "exit_code", 0)` after its own call and decide
for itself what counts as failure — eleven call sites in
`submission/gathering.py` alone, plus one in
`pipeline/stages/alert_production.py`, each repeating the same
`code = getattr(handle, "exit_code", 0); if code >= 64: raise ...`
shape. `pipeline/registration/products.py`'s `_check` closed exactly
this gap at the registration boundary; this is that adapter's sibling
at the gathering/alert-production boundary, over the handle rather
than around each individual call.

**Wraps the handle, not the `UnitSource` protocol.** `CheckedHandle`
sits between a caller and a raw `RAPIDDB` (or `RAPIDDB.borrowing(...)`)
instance; it is not itself typed as `submission.gathering.UnitSource`
and does not need to be. A `CheckedHandle` still exposes `exit_code`
and every query method the protocol declares (via `__getattr__`
delegation), so passing one where a `UnitSource` is expected works
structurally — but nothing here changes the protocol or the tests'
stub, which continue to satisfy `UnitSource` directly, unchecked. A
caller that wants the raising contract wraps the handle before handing
it to `gather_*`; a caller (or test) that does not, does not.

**Code 7 is not an error.** `rapid_db.py` sets `exit_code = 7` for its
own "no matching record" convention (e.g. `get_best_reference_image`
when no reference exists yet) — a data-shaped answer, not a query
failure. Swallowing it here into a raise would turn "no reference
image yet" into a crash on every field early in the survey. So `call`
raises only for `exit_code >= 64`, exactly the threshold `rapid_db.py`
itself documents as failure; code 7 passes through with the method's
own return value (typically `None` or `{}`), and it is still each call
site's job to decide what an absent record means for it.
"""

import functools
from typing import Any, Callable, TypeVar

#: The exit_code threshold `rapid_db.py` documents as failure. Below this,
#: including the "no record found" convention (7), a call answered; at or
#: above it, the call did not.
FAILURE_THRESHOLD = 64

T = TypeVar("T")


class RapidDBCallFailed(RuntimeError):
    """A `CheckedHandle`-mediated call reported `exit_code >= 64`.

    Carries the method name and the code so a caller three frames away
    does not have to re-derive which query failed from a bare message
    string. `code` is `handle.exit_code` at the moment the call was
    checked — since `RAPIDDB` mutates one shared attribute per
    instance rather than returning a code per call, this is the value
    read immediately after the wrapped method returned, before any
    other call could overwrite it.
    """

    def __init__(self, method: str, code: int):
        self.method = method
        self.code = code
        super().__init__(
            f"{method} failed: rapid_db exit_code {code}")


class CheckedHandle:
    """A `RAPIDDB` handle wrapped so every call either answers or raises.

    Every attribute access that resolves to a bound method on the
    wrapped handle returns a wrapper: call it, read `exit_code`
    immediately after, raise `RapidDBCallFailed(method, code)` if it is
    `>= FAILURE_THRESHOLD`, otherwise return the call's own result
    unchanged (including a `None` or empty result for code 7 — see
    module docstring). Non-callable attributes (`exit_code` itself,
    `conn`, ...) pass through unchecked, so `getattr(checked,
    "exit_code", 0)` still reads the live value for a caller that has
    not yet been converted.

    `handle` is stored, never copied: `CheckedHandle` adds no state of
    its own beyond the reference, so two wrappers over the same handle
    still see the one shared `exit_code`, exactly as two callers of the
    raw handle would.
    """

    def __init__(self, handle: Any):
        self._handle = handle

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._handle, name)
        if not callable(attr):
            return attr

        @functools.wraps(attr)
        def checked_call(*args: Any, **kwargs: Any) -> Any:
            result = attr(*args, **kwargs)
            code = getattr(self._handle, "exit_code", 0)
            if code >= FAILURE_THRESHOLD:
                raise RapidDBCallFailed(name, code)
            return result

        return checked_call

    @property
    def exit_code(self) -> int:
        """The wrapped handle's own `exit_code`, read live, never cached."""
        return getattr(self._handle, "exit_code", 0)
