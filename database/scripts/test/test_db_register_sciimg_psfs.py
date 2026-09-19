"""
The pure parts of database/scripts/db_register_sciimg_psfs.py: which objects under
a generation's psfs/ prefix are science PSFs, how the prefix is split, how the seal
check tells absence from failure, and how --filter is checked against the Filters
row for --fid.  No S3, no database.  The script imports its database modules and
boto3 inside main(), so this module needs no stand-ins for them.
"""

import unittest

from database.scripts.db_register_sciimg_psfs import (
    NOT_FOUND_CODES, PSF_OBJECT_PATTERN, filter_codes_agree, generation_is_sealed,
    generation_manifest_key, s3_error_code, select_psf_objects, split_s3_prefix)

PREFIX = "g0006-psf-f146/psfs/"


class SplitPrefixTests(unittest.TestCase):

    def test_bucket_and_key_prefix(self):
        self.assertEqual(split_s3_prefix("s3://roman-rapid-inputs-gbtds-sim/" + PREFIX),
                         ("roman-rapid-inputs-gbtds-sim", PREFIX))

    def test_trailing_slash_is_added(self):
        self.assertEqual(split_s3_prefix("s3://b/g0006-psf-f146/psfs"),
                         ("b", PREFIX))

    def test_not_an_s3_uri_is_refused(self):
        with self.assertRaises(ValueError):
            split_s3_prefix("/local/psfs/")


class GenerationManifestTests(unittest.TestCase):

    def test_the_marker_sits_beside_psfs_in_the_generation(self):
        self.assertEqual(generation_manifest_key(PREFIX), "g0006-psf-f146/_manifest.json")

    def test_a_prefix_that_is_not_a_psfs_directory_is_refused(self):
        for prefix in ("g0006-psf-f146/", "g0006-psf-f146/refimage_psfs/", "psfs/"):
            with self.assertRaises(ValueError, msg=prefix):
                generation_manifest_key(prefix)


class SelectPsfObjectsTests(unittest.TestCase):

    def test_pattern_matches_the_socsim_names(self):
        self.assertIsNotNone(PSF_OBJECT_PATTERN.match("sciimage_psf_f146_sca07.fits"))
        self.assertIsNotNone(PSF_OBJECT_PATTERN.match("sciimage_psf_F146_sca18.fits"))
        self.assertIsNone(PSF_OBJECT_PATTERN.match("refimage_psf_f146_sca07.fits"))
        self.assertIsNone(PSF_OBJECT_PATTERN.match("WFI_SCA07_F146_PSF_DET_DIST.fits"))

    def test_selects_only_science_psfs_directly_under_the_prefix(self):
        keys = [
            PREFIX + "sciimage_psf_f146_sca07.fits",
            PREFIX + "sciimage_psf_f146_sca01.fits",
            PREFIX + "manifest.json",
            PREFIX + "deeper/sciimage_psf_f146_sca02.fits",
            "g0006-psf-f146/refimage_psfs/refimage_psf_f146_sca07.fits",
            "g0002-psf/psfs/sciimage_psf_f146_sca03.fits",
        ]
        self.assertEqual(select_psf_objects(keys, PREFIX),
                         [(PREFIX + "sciimage_psf_f146_sca01.fits", 1),
                          (PREFIX + "sciimage_psf_f146_sca07.fits", 7)])

    def test_sorted_by_detector(self):
        keys = [PREFIX + "sciimage_psf_f146_sca{:02d}.fits".format(sca)
                for sca in (18, 3, 12)]
        self.assertEqual([sca for _key, sca in select_psf_objects(keys, PREFIX)],
                         [3, 12, 18])

    def test_empty_when_nothing_matches(self):
        self.assertEqual(select_psf_objects([PREFIX + "notes.txt"], PREFIX), [])

    def test_another_filters_psf_is_refused_not_registered(self):
        # PSFs is keyed by (fid, sca): an F158 object under an F146 registration
        # would become a version of the wrong identity, promotable over the right one.
        keys = [PREFIX + "sciimage_psf_f146_sca07.fits",
                PREFIX + "sciimage_psf_f158_sca07.fits"]
        with self.assertRaises(ValueError) as caught:
            select_psf_objects(keys, PREFIX, filter_name="f146")
        self.assertIn("f158", str(caught.exception))
        self.assertEqual(select_psf_objects(keys[:1], PREFIX, filter_name="F146"),
                         [(keys[0], 7)])


class _ClientError(Exception):
    """A botocore ClientError's shape: `response["Error"]["Code"]`."""

    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code, "Message": "stubbed"}}


class _S3Client:
    """head_object either succeeds or raises the configured error."""

    class exceptions:
        ClientError = _ClientError

    def __init__(self, error_code=None):
        self.error_code = error_code

    def head_object(self, Bucket, Key):
        if self.error_code is not None:
            raise _ClientError(self.error_code)
        return {}


class SealCheckTests(unittest.TestCase):

    def test_a_present_marker_is_sealed(self):
        self.assertTrue(generation_is_sealed(_S3Client(), "b", "g/_manifest.json"))

    def test_an_absent_marker_is_not_sealed(self):
        for code in NOT_FOUND_CODES:
            with self.subTest(code=code):
                self.assertFalse(generation_is_sealed(_S3Client(code), "b",
                                                      "g/_manifest.json"))

    def test_a_permission_failure_is_not_reported_as_unsealed(self):
        # The defect: every ClientError used to read as "the generation is not
        # complete", so a missing read grant sent the operator to look for an
        # abandoned staging run.  Not knowing is re-raised for main() to name.
        for code in ("403", "AccessDenied", "SlowDown", "InternalError"):
            with self.subTest(code=code):
                with self.assertRaises(_ClientError):
                    generation_is_sealed(_S3Client(code), "b", "g/_manifest.json")

    def test_the_error_code_is_read_from_the_botocore_shape(self):
        self.assertEqual(s3_error_code(_ClientError("AccessDenied")), "AccessDenied")
        self.assertEqual(s3_error_code(RuntimeError("no response attribute")), "")


class FilterFidAgreementTests(unittest.TestCase):

    def test_the_same_band_in_both_spellings_agrees(self):
        # Filters names the band with Roman's letter (W146); the filename says f146.
        self.assertTrue(filter_codes_agree("f146", "W146"))
        self.assertTrue(filter_codes_agree("F158", "H158"))
        self.assertTrue(filter_codes_agree("f087", "Z087"))

    def test_a_different_band_disagrees(self):
        # --filter f146 --fid 7 would file eighteen F146 PSFs under Z087.
        self.assertFalse(filter_codes_agree("f146", "Z087"))
        self.assertFalse(filter_codes_agree("f158", "W146"))

    def test_a_name_without_one_wavelength_code_never_agrees(self):
        for token, name in (("f146", ""), ("f146", None), ("grism", "W146"),
                            ("f146", "W146_146"), ("f14", "W146")):
            with self.subTest(token=token, name=name):
                self.assertFalse(filter_codes_agree(token, name))

    def test_every_seeded_filters_name_carries_exactly_one_code(self):
        # rapid_systems 009-seed-data.sql; if a band is ever added without a
        # three-digit code the cross-check must be rethought, not bypassed.
        for name in ("F184", "H158", "J129", "K213", "R062", "Y106", "Z087", "W146"):
            with self.subTest(name=name):
                self.assertTrue(filter_codes_agree("f" + name[1:], name))


if __name__ == "__main__":
    unittest.main()
