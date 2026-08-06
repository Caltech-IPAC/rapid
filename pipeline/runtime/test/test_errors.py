"""
File:    test_errors.py

Tests for the runtime error taxonomy: the exception classes, the category
allowlist they validate against, `categorize`, and `serialize_error`.

The allowlist itself is deliberately duplicated between this module and
`observability.attempts` (see errors.py's module docstring) so that
`pipeline.runtime` is importable without pulling in the attempt writer. What
makes that duplication safe rather than a second source of truth quietly
drifting is `AllowlistMatchesAttemptWriterTests` below — it pins the two
copies equal, so a change to one that is not mirrored in the other fails
here rather than at a database round trip.
"""

import unittest

from pipeline.runtime.errors import (
    APPLICATION_ERROR_CATEGORIES,
    ERROR_CATEGORIES,
    RECONCILER_ERROR_CATEGORIES,
    UNCLASSIFIED_CATEGORY,
    ConfigError,
    DBError,
    InputError,
    RecordsError,
    ResourceError,
    SerializedError,
    StorageError,
    ToolError,
    categorize,
    is_valid_category,
    serialize_error,
)


class DefaultCategoryTests(unittest.TestCase):
    """Each subclass carries the category documented in its docstring as the
    class default, used whenever a raiser does not say otherwise."""

    def test_tool_error_defaults_to_tool_failure(self):
        self.assertEqual(ToolError("boom").error_category, "tool_failure")

    def test_input_error_defaults_to_input_invalid(self):
        self.assertEqual(InputError("boom").error_category, "input_invalid")

    def test_config_error_defaults_to_config_invalid(self):
        self.assertEqual(ConfigError("boom").error_category, "config_invalid")

    def test_db_error_defaults_to_db_error(self):
        self.assertEqual(DBError("boom").error_category, "db_error")

    def test_storage_error_defaults_to_storage_error(self):
        self.assertEqual(StorageError("boom").error_category, "storage_error")

    def test_records_error_defaults_to_records_error(self):
        self.assertEqual(RecordsError("boom").error_category, "records_error")

    def test_resource_error_defaults_to_resource_exhausted(self):
        self.assertEqual(ResourceError("boom").error_category,
                         "resource_exhausted")


class CategoryOverrideTests(unittest.TestCase):
    """A raiser that knows better than the class default may say so, as long
    as what it says is one of the allowlisted, non-reconciler categories."""

    def test_category_kwarg_overrides_the_class_default(self):
        exc = InputError("missing", category="input_missing")
        self.assertEqual(exc.error_category, "input_missing")

    def test_config_error_can_be_raised_as_reference_missing(self):
        exc = ConfigError("no such version", category="reference_missing")
        self.assertEqual(exc.error_category, "reference_missing")

    def test_db_error_can_be_raised_as_db_unavailable(self):
        exc = DBError("connection refused", category="db_unavailable")
        self.assertEqual(exc.error_category, "db_unavailable")

    def test_category_outside_the_allowlist_raises_value_error_at_raise_site(self):
        # The point of validating in the constructor rather than at emission:
        # the traceback here points at the code that chose the bad category.
        with self.assertRaises(ValueError):
            InputError("boom", category="not_a_real_category")

    def test_reconciler_category_scheduler_reclaimed_is_rejected(self):
        # The application cannot author a fact describing an attempt it
        # never ran, even though the category is in the shared vocabulary.
        with self.assertRaises(ValueError):
            ResourceError("boom", category="scheduler_reclaimed")

    def test_reconciler_category_scheduler_provisioning_is_rejected(self):
        with self.assertRaises(ValueError):
            ToolError("boom", category="scheduler_provisioning")


class DetailsTests(unittest.TestCase):
    def test_details_kwargs_are_captured_on_the_exception(self):
        exc = ToolError("exited nonzero", argv=["ls", "-l"], exit_code=2)
        self.assertEqual(exc.details, {"argv": ["ls", "-l"], "exit_code": 2})

    def test_no_details_gives_an_empty_dict_not_none(self):
        exc = ToolError("boom")
        self.assertEqual(exc.details, {})


