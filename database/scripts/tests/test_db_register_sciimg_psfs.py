"""Tests for PSF filename parsing in db_register_sciimg_psfs.py.

Run with:  python -m pytest database/scripts/tests/test_db_register_sciimg_psfs.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, os.pardir))

from database.scripts.db_register_sciimg_psfs import parse_psf_filename


def test_each_family_parses_its_own_names_only():
    assert parse_psf_filename("WFI_SCA04_F146_PSF_DET_DIST.fits", "wfi") == (4, "F146", None)
    assert parse_psf_filename("sciimage_psf_phavg_fid8_sca04.fits", "phavg") == (4, None, 8)
    assert parse_psf_filename("sciimage_psf_phavg_fid8_sca04.fits", "sciimage") == (None, None, None)
