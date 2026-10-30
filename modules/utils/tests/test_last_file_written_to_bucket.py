"""Tests of get_datetime_of_last_file_written_to_bucket with a faked `aws s3 ls` listing (no S3 needed)."""

import sys
import types

try:
    import psycopg2  # noqa: F401
except ImportError:
    stub = types.ModuleType("psycopg2")
    stub.DatabaseError = Exception
    stub.ProgrammingError = Exception
    sys.modules["psycopg2"] = stub

import modules.utils.rapid_pipeline_subs as util


def _fake_listing(monkeypatch, exitcode, listing):
    monkeypatch.setattr(util, "execute_command_and_return_stdout", lambda cmd: (exitcode, listing))


def test_empty_bucket_path_returns_none(monkeypatch):

    # A discarded reference image leaves nothing under the job's prefix; `aws s3 ls` then exits 1 with no output.

    _fake_listing(monkeypatch, 1, "")

    assert util.get_datetime_of_last_file_written_to_bucket("s3://bucket/20261030/jid1/") == (None, None)


def test_last_file_is_latest_listed(monkeypatch):
    listing = ("2026-10-30 10:00:00       100 20261030/jid1/a.fits\n"
               "2026-10-30 11:30:00       100 20261030/jid1/b.fits\n"
               "2026-10-30 09:00:00       100 20261030/jid1/c.fits\n")
    _fake_listing(monkeypatch, 0, listing)

    ended_dt, last_file = util.get_datetime_of_last_file_written_to_bucket("s3://bucket/20261030/jid1/")

    assert last_file == "20261030/jid1/b.fits"
    assert ended_dt is not None
