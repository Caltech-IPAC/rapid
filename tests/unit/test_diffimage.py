"""Tests for rapidpipe.products.diffimage."""

from __future__ import annotations

from rapidpipe.products.diffimage import CATALOG_OUTCOME_BITS, catalog_outcome_bit


def test_catalog_outcome_bits_match_dev_docstring():
    # dev's awsBatchSubmitJobs_runSingleSciencePipeline.py module docstring.
    assert catalog_outcome_bit("zogy", "positive") == 1
    assert catalog_outcome_bit("zogy", "negative") == 2
    assert catalog_outcome_bit("sfft", "positive") == 4
    assert catalog_outcome_bit("sfft", "negative") == 8
    assert catalog_outcome_bit("naive", "positive") == 16
    assert catalog_outcome_bit("naive", "negative") == 32
    assert len(CATALOG_OUTCOME_BITS) == 6
