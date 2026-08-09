"""Tests for the restructured operator.

The one that matters most is `TestRehearsalCannotSubmit`. It does not
test that rehearsal *chooses* not to submit — a flag test would pass
against the code that submitted 5,057 real children, because that code's
flag worked exactly as written and simply governed something else. It
tests that the submitting seam is not REACHABLE from the rehearsal
class, by walking the code objects its methods can reach and failing if
the seam's name appears. A future edit that puts a submit call back
within reach fails here rather than on live infrastructure.
"""

import types
import unittest

from pipeline.operator import classes as opclasses
from pipeline.operator import inputs as opinputs
from pipeline.operator import registration as opregistration
from pipeline.operator.operator import Operator, build_accumulator_cadence
from pipeline.operator.submitters import LiveSubmitter, RehearsalSubmitter
from submission.manifest import ProcessingUnit


def unit(n):
    """One processing unit, enough of one for the accumulator."""
    return ProcessingUnit(exposure=n, sca=1)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class RecordingSubmitter:
    """A live-shaped submitter that records instead of calling AWS."""

    can_submit = True

    def __init__(self):
        self.calls = []

    def submit(self, units, operational_class, **kwargs):
        units = list(units)
        self.calls.append((units, operational_class.name))
        return [("submission", [u.exposure for u in units])]


# ---------------------------------------------------------------------
# The rehearsal refusal test
# ---------------------------------------------------------------------

def _reachable_names(start_class, depth=6):
    """Every name referenced by code reachable from this class's methods.

    Walks `co_names` and `co_consts` transitively through nested code
    objects, which is what catches a submit call hidden in a closure or
    a comprehension rather than only a direct method body.
    """
    seen_code = set()
    names = set()

    def walk(code, level):
        if level > depth or id(code) in seen_code:
            return
        seen_code.add(id(code))
        names.update(code.co_names)
        names.update(n for n in code.co_varnames)
        for const in code.co_consts:
            if isinstance(const, types.CodeType):
                walk(const, level + 1)

    for attr in vars(start_class).values():
        func = getattr(attr, "__func__", attr)
        code = getattr(func, "__code__", None)
        if code is not None:
            walk(code, 0)
    return names


#: The names that mean "this submits". If any becomes reachable from the
#: rehearsal submitter, rehearsal can submit again.
SUBMITTING_NAMES = frozenset({
    "submit_gathered", "submit_units", "submit_job", "batch_client",
})


