"""Tests for rapidpipe.stages.reference: declaration, validation exit codes, the manifest.

Inputs are the fixture's frames (rapidpipe.selftest.support.fakereftools.
write_frames) and the tools are the packaged fakes, selected through
``RAPIDPIPE_REFERENCE_TOOLKIT``. The packaged fixture itself runs at the
end (``rapidpipe selftest --stage reference``).
"""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

import pytest
from astropy.io import fits

import rapidpipe.stages.reference as reference
from rapidpipe.db.ids import is_valid_ulid
from rapidpipe.products.manifest import Manifest
from rapidpipe.science.reference.identity import selection_digest
from rapidpipe.selftest import run as run_selftest
from rapidpipe.selftest.runner import STAGE_NAMES
from rapidpipe.selftest.support.fakedifftools import cdf_dir
from rapidpipe.selftest.support.fakereftools import FRAMES, RTID, write_frames
from rapidpipe.stages.contract import ExitCode

RUN = "01J8Y6QZ3M0000000000000RUN"
ATTEMPT = "01J8Y6QZ3M00000000000000A1"
UNIT = f"{RTID}/W146"


@pytest.fixture(autouse=True)
def fakes(monkeypatch, tmp_path):
    monkeypatch.setenv(reference.TOOLKIT_ENV,
                       "rapidpipe.selftest.support.fakereftools:fake_toolkit")
    monkeypatch.setenv("RAPIDPIPE_WORK", str(tmp_path / "work"))


def _overlay(tmp_path: Path, extra: str = "") -> Path:
    """The test overlay; ``extra`` lines under a ``[mosaic]`` header join its table."""
    mosaic = "naxis1 = 128\nnaxis2 = 128\n"
    if extra.startswith("[mosaic]\n"):
        body = extra.removeprefix("[mosaic]\n")
        overrides = {line.split("=")[0].strip() for line in body.splitlines() if "=" in line}
        mosaic = "".join(line + "\n" for line in mosaic.splitlines()
                         if line.split("=")[0].strip() not in overrides) + body
        extra = ""
    path = tmp_path / "overlay.toml"
    path.write_text(f'[paths]\ncfg_path = "{cdf_dir()}"\n[mosaic]\n{mosaic}'
                    f'[statistics]\nclip_correction_seed = 1\n{extra}')
    return path


def _run(tmp_path: Path, *, unit: str = UNIT, extra_settings: str = "",
         edit=None) -> tuple[int, Path]:
    inputs = tmp_path / "inputs"
    if not (inputs / "manifest.json").exists():
        write_frames(inputs)
    if edit is not None:
        path = inputs / "manifest.json"
        manifest = json.loads(path.read_text())
        edit(manifest, inputs)
        path.write_text(json.dumps(manifest))
    outputs = tmp_path / "outputs"
    code = reference.main(["--run", RUN, "--unit", unit, "--attempt", ATTEMPT,
                           "--inputs", str(inputs), "--outputs", str(outputs),
                           "--settings", str(_overlay(tmp_path, extra_settings))])
    return code, outputs


def _refresh_member(manifest: dict, inputs: Path, index: int) -> None:
    member = manifest["outputs"][index]["members"][0]
    raw = (inputs / member["path"]).read_bytes()
    member["bytes"] = len(raw)
    member["sha256"] = "sha256:" + hashlib.sha256(raw).hexdigest()


def _rewrite_frame(inputs: Path, manifest: dict, index: int, **cards) -> None:
    path = inputs / manifest["outputs"][index]["members"][0]["path"]
    with fits.open(gzip.open(path)) as hdul:
        hdul = fits.HDUList([h.copy() for h in hdul])
    for key, value in cards.items():
        if value is None:
            del hdul[1].header[key]
        else:
            hdul[1].header[key] = value
    raw = path.with_suffix("")
    hdul.writeto(raw, overwrite=True)
    path.write_bytes(gzip.compress(raw.read_bytes()))
    raw.unlink()
    _refresh_member(manifest, inputs, index)


# ----------------------------------------------------------------------
# Declaration
# ----------------------------------------------------------------------


def test_declaration():
    d = reference.DECLARATION
    d.validate()
    assert (d.name, d.unit, d.database_access) == ("reference", "field", "none")
    assert d.consumes == ("l2-image",)
    assert d.produces == ("reference-image", "reference-catalog")
    assert d.resource_defaults == {"vcpus": 4, "memory_mib": 32768}
    assert Path(d.settings_schema_path).name == "reference.toml"


