"""Tests for turning database rows into processing units.

The gatherers take a handle rather than importing `rapid_db` directly, so
these drive the REAL gathering logic — the two-level field/filter loop, the
launcher's two-ppid reference lookup, the absent-not-sentinel rule — against
a stub that returns rows. No database, no monkeypatching.

What is deliberately NOT tested here: whether the queries return the right
rows. That is the database's contract and the live proof's job. What these
cover is everything between a row and a unit, which is where a fact can be
silently dropped, defaulted, or put in a second home.
"""

import unittest

from submission.gathering import (
    GatheringError,
    gather_post_process_units,
    gather_science_units,
    science_facts,
)
from submission.routes import JOB_TYPE_POST_PROCESS


class StubSource:
    """The eleven methods gathering uses, and nothing else.

    Structural, not a mock: it returns rows in the column order the real
    methods select in, so a change to that order breaks these tests the way
    it would break production.
    """

    def __init__(self, **overrides):
        self.exit_code = 0
        self.reference_calls = []
        # (rid, sca, fid, ra0, dec0, ra1..ra4, dec1..dec4)
        self.meta = overrides.get("meta", {
            101: (7, 8, 10.0, -43.0, 10.1, -43.1, 10.2, -43.2,
                  10.3, -43.3, 10.4, -43.4),
            102: (9, 8, 11.0, -44.0, 11.1, -44.1, 11.2, -44.2,
                  11.3, -44.3, 11.4, -44.4),
        })
        # (filename, expid, sca, field, mjdobs, exptime, infobits, status,
        #  vbest, version)
        self.info = overrides.get("info", {
            101: ("s3://in/exp1_sca7.fits", 5001, 7, 4678622, 61680.5,
                  139.8, 0, 1, 1, 1),
            102: ("s3://in/exp2_sca9.fits", 5002, 9, 4678622, 61681.5,
                  139.8, 0, 1, 1, 1),
        })
        self.pairs = overrides.get("pairs", {8: [(4678622, 8, 48)]})
        self.l2files = overrides.get("l2files",
                                     {(4678622, 8): [(101,), (102,)]})
        self.psf = overrides.get("psf", (77, "s3://prod/psf_sca7.fits"))
        self.reference = overrides.get("reference", None)
        self.filter_name = overrides.get("filter_name", "F146")

    def get_field_fid_nframes_records_for_mjdobs_range(
            self, start, end, min_nframes, fid=None):
        return self.pairs.get(fid, [])

    def get_l2files_records_for_datetime_range_field_fid(
            self, start, end, field, fid):
        return self.l2files.get((field, fid), [])

    def get_l2filemeta_record(self, rid):
        return self.meta.get(rid)

    def get_info_for_l2file(self, rid):
        return self.info.get(rid)

    def get_exposure_filter(self, fid):
        return self.filter_name

    def get_best_psf(self, sca, fid):
        return self.psf

    def get_best_reference_image(self, ppid, field, fid):
        self.reference_calls.append(ppid)
        if callable(self.reference):
            return self.reference(self, ppid, field, fid)
        return self.reference


# ---------------------------------------------------------------------------
# science_facts: one row -> one fact set
# ---------------------------------------------------------------------------

