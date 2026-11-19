"""scripts/compute_wfi_zeropoints.py: per-SCA WFI zero points, with CRDS, GitHub and downloads faked.

Contracts: a square band's count rate, C, K, pivot wavelength and AB zero
point match their closed forms, and the zero point falls by 2.5 log10(2) when
the gain doubles; the Roman Technical Information Repository's tables, whose
free-text header line is not YAML, are read as CSV; columns with units are
converted to um and m^2, a unitless table in cm^2 and a missing filter column
are refused; each SCA's table is found by its whole token (``SCA01``, never
``SCA10``), and a missing or duplicate table is refused; an SCA's gain is the
median of its gain reference's science pixels divided by the IPC factor, its
pixel area the area reference's ``pixelarea_steradians``; the data-folder
check reports a table changed, missing or not on GitHub, a newer table GitHub
has, an unreachable GitHub, a recommended reference missing or of the wrong
size, and local references recommended for no SCA; a reference of the wrong
size is downloaded again, one of the right size never; the script asks
before continuing past any problem, stops on no, on end of input and with no
terminal, and continues on yes or ``--yes``; an up-to-date folder runs with
no question.
"""

from __future__ import annotations

import builtins
import importlib.util
import json
import math
import os
import shutil
from pathlib import Path

import asdf
import numpy as np
import pytest

_SPEC = importlib.util.spec_from_file_location(
    "compute_wfi_zeropoints", Path(__file__).resolve().parents[2] / "scripts" / "compute_wfi_zeropoints.py")
zp = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(zp)

DATE = "2026-10-29T00:00:00"
WAVE = np.round(np.arange(0.4, 2.6, 0.001), 3)


def square_band(lo: float, hi: float, area: float) -> np.ndarray:
    """An effective area of ``area`` m^2 between ``lo`` and ``hi`` um, zero elsewhere."""
    return np.where((WAVE >= lo) & (WAVE <= hi), area, 0.0)


def write_stsci_table(path: Path, areas: dict[str, np.ndarray]) -> Path:
    """A table laid out as the Roman Technical Information Repository's, header line included."""
    names = ["Wave", *areas]
    lines = ["# %ECSV 1.0", "# ---", "# Effective area for Roman (m^2) - parameters as of 2024 03 01", "# datatype:"]
    lines += [f"# - {{name: {n}, datatype: float32}}" for n in names]
    lines += ["# schema: astropy-2.0", ", ".join(names)]
    lines += [", ".join([f"{w:.3f}", *(f"{areas[f][i]:.4f}" for f in areas)]) for i, w in enumerate(WAVE)]
    path.write_text("\n".join(lines) + "\n")
    return path


def all_filters(scale: float = 1.0) -> dict[str, np.ndarray]:
    return {f: square_band(1.0 + 0.1 * i, 1.3 + 0.1 * i, 2.0 * scale) for i, f in enumerate(zp.FILTERS)}


def write_gain(path: Path, inner: float = 2.2) -> None:
    """A 16 x 16 gain reference: ``inner`` in the science pixels, 100 in the reference border, one NaN and one 0."""
    data = np.full((16, 16), 100.0, dtype="f4")
    data[zp.REFPIX:-zp.REFPIX, zp.REFPIX:-zp.REFPIX] = inner
    data[6, 6], data[7, 7] = np.nan, 0.0
    asdf.AsdfFile({"roman": {"meta": {"reftype": "GAIN"}, "data": data}}).write_to(path)


def write_area(path: Path, omega: float = 2.8e-13) -> None:
    asdf.AsdfFile({"roman": {"meta": {"photometry": {"pixelarea_steradians": omega}},
                             "data": np.ones((8, 8), dtype="f4")}}).write_to(path)


def write_photom(path: Path, photmjsr: dict[str, float]) -> None:
    table = {f: {"photmjsr": v} for f, v in photmjsr.items()}
    table["DARK"] = {"photmjsr": None}
    asdf.AsdfFile({"roman": {"meta": {"reftype": "PHOTOM"}, "phot_table": table}}).write_to(path)


def ref_names(sca: str) -> dict[str, str]:
    n = int(sca[3:])
    return {"gain": f"roman_wfi_gain_{n:04d}.asdf", "area": f"roman_wfi_area_{n:04d}.asdf",
            "photom": f"roman_wfi_photom_{n:04d}.asdf"}


# --- the science ---