def test_selftest_registration():
    assert "reference" in STAGE_NAMES


@pytest.mark.parametrize("unit", ["4711398", "4711398/", "/F146", "abc/F146", "0/F146",
                                  "4711398/F146/x"])
def test_bad_unit_id_is_a_usage_error(tmp_path, unit):
    code, outputs = _run(tmp_path, unit=unit)
    assert code == ExitCode.USAGE
    assert not (outputs / "manifest.json").exists()


def test_parse_unit():
    assert reference.parse_unit("4711398/F146") == (4711398, "W146")
    assert reference.parse_unit("4711398/W146") == (4711398, "W146")
    assert reference.parse_unit("4711398/F184") == (4711398, "F184")


# ----------------------------------------------------------------------
# A good run
# ----------------------------------------------------------------------


def test_full_run_publishes_the_ruled_manifest(tmp_path):
    code, outputs = _run(tmp_path)
    assert code == ExitCode.SUCCESS
    manifest = Manifest.read(outputs / "manifest.json")
    assert manifest.stage == "reference" and manifest.unit.kind == "field"
    assert manifest.unit.id == UNIT
    ref, cat = manifest.outputs
    assert ref.kind == "reference-image" and cat.kind == "reference-catalog"
    assert is_valid_ulid(ref.instance) and is_valid_ulid(cat.instance)

    constituents = [f[0] for f in FRAMES]
    record = json.loads((outputs / manifest.execution_record).read_text())
    assert ref.key == {"field": str(RTID), "filter": "W146", "recipe": "awaicgen",
                       "version": selection_digest(constituents, record["settings_hash"])}
    assert [m.role for m in ref.members] == ["image", "coverage", "uncertainty"]
    assert ref.primary == "ref/awaicgen_output_mosaic_image.fits"
    assert sorted(ref.registration) == sorted(reference.REGISTRATION_FIELDS)
    r = ref.registration
    assert r["constituents"] == constituents and r["nframes"] == 3
    assert r["filter"] == "W146" and r["field"] == RTID
    assert r["npucatsources"] is None and r["status"] == 1 and r["infobits"] == 0
    assert r["md5"] == hashlib.md5((outputs / ref.primary).read_bytes()).hexdigest()
    assert r["settings_hash"] == "sha256:" + record["settings_hash"]
    assert r["jd_start"] == FRAMES[0][4] + 2400000.5
    assert r["jd_end"] == FRAMES[2][4] + 2400000.5

    assert cat.key == {"reference": ref.instance, "catalog_type": "sextractor"}
    assert cat.primary == "ref/awaicgen_output_mosaic_refimsexcat.txt"
    assert sorted(cat.registration) == sorted(reference.CATALOG_REGISTRATION_FIELDS)
    assert cat.registration["source_count"] == r["nsxcatsources"] == 4

    assert manifest.inputs.products == {
        "l2-image/001": constituents[0], "l2-image/002": constituents[1],
        "l2-image/003": constituents[2]}
    assert "notes" not in record
    # Nothing but the four products is published: the scratch inputs are not.
    published = sorted(p.relative_to(outputs).as_posix() for p in outputs.rglob("*")
                       if p.is_file())
    assert published == sorted([
        "manifest.json", f"exec/{ATTEMPT}.json", "ref/awaicgen_output_mosaic_image.fits",
        "ref/awaicgen_output_mosaic_cov_map.fits",
        "ref/awaicgen_output_mosaic_uncert_image.fits",
        "ref/awaicgen_output_mosaic_refimsexcat.txt"])
    assert not any((tmp_path / "work").iterdir())


def test_same_selection_same_version_different_settings_new_version(tmp_path):
    code, a = _run(tmp_path / "a")
    assert code == 0
    code, b = _run(tmp_path / "b")
    assert code == 0
    code, c = _run(tmp_path / "c", extra_settings="[instrument]\nsca_gain = 1.0\n")
    assert code == 0
    ka, kb, kc = (Manifest.read(p / "manifest.json").outputs[0].key for p in (a, b, c))
    assert ka == kb and ka != kc


