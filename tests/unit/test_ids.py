"""Unit tests for rapidpipe.db.ids: pure, no database, no network."""

from __future__ import annotations

import time

from rapidpipe.db.ids import ULID_LENGTH, is_valid_ulid, new_ulid


def test_new_ulid_has_the_domain_shape():
    value = new_ulid()
    assert isinstance(value, str)
    assert len(value) == ULID_LENGTH == 26
    assert is_valid_ulid(value)


def test_new_ulid_uses_only_crockford_base32_uppercase():
    value = new_ulid()
    assert value == value.upper()
    for excluded in "ILOU":
        assert excluded not in value
    for c in value:
        assert c in "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def test_new_ulid_is_unique_across_many_calls():
    values = {new_ulid() for _ in range(5000)}
    assert len(values) == 5000


def test_new_ulid_sorts_monotonically_when_called_in_a_tight_loop():
    # Many calls land in the same millisecond; the monotonic-random-bump
    # path in new_ulid() must still produce a strictly increasing sequence
    # under plain string comparison.
    values = [new_ulid() for _ in range(2000)]
    assert values == sorted(values)
    assert len(set(values)) == len(values)


def test_new_ulid_time_prefix_is_time_ordered_across_a_real_gap():
    first = new_ulid()
    time.sleep(0.01)
    second = new_ulid()
    assert first < second
    assert first[:10] <= second[:10]


def test_new_ulid_accepts_an_explicit_timestamp():
    # A fresh module state, so the monotonic clock-step-backwards path
    # (which pins the encoded prefix to the last call's timestamp, by
    # design -- see new_ulid()'s docstring) does not mask the requested
    # now_ms with whatever real wall-clock time an earlier test call used.
    import rapidpipe.db.ids as ids_module

    ids_module._last_time_ms = None
    ids_module._last_random = None
    value = new_ulid(now_ms=0)
    assert value.startswith("0" * 10)
    assert is_valid_ulid(value)


def test_new_ulid_rejects_a_timestamp_past_the_48_bit_range():
    import pytest

    with pytest.raises(OverflowError):
        new_ulid(now_ms=(1 << 48))


def test_is_valid_ulid_rejects_wrong_length():
    assert not is_valid_ulid("TOO-SHORT")
    assert not is_valid_ulid("0" * 25)
    assert not is_valid_ulid("0" * 27)


def test_is_valid_ulid_rejects_excluded_letters():
    # I, L, O, U are not in the Crockford alphabet this module uses.
    base = "0" * 25
    for excluded in "ILOU":
        assert not is_valid_ulid(base + excluded)


def test_is_valid_ulid_rejects_lowercase():
    value = new_ulid()
    assert not is_valid_ulid(value.lower())


def test_is_valid_ulid_rejects_non_string():
    assert not is_valid_ulid(12345)
    assert not is_valid_ulid(None)
