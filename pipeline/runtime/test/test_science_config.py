"""Tests for the release-content science configuration reader.

Two kinds of test here. The first kind covers the reader's contract —
fail-loud on absence, no defaults, canonical digest. The second kind is
the round-trip: every value in cdf/science/pipeline.toml is checked
against the master .ini it was extracted from, so the two cannot drift
while both exist. That test deletes itself with the .ini at W6.
"""

import configparser
import hashlib
import json
import os
import tempfile
import unittest
from unittest import mock

from pipeline.runtime import science_config
from pipeline.runtime.errors import ConfigError

# .../rapid/pipeline/runtime/test/test_science_config.py -> .../rapid
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
MASTER_INI = os.path.join(
    REPO_ROOT, "cdf", "awsBatchSubmitJobs_launchSingleSciencePipeline.ini")
SCIENCE_TOML = os.path.join(REPO_ROOT, "cdf", "science", "pipeline.toml")

MINIMAL = """
[release]
schema_version = 1

[science]
min_images_to_coadd = 3
diff_flavor = "sfft"

[sfft]
run_sfft = true
crossconv_flag = false
"""


def write_config(text):
    handle = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
    handle.write(text)
    handle.close()
    return handle.name


class PathResolutionTests(unittest.TestCase):

    def test_path_resolves_against_an_explicit_root(self):
        path = science_config.config_path(software_root="/code")
        self.assertEqual(path, "/code/cdf/science/pipeline.toml")

    def test_path_resolves_against_rapid_sw(self):
        with mock.patch.dict(os.environ, {"RAPID_SW": "/code"}):
            self.assertEqual(science_config.config_path(),
                             "/code/cdf/science/pipeline.toml")

    def test_unset_software_root_is_a_config_error_not_a_cwd_fallback(self):
        # Falling back to the working directory would let a job read
        # science configuration that nobody can identify afterwards.
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ConfigError) as caught:
                science_config.config_path()
        self.assertIn("RAPID_SW", str(caught.exception))


class LoadTests(unittest.TestCase):

    def test_values_keep_their_types(self):
        # The whole reason for TOML over the .ini: an int is an int and a
        # bool is a bool, so no consumer has to coerce and none can
        # coerce wrongly.
        content = science_config.load(path=write_config(MINIMAL))
        self.assertIsInstance(content["science"]["min_images_to_coadd"], int)
        self.assertIsInstance(content["sfft"]["run_sfft"], bool)
        self.assertIs(content["sfft"]["crossconv_flag"], False)
        self.assertIsInstance(content["science"]["diff_flavor"], str)

    def test_missing_file_is_a_config_error(self):
        with self.assertRaises(ConfigError) as caught:
            science_config.load(path="/nonexistent/pipeline.toml")
        self.assertIn("ships with the image", str(caught.exception))

    def test_unparseable_file_is_a_config_error(self):
        with self.assertRaises(ConfigError):
            science_config.load(path=write_config("[unclosed\n"))

    def test_wrong_schema_version_is_refused(self):
        text = MINIMAL.replace("schema_version = 1", "schema_version = 2")
        with self.assertRaises(ConfigError) as caught:
            science_config.load(path=write_config(text))
        self.assertIn("schema_version", str(caught.exception))

    def test_absent_schema_version_is_refused(self):
        with self.assertRaises(ConfigError):
            science_config.load(path=write_config("[science]\nx = 1\n"))


class DigestTests(unittest.TestCase):

    def test_digest_is_stable_across_key_order(self):
        first = science_config.digest({"b": {"y": 2}, "a": {"x": 1}})
        second = science_config.digest({"a": {"x": 1}, "b": {"y": 2}})
        self.assertEqual(first, second)

    def test_digest_changes_with_any_value(self):
        base = science_config.digest({"a": {"x": 1}})
        changed = science_config.digest({"a": {"x": 2}})
        self.assertNotEqual(base, changed)

    def test_digest_is_hex_sha256(self):
        value = science_config.digest({"a": {"x": 1}})
        self.assertEqual(len(value), 64)
        int(value, 16)

    def test_digest_matches_the_parameter_tree_canonical_form(self):
        # Both digests must be computed the same way, so that anyone
        # reading provenance reads them the same way.
        content = {"a": {"x": 1}}
        canonical = json.dumps(content, sort_keys=True,
                               separators=(",", ":"), ensure_ascii=False,
                               default=str)
        expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        self.assertEqual(science_config.digest(content), expected)


