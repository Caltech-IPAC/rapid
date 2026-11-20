"""``scripts/copy_pixel_area_maps.py``, with the CRDS query and the download replaced.

Contracts: each detector's best ``area`` reference is copied under the
store by its CRDS name and listed in ``index.json`` with its detector and
the file's own useafter date; a rerun copies nothing; a new delivery adds
its entry and keeps the older one; a file that is another detector's is
refused; an ``s3://`` store holds the same objects; and CRDS not answering
stops the script.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import asdf
import pytest

from rapidpipe.science import crds
from tests.unit.fakes3 import FakeS3

_SPEC = importlib.util.spec_from_file_location(
    "copy_pixel_area_maps", Path(__file__).resolve().parents[2] / "scripts/copy_pixel_area_maps.py")
copy = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(copy)

#: What CRDS recommends per detector, and each map's (detector, useafter) as the file states them.
BEST = {"WFI01": "roman_wfi_area_0088.asdf", "WFI16": "roman_wfi_area_0085.asdf"}
FILES = {"roman_wfi_area_0088.asdf": ("WFI01", "2026-10-13T02:22:00.000"),
         "roman_wfi_area_0085.asdf": ("WFI16", "2026-10-13T02:22:00.000"),
         "roman_wfi_area_0120.asdf": ("WFI16", "2027-03-01T00:00:00.000")}
ARGS = ["--detectors", "1", "16", "--date", "2027-07-01T00:00:00"]


@pytest.fixture()
def served(monkeypatch):
    """Replace the CRDS query (answers from ``best``) and the download; returns (best, fetched names)."""
    best, fetched = dict(BEST), []

    def best_reference(server, context, detector, date, reftype):
        assert (context, date, reftype) == ("roman-operational", "2027-07-01T00:00:00", "area")
        return best[detector]

    def fetch(server, name, directory):
        fetched.append(name)
        detector, useafter = FILES[name]
        path = directory / name
        asdf.AsdfFile({"roman": {"meta": {"instrument": {"detector": detector}, "useafter": useafter}}}
                      ).write_to(path)
        return path

    monkeypatch.setattr(copy.crds, "best_reference", best_reference)
    monkeypatch.setattr(copy, "fetch", fetch)
    return best, fetched


def index(store: Path) -> list[dict]:
    return json.loads((store / "index.json").read_text())["maps"]


def test_each_detectors_map_is_copied_by_name_and_indexed_with_its_useafter(tmp_path, served, capsys):
    assert copy.main(["--to", str(tmp_path / "store"), *ARGS]) == 0
    assert sorted(p.name for p in (tmp_path / "store").iterdir()) == [
        "index.json", "roman_wfi_area_0085.asdf", "roman_wfi_area_0088.asdf"]
    assert index(tmp_path / "store") == [
        {"detector": "WFI01", "useafter": "2026-10-13T02:22:00", "name": "roman_wfi_area_0088.asdf"},
        {"detector": "WFI16", "useafter": "2026-10-13T02:22:00", "name": "roman_wfi_area_0085.asdf"}]
    lines = capsys.readouterr().out.splitlines()
    assert lines[:2] == ["WFI01 roman_wfi_area_0088.asdf copied 2026-10-13T02:22:00",
                         "WFI16 roman_wfi_area_0085.asdf copied 2026-10-13T02:22:00"]
    assert lines[2].startswith("2 detectors, 2 maps copied, 2 in ")


def test_a_rerun_copies_nothing(tmp_path, served, capsys):
    _, fetched = served
    copy.main(["--to", (tmp_path / "store").as_uri(), *ARGS])
    before = index(tmp_path / "store")
    del fetched[:]
    capsys.readouterr()
    assert copy.main(["--to", (tmp_path / "store").as_uri(), *ARGS]) == 0
    assert fetched == [] and index(tmp_path / "store") == before
    assert capsys.readouterr().out.splitlines()[1] == "WFI16 roman_wfi_area_0085.asdf present 2026-10-13T02:22:00"


def test_a_new_delivery_adds_its_entry_and_keeps_the_older_one(tmp_path, served):
    best, fetched = served
    copy.main(["--to", str(tmp_path / "store"), *ARGS])
    best["WFI16"] = "roman_wfi_area_0120.asdf"
    del fetched[:]
    copy.main(["--to", str(tmp_path / "store"), *ARGS])
    assert fetched == ["roman_wfi_area_0120.asdf"]
    assert [(m["useafter"], m["name"]) for m in index(tmp_path / "store") if m["detector"] == "WFI16"] == [
        ("2026-10-13T02:22:00", "roman_wfi_area_0085.asdf"), ("2027-03-01T00:00:00", "roman_wfi_area_0120.asdf")]
    assert (tmp_path / "store" / "roman_wfi_area_0085.asdf").is_file()


def test_a_map_that_is_another_detectors_is_refused_and_no_index_is_written(tmp_path, served):
    best, _ = served
    best["WFI01"] = "roman_wfi_area_0085.asdf"      # WFI16's file
    with pytest.raises(ValueError, match="WFI16's map, not WFI01's"):
        copy.main(["--to", str(tmp_path / "store"), *ARGS])
    assert not (tmp_path / "store" / "index.json").exists()


def test_an_s3_store_holds_the_maps_and_the_index_under_the_prefix(served, monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(copy.storage, "s3_client", lambda: fake)
    assert copy.main(["--to", "s3://refs/pixel-area-maps/", *ARGS]) == 0
    assert copy.main(["--to", "s3://refs/pixel-area-maps/", *ARGS]) == 0       # reads the index back
    uploads = [key for op, key in fake.calls if op == "upload_file"]
    assert uploads == ["pixel-area-maps/roman_wfi_area_0088.asdf", "pixel-area-maps/roman_wfi_area_0085.asdf",
                       "pixel-area-maps/index.json", "pixel-area-maps/index.json"]
    maps = json.loads(fake._objects[("refs", "pixel-area-maps/index.json")])["maps"]
    assert [m["name"] for m in maps] == ["roman_wfi_area_0088.asdf", "roman_wfi_area_0085.asdf"]


def test_crds_not_answering_stops_the_script(tmp_path, monkeypatch):
    def best_reference(*args):
        raise crds.CRDSError("no answer")

    monkeypatch.setattr(copy.crds, "best_reference", best_reference)
    with pytest.raises(crds.CRDSError):
        copy.main(["--to", str(tmp_path / "store"), "--detectors", "1"])
