"""
File:    test_environment.py

Tests for the per-invocation environment contract.

Every test passes an explicit `env` dict to `read_environment` rather than
mutating `os.environ` — the seam the module provides exists precisely so the
contract is assertable without touching process-global state.
"""

import dataclasses
import unittest

from pipeline.runtime.environment import (
    ENV_ARRAY_INDEX,
    ENV_BATCH_ID,
    ENV_JOB_ATTEMPT,
    ENV_JOB_ID,
    ENV_JQ_NAME,
    ENV_MANIFEST_CHECKSUM,
    ENV_MANIFEST_URI,
    JobEnvironment,
    describe,
    read_environment,
    redacting_environ,
    resolve_region,
)
from pipeline.runtime.errors import ConfigError


def complete_env(**overrides) -> dict:
    """A complete, valid per-invocation environment. Tests override just the
    variable(s) under test rather than restating the whole contract."""
    env = {
        ENV_MANIFEST_URI: "s3://rapid-bucket/manifests/run-1.json",
        ENV_BATCH_ID: "batch-9",
        ENV_MANIFEST_CHECKSUM: "sha256:abcdef",
        ENV_JOB_ID: "job-abc",
        ENV_JOB_ATTEMPT: "1",
        ENV_JQ_NAME: "rapid-queue",
    }
    env.update(overrides)
    return env


class ReadEnvironmentSuccessTests(unittest.TestCase):
    def test_complete_env_parses_every_field(self):
        result = read_environment(complete_env())
        self.assertEqual(result.manifest_uri,
                         "s3://rapid-bucket/manifests/run-1.json")
        self.assertEqual(result.batch_id, "batch-9")
        self.assertEqual(result.manifest_checksum, "sha256:abcdef")
        self.assertEqual(result.scheduler_job_id, "job-abc")
        self.assertEqual(result.attempt_index, 1)
        self.assertEqual(result.queue_name, "rapid-queue")
        self.assertIsNone(result.array_index)

    def test_values_are_stripped_of_surrounding_whitespace(self):
        result = read_environment(complete_env(**{
            ENV_BATCH_ID: "  batch-9  ",
        }))
        self.assertEqual(result.batch_id, "batch-9")

    def test_returns_a_job_environment_instance(self):
        self.assertIsInstance(read_environment(complete_env()), JobEnvironment)


class MissingRequiredVariableTests(unittest.TestCase):
    def test_a_single_missing_variable_raises_config_error(self):
        env = complete_env()
        del env[ENV_BATCH_ID]
        with self.assertRaises(ConfigError):
            read_environment(env)

    def test_missing_variable_is_named_in_the_message(self):
        env = complete_env()
        del env[ENV_BATCH_ID]
        with self.assertRaises(ConfigError) as ctx:
            read_environment(env)
        self.assertIn(ENV_BATCH_ID, str(ctx.exception))

    def test_two_missing_variables_are_both_named_in_one_message(self):
        # All missing variables are reported together so a misconfigured job
        # definition is fixed in one submit-fail-fix cycle, not three.
        env = complete_env()
        del env[ENV_BATCH_ID]
        del env[ENV_MANIFEST_CHECKSUM]
        with self.assertRaises(ConfigError) as ctx:
            read_environment(env)
        message = str(ctx.exception)
        self.assertIn(ENV_BATCH_ID, message)
        self.assertIn(ENV_MANIFEST_CHECKSUM, message)

    def test_empty_string_value_counts_as_missing(self):
        env = complete_env(**{ENV_MANIFEST_URI: ""})
        with self.assertRaises(ConfigError) as ctx:
            read_environment(env)
        self.assertIn(ENV_MANIFEST_URI, str(ctx.exception))

    def test_whitespace_only_value_counts_as_missing(self):
        env = complete_env(**{ENV_MANIFEST_URI: "   "})
        with self.assertRaises(ConfigError) as ctx:
            read_environment(env)
        self.assertIn(ENV_MANIFEST_URI, str(ctx.exception))

    def test_all_required_variables_missing_are_all_named(self):
        with self.assertRaises(ConfigError) as ctx:
            read_environment({})
        message = str(ctx.exception)
        for name in (ENV_MANIFEST_URI, ENV_BATCH_ID, ENV_MANIFEST_CHECKSUM,
                    ENV_JOB_ID, ENV_JOB_ATTEMPT, ENV_JQ_NAME):
            self.assertIn(name, message)