class TestRehearsalCannotSubmit(unittest.TestCase):

    def test_submitting_seam_is_not_reachable_from_rehearsal(self):
        """THE test. Fails if a submit call becomes reachable in rehearsal."""
        reachable = _reachable_names(RehearsalSubmitter)
        offending = reachable & SUBMITTING_NAMES
        self.assertEqual(
            offending, set(),
            f"RehearsalSubmitter can reach {sorted(offending)} — a rehearsal "
            f"that can reach a submitting call is the 2026-08-07 defect. "
            f"Rehearsal must hold no submitting capability, not a guarded one.")

    def test_the_reachability_check_can_actually_fail(self):
        """The negative control: the checker must detect a real submitter.

        Without this, a checker that silently matched nothing would pass
        the test above forever while proving nothing — a false clean of
        exactly the kind this project has already paid for once.
        """
        reachable = _reachable_names(LiveSubmitter)
        self.assertTrue(
            reachable & SUBMITTING_NAMES,
            "the reachability walk found no submitting name in LiveSubmitter, "
            "which DOES submit — the checker is broken, so its clean verdict "
            "on RehearsalSubmitter means nothing")

    def test_rehearsal_holds_no_batch_client(self):
        rehearsal = RehearsalSubmitter()
        self.assertFalse(rehearsal.can_submit)
        for value in vars(rehearsal).values():
            self.assertNotIn("batch", type(value).__name__.lower())

    def test_rehearsal_submit_returns_nothing_and_counts(self):
        rehearsal = RehearsalSubmitter()
        prompt = opclasses.class_for(opclasses.PROMPT_PROCESSING)
        result = rehearsal.submit([unit(1), unit(2)], prompt)
        self.assertEqual(list(result), [])
        self.assertEqual(rehearsal.would_submit_units, 2)
        self.assertEqual(rehearsal.would_submit_batches, 1)

    def test_rehearsal_accepts_the_live_call_signature(self):
        """Identical call sites on both paths: divergence there is the bug."""
        rehearsal = RehearsalSubmitter()
        prompt = opclasses.class_for(opclasses.PROMPT_PROCESSING)
        result = rehearsal.submit(
            [unit(1)], prompt, run_id="r1",
            reference_observation_window=(1.0, 2.0))
        self.assertEqual(result, [])

    def test_rehearsal_is_not_a_live_submitter_subclass(self):
        """Inheritance would put submit() one super() call from reachable."""
        self.assertFalse(issubclass(RehearsalSubmitter, LiveSubmitter))
        self.assertNotIn(LiveSubmitter, RehearsalSubmitter.__mro__)

    def test_a_full_rehearsal_pass_submits_nothing(self):
        """End to end: gather, accumulate, cut, and still zero submissions."""
        clock = FakeClock()
        rehearsal = RehearsalSubmitter()
        prompt = opclasses.class_for(opclasses.PROMPT_PROCESSING)
        operator = Operator(
            prompt, rehearsal, gather=lambda: [unit(i) for i in range(120)],
            max_batch_size=60, max_wait_seconds=60, clock=clock)

        result = operator.run_pass()

        self.assertEqual(result.gathered, 120)
        self.assertEqual(result.cut_batches, 2)       # 120 units, size 60
        self.assertEqual(result.submissions, 0)       # and nothing submitted
        self.assertEqual(rehearsal.would_submit_units, 120)


# ---------------------------------------------------------------------
# Accumulator live path and cadence
# ---------------------------------------------------------------------

