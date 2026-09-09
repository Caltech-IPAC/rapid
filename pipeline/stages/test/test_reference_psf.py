"""
File:    test_reference_psf.py

The reference PSF is located from the science PSF's generation and a
release-content filename template (dev d11c87d4, e3c15953 lineage). Pure
logic; no I/O.
"""

import unittest

from pipeline.runtime.errors import InputError
from pipeline.stages.reference_psf import reference_psf_uri, substitute_tokens

SCIENCE_PSF = ("s3://roman-rapid-inputs-gbtds-sim/g0006-psf-f146/psfs/"
               "sciimage_psf_f146_sca07.fits")


class SubstituteTokensTests(unittest.TestCase):

    def test_fid_and_zero_padded_sca(self):
        self.assertEqual(substitute_tokens("refimage_psf_fFID_scaSCAID.fits", 8, 7),
                         "refimage_psf_f8_sca07.fits")

    def test_two_digit_sca_is_not_padded_further(self):
        self.assertEqual(substitute_tokens("x_scaSCAID.fits", 8, 18),
                         "x_sca18.fits")

    def test_a_template_without_tokens_is_unchanged(self):
        self.assertEqual(substitute_tokens("refimage_psf_fid8.fits", 8, 7),
                         "refimage_psf_fid8.fits")

    def test_sca_in_a_real_filename_is_not_a_token(self):
        # "SCA" appears in real names (WFI_SCA07_...); only "SCAID" is one.
        self.assertEqual(substitute_tokens("WFI_SCA07_F146_PSF.fits", 8, 3),
                         "WFI_SCA07_F146_PSF.fits")


class ReferencePsfUriTests(unittest.TestCase):

    def test_per_detector_template_beside_the_science_psf(self):
        self.assertEqual(
            reference_psf_uri(SCIENCE_PSF, "refimage_psf_f146_scaSCAID.fits",
                              8, 7),
            "s3://roman-rapid-inputs-gbtds-sim/g0006-psf-f146/refimage_psfs/"
            "refimage_psf_f146_sca07.fits")

    def test_per_filter_template(self):
        psf = ("s3://roman-rapid-inputs-gbtds-sim/g0002-psf/psfs/"
               "WFI_SCA07_F146_PSF_DET_DIST.fits")
        self.assertEqual(
            reference_psf_uri(psf, "refimage_psf_fidFID.fits", 8, 7),
            "s3://roman-rapid-inputs-gbtds-sim/g0002-psf/refimage_psfs/"
            "refimage_psf_fid8.fits")

    def test_empty_template_is_refused(self):
        for template in ("", "  "):
            with self.assertRaises(InputError):
                reference_psf_uri(SCIENCE_PSF, template, 8, 7)

    def test_science_psf_outside_a_psfs_directory_is_refused(self):
        with self.assertRaises(InputError):
            reference_psf_uri("s3://bucket/gen/other/psf.fits", "r.fits", 8, 7)
        with self.assertRaises(InputError):
            reference_psf_uri("s3://bucket/psf.fits", "r.fits", 8, 7)

    def test_non_s3_uri_is_refused(self):
        with self.assertRaises(InputError):
            reference_psf_uri("/scratch/psfs/psf.fits", "r.fits", 8, 7)


if __name__ == "__main__":
    unittest.main()