class CategorizeTests(unittest.TestCase):
    """The single mapping point from any exception to a v1 category."""

    def test_categorize_returns_the_instances_category_for_our_exceptions(self):
        self.assertEqual(categorize(StorageError("boom")), "storage_error")

    def test_categorize_returns_the_overridden_category(self):
        exc = ConfigError("boom", category="reference_missing")
        self.assertEqual(categorize(exc), "reference_missing")

    def test_categorize_returns_internal_error_for_value_error(self):
        self.assertEqual(categorize(ValueError("boom")), "internal_error")

    def test_categorize_returns_internal_error_for_key_error(self):
        self.assertEqual(categorize(KeyError("boom")), "internal_error")

    def test_categorize_returns_internal_error_for_bare_exception(self):
        self.assertEqual(categorize(Exception("boom")), "internal_error")

    def test_categorize_returns_internal_error_for_a_directly_corrupted_category(self):
        # Only reachable by assigning the attribute directly, bypassing the
        # constructor's validation — categorize must not trust it blindly and
        # propagate an invalid value toward the database's foreign key.
        exc = ToolError("boom")
        exc.error_category = "not_a_real_category"
        self.assertEqual(categorize(exc), "internal_error")


class AllowlistShapeTests(unittest.TestCase):
    def test_error_categories_has_exactly_thirteen_entries(self):
        self.assertEqual(len(ERROR_CATEGORIES), 13)

    def test_application_error_categories_has_eleven_entries(self):
        self.assertEqual(len(APPLICATION_ERROR_CATEGORIES), 11)

    def test_reconciler_error_categories_has_two_entries(self):
        self.assertEqual(len(RECONCILER_ERROR_CATEGORIES), 2)

    def test_the_two_subsets_are_disjoint_and_union_to_the_whole(self):
        self.assertEqual(
            APPLICATION_ERROR_CATEGORIES & RECONCILER_ERROR_CATEGORIES,
            frozenset())
        self.assertEqual(
            APPLICATION_ERROR_CATEGORIES | RECONCILER_ERROR_CATEGORIES,
            ERROR_CATEGORIES)

    def test_unclassified_category_is_internal_error(self):
        self.assertEqual(UNCLASSIFIED_CATEGORY, "internal_error")


class AllowlistMatchesAttemptWriterTests(unittest.TestCase):
    """Pins this module's allowlist equal to observability.attempts's copy.

    errors.py's module docstring is explicit that the allowlist here is
    "deliberately duplicated rather than imported from observability.attempts"
    so that `pipeline.runtime` stays importable without pulling in the
    attempt writer. A deliberate duplication is still a second source of
    truth unless something keeps the two pinned together — this test is that
    something. If a category is added, renamed, or removed on one side and
    not the other, this fails immediately instead of the drift surfacing as
    a database constraint violation or a misclassified attempt row.
    """

    def test_allowlist_matches_attempt_writer(self):
        from observability import attempts

        self.assertEqual(ERROR_CATEGORIES, attempts.ERROR_CATEGORIES)
        self.assertEqual(APPLICATION_ERROR_CATEGORIES,
                         attempts.APPLICATION_ERROR_CATEGORIES)
        self.assertEqual(RECONCILER_ERROR_CATEGORIES,
                         attempts.RECONCILER_ERROR_CATEGORIES)


class IsValidCategoryTests(unittest.TestCase):
    def test_application_category_is_valid(self):
        self.assertTrue(is_valid_category("tool_failure"))

    def test_reconciler_category_is_valid(self):
        # is_valid_category accepts reconciler categories even though
        # RuntimeErrorBase refuses to carry one — one shared vocabulary is
        # the point; only the raise-site authorship is restricted.
        self.assertTrue(is_valid_category("scheduler_reclaimed"))

    def test_unknown_category_is_invalid(self):
        self.assertFalse(is_valid_category("not_a_real_category"))

    def test_none_is_invalid(self):
        self.assertFalse(is_valid_category(None))


