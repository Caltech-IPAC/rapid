"""
Tests for pipeline/produceAlertsForProcDate.py, the launcher-side alert stage
the VPO runs after pruneNotBestMerges.py, over the fake chip from conftest.

Covers: the [ALERTS] settings (and the refusal to start with Kafka
publication requested), the date -> chips database lookup over a stub
RAPIDDB, one chip's archive + statistics, the worker loop's per-chip
failure recording, and the run summary's exit codes. No database, no S3:
upload_to_s3_bucket is False throughout, so products stay in tmp_path.
"""

import configparser
import json
import multiprocessing

import fastavro
import pytest

from pipeline import produceAlertsForProcDate as stage
from alerts.providers import AlertDataProvider

from conftest import CHIP_PID, FakeDB


PROC_DATE = "20260916"


def _config(**alerts_overrides):
    cfg = configparser.ConfigParser()
    cfg["JOB_PARAMS"] = {"upload_to_s3_bucket": "False",
                         "product_s3_bucket_base": "rapid-product-files"}
    cfg["SCI_IMAGE"] = {"ppid": "15"}
    cfg["ALERTS"] = {"diff_flavor": "sfft", "refcat_match": "True",
                     "ned_match": "False", "kona_file": "",
                     "archive_filename_base": "alerts_jid",
                     "archive_codec": "deflate", "publish_to_kafka": "False",
                     "log_level": "INFO", **alerts_overrides}
    return cfg


def _settings(**alerts_overrides):
    return stage.read_alert_settings(_config(**alerts_overrides))


def _chip(jid, pid=CHIP_PID):
    return {"jid": jid, "rid": 10 + jid, "pid": pid, "expid": 42, "sca": 7,
            "field": 4686817, "fid": 1}


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------

def test_settings_are_read_from_the_alerts_section():
    s = _settings()
    assert s["ppid"] == 15
    assert s["diff_flavor"] == "sfft"
    assert s["refcat_match"] is True and s["ned_match"] is False
    assert s["lvs_match"] is True                  # absent -> on, like refcat
    assert s["kona_file"] is None                  # blank -> association off
    assert s["upload_to_s3_bucket"] is False
    assert s["archive_codec"] == "deflate"
    # ned_source: absent -> the pipeline's default copy; blank -> None (off)
    from alerts.ned_reader import DEFAULT_NED_SOURCE
    assert s["ned_source"] == DEFAULT_NED_SOURCE
    assert _settings(ned_source="s3://other/ned")["ned_source"] == "s3://other/ned"
    assert _settings(ned_source="")["ned_source"] is None


def test_build_ned_reader_opens_a_copy_or_degrades(tmp_path, caplog):
    from alerts.cli import build_ned_reader
    from alerts.ned_reader import Hp6NedReader
    assert build_ned_reader(None) is None
    assert build_ned_reader(str(tmp_path), enabled=False) is None
    reader = build_ned_reader(str(tmp_path))              # a (bare) local copy opens
    assert isinstance(reader, Hp6NedReader) and not reader.complete
    with caplog.at_level("WARNING", logger="alerts.cli"):
        assert build_ned_reader("s3://no-such-bucket-rapid-test/ned") is None
    assert "NED matching off" in caplog.text               # cause logged, no raise


def test_donotuploadproducts_overrides_the_config(monkeypatch):
    monkeypatch.delenv("DONOTUPLOADPRODUCTS", raising=False)
    cfg = _config()
    cfg["JOB_PARAMS"]["upload_to_s3_bucket"] = "True"
    assert stage.read_alert_settings(cfg)["upload_to_s3_bucket"] is True
    monkeypatch.setenv("DONOTUPLOADPRODUCTS", "1")      # any value, like Russ's scripts
    assert stage.read_alert_settings(cfg)["upload_to_s3_bucket"] is False


def test_kafka_publication_is_refused():
    with pytest.raises(NotImplementedError, match="publish_to_kafka"):
        _settings(publish_to_kafka="True")


# ---------------------------------------------------------------------------
# date -> chips lookup over a stub RAPIDDB
# ---------------------------------------------------------------------------

class StubRAPIDDB:
    """Just the three RAPIDDB methods the lookup uses, with canned rows."""

    def __init__(self, fail_on=None):
        self.exit_code = 0
        self.fail_on = fail_on
        self.jobs = {1: {"rid": 11, "expid": 42, "sca": 7, "field": 4686817,
                         "fid": 1},
                     2: {"rid": 12, "expid": 42, "sca": 8, "field": 4686817,
                         "fid": 1},
                     3: {"rid": 13, "expid": 43, "sca": 7, "field": 4686818,
                         "fid": 1}}
        # rid 13 has no best difference image
        self.diffimages = {(11, 15): {"pid": 501}, (12, 15): {"pid": 502},
                           (13, 15): {}}

    def get_jids_of_normal_science_pipeline_jobs_for_processing_date(self, d):
        if self.fail_on == "jids":
            self.exit_code = 67
            return None
        return list(self.jobs)

    def get_info_for_job(self, jid):
        return self.jobs[jid]

    def get_best_difference_image(self, rid, ppid):
        if self.fail_on == "diffimage":
            self.exit_code = 67
        return self.diffimages[(rid, ppid)]


