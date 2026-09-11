"""`unit_provenance`, against a real `ProcessingUnit` for each grain.

**The defect this guards.** `unit_provenance` used to read `unit.exposure`
and `unit.sca` with `getattr(unit, name, None)`, which assumes an undeclared
attribute is ABSENT — a plain `AttributeError`. It is not: `ProcessingUnit`'s
`exposure`/`sca` properties (`submission/manifest.py`) RAISE `SubjectError`,
a `ValueError` subclass, for a grain that does not declare that component.
`getattr`'s default only catches `AttributeError`, so the `SubjectError`
propagated straight through `unit_provenance` and out of the entrypoint —
killing a live catalog-load attempt (job 16919) six milliseconds after it
recorded its start, because catalog-load units are date/SCA-grained and have
no `exposure`. It is the same defect class fixed at a second site the same
night, `pipeline/entrypoints/job.py`'s `_identity_extra` — both sites were
written when every unit carried an exposure/SCA-shaped carrier, and this one
was unreachable for the post-identity job types until that first site was
fixed, because those jobs died before reaching registration.

The fix asks `unit.payload.declares(name)` before reading, so each grain
below is exercised through a REAL `ProcessingUnit` built from the fixtures
`submission/test/payload_fixtures.py` already carries for exactly this
purpose — not a stub standing in for one.
"""

import unittest

from pipeline.registration import facts as registration_facts
from submission.routes import JOB_TYPE_STATISTICS
from submission.test import payload_fixtures as fixtures


class UnitProvenanceGrainTests(unittest.TestCase):
    """One test per COMPONENTS shape `unit_provenance` must survive.

    Each asserts two things: the call does not raise (the regression itself),
    and the exposure/sca keys land exactly where that grain's COMPONENTS say
    they should — present with the right value where declared, absent where
    not. `job_type` is passed as `None` throughout because `unit_provenance`
    does not consult it for these two keys; only `unit.payload.declares(...)`
    does.
    """

    def test_exposure_sca_grain_carries_both(self):
        # science: COMPONENTS = ("exposure", "sca") — the grain the original
        # `getattr` idiom was written for, and the only one it ever worked on.
        unit = fixtures.science_unit(exposure=90001, sca=3)

        provenance = registration_facts.unit_provenance(unit)

        self.assertEqual(provenance["expid"], 90001)
        self.assertEqual(provenance["sca"], 3)

    def test_date_sca_grain_carries_sca_but_not_exposure(self):
        # catalog-load: COMPONENTS = ("proc_date", "sca") — declares sca but
        # not exposure. This is the exact shape of the attempt that died:
        # `unit.exposure` raises `SubjectError` here, so a bare `getattr`
        # would propagate it instead of yielding None.
        unit = fixtures.catalog_load_unit(proc_date="20260812", sca=6)

        provenance = registration_facts.unit_provenance(unit)

        self.assertEqual(provenance["sca"], 6)
        self.assertNotIn("expid", provenance)

    def test_date_field_grain_carries_neither(self):
        # crossmatch: COMPONENTS = ("proc_date", "field") — declares neither
        # exposure nor sca, so both `unit.exposure` and `unit.sca` raise.
        unit = fixtures.crossmatch_unit(proc_date="20260812", field=4242)

        provenance = registration_facts.unit_provenance(unit)

        self.assertNotIn("expid", provenance)
        self.assertNotIn("sca", provenance)

    def test_field_grain_carries_neither(self):
        # statistics: COMPONENTS = ("field",) — the sweep-job shape, likewise
        # missing both identity components.
        unit = fixtures.field_unit(JOB_TYPE_STATISTICS, field=4242)

        provenance = registration_facts.unit_provenance(unit)

        self.assertNotIn("expid", provenance)
        self.assertNotIn("sca", provenance)


if __name__ == "__main__":
    unittest.main()