class SerializeErrorTests(unittest.TestCase):
    def raised(self, exc):
        """Actually raise and catch, so exc.__traceback__ is populated."""
        try:
            raise exc
        except type(exc) as caught:
            return caught

    def test_returns_a_serialized_error_with_category_type_and_message(self):
        exc = self.raised(StorageError("upload failed", bucket="b"))
        result = serialize_error(exc, redactor=None)
        self.assertIsInstance(result, SerializedError)
        self.assertEqual(result.error_category, "storage_error")
        self.assertEqual(result.error_type, "StorageError")
        self.assertEqual(result.message, "upload failed")

    def test_includes_traceback_when_requested_and_available(self):
        exc = self.raised(ToolError("boom"))
        result = serialize_error(exc, include_traceback=True, redactor=None)
        self.assertIsNotNone(result.traceback)
        self.assertIn("ToolError", result.traceback)

    def test_omits_traceback_when_not_requested(self):
        exc = self.raised(ToolError("boom"))
        result = serialize_error(exc, include_traceback=False, redactor=None)
        self.assertIsNone(result.traceback)

    def test_omits_traceback_when_exception_was_never_raised(self):
        # Constructed but not raised: __traceback__ is None, so there is
        # nothing to format even though include_traceback defaults to True.
        exc = ToolError("boom")
        result = serialize_error(exc, redactor=None)
        self.assertIsNone(result.traceback)

    def test_as_dict_shape_without_traceback(self):
        # argv is a str detail value, a JSON-safe scalar, so it passes
        # through unchanged rather than being repr()'d (unlike a list, which
        # is covered separately in SerializeErrorDetailReprTests).
        exc = self.raised(ToolError("boom", argv="ls -l"))
        result = serialize_error(exc, include_traceback=False, redactor=None)
        self.assertEqual(result.as_dict(), {
            "error_category": "tool_failure",
            "error_type": "ToolError",
            "message": "boom",
            "details": {"argv": "ls -l"},
        })

    def test_as_dict_shape_with_traceback(self):
        exc = self.raised(ToolError("boom"))
        result = serialize_error(exc, include_traceback=True, redactor=None)
        out = result.as_dict()
        self.assertIn("traceback", out)
        self.assertEqual(out["traceback"], result.traceback)

    def test_non_runtime_error_base_still_serializes_with_internal_error(self):
        exc = self.raised(ValueError("bad input"))
        result = serialize_error(exc, redactor=None)
        self.assertEqual(result.error_category, "internal_error")
        self.assertEqual(result.error_type, "ValueError")
        self.assertEqual(result.message, "bad input")


class SerializeErrorRedactionTests(unittest.TestCase):
    """The redactor is applied to every piece of free text that could carry a
    secret: the message, each string detail value, and the traceback."""

    def upper_redactor(self, text: str) -> str:
        return text.replace("secret", "***")

    def raised(self, exc):
        try:
            raise exc
        except type(exc) as caught:
            return caught

    def test_redactor_is_applied_to_the_message(self):
        exc = self.raised(ToolError("leaked secret in output"))
        result = serialize_error(exc, redactor=self.upper_redactor)
        self.assertEqual(result.message, "leaked *** in output")

    def test_redactor_is_applied_to_every_string_detail_value(self):
        exc = self.raised(ToolError("boom", argv="run --token secret",
                                    note="also secret here"))
        result = serialize_error(exc, redactor=self.upper_redactor)
        self.assertEqual(result.details["argv"], "run --token ***")
        self.assertEqual(result.details["note"], "also *** here")

    def test_redactor_is_applied_to_the_traceback(self):
        exc = self.raised(ToolError("secret in message"))
        result = serialize_error(exc, include_traceback=True,
                                 redactor=self.upper_redactor)
        self.assertNotIn("secret in message", result.traceback)
        self.assertIn("*** in message", result.traceback)

    def test_no_redactor_leaves_text_unchanged(self):
        exc = self.raised(ToolError("has a secret"))
        result = serialize_error(exc, redactor=None)
        self.assertEqual(result.message, "has a secret")


class SerializeErrorDetailReprTests(unittest.TestCase):
    """Non-JSON-safe detail values are bounded to repr() so a live object in
    `details` cannot make the terminal record unserializable."""

    def raised(self, exc):
        try:
            raise exc
        except type(exc) as caught:
            return caught

    def test_object_detail_is_rendered_with_repr(self):
        class Sentinel:
            def __repr__(self):
                return "<Sentinel object>"

        exc = self.raised(ToolError("boom", payload=Sentinel()))
        result = serialize_error(exc, redactor=None)
        self.assertEqual(result.details["payload"], "<Sentinel object>")

    def test_set_detail_is_rendered_with_repr(self):
        exc = self.raised(ToolError("boom", codes={1, 2, 3}))
        result = serialize_error(exc, redactor=None)
        self.assertEqual(result.details["codes"], repr({1, 2, 3}))

    def test_scalar_details_pass_through_unchanged(self):
        exc = self.raised(ToolError("boom", exit_code=2, retryable=False,
                                    ratio=0.5, note=None))
        result = serialize_error(exc, redactor=None)
        self.assertEqual(result.details["exit_code"], 2)
        self.assertIs(result.details["retryable"], False)
        self.assertEqual(result.details["ratio"], 0.5)
        self.assertIsNone(result.details["note"])

    def test_non_dict_details_attribute_is_tolerated(self):
        # An exception without our .details (e.g. a bare Exception) must not
        # crash serialization; details should come back empty.
        result = serialize_error(ValueError("boom"), redactor=None)
        self.assertEqual(result.details, {})


if __name__ == "__main__":
    unittest.main()
