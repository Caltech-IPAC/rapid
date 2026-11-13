"""scripts/stream_l2_files.py: a program's L2 files from MAST to a source root, with MAST, HTTP and S3 faked.

Contracts: the search asks for the program's WFI L2 files, every imaging
element unless a filter is named, with each pass, segment, visit and MJD
bound given; an exposure is kept only when three or more of its detectors
name a real guide star, unless ``--unguided``; ``--detector`` keeps one
SCA; a file already under the source root's prefix is skipped, one under
another prefix is not, and ``--force`` copies every file; ``--dryrun``
copies nothing; each file goes to ``<prefix><name>``, which
``loop.list_stems`` finds on the same root; a failed copy is reported and
exits 1; a source root without ``s3://``, a bucket or the trailing ``/``,
and a missing ``MAST_API_TOKEN``, are refused before MAST is asked.
"""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path

import pytest
from astropy.table import Table

from rapidpipe.foundation import loop
from rapidpipe.products import storage

_SPEC = importlib.util.spec_from_file_location(
    "stream_l2_files", Path(__file__).resolve().parents[2] / "scripts" / "stream_l2_files.py")
stream = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(stream)

ROOT = "s3://bucket/flight/l2/"


def stem(visit: int, exposure: int, sca: int = 2) -> str:
    return f"r0104901002001002{visit:03d}_{exposure:04d}_wfi{sca:02d}_f158"


def rows(exposure: int, guide_stars: list[str], visit: int = 1) -> list[tuple]:
    """One exposure's rows, one per detector, each naming its guide star."""
    return [(stem(visit, exposure, sca), f"WFI{sca:02d}", f"2026-10-24T10:{exposure:02d}:00", gs)
            for sca, gs in enumerate(guide_stars, start=1)]


GUIDED = rows(1, ["G1", "G2", "G3", "PGS4"])
UNGUIDED = rows(2, ["G1", "G2", "PGS3", "PGS4"])


class FakeMissions:
    """Stands in for ``MastMissions``: records the login and the search, answers with ``RESULT``."""

    RESULT: list[tuple] = []
    instances: list["FakeMissions"] = []

    def __init__(self, mission):
        self.mission, self.token, self.criteria = mission, None, None
        FakeMissions.instances.append(self)

    def login(self, token):
        self.token = token

    def query_criteria(self, **criteria):
        self.criteria = criteria
        return Table(rows=self.RESULT, names=("fileSetName", "detector", "exposure_start_time", "guide_star_id"))


class FakeS3:
    """A bucket of keys: the paginator, ``upload_fileobj``, and ``list_objects_v2`` with ``StartAfter`` and
    ``MaxKeys`` as ``loop.list_stems`` calls it."""

    def __init__(self, keys=()):
        self.objects = {("bucket", k): b"" for k in keys}
        self.uploaded = []

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return self

    def paginate(self, Bucket, Prefix):
        return [{"Contents": [{"Key": k} for b, k in sorted(self.objects) if b == Bucket and k.startswith(Prefix)]}]

    def list_objects_v2(self, Bucket, Prefix, StartAfter, MaxKeys):
        found = [k for b, k in sorted(self.objects) if b == Bucket and k.startswith(Prefix) and k > StartAfter]
        return {"Contents": [{"Key": k} for k in found[:MaxKeys]]} if found else {}

    def upload_fileobj(self, fileobj, bucket, key):
        self.objects[(bucket, key)] = fileobj.read()
        self.uploaded.append(key)


class Reply:
    def __init__(self, url, status=200):
        self.url, self.status = url, status
        self.headers = {"Location": "https://presigned.example/" + url.rsplit("=", 1)[-1]}
        self.raw = io.BytesIO(url.encode())

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def fakes(monkeypatch):
    """Fake MAST, HTTP and S3; returns the S3 client and the list of requested URLs."""
    FakeMissions.RESULT, FakeMissions.instances = GUIDED + UNGUIDED, []
    monkeypatch.setattr(stream, "MastMissions", FakeMissions)
    monkeypatch.setenv("MAST_API_TOKEN", "test-token")
    client, urls = FakeS3(), []

    def get(url, **kw):
        urls.append(url)
        return Reply(url)

    monkeypatch.setattr(stream.requests, "get", get)
    monkeypatch.setattr(storage, "s3_client", lambda unsigned=False: client)
    return client, urls


def test_search_asks_for_the_programs_wfi_imaging_l2_files(fakes):
    assert stream.main(["1049", "--source-root", ROOT, "--dryrun"]) == 0
    missions = FakeMissions.instances[0]
    assert missions.mission == "roman" and missions.token == "test-token"
    criteria = missions.criteria
    assert criteria["program"] == 1049 and criteria["instrument_name"] == "WFI" and criteria["productLevel"] == 2
    assert criteria["optical_element"] == ["!GRISM", "!PRISM"]
    assert "fileSetName" in criteria["select_cols"] and "guide_star_id" in criteria["select_cols"]
    assert not {"pass", "segment", "visit", "exposure_start_time", "exposure_end_time"} & set(criteria)


