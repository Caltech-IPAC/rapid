"""Who may submit, decided by which object exists — not by a flag.

THE EXHIBIT THIS MODULE EXISTS FOR (2026-08-07). The old operator had a
rehearsal switch, `RAPID_VPO_DRY_RUN`. It was read in exactly one place —
`production_registrar()`, which returned None when it was set — so what
it suppressed was *registration writes*. Submission was never on its
path at all: `submit_gathered` was called unconditionally, three times,
whatever the flag said. A rehearsal therefore submitted **5,057 real
children in 35 seconds** while reporting itself a dry run
(`smoke_run.rst`, "Shape, and why these numbers"). The flag was not
wrong; it was answering a different question than the operator running
it believed.

A flag cannot fix that, because the defect is not the flag's value — it
is that a submitting call site is reachable from the rehearsal path at
all. Any guard is one forgotten `if` from the same outcome, and the
forgotten `if` is not hypothetical: there were three call sites and the
flag guarded none of them.

So the capability is an OBJECT, and rehearsal is given one that does not
have it:

* `LiveSubmitter` holds the Batch and S3 clients and calls
  `submit_gathered`. It is the only thing in this package that imports
  it.
* `RehearsalSubmitter` holds no clients, has no import of the
  submission seam, and its `submit` reports what would go and returns
  no submissions.

There is no code path from a `RehearsalSubmitter` to a submit call, in
the plain sense that the function is not named anywhere its methods can
reach. `test_rehearsal_cannot_submit` asserts exactly that, by walking
the code objects reachable from the rehearsal class and failing if the
submitting seam appears among their names — so a future edit that
reintroduces reachability fails a test rather than a live run.
"""

import logging

from pipeline.operator.classes import OperationalClass

logger = logging.getLogger("rapid.operator.submitters")


class RehearsalSubmission(Exception):
    """A rehearsal submitter was asked for something only live work does.

    Distinct from a refusal to submit — `RehearsalSubmitter.submit`
    refuses quietly and counts, because refusing IS its job. This is for
    the case where a caller reaches past the interface for a live-only
    facility, which means the caller has assumed a capability that the
    rehearsal deliberately lacks.
    """


class RehearsalSubmitter:
    """Cuts batches, records what they would be, submits nothing.

    Deliberately NOT a `LiveSubmitter` subclass and deliberately not
    sharing its base: inheritance would put the submitting method on this
    object's own MRO, one `super()` call away from being reachable again.
    The two classes share an interface by both having `submit`, which is
    all a duck-typed caller needs and all the coupling that is safe.

    Holds no AWS client of any kind. There is nothing to call even if
    something tried.
    """

    #: What a caller can read to know what it is holding, without an
    #: isinstance check on a class it may not want to import.
    can_submit = False

    def __init__(self):
        self.would_submit_units = 0
        self.would_submit_batches = 0

    def submit(self, units, operational_class: OperationalClass, **_ignored):
        """Count what a live pass would have submitted. Submit nothing.

        Returns an empty submission list, which is the literal truth and
        is also what the caller's downstream wait and registration steps
        need to see: no submissions means nothing to wait for.

        The extra keyword arguments a live submission needs — clients,
        buckets, bindings — are accepted and ignored rather than
        rejected, so the operator's call site is IDENTICAL on both paths.
        A call site that had to differ would be a second place for the
        two paths to diverge, and divergence at the call site is the
        original defect.
        """
        units = list(units)
        self.would_submit_units += len(units)
        if units:
            self.would_submit_batches += 1
        logger.info(
            "REHEARSAL: %d %s unit(s) would be submitted; submitting "
            "nothing — this process holds no Batch client",
            len(units), operational_class.name)
        return []

    def __repr__(self):
        return (f"RehearsalSubmitter(would_submit_units="
                f"{self.would_submit_units})")


class LiveSubmitter:
    """The real thing: cuts batches and submits them to AWS Batch.

    Built only where live work is intended. `Operator` takes a submitter
    rather than building one, so the decision of which exists is made
    once, at the entry point, from the mode the operator was asked for —
    and a rehearsal never constructs this class.
    """

    can_submit = True

    def __init__(self, context, execute_factory, max_batch_size=None):
        """
        Parameters
        ----------
        context : dict
            The submission context: clients, buckets, binding, queue and
            definition, as the operator resolves them once per pass.
        execute_factory : callable
            Returns a context manager yielding a database execute
            callable. A factory rather than a connection because each
            submission takes its own transaction, exactly as the old
            operator's `with connection(...)` blocks did.
        max_batch_size : int, optional
            The accumulator's size trigger, from the parameter tree.
        """
        self._context = context
        self._execute_factory = execute_factory
        self._max_batch_size = max_batch_size

    def submit(self, units, operational_class: OperationalClass,
               run_id=None, reference_observation_window=None, **_ignored):
        """Submit these units as this class's route. Returns submissions.

        The import is INSIDE the method, and that is not laziness. It is
        what keeps `submit_gathered` out of the module namespace that
        `RehearsalSubmitter` shares — the reachability test walks names,
        and a module-level import would put the seam in this module's
        globals where both classes live.
        """
        from pipeline.seams import submit_gathered

        units = list(units)
        if not units:
            logger.info("nothing ready to submit for %s",
                        operational_class.name)
            return []

        route = operational_class.route
        with self._execute_factory() as execute:
            return submit_gathered(
                units,
                job_type=route.job_type,
                queue=self._context["queue"],
                job_definition=self._context["job_definition"],
                binding=self._context["binding"],
                manifest_bucket=self._context["manifest_bucket"],
                manifest_prefix=self._context["manifest_prefix"],
                s3_client=self._context["s3_client"],
                batch_client=self._context["batch_client"],
                execute=execute,
                run_id=run_id,
                max_batch_size=self._max_batch_size,
                reference_observation_window=reference_observation_window)

    def __repr__(self):
        return "LiveSubmitter(can_submit=True)"
