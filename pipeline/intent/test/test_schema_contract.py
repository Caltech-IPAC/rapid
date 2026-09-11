"""Stub-tier tests for `pipeline.intent.schema_contract`'s pure Python.

`verify_schema_contract`/`applied_migrations` read a real `schema_migrations`
table and belong to the contract tier
(`pipeline/contract/test_schema_preflight.py`) — a fake executor could not
demonstrate anything about SQL it never truly runs. `ROUTE_MIGRATIONS` and
`required_for_route` are different: composing two tuples has no server-side
semantics, so it is tested here with no database and no I/O.

`REQUIRED_MIGRATIONS_FAILS_CLOSED_ON_NEW_ENTRIES` below is a narrower
exception to that split: it does not attempt to prove `verify_schema_contract`
executes real SQL correctly (the contract tier's job) — it proves the FLOOR
itself, as pure Python data, actually contains the newly added entries and
that the fail-closed branch names each one when it alone is missing. That is
a property of `REQUIRED_MIGRATIONS`'s contents and `verify_schema_contract`'s
control flow, neither of which needs a real connection to demonstrate: the
function's only DB interaction is the one `execute` call inside
`applied_migrations`, which a fake stands in for exactly as
`pipeline/contract/fixture.py`'s real `executor(conn)` does for a live one.
"""

import unittest

from pipeline.intent.schema_contract import (REQUIRED_MIGRATIONS,
                                              ROUTE_MIGRATIONS,
                                              SchemaContractUnmet,
                                              required_for_route,
                                              verify_schema_contract)


class RouteMigrationKeysMatchSubmissionRoutesTests(unittest.TestCase):
    """`ROUTE_MIGRATIONS` is keyed by LITERAL strings, not an import of
    `submission.routes`'s constants (this module stays import-light — see
    `ROUTE_MIGRATIONS`'s own docstring). This test is what catches the two
    drifting apart if either side is ever renamed.
    """

    def test_every_key_is_a_live_submission_routes_job_type(self):
        from submission.routes import JOB_TYPE_ALERT_PRODUCTION, JOB_TYPE_CROSSMATCH

        self.assertEqual(set(ROUTE_MIGRATIONS),
                         {JOB_TYPE_CROSSMATCH, JOB_TYPE_ALERT_PRODUCTION})

    def test_both_routed_migrations_are_implemented_job_types(self):
        # Both routes are unconditionally implemented today (no rollout
        # flag) — see `ROUTE_MIGRATIONS`'s own docstring for why that is
        # exactly why 049/050 live here and not in `REQUIRED_MIGRATIONS`.
        from submission.routes import IMPLEMENTED_JOB_TYPES

        self.assertTrue(set(ROUTE_MIGRATIONS) <= IMPLEMENTED_JOB_TYPES)


class RequiredForRouteTests(unittest.TestCase):

    def test_an_unrouted_job_type_gets_only_the_global_floor(self):
        self.assertEqual(required_for_route("science"), REQUIRED_MIGRATIONS)

    def test_a_routed_job_type_gets_the_floor_plus_its_own(self):
        from submission.routes import JOB_TYPE_CROSSMATCH

        required = required_for_route(JOB_TYPE_CROSSMATCH)

        self.assertEqual(required[:len(REQUIRED_MIGRATIONS)],
                         REQUIRED_MIGRATIONS)
        self.assertEqual(required[len(REQUIRED_MIGRATIONS):],
                         ROUTE_MIGRATIONS[JOB_TYPE_CROSSMATCH])

    def test_the_two_routed_floors_are_disjoint_additions(self):
        # Crossmatch's addition must not leak into alert-production's and
        # vice versa — each route preflights against its OWN migration,
        # never the other route's.
        from submission.routes import (JOB_TYPE_ALERT_PRODUCTION,
                                       JOB_TYPE_CROSSMATCH)

        crossmatch_extra = required_for_route(JOB_TYPE_CROSSMATCH)[
            len(REQUIRED_MIGRATIONS):]
        alert_extra = required_for_route(JOB_TYPE_ALERT_PRODUCTION)[
            len(REQUIRED_MIGRATIONS):]

        crossmatch_names = {name for name, _why in crossmatch_extra}
        alert_names = {name for name, _why in alert_extra}
        self.assertFalse(crossmatch_names & alert_names)