def test_lookup_maps_normal_jobs_to_best_pids(capsys):
    chips = stage.lookup_chips_for_processing_date(StubRAPIDDB(), PROC_DATE, 15)
    assert [(c["jid"], c["rid"], c["pid"], c["sca"]) for c in chips] \
        == [(1, 11, 501, 7), (2, 12, 502, 8)]
    # the job without a best difference image is skipped, visibly
    assert "jid=3" in capsys.readouterr().out


def test_limit_chips_keeps_the_lowest_jids():
    chips = [_chip(7), _chip(5), _chip(6)]
    assert stage.limit_chips(chips, None) == chips          # unset: untouched
    assert stage.limit_chips(chips, "") == chips            # blank: untouched
    assert [c["jid"] for c in stage.limit_chips(chips, "2")] == [5, 6]
    assert [c["jid"] for c in stage.limit_chips(chips, "10")] == [5, 6, 7]
    for bad in ("0", "-3", "two"):
        with pytest.raises(ValueError):
            stage.limit_chips(chips, bad)


@pytest.mark.parametrize("fail_on", ["jids", "diffimage"])
def test_lookup_raises_on_database_error(fail_on):
    with pytest.raises(RuntimeError, match="Error getting"):
        stage.lookup_chips_for_processing_date(StubRAPIDDB(fail_on), PROC_DATE, 15)


# ---------------------------------------------------------------------------
# one chip: archive + statistics
# ---------------------------------------------------------------------------

def test_produce_chip_writes_archive_and_stats(make_provider, chip_data,
                                              tmp_path):
    settings = _settings()
    stats, archive_path = stage.produce_chip(make_provider(), _chip(5),
                                             settings, PROC_DATE, str(tmp_path))
    assert archive_path == str(tmp_path / "alerts_jid5.avro")
    with open(archive_path, "rb") as f:
        alerts = list(fastavro.reader(f))
    assert len(alerts) == stats.n_alerts == len(chip_data.sources)
    assert stats.pid == CHIP_PID and stats.n_failed == 0
    json.dumps(stats.as_dict())                    # summary-file ready


def test_produce_chip_removes_partial_archive_on_failure(make_provider,
                                                        chip_data, tmp_path):
    chip_data.partitions_exist = False            # prefetch aborts the chip
    with pytest.raises(Exception):
        stage.produce_chip(make_provider(), _chip(5), _settings(), PROC_DATE,
                           str(tmp_path))
    assert not (tmp_path / "alerts_jid5.avro").exists()


# ---------------------------------------------------------------------------
# the worker loop: one provider, several chips, failures recorded not raised
# ---------------------------------------------------------------------------

@pytest.fixture()
def fake_make_provider(monkeypatch, chip_data):
    """Route the stage's make_provider() to the fake chip instead of RAPIDDB."""
    made = []

    def _make(diff_flavor="sfft", kona_file=None, refcat=True, ned=True,
              ned_source=None, lvs=True):
        provider = AlertDataProvider(FakeDB(chip_data), diff_flavor=diff_flavor,
                                     kona_lookup=None, refcat=refcat,
                                     ned_reader=None, lvs_reader=None)
        made.append(provider)
        return provider

    monkeypatch.setattr(stage, "make_provider", _make)
    return made


def test_worker_produces_every_assigned_chip(fake_make_provider, chip_data,
                                             tmp_path):
    chips = [_chip(5), _chip(6), _chip(7)]
    # thread 0 of 2 gets chips 5 and 7; one provider serves both
    results = stage.run_single_core_job(chips, 0, 2, _settings(), PROC_DATE,
                                        str(tmp_path))
    assert [r["jid"] for r in results] == [5, 7]
    assert all(r["status"] == "ok" for r in results)
    assert len(fake_make_provider) == 1
    for r in results:
        assert r["stats"]["n_alerts"] == len(chip_data.sources)
        assert (tmp_path / r["archive_filename"]).stat().st_size \
            == r["archive_bytes"]
        summary = json.loads((tmp_path / f"alerts_jid{r['jid']}_summary.json")
                             .read_text())
        assert summary["stats"] == r["stats"]
    # the per-chip statistics land in the thread's log file at INFO
    log_text = (tmp_path / "produceAlertsForProcDate_thread0.out").read_text()
    assert f"pid={CHIP_PID}: {len(chip_data.sources)} alerts archived" in log_text
    assert "Chip end: jid=7" in log_text


