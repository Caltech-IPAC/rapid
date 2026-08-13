"""Stub-tier tests for `pipeline.association.watermark`.

Everything else in this module — the lane lease, the CAS advance, the
row-wise comparison — is PostgreSQL behaviour and belongs in the contract
tier (`pipeline/contract/test_association_watermark.py`), not here; a fake
cursor evaluating its own CAS predicate would be a tautology, exactly as
that suite's own docstring says.

`schema_present` is different: it is one `to_regclass` SELECT and an
`is not None` check on the result, with no server-side semantics a fake
could get wrong by being fake. That is what makes it the one function in
this module worth a no-I/O unit test — the crossmatch route's own
defense-in-depth probe, mirroring
`AlertOutboxRepository.outbox_schema_present`'s own coverage.
"""

import unittest

from pipeline.association import watermark


class _FakeCursor:
    """Records the statement and answers a fixed `fetchone()` row."""

    def __init__(self, row):
        self._row = row
        self.statements = []

    def execute(self, statement, params=None):
        self.statements.append((statement, params))

    def fetchone(self):
        return self._row


class SchemaPresentTests(unittest.TestCase):

    def test_a_real_regclass_reports_the_schema_present(self):
        # `to_regclass` returns the object's oid-printing text form for a
        # table that exists — never NULL, never absent from the row.
        cursor = _FakeCursor(("association_watermarks",))

        self.assertTrue(watermark.schema_present(cursor))

    def test_a_null_regclass_reports_the_schema_absent(self):
        # `to_regclass` returns SQL NULL, which psycopg2 hands back as
        # `None`, for a name that does not resolve to any relation — the
        # ordinary answer on a database behind migration 049.
        cursor = _FakeCursor((None,))

        self.assertFalse(watermark.schema_present(cursor))

    def test_probes_the_catalog_rather_than_reading_the_table(self):
        cursor = _FakeCursor(("association_watermarks",))

        watermark.schema_present(cursor)

        [(statement, params)] = cursor.statements
        self.assertEqual(statement, watermark._SCHEMA_PROBE)
        self.assertIsNone(params)


if __name__ == "__main__":
    unittest.main()
