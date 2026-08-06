"""
File:    test_workdir.py

Tests for `WorkingDirectory` — the per-attempt tree with derived, sanitized
paths and no cwd dependence.

Every test creates its tree under a `tempfile.TemporaryDirectory`, never under
the module's own `DEFAULT_WORK_ROOT` (`/tmp/rapid`): a test that wrote there
would leave real directories behind and could collide with another test run
or a real attempt on the same host.

Two properties get the most attention because they are the module's actual
security boundary: that `path()` refuses to resolve outside `root`, and that
`_safe_component` cannot be talked into producing `..`, an absolute path, or
an empty name.
"""

import os
import tempfile
import unittest

from pipeline.runtime.errors import ConfigError
from pipeline.runtime.workdir import (
    BUNDLE_SUBDIR,
    SCRATCH_SUBDIR,
    STAGE_LOG_SUBDIR,
    TOOL_CAPTURE_SUBDIR,
    WorkingDirectory,
    _safe_component,
)


class CreateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_create_makes_all_five_directories(self):
        work = WorkingDirectory.create("attempt-1", work_root=self.tmp.name)
        for directory in (work.root, work.bundle_dir, work.scratch_dir,
                          work.stage_log_dir, work.tool_capture_dir):
            self.assertTrue(os.path.isdir(directory), f"{directory} missing")

    def test_create_is_idempotent(self):
        # A runtime restarting inside the same container finds its own
        # directory and continues; create() must not raise on the second call.
        WorkingDirectory.create("attempt-1", work_root=self.tmp.name)
        work = WorkingDirectory.create("attempt-1", work_root=self.tmp.name)
        self.assertTrue(os.path.isdir(work.root))

    def test_root_is_under_the_work_root(self):
        work = WorkingDirectory.create("attempt-1", work_root=self.tmp.name)
        self.assertTrue(work.root.startswith(os.path.abspath(self.tmp.name)))


