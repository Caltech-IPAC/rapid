"""Tests for rapidpipe.db.diffimages that need no database: the
differencer-to-`pipelines` map.

The functions against a real PostgreSQL (register_difference_image and
its column sources) are in tests/db/test_register_difference.py.
"""

from __future__ import annotations

from rapidpipe.db.diffimages import DIFFERENCER_PPIDS


def test_zogy_registers_under_the_science_pipeline_row():
    assert DIFFERENCER_PPIDS["zogy"] == 15


def test_sfft_registers_under_its_own_pipelines_row():
    # database/migrations/20260924-01-pipelines-sfft.sql (lead ruling
    # 2026-09-24: ppid 16, priority 6).
    assert DIFFERENCER_PPIDS["sfft"] == 16