class TestAccumulatorLivePath(unittest.TestCase):

    def _operator(self, gather, clock, submitter=None,
                  max_batch_size=60, max_wait_seconds=60):
        prompt = opclasses.class_for(opclasses.PROMPT_PROCESSING)
        return Operator(prompt, submitter or RecordingSubmitter(), gather,
                        max_batch_size=max_batch_size,
                        max_wait_seconds=max_wait_seconds, clock=clock)

    def test_accumulator_persists_across_passes(self):
        """The live path's whole point: work waits between polls.

        `batch_units` could never do this — it builds an accumulator,
        drains it and discards it, so a partial batch was submitted
        immediately rather than waiting for the cadence.
        """
        clock = FakeClock()
        submitter = RecordingSubmitter()
        pending = [[unit(1), unit(2)], [unit(3), unit(4)]]
        operator = self._operator(lambda: pending.pop(0), clock, submitter)

        first = operator.run_pass()
        self.assertEqual(first.cut_batches, 0)        # 2 < 60, and not stale
        self.assertEqual(len(operator.accumulator), 2)

        second = operator.run_pass()
        self.assertEqual(second.cut_batches, 0)
        self.assertEqual(len(operator.accumulator), 4)  # accumulated, not sent
        self.assertEqual(submitter.calls, [])

    def test_age_trigger_cuts_a_small_batch(self):
        clock = FakeClock()
        submitter = RecordingSubmitter()
        pending = [[unit(1), unit(2)], []]
        operator = self._operator(lambda: pending.pop(0), clock, submitter)

        operator.run_pass()
        clock.advance(61)
        result = operator.run_pass()

        self.assertEqual(result.cut_batches, 1)
        self.assertEqual(result.cut_stale, 1)
        self.assertEqual(result.cut_full, 0)
        self.assertEqual(len(submitter.calls[0][0]), 2)

    def test_size_trigger_cuts_a_full_batch(self):
        clock = FakeClock()
        submitter = RecordingSubmitter()
        operator = self._operator(
            lambda: [unit(i) for i in range(60)], clock, submitter)

        result = operator.run_pass()

        self.assertEqual(result.cut_batches, 1)
        self.assertEqual(result.cut_full, 1)
        self.assertEqual(result.cut_stale, 0)

    def test_both_triggers_are_reachable_at_the_derived_cadence(self):
        """Why 60/60 and not the adopted 500/60.

        At the drip's measured 0.098 units/s, a size of 500 needs 5,103 s
        to fill against a 60 s age bound — unreachable by ~85x, so
        `max_batch_size` would be dead configuration. This asserts the
        derived pair keeps both triggers live.
        """
        rate = 60 / 612.3          # units/s, from the Q9 drip arrivals
        derived_size, bound = 60, 60.0
        self.assertLess(derived_size / rate, 5103,
                        "the derived size must fill sooner than the adopted "
                        "500 did")
        self.assertGreaterEqual(500 / rate, 5000,
                                "the adopted default really was unreachable")
        # A whole wave arriving at once cuts on size, within the bound.
        clock = FakeClock()
        submitter = RecordingSubmitter()
        operator = self._operator(lambda: [unit(i) for i in range(60)],
                                  clock, submitter,
                                  max_batch_size=derived_size,
                                  max_wait_seconds=bound)
        result = operator.run_pass()
        self.assertEqual(result.cut_full, 1)

    def test_batches_are_route_homogeneous(self):
        """One accumulator, one job type — one queue and definition."""
        clock = FakeClock()
        prompt = opclasses.class_for(opclasses.PROMPT_PROCESSING)
        operator = self._operator(lambda: [unit(i) for i in range(60)], clock)
        self.assertEqual(operator.accumulator.job_type, prompt.job_type)
        batch = operator.accumulator.cut(force=True) or None
        operator.accumulator.extend([unit(i) for i in range(60)])
        cut = operator.accumulator.cut(force=True)
        self.assertEqual(cut.manifest.job_type, prompt.job_type)

    def test_force_cut_drains_without_waiting(self):
        clock = FakeClock()
        submitter = RecordingSubmitter()
        operator = self._operator(lambda: [unit(1), unit(2)], clock, submitter)
        result = operator.run_pass(force_cut=True)
        self.assertEqual(result.cut_batches, 1)
        self.assertEqual(result.submissions, 1)


class TestCadenceFromTree(unittest.TestCase):

    def test_values_come_from_the_tree(self):
        size, wait = build_accumulator_cadence({
            "submission/max-batch-size": "60",
            "submission/max-wait-seconds": "60"})
        self.assertEqual((size, wait), (60, 60.0))

    def test_missing_values_raise_rather_than_default(self):
        """Falling back would restore the configuration this job retired."""
        with self.assertRaises(KeyError) as caught:
            build_accumulator_cadence({"submission/max-batch-size": "60"})
        self.assertIn("max-wait-seconds", str(caught.exception))


# ---------------------------------------------------------------------
# Declared classes
# ---------------------------------------------------------------------

