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
  E20 CLI surface: bad args exit nonzero
"""


import os
import shutil
import ast
import inspect
import fastavro
import pytest

from alerts import providers
from alerts.produce import (assemble_alert_for_source, batch_produce,
                            load_schema, open_alert_archive, produce_alert)
from alerts.providers import AlertDataProvider, CutoutStagingError


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
# missing per-field partitions: the real DB can lack merges_<field>/
# astroobjects_<field> for a chip's field (seen live: pid 339271 ->
# merges_4686817). Sources there must degrade to unassociated -- alerts
# with no diaObject -- in both flows, never crash the batch.
# ---------------------------------------------------------------------------

def test_missing_field_partition_degrades_to_unassociated(make_provider,
                                                          chip_data):
    chip_data.partitions_exist = False
    provider = make_provider()
    sources = list(provider.iter_sources(CHIP_PID))       # batch prefetch
    assert len(sources) == len(chip_data.sources)
    for source in sources:
        assert provider.get_object_for_source(source) is None

    single = make_provider()                              # single-alert flow
    detection = single.get_detection(9001)
    assert single.get_object_for_source(detection) is None


# ---------------------------------------------------------------------------
# History is strict prior (ruled 2026-08-13): prvDiaSources and
# diaObject.lastMjd must admit only detections with mjdobs strictly before
# the triggering detection's own mjdobs. Backfill/reprocessing can leave
# later-mjd rows in `sources` ahead of an earlier trigger being (re)alerted,
# and a same-instant detection is deliberately not history either. Checked
# in both the batch prefetch path (iter_sources -> get_prv_detections) and
# the single-alert path (produce_alert -> get_prv_detections), which must
# agree.
# ---------------------------------------------------------------------------

def test_history_excludes_future_and_same_instant_detections(make_provider,
                                                              chip_data,
                                                              tpv_header):
    trigger_sid = 9001                 # merged to aid 777, mjdobs 60500.5
    trigger_mjdobs = 60500.5
    future_sid, same_instant_sid = 9101, 9102
    chip_data.history += [
        make_source_row(future_sid, 150.2, 200.4, trigger_mjdobs + 1.0,
                        tpv_header, pid=44),
        make_source_row(same_instant_sid, 150.2, 200.4, trigger_mjdobs,
                        tpv_header, pid=44),
    ]
    chip_data.history_merges[future_sid] = 777
    chip_data.history_merges[same_instant_sid] = 777

    # single-alert path
    single = make_provider()
    detection = single.get_detection(trigger_sid)
    obj = single.get_object_for_source(detection)
    prv = single.get_prv_detections(detection, obj)
    prv_sids = {s.sid for s in prv}
    assert future_sid not in prv_sids
    assert same_instant_sid not in prv_sids
    assert prv_sids == {1001, 1002}
    assert max(s.mjdobs for s in prv) < trigger_mjdobs

    # batch path: same assertions from the chip-wide prefetch
    batch = make_provider()
    sources = {s.sid: s for s in batch.iter_sources(CHIP_PID)}
    batch_detection = sources[trigger_sid]
    batch_obj = batch.get_object_for_source(batch_detection)
    batch_prv = batch.get_prv_detections(batch_detection, batch_obj)
    batch_prv_sids = {s.sid for s in batch_prv}
    assert future_sid not in batch_prv_sids
    assert same_instant_sid not in batch_prv_sids
    assert batch_prv_sids == {1001, 1002}
    assert max(s.mjdobs for s in batch_prv) < trigger_mjdobs

    # diaObject.lastDiaSourceMjd must never postdate validityStartMjd
    packet = assemble_alert_for_source(make_provider(), detection)
    assert (packet["diaObject"]["lastDiaSourceMjd"]
            == packet["diaObject"]["validityStartMjd"] == trigger_mjdobs)


# ---------------------------------------------------------------------------
# The cutout comes from the REGISTERED difference image (the vocabulary
# ruling's third gate). The release binds the difference-image role to one
# product, that product is what registered, and diffimages.filename names
# it — so the provider follows the row rather than an algorithm of its own.
# Each product carries a distinct DC offset, so the stamp values identify
# the file that was read.
# ---------------------------------------------------------------------------

def test_cutout_comes_from_the_registered_difference_image(make_provider,
                                                           chip_data):
    detection_row = chip_data.sources[0]
    base = (round(detection_row["yfit"]) * 1000
            + round(detection_row["xfit"]))
    for product in ("sfftdiffimage_masked.fits", "zogy_diffimage_masked.fits"):
        # Rebinding the role changes what registers, so the row's filename
        # changes; the cutout must follow it with no argument anywhere.
        chip_data.diff_filename = str(chip_data.job_dir / product)
        provider = make_provider()
        detection = provider.get_detection(detection_row["sid"])
        diff, _ = clip_to_numpy(provider.get_cutouts(detection).difference)
        assert diff[64, 64] == base + PRODUCT_OFFSETS[product]


def test_the_provider_carries_no_algorithm_literal():
    """THE ANTI-LITERAL GUARD: a consumer must not reintroduce one.

    The ruling's third gate is that no consumer of a role-named product
    carries an algorithm literal — the alert layer's `zogy` cutout literal
    is the one it names. This fails if a future change puts an algorithm
    name back into the provider, whether as a constructor argument, a
    flavour map, or a bare basename.
    """
    # Prose may DISCUSS the algorithms; code may not select one. Comments
    # never reach the AST, and every docstring is dropped explicitly, so
    # what remains is the executable text — string literals included,
    # because a basename in a dict is exactly the defect being guarded.
    tree = ast.parse(inspect.getsource(providers))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = node.body
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            node.body = body[1:] or [ast.Pass()]
    code = ast.unparse(ast.fix_missing_locations(tree)).lower()
    for literal in ("zogy", "sfft", "hotpants", "naive"):
        assert literal not in code, (
            f"alerts/providers.py names the {literal!r} algorithm; the "
            "difference image is chosen by the release's role binding and "
            "reaches this module as diffimages.filename")


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
# serialization itself.
# ---------------------------------------------------------------------------

class CapturingProducer:
    """Stands in for the injected producer; keeps the alert bytes."""

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
    assert ([a["diaSourceId"] for a in alerts] # pyright: ignore
            == sorted(row["sid"] for row in chip_data.sources))
    # cutouts survive as complete FITS files
    assert alerts[0]["cutoutDifference"].startswith(b"SIMPLE") # pyright: ignore


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
        assert single == batch_by_sid[row["sid"]], \
            f"batch and single alerts differ for sid={row['sid']}"