def test_search_carries_each_cut_given(fakes):
    stream.main(["1049", "--source-root", ROOT, "--dryrun", "--filter", "F158", "--pass", "2", "--segment", "1",
                 "--visit", "3", "--mjdmin", "61337.0", "--mjdmax", "61338.0"])
    criteria = FakeMissions.instances[0].criteria
    assert criteria["optical_element"] == "F158"
    assert (criteria["pass"], criteria["segment"], criteria["visit"]) == (2, 1, 3)
    assert criteria["exposure_start_time"] == ">2026-10-24T00:00:00.000"
    assert criteria["exposure_end_time"] == "<2026-10-25T00:00:00.000"


def test_only_guided_exposures_are_copied_unless_unguided(fakes):
    client, _ = fakes
    assert stream.main(["1049", "--source-root", ROOT]) == 0
    assert sorted(client.uploaded) == sorted(f"flight/l2/{r[0]}_cal.asdf" for r in GUIDED)
    client.uploaded.clear()
    assert stream.main(["1049", "--source-root", ROOT, "--unguided", "--force"]) == 0
    assert sorted(client.uploaded) == sorted(f"flight/l2/{r[0]}_cal.asdf" for r in GUIDED + UNGUIDED)


def test_detector_keeps_one_sca(fakes):
    client, urls = fakes
    stream.main(["1049", "--source-root", ROOT, "--detector", "2"])
    assert client.uploaded == [f"flight/l2/{stem(1, 1, 2)}_cal.asdf"]
    assert urls[0].endswith(f"/search/roman/api/v0.1/retrieve_product?product_name={stem(1, 1, 2)}_cal.asdf")


def test_a_file_under_the_prefix_is_skipped_one_elsewhere_is_not_and_force_copies_all(fakes, capsys):
    client, _ = fakes
    first, second = (f"{r[0]}_cal.asdf" for r in GUIDED[:2])
    client.objects.update({("bucket", "flight/l2/" + first): b"held", ("bucket", "other/" + second): b"held"})
    stream.main(["1049", "--source-root", ROOT])
    assert "flight/l2/" + first not in client.uploaded and "flight/l2/" + second in client.uploaded
    assert len(client.uploaded) == len(GUIDED) - 1
    assert f"{len(GUIDED)} identified\n{len(GUIDED) - 1} to download" in capsys.readouterr().out
    client.uploaded.clear()
    stream.main(["1049", "--source-root", ROOT, "--force"])
    assert len(client.uploaded) == len(GUIDED)


def test_dryrun_copies_nothing_and_verbose_names_the_files(fakes, capsys):
    client, urls = fakes
    assert stream.main(["1049", "--source-root", ROOT, "--dryrun", "--verbose"]) == 0
    assert client.uploaded == [] and urls == []
    out = capsys.readouterr().out
    assert all(f"{r[0]}_cal.asdf" in out for r in GUIDED)


def test_a_copied_key_is_listed_by_the_loop_on_the_same_root(fakes):
    client, _ = fakes
    stream.main(["1049", "--source-root", ROOT, "--detector", "2"])
    assert loop.list_stems(client, ROOT, loop.Selection.parse("F158", "2", "all")) == [stem(1, 1, 2)]


def test_a_failed_copy_is_reported_and_exits_1(fakes, monkeypatch, capsys):
    client, _ = fakes
    monkeypatch.setattr(stream.requests, "get", lambda url, **kw: Reply(url, status=403))
    assert stream.main(["1049", "--source-root", ROOT, "--detector", "2"]) == 1
    assert client.uploaded == []
    assert f"FAILED {stem(1, 1, 2)}_cal.asdf: HTTP 403" in capsys.readouterr().out


@pytest.mark.parametrize("root", ["s3://bucket/flight/l2", "bucket/flight/l2/", "s3:///l2/"])
def test_a_malformed_source_root_is_refused_before_mast(fakes, root):
    with pytest.raises(SystemExit) as stop:
        stream.main(["1049", "--source-root", root])
    assert stop.value.code == 2 and FakeMissions.instances == []


def test_source_root_is_required_and_the_token_too(fakes, monkeypatch):
    with pytest.raises(SystemExit):
        stream.main(["1049"])
    monkeypatch.delenv("MAST_API_TOKEN")
    with pytest.raises(SystemExit) as stop:
        stream.main(["1049", "--source-root", ROOT])
    assert stop.value.code == 2 and FakeMissions.instances == []