class TestDeclaredClasses(unittest.TestCase):

    def test_all_four_are_declared(self):
        self.assertEqual(len(opclasses.CLASSES), 4)
        self.assertEqual(
            set(opclasses.CLASS_NAMES),
            {opclasses.PROMPT_PROCESSING, opclasses.REFERENCE_CONSTRUCTION,
             opclasses.HISTORICAL_BACKFILL, opclasses.RELEASE_REPROCESSING})

    def test_two_are_declared_not_implemented(self):
        unimplemented = [c.name for c in opclasses.CLASSES
                         if not c.implemented]
        self.assertEqual(sorted(unimplemented),
                         sorted([opclasses.HISTORICAL_BACKFILL,
                                 opclasses.RELEASE_REPROCESSING]))

    def test_running_an_unimplemented_class_raises_with_the_reason(self):
        backfill = opclasses.class_for(opclasses.HISTORICAL_BACKFILL)
        with self.assertRaises(opclasses.ClassNotImplemented) as caught:
            backfill.require_implemented()
        self.assertIn("failure-path design", str(caught.exception))

    def test_an_operator_for_an_unimplemented_class_cannot_be_built(self):
        backfill = opclasses.class_for(opclasses.HISTORICAL_BACKFILL)
        with self.assertRaises(opclasses.ClassNotImplemented):
            Operator(backfill, RehearsalSubmitter(), lambda: [],
                     max_batch_size=60, max_wait_seconds=60)

    def test_unknown_class_names_are_refused(self):
        with self.assertRaises(ValueError) as caught:
            opclasses.class_for("backfill")
        self.assertIn("declared set", str(caught.exception))

    def test_implemented_classes_map_to_routes(self):
        for declared in opclasses.implemented_classes():
            self.assertIsNotNone(declared.route.queue_parameter)


# ---------------------------------------------------------------------
# Operator input
# ---------------------------------------------------------------------

class TestOperatorInput(unittest.TestCase):

    def test_window_replaces_the_processing_date(self):
        got = opinputs.build("2027-10-01T00:00:00", "2027-10-07T00:00:00")
        self.assertEqual(got.start.year, 2027)
        self.assertEqual(got.end.day, 7)

    def test_naive_datetimes_are_utc(self):
        got = opinputs.build("2027-10-01T00:00:00", "2027-10-02T00:00:00")
        self.assertIsNotNone(got.start.tzinfo)
        self.assertEqual(got.start.utcoffset().total_seconds(), 0)

    def test_a_backwards_window_is_refused(self):
        with self.assertRaises(opinputs.InputError):
            opinputs.build("2027-10-07T00:00:00", "2027-10-01T00:00:00")

    def test_a_date_is_refused_as_a_window(self):
        with self.assertRaises(opinputs.InputError):
            opinputs.build("not-a-datetime", "2027-10-02T00:00:00")

    def test_the_census_is_complete(self):
        got = opinputs.build("2027-10-01", "2027-10-02",
                             {opclasses.PROMPT_PROCESSING: opinputs.RUN})
        self.assertEqual(set(got.dispositions), set(opclasses.CLASS_NAMES))

    def test_unnamed_implemented_classes_hold(self):
        got = opinputs.build("2027-10-01", "2027-10-02",
                             {opclasses.PROMPT_PROCESSING: opinputs.RUN})
        self.assertEqual(got.disposition_of(opclasses.REFERENCE_CONSTRUCTION),
                         opinputs.HOLD)

    def test_unimplemented_classes_are_marked_as_such_not_held(self):
        """'Cannot run' and 'chose not to run' are different records."""
        got = opinputs.build("2027-10-01", "2027-10-02")
        self.assertEqual(got.disposition_of(opclasses.HISTORICAL_BACKFILL),
                         opinputs.DECLARED_NOT_IMPLEMENTED)

    def test_asking_to_run_an_unimplemented_class_is_refused(self):
        with self.assertRaises(opinputs.InputError) as caught:
            opinputs.build("2027-10-01", "2027-10-02",
                           {opclasses.RELEASE_REPROCESSING: opinputs.RUN})
        self.assertIn("release machinery", str(caught.exception))

    def test_to_run_lists_only_run_classes(self):
        got = opinputs.build("2027-10-01", "2027-10-02", {
            opclasses.PROMPT_PROCESSING: opinputs.RUN,
            opclasses.REFERENCE_CONSTRUCTION: opinputs.HOLD})
        self.assertEqual([c.name for c in got.to_run],
                         [opclasses.PROMPT_PROCESSING])


# ---------------------------------------------------------------------
# Registration abort granularity
# ---------------------------------------------------------------------

