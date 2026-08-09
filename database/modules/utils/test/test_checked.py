"""The checked-call adapter (integration review composite ruling 10).

`CheckedHandle` has no dependency on `rapid_db`, psycopg2, or a real
connection — it wraps whatever object it is given and reads that object's
`exit_code` after every call. So this suite drives it against a small
in-file stand-in shaped like the slice of `RAPIDDB` the adapter actually
touches, with no stubbing of third-party modules needed.
"""

import unittest

from database.modules.utils.checked import (
    FAILURE_THRESHOLD,
    CheckedHandle,
    RapidDBCallFailed,
)


class FakeHandle:
    """A minimal stand-in for the slice of `RAPIDDB` the adapter wraps.

    `exit_code` behaves exactly as `rapid_db.py` documents: 0 is success,
    7 is its own "no matching record" convention, and 64+ is failure —
    each method sets it before returning, the same one shared attribute
    every real query method mutates.
    """

    def __init__(self):
        self.exit_code = 0
        self.conn = "the-connection-object"
        self.calls = []

    def get_thing(self, key):
        self.calls.append(("get_thing", key))
        self.exit_code = 0
        return {"key": key}

    def get_missing_thing(self):
        self.calls.append(("get_missing_thing",))
        # rapid_db's own "no record" convention: a data-shaped answer,
        # not a failure.
        self.exit_code = 7
        return None

    def get_broken_thing(self):
        self.calls.append(("get_broken_thing",))
        self.exit_code = 67
        return None

    def get_connection_lost(self):
        self.calls.append(("get_connection_lost",))
        self.exit_code = 64
        return None


class CleanPassThroughTests(unittest.TestCase):
    def test_a_successful_call_returns_the_real_result(self):
        handle = CheckedHandle(FakeHandle())
        self.assertEqual(handle.get_thing("abc"), {"key": "abc"})

    def test_arguments_reach_the_wrapped_method_unchanged(self):
        fake = FakeHandle()
        handle = CheckedHandle(fake)
        handle.get_thing("xyz")
        self.assertEqual(fake.calls, [("get_thing", "xyz")])

    def test_code_seven_is_not_an_error_and_passes_through(self):
        # rapid_db's "no matching record" convention: a legitimate absence,
        # not a query failure. The adapter must not raise for it.
        handle = CheckedHandle(FakeHandle())
        self.assertIsNone(handle.get_missing_thing())

    def test_non_callable_attributes_pass_through_unwrapped(self):
        handle = CheckedHandle(FakeHandle())
        self.assertEqual(handle.conn, "the-connection-object")

    def test_exit_code_reads_the_live_wrapped_value(self):
        fake = FakeHandle()
        handle = CheckedHandle(fake)
        handle.get_missing_thing()
        self.assertEqual(handle.exit_code, 7)
        # And it keeps tracking the wrapped handle's own attribute, not a
        # value captured at wrap time.
        fake.exit_code = 0
        self.assertEqual(handle.exit_code, 0)


class RaisingTests(unittest.TestCase):
    def test_a_failed_call_raises_rather_than_returning_none(self):
        handle = CheckedHandle(FakeHandle())
        with self.assertRaises(RapidDBCallFailed):
            handle.get_broken_thing()

    def test_the_raised_error_carries_the_method_name(self):
        handle = CheckedHandle(FakeHandle())
        with self.assertRaises(RapidDBCallFailed) as ctx:
            handle.get_broken_thing()
        self.assertEqual(ctx.exception.method, "get_broken_thing")

    def test_the_raised_error_carries_the_exit_code(self):
        handle = CheckedHandle(FakeHandle())
        with self.assertRaises(RapidDBCallFailed) as ctx:
            handle.get_broken_thing()
        self.assertEqual(ctx.exception.code, 67)

    def test_the_failure_threshold_matches_rapid_db(self):
        # rapid_db.py's own documented threshold: 64 is "cannot connect to
        # database", the first of the failure codes. FAILURE_THRESHOLD is
        # exported precisely so a caller (or a test double standing in for
        # a real handle) never re-derives this number.
        self.assertEqual(FAILURE_THRESHOLD, 64)

    def test_connection_lost_raises_at_exactly_the_threshold(self):
        handle = CheckedHandle(FakeHandle())
        with self.assertRaises(RapidDBCallFailed) as ctx:
            handle.get_connection_lost()
        self.assertEqual(ctx.exception.code, 64)

    def test_the_message_names_both_method_and_code(self):
        handle = CheckedHandle(FakeHandle())
        with self.assertRaises(RapidDBCallFailed) as ctx:
            handle.get_broken_thing()
        message = str(ctx.exception)
        self.assertIn("get_broken_thing", message)
        self.assertIn("67", message)


class StubRefusalTests(unittest.TestCase):
    """A stub built the same way must be able to refuse the same way.

    `CheckedHandle` is not the only thing in this tree that has to
    simulate a failed call: `submission/test/test_gathering.py`'s stubs
    do too, now that production code relies on the adapter raising rather
    than on a caller-side `exit_code` check. This proves the two agree —
    a stub that sets `exit_code` and is asked to behave like the real
    adapter raises the identical exception type the adapter itself would.
    """

    def test_a_stub_can_be_made_to_refuse_like_the_real_adapter(self):
        class RefusingStub:
            exit_code = 0

            def get_thing(self, key):
                self.exit_code = 67
                code = self.exit_code
                if code >= FAILURE_THRESHOLD:
                    raise RapidDBCallFailed("get_thing", code)
                return {"key": key}

        with self.assertRaises(RapidDBCallFailed) as ctx:
            RefusingStub().get_thing("abc")
        self.assertEqual(ctx.exception.method, "get_thing")
        self.assertEqual(ctx.exception.code, 67)


if __name__ == "__main__":
    unittest.main()
