"""The supersession tool refuses on anything but proven absence.

The tool exists to terminalize attempts whose cited record object is gone.
Its whole safety rests on one distinction the store makes and the caller must
not blur: `head` returning None means the object is definitively absent, and
`head` raising means the store could not answer. Acting on the second as
though it were the first would flag an attempt whose record is sitting intact
in the bucket — the failure `DEFERRED_STORE_FAULT` was introduced to prevent,
and the reason this class tests refusal before it tests success.

The doubles here can refuse. A store that only ever reports absence, or a
writer that accepts every call, would let all four of these tests pass against
a tool that never checked anything.
"""

import unittest

from pipeline.reconciler import supersede_lost_evidence as tool


class _Store:
    """A records store whose answers are scripted per key."""

    def __init__(self, absent=(), present=(), faulting=()):
        self.absent = set(absent)
        self.present = set(present)
        self.faulting = set(faulting)
        self.puts = []

    def head(self, key):
        if key in self.faulting:
            raise RuntimeError("throttled")
        if key in self.absent:
            return None
        if key in self.present:
            return {"checksum": "sha256:whatever"}
        raise AssertionError(f"the test did not script a head for {key!r}")

    def put_if_absent(self, key, body, content_type=None):
        self.puts.append((key, body))
        return _Put(key)


class _Put:
    def __init__(self, key):
        self.key = key
        self.checksum = "sha256:published"
        self.created = True


class _Writer:
    """Records the flag calls, and refuses the ones the DDL would refuse."""

    def __init__(self):
        self.flagged = []

    def __call__(self, sql, params):
        self.flagged.append((sql, params))
        return 1


def _row(attempt_id=126, key="attempts/records/r/j/attempt-126/seq-0001.json",
         sequence=1, state="terminal_after_start"):
    return {
        "attempt_id": attempt_id,
        "run_id": "fixd-chain-20260806T203403Z",
        "logical_job_id": "fixd-chain-20260806T203403Z_ref_1",
        "lifecycle_state": state,
        "rapid_outcome": "success",
        "product_disposition": "published",
        "terminal_record_key": key,
        "terminal_record_sequence": sequence,
        "terminal_record_checksum": "sha256:gone",
        "scheduler_job_id": "batch-x",
        "scheduler_state": "SUCCEEDED",
        "error_category": None,
        "exposure_id": 1,
        "sca": 1,
        "sky_tile": None,
    }


class EvidenceAbsenceTests(unittest.TestCase):
    """The one distinction everything else depends on."""

    def test_absent_object_is_absence(self):
        store = _Store(absent={"k"})
        self.assertTrue(tool.evidence_is_absent(store, "k"))

    def test_readable_object_is_not_absence(self):
        store = _Store(present={"k"})
        self.assertFalse(tool.evidence_is_absent(store, "k"))

    def test_store_fault_is_not_absence_but_an_error(self):
        """A throttle must not read as "the object is gone"."""
        store = _Store(faulting={"k"})
        with self.assertRaises(RuntimeError):
            tool.evidence_is_absent(store, "k")


class SupersedeRefusalTests(unittest.TestCase):
    """What the tool declines to do, which is the part that matters."""

    def setUp(self):
        self.writer = _Writer()
        self.factory = lambda conn: self.writer

    def test_a_readable_record_is_skipped_not_superseded(self):
        row = _row()
        store = _Store(present={row["terminal_record_key"]})
        report = tool.supersede(None, store, "attempts", row, True,
                                self.factory)
        self.assertEqual(report["action"], "skipped")
        self.assertIn("premise does not hold", report["reason"])
        self.assertEqual(store.puts, [], "wrote a record for a live object")
        self.assertEqual(self.writer.flagged, [], "flagged a live attempt")

    def test_a_store_fault_defers_and_writes_nothing(self):
        row = _row()
        store = _Store(faulting={row["terminal_record_key"]})
        report = tool.supersede(None, store, "attempts", row, True,
                                self.factory)
        self.assertEqual(report["action"], "deferred")
        self.assertEqual(store.puts, [])
        self.assertEqual(self.writer.flagged, [])

    def test_a_row_citing_no_key_is_skipped(self):
        row = _row(key=None)
        store = _Store()
        report = tool.supersede(None, store, "attempts", row, True,
                                self.factory)
        self.assertEqual(report["action"], "skipped")
        self.assertEqual(store.puts, [])
        self.assertEqual(self.writer.flagged, [])

    def test_dry_run_writes_nothing_at_all(self):
        row = _row()
        store = _Store(absent={row["terminal_record_key"]})
        report = tool.supersede(None, store, "attempts", row, False,
                                self.factory)
        self.assertEqual(report["action"], "would-supersede")
        self.assertEqual(report["sequence"], 2)
        self.assertEqual(store.puts, [], "a dry run wrote to the store")
        self.assertEqual(self.writer.flagged, [], "a dry run wrote to the DB")


class SupersedeActionTests(unittest.TestCase):
    """And what it does when absence really is proven."""

    def setUp(self):
        self.writer = _Writer()
        self.factory = lambda conn: self.writer

    def test_absent_record_is_superseded_at_the_next_sequence(self):
        row = _row()
        store = _Store(absent={row["terminal_record_key"]})
        report = tool.supersede(None, store, "attempts", row, True,
                                self.factory)

        self.assertEqual(report["action"], "superseded")
        self.assertEqual(report["landed_sequence"], 2,
                         "the reconciler writes above the row's sequence")
        self.assertEqual(len(store.puts), 1)
        key, _body = store.puts[0]
        self.assertIn("seq-0002.json", key)

        self.assertEqual(len(self.writer.flagged), 1,
                         "the row must be flagged or it stays a candidate")
        sql, params = self.writer.flagged[0]
        self.assertIn("missing_or_contradictory", params)
        self.assertIn("reconciler_materialized = false", sql,
                      "migration 013's check forbids the flag in this state")
        self.assertIn(["postgres", "s3"], params,
                      "the sources list must name the stores compared")

    def test_the_record_cites_the_absent_object_as_rejected(self):
        """"Evidence lost" is recorded, not merely implied."""
        row = _row()
        store = _Store(absent={row["terminal_record_key"]})
        tool.supersede(None, store, "attempts", row, True, self.factory)

        import json
        _key, body = store.puts[0]
        published = json.loads(body.decode("utf-8"))
        self.assertTrue(published["reconciler_first"])
        self.assertTrue(published["reconstructed"])
        self.assertEqual(published["rejected_predecessor"]["reason"], "absent")
        self.assertEqual(published["rejected_predecessor"]["key"],
                         row["terminal_record_key"])
        self.assertEqual(published["reconciliation_classification"], "missing")
        self.assertEqual(published["record_author"], "reconciler")

    def test_no_error_category_is_invented(self):
        """These attempts succeeded; only their evidence is gone.

        The v1 allowlist has no category for lost evidence, and the
        reconciler's own analogous path sets none either. Inventing one
        would assert a failure that did not happen.
        """
        row = _row()
        store = _Store(absent={row["terminal_record_key"]})
        tool.supersede(None, store, "attempts", row, True, self.factory)

        import json
        _key, body = store.puts[0]
        published = json.loads(body.decode("utf-8"))
        self.assertIsNone(published.get("error_category"))


if __name__ == "__main__":
    unittest.main()
