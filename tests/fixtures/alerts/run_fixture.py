#!/usr/bin/env python3
"""The alerts stage's fixture: prepare, run, check. ``make stage-alerts``.

Stage contract, "Local execution": "``make stage-<name>`` prepares an
isolated fixture, runs the stage without account credentials, and checks
its products and manifest; provenance fields are validated for shape, not
compared with fixed IDs or paths."

A thin CLI over :mod:`rapidpipe.selftest`, which holds the actual
prepare/run/check logic (also used by ``rapidpipe selftest --stage
load``, the same fixture running inside the pipeline image). The fixture
data this reads (``expected.json``, ``settings.toml``) is the packaged
copy under ``rapidpipe/selftest/fixtures/alerts/`` -- this directory holds
only this thin reference, not a second copy.

Exit 0 when every check passes, 1 otherwise. The same stage against
PostgreSQL (the outbox rows, the registered instances, nalertpackets) is
``tests/db/test_alerts.py``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent
REPO_ROOT = FIXTURE_DIR.parents[2]
sys.path.insert(0, str(REPO_ROOT))

from rapidpipe.selftest.alerts import FIXTURE  # noqa: E402
from rapidpipe.selftest.runner import run_fixture  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run_fixture.py", description=__doc__.splitlines()[0])
    parser.add_argument("--workdir", default=None,
                        help="an empty or new directory to prepare the fixture in "
                             "(default: a fresh temporary directory, kept for inspection)")
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args(argv)

    work_dir = Path(args.workdir) if args.workdir else None
    print(f"stage-alerts: database=fake "
          f"workdir={work_dir if work_dir else '(fresh temporary directory)'}")
    try:
        result = run_fixture(
            FIXTURE, tools="fake", python=args.python, repo_root=REPO_ROOT,
            work_dir=work_dir, output_location=None)
    except FileExistsError as exc:
        print(f"stage-alerts: {exc}", file=sys.stderr)
        return 1

    for failure in result.checks.failures:
        print(f"stage-alerts: FAIL {failure}")
    verdict = "PASS" if not result.checks.failures else "FAIL"
    print(f"stage-alerts: {verdict} ({result.checks.passed} checks passed, "
          f"{len(result.checks.failures)} failed; outputs in {result.output_location})")
    return 0 if not result.checks.failures else 1


if __name__ == "__main__":
    sys.exit(main())
