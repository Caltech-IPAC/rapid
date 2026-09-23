"""Re-exports :mod:`rapidpipe.selftest.support.fakedifftools`.

Moved there so ``rapidpipe selftest --stage difference`` can import it
inside the pipeline image, which excludes ``tests/`` at build time
(``containers/rapid-pipeline/build.sh``). Kept here, as a thin shim, so
the test suite's imports and ``RAPIDPIPE_DIFFERENCE_TOOLKIT=tests.unit.
fakedifftools:fake_toolkit`` keep working unchanged.
"""

from __future__ import annotations

from rapidpipe.selftest.support.fakedifftools import *  # noqa: F401,F403
from rapidpipe.selftest.support.fakedifftools import _gaussian, _l2_registration  # noqa: F401
