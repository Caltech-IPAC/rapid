"""The operator itself: one pass, and the accumulator's live path.

The old operator was a `while True:` inside an `if __name__ == '__main__'`
block, 590 lines long, reading `sys.argv[1]` at import time and calling
`exit()` from nine places. Nothing in it was callable from a test — which
is why the rehearsal defect could only be found by running it against
real infrastructure.

What changed in shape:

* one pass is a FUNCTION on an object, so a test can run one;
* submission is delegated to a submitter the caller supplies, which is
  what makes rehearsal structurally incapable rather than flag-guarded
  (see `submitters`);
* the accumulator is on the live path rather than only in the
  one-shot `batch_units` helper, so cadence is a running policy and not
  a property of how big a gathering pass happened to be;
* registration failures are per-item (see `registration`).

THE ACCUMULATOR'S LIVE PATH. `ReadyWorkAccumulator` existed and was
tested, but the only thing that used it was `batch_units`, which builds
one, dumps a finite list in, drains it and throws it away — so the
cadence policy could never actually fire: `should_cut` was never
consulted, and a batch was cut by the array ceiling alone. Under
continuous arrival that is the wrong shape entirely, because the
question is not "how do I cut this list" but "when has enough arrived".

Here the accumulator LIVES ACROSS POLLS. Ready work is offered to it
each poll; it is asked whether to cut; a cut batch goes to the
submitter. Its cadence values come from the parameter tree — derived
from the Q9 drip evidence, see the ledger — and one accumulator holds
one job type, which is what keeps a batch route-homogeneous: "one job
type, one queue, one definition per array submission" (operations.md,
ADOPTED).
"""

import logging

from pipeline.operator import classes as opclasses
from pipeline.operator import registration as opregistration
from submission.batching import ReadyWorkAccumulator

logger = logging.getLogger("rapid.operator")


class PassResult:
    """What one operator pass did. Returned rather than printed."""

    def __init__(self, operational_class):
        self.operational_class = operational_class
        self.gathered = 0
        self.cut_batches = 0
        self.cut_full = 0
        self.cut_stale = 0
        self.submitted = []
        self.registration = None

    @property
    def submissions(self):
        return len(self.submitted)

    def as_dict(self):
        return {
            "class": self.operational_class.name,
            "gathered": self.gathered,
            "cut_batches": self.cut_batches,
            "cut_full": self.cut_full,
            "cut_stale": self.cut_stale,
            "submissions": self.submissions,
            "registration": (self.registration.as_dict()
                             if self.registration else None),
        }


class Operator:
    """One operational class's work, over a live accumulator.

    Parameters
    ----------
    operational_class : OperationalClass
        Which of the four this operator runs. Unimplemented classes are
        refused at construction rather than at the first pass: an
        operator for a class that cannot run should not exist.
    submitter : object
        Anything with `submit(units, operational_class, **kwargs)`. This
        is the rehearsal seam — see `submitters`.
    gather : callable
        Returns the ready units for this pass. Injected so a test can
        supply a list without a database.
    max_batch_size, max_wait_seconds : numbers
        The cadence, from the parameter tree.
    clock : callable, optional
        Monotonic seconds, injected for testing the age trigger.
    """

    def __init__(self, operational_class, submitter, gather,
                 max_batch_size, max_wait_seconds, clock=None,
                 registrar_factory=None, connection_factory=None):
        operational_class.require_implemented()
        self.operational_class = operational_class
        self.submitter = submitter
        self._gather = gather
        self._registrar_factory = registrar_factory
        self._connection_factory = connection_factory
        self.accumulator = ReadyWorkAccumulator(
            max_batch_size=int(max_batch_size),
            max_wait_seconds=float(max_wait_seconds),
            clock=clock,
            job_type=operational_class.job_type)
        logger.info(
            "operator for %s: cadence max_batch_size=%s max_wait_seconds=%s "
            "(parameter tree), submitter=%r",
            operational_class.name, max_batch_size, max_wait_seconds,
            submitter)

    # -- one pass ----------------------------------------------------

    def _run_id_for(self, batch, explicit):
        """This batch's run id — its manifest's identity, never None.

        `submit_units` builds the manifest with `batch_id=run_id`, and
        `publish_manifest` refuses a manifest with no batch_id: "manifest
        has no batch_id; cannot key its object". The old operator always
        minted one per phase (`vpo-<date>-science-...`), so nothing ever
        reached that guard; this operator passed run_id=None and hit it on
        the first live probe, before any child was submitted (2026-08-08).

        The accumulator already stamps every batch with a unique id, so
        that is what identifies this submission — one manifest, one batch,
        one identity, rather than a second id minted here that would have
        to be kept in step with it. An explicit caller-supplied id still
        wins, for a probe that wants a recognisable name.
        """
        if explicit:
            return explicit
        return f"vpo-{self.operational_class.name}-{batch.manifest.batch_id}"

    def run_pass(self, run_id=None, force_cut=False,
                 reference_observation_window=None):
        """Gather, accumulate, cut what the cadence says, submit, register.

        `force_cut` drains whatever is waiting regardless of the triggers
        — for shutdown, and for a bounded probe that should not wait out
        the age trigger to see its work go.
        """
        result = PassResult(self.operational_class)

        units = list(self._gather())
        result.gathered = len(units)
        self.accumulator.extend(units)
        logger.info("%s: gathered %d unit(s); %d waiting, oldest %.1fs",
                    self.operational_class.name, len(units),
                    len(self.accumulator), self.accumulator.waiting_seconds)

        for batch in self._cut_batches(force=force_cut):
            result.cut_batches += 1
            if batch.reason == "size":
                result.cut_full += 1
            elif batch.reason == "age":
                result.cut_stale += 1

            submissions = self.submitter.submit(
                batch.manifest.units,
                self.operational_class,
                run_id=self._run_id_for(batch, run_id),
                reference_observation_window=reference_observation_window)
            result.submitted.extend(submissions or [])

        result.registration = self._register()
        return result

    def _cut_batches(self, force=False):
        """Every batch the cadence says to cut this poll.

        A loop rather than a single cut: a poll that gathered more than
        `max_batch_size` should submit all of it now, not one batch per
        poll while the rest ages.
        """
        while True:
            if force:
                batch = self.accumulator.cut(force=True)
            elif self.accumulator.should_cut():
                batch = self.accumulator.cut()
            else:
                batch = None
            if batch is None:
                return
            yield batch

    def _register(self):
        """The registration step, if this operator was given the means.

        Returns None where no connection factory was supplied — a probe
        or a test that is only exercising submission should not have to
        stand up registration to do it.
        """
        if self._connection_factory is None:
            return None

        with self._connection_factory() as conn:
            register = (self._registrar_factory(conn)
                        if self._registrar_factory else None)
            return opregistration.run_pass(conn, register=register)


def build_accumulator_cadence(parameters):
    """The cadence values, from the tree, with the tree as the only home.

    Missing keys are an error naming them rather than a silent fallback
    to the module defaults: the defaults are the ADOPTED starting values
    that the drip evidence REPLACED, so falling back to them would
    quietly restore the configuration this job exists to retire.
    """
    missing = [k for k in ("submission/max-batch-size",
                           "submission/max-wait-seconds")
               if not parameters.get(k)]
    if missing:
        raise KeyError(
            f"the submission cadence is operational configuration and lives "
            f"in the parameter tree; missing {', '.join(missing)}. These "
            f"values were derived from the smoke run's drip evidence and "
            f"there is no safe default to substitute.")
    return (int(parameters["submission/max-batch-size"]),
            float(parameters["submission/max-wait-seconds"]))
