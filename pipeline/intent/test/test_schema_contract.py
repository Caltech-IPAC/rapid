"""Stub-tier tests for `pipeline.intent.schema_contract`'s pure Python.

`verify_schema_contract`/`applied_migrations` read a real `schema_migrations`
table and belong to the contract tier
(`pipeline/contract/test_schema_preflight.py`) — a fake executor could not
demonstrate anything about SQL it never truly runs. `ROUTE_MIGRATIONS` and
`required_for_route` are different: composing two tuples has no server-side
semantics, so it is tested here with no database and no I/O.
"""

import unittest

from pipeline.intent.schema_contract import (REQUIRED_MIGRATIONS,
                                              ROUTE_MIGRATIONS,
                                              required_for_route)


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


if __name__ == "__main__":
    unittest.main()
