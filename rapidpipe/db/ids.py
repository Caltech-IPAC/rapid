"""Time-ordered identifiers: a stdlib-only ULID generator.

The runs page's "Identifiers" section: runs, attempts and product instances
"receive globally unique, time-ordered identifiers (ULID) before execution
or manifest publication, allocated by whoever creates the row, so the local
runner and the Batch wrapper need no database to allocate; registration
preserves them." The run-model migration's ``rapid_ulid`` domain enforces
the shape this module emits: 26 characters, Crockford base32, uppercase.

This module imports only the standard library, matching ``rapidpipe.db``'s
contract of providing persistence without importing ``rapidpipe.runs`` or
any stage module -- a plain ID generator has no reason to import either.
"""

from __future__ import annotations

import os
import threading
import time

#: Crockford base32: 0-9 and A-Z minus I, L, O, U (avoids visual confusion
#: with 1, 1, 0 and V). 32 symbols, matching the alphabet the migration's
#: ``rapid_ulid`` domain CHECK constraint accepts.
_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

_TIME_LEN = 10   # 48-bit timestamp, base32-encoded, left-padded
_RANDOM_LEN = 16  # 80 bits of randomness, base32-encoded
ULID_LENGTH = _TIME_LEN + _RANDOM_LEN  # 26, matching the domain's length check

_MAX_TIME_MS = (1 << 48) - 1
_MAX_RANDOM = (1 << 80) - 1

_lock = threading.Lock()
_last_time_ms: int | None = None
_last_random: int | None = None


def _encode(value: int, length: int) -> str:
    chars = ["0"] * length
    for i in range(length - 1, -1, -1):
        chars[i] = _CROCKFORD_ALPHABET[value & 0x1F]
        value >>= 5
    return "".join(chars)


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def new_ulid(now_ms: int | None = None) -> str:
    """Return a new 26-character, time-ordered, Crockford-base32 ULID.

    Time-ordered per the runs page: the first ten characters encode a
    48-bit millisecond timestamp, so ULIDs allocated later sort after
    ULIDs allocated earlier by plain string comparison. The remaining
    sixteen characters encode 80 bits from :func:`os.urandom`.

    Within one process, two calls in the same millisecond never collide:
    a monotonic counter (the previous call's random component, plus one)
    replaces fresh randomness when the millisecond has not advanced, and
    still sorts strictly after the previous id. This does not by itself
    guarantee ordering across processes or hosts within the same
    millisecond -- across processes, 80 bits of randomness make a
    collision negligible but ordering within that millisecond is not
    promised, which matches a plain ULID's own guarantees.
    """
    global _last_time_ms, _last_random

    time_ms = _now_ms() if now_ms is None else now_ms
    if time_ms > _MAX_TIME_MS:
        raise OverflowError(f"timestamp {time_ms} exceeds the 48-bit ULID range")

    with _lock:
        if _last_time_ms is not None and time_ms <= _last_time_ms:
            # Same (or, on a clock step backwards, earlier) millisecond as
            # the last id this process minted: increment the random
            # component instead of drawing fresh bits, so this id still
            # sorts strictly after the previous one. Use the previous
            # timestamp so the encoded prefix does not go backwards either.
            time_ms = _last_time_ms
            random_value = (_last_random + 1) & _MAX_RANDOM
            if random_value == 0:
                # 80-bit random component wrapped inside one millisecond:
                # astronomically unlikely at any real call rate, but carry
                # into the timestamp rather than silently reuse an id.
                time_ms = min(time_ms + 1, _MAX_TIME_MS)
        else:
            random_value = int.from_bytes(os.urandom(10), "big") & _MAX_RANDOM

        _last_time_ms = time_ms
        _last_random = random_value

    return _encode(time_ms, _TIME_LEN) + _encode(random_value, _RANDOM_LEN)


def is_valid_ulid(value: str) -> bool:
    """Return whether ``value`` has the shape this module's ids have.

    Uppercase only, matching what :func:`new_ulid` emits and what the
    migration's ``rapid_ulid`` domain accepts -- this module does not
    normalize case, so a lowercase-but-otherwise-valid string is rejected
    rather than silently accepted as equivalent.
    """
    if not isinstance(value, str) or len(value) != ULID_LENGTH:
        return False
    return all(c in _CROCKFORD_ALPHABET for c in value)
