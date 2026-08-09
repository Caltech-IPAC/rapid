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

import hashlib
import os
import unittest
from unittest import mock

from submission import gathering
from submission.gathering import (
    GatheringError,
    gather_alert_production_units,
    gather_catalog_load_units,
    gather_crossmatch_units,
    gather_merge_currency_units,
    gather_merge_dedup_units,
    gather_post_process_units,
    gather_science_units,
    gather_source_currency_units,
    gather_statistics_units,
    initialize_alert_watermark,
    science_facts,
)
from submission.manifest import UnitFacts
from submission.routes import (
    JOB_TYPE_ALERT_PRODUCTION,
    JOB_TYPE_CATALOG_LOAD,
    JOB_TYPE_CROSSMATCH,
    JOB_TYPE_MERGE_CURRENCY,
    JOB_TYPE_MERGE_DEDUP,
    JOB_TYPE_POST_PROCESS,
    JOB_TYPE_SCIENCE,
    JOB_TYPE_SOURCE_CURRENCY,
    JOB_TYPE_STATISTICS,
)


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
        self.assertEqual(units[0].key, "005001/07")

    def test_the_run_scoped_logical_key_is_unique_per_run(self):
        unit = self._gather(make_references=True)[0]
        self.assertNotEqual(unit.logical_job_key("run-a", JOB_TYPE_SCIENCE),
                            unit.logical_job_key("run-b", JOB_TYPE_SCIENCE))

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


class PostProcessFactsTests(unittest.TestCase):
    """The facts post-process stages require, from real queries.

    `UnitFacts()` with no arguments used to be yielded — no product URIs, no
    database identities — while `stamp_reference_image` requires
    `reference_image_uri` and `stamp_difference_image` requires
    `difference_image_uri`, `pid`, `rid`, `expid`, `fid` and `field` as its
    first act. Every post-process job would have failed `input_missing` before
    stamping either product.
    """

    class Source:
        exit_code = 0

        def __init__(self, difference=None, reference=None, failure=None):
            self.difference = difference if difference is not None else {}
            self.reference = reference if reference is not None else {}
            #: The exit_code the query leaves behind. 7 is the documented "no
            #: best record" signal; anything else nonzero is a real failure.
            self.failure = failure

        def get_best_difference_image(self, rid, ppid):
            if self.failure is not None:
                self.exit_code = self.failure
                return {}
            if not self.difference:
                self.exit_code = 7
            return self.difference

        def get_reference_image(self, rfid):
            return self.reference

    JOB = (5001, 7, 4678636, 1, 42, 15, 1, 0)   # expid, sca, field, fid, rid…

    def _facts(self, **kwargs):
        return gathering.post_process_facts(self.Source(**kwargs), self.JOB)

    def test_the_job_row_supplies_the_units_own_identity(self):
        # No `sca` here: it identifies the processing UNIT, not the facts,
        # and `_job_identity` reads it from the same row for that purpose.
        facts = self._facts()
        self.assertEqual((facts.expid, facts.field, facts.fid, facts.rid),
                         (5001, 4678636, 1, 42))

    def test_the_difference_image_and_its_pid_come_from_diffimages(self):
        facts = self._facts(difference={
            "pid": 900, "rfid": 12, "filename": "s3://p/diff.fits",
            "infobitssci": 4, "version": 3})

        self.assertEqual(facts.pid, 900)
        self.assertEqual(facts.difference_image_uri, "s3://p/diff.fits")
        self.assertEqual(facts.infobits, 4)
        self.assertEqual(facts.difference_image_version, 3)

    def test_the_reference_is_the_one_this_difference_was_made_against(self):
        # By rfid, not by a fresh field/filter lookup: looking one up could
        # return a NEWER reference than the one actually differenced against.
        facts = self._facts(
            difference={"pid": 900, "rfid": 12,
                        "filename": "s3://p/diff.fits"},
            reference={"rfid": 12, "filename": "s3://p/ref.fits",
                       "infobits": 2, "version": 5})

        self.assertEqual(facts.reference_image_id, 12)
        self.assertEqual(facts.reference_image_uri, "s3://p/ref.fits")
        self.assertEqual(facts.reference_image_infobits, 2)
        self.assertEqual(facts.reference_image_version, 5)

    def test_no_difference_image_leaves_those_facts_absent(self):
        # Absent, not defaulted: `UnitFacts.require` turns absence into one
        # named failure at startup rather than a header stamped with a zero.
        facts = self._facts()

        self.assertIsNone(facts.pid)
        self.assertIsNone(facts.difference_image_uri)
        self.assertEqual(facts.rid, 42)

    def test_no_job_row_yields_empty_facts_rather_than_raising(self):
        self.assertEqual(UnitFacts(),
                         gathering.post_process_facts(self.Source(), None))

    def test_a_real_query_failure_is_not_read_as_no_difference_image(self):
        # exit_code 7 means "no best record"; 67 means the query failed. A
        # gatherer that cannot tell them apart yields a unit with no products
        # and reports success.
        with self.assertRaises(gathering.GatheringError):
            gathering.post_process_facts(self.Source(failure=67), self.JOB)


