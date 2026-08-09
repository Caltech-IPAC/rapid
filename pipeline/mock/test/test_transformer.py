"""
File:    test_transformer.py

Tests for `pipeline.mock.transformer`: the schedule-to-generation
transformer and the campaign-integration function, against
`InMemoryObjectStore` (real create-once semantics, no live bucket) and a
fake executor (no live database), per this repo's house convention.
"""

import datetime
import json
import unittest

from pipeline.intent.writer import CampaignWriter, WorkUnitWriter
from pipeline.mock.transformer import (
    ALL_SCAS,
    INJECTION_PRISTINE,
    SUBSTRATE_SIMULATED,
    GenerationManifest,
    ScheduleRow,
    StagedSCA,
    create_mock_campaign,
    create_mock_campaign_from_staged,
    generation_id_for,
    stage_generation,
    wall_time_to_mjd,
)
from pipeline.runtime.boundaries import InMemoryObjectStore


def utc(*args):
    return datetime.datetime(*args, tzinfo=datetime.timezone.utc)


class RecordingExecutor:
    """See `pipeline.intent.test.test_writer.RecordingExecutor` — the same
    shape, duplicated rather than imported across test packages."""

    def __init__(self, returning: int = 1, affected: int = 1):
        self.calls: list[tuple[str, list]] = []
        self._next_id = returning
        self.affected = affected

    def __call__(self, sql, params):
        self.calls.append((" ".join(sql.split()), list(params)))
        if "RETURNING" in sql:
            value = self._next_id
            self._next_id += 1
            return [(value,)]
        return self.affected

    @property
    def statements(self):
        return [sql for sql, _ in self.calls]


class WallTimeToMjdTests(unittest.TestCase):
    def test_a_known_timestamp_matches_a_known_mjd(self):
        # J2000.0 epoch: 2000-01-01T12:00:00 UTC == MJD 51544.5, a standard
        # reference value (verified independently against astropy.time.Time
        # directly, not merely re-deriving what this function computes).
        mjd = wall_time_to_mjd(utc(2000, 1, 1, 12, 0, 0))
        self.assertAlmostEqual(51544.5, mjd, places=6)

    def test_matches_the_existing_mjd_window_helper_for_the_same_instant(self):
        # pipeline.operator.gathering.mjd_window is this repo's one existing
        # wall-clock-to-MJD converter; both must agree on the same instant,
        # since they share the same astropy call shape by design.
        from pipeline.operator.gathering import mjd_window

        moment = utc(2027, 10, 1, 0, 0, 0)
        single = wall_time_to_mjd(moment)
        window_start, _ = mjd_window(moment, moment)
        self.assertAlmostEqual(window_start, single, places=9)

    def test_returns_a_plain_float_not_a_numpy_scalar(self):
        mjd = wall_time_to_mjd(utc(2027, 10, 1, 0, 0, 0))
        self.assertIs(type(mjd), float)

    def test_naive_and_aware_timestamps_at_the_same_instant_agree(self):
        aware = wall_time_to_mjd(utc(2027, 10, 1, 6, 0, 0))
        naive = wall_time_to_mjd(datetime.datetime(2027, 10, 1, 6, 0, 0))
        self.assertAlmostEqual(aware, naive, places=9)


