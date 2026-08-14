"""`pipeline.operator.registration.run_pass`: the operator's registration
step, and its `store` parameter (2026-08-14) — the GC-fence records store
threaded through to `pipeline.registration.consumer.register_batch`'s own
`store=` fencing argument.

`run_pass` calls `pipeline.seams.run_registration`, which now accepts and
forwards `store` unconditionally (the integration request `run_pass`'s
docstring used to describe has landed). These tests stub `pipeline.seams.
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

    def test_store_none_is_forwarded_as_none(self):
        # The overwhelmingly common case for a caller with no records
        # store (a rehearsal, or a class that does not register): `store`
        # reaches `run_registration` as None, exactly as before this
        # parameter existed.
        calls = []

        def fake_run_registration(conn, register=None, store=None):
            calls.append((conn, register, store))
            return _FakeRun()

        with _install_fake_seams(fake_run_registration):
            verdict = opregistration.run_pass("CONN", register="REG")

        self.assertEqual(calls, [("CONN", "REG", None)])
        self.assertEqual(verdict.registered, 1)

    def test_store_genuinely_reaches_run_registration(self):
        # THE POINT OF THIS PARAMETER: a caller that supplies a records
        # store (the operator's fence store, built by `Operator._register`)
        # must see it forwarded through to `run_registration` — and from
        # there to `register_batch`'s own `store=`, which is what holds the
        # GC bind fence over the attempt. A caller passing `store=None`
        # explicitly is indistinguishable from one that omits it.
        calls = []

        def fake_run_registration(conn, register=None, store=None):
            calls.append((conn, register, store))
            return _FakeRun()

        store = object()
        with _install_fake_seams(fake_run_registration):
            opregistration.run_pass("CONN", register="REG", store=store)

        self.assertEqual(calls, [("CONN", "REG", store)])


if __name__ == "__main__":
    unittest.main()