class FakeConditionalS3:
    """Just enough S3 to exercise the conditional put, semantics not calls.

    It genuinely refuses to overwrite when `IfNoneMatch="*"` is sent, and
    genuinely serves the existing bytes back on `get_object` — so a test that
    passes here is a test of publish-once behaviour rather than of which
    keyword arguments were assembled. A store that only recorded the call
    could not tell the replay case from the collision case at all, and those
    are the two outcomes that matter.

    `bare_exception` raises `PreconditionFailed` with no `response` mapping,
    which is what a stubbed client and moto actually do — the shape the
    stricter code-only predicate used to miss.
    """

    def __init__(self, bare_exception=False):
        self.objects = {}
        self.put_kwargs = []
        self._bare = bare_exception

    def put_object(self, Bucket, Key, Body, ContentType=None, **kwargs):
        self.put_kwargs.append({"Bucket": Bucket, "Key": Key,
                                "ContentType": ContentType, **kwargs})
        if kwargs.get("IfNoneMatch") == "*" and Key in self.objects:
            raise self._refusal()
        self.objects[Key] = Body

    def get_object(self, Bucket, Key):
        return {"Body": _Body(self.objects[Key])}

    def _refusal(self):
        if self._bare:
            return PreconditionFailed("the key is taken")
        return _ClientError("PreconditionFailed")


class PreconditionFailed(Exception):
    """A refusal identifiable only by its class name — no `response` dict."""


class _ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _Body:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


