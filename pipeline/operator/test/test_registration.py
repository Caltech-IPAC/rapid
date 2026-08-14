"""`pipeline.operator.registration.run_pass`: the operator's registration
step, and its `store` parameter (2026-08-14) — the GC-fence records store
threaded through toward `pipeline.registration.consumer.register_batch`'s
own `store=` fencing argument.

`run_pass` calls `pipeline.seams.run_registration`, which does not accept
`store` as of this wave (that wiring is a filed integration request, see
`run_pass`'s own docstring). These tests stub `pipeline.seams.
run_registration` directly rather than exercising a real database, matching
this module's own dependency shape: `run_pass` never touches SQL itself,
it only calls through to `run_registration` and reduces the result.
"""

import sys
import types
import unittest
from unittest import mock

from pipeline.operator import registration as opregistration


class _FakeRun:
    """The minimal shape `RegistrationVerdict` reads off a `run`."""

    def __init__(self, failed=0, registered=1, skipped=0, deferred=0,
                would_register=0):
        self.failed = failed
        self.registered = registered
        self.skipped = skipped
        self.deferred = deferred
        self.would_register = would_register

    def as_dict(self):
        return {"failed": self.failed, "registered": self.registered,
               "skipped": self.skipped, "deferred": self.deferred,
               "would_register": self.would_register}


def _install_fake_seams(run_registration):
    """Install a fake `pipeline.seams` module carrying only `run_registration`.

    `run_pass` does `from pipeline.seams import run_registration` INSIDE
    the function body, so patching `pipeline.seams.run_registration` in
    `sys.modules` before the call is what a caller of this test actually
    controls — `mock.patch("pipeline.operator.registration.run_registration")`
    would not intercept a deferred import.
    """
    fake = types.ModuleType("pipeline.seams")
    fake.run_registration = run_registration
    return mock.patch.dict(sys.modules, {"pipeline.seams": fake})


class RunPassStoreForwardingTests(unittest.TestCase):

    def test_store_none_calls_run_registration_without_a_store_kwarg_error(self):
        # The overwhelmingly common case today: no caller passes `store` at
        # all (the operator/service wiring that would build one is a
        # separate integration request), so this must behave exactly as it
        # did before this parameter existed.
        calls = []

        def fake_run_registration(conn, register=None, store=None):
            calls.append((conn, register, store))
            return _FakeRun()

        with _install_fake_seams(fake_run_registration):
            verdict = opregistration.run_pass("CONN", register="REG")

        self.assertEqual(calls, [("CONN", "REG", None)])
        self.assertEqual(verdict.registered, 1)

    def test_store_is_forwarded_when_run_registration_accepts_it(self):
        calls = []

        def fake_run_registration(conn, register=None, store=None):
            calls.append((conn, register, store))
            return _FakeRun()

        store = object()
        with _install_fake_seams(fake_run_registration):
            opregistration.run_pass("CONN", register="REG", store=store)

        self.assertEqual(calls, [("CONN", "REG", store)])

    def test_store_falls_back_unfenced_when_run_registration_predates_the_kwarg(self):
        # THE FORWARD-COMPATIBILITY CONTRACT THIS WAVE ADDS. Simulates
        # today's real `pipeline.seams.run_registration`, which has no
        # `store` parameter: a naive unconditional forward would raise
        # TypeError and take down every real registration pass. `run_pass`
        # must retry without `store` instead, and must still return a
        # usable verdict.
        calls = []

        def old_run_registration(conn, register=None):  # no `store` param
            calls.append((conn, register))
            return _FakeRun(registered=3)

        store = object()
        with _install_fake_seams(old_run_registration):
            verdict = opregistration.run_pass("CONN", register="REG",
                                              store=store)

        self.assertEqual(calls, [("CONN", "REG")])
        self.assertEqual(verdict.registered, 3)

    def test_store_none_against_a_run_registration_predating_the_kwarg_never_retries(self):
        # No store, old signature: the first call already succeeds (both
        # signatures accept `register` alone), so there is exactly one call
        # and no TypeError to catch in the first place.
        calls = []

        def old_run_registration(conn, register=None):
            calls.append((conn, register))
            return _FakeRun()

        with _install_fake_seams(old_run_registration):
            opregistration.run_pass("CONN", register="REG")

        self.assertEqual(calls, [("CONN", "REG")])


if __name__ == "__main__":
    unittest.main()
