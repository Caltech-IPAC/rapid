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

from database.modules.utils.checked import FAILURE_THRESHOLD, RapidDBCallFailed
from submission import gathering
from submission.gathering import (
    GatheringError,
    gather_alert_production_units,
    gather_campaign_units,
    gather_catalog_load_units,
    gather_crossmatch_units,
    gather_merge_currency_units,
    gather_merge_dedup_units,
    gather_science_units,
    gather_source_currency_units,
    gather_statistics_units,
    initialize_alert_watermark,
    science_facts,
)
from submission.routes import (
    JOB_TYPE_ALERT_PRODUCTION,
    JOB_TYPE_CATALOG_LOAD,
    JOB_TYPE_CROSSMATCH,
    JOB_TYPE_MERGE_CURRENCY,
    JOB_TYPE_MERGE_DEDUP,
    JOB_TYPE_REFERENCE_IMAGE,
    JOB_TYPE_SCIENCE,
    JOB_TYPE_SOURCE_CURRENCY,
    JOB_TYPE_STATISTICS,
)
from pipeline.repositories.errors import RepositoryQueryFailed
from submission.subjects import SubjectError


def _refuse_if_failed(source, method):
    """Raise `RapidDBCallFailed` for a stub call that set `exit_code >= 64`.

    Production gathering no longer reads `source.exit_code` itself — that
    check now lives in `database.modules.utils.checked.CheckedHandle`,
    which wraps the real handle before a gatherer ever sees it. These
    stubs are called directly, with no adapter in front of them, so a
    stub that wants to exercise "the query failed" must refuse the same
    way the adapter would: raise, rather than return `None` and rely on
    a caller-side `exit_code` check that no longer exists. Code 7 (the
    documented "no record" convention) is not a failure and is not
    raised here, matching `CheckedHandle.FAILURE_THRESHOLD`.
    """
    code = getattr(source, "exit_code", 0)
    if code >= FAILURE_THRESHOLD:
        raise RapidDBCallFailed(method, code)


class _StubAssociationRepository:
    """A double for `AssociationRepository`, shaped like the real one.

    Two methods, the two the claim path calls, with the same return contract:
    `claim_position` answers `None` for "DRAFT 049 not applied" and a
    `(proc_date, field)` pair otherwise — where `(None, None)` is the origin,
    a genuinely different state from `None`.

    IT CAN REFUSE. Both methods raise `RepositoryQueryFailed` when their
    switch is set, because the production code catches exactly that and turns
    it into a `GatheringError`. A double that could only succeed would leave
    the failure path — the one that decides whether a database outage silently
    disables the ordering guarantee — untested by construction.
    """

    def __init__(self, source):
        self._source = source

    def claim_position(self, association_set=None, lane=None):
        self._source.asked_for.append(("watermark", association_set, lane))
        if self._source.watermark_failure:
            # Two arguments, matching the real constructor's `(method,
            # message)` — a double that raised the typed error WRONGLY would
            # fail with a TypeError instead of the failure it meant to
            # simulate, which is a green-looking red for the wrong reason.
            raise RepositoryQueryFailed(
                "claim_position", "stubbed watermark read failure")
        return self._source.watermark

    def earliest_unaccepted_date(self, association_set=None):
        self._source.asked_for.append(("earliest_owed", association_set))
        if self._source.owed_failure:
            raise RepositoryQueryFailed(
                "earliest_unaccepted_date", "stubbed owed-date read failure")
        return self._source.earliest_owed


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

    def get_blocking_exposure_scas_for_job_type(self, job_type, expids):
        # The EXPOSURE_SCA resubmission gate (final convergence round):
        # default empty — nothing blocks — so existing tests keep their
        # semantics; gate tests override `blocking_exposure_scas`.
        return getattr(self, "blocking_exposure_scas", [])

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
            result = self.reference(self, ppid, field, fid)
        else:
            result = self.reference
        _refuse_if_failed(self, "get_best_reference_image")
        return result


# ---------------------------------------------------------------------------
# science_facts: one row -> one fact set
# ---------------------------------------------------------------------------