class ScienceFactsTests(unittest.TestCase):

    def test_every_fact_traces_to_its_column(self):
        facts = science_facts(StubSource(), 101, field=4678622, fid=8)
        self.assertEqual(facts.rid, 101)
        self.assertEqual(facts.fid, 8)
        self.assertEqual(facts.field, 4678622)
        self.assertEqual(facts.expid, 5001)
        self.assertEqual(facts.mjdobs, 61680.5)
        self.assertEqual(facts.exptime, 139.8)
        self.assertEqual(facts.infobits, 0)
        self.assertEqual(facts.status, 1)
        self.assertEqual(facts.science_image_uri, "s3://in/exp1_sca7.fits")
        self.assertEqual(facts.filter_name, "F146")

    def test_rtid_is_carried_not_looked_up(self):
        # W7 retired the per-source R-tree query; rtid equals field by
        # construction in this tessellation. Re-adding a lookup here would
        # reintroduce exactly what the retirement removed.
        facts = science_facts(StubSource(), 101, field=4678622, fid=8)
        self.assertEqual(facts.rtid, facts.field)

    def test_sky_position_is_all_or_nothing(self):
        facts = science_facts(StubSource(), 101, field=4678622, fid=8)
        self.assertEqual(facts.sky_position["ra0"], 10.0)
        self.assertEqual(facts.sky_position["dec4"], -43.4)
        self.assertEqual(len(facts.sky_position), 10)

    def test_a_half_populated_corner_set_is_absent_entirely(self):
        # A half-populated mapping is worse than an absent one: a consumer
        # reading ra3 without checking would silently use a defaulted value.
        broken = dict(StubSource().meta)
        broken[101] = (7, 8, 10.0, -43.0, None, -43.1, 10.2, -43.2,
                       10.3, -43.3, 10.4, -43.4)
        facts = science_facts(StubSource(meta=broken), 101,
                              field=4678622, fid=8)
        self.assertIsNone(facts.sky_position)

    def test_psf_facts_are_absent_when_no_psf_exists(self):
        facts = science_facts(StubSource(psf=None), 101,
                              field=4678622, fid=8)
        self.assertIsNone(facts.psfid)
        self.assertIsNone(facts.psf_uri)

    def test_absent_facts_are_omitted_not_nulled(self):
        # The adopted absent-not-sentinel rule: a fact never resolved and a
        # fact resolved to nothing are different, and only the first is what
        # an omitted key means.
        facts = science_facts(StubSource(psf=None, reference=None), 101,
                              field=4678622, fid=8)
        self.assertNotIn("psf_uri", facts.to_dict())
        self.assertNotIn("reference_image_uri", facts.to_dict())

    def test_a_missing_metadata_row_raises_rather_than_guessing(self):
        with self.assertRaises(GatheringError) as ctx:
            science_facts(StubSource(meta={}), 101, field=4678622, fid=8)
        self.assertIn("101", str(ctx.exception))

    def test_a_missing_l2files_row_raises(self):
        with self.assertRaises(GatheringError):
            science_facts(StubSource(info={}), 101, field=4678622, fid=8)


# ---------------------------------------------------------------------------
# Reference selection: the launcher's two-ppid lookup
# ---------------------------------------------------------------------------

class ReferenceSelectionTests(unittest.TestCase):

    def _reference_under(self, wanted_ppid):
        def answer(source, ppid, field, fid):
            if ppid == wanted_ppid:
                return {"rfid": 900, "filename": "s3://prod/ref.fits",
                        "infobits": 0, "version": 2}
            source.exit_code = 7          # the documented "none yet" signal
            return None
        return answer

    def test_the_reference_ppid_is_tried_first(self):
        source = StubSource(reference=self._reference_under(12))
        facts = science_facts(source, 101, field=4678622, fid=8,
                              reference_ppid=12, science_ppid=15)
        self.assertEqual(facts.reference_image_id, 900)
        self.assertEqual(source.reference_calls, [12])

    def test_it_falls_back_to_the_science_ppid(self):
        # A reference built incidentally by an earlier science run is
        # registered under the science ppid — the launcher's second call.
        source = StubSource(reference=self._reference_under(15))
        facts = science_facts(source, 101, field=4678622, fid=8,
                              reference_ppid=12, science_ppid=15)
        self.assertEqual(facts.reference_image_id, 900)
        self.assertEqual(source.reference_calls, [12, 15])

    def test_the_matching_ppid_is_recorded_as_provenance(self):
        # Which ppid answered is provenance a consumer needs: a reference
        # built by the reference-image job type and one built incidentally
        # are different objects, and the query does not return the ppid.
        source = StubSource(reference=self._reference_under(15))
        facts = science_facts(source, 101, field=4678622, fid=8,
                              reference_ppid=12, science_ppid=15)
        self.assertEqual(facts.reference_image_ppid, 15)

    def test_no_reference_anywhere_leaves_the_facts_absent(self):
        # Not an error: "no reference exists yet" is a legitimate state, and
        # whether a unit is submittable without one is the job type's call,
        # made by `require`, not gathering's.
        def none_at_all(source, ppid, field, fid):
            source.exit_code = 7
            return None
        source = StubSource(reference=none_at_all)
        facts = science_facts(source, 101, field=4678622, fid=8)
        self.assertIsNone(facts.reference_image_id)

    def test_a_real_query_failure_is_not_read_as_no_reference(self):
        # exit_code 7 means "none yet"; anything else nonzero is a failure
        # and must not silently become an absent reference.
        def broken(source, ppid, field, fid):
            source.exit_code = 64
            return None
        with self.assertRaises(GatheringError) as ctx:
            science_facts(StubSource(reference=broken), 101,
                          field=4678622, fid=8)
        self.assertIn("64", str(ctx.exception))


# ---------------------------------------------------------------------------
# The two-stage gathering loop
# ---------------------------------------------------------------------------

