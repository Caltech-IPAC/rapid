"""
File:    test_lock_order.py

Tests for `pipeline.runtime.lock_order` (campaign ruling C3): the three
advisory-lock namespaces and their two-level order, extracted here from
`pipeline.reconciler.lease`, `pipeline.registration.consumer`, and
`pipeline.intent.lock` — this file pins that the three constants those
modules still re-export are the SAME values as this module's canonical
ones, so a future edit to one cannot silently drift from the others, and
exercises the two acquisition helpers against fake cursor/executor shapes.
"""

import unittest

from pipeline.runtime import lock_order


class NamespaceTests(unittest.TestCase):
    def test_the_three_namespaces_are_distinct(self):
        values = (lock_order.RECONCILER_LEASE_NAMESPACE,
                  lock_order.REGISTRAR_LEASE_NAMESPACE,
                  lock_order.WORK_UNIT_NAMESPACE)
        self.assertEqual(3, len(set(values)),
                         "the three namespaces must never collide")

    def test_the_two_level_1_namespaces_are_grouped(self):
        self.assertEqual(
            (lock_order.RECONCILER_LEASE_NAMESPACE,
             lock_order.REGISTRAR_LEASE_NAMESPACE),
            lock_order.ATTEMPT_LEASE_NAMESPACES)

    def test_every_namespace_has_a_level_name(self):
        for namespace in (lock_order.RECONCILER_LEASE_NAMESPACE,
                          lock_order.REGISTRAR_LEASE_NAMESPACE,
                          lock_order.WORK_UNIT_NAMESPACE):
            self.assertIn(namespace, lock_order.LEVEL_NAME)

    def test_the_reexported_constants_agree_with_the_canonical_ones(self):
        """The three adopters keep their own constant NAME (so no importer
        of theirs needs to change) but the VALUE must be this module's —
        anything else would mean the extraction introduced drift rather
        than removing it.
        """
        from pipeline.intent.lock import WORK_UNIT_NAMESPACE as intent_wu
        from pipeline.reconciler.lease import LEASE_NAMESPACE as reconciler_w6
        from pipeline.registration.consumer import (
            ATTEMPT_LEASE_NAMESPACE as registrar_r4)

        self.assertIs(intent_wu, lock_order.WORK_UNIT_NAMESPACE)
        self.assertIs(reconciler_w6, lock_order.RECONCILER_LEASE_NAMESPACE)
        self.assertIs(registrar_r4, lock_order.REGISTRAR_LEASE_NAMESPACE)


class _FakeCursor:
    """Captures `execute` calls; `fetchone` answers a scripted row."""

    def __init__(self, fetchone_result=(True,)):
        self.calls = []
        self._fetchone_result = fetchone_result

    def execute(self, statement, params):
        self.calls.append((statement, params))

    def fetchone(self):
        return self._fetchone_result


class AcquireBlockingTests(unittest.TestCase):
    def test_over_a_cursor_calls_execute_with_the_namespace_and_key(self):
        cursor = _FakeCursor()
        lock_order.acquire_blocking(cursor, lock_order.WORK_UNIT_NAMESPACE, 42)
        self.assertEqual(1, len(cursor.calls))
        statement, params = cursor.calls[0]
        self.assertIn("pg_advisory_xact_lock", statement)
        self.assertEqual((lock_order.WORK_UNIT_NAMESPACE, 42), params)

    def test_over_a_bare_executor_callable(self):
        calls = []

        def execute(statement, params):
            calls.append((statement, params))

        lock_order.acquire_blocking(execute, lock_order.WORK_UNIT_NAMESPACE, 7)
        self.assertEqual(1, len(calls))
        statement, params = calls[0]
        self.assertIn("pg_advisory_xact_lock", statement)
        self.assertEqual((lock_order.WORK_UNIT_NAMESPACE, 7), params)

    def test_key_is_coerced_to_int(self):
        cursor = _FakeCursor()
        lock_order.acquire_blocking(cursor, lock_order.WORK_UNIT_NAMESPACE,
                                    "42")
        _statement, params = cursor.calls[0]
        self.assertEqual(42, params[1])
        self.assertIsInstance(params[1], int)


class TryAcquireTests(unittest.TestCase):
    def test_returns_true_when_the_row_says_so(self):
        cursor = _FakeCursor(fetchone_result=(True,))
        acquired = lock_order.try_acquire(
            cursor, lock_order.RECONCILER_LEASE_NAMESPACE, 1)
        self.assertTrue(acquired)
        statement, params = cursor.calls[0]
        self.assertIn("pg_try_advisory_xact_lock", statement)
        self.assertEqual((lock_order.RECONCILER_LEASE_NAMESPACE, 1), params)

    def test_returns_false_when_the_row_says_so(self):
        cursor = _FakeCursor(fetchone_result=(False,))
        self.assertFalse(
            lock_order.try_acquire(cursor, lock_order.REGISTRAR_LEASE_NAMESPACE,
                                   1))

    def test_returns_false_when_no_row_is_returned(self):
        cursor = _FakeCursor(fetchone_result=None)
        self.assertFalse(
            lock_order.try_acquire(cursor, lock_order.WORK_UNIT_NAMESPACE, 1))


if __name__ == "__main__":
    unittest.main()
