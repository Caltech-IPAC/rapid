"""``rapidpipe selftest``: run a stage's own packaged fixture, in place.

Ruling (Ben, 2026-09-23): the stage fixtures become runnable inside the
pipeline image on AWS Batch through the fixed entrypoint (``python -m
rapidpipe.cli.main``), as ``rapidpipe selftest --stage difference|load|maintain|alerts
[--real-tools] [--work-dir DIR] [--output-location s3://... or path]``.
The fixture gate then runs as a submitted Batch job whose execution
record is the evidence -- no separate test harness needs installing
inside the image.

:mod:`rapidpipe.selftest.runner` holds the prepare/run/check machinery
shared with ``make stage-<name>`` (``tests/fixtures/<stage>/
run_fixture.py``, now a thin CLI over the same code);
:mod:`rapidpipe.selftest.difference` and :mod:`rapidpipe.selftest.load`
supply each stage's own hooks and packaged fixture data
(``rapidpipe/selftest/fixtures/<stage>/``, shipped inside the pipeline
image, unlike ``tests/`` -- ``containers/rapid-pipeline/build.sh``).

:func:`run` is this package's one entry point: it prepares and runs the
named stage's fixture and prints its report, returning the exit code
``rapidpipe.cli.main`` should use -- 0 on a full pass, 1 on a fixture
mismatch (the stage ran, but its manifest or products didn't match
``expected.json``), and the stage's own subprocess exit code when that
itself was not what the fixture expected (a stage failure, not a fixture
mismatch: something the stage contract's own exit codes already
classify, so re-using them here needs no second vocabulary).
"""

from __future__ import annotations

import sys
from pathlib import Path

from rapidpipe.selftest.runner import FixtureResult, STAGE_NAMES, run_fixture

REPO_ROOT = Path(__file__).resolve().parents[2]


def _fixture_module(stage: str):
    if stage == "difference":
        from rapidpipe.selftest import difference as module
    elif stage == "load":
        from rapidpipe.selftest import load as module
    elif stage == "maintain":
        from rapidpipe.selftest import maintain as module
    elif stage == "crossmatch":
        from rapidpipe.selftest import crossmatch as module
    elif stage == "alerts":
        from rapidpipe.selftest import alerts as module
    else:
        raise ValueError(f"no such stage fixture: {stage!r}; known: {', '.join(STAGE_NAMES)}")
    return module


def run(*, stage: str, real_tools: bool, work_dir: str | None, output_location: str | None,
        python: str = sys.executable) -> int:
    """Run ``stage``'s packaged fixture; print its report; return the exit code.

    ``real_tools`` selects the pipeline image's own tools/database instead
    of the packaged fakes -- only meaningful inside that image (or a
    checkout with the same tools on ``PATH``). ``work_dir`` is an empty or
    new directory to prepare the fixture in (default: a fresh temporary
    one). ``output_location`` names where the outputs end up after the
    fixture passes or fails (default: ``<work_dir>/outputs``); an
    ``s3://`` location is uploaded to only after every check has already
    run against a local copy -- see :mod:`rapidpipe.selftest.runner`'s
    docstring, and :func:`rapidpipe.selftest.runner.run_fixture`.
    """
    module = _fixture_module(stage)
    tools = "real" if real_tools else "fake"
    work_dir_path = Path(work_dir) if work_dir else None

    print(f"selftest: stage={stage} tools={tools} "
          f"work-dir={work_dir_path if work_dir_path else '(fresh temporary directory)'}")
    try:
        result = run_fixture(
            module.FIXTURE, tools=tools, python=python, repo_root=REPO_ROOT,
            work_dir=work_dir_path, output_location=output_location)
    except FileExistsError as exc:
        print(f"selftest: {exc}", file=sys.stderr)
        return 1

    return _report(stage, result)


def _report(stage: str, result: FixtureResult) -> int:
    for failure in result.checks.failures:
        print(f"selftest: FAIL {failure}")

    expected_exit_matched = not any(f.startswith("exit code:") for f in result.checks.failures)
    if not expected_exit_matched:
        # The stage itself did not exit as the fixture expects: a stage
        # failure, not a fixture mismatch -- propagate its own exit code
        # so the caller (a Batch job, or a developer at a shell) sees the
        # same signal `rapidpipe stage <name>` itself would have given.
        print(f"selftest: {stage}: STAGE FAILURE (exit {result.exit_code}); "
              f"work dir {result.work_dir}")
        return result.exit_code

    verdict = "PASS" if not result.checks.failures else "FAIL"
    location_note = (f"outputs checked in {result.output_location}, uploaded to "
                      f"{result.uploaded_to}" if result.uploaded_to
                      else f"outputs in {result.output_location}")
    print(f"selftest: {stage}: {verdict} ({result.checks.passed} checks passed, "
          f"{len(result.checks.failures)} failed; tools={result.tools}; {location_note})")
    return 0 if not result.checks.failures else 1