def test_worker_with_no_assigned_chips_does_nothing(fake_make_provider,
                                                    tmp_path):
    # 1 chip, 2 workers: thread 1 has nothing and opens no provider
    results = stage.run_single_core_job([_chip(5)], 1, 2, _settings(),
                                        PROC_DATE, str(tmp_path))
    assert results == [] and fake_make_provider == []


def test_worker_records_a_failed_chip_and_continues(fake_make_provider,
                                                    chip_data, tmp_path,
                                                    monkeypatch):
    # a chip-level failure (a CutoutStagingError in real life) on jid 6 only
    real_produce_chip = stage.produce_chip

    def _produce_chip(provider, chip, *args):
        if chip["jid"] == 6:
            raise RuntimeError("boom: could not stage the difference image")
        return real_produce_chip(provider, chip, *args)

    monkeypatch.setattr(stage, "produce_chip", _produce_chip)
    chips = [_chip(5), _chip(6), _chip(7)]
    results = stage.run_single_core_job(chips, 0, 1, _settings(), PROC_DATE,
                                        str(tmp_path))
    by_jid = {r["jid"]: r for r in results}
    assert by_jid[5]["status"] == "ok" and by_jid[7]["status"] == "ok"
    failed = by_jid[6]
    assert failed["status"] == "failed"
    assert failed["error"] == "RuntimeError"          # basic failure record
    assert failed["message"].startswith("boom")
    assert "stats" not in failed
    assert not (tmp_path / "alerts_jid6.avro").exists()
    log_text = (tmp_path / "produceAlertsForProcDate_thread0.out").read_text()
    assert "chip failed: jid=6" in log_text


def test_worker_drops_unassociated_source_but_keeps_the_chip(
        fake_make_provider, chip_data, tmp_path):
    del chip_data.merges[9003]                   # cross-match missed one
    [result] = stage.run_single_core_job([_chip(5)], 0, 1, _settings(),
                                         PROC_DATE, str(tmp_path))
    assert result["status"] == "ok"
    assert result["stats"]["n_alerts"] == 2
    assert result["stats"]["failures"] == [
        {"sid": 9003, "error": "AssociationError",
         "message": result["stats"]["failures"][0]["message"]}]
    assert "sid=9003" in result["stats"]["failures"][0]["message"]


# ---------------------------------------------------------------------------
# the multi-process path (what the launcher machine actually runs). The
# fake provider is installed by monkeypatching the module, which only
# reaches the workers when they are forked -- so this skips under the
# "spawn" start method (macOS default) and runs on Linux.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(multiprocessing.get_start_method() != "fork",
                    reason="fake provider reaches workers only via fork")
def test_parallel_processes_gather_every_worker(fake_make_provider, chip_data,
                                                tmp_path):
    chips = [_chip(5), _chip(6), _chip(7)]
    results = stage.execute_parallel_processes(chips, 2, _settings(),
                                               PROC_DATE, str(tmp_path))
    assert sorted(r["jid"] for r in results) == [5, 6, 7]
    assert all(r["status"] == "ok" for r in results)
    assert (tmp_path / "produceAlertsForProcDate_thread0.out").exists()
    assert (tmp_path / "produceAlertsForProcDate_thread1.out").exists()


# ---------------------------------------------------------------------------
# run summary and exit codes
# ---------------------------------------------------------------------------

def _ok(jid, n_alerts, n_failed=0, nbytes=1000):
    return {"jid": jid, "pid": 500 + jid, "status": "ok",
            "archive_bytes": nbytes,
            "stats": {"n_alerts": n_alerts, "n_failed": n_failed}}


def test_summarize_run_all_ok():
    exitcode, agg = stage.summarize_run([_ok(1, 10), _ok(2, 5, n_failed=1)])
    assert exitcode == stage.EXIT_NORMAL == 0
    assert agg["n_chips"] == 2 and agg["n_chips_failed"] == 0
    assert agg["n_alerts"] == 15 and agg["n_sources_dropped"] == 1
    assert agg["n_archive_bytes"] == 2000 and agg["failed_chips"] == []


def test_summarize_run_failed_chip_is_a_warning_exit():
    failed = {"jid": 2, "pid": 502, "status": "failed",
              "error": "CutoutStagingError", "message": "boom"}
    exitcode, agg = stage.summarize_run([_ok(1, 10), failed])
    assert exitcode == stage.EXIT_CHIPS_FAILED
    assert 0 < exitcode < 64                     # the VPO continues
    assert agg["n_chips_ok"] == 1 and agg["n_alerts"] == 10
    assert agg["failed_chips"] == [{"jid": 2, "pid": 502,
                                    "error": "CutoutStagingError",
                                    "message": "boom"}]
