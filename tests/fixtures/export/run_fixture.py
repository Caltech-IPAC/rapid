#!/usr/bin/env python3
"""The export stage's fixture: prepare, run, check. ``make stage-export``.

``export`` is a declared stub (supervisor step 8, 2026-09-24, ruling R9):
a valid invocation exits 69 and publishes no manifest, so "PASS" here
means exactly that, not a completed run -- see
``rapidpipe/selftest/export.py`` and
``rapidpipe/selftest/fixtures/export/expected.json``.

A thin CLI over :mod:`rapidpipe.selftest`, which holds the actual
prepare/run/check logic (also used by ``rapidpipe selftest --stage
export``, the same fixture running inside the pipeline image). The
fixture data this reads (``expected.json``, ``settings.toml``) is the
packaged copy under ``rapidpipe/selftest/fixtures/export/`` -- this
directory holds only this thin reference, not a second copy.

Exit 0 when every check passes, 1 otherwise. The stage runs no external
tool and opens no database connection in this build, so ``--tools``
changes only the label.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent
REPO_ROOT = FIXTURE_DIR.parents[2]
sys.path.insert(0, str(REPO_ROOT))

from rapidpipe.selftest.export import FIXTURE  # noqa: E402
from rapidpipe.selftest.runner import run_fixture  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run_fixture.py", description=__doc__.splitlines()[0])
    parser.add_argument("--tools", choices=("fake", "real"), default="fake")
    parser.add_argument("--workdir", default=None,
                        help="an empty or new directory to prepare the fixture in "
                             "(default: a fresh temporary directory, kept for inspection)")
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args(argv)

    work_dir = Path(args.workdir) if args.workdir else None
    print(f"stage-export: tools={args.tools} "
          f"workdir={work_dir if work_dir else '(fresh temporary directory)'}")
    try:
        result = run_fixture(
            FIXTURE, tools=args.tools, python=args.python, repo_root=REPO_ROOT,
            work_dir=work_dir, output_location=None)
    except FileExistsError as exc:
        print(f"stage-export: {exc}", file=sys.stderr)
        return 1

    for failure in result.checks.failures:
        print(f"stage-export: FAIL {failure}")
    verdict = "PASS" if not result.checks.failures else "FAIL"
    print(f"stage-export: {verdict} ({result.checks.passed} checks passed, "
          f"{len(result.checks.failures)} failed; tools={args.tools}; "
          f"outputs in {result.output_location})")
    return 0 if not result.checks.failures else 1


if __name__ == "__main__":
    sys.exit(main())