class GatherScienceUnitsTests(unittest.TestCase):

    def _gather(self, **kwargs):
        return list(gather_science_units(
            StubSource(), start="2026-01-01", end="2026-12-31",
            start_mjdobs=61600.0, end_mjdobs=61700.0,
            min_images_to_coadd=10, fids=[8], **kwargs))

    def test_stage_one_takes_one_representative_per_field_filter(self):
        # StageOne builds the reference from the FIRST image in mjdobs/SCA
        # order — the launcher's choice, kept identical so a reference built
        # here is the reference the old chain would have built.
        units = self._gather(make_references=True)
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].facts.rid, 101)

    def test_stage_two_takes_all_the_others(self):
        units = self._gather(make_references=False)
        self.assertEqual([unit.facts.rid for unit in units], [102])

    def test_units_are_keyed_by_exposure_and_sca(self):
        units = self._gather(make_references=True)
        self.assertEqual(units[0].exposure, 5001)
        self.assertEqual(units[0].sca, 7)
        self.assertEqual(units[0].key, "5001/7")

    def test_the_run_scoped_logical_key_is_unique_per_run(self):
        unit = self._gather(make_references=True)[0]
        self.assertNotEqual(unit.logical_job_key("run-a"),
                            unit.logical_job_key("run-b"))

    def test_a_field_with_no_files_is_skipped_not_an_error(self):
        source = StubSource(l2files={})
        units = list(gather_science_units(
            source, start="2026-01-01", end="2026-12-31",
            start_mjdobs=61600.0, end_mjdobs=61700.0,
            min_images_to_coadd=10, fids=[8]))
        self.assertEqual(units, [])

    def test_a_failing_pair_query_raises_rather_than_returning_empty(self):
        # "No ready work" and "the query could not run" are different
        # answers, and a submitter that cannot tell them apart submits
        # nothing and reports success.
        class Broken(StubSource):
            def get_field_fid_nframes_records_for_mjdobs_range(self, *a, **k):
                raise RuntimeError("connection reset")

        with self.assertRaises(GatheringError):
            list(gather_science_units(
                Broken(), start="2026-01-01", end="2026-12-31",
                start_mjdobs=61600.0, end_mjdobs=61700.0,
                min_images_to_coadd=10, fids=[8]))

    def test_a_row_without_an_exposure_id_raises(self):
        info = dict(StubSource().info)
        info[101] = ("s3://in/x.fits", None, 7, 4678622, 61680.5,
                     139.8, 0, 1, 1, 1)
        with self.assertRaises(GatheringError) as ctx:
            list(gather_science_units(
                StubSource(info=info), start="2026-01-01", end="2026-12-31",
                start_mjdobs=61600.0, end_mjdobs=61700.0,
                min_images_to_coadd=10, fids=[8], make_references=True))
        self.assertIn("exposure", str(ctx.exception))


# ---------------------------------------------------------------------------
# Post-process units
# ---------------------------------------------------------------------------

class GatherPostProcessUnitsTests(unittest.TestCase):

    class JobSource:
        def __init__(self, jids, job=None):
            self.jids = jids
            self.job = job

        def get_jids_of_normal_science_pipeline_jobs_for_processing_date(
                self, proc_date):
            return [(jid,) for jid in self.jids]

        def get_job_record(self, jid):
            return self.job

    def test_the_jid_rides_in_fields_not_in_unit_facts(self):
        # Post-process work is keyed by JOB, not by rid. `fields` exists
        # precisely for what `UnitFacts` does not name.
        units = list(gather_post_process_units(
            self.JobSource([4242]), "2026-08-06"))
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].fields["jid"], 4242)
        self.assertEqual(units[0].fields["job_type"], JOB_TYPE_POST_PROCESS)

    def test_exposure_and_sca_come_from_the_job_row_when_available(self):
        units = list(gather_post_process_units(
            self.JobSource([4242], job={"expid": 5001, "sca": 7}),
            "2026-08-06"))
        self.assertEqual((units[0].exposure, units[0].sca), (5001, 7))

    def test_without_a_job_row_the_jid_still_keys_the_unit_uniquely(self):
        # A labelled degenerate case, not a silent default: the jid is
        # unique, so the run-scoped key stays unique, without pretending to
        # know an SCA nobody supplied.
        units = list(gather_post_process_units(
            self.JobSource([4242, 4243]), "2026-08-06"))
        keys = {unit.key for unit in units}
        self.assertEqual(len(keys), 2)


if __name__ == "__main__":
    unittest.main()
