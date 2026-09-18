"""Section B of the test plan: AlertDataProvider behavior over a fake DB
and a synthetic on-disk job directory (see conftest.py).

Priority tests implemented here:
  B9  cutout staging policy -- transient S3 failures retry and recover;
      a persistent failure or a missing/unreadable image aborts the chip
      loudly (CutoutStagingError) rather than silently shipping null
      cutouts for every source on it
  B11 batch/single equivalence -- the same source produced through
      produce_alert() and through batch_produce() must yield
      byte-identical alerts, pinning the prefetch path to the per-query
      path

TODO (test plan, not yet implemented):
  B8  grid-mismatch guard: a template written with a shifted CRVAL gets
      null cutouts (and a warning), the other images unaffected
  B10 per-chip caching: images load once per pid (count load_fits_image
      calls); pid change reloads and replaces staged files
  B12 prefetch semantics: prv window filtering per-trigger, multi-field
      chips, independent ObjectRecords for sources sharing an object
  E20 CLI surface: --diff-flavor choices enforced, bad args exit nonzero
"""

import io
import json
import os
import shutil
from pathlib import Path

import fastavro
import pytest

from alerts.produce import (CUTOUT_FIELDS, MATCH_FIELDS, BatchStats,
                            batch_produce, load_schema, open_alert_archive,
                            produce_alert)
from alerts.providers import (ALERTABLE_FLAGS, AlertDataProvider,
                              AssociationError, CutoutStagingError,
                              FlaggedSourceError)

from conftest import CHIP_PID, PRODUCT_OFFSETS, FakeDB, make_source_row
from test_clips import clip_to_numpy


# ---------------------------------------------------------------------------
# resolve_pid: (exposure, SCA) -> the newest vbest>0 processing. The fake
# chip_data.campaigns mirrors the real database's reprocessing-campaign
# mess: several vbest=1 rows per (expid, sca), plus a newer vbest=0 row
# that must NOT win.
# ---------------------------------------------------------------------------

def test_resolve_pid_picks_newest_best_campaign(make_provider):
    provider = make_provider()
    # newest vbest>0 is CHIP_PID (99); pid 100 is newer but vbest=0
    assert provider.resolve_pid(42, 7) == CHIP_PID


def test_resolve_pid_unknown_exposure_raises(make_provider):
    with pytest.raises(ValueError, match="expid=1 sca=1"):
        make_provider().resolve_pid(1, 1)


# ---------------------------------------------------------------------------
# missing associations: source cross-matching creates an object for every
# flags = 0 source, and the alert path selects exactly that population
# (providers.ALERTABLE_FLAGS), so by alert time every detection it handles
# has a merges row by definition. A missing merges_<field> partition (seen
# live: pid 339271 -> merges_4686817) or a missing merges row means
# cross-matching did not run or failed -- the provider raises
# AssociationError in both flows instead of shipping an object-less alert.
# A missing partition still aborts the chip (it fails in the prefetch); a
# missing row is dropped per source by batch_produce(), see below.
# ---------------------------------------------------------------------------

def test_missing_field_partition_aborts(make_provider, chip_data):
    chip_data.partitions_exist = False
    provider = make_provider()
    with pytest.raises(AssociationError, match="merges_"):
        list(provider.iter_sources(CHIP_PID))             # batch prefetch

    single = make_provider()                              # single-alert flow
    detection = single.get_detection(9001)
    with pytest.raises(AssociationError, match="merges_"):
        single.get_object_for_source(detection)


def test_missing_merges_row_aborts(make_provider, chip_data):
    del chip_data.merges[9003]        # cross-matching missed one source
    provider = make_provider()
    sources = list(provider.iter_sources(CHIP_PID))       # prefetch is fine
    trigger = next(s for s in sources if s.sid == 9003)
    with pytest.raises(AssociationError, match="sid=9003"):
        provider.get_object_for_source(trigger)