def test_a_square_band_s_conversions_match_their_closed_forms():
    area, gain, omega = 2.0, 1.8, 2.8e-13
    m = zp.compute_filter_metrics(WAVE, square_band(1.0, 1.3, area), gain, omega)
    # The trapezoid rule ramps each sampled edge over one grid step, so the
    # band integrates as one half a step wider at each end
    lo, hi = 1.0 - 0.0005, 1.3 + 0.0005
    int_over_lam = area * math.log(hi / lo)
    c = 1.0 / (1e-26 / (zp.H_PLANCK * gain) * int_over_lam)
    assert m["C"] == pytest.approx(c, rel=1e-5)
    assert m["K"] == pytest.approx(c / (1e6 * omega), rel=1e-5)
    assert m["ZP_AB"] == pytest.approx(-2.5 * math.log10(c) + 8.90, abs=1e-5)
    assert m["lambda_piv"] == pytest.approx(math.sqrt((hi**2 - lo**2) / 2 / math.log(hi / lo)), rel=1e-5)
    assert m["lambda_0"] == pytest.approx((lo + hi) / 2, rel=1e-5)
    assert m["ew"] == pytest.approx(hi - lo, rel=1e-5)
    assert m["a_peak"] == area


def test_doubling_the_gain_lowers_the_zero_point_by_2_5_log10_2():
    band = square_band(1.0, 1.3, 2.0)
    one = zp.compute_filter_metrics(WAVE, band, 1.0, 2.8e-13)["ZP_AB"]
    two = zp.compute_filter_metrics(WAVE, band, 2.0, 2.8e-13)["ZP_AB"]
    assert one - two == pytest.approx(2.5 * math.log10(2), abs=1e-9)


# --- the tables ---

def test_the_repository_s_table_with_its_non_yaml_header_is_read(tmp_path):
    path = write_stsci_table(tmp_path / "Roman_effarea_v8_SCA01_20240301.ecsv", all_filters())
    wave, area = zp.load_effective_area(path, "F158")
    assert wave[0] == pytest.approx(0.4) and np.all(np.diff(wave) > 0)
    assert area.max() == pytest.approx(2.0)


def test_columns_with_units_are_converted_and_nan_dropped(tmp_path):
    from astropy.table import Table
    import astropy.units as u
    band = square_band(1.0, 1.3, 2.0)
    band[5] = np.nan
    t = Table({"Wavelength": WAVE[::-1] * 1000 * u.nm, "F158": band[::-1] * 1e4 * u.cm**2})
    t.write(tmp_path / "eff_SCA01.ecsv", format="ascii.ecsv")
    wave, area = zp.load_effective_area(tmp_path / "eff_SCA01.ecsv", "F158")
    assert np.all(np.diff(wave) > 0) and wave.size == WAVE.size - 1
    assert area.max() == pytest.approx(2.0)


def test_a_unitless_table_in_cm2_and_a_missing_filter_are_refused(tmp_path):
    path = write_stsci_table(tmp_path / "eff_SCA01.ecsv", {"F158": square_band(1.0, 1.3, 2.0e4)})
    with pytest.raises(ValueError, match="not in m\\^2"):
        zp.load_effective_area(path, "F158")
    with pytest.raises(ValueError, match="no F213 column"):
        zp.load_effective_area(path, "F213")


def test_each_sca_s_table_is_found_by_its_whole_token(tmp_path):
    for sca in ("SCA01", "SCA10"):
        write_stsci_table(tmp_path / f"Roman_effarea_v8_{sca}_20240301.ecsv", all_filters())
    assert Path(zp.find_effarea_file(tmp_path, "SCA01")).name == "Roman_effarea_v8_SCA01_20240301.ecsv"
    with pytest.raises(FileNotFoundError, match="SCA02"):
        zp.find_effarea_file(tmp_path, "SCA02")
    shutil.copy(tmp_path / "Roman_effarea_v8_SCA01_20240301.ecsv", tmp_path / "Roman_effarea_v9_SCA01_x.ecsv")
    with pytest.raises(FileNotFoundError, match="expected one"):
        zp.find_effarea_file(tmp_path, "SCA01")


# --- the CRDS references ---

def test_an_sca_s_gain_and_pixel_area_come_from_its_references(tmp_path):
    names = ref_names("SCA01")
    write_gain(tmp_path / names["gain"], inner=2.2)
    write_area(tmp_path / names["area"], omega=2.81e-13)
    write_photom(tmp_path / names["photom"], {"F158": 0.73})
    cal = zp.sca_calibration(names, "https://crds.invalid", str(tmp_path), ipc_factor=1.1)
    assert cal["gain"] == pytest.approx(2.0)
    assert cal["omega_pix"] == 2.81e-13
    assert cal["photmjsr"] == {"F158": pytest.approx(0.73)}