def test_max_frames_truncates_in_manifest_order_and_notes_the_rest(tmp_path):
    code, outputs = _run(tmp_path, extra_settings="[selection]\nmax_frames = 2\n")
    assert code == ExitCode.SUCCESS
    manifest = Manifest.read(outputs / "manifest.json")
    r = manifest.outputs[0].registration
    assert r["constituents"] == [FRAMES[0][0], FRAMES[1][0]] and r["nframes"] == 2
    assert r["total_exptime"] == 280.0
    record = json.loads((outputs / manifest.execution_record).read_text())
    assert record["notes"] == {"not_coadded": [FRAMES[2][0]]}
    assert set(manifest.inputs.products) == {"l2-image/001", "l2-image/002"}


def test_explicit_mosaic_centre(tmp_path):
    code, outputs = _run(tmp_path, extra_settings="[mosaic]\nra_center = 267.5391\n"
                                                  "dec_center = -29.8279\n")
    assert code == 0
    r = Manifest.read(outputs / "manifest.json").outputs[0].registration
    assert (r["ra_center"], r["dec_center"]) == (267.5391, -29.8279)


# ----------------------------------------------------------------------
# Rejections
# ----------------------------------------------------------------------


def test_too_few_frames_is_rejected(tmp_path):
    def edit(m, inputs):
        m["outputs"] = m["outputs"][:1]
    code, outputs = _run(tmp_path, edit=edit)
    assert code == ExitCode.INPUT_REJECTED
    assert not (outputs / "manifest.json").exists()


def test_a_frame_in_another_filter_is_rejected(tmp_path):
    code, _ = _run(tmp_path, edit=lambda m, i: _rewrite_frame(i, m, 1, FILTER="F184"))
    assert code == ExitCode.INPUT_REJECTED


def test_a_frame_without_filter_is_rejected(tmp_path):
    code, _ = _run(tmp_path, edit=lambda m, i: _rewrite_frame(i, m, 0, FILTER=None))
    assert code == ExitCode.INPUT_REJECTED


def test_a_frame_without_exptime_is_rejected(tmp_path):
    code, _ = _run(tmp_path, edit=lambda m, i: _rewrite_frame(i, m, 2, EXPTIME=None))
    assert code == ExitCode.INPUT_REJECTED


def test_the_unit_filter_may_use_either_spelling_and_is_normalised(tmp_path):
    code, outputs = _run(tmp_path, unit=f"{RTID}/F146")
    assert code == ExitCode.SUCCESS
    entry = Manifest.read(outputs / "manifest.json").outputs[0]
    assert entry.key["filter"] == "W146" and entry.registration["filter"] == "W146"


def test_a_frame_whose_filter_uses_the_roman_spelling_is_accepted(tmp_path):
    code, _ = _run(tmp_path, edit=lambda m, i: _rewrite_frame(i, m, 1, FILTER="F146"))
    assert code == ExitCode.SUCCESS


def test_an_empty_reference_catalog_is_rejected(tmp_path, monkeypatch):
    from rapidpipe.selftest.support import fakedifftools

    def empty_catalog(self, args, cwd):
        names = fakedifftools._params(args[args.index("-PARAMETERS_NAME") + 1])
        (cwd / args[args.index("-CATALOG_NAME") + 1]).write_text(
            "".join(f"#{i + 1:4d} {p}\n" for i, p in enumerate(names)))
    monkeypatch.setattr(fakedifftools.FakeToolRunner, "_sextractor", empty_catalog)
    code, outputs = _run(tmp_path)
    assert code == ExitCode.INPUT_REJECTED
    assert not (outputs / "manifest.json").exists()


def test_the_bundle_keeps_awaicgens_wcs(tmp_path):
    code, outputs = _run(tmp_path)
    assert code == 0
    entry = Manifest.read(outputs / "manifest.json").outputs[0]
    for member in entry.members:
        with fits.open(outputs / member.path) as hdul:
            assert len(hdul) == 1
            hdr = hdul[0].header
        assert (hdr["CTYPE1"], hdr["CTYPE2"]) == ("RA---TAN", "DEC--TAN")
        assert not any(k.startswith(("PV1_", "PV2_", "A_", "B_")) for k in hdr)
        assert (hdr["CRVAL1"], hdr["CRVAL2"]) == (entry.registration["ra_center"],
                                                  entry.registration["dec_center"])