class CoaddInputsTests(unittest.TestCase):
    """Reference units carry the coadd inputs their first stage requires.

    `gather_science_units(make_references=True)` yields the representative
    image per (field, filter) but NOT `coadd_inputs_uri` — which
    `reference_image.download_inputs` requires as its first act, so every
    reference job it produced failed `input_missing` before doing any work.
    """

    SKY = {"ra0": 10.0, "dec0": -5.0, "ra1": 10.1, "dec1": -5.1,
           "ra2": 10.2, "dec2": -5.2, "ra3": 10.3, "dec3": -5.3,
           "ra4": 10.4, "dec4": -5.4}

    class Source:
        def __init__(self, overlapping=(), info=None, overlap_failure=None):
            self.exit_code = 0
            self.overlapping = list(overlapping)
            self.info = info or {}
            #: The exit_code the overlap query leaves behind. `rapid_db`
            #: reports a failed query as 67 and returns None SILENTLY, so a
            #: stub that only ever returns rows cannot exercise the path
            #: that mattered.
            self.overlap_failure = overlap_failure
            #: (rid, mjdobs) as the query actually received them — the two
            #: arguments whose values ARE the query's semantics.
            self.overlap_calls = []
            #: (start_mjdobs, end_mjdobs) per call: since O1 the observation
            #: window is passed rather than read from the environment, so
            #: what arrives here is the whole of what selects the frames.
            self.overlap_windows = []

        def get_overlapping_l2files(self, rid, fid, mjdobs, *corners,
                                    radius_of_initial_cone_search=None,
                                    start_mjdobs=None, end_mjdobs=None):
            self.overlap_calls.append((rid, mjdobs))
            self.overlap_windows.append((start_mjdobs, end_mjdobs))
            if self.overlap_failure is not None:
                self.exit_code = self.overlap_failure
                return None
            return self.overlapping

        def get_info_for_l2file(self, rid):
            return self.info.get(rid)

    def _overlap_row(self, rid, field=4678636):
        return [rid, 10.0, -5.0, 10.1, -5.1, 10.2, -5.2, 10.3, -5.3,
                10.4, -5.4, field, 0.01]

    def _info(self, filename, field=4678636, status=1, vbest=1):
        # filename, expid, sca, field, mjdobs, exptime, infobits, status,
        # vbest, version
        return (filename, 2, 1, field, 61679.08, 66.4, 0, status, vbest, 1)

    def test_the_rows_carry_the_columns_the_coadd_reader_parses(self):
        source = self.Source(
            overlapping=[self._overlap_row(1), self._overlap_row(2)],
            info={1: self._info("a.fits"), 2: self._info("b.fits")})

        rows = gathering.coadd_input_rows(
            source, rid=9, fid=1, mjdobs=61679.1, sky_position=self.SKY,
            min_images_to_coadd=2)

        self.assertEqual(len(rows), 2)
        self.assertEqual(len(rows[0]), len(gathering.COADD_INPUT_COLUMNS))
        self.assertEqual(rows[0][0], 1)
        self.assertEqual(rows[0][11], "a.fits")

    def test_bad_and_superseded_files_are_excluded(self):
        # status == 0 is a file marked bad; vbest == 0 is a superseded
        # version. Coadding either would build a reference from images the
        # database says not to use.
        source = self.Source(
            overlapping=[self._overlap_row(1), self._overlap_row(2),
                         self._overlap_row(3)],
            info={1: self._info("a.fits"),
                  2: self._info("b.fits", status=0),
                  3: self._info("c.fits", vbest=0)})

        rows = gathering.coadd_input_rows(
            source, rid=9, fid=1, mjdobs=61679.1, sky_position=self.SKY,
            min_images_to_coadd=1)

        self.assertEqual([row[11] for row in rows], ["a.fits"])

    def test_too_few_inputs_is_an_error_not_a_short_list(self):
        source = self.Source(overlapping=[self._overlap_row(1)],
                             info={1: self._info("a.fits")})

        with self.assertRaises(gathering.NotReadyYet):
            gathering.coadd_input_rows(
                source, rid=9, fid=1, mjdobs=61679.1, sky_position=self.SKY,
                min_images_to_coadd=5)

    def test_the_overlap_query_runs_over_the_open_window(self):
        # `mjdobs` is the EXCLUSIVE upper bound of the query's own window,
        # not a description of the representative image. The representative
        # is the earliest frame in time order, so forwarding its mjdobs asks
        # for frames observed strictly before the earliest frame — empty by
        # construction, which is why every field looked unready. The deleted
        # launcher passed 999999.9 to mean "everything ever observed"; so do
        # we, and the caller's mjdobs must not reach the query.
        source = self.Source(
            overlapping=[self._overlap_row(1), self._overlap_row(2)],
            info={1: self._info("a.fits"), 2: self._info("b.fits")})

        gathering.coadd_input_rows(
            source, rid=9, fid=1, mjdobs=61679.1, sky_position=self.SKY,
            min_images_to_coadd=2)

        _, mjdobs = source.overlap_calls[0]
        self.assertEqual(mjdobs, gathering.REFERENCE_OVERLAP_OPEN_MJDOBS)
        self.assertNotEqual(mjdobs, 61679.1)

    def test_the_window_comes_from_release_content_by_default(self):
        # O1: the window used to be read inside `rapid_db` from
        # STARTREFIMMJDOBS/ENDREFIMMJDOBS. It is release content now, and
        # arrives at the query as a passed pair — so a process with those
        # variables set in its environment cannot change which frames a
        # reference image is built from.
        source = self.Source(
            overlapping=[self._overlap_row(1), self._overlap_row(2)],
            info={1: self._info("a.fits"), 2: self._info("b.fits")})

        # RAPID_SW points at this checkout so the release content read is
        # the repo's own `cdf/science/pipeline.toml`, not whatever tree the
        # runner happens to be in.
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        with mock.patch.dict(os.environ,
                             {"RAPID_SW": repo_root,
                              "STARTREFIMMJDOBS": "1.0",
                              "ENDREFIMMJDOBS": "2.0"}):
            gathering.coadd_input_rows(
                source, rid=9, fid=1, mjdobs=61679.1, sky_position=self.SKY,
                min_images_to_coadd=2)
            release_window = gathering.reference_observation_window()

        window = source.overlap_windows[0]
        self.assertEqual(window, release_window)
        # The retired variables have no effect: they are set here precisely
        # so that a regression restoring the environment read fails.
        self.assertNotEqual(window, (1.0, 2.0))

    def test_an_explicit_window_is_what_reaches_the_query(self):
        # The manifest override's path: `gather_reference_units` resolves it
        # once and hands it down, so every unit of one submission is built
        # against one window.
        source = self.Source(
            overlapping=[self._overlap_row(1), self._overlap_row(2)],
            info={1: self._info("a.fits"), 2: self._info("b.fits")})

        gathering.coadd_input_rows(
            source, rid=9, fid=1, mjdobs=61679.1, sky_position=self.SKY,
            min_images_to_coadd=2, window=(60000.0, 60100.0))

        self.assertEqual(source.overlap_windows[0], (60000.0, 60100.0))

    def test_the_representative_image_is_not_excluded_from_its_own_coadd(self):
        # A real rid in the tail parameter renders as `a.rid != %s` and drops
        # the representative from the coadd it is an input to. None takes the
        # other branch, which emits no exclusion clause at all.
        #
        # It was the string 'null' until round-4 finding #3: that selected an
        # `a.rid is not %s` branch which excluded nothing only by accident,
        # and which — once the query was parameterized — sent PostgreSQL the
        # invalid `a.rid IS NOT 'null'`. What that branch says is now checked
        # against a real server by `live_fixe_overlap_sql`, because a stubbed
        # source like this one cannot fail the way the defect failed.
        source = self.Source(overlapping=[self._overlap_row(1)],
                             info={1: self._info("a.fits")})

        gathering.coadd_input_rows(
            source, rid=9, fid=1, mjdobs=61679.1, sky_position=self.SKY,
            min_images_to_coadd=1)

        rid, _ = source.overlap_calls[0]
        self.assertEqual(rid, gathering.REFERENCE_OVERLAP_NO_EXCLUSION)
        self.assertNotEqual(rid, 9)

    def test_exactly_min_images_to_coadd_inputs_is_enough(self):
        # The off-by-one this closes. `min_n_images_to_coadd` is 2 in the
        # release content, and with the representative excluded a field
        # holding exactly two good frames returned one row and was skipped
        # every night for the life of the survey.
        source = self.Source(
            overlapping=[self._overlap_row(1), self._overlap_row(2)],
            info={1: self._info("a.fits"), 2: self._info("b.fits")})

        rows = gathering.coadd_input_rows(
            source, rid=1, fid=1, mjdobs=61679.1, sky_position=self.SKY,
            min_images_to_coadd=2)

        self.assertEqual([row[11] for row in rows], ["a.fits", "b.fits"])

    def test_a_failed_overlap_query_raises_rather_than_reading_as_unready(self):
        # `get_overlapping_l2files` reports failure by setting exit_code 67
        # and returning None SILENTLY — no exception, so the try/except that
        # used to guard this call never saw it. None then iterated as nothing
        # and a database outage was reported as "this field is not ready".
        # The deleted launcher checked `>= 64` after every such call.
        source = self.Source(overlap_failure=67)

        with self.assertRaises(gathering.GatheringError) as ctx:
            gathering.coadd_input_rows(
                source, rid=9, fid=1, mjdobs=61679.1, sky_position=self.SKY,
                min_images_to_coadd=2)

        self.assertNotIsInstance(ctx.exception, gathering.NotReadyYet)
        self.assertIn("67", str(ctx.exception))

    def test_disagreeing_field_identities_refuse_to_coadd(self):
        source = self.Source(
            overlapping=[self._overlap_row(1, field=111)],
            info={1: self._info("a.fits", field=222)})

        with self.assertRaises(gathering.GatheringError):
            gathering.coadd_input_rows(
                source, rid=9, fid=1, mjdobs=61679.1, sky_position=self.SKY,
                min_images_to_coadd=1)

    def test_an_incomplete_sky_position_raises_rather_than_cone_searching(self):
        with self.assertRaises(gathering.GatheringError):
            gathering.coadd_input_rows(
                self.Source(), rid=9, fid=1, mjdobs=61679.1,
                sky_position={"ra0": 10.0}, min_images_to_coadd=1)

    def test_publishing_returns_the_uri_the_stage_downloads(self):
        s3 = FakeConditionalS3()
        uri, _checksum = gathering.publish_coadd_inputs(
            s3, "job-info", "coadd-inputs/run-1/u/in.csv",
            [[1, "a.fits"], [2, "b.fits"]])

        self.assertEqual(uri, "s3://job-info/coadd-inputs/run-1/u/in.csv")
        self.assertEqual(s3.objects["coadd-inputs/run-1/u/in.csv"],
                         b"1,a.fits\n2,b.fits\n")

    def test_a_failed_publish_raises_rather_than_yielding_a_dead_uri(self):
        class Broken:
            def put_object(self, **_kwargs):
                raise RuntimeError("AccessDenied")

        with self.assertRaises(gathering.GatheringError):
            gathering.publish_coadd_inputs(Broken(), "b", "k", [[1]])

    # -- write-once (review finding #9) --------------------------------------

    def test_the_create_is_conditional_server_side(self):
        """`IfNoneMatch="*"`, so S3 refuses a second writer rather than us
        racing a head-then-put."""
        s3 = FakeConditionalS3()
        gathering.publish_coadd_inputs(s3, "b", "k", [[1, "a.fits"]])

        self.assertEqual(s3.put_kwargs[0]["IfNoneMatch"], "*")

    def test_a_checksum_is_sent_with_the_put(self):
        s3 = FakeConditionalS3()
        gathering.publish_coadd_inputs(s3, "b", "k", [[1, "a.fits"]])

        self.assertIn("ChecksumSHA256", s3.put_kwargs[0])

    def test_the_returned_checksum_is_of_exactly_the_published_bytes(self):
        """What the manifest will cite has to hash the object that exists,
        not the rows it was built from — those are different artifacts."""
        s3 = FakeConditionalS3()
        _uri, digest = gathering.publish_coadd_inputs(
            s3, "b", "k", [[1, "a.fits"], [2, "b.fits"]])

        self.assertEqual(digest, hashlib.sha256(s3.objects["k"]).hexdigest())

    def test_republishing_identical_rows_is_an_ordinary_replay(self):
        """A resumed submission that re-gathers the same overlap set is not a
        defect: the object already there IS the one this pass meant to
        write."""
        s3 = FakeConditionalS3()
        rows = [[1, "a.fits"], [2, "b.fits"]]
        first = gathering.publish_coadd_inputs(s3, "b", "k", rows)
        second = gathering.publish_coadd_inputs(s3, "b", "k", rows)

        self.assertEqual(first, second)

    def test_republishing_different_rows_raises_rather_than_replacing(self):
        """The sharp case. The overlap query returns more frames as the survey
        advances, so a second pass under one run identity genuinely disagrees
        with the first — and units already submitted cite this key."""
        s3 = FakeConditionalS3()
        gathering.publish_coadd_inputs(s3, "b", "k", [[1, "a.fits"]])

        with self.assertRaises(gathering.GatheringError) as ctx:
            gathering.publish_coadd_inputs(
                s3, "b", "k", [[1, "a.fits"], [2, "b.fits"]])
        self.assertIn("DIFFERENT images", str(ctx.exception))

    def test_the_refused_object_keeps_the_first_pass_bytes(self):
        """Refusing has to mean the object is untouched. A version that raised
        after writing would be no better than the unconditional put."""
        s3 = FakeConditionalS3()
        gathering.publish_coadd_inputs(s3, "b", "k", [[1, "a.fits"]])

        with self.assertRaises(gathering.GatheringError):
            gathering.publish_coadd_inputs(s3, "b", "k", [[9, "z.fits"]])

        self.assertEqual(s3.objects["k"], b"1,a.fits\n")

    def test_a_conflict_reported_only_by_exception_type_is_recognized(self):
        """Stubbed clients and moto raise a bare `PreconditionFailed` with no
        `response` mapping. Matching on the error code alone treated that as a
        transport fault, so a replay was reported as a publish failure."""
        s3 = FakeConditionalS3(bare_exception=True)
        rows = [[1, "a.fits"]]
        gathering.publish_coadd_inputs(s3, "b", "k", rows)

        # Does not raise: recognized as the conditional refusal it is.
        uri, _digest = gathering.publish_coadd_inputs(s3, "b", "k", rows)
        self.assertEqual(uri, "s3://b/k")