def test_missing_merges_row_is_dropped_by_batch_produce(make_provider,
                                                       chip_data, tmp_path,
                                                       caplog):
    del chip_data.merges[9003]        # cross-matching missed one source
    stats = BatchStats()
    path = str(tmp_path / "alerts.avro")
    with caplog.at_level("WARNING", logger="alerts.produce"):
        with open_alert_archive(path) as archive:
            count = batch_produce(make_provider(), CHIP_PID, archive=archive,
                                  stats=stats)

    # the other two sources still make it into the archive
    assert count == 2
    with open(path, "rb") as f:
        assert [a["diaSourceId"] for a in fastavro.reader(f)] == [9001, 9002]
    # and the drop is logged and recorded, naming the source and the reason
    assert "sid=9003" in caplog.text
    assert stats.n_alerts == 2 and stats.n_failed == 1
    [failure] = stats.failures
    assert failure["sid"] == 9003
    assert failure["error"] == "AssociationError"
    assert "sid=9003" in failure["message"]


# ---------------------------------------------------------------------------
# BatchStats: what batch_produce() reports about a chip's archive, at INFO,
# so a run can be checked without decoding the Avro. Over the fake chip:
# object 777 (sid 9001) has two prior detections inside the look-back
# window, 888 and 999 have none; no provider fills forced photometry; no
# KONA file, no reference catalog registered and no NED reader, so every
# match list is null (= could not run) rather than empty.
# ---------------------------------------------------------------------------

def test_batch_stats_describe_the_archive(make_provider, chip_data, caplog):
    stats = BatchStats()
    with caplog.at_level("INFO", logger="alerts.produce"):
        count = batch_produce(make_provider(), CHIP_PID, stats=stats)

    assert stats.pid == CHIP_PID
    assert stats.n_alerts == count == len(chip_data.sources) == 3
    assert stats.n_failed == 0 and stats.failures == []
    assert stats.n_bytes > 0
    assert (stats.n_with_prv, stats.n_prv_total, stats.max_prv) == (1, 2, 2)
    assert stats.n_with_forced == 0
    assert stats.cutouts_present == {f: 3 for f in CUTOUT_FIELDS}
    for field in MATCH_FIELDS:
        assert stats.match_states[field] == {"null": 3, "empty": 0,
                                             "matched": 0}, field
    # the INFO summary carries the headline numbers
    assert f"pid={CHIP_PID}: 3 alerts archived, 0 sources dropped" in caplog.text
    assert "1 alerts with prior detections (mean 2.0, max 2)" in caplog.text
    # and the whole thing is JSON-ready for the stage's summary files
    json.loads(json.dumps(stats.as_dict()))


# ---------------------------------------------------------------------------
# object statistics live on astroobjectsmeta_<field>, not astroobjects_<field>
# (Russ split them 2026-07-29; the first run against the current schema
# failed every alert with "column a.stdevra does not exist"). Statistics
# runs after cross-matching, so the meta table -- or one aid's row in it --
# can legitimately be absent: the association still stands, the sigmas go
# null (raSigma/decSigma are nullable) and nsources falls back to the
# merges count, which is the very thing statistics counts.
# ---------------------------------------------------------------------------

def test_object_statistics_come_from_the_meta_table(make_provider, chip_data):
    provider = make_provider()
    obj = provider.get_object_for_source(provider.get_detection(9001))
    expected = chip_data.objects[777]
    assert (obj.aid, obj.ra0, obj.dec0) == (777, expected["ra0"], expected["dec0"])
    assert (obj.stdevra, obj.stdevdec, obj.nsources) == (
        expected["stdevra"], expected["stdevdec"], expected["nsources"])


def test_missing_meta_table_gives_null_sigmas_and_merge_count(make_provider,
                                                              chip_data):
    chip_data.meta_exists = False              # statistics never ran here
    schema = load_schema()

    single = make_provider()
    obj = single.get_object_for_source(single.get_detection(9001))
    assert (obj.stdevra, obj.stdevdec) == (None, None)
    assert obj.nsources == 3                   # 9001 + history 1001, 1002
    # the alert still serializes: raSigma/decSigma are nullable
    alert = fastavro.schemaless_reader(
        io.BytesIO(produce_alert(single, 9001, schema=schema)), schema)
    assert alert["diaObject"]["raSigma"] is None
    assert alert["diaObject"]["nDiaSources"] == 3

    # and the batch prefetch agrees with the single path
    batch = make_provider()
    sources = {s.sid: s for s in batch.iter_sources(CHIP_PID)}
    assert batch.get_object_for_source(sources[9001]).nsources == 3
    assert batch.get_object_for_source(sources[9002]).nsources == 1
    assert batch.get_object_for_source(sources[9002]).stdevra is None


