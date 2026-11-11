"""A publisher's wait between tries of its fields' locks stays within the cap at any try count.

Run 229's publishers retried for about 85 minutes behind held field locks; at try 1,024
``0.5 * 2 ** tries`` overflowed a float and failed 945 finished differences.
"""

from __future__ import annotations

import importlib

# The package re-exports the function ``publish`` under the module's name.
publisher = importlib.import_module("rapidpipe.foundation.publish")


def test_the_wait_stays_within_the_cap_at_any_try():
    for tries in (0, 1, 5, 1023, 1024, 100_000):
        assert 0 <= publisher.lock_wait(tries) <= publisher.LOCK_RETRY_CAP_S


def test_the_first_wait_is_within_the_initial_wait():
    assert all(publisher.lock_wait(0) <= publisher.LOCK_RETRY_INITIAL_S for _ in range(100))
