"""Re-exports :mod:`rapidpipe.selftest.support.fakemaintaindb`.

Moved there so ``rapidpipe selftest --stage maintain`` can import it
inside the pipeline image, which excludes ``tests/`` at build time
(``containers/rapid-pipeline/build.sh``). Kept here, as a thin shim, so
the test suite's imports keep working unchanged.
"""

from __future__ import annotations

from rapidpipe.selftest.support.fakemaintaindb import *  # noqa: F401,F403
