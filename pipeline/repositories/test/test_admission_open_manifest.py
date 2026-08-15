"""`AdmissionRepository.open_manifest`'s release/scope guard, stub-tier.

Everything else in `pipeline.repositories.admission` is exercised against a
real database in `pipeline/contract/test_admission_repository.py` — that file
states at length why: idempotence and the concurrency convergence are
properties of a real constraint, not of Python. `open_manifest`'s new
`ReleaseScopeConflict` refusal is different: it is a plain SELECT-then-branch
this module writes and runs itself, so a fake connection is enough to prove
the branch fires on the right input and stays quiet on the others — no
`admission_manifests` row shape, trigger or grant is under test here.

**WHY A NEW STUB-TIER FILE.** No stub-tier test existed for anything in
`pipeline/repositories/` before this: every other repository in that package
(`alert_outbox.py`, `products.py`, `association.py`, `diffimages.py`,
`skycatalogs.py`, `data_class.py`) is covered contract-tier only. This file
does not attempt to retrofit that gap for the whole package — only for the
one branch this change adds, which is cheap enough to stub honestly (see
`FakeConnection` below) and cheap enough to run on every commit with no
database in sight.
"""

import unittest

from pipeline.repositories.admission import (AdmissionRepository,
                                             ReleaseScopeConflict)


class FakeConnection:
    """A connection double that answers each statement by its SQL text.

    **STUB-BLIND HONESTY, NOT A GENERIC MOCK.** A double that returned a
    canned row regardless of the query would pass this test whether or not
    `open_manifest` actually asks the right question — so this one is keyed
    on recognisable fragments of the statements `admission.py` is known to
    send (the schema probe, the scope-conflict SELECT, the manifest INSERT),
    and it raises `AssertionError` on anything it does not recognise rather
    than guessing. `statements` records every execute call in order, so a
    test can assert the conflict check ran BEFORE the insert and not merely
    that both ran.

    `existing_scopes` maps a `release_identity` to the OTHER source scopes
    already on record for it — the fake's model of what the scope-conflict
    SELECT would find in `admission_manifests`. `manifest_rows` maps
    `manifest_key` to the row `RETURNING` would hand back on the insert arm,
    standing in for one call's worth of database state; this fake does not
    attempt to model the table across calls, because no test here needs it
    to.
    """

    def __init__(self, existing_scopes=None, manifest_rows=None):
        self.existing_scopes = existing_scopes or {}
        self.manifest_rows = manifest_rows or {}
        self.statements = []

    def cursor(self):
        return _FakeCursor(self)


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self._result = None
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement, params=()):
        self._conn.statements.append((statement, params))
        text = " ".join(statement.split())

        if "to_regclass" in text:
            # The schema-presence probe: DRAFT 051 is always applied in
            # these tests, so admission never refuses on that account.
            self._result = [(True,)]
            self.description = (("?",),)
            return

        if "SELECT DISTINCT source_scope" in text:
            release_identity, requested_scope = params
            others = [scope for scope
                     in self._conn.existing_scopes.get(release_identity, [])
                     if scope != requested_scope]
            self._result = [(scope,) for scope in others]
            self.description = (("?",),)
            return

        if "INSERT INTO admission_manifests" in text:
            manifest_key = params[0]
            row = self._conn.manifest_rows.get(
                manifest_key,
                (1, manifest_key, None, 0))     # unsealed, no entries: the
                                                  # default for a first open
            self._result = [row]
            self.description = (("?",),)
            return

        raise AssertionError("FakeConnection did not recognise: %s" % text)

    def fetchall(self):
        return self._result

    def close(self):
        pass