class FakeRun:
    def __init__(self, registered=0, failed=0, skipped=0, deferred=0,
                 would_register=0):
        self.registered = registered
        self.failed = failed
        self.skipped = skipped
        self.deferred = deferred
        self.would_register = would_register
        self.refused_application_failed = 0

    def as_dict(self):
        return {"registered": self.registered, "failed": self.failed,
                "skipped": self.skipped, "deferred": self.deferred,
                "would_register": self.would_register}


class TestRegistrationGranularity(unittest.TestCase):

    def test_clean_pass_is_zero(self):
        verdict = opregistration.RegistrationVerdict(FakeRun(registered=5))
        self.assertEqual(verdict.exit_code, opregistration.EXIT_OK)

    def test_partial_failure_is_distinct_from_total(self):
        """The acceptance clause: partial and total must differ."""
        partial = opregistration.RegistrationVerdict(
            FakeRun(registered=4, failed=1))
        total = opregistration.RegistrationVerdict(
            FakeRun(registered=0, failed=5))
        self.assertEqual(partial.exit_code, opregistration.EXIT_PARTIAL)
        self.assertEqual(total.exit_code, opregistration.EXIT_TOTAL)
        self.assertNotEqual(partial.exit_code, total.exit_code)

    def test_partial_is_reported_as_partial(self):
        verdict = opregistration.RegistrationVerdict(
            FakeRun(registered=4, failed=1))
        self.assertTrue(verdict.partial_failure)
        self.assertFalse(verdict.total_failure)
        self.assertEqual(verdict.as_dict()["verdict"], "partial")

    def test_a_failed_item_does_not_stop_the_pass(self):
        """Fourteen bad records must not block every later pass.

        The consumer already registers each item in its own transaction
        and counts failures; what this asserts is that the operator level
        turns that into a verdict rather than an exit — `run_pass` returns
        for a run with failures instead of raising.
        """
        import pipeline.seams as seams

        original = seams.run_registration
        seams.run_registration = lambda conn, register=None: FakeRun(
            registered=3, failed=14)
        try:
            verdict = opregistration.run_pass(conn=None)
        finally:
            seams.run_registration = original

        self.assertEqual(verdict.failed, 14)
        self.assertEqual(verdict.registered, 3)
        self.assertEqual(verdict.exit_code, opregistration.EXIT_PARTIAL)


class TestSubmissionContextReusesTheOwner(unittest.TestCase):
    """The binding contract has one implementation; borrow it.

    `build_submission_context` briefly rebuilt what
    `virtualPipelineOperator.submission_env` already owned, and got it
    wrong: it put `active_definition`'s raw dict where a
    `SubmissionBinding` belonged, and the live probe died at
    `binding.job_definition_arn` with "'dict' object has no attribute".
    """

    def test_it_delegates_rather_than_rebuilding(self):
        import inspect

        from pipeline.operator import service as opservice

        source = inspect.getsource(opservice.build_submission_context)
        self.assertIn("submission_env", source,
                      "the submission context must come from the function "
                      "that owns it, not be rebuilt here")
        self.assertNotIn(
            "SubmissionBinding(", source,
            "constructing a binding here duplicates submission_env's "
            "contract, which is how the two drift")