class AttemptIndexTests(unittest.TestCase):
    def test_attempt_one_is_accepted(self):
        result = read_environment(complete_env(**{ENV_JOB_ATTEMPT: "1"}))
        self.assertEqual(result.attempt_index, 1)

    def test_attempt_zero_is_rejected(self):
        # Batch numbers attempts from 1; a zero index would key the attempt
        # resolver to the wrong row.
        with self.assertRaises(ConfigError):
            read_environment(complete_env(**{ENV_JOB_ATTEMPT: "0"}))

    def test_negative_attempt_is_rejected(self):
        with self.assertRaises(ConfigError):
            read_environment(complete_env(**{ENV_JOB_ATTEMPT: "-1"}))

    def test_non_integer_attempt_is_rejected(self):
        with self.assertRaises(ConfigError):
            read_environment(complete_env(**{ENV_JOB_ATTEMPT: "abc"}))


class ArrayIndexTests(unittest.TestCase):
    """AWS_BATCH_JOB_ARRAY_INDEX is the one conditional variable: absence and
    zero are different facts (not an array child vs. array child zero), so
    they must never collapse into each other."""

    def test_absent_array_index_gives_none_and_not_array_child(self):
        result = read_environment(complete_env())
        self.assertIsNone(result.array_index)
        self.assertFalse(result.is_array_child)

    def test_array_index_zero_gives_zero_and_is_array_child(self):
        # Zero is a real array index, not an absence — defaulting an absent
        # value to 0 would make every non-array job claim to be array child
        # zero, which is exactly the bug this distinction exists to prevent.
        result = read_environment(complete_env(**{ENV_ARRAY_INDEX: "0"}))
        self.assertEqual(result.array_index, 0)
        self.assertTrue(result.is_array_child)

    def test_array_index_five_is_parsed(self):
        result = read_environment(complete_env(**{ENV_ARRAY_INDEX: "5"}))
        self.assertEqual(result.array_index, 5)
        self.assertTrue(result.is_array_child)

    def test_negative_array_index_is_rejected(self):
        with self.assertRaises(ConfigError):
            read_environment(complete_env(**{ENV_ARRAY_INDEX: "-1"}))

    def test_non_integer_array_index_is_rejected(self):
        with self.assertRaises(ConfigError):
            read_environment(complete_env(**{ENV_ARRAY_INDEX: "x"}))

    def test_empty_string_array_index_is_treated_as_absent(self):
        # Unlike the required variables, an empty optional variable is a
        # legitimate "not set", not a validation failure.
        result = read_environment(complete_env(**{ENV_ARRAY_INDEX: ""}))
        self.assertIsNone(result.array_index)
        self.assertFalse(result.is_array_child)


class AttemptKeyTests(unittest.TestCase):
    def test_attempt_key_contains_scheduler_job_id_and_attempt_index(self):
        result = read_environment(complete_env(
            **{ENV_JOB_ID: "job-abc", ENV_JOB_ATTEMPT: "3"}))
        self.assertIn("job-abc", result.attempt_key)
        self.assertIn("3", result.attempt_key)

    def test_attempt_key_is_stable_for_the_same_inputs(self):
        result = read_environment(complete_env())
        self.assertEqual(result.attempt_key, result.attempt_key)


class AsDictTests(unittest.TestCase):
    def test_includes_the_six_always_present_fields(self):
        result = read_environment(complete_env())
        out = result.as_dict()
        for key in ("manifest_uri", "batch_id", "manifest_checksum",
                    "scheduler_job_id", "attempt_index", "queue_name"):
            self.assertIn(key, out)

    def test_array_index_absent_when_not_an_array_child(self):
        result = read_environment(complete_env())
        self.assertNotIn("array_index", result.as_dict())

    def test_array_index_present_when_an_array_child(self):
        result = read_environment(complete_env(**{ENV_ARRAY_INDEX: "2"}))
        self.assertEqual(result.as_dict()["array_index"], 2)

    def test_as_dict_has_no_full_environment_dump(self):
        # The observability policy prohibits a full environment dump in
        # provenance/logs; as_dict exists so there is an obvious right thing
        # to log instead of dict(os.environ).
        result = read_environment(complete_env())
        out = result.as_dict()
        self.assertNotIn("os.environ", str(out))
        expected_keys = {"manifest_uri", "batch_id", "manifest_checksum",
                         "scheduler_job_id", "attempt_index", "queue_name",
                         "array_index"}
        self.assertTrue(set(out.keys()).issubset(expected_keys))