class ScienceFactsTests(unittest.TestCase):

    # NOTE ON THIS CLASS: `submission.gathering.science_facts()` is typed
    # `-> dict` and IS a plain dict, deliberately — its two callers
    # (`gather_science_units`, `gather_campaign_units`) spread it with
    # `**facts` straight into `payloads.build(...)`, which is what turns it
    # into a typed `SciencePayload`. These tests originally read it with
    # attribute access (`facts.rid`) as though it were already a payload;
    # retargeted at dict access (`facts["rid"]`) to match the function's own
    # return-type annotation and its two real call sites, rather than
    # weakening what each assertion actually checks.

    def test_every_fact_traces_to_its_column(self):
        facts = science_facts(StubSource(), 101, field=4678622, fid=8)
        self.assertEqual(facts["rid"], 101)
        self.assertEqual(facts["fid"], 8)
        self.assertEqual(facts["field"], 4678622)
        self.assertEqual(facts["expid"], 5001)
        self.assertEqual(facts["mjdobs"], 61680.5)
        self.assertEqual(facts["exptime"], 139.8)
        self.assertEqual(facts["infobits"], 0)
        # `status` was deleted as a dead member (D4): resolved by
        # `science_facts` in the past but never read by any consumer, and
        # `SciencePayload` declares no such component or invocation fact.
        self.assertEqual(facts["science_image_uri"], "s3://in/exp1_sca7.fits")
        self.assertEqual(facts["filter_name"], "F146")

    def test_rtid_is_carried_not_looked_up(self):
        # W7 retired the per-source R-tree query; rtid equals field by
        # construction in this tessellation. Re-adding a lookup here would
        # reintroduce exactly what the retirement removed.
        facts = science_facts(StubSource(), 101, field=4678622, fid=8)
        self.assertEqual(facts["rtid"], facts["field"])

    def test_sky_position_is_all_or_nothing(self):
        facts = science_facts(StubSource(), 101, field=4678622, fid=8)
        self.assertEqual(facts["sky_position"]["ra0"], 10.0)
        self.assertEqual(facts["sky_position"]["dec4"], -43.4)
        self.assertEqual(len(facts["sky_position"]), 10)

    def test_a_half_populated_corner_set_is_absent_entirely(self):
        # A half-populated mapping is worse than an absent one: a consumer
        # reading ra3 without checking would silently use a defaulted value.
        broken = dict(StubSource().meta)
        broken[101] = (7, 8, 10.0, -43.0, None, -43.1, 10.2, -43.2,
                       10.3, -43.3, 10.4, -43.4)
        facts = science_facts(StubSource(meta=broken), 101,
                              field=4678622, fid=8)
        self.assertIsNone(facts["sky_position"])

    def test_psf_facts_are_absent_when_no_psf_exists(self):
        facts = science_facts(StubSource(psf=None), 101,
                              field=4678622, fid=8)
        self.assertNotIn("psfid", facts)
        self.assertNotIn("psf_uri", facts)

    def test_absent_facts_are_omitted_not_nulled(self):
        # The adopted absent-not-sentinel rule: a fact never resolved and a
        # fact resolved to nothing are different, and only the first is what
        # an omitted key means. Retargeted at `facts` itself rather than
        # `facts.to_dict()` — a plain dict has no such method, and
        # `science_facts` already omits an unresolved key rather than
        # writing it as null (see its own `if ... is not None:` guards), so
        # the dict IS the wire-shaped form here.
        facts = science_facts(StubSource(psf=None, reference=None), 101,
                              field=4678622, fid=8)
        self.assertNotIn("psf_uri", facts)
        self.assertNotIn("reference_image_uri", facts)

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
        self.assertEqual(facts["reference_image_id"], 900)
        self.assertEqual(source.reference_calls, [12])

    def test_it_falls_back_to_the_science_ppid(self):
        # A reference built incidentally by an earlier science run is
        # registered under the science ppid — the launcher's second call.
        source = StubSource(reference=self._reference_under(15))
        facts = science_facts(source, 101, field=4678622, fid=8,
                              reference_ppid=12, science_ppid=15)
        self.assertEqual(facts["reference_image_id"], 900)
        self.assertEqual(source.reference_calls, [12, 15])

    def test_the_matching_ppid_is_recorded_as_provenance(self):
        # Which ppid answered is provenance a consumer needs: a reference
        # built by the reference-image job type and one built incidentally
        # are different objects, and the query does not return the ppid.
        source = StubSource(reference=self._reference_under(15))
        facts = science_facts(source, 101, field=4678622, fid=8,
                              reference_ppid=12, science_ppid=15)
        self.assertEqual(facts["reference_image_ppid"], 15)

    def test_no_reference_anywhere_leaves_the_facts_absent(self):
        # Not an error: "no reference exists yet" is a legitimate state, and
        # whether a unit is submittable without one is the job type's call,
        # made by `require`, not gathering's. Retargeted at `assertNotIn` —
        # `science_facts` OMITS an unresolved reference key entirely (see
        # its own `if reference is not None:` guard) rather than writing it
        # as null, so "absent" means missing from the dict, not None.
        def none_at_all(source, ppid, field, fid):
            source.exit_code = 7
            return None
        source = StubSource(reference=none_at_all)
        facts = science_facts(source, 101, field=4678622, fid=8)
        self.assertNotIn("reference_image_id", facts)

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
        # `make_references=True` yields REFERENCE-IMAGE units, so the key is
        # asked for under that job type. This test used to pass
        # JOB_TYPE_SCIENCE against those units and the old API accepted it
        # silently — `dedup_key(job_type)` read whatever job type it was
        # handed and built a tuple from the unit's exposure/sca regardless,
        # so the mismatch was invisible. The typed payload declares its own
        # job type and refuses the disagreement, which is what surfaced this.
        unit = self._gather(make_references=True)[0]
        self.assertEqual(unit.job_type, JOB_TYPE_REFERENCE_IMAGE)
        self.assertNotEqual(
            unit.logical_job_key("run-a", JOB_TYPE_REFERENCE_IMAGE),
            unit.logical_job_key("run-b", JOB_TYPE_REFERENCE_IMAGE))

    def test_a_field_with_no_files_is_skipped_not_an_error(self):
        source = StubSource(l2files={})
        units = list(gather_science_units(
            source, start="2026-01-01", end="2026-12-31",
            start_mjdobs=61600.0, end_mjdobs=61700.0,
            min_images_to_coadd=10, fids=[8]))
        self.assertEqual(units, [])

    def test_a_blocked_exposure_sca_is_not_regathered(self):
        # THE RESUBMISSION GATE (final convergence round, 2026-08-09):
        # science was the last state-blind enumeration — a fixed window
        # re-yielded every unit each poll for the whole flight of its
        # first attempt and forever after its success.
        source = StubSource()
        baseline = list(gather_science_units(
            source, start="2026-01-01", end="2026-12-31",
            start_mjdobs=61600.0, end_mjdobs=61700.0,
            min_images_to_coadd=10, fids=[8]))
        self.assertTrue(baseline)
        source.blocking_exposure_scas = [
            (u.exposure, u.sca) for u in baseline]

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

    def setUp(self):
        # `coadd_input_rows(window=None)` — the default every test here uses
        # except `test_an_explicit_window_is_what_reaches_the_query` — calls
        # `reference_observation_window()`, which reads release content via
        # `science_config.load()`, which fails loud (`ConfigError`) if
        # `RAPID_SW` is not set (`science_config.config_path`'s own
        # docstring: "There is deliberately no fallback to the current
        # directory"). One test in this class
        # (`test_the_window_comes_from_release_content_by_default`) already
        # patched `RAPID_SW` correctly around its own call; the other eleven
        # did not, so they read whichever `RAPID_SW` happened to be AMBIENT
        # in the process actually running the suite — unset on a bare
        # laptop shell (`ConfigError`, reproduced 2026-08-14: 8 of this
        # class's 20 tests failed outright with `RAPID_SW is not set`), and
        # whatever tree a CI/SSM runner's environment happens to export
        # otherwise, which is a different bug: those tests would then be
        # silently reading a REAL `cdf/science/pipeline.toml` from wherever
        # that root points, not the fixed value they assert against. Setting
        # it once here, for the whole class, is what "no database, no
        # monkeypatching [outside what the test itself declares]" (this
        # module's own docstring) actually requires for a method whose
        # inputs are not fully passed as arguments.
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        patcher = mock.patch.dict(os.environ, {"RAPID_SW": repo_root})
        patcher.start()
        self.addCleanup(patcher.stop)

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
                _refuse_if_failed(self, "get_overlapping_l2files")
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

        # RAPID_SW is already patched at the checkout root by `setUp`, so
        # the release content read here is the repo's own
        # `cdf/science/pipeline.toml`, not whatever tree the runner happens
        # to be in. Only the retired variables need patching here.
        with mock.patch.dict(os.environ,
                             {"STARTREFIMMJDOBS": "1.0",
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

    def setUp(self):
        # Same ambient-`RAPID_SW` gap as `CoaddInputsTests.setUp` (wave-E
        # finding #11): `_gather` below calls `gather_reference_units`,
        # which resolves the observation window via
        # `reference_observation_window()` when no override reaches it,
        # exactly like `coadd_input_rows` does. Reproduced 2026-08-14: 4 of
        # this class's tests failed outright with `RAPID_SW is not set`
        # against an environment where it truly is unset.
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        patcher = mock.patch.dict(os.environ, {"RAPID_SW": repo_root})
        patcher.start()
        self.addCleanup(patcher.stop)

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
                _refuse_if_failed(self, "get_overlapping_l2files")
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
                     products=None, incomplete_catalog_load=(),
                     gatherable_catalog_load=None, blocking_crossmatch=(),
                     blocking_per_field=(), watermark=None,
                     earliest_owed=None, watermark_failure=0,
                     owed_failure=0):
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
            # The resubmission gates (mission mock, live 2026-08-09).
            # `gatherable_catalog_load` defaults to `scas`: absent pending
            # or successful attempts, the gather set IS the science set —
            # tests that narrow it are exercising the gate.
            self.gatherable_catalog_load = (
                list(scas) if gatherable_catalog_load is None
                else list(gatherable_catalog_load))
            self.blocking_crossmatch = list(blocking_crossmatch)
            self.blocking_per_field = list(blocking_per_field)
            # The association claim watermark (conformance rule 19, brief F2).
            # `None` by DEFAULT, and that default is load-bearing: it is the
            # database that has not adopted DRAFT 049, for which the ordering
            # gate degrades to the previous unordered gathering. Every test
            # above that predates the gate therefore keeps its old
            # expectations unchanged — the degradation is asserted directly by
            # `test_crossmatch_without_the_ordering_schema_yields_every_ready_
            # field`, and demonstrated incidentally by all of them.
            #
            # `(None, None)` is the ORIGIN — the schema is there and nothing
            # has been accepted yet — which is a different answer from `None`
            # and gates rather than degrades. A stub that could not tell those
            # two apart could not test the distinction the whole gate rests on.
            self.watermark = watermark
            # The cross-date half: the earliest processing date still owing
            # crossmatch work. Only consulted when the watermark is present.
            self.earliest_owed = earliest_owed
            self.watermark_failure = watermark_failure
            self.owed_failure = owed_failure

        def get_scas_with_science_jobs_for_processing_date(self, proc_date):
            self.asked_for.append(("scas", proc_date))
            self.exit_code = self.failure
            _refuse_if_failed(self, "get_scas_with_science_jobs_for_processing_date")
            return None if self.failure else self.scas

        def get_scas_with_incomplete_catalog_load_for_processing_date(
                self, proc_date):
            self.asked_for.append(("incomplete_catalog_load", proc_date))
            self.exit_code = self.failure
            _refuse_if_failed(
                self, "get_scas_with_incomplete_catalog_load_for_processing_date")
            return None if self.failure else self.incomplete_catalog_load

        def get_fields_with_science_jobs_for_processing_date(self, proc_date):
            self.asked_for.append(("fields", proc_date))
            self.exit_code = self.failure
            _refuse_if_failed(self, "get_fields_with_science_jobs_for_processing_date")
            return None if self.failure else self.fields

        def get_fields_with_per_field_table(self, prototype):
            self.asked_for.append(("per_field", prototype))
            self.exit_code = self.failure
            _refuse_if_failed(self, "get_fields_with_per_field_table")
            return None if self.failure else self.per_field

        def get_registered_diffimages_for_processing_date_sca(self, proc_date,
                                                              sca):
            self.asked_for.append(("products", proc_date, sca))
            self.exit_code = self.failure
            _refuse_if_failed(
                self, "get_registered_diffimages_for_processing_date_sca")
            return None if self.failure else self.products.get(int(sca), [])

        def get_scas_with_gatherable_catalog_load_for_processing_date(
                self, proc_date):
            self.asked_for.append(("gatherable_catalog_load", proc_date))
            self.exit_code = self.failure
            _refuse_if_failed(
                self,
                "get_scas_with_gatherable_catalog_load_for_processing_date")
            return None if self.failure else self.gatherable_catalog_load

        def get_fields_with_blocking_crossmatch_attempt_for_processing_date(
                self, proc_date):
            self.asked_for.append(("blocking_crossmatch", proc_date))
            self.exit_code = self.failure
            _refuse_if_failed(
                self,
                "get_fields_with_blocking_crossmatch_attempt_for_processing_date")
            return None if self.failure else self.blocking_crossmatch

        def get_fields_with_blocking_attempt_for_job_type_since(
                self, job_type, since):
            self.asked_for.append(("blocking_per_field", job_type, since))
            self.exit_code = self.failure
            _refuse_if_failed(
                self, "get_fields_with_blocking_attempt_for_job_type_since")
            return None if self.failure else self.blocking_per_field

        # THE ORDERING GATE IS NOT A HANDLE METHOD. `RAPIDDB` is frozen
        # (brief G's ratified merge decision), so the two reads go through
        # `pipeline.repositories.association.AssociationRepository` over a
        # connection. The stub therefore injects a repository double at the
        # same seam the production code takes it from — `association_
        # repository` on the handle — rather than growing two methods the real
        # handle does not have. A stub shaped like an interface that no longer
        # exists is the stub-blind failure in its purest form: it would pass
        # forever against code nothing calls.
        #
        # The failure switches stay per-read rather than reusing the shared
        # `failure`, and that is not a convenience: `failure` refuses the
        # FIRST query a pass makes, which for crossmatch is the catalog-load
        # coverage check — so a test using it would raise before the watermark
        # was ever read and would still pass while the gate was completely
        # unguarded. A gate needs a failure switch that can reach IT.
        @property
        def association_repository(self):
            return _StubAssociationRepository(self)

    # -- the resubmission gates (mission mock, live 2026-08-09) ------------

    def test_catalog_load_gathers_only_scas_without_blocking_attempts(self):
        # The gather set is the GATHERABLE set — science SCAs lacking a
        # pending-or-successful catalog-load attempt — never the whole
        # science set: the state-blind enumeration resubmitted every
        # accumulator cut, live.
        units = list(gather_catalog_load_units(
            self.Source(scas=[1, 2, 18], gatherable_catalog_load=[2]),
            "20260809"))

        self.assertEqual([u.payload.sca for u in units], [2])

    def test_crossmatch_skips_fields_with_blocking_attempts(self):
        units = list(gather_crossmatch_units(
            self.Source(fields=[4641773, 4641774],
                        blocking_crossmatch=[4641773]), "20260809"))

        self.assertEqual([u.payload.field for u in units], [4641774])

    def test_per_field_types_skip_fields_with_blocking_attempts_today(self):
        source = self.Source(per_field=[4641773, 4641774],
                             blocking_per_field=[4641774])

        units = list(gather_merge_currency_units(source))

        self.assertEqual([u.payload.field for u in units], [4641773])
        # The gate's window is the pass's UTC day: `since` must be a
        # timezone-aware UTC midnight, not a naive datetime and not a
        # rolling interval.
        blocking_calls = [c for c in source.asked_for
                          if c[0] == "blocking_per_field"]
        self.assertEqual(len(blocking_calls), 1)
        _, job_type, since = blocking_calls[0]
        self.assertEqual(job_type, JOB_TYPE_MERGE_CURRENCY)
        self.assertEqual((since.hour, since.minute, since.second), (0, 0, 0))
        self.assertIsNotNone(since.tzinfo)

    # -- catalog load: (processing date, SCA) ------------------------------

    def test_catalog_load_enumerates_one_unit_per_sca(self):
        units = list(gather_catalog_load_units(
            self.Source(scas=[1, 2, 18]), "20260808"))

        self.assertEqual(len(units), 3)
        self.assertEqual([u.payload.sca for u in units], [1, 2, 18])
        for unit in units:
            self.assertEqual(unit.payload.JOB_TYPE, JOB_TYPE_CATALOG_LOAD)
            self.assertEqual(unit.payload.proc_date, "20260808")

    def test_catalog_load_names_its_target_table_in_the_manifest(self):
        # The declared input. The job type does not build this name from its
        # own environment and hope it matches what was gathered.
        units = list(gather_catalog_load_units(
            self.Source(scas=[7]), "20260808"))

        self.assertEqual(units[0].payload.target_table, "sources_20260808_7")

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
                           "attempt-0000006765/sfftdiffimage_masked.fits",
                           4641773, 3, 61679.09)]})

        units = list(gather_catalog_load_units(source, "20260808"))

        inputs = units[0].payload.product_inputs
        self.assertEqual(len(inputs), 1)
        self.assertEqual(inputs[0]["pid"], 1086)
        self.assertEqual(inputs[0]["attempt_id"], 6765)
        self.assertTrue(
            inputs[0]["difference_image_uri"].endswith(
                "attempt-0000006765/sfftdiffimage_masked.fits"))
        # Per-product identity for the sources rows (mission mock, live
        # 2026-08-09): field/fid/mjdobs ride each product, never a
        # unit-constant fact.
        self.assertEqual(inputs[0]["field"], 4641773)
        self.assertEqual(inputs[0]["fid"], 3)
        self.assertEqual(inputs[0]["mjdobs"], 61679.09)
        # No `jids` anywhere: the legacy fact is gone, not merely unused —
        # the payload is a closed dataclass, so the equivalent assertion is
        # that its type declares no such component or invocation fact.
        payload = units[0].payload
        self.assertNotIn("jids", payload.COMPONENTS + payload.INVOCATION_FACTS)

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
        self.assertEqual([u.payload.field for u in units], [101, 202])
        for unit in units:
            self.assertEqual(unit.payload.JOB_TYPE, JOB_TYPE_CROSSMATCH)

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

        self.assertEqual(units[0].payload.target_tables,
                         ("astroobjects_101", "merges_101"))

    def test_a_failed_field_query_raises(self):
        with self.assertRaises(GatheringError):
            list(gather_crossmatch_units(
                self.Source(fields=[1], failure=67), "20260808"))

    # -- crossmatch: the ordering gate (conformance rule 19, brief F2) -----
    #
    # Readiness is not order. The two gates above answer "is this unit
    # runnable"; these answer "is it this unit's turn", asked afterwards and
    # separately. The property is that a pass yields AT MOST ONE unit — the
    # next in canonical `(proc_date, field)` order strictly ahead of the live
    # lane's watermark — so §2.5's hard concurrency cap of 1 falls out of the
    # claim discipline rather than out of a queue property. No route or queue
    # changed; crossmatch is still CLASS_BULK on the unbounded bulk queue.

    def test_crossmatch_at_the_origin_claims_only_the_lowest_field(self):
        # `(None, None)` is the origin: the ordering schema IS there and
        # nothing has been accepted in this set yet. Two fields are ready and
        # exactly one is claimable — the lowest, because canonical order is
        # ascending. The old behaviour would have yielded both, which is the
        # whole difference the gate makes.
        units = list(gather_crossmatch_units(
            self.Source(fields=[4641774, 4641773], watermark=(None, None)),
            "20260808"))

        self.assertEqual([u.payload.field for u in units], [4641773])

    def test_crossmatch_claims_the_field_after_the_watermark(self):
        # Mid-date: the watermark sits on f1, so f2 is the frontier's
        # successor. f1 is NOT re-yielded — it was accepted, and acceptance is
        # what put the watermark there.
        units = list(gather_crossmatch_units(
            self.Source(fields=[4641773, 4641774],
                        watermark=("20260808", 4641773)),
            "20260808"))

        self.assertEqual([u.payload.field for u in units], [4641774])

    def test_crossmatch_yields_nothing_when_the_date_is_exhausted(self):
        # The watermark is on the highest ready field, so no field of this
        # date is ahead of the frontier. Nothing claimable is an empty yield,
        # not an exception: the same "nothing ready yet" answer every other
        # gatherer gives, and the next date's pass is what moves things on.
        units = list(gather_crossmatch_units(
            self.Source(fields=[4641773, 4641774],
                        watermark=("20260808", 4641774)),
            "20260808"))

        self.assertEqual(units, [])

    def test_crossmatch_yields_nothing_for_a_date_behind_the_watermark(self):
        # A stale pass for an already-advanced-past date. Every unit of it was
        # accepted — that is what moved the watermark to a LATER date — so
        # re-gathering it would resubmit accepted work.
        units = list(gather_crossmatch_units(
            self.Source(fields=[4641773, 4641774],
                        watermark=("20260809", 4641773)),
            "20260808"))

        self.assertEqual(units, [])

    def test_crossmatch_waits_behind_an_earlier_date_still_owing_work(self):
        # ACCEPTANCE CRITERION 1, SECOND HALF — the case the whole item
        # exists for. §2.5: "serial execution does not by itself guarantee
        # that a later observation's association cannot run ahead of an
        # earlier one still in retry". Gathering is invoked once per date, so
        # the watermark alone cannot see that 20260808 is still in retry while
        # THIS pass asks about 20260809: a failed-and-retryable unit did not
        # advance the watermark, because only acceptance does, so the frontier
        # is silent about it. `earliest_owed` is the fact that closes it, and
        # naming an earlier date stops this pass dead.
        units = list(gather_crossmatch_units(
            self.Source(fields=[4641773, 4641774],
                        watermark=("20260808", 4641773),
                        earliest_owed="20260808"),
            "20260809"))

        self.assertEqual(units, [])

    def test_crossmatch_claims_a_later_date_once_no_earlier_one_is_owed(self):
        # The converse, and the reason the gate is a comparison rather than a
        # presence check: `earliest_owed` naming THIS date means the earliest
        # outstanding work is the work of this pass, so there is no
        # predecessor to wait behind and the date's lowest ready field is
        # claimable. A gate that blocked on any owed date at all would
        # deadlock the pipeline the moment it had work to do.
        units = list(gather_crossmatch_units(
            self.Source(fields=[4641774, 4641773],
                        watermark=("20260808", 4641774),
                        earliest_owed="20260809"),
            "20260809"))

        self.assertEqual([u.payload.field for u in units], [4641773])

    def test_crossmatch_without_the_ordering_schema_yields_every_ready_field(
            self):
        # THE EXPLICIT DEGRADATION. `association_watermarks` ships as a DRAFT
        # under `migrations-draft/`, so a database that has not adopted it
        # answers `None` — not `(None, None)` — and gathering then behaves
        # exactly as it did before the gate: unordered, every ready field.
        # That is deliberate: the ordering is a property of the deployed
        # schema, so raising here would take the crossmatch chain down on
        # every unadopted database in the fleet. The cross-date fact is not
        # even asked for, because an unordered deployment has no frontier for
        # it to qualify.
        source = self.Source(fields=[4641773, 4641774], watermark=None)

        units = list(gather_crossmatch_units(source, "20260808"))

        self.assertEqual([u.payload.field for u in units],
                         [4641773, 4641774])
        self.assertNotIn(("earliest_owed", None), source.asked_for)

    def test_the_ordering_gate_does_not_replace_the_blocking_attempt_gate(
            self):
        # The gates COMPOSE: readiness first, order second, over what
        # readiness left. The lowest field is blocked (a pending-or-successful
        # attempt), so it is not a claimable predecessor at all and the
        # frontier's successor is the next READY one — not "nothing, because
        # the field after the watermark is unavailable".
        units = list(gather_crossmatch_units(
            self.Source(fields=[4641773, 4641774],
                        blocking_crossmatch=[4641773],
                        watermark=(None, None)),
            "20260808"))

        self.assertEqual([u.payload.field for u in units], [4641774])

    def test_an_incomplete_catalog_load_stops_the_pass_before_the_watermark(
            self):
        # Ordering is asked only of units that are RUNNABLE. A date whose
        # catalog load is incomplete has nothing runnable, so the pass returns
        # before the watermark is read at all — the ordering gate never gets
        # to nominate a unit whose data is not there.
        source = self.Source(fields=[4641773], incomplete_catalog_load=[7],
                             watermark=(None, None))

        units = list(gather_crossmatch_units(source, "20260808"))

        self.assertEqual(units, [])
        self.assertNotIn(("watermark", None, None), source.asked_for)

    def test_a_failed_watermark_read_raises_rather_than_gathering_unordered(
            self):
        # The silent-failure class again, and it bites HARDER here than
        # elsewhere: `None` is exactly the value that means "no ordering
        # schema", so a read that answered None on failure would turn a
        # database outage into a silent loss of the ordering guarantee —
        # gathering every ready field, unordered, on a database that does
        # carry the schema. `AssociationRepository` RAISES
        # `RepositoryQueryFailed` instead of returning a sentinel, which is
        # the whole reason the carved repositories replaced `RAPIDDB`'s
        # `exit_code = 67; return None` convention. The refusal is what keeps
        # "degraded" distinguishable from "broken".
        source = self.Source(fields=[4641773], watermark=(None, None),
                             watermark_failure=67)

        with self.assertRaises(GatheringError) as caught:
            list(gather_crossmatch_units(source, "20260808"))

        # The message names the READ that failed, not an exit code: the
        # repository raises `RepositoryQueryFailed(method, message)` rather
        # than setting `exit_code = 67`, which is the whole difference the
        # carved layer was introduced to make.
        self.assertIn("claim_position", str(caught.exception))
        # It is THIS read that refused, not an earlier one raising first: the
        # pass reached the watermark, which is the only way this test proves
        # anything about the watermark's own guard.
        self.assertIn(("watermark", None, None), source.asked_for)

    def test_a_failed_earliest_owed_read_raises(self):
        # The cross-date fact carries the same weight as the watermark and
        # fails the same way: None means "nothing owed", so an unguarded read
        # would let a later date claim past an earlier one still in retry —
        # precisely the §2.5 violation the gate exists to prevent — on the
        # strength of a query that never ran.
        source = self.Source(fields=[4641773],
                             watermark=("20260808", 4641773),
                             owed_failure=67)

        with self.assertRaises(GatheringError) as caught:
            list(gather_crossmatch_units(source, "20260809"))

        self.assertIn("earliest_unaccepted_date", str(caught.exception))
        self.assertIn(("earliest_owed", None), source.asked_for)

    # -- the corpus-wide per-field job types -------------------------------

    def test_statistics_enumerates_from_the_astroobjects_clones(self):
        source = self.Source(per_field=[11, 22])
        units = list(gather_statistics_units(source))

        self.assertEqual([u.payload.field for u in units], [11, 22])
        self.assertEqual(units[0].payload.JOB_TYPE, JOB_TYPE_STATISTICS)
        self.assertIn(("per_field", "astroobjects"), source.asked_for)

    def test_the_sweeps_enumerate_from_the_merges_clones(self):
        for gather, job_type in (
                (gather_merge_currency_units, JOB_TYPE_MERGE_CURRENCY),
                (gather_source_currency_units, JOB_TYPE_SOURCE_CURRENCY),
                (gather_merge_dedup_units, JOB_TYPE_MERGE_DEDUP)):
            source = self.Source(per_field=[33])
            units = list(gather(source))

            self.assertEqual(len(units), 1, job_type)
            self.assertEqual(units[0].payload.JOB_TYPE, job_type)
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
        # The overflow risk this guarded against — a seven-digit field
        # identifier smeared into the smallint `sca` column — is now
        # structural rather than merely avoided: a FieldPayload has no `sca`
        # attribute at all, only `field`, so there is no smallint-shaped slot
        # left for it to overflow. What survives to check is that the real
        # field magnitude rides through gathering intact.
        units = list(gather_statistics_units(
            self.Source(per_field=[4641773, 4645869])))

        self.assertTrue(units)
        self.assertEqual([u.payload.field for u in units], [4641773, 4645869])
        for unit in units:
            self.assertLessEqual(abs(unit.payload.field), self.INTEGER_MAX)

    def test_per_field_units_carry_no_sca(self):
        # A field identifier in `sca` would be a field pretending to be an
        # SCA. These units have none at all now: a FieldPayload declares no
        # `sca` component, so asking for one is refused rather than
        # answering with a sentinel `0`.
        units = list(gather_merge_dedup_units(self.Source(per_field=[4641773])))

        self.assertNotIn("sca", units[0].payload.COMPONENTS)
        with self.assertRaises(SubjectError):
            units[0].sca
        with self.assertRaises(SubjectError):
            units[0].exposure
        self.assertEqual(units[0].payload.field, 4641773)

    def test_real_field_units_still_key_uniquely(self):
        units = list(gather_statistics_units(
            self.Source(per_field=[4641773, 4645869, 4637678])))

        self.assertEqual(len({u.key for u in units}), 3)

    def test_a_real_processing_date_fits_the_exposure_column(self):
        # The old sentinel this guarded against — the processing date's
        # ordinal smeared into the integer `exposure` column — no longer has
        # a representation to construct: CatalogLoadPayload declares
        # `proc_date` (carried as a string) and `sca`, and no `exposure`
        # component at all. What survives to check is that the real `sca`
        # still fits its smallint column, and that asking this unit for an
        # `exposure` is refused rather than answering with the date ordinal.
        units = list(gather_catalog_load_units(
            self.Source(scas=[1, 18]), "20260808"))

        for unit in units:
            self.assertNotIn("exposure", unit.payload.COMPONENTS)
            with self.assertRaises(SubjectError):
                unit.exposure
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
                self.assertTrue(unit.payload.JOB_TYPE)


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
            _refuse_if_failed(self, "get_attempts_awaiting_alert_emission")
            if self.failure:
                return None
            rows = [row for row in self.rows
                    if (row[1], row[2], release_identity) not in self.emitted]
            return rows[:limit] if limit is not None else rows

        def seed_alert_emission_watermark(self, exposure_id, sca,
                                          release_identity, attempt_id,
                                          pid=None):
            # Renamed from `record_alert_emission` (migration 037 /
            # integration ruling 3): seeding and the live CAS claim are
            # different writes now — this double models seeding only, the
            # one `initialize_alert_watermark` calls. `ON CONFLICT DO
            # NOTHING` semantics are unchanged: a unit already carrying a
            # row (seeded or otherwise) is not re-seeded.
            self.exit_code = self.failure
            _refuse_if_failed(self, "seed_alert_emission_watermark")
            if self.failure:
                return None
            key = (exposure_id, sca, release_identity)
            self.claims.append((key, attempt_id, 0))
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
        self.assertEqual(units[0].payload.JOB_TYPE, JOB_TYPE_ALERT_PRODUCTION)

    def test_the_manifest_names_the_attempt_and_the_difference_image(self):
        # The design: "The manifest names the attempt identity and the
        # promoted difference-image identity as declared inputs."
        units = list(gather_alert_production_units(
            self.Source(rows=[self.ROW]), "rel-1"))

        self.assertEqual(units[0].payload.promoted_attempt_id, 6765)
        self.assertEqual(units[0].payload.difference_image_pid, 1086)
        # Same fact, read through the `facts` alias (`facts` IS the
        # payload, D4) — there is no separate `pid` member, only
        # `difference_image_pid`.
        self.assertEqual(units[0].facts.difference_image_pid, 1086)
        self.assertEqual(units[0].payload.release_identity, "rel-1")

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


