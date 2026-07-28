"""Section B of the test plan: AlertDataProvider behavior over a fake DB
and a synthetic on-disk job directory (see conftest.py).

Priority tests implemented here:
  B9  degradation ladder -- cutout failures must degrade to null cutouts,
      never crash a production run (regression for the live S3
      EndpointConnectionError crash of 2026-07-13)
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

import fastavro
import pytest

from rapid_alerts.produce import (batch_produce, load_schema,
                                  open_alert_archive, produce_alert)
from rapid_alerts.providers import AlertDataProvider

from conftest import CHIP_PID, PRODUCT_OFFSETS, FakeDB
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


# ---------------------------------------------------------------------------
# B9: the degradation ladder. Cutouts are best-effort; every failure mode
# ends in null cutouts and a completed alert, never an exception.
# ---------------------------------------------------------------------------

def test_missing_product_file_nulls_only_that_cutout(make_provider,
                                                     chip_data, job_dir):
    (job_dir / "awaicgen_output_mosaic_image_resampled_gainmatched.fits"
     ).unlink()
    provider = make_provider()
    cutouts = provider.get_cutouts(
        provider.get_detection(chip_data.sources[0]["sid"]))
    assert cutouts.template is None
    assert cutouts.difference is not None
    assert cutouts.science is not None


def test_no_diffimages_row_nulls_all_cutouts(make_provider, chip_data):
    chip_data.diff_filename = None
    provider = make_provider()
    cutouts = provider.get_cutouts(
        provider.get_detection(chip_data.sources[0]["sid"]))
    assert (cutouts.difference, cutouts.science, cutouts.template) \
        == (None, None, None)


def test_s3_staging_failure_degrades_to_null_cutouts(make_provider,
                                                     chip_data, monkeypatch):
    """Regression: a transient S3 failure used to abort the whole run."""
    import boto3

    def refuse(*args, **kwargs):
        raise ConnectionError("simulated S3 outage")

    monkeypatch.setattr(boto3, "client", refuse)
    # an s3:// filename forces the staging path (local paths bypass it)
    chip_data.diff_filename = ("s3://no-such-bucket/20260706/jid1/"
                               "zogy_diffimage_masked.fits")
    provider = make_provider()

    # the alert must still be produced, just without cutouts
    blob = produce_alert(provider, chip_data.sources[0]["sid"],
                         schema=load_schema())
    assert len(blob) > 0
    cutouts = provider.get_cutouts(
        provider.get_detection(chip_data.sources[0]["sid"]))
    assert (cutouts.difference, cutouts.science, cutouts.template) \
        == (None, None, None)


# ---------------------------------------------------------------------------
# B11: batch/single equivalence. batch_produce() answers from set-based
# prefetches while produce_alert() issues per-source queries; any semantic
# drift between the two paths shows up as differing bytes. Byte-identity
# is deliberately strict -- it also catches nondeterminism in the clip
# serialization itself.
# ---------------------------------------------------------------------------

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
        assert single == batch_by_sid[row["sid"]], \
            f"batch and single alerts differ for sid={row['sid']}"