class AttemptKeySanitizationTests(unittest.TestCase):
    """A scheduler-supplied attempt_key is untrusted input; a slash or a `..`
    component must not be able to place the tree elsewhere on the filesystem.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_slash_in_attempt_key_is_sanitized_to_one_component(self):
        work = WorkingDirectory.create("job/../../etc", work_root=self.tmp.name)
        root_parent = os.path.abspath(self.tmp.name)
        # Exactly one path component was added under work_root.
        relative = os.path.relpath(work.root, root_parent)
        self.assertNotIn(os.sep, relative)

    def test_sanitized_directory_does_not_escape_the_work_root(self):
        work = WorkingDirectory.create("../../../etc/passwd",
                                       work_root=self.tmp.name)
        root_parent = os.path.abspath(self.tmp.name) + os.sep
        self.assertTrue(work.root.startswith(root_parent))

    def test_dotdot_only_key_is_rejected_rather_than_producing_dotdot(self):
        # "..", stripped of everything _safe_component strips, reduces to
        # nothing — _safe_component refuses rather than returning "..", which
        # is the one string that would be actively dangerous to accept here.
        with self.assertRaises(ValueError):
            WorkingDirectory.create("..", work_root=self.tmp.name)


class PathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = WorkingDirectory.create("attempt-1", work_root=self.tmp.name)

    def test_no_parts_returns_root(self):
        self.assertEqual(self.work.path(), self.work.root)

    def test_simple_relative_path_lands_under_root(self):
        result = self.work.path("subdir", "file.txt")
        self.assertEqual(result,
                         os.path.join(self.work.root, "subdir", "file.txt"))

    def test_dotdot_component_raises_configerror(self):
        with self.assertRaises(ConfigError):
            self.work.path("..")

    def test_nested_dotdot_traversal_raises_configerror(self):
        with self.assertRaises(ConfigError):
            self.work.path("a", "..", "..")

    def test_absolute_path_component_raises_configerror(self):
        with self.assertRaises(ConfigError):
            self.work.path("/etc/passwd")


class DerivedPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = WorkingDirectory.create("attempt-1", work_root=self.tmp.name)

    def test_scratch_is_under_scratch_subdir(self):
        result = self.work.scratch("intermediate.fits")
        self.assertEqual(
            result,
            os.path.join(self.work.root, SCRATCH_SUBDIR, "intermediate.fits"))

    def test_bundle_path_is_under_bundle_subdir(self):
        result = self.work.bundle_path("stderr.log")
        self.assertEqual(
            result, os.path.join(self.work.root, BUNDLE_SUBDIR, "stderr.log"))

    def test_stage_dir_is_under_scratch_and_is_created(self):
        directory = self.work.stage_dir("difference")
        self.assertEqual(
            directory,
            os.path.join(self.work.root, SCRATCH_SUBDIR, "difference"))
        self.assertTrue(os.path.isdir(directory))

    def test_stage_log_path_is_under_bundle_stage_logs(self):
        result = self.work.stage_log_path("difference")
        self.assertEqual(
            result,
            os.path.join(self.work.root, BUNDLE_SUBDIR, STAGE_LOG_SUBDIR,
                         "difference.log"))

    def test_tool_capture_path_is_under_bundle_tool_output(self):
        result = self.work.tool_capture_path("difference")
        self.assertEqual(
            result,
            os.path.join(self.work.root, BUNDLE_SUBDIR, TOOL_CAPTURE_SUBDIR,
                         "difference.out"))

    def test_stage_names_with_unsafe_characters_are_sanitized_in_log_path(self):
        result = self.work.stage_log_path("stage/with/slashes")
        self.assertEqual(
            result,
            os.path.join(self.work.root, BUNDLE_SUBDIR, STAGE_LOG_SUBDIR,
                         "stage_with_slashes.log"))

    def test_stage_names_with_unsafe_characters_are_sanitized_in_capture_path(self):
        # Each '/' becomes '_', then a leading run of dots is stripped:
        # "../escape" -> ".._escape" -> "_escape" (the dots go, the
        # underscore from the slash does not).
        result = self.work.tool_capture_path("../escape")
        self.assertEqual(
            result,
            os.path.join(self.work.root, BUNDLE_SUBDIR, TOOL_CAPTURE_SUBDIR,
                         "_escape.out"))
        root_with_sep = self.work.root.rstrip(os.sep) + os.sep
        self.assertTrue(result.startswith(root_with_sep))


class RemoveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_remove_deletes_the_tree(self):
        work = WorkingDirectory.create("attempt-1", work_root=self.tmp.name)
        self.assertTrue(os.path.isdir(work.root))
        work.remove()
        self.assertFalse(os.path.exists(work.root))

    def test_remove_on_already_removed_tree_does_not_raise(self):
        # Called only after the bundle is uploaded; a container about to exit
        # should not fail over a directory that is already gone (or is
        # leaving with the container regardless).
        work = WorkingDirectory.create("attempt-1", work_root=self.tmp.name)
        work.remove()
        work.remove()  # must not raise


class SafeComponentTests(unittest.TestCase):
    def test_empty_string_raises_valueerror(self):
        with self.assertRaises(ValueError):
            _safe_component("")

    def test_dots_only_string_raises_valueerror(self):
        with self.assertRaises(ValueError):
            _safe_component("...")

    def test_unsafe_characters_become_underscores(self):
        self.assertEqual(_safe_component("job/id:7"), "job_id_7")

    def test_leading_dot_is_dropped(self):
        self.assertEqual(_safe_component(".hidden"), "hidden")

    def test_safe_characters_pass_through_unchanged(self):
        self.assertEqual(_safe_component("job-1.attempt_2"), "job-1.attempt_2")

    def test_non_string_input_is_stringified(self):
        self.assertEqual(_safe_component(42), "42")


if __name__ == "__main__":
    unittest.main()
