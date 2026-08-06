"""Registration as a consumer of reconciled outcomes.

What this replaces: four scripts (2,850 lines) that decided a job's fate by
downloading its stdout log from S3 and regex-grepping `terminating_exitcode`
out of it, wrote `.done` sentinel files on failure paths as well as success
ones, and hardcoded their own process exit to 0 so a failed registration run
looked identical to a clean one.

What it does instead: queries the attempt table for attempts the *reconciler*
has closed, asks the adopted taxonomy whether each one's products should be
registered, and exits with a code that reflects its own failures.

Three properties, each the direct inverse of a defect in what it replaces:

- **It reads records, never logs.** The attempt record is the sole
  machine-readable account of what a job did. No stdout is fetched, no text is
  parsed, and no sentinel file is written or read.
- **It waits for reconciliation.** An attempt is a candidate only once the
  reconciler has moved it to a terminal state AND published its closure
  record. A sequence-0 record whose scheduler truth is still pending is not
  acted on — that is what stops a container killed after writing its record
  from having its products registered.
- **Its exit code is real.** The g0001 counting pattern: failures are counted
  and the process exits nonzero when any occurred. A registration run that
  could not do its job says so.

Supersession is handled by the same query rather than by a special case: a
later record at a higher sequence changes the row the query reads, so the
attempt reappears as a candidate and is reprocessed.
"""

from .consumer import (
    EXIT_FAILURES,
    EXIT_OK,
    RegistrationRun,
    candidates,
    register_batch,
)

__all__ = [
    "EXIT_FAILURES",
    "EXIT_OK",
    "RegistrationRun",
    "candidates",
    "register_batch",
]
