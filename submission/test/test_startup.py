"""Startup tests: parameter fetch, configuration digest, self-resolution.

The digest is the load-bearing assertion set. It goes into every attempt
record and is what makes a product's configuration provenance checkable,
so the tests pin the properties provenance depends on: same configuration
gives the same digest regardless of read order, any value change changes
it, and per-invocation identifiers stay out of it.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from submission import payloads
from submission.routes import JOB_TYPE_SCIENCE
from submission.startup import (PIPELINE_PARAMETER_PATH, ParameterFetchError,
                                configuration_digest, fetch_parameters)


class FakeSsm:
    """Paginating SSM stand-in over a name -> value dict."""

    def __init__(self, values, page_size=10, error=None):
        self.values = values
        self.page_size = page_size
        self.error = error
        self.calls = []

    def get_parameters_by_path(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        items = sorted(self.values.items())
        start = int(kwargs.get("NextToken", 0))
        page = items[start:start + self.page_size]
        response = {"Parameters": [{"Name": n, "Value": v} for n, v in page]}
        if start + self.page_size < len(items):
            response["NextToken"] = str(start + self.page_size)
        return response


# As Parameter Store returns them: absolute names. fetch_parameters()
# relativizes to the tree root.
TREE = {
    "/rapid/pipeline/kafka/topic": "rapid.internal.alerts.v1",
    "/rapid/pipeline/kafka/bootstrap-servers": "b-1.example:9098",
    "/rapid/pipeline/s3/products-bucket": "roman-rapid-products",
    "/rapid/pipeline/science/min-images-to-coadd": "3",
}

# As a job sees them after the fetch: relative. This is what a
# pre-fetched `parameters=` argument carries, so the startup tests use it.
CONFIG = {name.removeprefix("/rapid/pipeline/"): value
          for name, value in TREE.items()}


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def test_fetch_reads_the_pipeline_tree():
    params = fetch_parameters(client=FakeSsm(TREE))
    assert params["kafka/topic"] == "rapid.internal.alerts.v1"
    assert params["science/min-images-to-coadd"] == "3"


def test_names_are_relative_to_the_tree_root():
    # The root is a deployment detail; consumers name parameters relative
    # to it so a tree move is not a code change.
    params = fetch_parameters(client=FakeSsm(TREE))
    assert all(not n.startswith("/") for n in params)


def test_fetch_is_recursive_over_the_right_path():
    ssm = FakeSsm(TREE)
    fetch_parameters(client=ssm)
    assert ssm.calls[0]["Path"] == PIPELINE_PARAMETER_PATH
    assert ssm.calls[0]["Recursive"] is True


def test_fetch_paginates():
    ssm = FakeSsm(TREE, page_size=2)
    assert len(fetch_parameters(client=ssm)) == len(TREE)
    assert len(ssm.calls) > 1


def test_an_empty_tree_is_an_error_not_an_empty_config():
    # A job must not run on defaults: its configuration digest would
    # describe configuration it never used.
    with pytest.raises(ParameterFetchError, match="empty"):
        fetch_parameters(client=FakeSsm({}))


def test_a_failed_read_is_an_error():
    ssm = FakeSsm(TREE, error=RuntimeError("AccessDenied"))
    with pytest.raises(ParameterFetchError, match="could not read"):
        fetch_parameters(client=ssm)


# ---------------------------------------------------------------------------
# D9: the default SSM client carries adaptive retry, >= 10 attempts.
#
# THE REGRESSION GUARD for the 2026-09-10 measurement: a 1,000-job start
# burst had 218 jobs die because this call built its client with no retry
# Config at all and SSM throttled the herd. Proven by reverting the
# `config=` kwarg below and watching this test fail -- see
# LEDGER-rusage-backoff.md for the revert demonstration.
# ---------------------------------------------------------------------------

def test_the_default_ssm_client_carries_adaptive_retry_with_at_least_10_attempts():
    from unittest import mock

    captured = {}

    class _RecordingBoto3:
        @staticmethod
        def client(service, **kwargs):
            captured["service"] = service
            captured["kwargs"] = kwargs
            return FakeSsm(TREE)

    with mock.patch.dict(sys.modules, {"boto3": _RecordingBoto3}):
        fetch_parameters()

    assert captured["service"] == "ssm"
    config = captured["kwargs"].get("config")
    assert config is not None, (
        "fetch_parameters() must construct its default SSM client with a "
        "botocore retries Config -- none was passed")
    retries = config.retries
    assert retries.get("mode") == "adaptive"
    assert retries.get("max_attempts", 0) >= 10


# ---------------------------------------------------------------------------
# Digest
# ---------------------------------------------------------------------------

def test_digest_is_order_independent():
    forward = {"a": "1", "b": "2", "c": "3"}
    backward = {"c": "3", "b": "2", "a": "1"}
    assert configuration_digest(forward) == configuration_digest(backward)


def test_digest_changes_when_a_value_changes():
    before = configuration_digest({"a": "1"})
    assert configuration_digest({"a": "2"}) != before


def test_digest_changes_when_a_parameter_is_added():
    before = configuration_digest({"a": "1"})
    assert configuration_digest({"a": "1", "b": "2"}) != before


def test_digest_is_a_sha256_hex():
    digest = configuration_digest({"a": "1"})
    assert len(digest) == 64 and int(digest, 16) >= 0


def test_digest_is_reproducible_across_calls():
    assert configuration_digest(TREE) == configuration_digest(dict(TREE))