def test_missing_meta_row_falls_back_for_that_object_only(make_provider,
                                                          chip_data):
    chip_data.stats_missing_aids = {888}       # 9002's object has no row
    provider = make_provider()
    sources = {s.sid: s for s in provider.iter_sources(CHIP_PID)}
    with_stats = provider.get_object_for_source(sources[9001])
    without = provider.get_object_for_source(sources[9002])
    assert with_stats.stdevra == chip_data.objects[777]["stdevra"]
    assert (without.stdevra, without.stdevdec, without.nsources) == (None, None, 1)


# ---------------------------------------------------------------------------
# a failed query must not poison the connection. psycopg2 keeps a
# connection whose statement failed in an aborted transaction until it is
# rolled back; without that, every later query on the same connection
# raises InFailedSqlTransaction. Live 2026-09-18: the stage's 18 workers
# each hit one real error, and their remaining 7362 chips all failed with
# exactly that. Reads only, so the provider rolls back after every query.
# ---------------------------------------------------------------------------

def test_failed_query_is_rolled_back_and_the_connection_stays_usable(
        make_provider, monkeypatch):
    provider = make_provider()
    conn = provider.db.conn
    from conftest import FakeCursor
    real_execute = FakeCursor.execute
    calls = {"n": 0}

    def failing_once(self, sql, params):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("column a.stdevra does not exist")
        return real_execute(self, sql, params)

    monkeypatch.setattr(FakeCursor, "execute", failing_once)
    with pytest.raises(RuntimeError, match="stdevra"):
        provider.get_detection(9001)
    assert conn.rollbacks == 1                     # the failed read was closed out
    # the next query on the same connection works and is closed out too
    assert provider.get_detection(9001).sid == 9001
    assert conn.rollbacks == 2


# ---------------------------------------------------------------------------
# flagged detections are not alertable. The cross-match associates only
# flags = 0 sources (pipeline/crossMatchSources.py), so the alert path must
# select the same population -- providers.ALERTABLE_FLAGS. Before this rule
# was applied here, every live chip (21-26% flagged) aborted with
# AssociationError at its first flagged source and no batch could complete.
# ---------------------------------------------------------------------------

def test_flagged_detections_are_skipped_by_the_batch_path(
        make_provider, chip_data, tpv_header, caplog):
    # a failed PSF fit (bit 4, non-positive fitted flux) that the
    # cross-match never associated -- exactly what live chips carry
    chip_data.sources.append(
        make_source_row(9005, 50.0, 60.0, 60500.5, tpv_header, flags=4))
    provider = make_provider()
    with caplog.at_level("INFO", logger="alerts.providers"):
        sids = [s.sid for s in provider.iter_sources(CHIP_PID)]

    assert sids == [9001, 9002, 9003]                 # 9005 never yielded
    assert "1 flagged detections" in caplog.text     # and it is accounted for
    assert ALERTABLE_FLAGS == 0                       # the rule this pins


def test_flagged_sid_is_refused_on_the_single_alert_path(
        make_provider, chip_data, tpv_header):
    chip_data.sources.append(
        make_source_row(9005, 50.0, 60.0, 60500.5, tpv_header, flags=4))
    provider = make_provider()
    # the refusal names the sid, the flags, and the rule -- and is NOT an
    # AssociationError, which would wrongly claim the database is broken
    with pytest.raises(FlaggedSourceError, match=r"sid=9005 .*flags=4"):
        provider.get_detection(9005)
    with pytest.raises(FlaggedSourceError):
        produce_alert(provider, 9005)
    # an unflagged neighbour is unaffected
    assert provider.get_detection(9001).sid == 9001


# ---------------------------------------------------------------------------
# diff_flavor selection (plan B7): the flavor argument picks which
# difference image feeds cutoutDifference; each product carries a distinct
# DC offset, so the stamp values identify the file that was read
# ---------------------------------------------------------------------------

