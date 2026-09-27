"""Suite-wide fixtures shared by tests/unit, tests/db and tests/cli.

The launcher reads a unit's input-set manifest before it submits
(``rapidpipe.runs.inputs``; supervisor step 9, 2026-09-25, R4) and refuses
(exit 65) when there is none. Most launcher tests predate that read and
pass a placeholder ``--inputs`` (``s3://d``, ``s3://in/x``) that names no
manifest, because what they test is not the binding. For those, the
reader is stubbed to "a manifest naming no instance": nothing binds and
nothing is refused, exactly the behaviour of a delivery manifest.

A test that exercises the real read -- the binding itself, the refusal,
a retry or a seeded re-run reading back a recorded location -- opts out
with ``@pytest.mark.real_input_manifest`` (tests/cli/test_bind_inputs.py,
tests/unit/test_bind_inputs.py).

Every stage invocation runs the read guard inside ``run_stage``
(``rapidpipe.runs.readguard``; supervisor step 6, 2026-09-26, R6), which
needs the database whenever the input manifest names an instance. The
existing stage tests are database-free, so the guard is stubbed to a
no-op in-process the same way. A test that exercises the real guard
opts out with ``@pytest.mark.readguard`` (tests/unit/test_readguard.py,
tests/cli/test_readguard.py). The stub reaches only this process: a
stage run as a subprocess runs the real guard.
"""

from __future__ import annotations

import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_input_manifest: use the launcher's real input-manifest read "
        "(rapidpipe.runs.inputs.read_input_instances) instead of the suite's stub")
    config.addinivalue_line(
        "markers",
        "readguard: use the stage read guard "
        "(rapidpipe.runs.readguard.assert_inputs_readable) instead of the suite's no-op stub")


@pytest.fixture(autouse=True)
def _stub_input_manifest_read(request, monkeypatch):
    if request.node.get_closest_marker("real_input_manifest") is not None:
        return
    from rapidpipe.runs import inputs as run_inputs

    monkeypatch.setattr(run_inputs, "read_input_instances",
                        lambda inputs_location, *, s3_client=None: [])


@pytest.fixture(autouse=True)
def _stub_stage_read_guard(request, monkeypatch):
    if request.node.get_closest_marker("readguard") is not None:
        return
    from rapidpipe.runs import readguard

    monkeypatch.setattr(readguard, "assert_inputs_readable",
                        lambda manifest, run_id, *, connect=None: None)
