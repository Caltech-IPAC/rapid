"""
Tests of the gzip compression level of the FITS files written by pipeline/ingestL2Files.py
(gzip_file and the L2GZIPLEVEL environment variable).

ingestL2Files.py reads its configuration when it is imported and exits on a bad value, so it is
imported in a subprocess here.

Run from the repository root:  python -m pytest pipeline/test
"""

import gzip
import os
import subprocess
import sys

import pytest

pytest.importorskip("roman_datamodels")

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))

IMPORT_AND_GZIP = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location("ingest", sys.argv[1] + "/pipeline/ingestL2Files.py")
ingest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ingest)
level = None if sys.argv[4] == "default" else int(sys.argv[4])
ingest.gzip_file(sys.argv[2], sys.argv[3], compresslevel=level)
"""


def run_ingest(tmp_path, env_level, call_level="default"):
    env = dict(os.environ, RAPIDL2INPUTBUCKET="in", RAPIDL2OUTPUTBUCKET="out", RAPIDDATASOURCE="SOCSIMS",
               RAPID_WORK=str(tmp_path), PYTHONPATH=REPO)
    env.pop("L2GZIPLEVEL", None)
    if env_level is not None:
        env["L2GZIPLEVEL"] = env_level
    data_file = tmp_path / "data.fits"
    data_file.write_bytes(os.urandom(4096) + bytes(range(256)) * 4096)
    gz_file = tmp_path / "data.fits.gz"
    result = subprocess.run([sys.executable, "-c", IMPORT_AND_GZIP, REPO, str(data_file), str(gz_file), call_level],
                            env=env, capture_output=True, text=True)
    return result, data_file, gz_file


def gzip_xfl(path):
    # Byte 8 of the gzip header: 2 = slowest (level 9), 4 = fastest (level 1), 0 otherwise.
    return path.read_bytes()[8]


@pytest.mark.parametrize("env_level,expected_xfl", [(None, 4), ("1", 4), ("9", 2), ("6", 0)])
def test_level_from_environment(tmp_path, env_level, expected_xfl):
    result, data_file, gz_file = run_ingest(tmp_path, env_level)
    assert result.returncode == 0, result.stdout + result.stderr
    assert gzip.decompress(gz_file.read_bytes()) == data_file.read_bytes()
    assert gzip_xfl(gz_file) == expected_xfl


def test_explicit_level_overrides_environment(tmp_path):
    result, _, gz_file = run_ingest(tmp_path, "1", call_level="9")
    assert result.returncode == 0, result.stdout + result.stderr
    assert gzip_xfl(gz_file) == 2


@pytest.mark.parametrize("bad", ["10", "-1", "fast"])
def test_bad_level_exits_64(tmp_path, bad):
    result, _, _ = run_ingest(tmp_path, bad)
    assert result.returncode == 64
    assert "L2GZIPLEVEL" in result.stdout