class OpenManifestScopeGuardTests(unittest.TestCase):
    """`open_manifest` refuses to reuse a release identity across scopes."""

    def test_a_new_release_and_a_new_scope_opens_cleanly(self):
        """The ordinary first-ingest path: nothing is on record yet."""
        conn = FakeConnection()
        repo = AdmissionRepository(conn)

        manifest = repo.open_manifest(
            manifest_key="run-1", source_scope="s3://bucket/prefix",
            release_identity="release-2026-08-15", byte_custody="none")

        self.assertEqual(manifest.manifest_key, "run-1")

    def test_reopening_the_same_manifest_key_and_scope_still_succeeds(self):
        """Idempotent replay is unaffected — same release, same scope.

        The scope-conflict check only ever sees OTHER scopes (its own SQL
        excludes `source_scope = requested_scope`), so a manifest re-opened
        under the release and scope it was already opened with finds nothing
        to conflict with, exactly like the real `admission_manifests` row
        would report for the un-changed case.
        """
        conn = FakeConnection(
            existing_scopes={"release-a": ["socsim-r00340"]})
        repo = AdmissionRepository(conn)

        manifest = repo.open_manifest(
            manifest_key="run-socsim", source_scope="socsim-r00340",
            release_identity="release-a", byte_custody="external-versioned")

        self.assertEqual(manifest.manifest_key, "run-socsim")

    def test_a_different_scope_under_the_same_release_is_refused(self):
        """THE DEFECT THIS GUARD CLOSES, reproduced directly: the backfill's
        release, `provenance-backfill-g0001-2026-08-15`, is on record under
        `socsim-r00340`; a real socsim ingest passing an `s3://` scope while
        that release is still current must be refused, not silently admitted
        under a claim that no longer describes what it means.
        """
        conn = FakeConnection(
            existing_scopes={
                "provenance-backfill-g0001-2026-08-15": ["socsim-r00340"]})
        repo = AdmissionRepository(conn)

        with self.assertRaises(ReleaseScopeConflict) as caught:
            repo.open_manifest(
                manifest_key="run-real-ingest",
                source_scope="s3://rapid-ingest/socsim/",
                release_identity="provenance-backfill-g0001-2026-08-15",
                byte_custody="external-versioned")

        conflict = caught.exception
        self.assertEqual(conflict.release_identity,
                         "provenance-backfill-g0001-2026-08-15")
        self.assertEqual(conflict.existing_scope, "socsim-r00340")
        self.assertEqual(conflict.requested_scope,
                         "s3://rapid-ingest/socsim/")
        self.assertEqual(conflict.error_category,
                         "admission_release_scope_conflict")

    def test_the_refusal_message_names_both_scopes_and_the_release(self):
        """The operator reading this at 3am is the user this message serves:
        it must name what disagreed, not merely that something did, and tell
        them what to do about it.
        """
        conn = FakeConnection(
            existing_scopes={"release-x": ["socsim-r00340"]})
        repo = AdmissionRepository(conn)

        with self.assertRaises(ReleaseScopeConflict) as caught:
            repo.open_manifest(
                manifest_key="run-2", source_scope="s3://bucket/new-prefix",
                release_identity="release-x", byte_custody="none")

        message = str(caught.exception)
        self.assertIn("release-x", message)
        self.assertIn("socsim-r00340", message)
        self.assertIn("s3://bucket/new-prefix", message)
        self.assertIn("set-admission-release", message)

    def test_a_different_release_with_the_same_scope_is_fine(self):
        """The constraint is PER-RELEASE, not per-scope: two different
        releases both describing `socsim-r00340` — an original ingest and a
        deliberate re-release of the same source, say — is not the failure
        this guard exists to catch, and must not be refused.
        """
        conn = FakeConnection(
            existing_scopes={"release-old": ["socsim-r00340"]})
        repo = AdmissionRepository(conn)

        manifest = repo.open_manifest(
            manifest_key="run-3", source_scope="socsim-r00340",
            release_identity="release-new", byte_custody="none")

        self.assertEqual(manifest.manifest_key, "run-3")

    def test_the_conflict_check_runs_before_any_write(self):
        """A refusal must precede any write. Asserted on the statements the
        fake actually recorded: the scope-conflict SELECT ran, and no
        `INSERT INTO admission_manifests` appears anywhere in the log — not
        merely that the method raised, which a version that inserted and
        then raised on the RETURNING row would also satisfy.
        """
        conn = FakeConnection(
            existing_scopes={"release-y": ["socsim-r00340"]})
        repo = AdmissionRepository(conn)

        with self.assertRaises(ReleaseScopeConflict):
            repo.open_manifest(
                manifest_key="run-4", source_scope="s3://bucket/y",
                release_identity="release-y", byte_custody="none")

        texts = [stmt for stmt, _params in conn.statements]
        self.assertTrue(
            any("SELECT DISTINCT source_scope" in stmt for stmt in texts),
            "the scope-conflict check never ran")
        self.assertFalse(
            any("INSERT INTO admission_manifests" in stmt for stmt in texts),
            "the insert ran despite the scope conflict")


if __name__ == "__main__":
    unittest.main()