def test_diff_flavor_selects_difference_image(make_provider, chip_data):
    detection_row = chip_data.sources[0]
    for flavor, product in (("sfft", "sfftdiffimage_masked.fits"),
                            ("zogy", "zogy_diffimage_masked.fits")):
        provider = make_provider(diff_flavor=flavor)
        detection = provider.get_detection(detection_row["sid"])
        diff, _ = clip_to_numpy(provider.get_cutouts(detection).difference)
        base = (round(detection_row["yfit"]) * 1000
                + round(detection_row["xfit"]))
        assert diff[64, 64] == base + PRODUCT_OFFSETS[product]


def test_invalid_diff_flavor_rejected_at_construction(chip_data):
    with pytest.raises(ValueError):
        AlertDataProvider(FakeDB(chip_data), diff_flavor="hotpants")


def test_provider_close_removes_staging_directory(make_provider):
    provider = make_provider()
    staging_dir = Path(provider._staging_dir)
    assert staging_dir.is_dir()

    provider.close()
    provider.close()                         # cleanup is deliberately idempotent

    assert not staging_dir.exists()


def test_provider_context_manager_removes_staging_directory(chip_data):
    with AlertDataProvider(FakeDB(chip_data)) as provider:
        staging_dir = Path(provider._staging_dir)
        assert staging_dir.is_dir()
    assert not staging_dir.exists()


# ---------------------------------------------------------------------------
# B9: the degradation ladder. Cutouts are best-effort; every failure mode
# ends in null cutouts and a completed alert, never an exception.
# ---------------------------------------------------------------------------

def test_missing_product_file_aborts(make_provider, chip_data, job_dir):
    # one image absent (local staging: file simply gone) must abort the
    # whole chip, not null just that cutout -- the file would be missing
    # for every source on the chip
    (job_dir / "awaicgen_output_mosaic_image_resampled_gainmatched.fits"
     ).unlink()
    provider = make_provider()
    with pytest.raises(CutoutStagingError, match="ref"):
        provider.get_cutouts(
            provider.get_detection(chip_data.sources[0]["sid"]))


def test_no_diffimages_row_aborts(make_provider, chip_data):
    chip_data.diff_filename = None
    provider = make_provider()
    with pytest.raises(CutoutStagingError, match="no diffimages row"):
        provider.get_cutouts(
            provider.get_detection(chip_data.sources[0]["sid"]))


class _FlakyS3:
    """Stand-in S3 client: fails the first `fail_times` download_file calls
    with a transient error, then serves the real fixture file from the
    on-disk job_dir. head_object returns that file's true size."""

    def __init__(self, job_dir, fail_times):
        self.job_dir = job_dir
        self.remaining = fail_times
        self.calls = 0

    def download_file(self, bucket, key, local):
        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise ConnectionError("simulated transient S3 error")
        shutil.copy(os.path.join(self.job_dir, os.path.basename(key)), local)

    def head_object(self, Bucket, Key):
        path = os.path.join(self.job_dir, os.path.basename(Key))
        return {"ContentLength": os.path.getsize(path)}


def _use_s3(monkeypatch, chip_data, job_dir, client):
    """Point staging at s3:// (so _stage runs), hand it `client`, and make
    backoff instant."""
    import boto3
    monkeypatch.setattr(boto3, "client", lambda *a, **k: client)
    monkeypatch.setattr("alerts.providers.STAGE_BACKOFF_BASE_S", 0.0)
    # an s3:// diff filename forces the download path; the job dir basename
    # is irrelevant since _FlakyS3 keys off the product basename
    chip_data.diff_filename = (
        "s3://bucket/20260706/jid1/zogy_diffimage_masked.fits")


def test_s3_staging_retries_then_recovers(make_provider, chip_data,
                                          job_dir, monkeypatch):
    # one transient failure on the first download must be absorbed by the
    # retry, not lost -- all three cutouts still produced
    flaky = _FlakyS3(job_dir, fail_times=1)
    _use_s3(monkeypatch, chip_data, job_dir, flaky)
    provider = make_provider()

    cutouts = provider.get_cutouts(
        provider.get_detection(chip_data.sources[0]["sid"]))
    assert None not in (cutouts.difference, cutouts.science,
                        cutouts.template)
    assert flaky.calls == 4          # 1 failed + 3 successful downloads


