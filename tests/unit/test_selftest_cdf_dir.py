"""``rapidpipe.selftest.support.fakedifftools.cdf_dir``'s resolution order.

Defect (measured on AWS Batch, job 6d256b71-5f82-47c0-91f6-025588d08467):
inside the pipeline image, ``rapidpipe`` is installed into the conda
environment's ``site-packages`` and the repository's own ``cdf/`` never
lands under ``fakedifftools.REPO_ROOT`` there (``tests/`` and the source
layout that makes ``REPO_ROOT / "cdf"`` correct are both checkout-only
things -- ``containers/rapid-pipeline/build.sh`` excludes ``tests/`` and
copies the filtered source tree to ``/code``). ``build_input_set`` and
``rapidpipe.selftest.difference`` used the checkout-relative path
unconditionally and failed with ``FileNotFoundError`` there. These tests
cover the resolver in isolation, monkeypatching ``REPO_ROOT`` rather than
actually relocating the package, since the real failure mode is about
*which directory exists*, not this test process's own install layout.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import rapidpipe.selftest.support.fakedifftools as fakedifftools


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(fakedifftools.CDF_DIR_ENV, raising=False)


def test_cdf_dir_prefers_the_checkout_when_present():
    # The real checkout's cdf/ exists in this test's own environment --
    # REPO_ROOT is untouched, so this is the normal, unpatched path.
    assert fakedifftools.cdf_dir() == fakedifftools.REPO_ROOT / "cdf"


def test_cdf_dir_falls_back_to_rapid_cfg_when_the_checkout_cdf_is_absent(
        monkeypatch, tmp_path):
    monkeypatch.setattr(fakedifftools, "REPO_ROOT", tmp_path / "no-such-repo")
    override = tmp_path / "override-cdf"
    override.mkdir()
    monkeypatch.setenv(fakedifftools.CDF_DIR_ENV, str(override))

    assert fakedifftools.cdf_dir() == override


def test_cdf_dir_last_candidate_is_hard_coded_code_cdf(monkeypatch, tmp_path):
    # No RAPID_CFG set (the autouse fixture clears it), and this test
    # host has no /code/cdf of its own -- /code/cdf is a fixed candidate,
    # not derived from REPO_ROOT, so it can't be relocated into tmp_path
    # for a positive test here. Confirm it is exactly the last candidate
    # tried, matching the image's own default cfg_path
    # (rapidpipe/settings/difference.toml).
    monkeypatch.setattr(fakedifftools, "REPO_ROOT", tmp_path / "no-such-repo")
    with pytest.raises(FileNotFoundError) as excinfo:
        fakedifftools.cdf_dir()
    assert "/code/cdf" in str(excinfo.value)
    assert str(tmp_path / "no-such-repo" / "cdf") in str(excinfo.value)


def test_cdf_dir_raises_a_clear_error_naming_every_candidate_when_none_exists(
        monkeypatch, tmp_path):
    monkeypatch.setattr(fakedifftools, "REPO_ROOT", tmp_path / "no-such-repo")
    monkeypatch.setenv(fakedifftools.CDF_DIR_ENV, str(tmp_path / "no-such-override"))

    with pytest.raises(FileNotFoundError) as excinfo:
        fakedifftools.cdf_dir()
    message = str(excinfo.value)
    assert str(tmp_path / "no-such-repo" / "cdf") in message
    assert str(tmp_path / "no-such-override") in message
    assert "/code/cdf" in message


def test_cdf_dir_rapid_cfg_only_consulted_when_checkout_cdf_is_absent(monkeypatch, tmp_path):
    # REPO_ROOT / "cdf" exists (this test's real checkout) -- an env
    # override must not shadow it, so the fixture and the stage under
    # test keep reading the same directory as everyone else in a checkout.
    monkeypatch.setenv(fakedifftools.CDF_DIR_ENV, str(tmp_path))
    assert fakedifftools.cdf_dir() == fakedifftools.REPO_ROOT / "cdf"


def test_cdf_dir_return_type_is_a_path():
    assert isinstance(fakedifftools.cdf_dir(), Path)