def _fake_executor(applied_filenames):
    """A stand-in for the real `execute(sql, params)` callable.

    `applied_migrations` only ever issues one statement
    (`SELECT filename FROM schema_migrations`) and ignores its params, so the
    fake need not parse SQL at all — it just hands back one row per filename,
    matching the tuple shape a real driver returns for a single-column
    SELECT (see `applied_migrations`'s own tuple/list/dict handling).
    """

    def execute(sql, params):
        return [(name,) for name in applied_filenames]

    return execute


class RequiredMigrationsFailsClosedOnNewEntriesTests(unittest.TestCase):
    """108/109/113/115/116 were added to the floor on this branch. Each one
    must actually make `verify_schema_contract` fail closed when it alone is
    absent — an entry present in the tuple but never exercised by this check
    would be exactly the silent drift the floor exists to catch.
    """

    def _assert_fails_closed_missing_only(self, missing_name):
        present = tuple(name for name, _why in REQUIRED_MIGRATIONS
                        if name != missing_name)
        execute = _fake_executor(present)

        with self.assertRaises(SchemaContractUnmet) as caught:
            verify_schema_contract(execute)

        missing_names = [name for name, _why in caught.exception.missing]
        self.assertEqual(missing_names, [missing_name])

    def test_missing_108_runs_fails_closed(self):
        self._assert_fails_closed_missing_only("108-runs.sql")

    def test_missing_109_run_mutation_functions_fails_closed(self):
        self._assert_fails_closed_missing_only(
            "109-run-mutation-functions.sql")

    def test_missing_113_attempts_resource_usage_fails_closed(self):
        self._assert_fails_closed_missing_only(
            "113-attempts-resource-usage.sql")

    def test_missing_115_product_writers_run_id_fails_closed(self):
        self._assert_fails_closed_missing_only(
            "115-product-writers-run-id.sql")

    def test_missing_116_attempts_cgroup_peak_fails_closed(self):
        self._assert_fails_closed_missing_only(
            "116-attempts-cgroup-peak.sql")

    def test_the_full_floor_passes_when_every_entry_is_present(self):
        # The complement of the five tests above: staging every required
        # migration (nothing withheld) must verify cleanly, so a future typo
        # in one of the `_assert_fails_closed_missing_only` calls above
        # cannot hide behind a floor that never actually passes.
        present = tuple(name for name, _why in REQUIRED_MIGRATIONS)
        execute = _fake_executor(present)

        verified = verify_schema_contract(execute)

        self.assertEqual(verified, len(REQUIRED_MIGRATIONS))


class Migrations114And117AreExcludedFromTheFloorTests(unittest.TestCase):
    """114 granted `rapid_pipeline_write` raw UPDATE on `work_units`; 117, on
    this same branch, revokes that exact grant. Pinning the floor to 114
    would assert a deployment state this branch deliberately reverses, and
    117 itself creates no object this repository's SQL references (a REVOKE
    has no object to point a `reason` string at), which fails the floor's
    own derivation rule stated in the module docstring. Both are therefore
    absent from `REQUIRED_MIGRATIONS` by design, not by omission — this test
    is what would catch either one being added back without that reasoning
    being revisited.
    """

    def test_114_and_117_are_not_in_the_required_floor(self):
        names = {name for name, _why in REQUIRED_MIGRATIONS}
        self.assertNotIn("114-orchestrator-work-unit-update.sql", names)
        self.assertNotIn("117-revoke-114-work-units-update.sql", names)


if __name__ == "__main__":
    unittest.main()
