"""modules/sfft/sfft_rapid_crowded.py: the kernel-fit masks on small synthetic images.

The script imports sfft at module level; these tests stub those imports so
the pure masking functions, ``bkg_mask`` (catalog masking, no segmentation)
and ``zero_mask``, run without the sfft library.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits
from astropy.table import Table

SCRIPT = Path(__file__).resolve().parents[2] / "modules" / "sfft" / "sfft_rapid_crowded.py"
STUBS = {
    "sfft": [], "sfft.utils": [], "sfft.utils.pyAstroMatic": [],
    "sfft.utils.pyAstroMatic.PYSEx": ["PY_SEx"], "sfft.CustomizedPacket": ["Customized_Packet"],
    "sfft.utils.SFFTSolutionReader": ["Realize_MatchingKernel"],
    "sfft.utils.DeCorrelationCalculator": ["DeCorrelation_Calculator"],
    "sfft_lowmem": [],
}


@pytest.fixture()
def script(monkeypatch):
    for name, attrs in STUBS.items():
        module = types.ModuleType(name)
        for attr in attrs:
            setattr(module, attr, object())
        monkeypatch.setitem(sys.modules, name, module)
    sys.modules["sfft_lowmem"].install = lambda: None
    spec = importlib.util.spec_from_file_location("sfft_rapid_crowded", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _image(tmp_path, data):
    path = tmp_path / "image.fits"
    fits.PrimaryHDU(data).writeto(path)
    return str(path)


def _catalog(tmp_path, rows):
    path = tmp_path / "cat.txt"
    Table(rows=rows, names=["X_IMAGE", "Y_IMAGE", "FLUX_MAX", "ISOAREA_IMAGE"]).write(path, format="ascii")
    return str(path)


def _mask(script, tmp_path, rows, **kwargs):
    image = _image(tmp_path, np.zeros((61, 61), dtype=np.float32))
    return script.bkg_mask(image, use_segm=False, bsmask=True, bsmask_cat=_catalog(tmp_path, rows),
                           sat_value=1e6, bsmask_value=50.0, **kwargs)


def test_a_bright_source_disc_is_centred_on_its_catalog_position(script, tmp_path):
    # 1-based catalog (31, 21) is 0-based pixel (x=30, y=20); before the fix the disc sat at (32, 22)
    mask = _mask(script, tmp_path, [(31.0, 21.0, 100.0, 10.0)], bsmask_radius=3.0)
    ys, xs = np.nonzero(mask)
    assert (xs.mean(), ys.mean()) == (30.0, 20.0)
    assert mask[20, 30 + 3] and mask[20, 30 - 3] and not mask[20, 30 + 4] and not mask[20, 30 - 4]


def test_a_saturated_source_disc_is_centred_on_its_catalog_position(script, tmp_path):
    image = _image(tmp_path, np.zeros((61, 61), dtype=np.float32))
    mask = script.bkg_mask(image, use_segm=False, bsmask=True,
                           bsmask_cat=_catalog(tmp_path, [(11.0, 41.0, 2e6, 1.0)]),
                           sat_value=1e6, satmask_radius1=4.0, satmask_radius2=4.0, bsmask_value=50.0,
                           bsmask_radius=0.0)
    ys, xs = np.nonzero(mask)
    assert (xs.mean(), ys.mean()) == (10.0, 40.0)


def test_without_isoarea_a_faint_large_source_is_not_masked(script, tmp_path):
    mask = _mask(script, tmp_path, [(31.0, 31.0, 10.0, 400.0)], bsmask_radius=3.0)
    assert not mask.any()


def test_isoarea_masks_a_faint_large_source_with_its_isophotal_radius(script, tmp_path):
    # ISOAREA 100 pi px^2 is an equivalent radius of 10 px, so isofactor 2 gives 20 px
    mask = _mask(script, tmp_path, [(31.0, 31.0, 10.0, 100.0 * np.pi)], bsmask_radius=3.0,
                 bsmask_isoarea=200.0, bsmask_isofactor=2.0)
    assert mask[30, 30 + 20] and not mask[30, 30 + 21]
    assert mask.sum() == np.count_nonzero(np.hypot(*np.indices((61, 61)) - 30.0) <= 20.0)


def test_isoarea_leaves_a_small_faint_source_alone_and_keeps_bsmask_radius_as_the_floor(script, tmp_path):
    rows = [(11.0, 11.0, 10.0, 50.0), (41.0, 41.0, 100.0, 4.0)]
    mask = _mask(script, tmp_path, rows, bsmask_radius=3.0, bsmask_isoarea=200.0, bsmask_isofactor=2.0)
    assert not mask[:25, :25].any()
    assert mask[40, 40 + 3] and not mask[40, 40 + 4]


def test_bright_pixels_are_grown_into_a_disc_without_a_catalog(script, tmp_path):
    data = np.zeros((61, 61), dtype=np.float32)
    data[30, 30] = 100.0
    mask = script.bkg_mask(_image(tmp_path, data), use_segm=False, bsmask=True, bsmask_cat=None,
                           bsmask_value=50.0, bsmask_radius=5.0)
    assert mask.sum() == np.count_nonzero(np.hypot(*np.indices((61, 61)) - 30.0) <= 5.0)


def test_zero_mask_covers_zeros_in_either_image_and_their_margin(script):
    sci = np.ones((21, 21))
    ref = np.ones((21, 21))
    sci[5, 5] = 0.0
    ref[15, 15] = 0.0
    mask = script.zero_mask(sci, ref, 2)
    yy, xx = np.indices(sci.shape)
    expected = (np.hypot(yy - 5, xx - 5) <= 2) | (np.hypot(yy - 15, xx - 15) <= 2)
    assert np.array_equal(mask, expected)


def test_zero_mask_with_no_zero_pixel_masks_nothing(script):
    assert not script.zero_mask(np.ones((9, 9)), np.ones((9, 9)), 2).any()
