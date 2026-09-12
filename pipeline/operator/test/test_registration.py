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


class AnUncommittedPassIsNotAnOkVerdictTests(unittest.TestCase):
    """A pass that registered nothing must not report "ok".

    `RegistrationVerdict`, not `RegistrationRun`, is what the operator's
    PERIODIC pass reports from (`Operator._register` -> `run_pass` ->
    `service.py`'s `result.registration.exit_code`). It used to copy only
    `failed` off the run, so an attempt that swallowed its database error
    and committed nothing -- which never raises, and so lands in
    `uncommitted` rather than `failed` -- was invisible here:

        run.uncommitted = 20; run.failed = 0
        run.exit_code                       -> 65   (correct)
        RegistrationVerdict(run).exit_code  -> 0    ("ok")

    So the recurring unattended path logged `'verdict': 'ok'` for a pass
    that wrote nothing. That is the 2026-09-11 acceptance-run incident's own
    shape reaching a second, worse place: not a one-off CLI invocation an
    operator reads, but the loop nobody is watching.
    """

    def _run(self, **counts):
        from pipeline.registration.consumer import RegistrationRun
        run = RegistrationRun()
        for name, value in counts.items():
            setattr(run, name, value)
        return run

    def test_an_all_uncommitted_pass_is_a_total_failure(self):
        verdict = opregistration.RegistrationVerdict(
            self._run(registered=0, failed=0, uncommitted=20))

        self.assertNotEqual(opregistration.EXIT_OK, verdict.exit_code,
                            "a pass that registered nothing reported ok")
        self.assertEqual(opregistration.EXIT_TOTAL, verdict.exit_code)
        self.assertEqual("total", verdict.as_dict()["verdict"])
        self.assertTrue(verdict.total_failure)

    def test_a_partly_uncommitted_pass_is_a_partial_failure(self):
        verdict = opregistration.RegistrationVerdict(
            self._run(registered=5, failed=0, uncommitted=3))

        self.assertEqual(opregistration.EXIT_PARTIAL, verdict.exit_code)
        self.assertEqual("partial", verdict.as_dict()["verdict"])
        self.assertTrue(verdict.partial_failure)

    def test_uncommitted_attempts_count_as_attempted(self):
        # `attempted` is "items that got as far as a registration call",
        # and an uncommitted attempt got all the way through one -- it just
        # did not survive its commit. Leaving it out silently undercounted.
        verdict = opregistration.RegistrationVerdict(
            self._run(registered=5, failed=2, uncommitted=3))

        self.assertEqual(10, verdict.attempted)
        self.assertEqual(5, verdict.unsuccessful)

    def test_a_clean_pass_is_still_ok(self):
        verdict = opregistration.RegistrationVerdict(
            self._run(registered=7, failed=0, uncommitted=0))

        self.assertEqual(opregistration.EXIT_OK, verdict.exit_code)
        self.assertEqual("ok", verdict.as_dict()["verdict"])
        self.assertFalse(verdict.total_failure)
        self.assertFalse(verdict.partial_failure)

    def test_the_count_is_carried_into_the_reported_dict(self):
        verdict = opregistration.RegistrationVerdict(
            self._run(registered=0, failed=0, uncommitted=4))

        self.assertEqual(4, verdict.as_dict()["uncommitted"])


if __name__ == "__main__":
    unittest.main()