# ---------------------------------------------------------------------------
# gather_campaign_units (IR-13-a): the campaign-unit gatherer.
# ---------------------------------------------------------------------------

class CampaignSource(StubSource):
    """`StubSource` plus the campaign gatherer's two extra methods.

    Reuses `StubSource`'s `meta`/`info` fixtures unchanged — a campaign
    unit's `science_facts` call must resolve exactly like an arrival-driven
    unit's, against the SAME kind of L2FileMeta/L2Files rows, which is the
    whole point of the W2-fallback substrate (module docstring in
    `submission.gathering`).
    """

    def __init__(self, campaign_rows=(), source_l2=None, failure=0,
                **overrides):
        super().__init__(**overrides)
        self.campaign_rows = list(campaign_rows)
        # work_unit_id -> (rid, field, fid)
        self.source_l2 = dict(source_l2 or {})
        self.failure = failure

    def get_ready_test_campaign_units(self):
        self.exit_code = self.failure
        _refuse_if_failed(self, "get_ready_test_campaign_units")
        return self.campaign_rows

    def get_campaign_unit_source_l2_identity(self, work_unit_id):
        self.exit_code = self.failure
        _refuse_if_failed(self, "get_campaign_unit_source_l2_identity")
        return self.source_l2.get(work_unit_id, (None, None, None))