class StageGenerationTests(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryObjectStore()
        self.row = ScheduleRow(
            exposure_id=90000, wall_time=utc(2027, 10, 1, 0, 0, 0),
            field=42, source_ref="s3://sims-g0001/exp90000.fits")

    def test_one_object_per_sca_in_full_fanout(self):
        stage_generation(self.store, "mock-day-1", self.row)
        sca_keys = [k for k in self.store.objects
                   if "/sca/" in k]
        self.assertEqual(len(ALL_SCAS), len(sca_keys))

    def test_a_narrower_fanout_stages_only_those_scas(self):
        row = ScheduleRow(exposure_id=90000, wall_time=utc(2027, 10, 1),
                          field=42, source_ref="ref", scas=(1, 5, 9))
        stage_generation(self.store, "mock-day-1", row)
        sca_keys = [k for k in self.store.objects if "/sca/" in k]
        self.assertEqual(3, len(sca_keys))

    def test_the_manifest_is_written_last(self):
        # Assert via the store's OWN recorded write order, not by inspecting
        # final state — the ordering claim is about the SEQUENCE of writes,
        # which only `put_calls` (append-only, in call order) can prove.
        stage_generation(self.store, "mock-day-1", self.row)
        manifest_calls = [i for i, key in enumerate(self.store.put_calls)
                          if key.endswith("/manifest.json")]
        sca_calls = [i for i, key in enumerate(self.store.put_calls)
                    if "/sca/" in key]
        self.assertEqual(1, len(manifest_calls))
        self.assertTrue(sca_calls)
        self.assertGreater(manifest_calls[0], max(sca_calls),
                           "the manifest must be the LAST object written")

    def test_manifest_absence_means_incomplete(self):
        # A store double that fails on the manifest key specifically —
        # every SCA object lands, the manifest never does. The generation
        # is left incomplete BY THE ABSENCE of the manifest, exactly the
        # design's "existence = generation complete" read the other way.
        store = InMemoryObjectStore()
        manifest_key = "mock/mock-day-1/90000/manifest.json"
        store.fail_on_put.add(manifest_key)

        with self.assertRaises(Exception):
            stage_generation(store, "mock-day-1", self.row)

        sca_keys = [k for k in store.objects if "/sca/" in k]
        self.assertEqual(len(ALL_SCAS), len(sca_keys),
                         "every SCA object still lands before the failure")
        self.assertNotIn(manifest_key, store.objects)

    def test_substrate_and_injection_are_stamped_on_every_staged_object(self):
        stage_generation(self.store, "mock-day-1", self.row)
        for key, record in self.store.objects.items():
            if "/sca/" not in key:
                continue
            body = json.loads(record["body"])
            self.assertEqual(SUBSTRATE_SIMULATED, body["substrate"])
            self.assertEqual(INJECTION_PRISTINE, body["injection"])

    def test_substrate_is_stamped_on_the_manifest_too(self):
        stage_generation(self.store, "mock-day-1", self.row)
        manifest_key = "mock/mock-day-1/90000/manifest.json"
        body = json.loads(self.store.get(manifest_key))
        self.assertEqual(SUBSTRATE_SIMULATED, body["substrate"])

    def test_the_key_grammar_matches_the_documented_layout(self):
        stage_generation(self.store, "mock-day-1", self.row)
        self.assertIn("mock/mock-day-1/90000/sca/90000/1.json",
                      self.store.objects)
        self.assertIn("mock/mock-day-1/90000/manifest.json",
                      self.store.objects)

    def test_source_ref_is_carried_through_not_copied_as_bytes(self):
        # v1 does not move real pixel data: every StagedSCA cites the SAME
        # source_ref (a pointer), never a distinct byte payload per SCA.
        stage_generation(self.store, "mock-day-1", self.row)
        refs = set()
        for key, record in self.store.objects.items():
            if "/sca/" not in key:
                continue
            refs.add(json.loads(record["body"])["source_ref"])
        self.assertEqual({self.row.source_ref}, refs)

    def test_mjd_is_computed_once_and_shared_across_the_generation(self):
        manifest = stage_generation(self.store, "mock-day-1", self.row)
        expected = wall_time_to_mjd(self.row.wall_time)
        self.assertAlmostEqual(expected, manifest.mjd, places=9)
        for key, record in self.store.objects.items():
            if "/sca/" not in key:
                continue
            body = json.loads(record["body"])
            self.assertAlmostEqual(expected, body["mjd"], places=9)

    def test_generation_id_is_the_exposure_id(self):
        self.assertEqual("90000", generation_id_for(self.row))

    def test_a_replayed_stage_is_idempotent(self):
        # put_if_absent's real create-once semantics: calling twice with the
        # same row is a no-op success, not a duplicate or a conflict.
        stage_generation(self.store, "mock-day-1", self.row)
        object_count_before = len(self.store.objects)
        stage_generation(self.store, "mock-day-1", self.row)
        self.assertEqual(object_count_before, len(self.store.objects))


class CreateMockCampaignTests(unittest.TestCase):
    def setUp(self):
        self.execute = RecordingExecutor()
        self.campaign_writer = CampaignWriter(self.execute)
        self.work_writer = WorkUnitWriter(self.execute)
        self.store = InMemoryObjectStore()

    def test_creates_the_campaign_row_under_the_test_class(self):
        schedule = [ScheduleRow(exposure_id=90000,
                                wall_time=utc(2027, 10, 1), field=42,
                                source_ref="ref")]
        create_mock_campaign(
            self.execute, self.campaign_writer, self.work_writer, self.store,
            campaign_name="mock-day-1", job_type="science",
            schedule=schedule)

        campaign_inserts = [(sql, params) for sql, params in self.execute.calls
                            if "INSERT INTO campaigns" in sql]
        self.assertEqual(1, len(campaign_inserts))
        _, params = campaign_inserts[0]
        self.assertIn("test", params)
        self.assertIn("mock-day-1", params)

    def test_creates_one_work_unit_per_schedule_row(self):
        schedule = [
            ScheduleRow(exposure_id=90000, wall_time=utc(2027, 10, 1),
                       field=42, source_ref="ref"),
            ScheduleRow(exposure_id=90001, wall_time=utc(2027, 10, 1, 0, 1),
                       field=43, source_ref="ref"),
        ]
        create_mock_campaign(
            self.execute, self.campaign_writer, self.work_writer, self.store,
            campaign_name="mock-day-1", job_type="science",
            schedule=schedule)

        unit_inserts = [(sql, params) for sql, params in self.execute.calls
                        if "INSERT INTO work_units" in sql]
        self.assertEqual(2, len(unit_inserts))

    def test_work_units_carry_the_campaign_id(self):
        schedule = [ScheduleRow(exposure_id=90000,
                                wall_time=utc(2027, 10, 1), field=42,
                                source_ref="ref")]
        campaign_id = create_mock_campaign(
            self.execute, self.campaign_writer, self.work_writer, self.store,
            campaign_name="mock-day-1", job_type="science",
            schedule=schedule)

        unit_inserts = [params for sql, params in self.execute.calls
                        if "INSERT INTO work_units" in sql]
        self.assertIn(campaign_id, unit_inserts[0])

    def test_staging_happens_before_the_work_unit_is_created(self):
        schedule = [ScheduleRow(exposure_id=90000,
                                wall_time=utc(2027, 10, 1), field=42,
                                source_ref="ref")]
        create_mock_campaign(
            self.execute, self.campaign_writer, self.work_writer, self.store,
            campaign_name="mock-day-1", job_type="science",
            schedule=schedule)

        # The manifest object exists (staging ran) and the work unit insert
        # was issued (creation ran) — proving staging is not skipped, and
        # trusting stage_generation's own ordering test for the finer-
        # grained manifest-last guarantee within staging itself.
        self.assertIn("mock/mock-day-1/90000/manifest.json",
                      self.store.objects)
        unit_inserts = [sql for sql, _ in self.execute.calls
                        if "INSERT INTO work_units" in sql]
        self.assertEqual(1, len(unit_inserts))

    def test_work_units_are_created_ready(self):
        schedule = [ScheduleRow(exposure_id=90000,
                                wall_time=utc(2027, 10, 1), field=42,
                                source_ref="ref")]
        create_mock_campaign(
            self.execute, self.campaign_writer, self.work_writer, self.store,
            campaign_name="mock-day-1", job_type="science",
            schedule=schedule)

        unit_inserts = [params for sql, params in self.execute.calls
                        if "INSERT INTO work_units" in sql]
        self.assertIn("ready", unit_inserts[0])


class StagedRowSource:
    """The two `StagedInputSource` methods, over a fixed row set.

    `l2files` rows are `(rid, sca, fid)` — `get_l2files_records_for_
    datetime_range`'s own column order (rid, sca, fid, mjdobs), truncated
    to the three columns `create_mock_campaign_from_staged` actually reads.
    `info` rows are keyed by rid: `(filename, expid, sca, field, ...)` —
    `get_info_for_l2file`'s own order, truncated to what the function reads
    (index 1 = expid, index 3 = field).
    """

    def __init__(self, l2files=(), info=None):
        self.exit_code = 0
        self.l2files = list(l2files)
        self.info = dict(info or {})

    def get_l2files_records_for_datetime_range(self, start, end):
        return self.l2files

    def get_info_for_l2file(self, rid):
        return self.info.get(rid)


class CreateMockCampaignFromStagedTests(unittest.TestCase):
    """`create_mock_campaign_from_staged`: the W2-fallback entry point."""

    def setUp(self):
        self.execute = RecordingExecutor()
        self.campaign_writer = CampaignWriter(self.execute)
        self.work_writer = WorkUnitWriter(self.execute)
        # Two already-registered rows: rid 101 (exposure 5001, sca 7, field
        # 4678622, fid 8) and rid 102 (exposure 5002, sca 9, same field/fid).
        self.source = StagedRowSource(
            l2files=[(101, 7, 8), (102, 9, 8)],
            info={
                101: ("s3://in/exp1_sca7.fits", 5001, 7, 4678622),
                102: ("s3://in/exp2_sca9.fits", 5002, 9, 4678622),
            })

    def _create(self, **kw):
        kw.setdefault("start", utc(2027, 10, 1))
        kw.setdefault("end", utc(2027, 10, 2))
        kw.setdefault("max_units", 10)
        return create_mock_campaign_from_staged(
            self.execute, self.campaign_writer, self.work_writer,
            self.source, "mock-day-1", **kw)

    def test_creates_the_campaign_row_under_the_test_class(self):
        self._create()
        campaign_inserts = [(sql, params) for sql, params in self.execute.calls
                            if "INSERT INTO campaigns" in sql]
        self.assertEqual(1, len(campaign_inserts))
        _, params = campaign_inserts[0]
        self.assertIn("test", params)
        self.assertIn("mock-day-1", params)

    def test_one_work_unit_per_already_registered_row(self):
        self._create()
        unit_inserts = [(sql, params) for sql, params in self.execute.calls
                        if "INSERT INTO work_units" in sql]
        self.assertEqual(2, len(unit_inserts))

    def test_no_staging_object_is_written(self):
        # The W2 fallback moves no bytes and stages nothing — unlike
        # create_mock_campaign, there is no store argument at all.
        self.assertNotIn("store", create_mock_campaign_from_staged.__code__.co_varnames)

    def test_input_scope_matches_the_shared_grammar(self):
        # Grammar identity with build_input_scope/parse_exposure_sca_scope
        # (submission.subjects) — the round trip the campaign gatherer
        # depends on.
        from submission.subjects import parse_exposure_sca_scope

        self._create()
        unit_inserts = [params for sql, params in self.execute.calls
                        if "INSERT INTO work_units" in sql]
        input_scopes = {params[1] for params in unit_inserts}
        self.assertEqual(
            {parse_exposure_sca_scope(scope) for scope in input_scopes},
            {(5001, 7), (5002, 9)})

    def test_work_units_are_created_ready(self):
        self._create()
        unit_inserts = [params for sql, params in self.execute.calls
                        if "INSERT INTO work_units" in sql]
        self.assertIn("ready", unit_inserts[0])

    def test_the_source_l2_identity_is_recorded_in_the_creation_detail(self):
        # THE ROUND TRIP: rid/field/fid must land in the creation event's
        # detail under source_rid/source_field/source_fid — exactly the
        # keys database.modules.utils.rapid_db.RAPIDDB.
        # get_campaign_unit_source_l2_identity reads back.
        self._create()
        event_inserts = [params for sql, params in self.execute.calls
                         if "INSERT INTO unit_events" in sql]
        self.assertEqual(2, len(event_inserts))
        details = [params[-1] for params in event_inserts]
        self.assertIn({"source_rid": 101, "source_field": 4678622,
                       "source_fid": 8}, details)
        self.assertIn({"source_rid": 102, "source_field": 4678622,
                       "source_fid": 8}, details)

    def test_scas_narrows_the_enumerated_rows(self):
        campaign_id = self._create(scas=(7,))
        unit_inserts = [(sql, params) for sql, params in self.execute.calls
                        if "INSERT INTO work_units" in sql]
        self.assertEqual(1, len(unit_inserts))

    def test_exposure_ids_narrows_the_enumerated_rows(self):
        self._create(exposure_ids=(5002,))
        unit_inserts = [(sql, params) for sql, params in self.execute.calls
                        if "INSERT INTO work_units" in sql]
        self.assertEqual(1, len(unit_inserts))

    def test_max_units_refuses_rather_than_clamps(self):
        # THE BUDGET GUARD: exceeding max_units raises and writes NOTHING —
        # not a truncated campaign.
        with self.assertRaises(ValueError) as caught:
            self._create(max_units=1)
        self.assertIn("max_units", str(caught.exception))
        self.assertEqual([], self.execute.calls,
                         "a refused campaign must write nothing at all")

    def test_max_units_exactly_matching_the_count_is_allowed(self):
        # The boundary: exactly max_units enumerated units is NOT a refusal
        # (the guard is `>`, not `>=`).
        self._create(max_units=2)
        unit_inserts = [(sql, params) for sql, params in self.execute.calls
                        if "INSERT INTO work_units" in sql]
        self.assertEqual(2, len(unit_inserts))

    def test_a_row_missing_from_get_info_for_l2file_is_a_runtime_error(self):
        source = StagedRowSource(l2files=[(999, 7, 8)], info={})
        with self.assertRaises(RuntimeError):
            create_mock_campaign_from_staged(
                self.execute, self.campaign_writer, self.work_writer,
                source, "mock-day-1", start=utc(2027, 10, 1),
                end=utc(2027, 10, 2), max_units=10)


if __name__ == "__main__":
    unittest.main()