def test_a_corrupt_member_is_rejected(tmp_path):
    def edit(m, inputs):
        m["outputs"][0]["members"][0]["sha256"] = "sha256:" + "0" * 64
    code, _ = _run(tmp_path, edit=edit)
    assert code == ExitCode.INPUT_REJECTED


def test_a_missing_member_is_rejected(tmp_path):
    def edit(m, inputs):
        (inputs / m["outputs"][1]["members"][0]["path"]).unlink()
    code, _ = _run(tmp_path, edit=edit)
    assert code == ExitCode.INPUT_REJECTED


def test_another_kind_in_the_input_set_is_rejected(tmp_path):
    def edit(m, inputs):
        extra = json.loads(json.dumps(m["outputs"][0]))
        extra["kind"] = "psf"
        extra["instance"] = "01K6AREF00000000000000P5F0"
        m["outputs"].append(extra)
    code, _ = _run(tmp_path, edit=edit)
    assert code == ExitCode.INPUT_REJECTED


def test_a_duplicated_instance_is_rejected(tmp_path):
    def edit(m, inputs):
        m["outputs"][1]["instance"] = m["outputs"][0]["instance"]
    code, _ = _run(tmp_path, edit=edit)
    assert code == ExitCode.INPUT_REJECTED


def test_the_input_set_must_be_a_field_unit(tmp_path):
    def edit(m, inputs):
        m["unit"] = {"kind": "detector-image", "id": "x/SCA01"}
    code, _ = _run(tmp_path, edit=edit)
    assert code == ExitCode.INPUT_REJECTED


def test_an_image_role_that_is_not_primary_is_rejected(tmp_path):
    def edit(m, inputs):
        m["outputs"][0]["primary"] = "l2/elsewhere.fits.gz"
    code, _ = _run(tmp_path, edit=edit)
    assert code == ExitCode.INPUT_REJECTED


@pytest.mark.parametrize("extra", [
    "[psfcat]\nenabled = true\n",
    "[fake_sources]\ninject_fake_sources_flag = true\n",
    "[selection]\nmin_frames = 0\n",
    "[selection]\nmin_frames = 5\nmax_frames = 4\n",
    "[mosaic]\nra_center = 10.0\n",
    "[mosaic]\ncdelt1 = -0.0001\n",
    "[mosaic]\nnaxis1 = -3\n",
    "[awaicgen]\nawaicgen_output_mosaic_image_file = \"mosaic.fits\"\n",
    "[selection]\nunknown_key = 1\n",
])
def test_bad_settings_are_usage_errors(tmp_path, extra):
    code, outputs = _run(tmp_path, extra_settings=extra)
    assert code == ExitCode.USAGE
    assert not (outputs / "manifest.json").exists()


def test_awaicgen_leaving_no_mosaic_is_a_stage_error(tmp_path, monkeypatch):
    from rapidpipe.selftest.support import fakereftools

    monkeypatch.setattr(fakereftools.FakeReferenceToolRunner, "_awaicgen",
                        lambda self, args, cwd: None)
    code, outputs = _run(tmp_path)
    assert code == ExitCode.STAGE_ERROR
    assert not (outputs / "manifest.json").exists()


def test_dry_run_writes_nothing(tmp_path):
    inputs = tmp_path / "inputs"
    write_frames(inputs)
    outputs = tmp_path / "outputs"
    code = reference.main(["--run", RUN, "--unit", UNIT, "--attempt", ATTEMPT,
                           "--inputs", str(inputs), "--outputs", str(outputs),
                           "--settings", str(_overlay(tmp_path)), "--dry-run"])
    assert code == ExitCode.SUCCESS
    assert not outputs.exists()


# ----------------------------------------------------------------------
# The packaged fixture
# ----------------------------------------------------------------------


def test_packaged_fixture_passes_with_fake_tools(tmp_path, monkeypatch):
    monkeypatch.delenv(reference.TOOLKIT_ENV)
    assert run_selftest(stage="reference", real_tools=False, work_dir=str(tmp_path / "fx"),
                        output_location=None) == 0


def test_packaged_fixture_is_small():
    from rapidpipe.selftest.runner import fixture_dir

    total = sum(p.stat().st_size for p in fixture_dir("reference").rglob("*") if p.is_file())
    assert total < 1_000_000