# ---------------------------------------------------------------------------
# Reference units end to end: which failures are ordinary and which travel
# ---------------------------------------------------------------------------

class GatherReferenceUnitsTests(unittest.TestCase):
    """The catch site, which decides what an operator gets told.

    `gather_reference_units` skips a field that cannot yet support a
    reference image, and must: it is the ordinary state early in the
    survey. It used to skip on `GatheringError`, which is every failure
    this module raises, so an unreachable database produced a night of
    "not yet" at INFO and no reference images.
    """

    class Source(StubSource):
        def __init__(self, overlapping=(), overlap_failure=None, **overrides):
            super().__init__(**overrides)
            self.overlapping = list(overlapping)
            self.overlap_failure = overlap_failure
            self.overlap_windows = []

        def get_overlapping_l2files(self, rid, fid, mjdobs, *corners,
                                    radius_of_initial_cone_search=None,
                                    start_mjdobs=None, end_mjdobs=None):
            self.overlap_windows.append((start_mjdobs, end_mjdobs))
            if self.overlap_failure is not None:
                self.exit_code = self.overlap_failure
                return None
            return self.overlapping

    def _gather(self, source, min_images_to_coadd=2):
        # The shared create-once double, not a permissive local one: a stub
        # that accepted any keyword and overwrote silently would pass whether
        # or not the conditional create survived.
        return list(gathering.gather_reference_units(
            source, "2026-01-01", "2026-12-31",
            start_mjdobs=61600.0, end_mjdobs=61700.0,
            min_images_to_coadd=min_images_to_coadd,
            s3_client=FakeConditionalS3(), job_bucket="job-info",
            run_id="run-1", fids=[8]))

    def _overlap_row(self, rid):
        return [rid, 10.0, -43.0, 10.1, -43.1, 10.2, -43.2, 10.3, -43.3,
                10.4, -43.4, 4678622, 0.01]

    def test_a_field_with_exactly_enough_frames_yields_a_unit(self):
        # min_n_images_to_coadd is 2 in the release content. The
        # representative is rid 101, and it counts itself: with the
        # exclusion removed the query returns both frames, and the field
        # that used to be skipped forever now produces its reference.
        source = self.Source(
            overlapping=[self._overlap_row(101), self._overlap_row(102)])

        units = self._gather(source)

        self.assertEqual([unit.facts.rid for unit in units], [101])
        self.assertTrue(units[0].facts.coadd_inputs_uri.startswith("s3://"))

    def test_a_genuinely_unready_field_is_skipped_not_fatal(self):
        source = self.Source(overlapping=[self._overlap_row(101)])
        self.assertEqual(self._gather(source), [])

    def test_a_null_mjdobs_is_a_named_failure_not_a_bare_type_error(self):
        """`UnitFacts` documents every field as optional, and `science_facts`
        builds `mjdobs` with `_maybe_float` precisely so a NULL
        `L2Files.mjdobs` passes through. Only `rid` was guarded, so
        `float(facts.mjdobs)` raised `TypeError: float() argument must be a
        string or a real number, not 'NoneType'` from inside a three-argument
        call that named neither the field nor the unit.
        """
        info = dict(StubSource().info)
        info[101] = ("s3://in/exp1_sca7.fits", 5001, 7, 4678622, None,
                     139.8, 0, 1, 1, 1)
        source = self.Source(
            info=info,
            overlapping=[self._overlap_row(101), self._overlap_row(102)])

        with self.assertRaises(gathering.GatheringError) as caught:
            self._gather(source)

        self.assertIn("mjdobs", str(caught.exception))

    def test_a_failed_overlap_query_is_not_reported_as_an_unready_field(self):
        # The distinction the narrowed catch exists for. A night in which
        # every field was skipped because the database stopped answering
        # must not read the same as a night early in the survey.
        source = self.Source(overlap_failure=67)

        with self.assertRaises(gathering.GatheringError) as ctx:
            self._gather(source)

        self.assertNotIsInstance(ctx.exception, gathering.NotReadyYet)
        self.assertIn("67", str(ctx.exception))


