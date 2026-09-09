"""
The pure parts of database/scripts/db_register_sciimg_psfs.py: which objects under
a generation's psfs/ prefix are science PSFs, and how the prefix is split.  No S3, no
database; psycopg2 and boto3 are stubbed if absent so the module imports anywhere.
"""

import sys
import types
import unittest

for name in ("psycopg2", "boto3"):
    if name not in sys.modules:
        try:
            __import__(name)
        except ImportError:
            stub = types.ModuleType(name)
            stub.DatabaseError = Exception
            sys.modules[name] = stub

from database.scripts.db_register_sciimg_psfs import (  # noqa: E402
    PSF_OBJECT_PATTERN, generation_manifest_key, select_psf_objects, split_s3_prefix)

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


if __name__ == "__main__":
    unittest.main()