class DescribeTests(unittest.TestCase):
    def test_describe_is_one_line(self):
        result = read_environment(complete_env())
        rendered = describe(result)
        self.assertNotIn("\n", rendered)

    def test_describe_contains_job_attempt_and_queue(self):
        result = read_environment(complete_env(
            **{ENV_JOB_ID: "job-abc", ENV_JOB_ATTEMPT: "2",
               ENV_JQ_NAME: "rapid-queue"}))
        rendered = describe(result)
        self.assertIn("job-abc", rendered)
        self.assertIn("2", rendered)
        self.assertIn("rapid-queue", rendered)


class RedactingEnvironTests(unittest.TestCase):
    def test_sensitive_named_values_are_masked(self):
        env = {"RAPID_DB_PASSWORD": "hunter2", "SOME_TOKEN": "abcdef123456"}
        result = redacting_environ(env)
        self.assertNotEqual(result["RAPID_DB_PASSWORD"], "hunter2")
        self.assertNotEqual(result["SOME_TOKEN"], "abcdef123456")

    def test_non_sensitive_values_are_left_intact(self):
        env = {"RAPID_BATCH_ID": "batch-9", "AWS_BATCH_JQ_NAME": "rapid-queue"}
        result = redacting_environ(env)
        self.assertEqual(result["RAPID_BATCH_ID"], "batch-9")
        self.assertEqual(result["AWS_BATCH_JQ_NAME"], "rapid-queue")

    def test_mixed_environment_masks_only_the_sensitive_entries(self):
        env = {"RAPID_BATCH_ID": "batch-9", "API_KEY": "supersecretvalue"}
        result = redacting_environ(env)
        self.assertEqual(result["RAPID_BATCH_ID"], "batch-9")
        self.assertNotEqual(result["API_KEY"], "supersecretvalue")


class ResolveRegionTests(unittest.TestCase):
    """The policy's order, and the raise at the end of it.

    The pattern this replaces was `os.environ.get("AWS_DEFAULT_REGION",
    "us-east-1")`: in an account deployed anywhere else it reconciled
    against a region holding none of its work and reported nothing wrong.
    """

    def test_aws_region_wins(self):
        env = {"AWS_REGION": "us-west-2", "AWS_DEFAULT_REGION": "us-east-1"}
        self.assertEqual(resolve_region(env, session_region="eu-west-1"),
                         "us-west-2")

    def test_default_region_is_the_second_choice(self):
        env = {"AWS_DEFAULT_REGION": "us-east-2"}
        self.assertEqual(resolve_region(env, session_region="eu-west-1"),
                         "us-east-2")

    def test_the_sdk_session_is_the_third(self):
        self.assertEqual(resolve_region({}, session_region="ap-south-1"),
                         "ap-south-1")

    def test_an_empty_value_does_not_count_as_set(self):
        env = {"AWS_REGION": "   ", "AWS_DEFAULT_REGION": "us-east-1"}
        self.assertEqual(resolve_region(env, session_region="eu-west-1"),
                         "us-east-1")

    def test_nothing_anywhere_raises_rather_than_defaulting(self):
        # `session_region=""` is "the SDK was consulted and had none"; None
        # would mean "consult it", which would reach boto3 from a unit test.
        with self.assertRaises(ConfigError) as caught:
            resolve_region({}, session_region="")
        # The variable is named, so the operator knows what to set.
        self.assertIn("AWS_REGION", str(caught.exception))

    def test_no_us_east_1_is_ever_substituted(self):
        # The specific defect: a hardcoded region that made a
        # misconfiguration look like a healthy service.
        with self.assertRaises(ConfigError) as caught:
            resolve_region({}, session_region="")
        self.assertNotIn("us-east-1", str(caught.exception))


class JobEnvironmentFrozenTests(unittest.TestCase):
    def test_assigning_a_field_raises(self):
        result = read_environment(complete_env())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.batch_id = "different"


if __name__ == "__main__":
    unittest.main()