class PostDbGatheringTests(unittest.TestCase):
    """The post-DB chain's unit enumeration, one job type at a time.

    The property under test is the one the co-design's first ruling is about:
    the work list is built HERE, from operational rows, and each unit is
    individually addressable. Every one of these six scripts used to answer
    the same question at runtime by asking the catalog what tables happened
    to exist — so a unit whose table was missing silently vanished from the
    work list instead of being reported as work not done.
    """

    class Source:
        """The three enumeration methods, and a failure switch for each.

        `exit_code` is how the real `rapid_db` reports failure: it returns
        None and sets a code, WITHOUT raising. A stub that could only succeed
        would never exercise the check that turns that silence into an error,
        which is the defect class this repo has hit before (a failed query
        reading as "no work").
        """

        def __init__(self, scas=(), fields=(), per_field=(), failure=0,
                     products=None, incomplete_catalog_load=()):
            self.scas = list(scas)
            self.fields = list(fields)
            self.per_field = list(per_field)
            self.failure = failure
            self.exit_code = 0
            self.asked_for = []
            # (pid, expid, sca, attempt_id, filename) per SCA — the loader's
            # re-source. Keyed by SCA because the real query is per-SCA, so a
            # gatherer that asked for the wrong one would get the wrong rows
            # here too rather than silently getting the same list.
            self.products = dict(products or {})
            # Which SCAs the durable-state predicate should report as NOT
            # having a completed catalog-load attempt yet. Empty by default
            # (the coverage check passes and crossmatch gathers normally) —
            # a stub that could only report "complete" would never exercise
            # the refusal path, which is exactly the stub-refusal principle
            # this suite's own tests are held to.
            self.incomplete_catalog_load = list(incomplete_catalog_load)

        def get_scas_with_science_jobs_for_processing_date(self, proc_date):
            self.asked_for.append(("scas", proc_date))
            self.exit_code = self.failure
            return None if self.failure else self.scas

        def get_scas_with_incomplete_catalog_load_for_processing_date(
                self, proc_date):
            self.asked_for.append(("incomplete_catalog_load", proc_date))
            self.exit_code = self.failure
            return None if self.failure else self.incomplete_catalog_load

        def get_fields_with_science_jobs_for_processing_date(self, proc_date):
            self.asked_for.append(("fields", proc_date))
            self.exit_code = self.failure
            return None if self.failure else self.fields

        def get_fields_with_per_field_table(self, prototype):
            self.asked_for.append(("per_field", prototype))
            self.exit_code = self.failure
            return None if self.failure else self.per_field

        def get_registered_diffimages_for_processing_date_sca(self, proc_date,
                                                              sca):
            self.asked_for.append(("products", proc_date, sca))
            self.exit_code = self.failure
            return None if self.failure else self.products.get(int(sca), [])

    # -- catalog load: (processing date, SCA) ------------------------------

    def test_catalog_load_enumerates_one_unit_per_sca(self):
        units = list(gather_catalog_load_units(
            self.Source(scas=[1, 2, 18]), "20260808"))

        self.assertEqual(len(units), 3)
        self.assertEqual([u.fields["sca"] for u in units], [1, 2, 18])
        for unit in units:
            self.assertEqual(unit.fields["job_type"], JOB_TYPE_CATALOG_LOAD)
            self.assertEqual(unit.fields["proc_date"], "20260808")

    def test_catalog_load_names_its_target_table_in_the_manifest(self):
        # The declared input. The job type does not build this name from its
        # own environment and hope it matches what was gathered.
        units = list(gather_catalog_load_units(
            self.Source(scas=[7]), "20260808"))

        self.assertEqual(units[0].fields["target_table"], "sources_20260808_7")

    def test_catalog_load_units_key_uniquely(self):
        # `logical_job_key` is run-scoped and built from `unit.key`; two units
        # sharing a key would collide on the logical_jobs primary key.
        units = list(gather_catalog_load_units(
            self.Source(scas=[1, 2, 3]), "20260808"))

        self.assertEqual(len({u.key for u in units}), 3)

    def test_catalog_load_declares_the_registered_products_it_loads(self):
        # THE RE-SOURCE. The unit used to declare a `jids` list that
        # `download_psf_catalogs` turned into `<proc_date>/jid<N>/<name>`
        # keys — a table with no rows feeding a prefix that does not exist.
        # It now declares each contributing product's own registered URI, and
        # the loader resolves the catalogue as that object's sibling.
        source = self.Source(
            scas=[7],
            products={7: [(1086, 20, 7, 6765,
                           "s3://roman-rapid-products/science/run/000020/07/"
                           "attempt-0000006765/sfftdiffimage_masked.fits")]})

        units = list(gather_catalog_load_units(source, "20260808"))

        inputs = units[0].fields["product_inputs"]
        self.assertEqual(len(inputs), 1)
        self.assertEqual(inputs[0]["pid"], 1086)
        self.assertEqual(inputs[0]["attempt_id"], 6765)
        self.assertTrue(
            inputs[0]["difference_image_uri"].endswith(
                "attempt-0000006765/sfftdiffimage_masked.fits"))
        # No `jids` anywhere: the legacy fact is gone, not merely unused.
        self.assertNotIn("jids", units[0].fields)

    def test_catalog_load_asks_for_products_per_sca(self):
        # The product query is per-SCA, and a gatherer that asked once for
        # the date would give every unit every SCA's catalogues.
        source = self.Source(scas=[1, 2], products={1: [], 2: []})

        list(gather_catalog_load_units(source, "20260808"))

        self.assertIn(("products", "20260808", 1), source.asked_for)
        self.assertIn(("products", "20260808", 2), source.asked_for)

    def test_a_failed_product_query_raises_rather_than_declaring_nothing(self):
        # Same silent-failure class as the SCA query below: a unit that
        # declared no inputs because the query failed would load nothing and
        # close successfully, reporting an outage as an empty catalogue.
        with self.assertRaises(GatheringError) as caught:
            list(gather_catalog_load_units(
                self.Source(scas=[7], failure=67), "20260808"))

        self.assertIn("67", str(caught.exception))

    def test_a_malformed_processing_date_is_refused(self):
        # The unit key is derived from it, so a malformed date collides
        # silently rather than failing.
        with self.assertRaises(GatheringError):
            list(gather_catalog_load_units(self.Source(scas=[1]), "2026-08-08"))

    def test_a_failed_sca_query_raises_rather_than_enumerating_nothing(self):
        # THE SILENT-FAILURE CHECK. `rapid_db` returns None and sets
        # exit_code 67 without raising, so an unguarded caller reads a
        # database outage as "this date has no work" and submits an empty
        # chain that looks like a clean run.
        with self.assertRaises(GatheringError) as caught:
            list(gather_catalog_load_units(
                self.Source(scas=[1], failure=67), "20260808"))

        self.assertIn("67", str(caught.exception))

    def test_a_date_with_no_science_jobs_yields_no_units(self):
        # Distinct from the failure above: a real empty answer is empty, and
        # is not an error.
        self.assertEqual(
            list(gather_catalog_load_units(self.Source(scas=[]), "20260808")),
            [])

    # -- crossmatch: (processing date, field) ------------------------------

    def test_crossmatch_enumerates_one_unit_per_field(self):
        units = list(gather_crossmatch_units(
            self.Source(fields=[101, 202]), "20260808"))

        self.assertEqual(len(units), 2)
        self.assertEqual([u.fields["field"] for u in units], [101, 202])
        for unit in units:
            self.assertEqual(unit.fields["job_type"], JOB_TYPE_CROSSMATCH)

    def test_crossmatch_gathers_nothing_when_a_sca_is_incomplete(self):
        # The durable-state gate: one SCA without a completed catalog-load
        # attempt holds the WHOLE date's crossmatch gathering (the stage
        # reads every SCA's sources table), and the fields are never even
        # enumerated — gather again next poll, no exception.
        source = self.Source(fields=[101, 202], incomplete_catalog_load=[7])

        units = list(gather_crossmatch_units(source, "20260808"))

        self.assertEqual(units, [])
        self.assertIn(("incomplete_catalog_load", "20260808"),
                      source.asked_for)
        self.assertNotIn(("fields", "20260808"), source.asked_for)

    def test_crossmatch_carries_the_field_as_a_unit_fact_too(self):
        # `field` is a named `UnitFacts` entry, so it rides there as well as
        # in the open mapping — the stages read it through `context.fact`.
        units = list(gather_crossmatch_units(
            self.Source(fields=[101]), "20260808"))

        self.assertEqual(units[0].facts.field, 101)

    def test_crossmatch_names_both_target_tables(self):
        units = list(gather_crossmatch_units(
            self.Source(fields=[101]), "20260808"))

        self.assertEqual(units[0].fields["target_tables"],
                         ["astroobjects_101", "merges_101"])

    def test_a_failed_field_query_raises(self):
        with self.assertRaises(GatheringError):
            list(gather_crossmatch_units(
                self.Source(fields=[1], failure=67), "20260808"))

    # -- the corpus-wide per-field job types -------------------------------

    def test_statistics_enumerates_from_the_astroobjects_clones(self):
        source = self.Source(per_field=[11, 22])
        units = list(gather_statistics_units(source))

        self.assertEqual([u.fields["field"] for u in units], [11, 22])
        self.assertEqual(units[0].fields["job_type"], JOB_TYPE_STATISTICS)
        self.assertIn(("per_field", "astroobjects"), source.asked_for)

    def test_the_sweeps_enumerate_from_the_merges_clones(self):
        for gather, job_type in (
                (gather_merge_currency_units, JOB_TYPE_MERGE_CURRENCY),
                (gather_source_currency_units, JOB_TYPE_SOURCE_CURRENCY),
                (gather_merge_dedup_units, JOB_TYPE_MERGE_DEDUP)):
            source = self.Source(per_field=[33])
            units = list(gather(source))

            self.assertEqual(len(units), 1, job_type)
            self.assertEqual(units[0].fields["job_type"], job_type)
            self.assertIn(("per_field", "merges"), source.asked_for)

    def test_per_field_units_key_uniquely(self):
        units = list(gather_statistics_units(self.Source(per_field=[1, 2, 3])))

        self.assertEqual(len({u.key for u in units}), 3)

    def test_a_failed_per_field_query_raises(self):
        with self.assertRaises(GatheringError):
            list(gather_statistics_units(self.Source(per_field=[1],
                                                     failure=67)))

    # `attempts.sca` is smallint and `attempts.exposure_id` is integer, probed
    # live on rapid-db. A unit whose identity does not fit those domains
    # cannot have a row created for it, so the constraint belongs here rather
    # than being discovered at submission time — which is exactly how it WAS
    # discovered: the first live merge-dedup submission raised
    # `NumericValueOutOfRange: smallint out of range` from `create_submitted`,
    # because the per-field units put a seven-digit field identifier
    # (4641773) in `sca`. These tests use REAL field magnitudes for that
    # reason; the original tests used single-digit fields, which fit smallint
    # and so proved nothing.
    SMALLINT_MAX = 32767
    INTEGER_MAX = 2147483647

    def test_a_real_field_identifier_does_not_overflow_the_sca_column(self):
        units = list(gather_statistics_units(
            self.Source(per_field=[4641773, 4645869])))

        self.assertTrue(units)
        for unit in units:
            self.assertLessEqual(abs(unit.sca), self.SMALLINT_MAX)
            self.assertLessEqual(abs(unit.exposure), self.INTEGER_MAX)

    def test_per_field_units_carry_no_sca(self):
        # A field identifier in `sca` would be a field pretending to be an
        # SCA. These units have none, and 0 says so.
        units = list(gather_merge_dedup_units(self.Source(per_field=[4641773])))

        self.assertEqual(units[0].sca, 0)
        self.assertEqual(units[0].exposure, 4641773)
        self.assertEqual(units[0].fields["field"], 4641773)

    def test_real_field_units_still_key_uniquely(self):
        units = list(gather_statistics_units(
            self.Source(per_field=[4641773, 4645869, 4637678])))

        self.assertEqual(len({u.key for u in units}), 3)

    def test_a_real_processing_date_fits_the_exposure_column(self):
        units = list(gather_catalog_load_units(
            self.Source(scas=[1, 18]), "20260808"))

        for unit in units:
            self.assertLessEqual(unit.exposure, self.INTEGER_MAX)
            self.assertLessEqual(unit.sca, self.SMALLINT_MAX)

    def test_every_post_db_gatherer_stamps_its_job_type(self):
        # The manifest's job type fixes the route, so a unit gathered without
        # one is a unit no submitter can route.
        source = self.Source(scas=[1], fields=[1], per_field=[1])
        gathered = [
            list(gather_catalog_load_units(source, "20260808")),
            list(gather_crossmatch_units(source, "20260808")),
            list(gather_statistics_units(source)),
            list(gather_merge_currency_units(source)),
            list(gather_source_currency_units(source)),
            list(gather_merge_dedup_units(source)),
        ]
        for units in gathered:
            self.assertTrue(units)
            for unit in units:
                self.assertIn("job_type", unit.fields)


