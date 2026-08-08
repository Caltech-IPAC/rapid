"""The operator's registration step: item-level abort, not invocation-level.

THE GRANULARITY EVIDENCE. The old operator ended each of its three
registration steps with

    if reg_run.failed:
        print(f"*** Error: {reg_run.failed} registration(s) failed; quitting")
        dbh.close()
        exit(65)

so ONE attempt whose record could not be registered ended the whole
invocation — and the operator is a loop, so it ended every subsequent
pass too. Fourteen records in that state blocked every operator pass
during the smoke run: not fourteen failures, one failure repeated
because nothing could get past them to the work behind.

The consumer underneath was already right. `register_batch` wraps each
attempt in its own transaction and its own `except`, counts the failure,
and carries on to the next row — "one attempt whose record is incomplete
must not roll back the registrations of the attempts before it". The
operator then threw that away by treating a nonzero `failed` count as
fatal to the invocation.

So this module does not reimplement registration — `catalog.md`
§ Promotion owns the ordering and the outcome contract, and
`pipeline.registration.consumer` implements it. What it does is give the
operator a pass-level *verdict* that distinguishes three states the old
exit(65) collapsed into one:

* nothing failed                        -> OK
* some failed, some succeeded or were   -> PARTIAL
  skipped/deferred
* everything that could be attempted    -> TOTAL
  failed

Partial and total are different operator responses — partial means the
pipeline is moving and some records need triage, total means something
systemic — so they get different exit codes and neither stops the pass.
"""

import logging

logger = logging.getLogger("rapid.operator.registration")

#: The pass did its job.
EXIT_OK = 0
#: Some items failed, others did not: work is moving, triage the failures.
#: Distinct from total by design — the acceptance contract asks for
#: "partial failure distinctly from total".
EXIT_PARTIAL = 66
#: Every attemptable item failed: systemic, and the operator should say so
#: even though it does not stop.
EXIT_TOTAL = 65


class RegistrationVerdict:
    """What one registration pass amounts to, above the per-item counts."""

    def __init__(self, run):
        self.run = run
        self.failed = run.failed
        self.registered = run.registered
        self.skipped = run.skipped
        self.deferred = run.deferred
        self.would_register = run.would_register

    @property
    def attempted(self):
        """Items that got as far as a registration call."""
        return self.registered + self.failed

    @property
    def total_failure(self):
        """Every item that could be attempted failed, and some were."""
        return self.failed > 0 and self.registered == 0

    @property
    def partial_failure(self):
        """Some failed while others got through."""
        return self.failed > 0 and self.registered > 0

    @property
    def exit_code(self):
        if not self.failed:
            return EXIT_OK
        return EXIT_TOTAL if self.total_failure else EXIT_PARTIAL

    def as_dict(self):
        d = dict(self.run.as_dict())
        d["exit_code"] = self.exit_code
        d["verdict"] = ("ok" if not self.failed else
                        "total" if self.total_failure else "partial")
        return d


def run_pass(conn, register=None):
    """One registration pass. Never raises for a failed item; returns a verdict.

    The failed items are already recorded by the consumer — each logged
    with its attempt id and left as a candidate, its transaction rolled
    back — so a failure here is *recorded and skipped*, and the pass
    continues, which is what the granularity fix asks for. The caller
    gets the verdict and decides what the invocation's exit code should
    be; it does not get an exception that would end the pass at the first
    bad item.
    """
    from pipeline.seams import run_registration

    run = run_registration(conn, register=register)
    verdict = RegistrationVerdict(run)

    if verdict.total_failure:
        logger.error(
            "registration pass: ALL %d attemptable item(s) failed; the pass "
            "continues and each is recorded and left as a candidate, but a "
            "total failure is usually systemic (%s)",
            verdict.failed, verdict.as_dict())
    elif verdict.partial_failure:
        logger.warning(
            "registration pass: %d item(s) failed, %d registered; the failed "
            "items are recorded and skipped and the pass continued (%s)",
            verdict.failed, verdict.registered, verdict.as_dict())
    else:
        logger.info("registration pass: %s", verdict.as_dict())

    return verdict