class AccessorTests(unittest.TestCase):

    def setUp(self):
        self.content = science_config.load(path=write_config(MINIMAL))

    def test_section_returns_a_copy(self):
        values = science_config.section(self.content, "sfft")
        values["run_sfft"] = False
        self.assertIs(self.content["sfft"]["run_sfft"], True)

    def test_missing_section_is_a_config_error_naming_what_exists(self):
        with self.assertRaises(ConfigError) as caught:
            science_config.section(self.content, "zogy")
        self.assertIn("sfft", str(caught.exception))

    def test_value_reads_through(self):
        self.assertEqual(
            science_config.value(self.content, "science", "diff_flavor"),
            "sfft")

    def test_missing_key_is_a_config_error_with_no_default_parameter(self):
        with self.assertRaises(ConfigError) as caught:
            science_config.value(self.content, "science", "absent")
        self.assertIn("release fault", str(caught.exception))
        # There is no default= parameter to reach for, by design.
        self.assertNotIn("default", science_config.value.__code__.co_varnames)


class AuxiliaryIdentityTests(unittest.TestCase):

    def test_auxiliary_files_are_identified_by_the_image_digest(self):
        identity = science_config.auxiliary_identity("sha256:abc")
        self.assertEqual(identity["auxiliary_identified_by"], "image_digest")
        self.assertEqual(identity["image_digest"], "sha256:abc")


class ShippedConfigurationTests(unittest.TestCase):
    """The file this repo actually ships."""

    def test_the_shipped_file_loads(self):
        content = science_config.load(path=SCIENCE_TOML)
        self.assertEqual(content["release"]["schema_version"],
                         science_config.SUPPORTED_SCHEMA_VERSION)

    def test_the_relocated_tree_parameters_are_present(self):
        # These two left the SSM tree in W4A; if they are not here, they
        # are nowhere. PRESENCE is what this test is for.
        #
        # It used to pin `min_images_to_coadd` to 3 and broke when the release
        # content moved to 2 (0f39ee6, Ben resolving the W4 divergence) — the
        # commit changed the toml and nothing told it a test held the old
        # number. An OPERATING VALUE is the release content's to state and an
        # operator's to change; a unit test asserting one turns every such
        # decision into a test failure, which teaches people to edit the test
        # rather than to think about the change. The type and the presence are
        # the contract here; `ref_image.min_n_images_to_coadd` is the value the
        # gatherers actually read, and it lives in the same file.
        content = science_config.load(path=SCIENCE_TOML)
        self.assertIsInstance(content["science"]["min_images_to_coadd"], int)
        self.assertGreater(content["science"]["min_images_to_coadd"], 0)
        self.assertEqual(content["science"]["diff_flavor"], "sfft")

    def test_no_placeholder_sentinels_survived_the_extraction(self):
        # A carried "fill_in_by_launch_script" would be a landmine: a
        # per-invocation slot masquerading as release content.
        content = science_config.load(path=SCIENCE_TOML)
        sentinels = ("fill_in_by_launch_script", "fill_in_by_pipeline_script",
                     "to_be_filled_by_script")
        for name, values in content.items():
            for key, value in values.items():
                if isinstance(value, str):
                    self.assertNotIn(value, sentinels,
                                     f"{name}.{key} carries a placeholder")

    def test_load_with_digest_returns_an_independent_copy(self):
        first, digest_one = science_config.load_with_digest(path=SCIENCE_TOML)
        first["science"]["diff_flavor"] = "mutated"
        second, digest_two = science_config.load_with_digest(path=SCIENCE_TOML)
        self.assertEqual(second["science"]["diff_flavor"], "sfft")
        self.assertEqual(digest_one, digest_two)