class AlertProductionGatheringTests(unittest.TestCase):
    """The step-4 trigger's gathering and its emission watermark.

    The design's own sentences are the test names where possible: emission is
    once per logical unit per release, no promotion means no alert, and the
    watermark is what makes a replay silent.
    """

    class Source:
        """Attempts awaiting emission, plus the watermark they claim against.

        The claim is modelled as the real one is — `ON CONFLICT DO NOTHING`
        returning whether THIS caller won — because the whole
        at-least-once/suppression posture turns on losing that race being an
        ordinary outcome rather than an error. A stub that always claimed
        successfully could not exercise the suppression path at all.
        """

        def __init__(self, rows=(), failure=0, already=()):
            self.rows = list(rows)
            self.failure = failure
            self.exit_code = 0
            self.emitted = set(already)
            self.claims = []

        def get_attempts_awaiting_alert_emission(self, release_identity,
                                                 limit=None):
            self.exit_code = self.failure
            if self.failure:
                return None
            rows = [row for row in self.rows
                    if (row[1], row[2], release_identity) not in self.emitted]
            return rows[:limit] if limit is not None else rows

        def record_alert_emission(self, exposure_id, sca, release_identity,
                                  attempt_id, pid=None, alerts_published=0):
            self.exit_code = self.failure
            if self.failure:
                return None
            key = (exposure_id, sca, release_identity)
            self.claims.append((key, attempt_id, alerts_published))
            if key in self.emitted:
                return False
            self.emitted.add(key)
            return True

    #: (attempt_id, expid, sca, pid, product, role_resolved_from,
    #:  registered_at, sequence) — the query's column order.
    ROW = (6765, 20, 7, 1086, "sfft_diffimage", "release", "2026-08-09", 1)

    def test_one_unit_per_attempt_awaiting_emission(self):
        units = list(gather_alert_production_units(
            self.Source(rows=[self.ROW]), "rel-1"))

        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].exposure, 20)
        self.assertEqual(units[0].sca, 7)
        self.assertEqual(units[0].fields["job_type"], JOB_TYPE_ALERT_PRODUCTION)

    def test_the_manifest_names_the_attempt_and_the_difference_image(self):
        # The design: "The manifest names the attempt identity and the
        # promoted difference-image identity as declared inputs."
        units = list(gather_alert_production_units(
            self.Source(rows=[self.ROW]), "rel-1"))

        self.assertEqual(units[0].fields["attempt_id"], 6765)
        self.assertEqual(units[0].fields["difference_image_pid"], 1086)
        self.assertEqual(units[0].facts.pid, 1086)
        self.assertEqual(units[0].fields["release_identity"], "rel-1")

    def test_an_already_emitted_unit_is_not_gathered_again(self):
        # "Emission is once per logical unit per release."
        source = self.Source(rows=[self.ROW], already={(20, 7, "rel-1")})

        self.assertEqual(
            list(gather_alert_production_units(source, "rel-1")), [])

    def test_the_same_unit_is_eligible_under_a_different_release(self):
        # Cross-release re-emission is the release machinery's ruling and is
        # deliberately NOT decided here — so the scope must actually be
        # per-release rather than per-unit.
        source = self.Source(rows=[self.ROW], already={(20, 7, "rel-1")})

        self.assertEqual(
            len(list(gather_alert_production_units(source, "rel-2"))), 1)

    def test_gathering_without_a_release_identity_is_refused(self):
        # A pass that did not know its release could not tell an emitted unit
        # from a new one, which is the one thing the watermark exists to do.
        for empty in (None, "", "   "):
            with self.assertRaises(GatheringError):
                list(gather_alert_production_units(self.Source(), empty))

    def test_a_failed_query_raises_rather_than_emitting_nothing(self):
        # The silent-failure class again: "no units awaiting emission" and
        # "the query failed" must not look the same to the submitter.
        with self.assertRaises(GatheringError) as caught:
            list(gather_alert_production_units(
                self.Source(rows=[self.ROW], failure=67), "rel-1"))

        self.assertIn("67", str(caught.exception))

    def test_the_watermark_initialization_claims_without_publishing(self):
        # THE PROPOSED DISPOSITION. Seeding at deployment is what stops the
        # replay-backfilled promotions emitting retroactively; each claim
        # records zero alerts published, because none were.
        source = self.Source(rows=[self.ROW])

        claimed = initialize_alert_watermark(source, "rel-1")

        self.assertEqual(claimed, 1)
        self.assertEqual([published for _, _, published in source.claims], [0])
        # And the unit is silent afterwards.
        self.assertEqual(
            list(gather_alert_production_units(source, "rel-1")), [])

    def test_initialization_is_idempotent(self):
        # Running the deployment step twice must not double-count or raise:
        # the second pass finds nothing outstanding.
        source = self.Source(rows=[self.ROW])

        self.assertEqual(initialize_alert_watermark(source, "rel-1"), 1)
        self.assertEqual(initialize_alert_watermark(source, "rel-1"), 0)


if __name__ == "__main__":
    unittest.main()