def test_a_reference_of_the_wrong_size_is_downloaded_again_and_one_of_the_right_size_never(tmp_path, monkeypatch):
    fetched = []

    def urlretrieve(url, path):
        fetched.append(url)
        Path(path).write_bytes(b"x" * 10)

    monkeypatch.setattr(zp.urllib.request, "urlretrieve", urlretrieve)
    (tmp_path / "roman_wfi_photom_0001.asdf").write_bytes(b"x" * 3)
    zp.fetch_reference("https://crds.invalid", "roman_wfi_photom_0001.asdf", str(tmp_path), size=10)
    zp.fetch_reference("https://crds.invalid", "roman_wfi_photom_0001.asdf", str(tmp_path), size=10)
    assert fetched == ["https://crds.invalid/unchecked_get/references/roman/roman_wfi_photom_0001.asdf"]
    assert (tmp_path / "roman_wfi_photom_0001.asdf").stat().st_size == 10


def fake_crds(cache: Path, sizes: dict[str, int] | None = None):
    """Stands in for ``crds_call``: each SCA's references by number, sizes as the cache holds them unless given."""
    sizes = sizes or {}

    def size(name: str) -> int:
        if name in sizes:
            return sizes[name]
        return (cache / name).stat().st_size if (cache / name).exists() else 1000

    def call(server, method, params):
        if method == "get_default_context":
            return "roman_9999.pmap"
        if method == "get_best_references":
            return ref_names("SCA" + params[1]["ROMAN.META.INSTRUMENT.DETECTOR"][3:])
        if method == "get_file_info_map":
            return {n: {"size": str(size(n))} for n in params[1]}
        raise AssertionError(method)
    return call


def test_the_reference_check_reports_missing_wrong_size_and_unused_files(tmp_path, monkeypatch):
    names = ref_names("SCA01")
    (tmp_path / names["area"]).write_bytes(b"a" * 5)
    (tmp_path / names["photom"]).write_bytes(b"p" * 5)
    (tmp_path / "roman_wfi_gain_0099.asdf").write_bytes(b"old")
    monkeypatch.setattr(zp, "crds_call", fake_crds(tmp_path, {names["photom"]: 7}))
    problems, unused, sizes = zp.check_references(str(tmp_path), {"SCA01": names}, "https://crds.invalid",
                                                  all_names=set(names.values()))
    assert len(problems) == 2
    assert problems[0].startswith(f"1 recommended CRDS reference is not in {tmp_path}") and names["gain"] in problems[0]
    assert problems[1].startswith(f"{names['photom']}: local size 5 bytes, CRDS's 7")
    assert unused == ["roman_wfi_gain_0099.asdf"]
    assert sizes[names["area"]] == 5


# --- the effective-area check against GitHub ---

def test_git_blob_sha_is_git_s(tmp_path):
    path = tmp_path / "f.txt"
    path.write_bytes(b"hello\n")
    assert zp.git_blob_sha(path) == "ce013625030ba8dba906f756967f9e9ca394464a"  # git hash-object's


@pytest.fixture
def tables(tmp_path):
    """SCA01 to SCA03's tables, and a GitHub listing that matches them."""
    local = tmp_path / "local"
    local.mkdir()
    listing = []
    for sca in ("SCA01", "SCA02", "SCA03"):
        path = write_stsci_table(local / f"Roman_effarea_v8_{sca}_20240301.ecsv", all_filters())
        listing.append({"name": path.name, "sha": zp.git_blob_sha(path)})
    listing.append({"name": "README.md", "sha": "0" * 40})
    return local, listing, tmp_path / "listing.json"


def check(tables, listing, scas=("SCA01", "SCA02", "SCA03")):
    local, _, url = tables
    url.write_text(json.dumps(listing))
    return zp.check_effarea(str(local), list(scas), url.as_uri())


def test_tables_that_match_github_s_pass(tables):
    assert check(tables, tables[1]) == []


def test_a_changed_missing_or_unlisted_table_and_a_newer_one_are_reported(tables):
    local, listing, _ = tables
    write_stsci_table(local / "Roman_effarea_v8_SCA01_20240301.ecsv", all_filters(scale=1.01))
    (local / "Roman_effarea_v8_SCA02_20240301.ecsv").unlink()
    listing = [x for x in listing if "SCA03" not in x["name"]]
    listing.append({"name": "Roman_effarea_v9_SCA03_20261101.ecsv", "sha": "1" * 40})
    assert check(tables, listing) == [
        "SCA01: Roman_effarea_v8_SCA01_20240301.ecsv differs from GitHub's copy",
        "SCA02: no local table; GitHub has Roman_effarea_v8_SCA02_20240301.ecsv",
        "SCA03: Roman_effarea_v8_SCA03_20240301.ecsv is not on GitHub, which has "
        "Roman_effarea_v9_SCA03_20261101.ecsv",
        "SCA03: GitHub also has Roman_effarea_v9_SCA03_20261101.ecsv",
    ]