class RoundTripAgainstTheMasterIniTests(unittest.TestCase):
    """Every extracted value still equals the .ini it came from.

    This is the guard on a transitional state: two files hold the same
    science values until W6 deletes the .ini behind the cutover fence,
    and an edit to one and not the other is exactly the drift the
    one-home principle exists to prevent. When the .ini goes, so does
    this test class.
    """

    @classmethod
    def setUpClass(cls):
        cls.ini = configparser.ConfigParser()
        cls.ini.read(MASTER_INI)
        cls.toml = science_config.load(path=SCIENCE_TOML)

    def test_the_master_ini_is_still_present_and_readable(self):
        # Its deletion is W6's fence, not W4's.
        self.assertTrue(self.ini.sections())

    def test_every_extracted_value_equals_the_ini(self):
        # `release`, `science` and `tessellation` are authored, not
        # extracted: the first is new, the second was relocated from SSM
        # rather than from the .ini, and the third (W7) pins the sky
        # tessellation version, which the .ini never carried at all — the
        # tessellation was identified only by the SQLite file baked into
        # the image. Everything else must round-trip exactly.
        authored = {"release", "science", "tessellation"}
        # ONE DELIBERATE DIVERGENCE, recorded rather than waived silently.
        #
        # The .ini carries `sextractor_FLAG_IMAGE = fill_in_by_launch_script`
        # in all four of its [SEXTRACTOR_*] blocks. No launcher ever
        # substituted it, and `build_sextractor_command_line_args` reads the
        # key but has its two emission lines commented out — so the value is
        # required to exist and can never reach SExtractor. Release content
        # states "NONE" instead, because
        # `test_no_placeholder_sentinels_survived_the_extraction` rightly
        # refuses to let a per-invocation placeholder pose as a release value.
        # The two rules cannot both hold for this key; the sentinel ban is the
        # one that protects the next reader, so the round-trip yields here.
        diverged = {"sextractor_flag_image"}
        compared = 0
        for name, values in self.toml.items():
            if name in authored:
                continue
            ini_section = name.upper()
            self.assertIn(ini_section, self.ini,
                          f"{name} has no counterpart section in the .ini")
            for key, value in values.items():
                if key in diverged:
                    # Still assert the .ini side is what this exemption was
                    # written for: if the master ever gains a real value, the
                    # divergence is no longer justified and this must fail.
                    self.assertEqual(
                        "fill_in_by_launch_script",
                        self.ini[ini_section][key].strip(),
                        f"{ini_section}.{key} is no longer a placeholder in "
                        "the .ini; the toml should now round-trip it")
                    self.assertEqual("NONE", value, f"{ini_section}.{key}")
                    continue
                self.assertIn(key, self.ini[ini_section],
                              f"{ini_section}.{key} is not in the .ini")
                raw = self.ini[ini_section][key].strip()
                if isinstance(value, bool):
                    self.assertEqual(str(value).lower(), raw.lower(),
                                     f"{ini_section}.{key}")
                elif isinstance(value, (int, float)):
                    self.assertEqual(float(value), float(raw),
                                     f"{ini_section}.{key}")
                else:
                    self.assertEqual(value, raw, f"{ini_section}.{key}")
                compared += 1
        # A silently-empty comparison would pass this test while proving
        # nothing, so the count is asserted too.
        self.assertGreater(compared, 300,
                           "the round-trip compared implausibly few keys")

    def test_the_known_divergence_is_resolved(self):
        # THE DIVERGENCE IS GONE, and this test is what noticed.
        #
        # It used to assert the divergence's shape — .ini 2, ref_image 2,
        # science 3 — so that a silent change to either side failed here. A
        # change came (0f39ee6, "min_images_to_coadd operating value is 2",
        # Ben resolving the W4 divergence): the toml moved to 2 and this test
        # failed, which is exactly the job it was written for. It is NOT
        # silent, so the assertion moves to the new state rather than the
        # change being argued with.
        #
        # What it guards now is agreement: the two toml keys and the .ini all
        # state one value, so a future edit that re-opens the gap between them
        # fails here. The specific NUMBER is deliberately not pinned — that is
        # an operating value and the release content's to state.
        ini_value = int(self.ini["REF_IMAGE"]["min_n_images_to_coadd"])
        self.assertEqual(ini_value,
                         self.toml["ref_image"]["min_n_images_to_coadd"])
        self.assertEqual(ini_value,
                         self.toml["science"]["min_images_to_coadd"],
                         "the W4 divergence has re-opened: the science and "
                         "ref_image minimums no longer agree with the .ini")


if __name__ == "__main__":
    unittest.main()