def test_s3_staging_failure_aborts_after_retries(make_provider, chip_data,
                                                 job_dir, monkeypatch):
    # a persistent failure exhausts the retries and aborts loudly; call
    # _stage directly with an explicit retries to exercise the argument
    always = _FlakyS3(job_dir, fail_times=10**9)
    _use_s3(monkeypatch, chip_data, job_dir, always)
    provider = make_provider()

    with pytest.raises(CutoutStagingError, match="after 3 attempts"):
        provider._stage("s3://bucket/jid1/sfftdiffimage_masked.fits",
                        retries=3)
    assert always.calls == 3         # tried exactly `retries` times


def test_s3_staging_default_retries_is_five(make_provider, chip_data,
                                            job_dir, monkeypatch):
    # the default attempt count is 5 (the documented default), verified
    # without hardcoding it in the production code
    always = _FlakyS3(job_dir, fail_times=10**9)
    _use_s3(monkeypatch, chip_data, job_dir, always)
    provider = make_provider()

    with pytest.raises(CutoutStagingError):
        provider._stage("s3://bucket/jid1/sfftdiffimage_masked.fits")
    assert always.calls == 5


# ---------------------------------------------------------------------------
# B11: batch/single equivalence. batch_produce() answers from set-based
# prefetches while produce_alert() issues per-source queries; any semantic
# drift between the two paths shows up as differing bytes. Byte-identity
# is deliberately strict -- it also catches nondeterminism in the clip
# serialization itself. The one legitimate difference is timeProcessedMjd
# (stamped from the wall clock at assembly), so alerts are compared after
# nulling it and re-serializing.
# ---------------------------------------------------------------------------

def _bytes_without_time_processed(alert_bytes, schema):
    """Re-serialize an alert with every timeProcessedMjd nulled."""
    alert = fastavro.schemaless_reader(io.BytesIO(alert_bytes), schema)
    alert["diaSource"]["timeProcessedMjd"] = None
    for prv in alert["prvDiaSources"] or []:
        prv["timeProcessedMjd"] = None
    buf = io.BytesIO()
    fastavro.schemaless_writer(buf, schema, alert)
    return buf.getvalue()

class CapturingProducer:
    """Stands in for confluent_kafka.Producer; keeps the alert bytes."""

    def __init__(self):
        self.messages = []
        self.flushes = 0

    def produce(self, topic, value, callback=None):
        self.messages.append(value)

    def flush(self):
        self.flushes += 1


# ---------------------------------------------------------------------------
# --save archives: one Avro object-container file per run, self-describing
# (schema embedded), read back with fastavro.reader alone
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("codec", ["deflate", "null"])
def test_alert_archive_round_trips(make_provider, chip_data, tmp_path, codec):
    path = str(tmp_path / "alerts.avro")
    with open_alert_archive(path, codec=codec) as archive:
        count = batch_produce(make_provider(), CHIP_PID, archive=archive)

    with open(path, "rb") as f:
        alerts = list(fastavro.reader(f))   # codec read from file header
    assert len(alerts) == count == len(chip_data.sources)
    assert ([a["diaSourceId"] for a in alerts]
            == sorted(row["sid"] for row in chip_data.sources))
    # cutouts survive as complete FITS files
    assert alerts[0]["cutoutDifference"].startswith(b"SIMPLE")


def test_batch_and_single_paths_produce_identical_bytes(make_provider,
                                                        chip_data):
    schema = load_schema()

    # batch flow: one provider, whole chip through the prefetch path
    producer = CapturingProducer()
    count = batch_produce(make_provider(), CHIP_PID, producer=producer,
                          schema=schema)
    assert count == len(chip_data.sources)
    assert producer.flushes == 1               # one flush per chip, at the end

    # single-alert flow: a fresh provider per source so no chip prefetch
    # state can leak in
    batch_by_sid = dict(zip(sorted(r["sid"] for r in chip_data.sources),
                            producer.messages))
    for row in chip_data.sources:
        single = produce_alert(make_provider(), row["sid"], schema=schema)
        assert (_bytes_without_time_processed(single, schema)
                == _bytes_without_time_processed(batch_by_sid[row["sid"]],
                                                 schema)), \
            f"batch and single alerts differ for sid={row['sid']}"
