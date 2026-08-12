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
from submission.manifest import Manifest, ProcessingUnit
from submission.routes import JOB_TYPE_SCIENCE
from submission.startup import (PIPELINE_PARAMETER_PATH, ParameterFetchError,
                                configuration_digest, fetch_parameters,
                                resolve_job_context)
from submission.test import payload_fixtures as fixtures


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


def manifest_of(count=18, batch_id="b-1"):
    return Manifest(
        [ProcessingUnit(payload=fixtures.science_payload(exposure=90210,
                                                          sca=i + 1))
         for i in range(count)], batch_id=batch_id)


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


# ---------------------------------------------------------------------------
# The startup sequence
# ---------------------------------------------------------------------------

def test_a_child_resolves_its_own_unit():
    manifest = manifest_of(18)
    context = resolve_job_context(
        environ={"AWS_BATCH_JOB_ARRAY_INDEX": "5"},
        manifest=manifest, parameters=CONFIG)
    assert context.array_index == 5
    assert context.unit.sca == 6


def test_a_plain_job_resolves_index_zero():
    # The single-unit batch case: no AWS_BATCH_JOB_ARRAY_INDEX is set.
    context = resolve_job_context(environ={}, manifest=manifest_of(1),
                                  parameters=CONFIG)
    assert context.array_index == 0
    assert context.unit.sca == 1


def test_every_child_of_one_array_shares_one_config_digest():
    # Identifiers must NOT be folded into the digest: identical
    # configuration has to digest identically across the whole array.
    manifest = manifest_of(18)
    digests = {
        resolve_job_context(environ={"AWS_BATCH_JOB_ARRAY_INDEX": str(i)},
                            manifest=manifest, parameters=CONFIG).config_digest
        for i in range(18)
    }
    assert len(digests) == 1


def test_the_digest_matches_a_direct_digest_of_the_tree():
    context = resolve_job_context(environ={}, manifest=manifest_of(1),
                                  parameters=CONFIG)
    assert context.config_digest == configuration_digest(CONFIG)


def test_parameters_are_fetched_when_not_supplied():
    ssm = FakeSsm(TREE)
    context = resolve_job_context(environ={}, manifest=manifest_of(1),
                                  ssm_client=ssm)
    assert context.parameters["kafka/topic"] == "rapid.internal.alerts.v1"


def test_the_manifest_is_loaded_from_its_uri_when_not_supplied():
    manifest = manifest_of(4)
    loaded = []

    def loader(uri):
        loaded.append(uri)
        return manifest.to_json().encode()

    context = resolve_job_context(
        environ={"RAPID_MANIFEST_URI": "s3://bucket/m.json",
                 "AWS_BATCH_JOB_ARRAY_INDEX": "2"},
        manifest_loader=loader, parameters=CONFIG)
    assert loaded == ["s3://bucket/m.json"]
    assert context.unit.sca == 3


def test_a_checksum_mismatch_stops_the_job():
    # The child would otherwise process the wrong SCA: its index was
    # bound by a different manifest than the one it just read.
    with pytest.raises(ValueError, match="checksum mismatch"):
        resolve_job_context(
            environ={"AWS_BATCH_JOB_ARRAY_INDEX": "0",
                     "RAPID_MANIFEST_CHECKSUM": "0" * 64},
            manifest=manifest_of(4), parameters=CONFIG)


def test_a_matching_checksum_passes():
    manifest = manifest_of(4)
    context = resolve_job_context(
        environ={"AWS_BATCH_JOB_ARRAY_INDEX": "0",
                 "RAPID_MANIFEST_CHECKSUM": manifest.checksum()},
        manifest=manifest, parameters=CONFIG)
    assert context.unit.sca == 1


def test_an_index_beyond_the_manifest_stops_the_job():
    with pytest.raises(IndexError):
        resolve_job_context(environ={"AWS_BATCH_JOB_ARRAY_INDEX": "99"},
                            manifest=manifest_of(4), parameters=CONFIG)


def test_a_missing_manifest_uri_is_an_error():
    with pytest.raises(ValueError, match="RAPID_MANIFEST_URI"):
        resolve_job_context(environ={}, parameters=CONFIG)


# ---------------------------------------------------------------------------
# Reading configuration back out
# ---------------------------------------------------------------------------

def test_parameter_reads_a_value():
    context = resolve_job_context(environ={}, manifest=manifest_of(1),
                                  parameters=CONFIG)
    assert context.parameter("kafka/topic") == "rapid.internal.alerts.v1"


def test_a_missing_parameter_without_a_default_raises():
    context = resolve_job_context(environ={}, manifest=manifest_of(1),
                                  parameters=CONFIG)
    with pytest.raises(KeyError, match="not in the pipeline parameter tree"):
        context.parameter("kafka/nonexistent")


def test_a_default_covers_a_missing_parameter():
    context = resolve_job_context(environ={}, manifest=manifest_of(1),
                                  parameters=CONFIG)
    assert context.parameter("kafka/nonexistent", "fallback") == "fallback"