class GatherCampaignUnitsTests(unittest.TestCase):
    """`gather_campaign_units`: campaign rows in, science-shaped units out."""

    #: (work_unit_id, campaign_id, campaign_name, job_type, input_scope) —
    #: the query's own column order. input_scope "5001/7" is exposure/SCA,
    #: matching submission.subjects.build_input_scope's EXPOSURE_SCA
    #: grammar for job_type=science.
    ROW = (901, 42, "mock-day-1", JOB_TYPE_SCIENCE, "5001/7")

    def _source(self, **kw):
        kw.setdefault("campaign_rows", [self.ROW])
        kw.setdefault("source_l2", {901: (101, 4678622, 8)})
        return CampaignSource(**kw)

    def test_one_unit_per_ready_campaign_row(self):
        units = list(gather_campaign_units(self._source()))
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].exposure, 5001)
        self.assertEqual(units[0].sca, 7)

    def test_the_unit_is_science_shaped_like_an_arrival_driven_unit(self):
        # Same science_facts call, same source rid — the campaign unit and
        # gather_science_units's own unit for rid 101 must carry identical
        # facts, proving the downstream chain runs unmodified.
        #
        # JUDGEMENT CALL: `campaign_units[0].facts` is a `SciencePayload`
        # (gather_campaign_units spreads `science_facts`'s raw dict into
        # `payloads.build`), while `direct_facts` here is that same raw
        # dict — `science_facts()` is typed `-> dict` and returns one, it
        # is never a payload. The two are never equal by `assertEqual`
        # regardless of D4, so this compares every key `direct_facts`
        # resolved against the same-named attribute on the built payload,
        # which is the "identical facts" claim the original assertion meant.
        campaign_units = list(gather_campaign_units(self._source()))
        direct_facts = science_facts(StubSource(), 101, field=4678622, fid=8)

        payload = campaign_units[0].facts
        for name, value in direct_facts.items():
            self.assertEqual(getattr(payload, name), value,
                             f"fact {name!r} disagreed between the campaign "
                             f"unit's payload and a direct science_facts call")

    def test_no_ready_rows_yields_nothing(self):
        self.assertEqual(
            list(gather_campaign_units(self._source(campaign_rows=[]))), [])

    def test_a_non_science_job_type_is_refused_not_skipped(self):
        # THE V1 ROUTE RESTRICTION, re-asserted at gather time: a test
        # campaign whose work unit does not declare job_type=science is a
        # defect (the creation-time assertion should have refused it) and
        # must raise loudly here, never be silently dropped.
        bad_row = (901, 42, "mock-day-1", JOB_TYPE_REFERENCE_IMAGE, "5001/7")
        with self.assertRaises(GatheringError) as caught:
            list(gather_campaign_units(self._source(campaign_rows=[bad_row])))
        self.assertIn("science", str(caught.exception))

    def test_an_unparseable_input_scope_is_refused(self):
        bad_row = (901, 42, "mock-day-1", JOB_TYPE_SCIENCE, "not-a-scope")
        with self.assertRaises(GatheringError) as caught:
            list(gather_campaign_units(self._source(campaign_rows=[bad_row])))
        self.assertIn("901", str(caught.exception))

    def test_a_missing_source_l2_identity_is_refused_not_skipped(self):
        # "a campaign unit whose backing rows are missing raises loudly —
        # refusal, not skip" (the supervisor ruling). No source_l2 entry for
        # work_unit_id 901 means create_mock_campaign_from_staged's own
        # bookkeeping is incomplete, which is a real defect.
        source = self._source(source_l2={})
        with self.assertRaises(GatheringError) as caught:
            list(gather_campaign_units(source))
        self.assertIn("901", str(caught.exception))

    def test_a_missing_l2filemeta_row_for_the_recorded_rid_is_refused(self):
        # The recorded rid (999) has no meta/info row in the stub — mirrors
        # science_facts's own GatheringError for a dangling reference.
        source = self._source(source_l2={901: (999, 4678622, 8)})
        with self.assertRaises(GatheringError):
            list(gather_campaign_units(source))

    def test_a_failed_enumeration_query_raises(self):
        source = self._source(failure=FAILURE_THRESHOLD)
        with self.assertRaises(GatheringError):
            list(gather_campaign_units(source))

    def test_two_campaigns_each_gather_their_own_units(self):
        row_a = (901, 42, "mock-day-1", JOB_TYPE_SCIENCE, "5001/7")
        row_b = (902, 43, "mock-day-2", JOB_TYPE_SCIENCE, "5002/9")
        source = self._source(
            campaign_rows=[row_a, row_b],
            source_l2={901: (101, 4678622, 8), 902: (102, 4678622, 8)})

        units = list(gather_campaign_units(source))
        self.assertEqual({(u.exposure, u.sca) for u in units},
                         {(5001, 7), (5002, 9)})


if __name__ == "__main__":
    unittest.main()