class TestEverySubmissionCarriesARunId(unittest.TestCase):
    """Found live, 2026-08-08, on the first width-2 live probe.

    `submit_units` builds its manifest with `batch_id=run_id`, and
    `publish_manifest` refuses a manifest with no batch_id. The operator
    passed run_id=None, so the probe died with "manifest has no batch_id;
    cannot key its object" — exit 70, before any child was submitted. The
    old operator always minted one per phase, so nothing ever reached that
    guard until this operator replaced it.
    """

    def test_run_id_is_never_none(self):
        clock = FakeClock()
        submitter = RecordingRunIdSubmitter()
        prompt = opclasses.class_for(opclasses.PROMPT_PROCESSING)
        operator = Operator(prompt, submitter,
                            gather=lambda: [unit(1), unit(2)],
                            max_batch_size=60, max_wait_seconds=60,
                            clock=clock)
        operator.run_pass(force_cut=True)

        self.assertTrue(submitter.run_ids, "nothing was submitted")
        for run_id in submitter.run_ids:
            self.assertIsNotNone(
                run_id,
                "a submission with run_id=None becomes a manifest with no "
                "batch_id, which publish_manifest refuses")
            self.assertTrue(str(run_id).strip())

    def test_the_run_id_is_the_batch_identity(self):
        """One manifest, one batch, one identity — not a second id."""
        clock = FakeClock()
        submitter = RecordingRunIdSubmitter()
        prompt = opclasses.class_for(opclasses.PROMPT_PROCESSING)
        operator = Operator(prompt, submitter, gather=lambda: [unit(1)],
                            max_batch_size=60, max_wait_seconds=60,
                            clock=clock)
        operator.run_pass(force_cut=True)
        self.assertIn(opclasses.PROMPT_PROCESSING, submitter.run_ids[0])

    def test_an_explicit_run_id_wins(self):
        clock = FakeClock()
        submitter = RecordingRunIdSubmitter()
        prompt = opclasses.class_for(opclasses.PROMPT_PROCESSING)
        operator = Operator(prompt, submitter, gather=lambda: [unit(1)],
                            max_batch_size=60, max_wait_seconds=60,
                            clock=clock)
        operator.run_pass(run_id="probe-1", force_cut=True)
        self.assertEqual(submitter.run_ids[0], "probe-1")


class RecordingRunIdSubmitter:
    """Records the run_id each submission was given."""

    can_submit = True

    def __init__(self):
        self.run_ids = []

    def submit(self, units, operational_class, run_id=None, **kwargs):
        self.run_ids.append(run_id)
        return [("submission", [])]


class TestBoundedProbeWidth(unittest.TestCase):
    """`--width` capped by an explicit `--max-width`, refused not clamped.

    Same shape as scripts/q8_ramp_probe.py's pair, and for its reason: a
    width alone can be mistyped into a runaway, so the caller must also
    state the ceiling it believes it is under. The drip's submissions were
    exactly attributable because of this guard.
    """

    def test_width_caps_the_gathered_list(self):
        from pipeline.operator.service import _bounded

        gather = lambda: [unit(i) for i in range(3779)]
        self.assertEqual(len(_bounded(gather, 2, 4, "prompt-processing")()), 2)

    def test_a_cap_below_the_gathered_count_is_logged_not_silent(self):
        """A silent cap reads exactly like a complete run."""
        from pipeline.operator.service import _bounded

        with self.assertLogs("rapid.operator.service", level="INFO") as caught:
            _bounded(lambda: [unit(i) for i in range(10)], 2, 4, "prompt")()
        self.assertTrue(any("dropping 8" in m for m in caught.output),
                        f"the drop count must be logged; got {caught.output}")

    def test_width_within_the_gathered_count_drops_nothing(self):
        from pipeline.operator.service import _bounded

        self.assertEqual(
            len(_bounded(lambda: [unit(1), unit(2)], 5, 5, "prompt")()), 2)