def test_an_unreachable_github_is_reported_not_raised(tables):
    local, _, url = tables
    problems = zp.check_effarea(str(local), ["SCA01"], (url.parent / "absent.json").as_uri())
    assert len(problems) == 1 and problems[0].startswith("effective-area tables not checked: GitHub listing failed")


# --- asking ---

class Terminal:
    def isatty(self):
        return True


@pytest.mark.parametrize("answer, expected", [("y", True), ("YES", True), ("n", False), ("", False)])
def test_a_terminal_s_answer_decides(monkeypatch, answer, expected):
    monkeypatch.setattr(zp.sys, "stdin", Terminal())
    monkeypatch.setattr(builtins, "input", lambda: answer)
    assert zp.ask_to_continue(["x"], assume_yes=False) is expected


def test_end_of_input_is_no(monkeypatch):
    def closed():
        raise EOFError

    monkeypatch.setattr(zp.sys, "stdin", Terminal())
    monkeypatch.setattr(builtins, "input", closed)
    assert zp.ask_to_continue(["x"], assume_yes=False) is False


def test_with_no_terminal_it_stops_and_yes_continues_without_asking(monkeypatch, capsys):
    monkeypatch.setattr(builtins, "input", lambda: pytest.fail("asked"))
    assert zp.ask_to_continue(["x"], assume_yes=False) is False
    assert "rerun with --yes" in capsys.readouterr().err
    assert zp.ask_to_continue(["x"], assume_yes=True) is True


# --- the whole script ---

@pytest.fixture
def folder(tmp_path, monkeypatch):
    """A data folder with SCA01's table and references, CRDS faked and GitHub's check passing."""
    write_stsci_table(tmp_path / "Roman_effarea_v8_SCA01_20240301.ecsv", all_filters())
    names = ref_names("SCA01")
    write_gain(tmp_path / names["gain"], inner=2.2)
    write_area(tmp_path / names["area"])
    k = {f: zp.compute_filter_metrics(*zp.load_effective_area(tmp_path / "Roman_effarea_v8_SCA01_20240301.ecsv", f),
                                      2.0, 2.8e-13)["K"] for f in zp.FILTERS}
    write_photom(tmp_path / names["photom"], k)
    monkeypatch.setattr(zp, "crds_call", fake_crds(tmp_path))
    monkeypatch.setattr(zp, "check_effarea", lambda *a, **k: [])
    return tmp_path


def run(folder, *extra):
    return zp.main(["--effarea-dir", str(folder), "--cache-dir", str(folder), "--sca", "SCA01",
                    "--date", DATE, *extra])


def test_an_up_to_date_folder_runs_without_asking(folder, monkeypatch, capsys):
    monkeypatch.setattr(builtins, "input", lambda: pytest.fail("asked"))
    run(folder)
    rows = [line for line in capsys.readouterr().out.splitlines() if line.startswith("SCA01 |")]
    assert len(rows) == len(zp.FILTERS)
    assert all(row.split("|")[10].strip() == "1.0000" for row in rows)  # K equals the photom's photmjsr
    assert all(row.split("|")[6].strip() == "2.0000" for row in rows)   # gain 2.2 / 1.10


def test_a_missing_reference_with_no_terminal_stops_before_any_download(folder, monkeypatch, capsys):
    os.remove(folder / ref_names("SCA01")["gain"])
    monkeypatch.setattr(zp.urllib.request, "urlretrieve", lambda *a: pytest.fail("downloaded"))
    with pytest.raises(SystemExit, match="Stopped: the data folder is not up to date"):
        run(folder)
    captured = capsys.readouterr()
    assert captured.out == "" and "roman_wfi_gain_0001.asdf" in captured.err


def test_yes_downloads_the_missing_reference_and_runs(folder, monkeypatch, capsys):
    gain = folder / ref_names("SCA01")["gain"]
    saved = gain.read_bytes()
    gain.unlink()
    monkeypatch.setattr(zp, "crds_call", fake_crds(folder, {gain.name: len(saved)}))
    monkeypatch.setattr(zp.urllib.request, "urlretrieve", lambda url, path: Path(path).write_bytes(saved))
    run(folder, "--yes")
    out = capsys.readouterr().out
    assert gain.read_bytes() == saved
    assert sum(line.startswith("SCA01 |") for line in out.splitlines()) == len(zp.FILTERS)


def test_a_duplicate_table_stops_with_one_line_even_with_yes(folder, monkeypatch):
    shutil.copy(folder / "Roman_effarea_v8_SCA01_20240301.ecsv", folder / "Roman_effarea_v9_SCA01_x.ecsv")
    monkeypatch.setattr(zp.urllib.request, "urlretrieve", lambda *a: pytest.fail("downloaded"))
    with pytest.raises(SystemExit, match="Stopped: expected one .ecsv file naming SCA01"):
        run(folder, "--yes")