class TestIdleServiceDoesNotExit(unittest.TestCase):
    """Found live, 2026-08-08, on the first enabled deploy.

    Both classes were deployed on `hold` — this stack's own default — and
    the service returned 0 for "nothing to do". systemd's Restart=always
    turned that into a restart loop: start, exit 0, restart 15 s later,
    restart counter climbing. A supervised service with nothing to do must
    idle; only `--once` should exit.
    """

    def test_all_hold_is_a_legitimate_state_not_an_exit(self):
        got = opinputs.build("2027-10-01", "2027-10-07", {
            opclasses.PROMPT_PROCESSING: opinputs.HOLD,
            opclasses.REFERENCE_CONSTRUCTION: opinputs.HOLD})
        self.assertEqual(got.to_run, ())
        # Nothing to run is a complete, valid census — not an error and
        # not a reason to refuse to start.
        self.assertEqual(set(got.dispositions), set(opclasses.CLASS_NAMES))

    def test_the_service_idles_rather_than_exiting_when_nothing_runs(self):
        """The service loop must not fall through when `to_run` is empty."""
        import inspect

        from pipeline.operator import service as opservice

        source = inspect.getsource(opservice.main)
        marker = "if not to_run and args.once:"
        self.assertIn(
            marker, source,
            "the empty-disposition path must distinguish --once (exit) "
            "from service mode (idle); without that split, a service "
            "deployed with every class on hold restart-loops")
        # And the service branch must actually block rather than return.
        after = source.split("if not to_run:", 1)[1]
        self.assertIn(
            "while running[", after,
            "the service branch must idle in a loop, not return — "
            "returning is what systemd turns into a restart loop")


class TestLegacyModuleIsImportable(unittest.TestCase):
    """The regression found live on rapid-admin, 2026-08-08.

    `pipeline/virtualPipelineOperator.py` ran its whole startup at import:
    argv, RAPID_SW, STARTDATETIME, and `exit(64)` when any was missing. The
    restructured service's gather step imported it for one pure helper and
    died mid-rehearsal — after resolving a credential and announcing
    REHEARSAL MODE — with "Env. var. STARTDATETIME not set".

    Importing it must be side-effect-free, with no environment set. If this
    test ever fails, something has been moved back to module scope.
    """

    def test_importing_the_legacy_module_does_not_run_its_startup(self):
        import os
        import subprocess
        import sys

        # A SUBPROCESS with a cleared environment, not an import here: this
        # test module's process may already have the variables set, and an
        # in-process import would also be cached by an earlier import
        # elsewhere in the suite. Neither would prove anything.
        env = {k: v for k, v in os.environ.items()
               if k in ("PATH", "PYTHONPATH", "HOME", "LANG")}
        proc = subprocess.run(
            [sys.executable, "-c",
             "import pipeline.virtualPipelineOperator as m; "
             "print('IMPORT_OK', hasattr(m, 'active_definition'))"],
            capture_output=True, text=True, env=env, timeout=120)

        # A missing third-party dependency (boto3, astropy) is an
        # environment gap, not the defect under test — this module's real
        # home is in-container, where they exist. The defect is the module
        # running its own STARTUP at import, which announces itself
        # specifically. Skipping rather than passing: a green result here
        # off-container would claim proof this run did not obtain.
        if proc.returncode != 0 and "ModuleNotFoundError" in proc.stderr:
            self.skipTest(
                f"third-party import unavailable in this environment, so "
                f"the startup-at-import check cannot run here: "
                f"{proc.stderr.strip().splitlines()[-1]}")

        self.assertNotIn(
            "STARTDATETIME", proc.stdout + proc.stderr,
            "importing the legacy operator module ran its startup — it is "
            "reading the environment interface at import again")
        self.assertEqual(
            proc.returncode, 0,
            f"importing the legacy operator module exited {proc.returncode} "
            f"with no environment set — it is running startup at import "
            f"again.\nstdout: {proc.stdout}\nstderr: {proc.stderr}")
        self.assertIn("IMPORT_OK True", proc.stdout)

    def test_the_operator_does_not_import_the_legacy_module_for_helpers(self):
        """The helpers moved; the import that caused the failure is gone."""
        import pathlib

        service = pathlib.Path(__file__).parent.parent / "operator" / "service.py"
        gathering = pathlib.Path(__file__).parent.parent / "operator" / "gathering.py"
        for path in (service, gathering):
            text = path.read_text()
            self.assertNotIn(
                "from pipeline.virtualPipelineOperator import mjd_window",
                text,
                f"{path.name} imports a helper from the legacy script module")


if __name__ == "__main__":
    unittest.main()
