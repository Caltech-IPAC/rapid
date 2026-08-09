.. _vpo_service:

The Virtual Pipeline Operator as a service
##########################################

The VPO's documentation page. It carries the restructure record: what the
operator is now, why each piece has the shape it has, and what is
verified against what evidence.

Design basis: ``design/operations.md`` § The Virtual Pipeline Operator
(the ADOPTED paragraphs), ``design/compute.md`` § Submission,
``design/catalog.md`` § Promotion, and ``design/code-standards.md``
§ Environment variables.

The service shape
*****************

**ADOPTED:** "The VPO is a supervised long-running service under the same
service-supervision requirement as the reconciler — clean
start/stop/restart, bounded local diagnostics — not a scheduled script."

It was a scheduled script, and the script shape was load-bearing in the
wrong direction. ``pipeline/virtualPipelineOperator.py`` was 1,360 lines
whose top level read ``sys.argv[1]``, printed diagnostics, demanded five
environment variables and called ``exit(64)`` when any was absent; the
work ran in a ``while True:`` inside ``if __name__ == '__main__'``.
Nothing in it was callable from a test, which is why its worst defect —
below — could only ever be found by running it against real
infrastructure.

The replacement is ``pipeline/operator/``:

.. list-table::
   :header-rows: 1

   * - Module
     - What it owns
   * - ``classes.py``
     - The four declared operational classes; the two unimplemented ones
       carry the reason each is blocked.
   * - ``inputs.py``
     - One invocation's complete statement: a window, and a disposition
       for every declared class.
   * - ``submitters.py``
     - Who may submit. The rehearsal seam.
   * - ``operator.py``
     - One pass: gather, accumulate, cut on the tree's cadence, submit,
       register.
   * - ``registration.py``
     - Pass-level verdict over the consumer's per-item results.
   * - ``gathering.py``
     - The ready-work queries and the two helpers they need.
   * - ``service.py``
     - The entry point the systemd unit runs.

``service.py`` deliberately mirrors ``pipeline/reconciler/main.py``: the
same role-chain into the orchestrator identity, the same
parameter-tree-first configuration, the same practice of passing the
database endpoint and credential to the connection helper rather than
exporting them into the environment, and the same exit-code vocabulary
(70 could not start, 71 was working and stopped being able to).

Rehearsal cannot submit, structurally
*************************************

**This is the load-bearing fix.** The old operator had a rehearsal
switch, ``RAPID_VPO_DRY_RUN``. It was read in exactly one place —
``production_registrar()``, which returned ``None`` when it was set — so
what it suppressed was *registration writes*. Submission was never on its
path at all: ``submit_gathered`` was called unconditionally, at three
call sites, whatever the flag said.

A rehearsal therefore **submitted 5,057 real children in 35 seconds**
while reporting itself a dry run (2026-08-07; see ``smoke_run.rst``
§ "Shape, and why these numbers", which records the same event as the
rogue-VPO class the drip harness was built to make impossible).

The flag was not wrong. It answered a different question than the
operator running it believed, and **no value of it could have helped** —
which is why the fix is not a better flag. The capability is now an
object, and rehearsal is given one that does not have it:

* ``LiveSubmitter`` holds the Batch and S3 clients and calls
  ``submit_gathered``. It is the only thing in the package that imports
  that seam, and it imports it *inside* the method so the name never
  enters the module namespace the two classes share.
* ``RehearsalSubmitter`` holds no clients, has no import of the seam, and
  is deliberately **not** a subclass of ``LiveSubmitter`` — inheritance
  would put the submitting method on its MRO, one ``super()`` call from
  reachable.

Both expose the same ``submit(units, operational_class, **kwargs)``, so
the operator's call site is identical on both paths. A call site that had
to differ would be a second place for the two paths to diverge, and
divergence at the call site is the original defect.

``RAPID_VPO_DRY_RUN`` is now **refused at startup** rather than honoured
or ignored: anything still setting it is asking for the semantics that
submitted 5,057 children, and refusing to start is the only response that
cannot be misread.

The refusal test, and why it is not a flag test
===============================================

``test_submitting_seam_is_not_reachable_from_rehearsal`` walks the code
objects reachable from ``RehearsalSubmitter`` — transitively, through
nested code objects, so a call hidden in a closure or comprehension is
caught — and fails if a submitting name appears among them.

It deliberately does not test that rehearsal *chooses* not to submit. A
flag test would have passed against the code that submitted 5,057
children: that flag worked exactly as written and simply governed
something else.

``test_the_reachability_check_can_actually_fail`` is the negative
control: it asserts the same walk **does** find a submitting name in
``LiveSubmitter``. Without it, a checker that silently matched nothing
would pass forever while proving nothing — the false-clean failure this
project has already paid for once (the env-policy checker's first
version, caught by its own control). The walk was additionally verified
by hand against a class whose submit call sits behind ``if False:`` —
dead code, the exact shape of the original defect — and it catches that.

The accumulator's live path
***************************

**ADOPTED:** "Under continuous arrival the accumulator is the live
submission mechanism: ready work flows into it and batches are cut by
size or age. A batch is homogeneous in its validated route — one job
type, one queue, one definition per array submission."

``ReadyWorkAccumulator`` existed and was well tested, but the only thing
that used it was ``batch_units``, which builds one, dumps a finite list
in, drains it and throws it away. So ``should_cut`` was never consulted
and the cadence policy could never fire: a batch was cut by the array
ceiling alone. Under continuous arrival that is the wrong shape entirely,
because the question is not "how do I cut this list" but "when has enough
arrived".

The operator now holds an accumulator **across polls**. Ready work is
offered each poll, the cadence is asked whether to cut, and a cut batch
goes to the submitter. One accumulator holds one job type, which is what
keeps a batch route-homogeneous.

Cadence, derived from the drip evidence
=======================================

The adopted defaults were 500 units / 60 s, explicitly PROPOSED pending
the smoke run's measured arrival rate. The drip supplied it.

From ``fix_round.rst`` § "The overlap is measured, not assumed" — waves
of 60 children arriving at 12:49:10, 12:59:23, 13:09:36 and 13:19:47 UTC:

.. list-table::
   :header-rows: 1

   * - Quantity
     - Value
   * - Mean inter-arrival gap
     - 612.3 s
   * - Children per wave
     - 60
   * - Measured arrival rate
     - **0.098 units/s** (5.88 units/min)

**Why 500 could not stand.** At that rate 500 units take **5,103 s** to
accumulate, against a ``max_wait_seconds`` bound of **60 s** — the
allocation ``operations.md`` makes from the latency budget. The size
trigger was unreachable by a factor of ~85: every batch would cut stale,
``Batch.reason`` would read ``age`` forever, and ``max_batch_size`` would
be dead configuration whose value could be anything without changing
behaviour. The design asks for the drip's cut-full-versus-cut-stale
counts precisely because this is what they were going to show.

.. list-table::
   :header-rows: 1

   * - Parameter
     - Adopted
     - Derived
     - Basis
   * - ``submission/max-batch-size``
     - 500
     - **60**
     - the measured arrival quantum — one wave, the largest quantity the
       evidence shows arriving at once
   * - ``submission/max-wait-seconds``
     - 60
     - **60**
     - unchanged: a design bound, not a tuning knob, and the measurement
       gives no reason to move it

60 keeps **both** triggers live, which is the property the defaults lose.
A full wave arriving together cuts as one array submission rather than
fragmenting into ten stale batches of six; a slow or partial drip still
cuts on age within the bound. Under steady drip a 60-unit batch fills in
~613 s, so age remains the binding trigger at that rate and size binds on
the burst.

Not derived from exposure structure (18 SCAs/exposure) deliberately:
``design/compute.md`` § Submission requires cadence "proportional to
arrival rate rather than exposure count".

Both values live in the parameter tree, so retuning stays a parameter
edit. The service **refuses to start** when they are absent rather than
falling back to the module defaults — a fallback would quietly restore
the configuration this evidence retired.

The four declared classes
*************************

**ADOPTED:** the four operational classes are "the declared, normative
set of kinds of work", and backfill and release reprocessing "are
declared ahead of implementation [...] and nothing may claim their names
meanwhile."

.. list-table::
   :header-rows: 1

   * - Class
     - State
     - Route / blocking reason
   * - ``prompt-processing``
     - implemented
     - ``science`` → prompt queue
   * - ``reference-construction``
     - implemented
     - ``reference-image`` → bulk queue
   * - ``historical-backfill``
     - **declared, not implemented**
     - belongs to the failure-path design as the resume mechanism of the
       pending state, which is not designed yet
   * - ``release-reprocessing``
     - **declared, not implemented**
     - belongs to the release machinery, which is not built yet

Declaring the unimplemented pair *with* their reasons is the point:
declaring them by omission is what would let a later reader add a
``backfill`` string somewhere and have it mean whatever their code does.
Asking to run one is refused at the command line — the argument does not
accept ``run`` — and again at the input layer and at operator
construction.

The operator's input
********************

The old operator took a processing **date**: ``sys.argv[1]``, defaulting
to today in Pacific time. Two wrong assumptions followed.

A date is not a window. Deriving a window from a date meant the operator
could only ask for a calendar day in one fixed timezone, and the smoke
run paid for it: the staged inputs occupy 2027-10-01 to 2027-10-07, and a
2026 window gathers **zero** units while still reporting 109 (field,
filter) pairs — "a confident 109 pairs followed by nothing to submit,
which reads like an empty pipeline rather than a wrong window"
(``smoke_run.rst``).

And one date said nothing about *which* work. There was no way to ask for
reference construction without also asking for prompt processing.

The input is now ``--start``/``--end`` (both required, no default window)
plus a disposition per declared class. The census is completed by
construction: a class the caller did not name is ``hold``, except the
unimplemented pair, which take ``declared-not-implemented`` so the record
distinguishes "the operator chose not to run this" from "this cannot
run".

Registration aborts at the item
*******************************

Each of the old operator's three registration steps ended with

.. code-block:: python

   if reg_run.failed:
       print(f"*** Error: {reg_run.failed} registration(s) failed; quitting...")
       dbh.close()
       exit(65)

so **one** attempt whose record could not be registered ended the whole
invocation — and the operator is a loop, so it ended every subsequent
pass too. Fourteen records in that state blocked every operator pass
during the smoke run: not fourteen failures, one failure repeated because
nothing could get past them to the work behind.

The consumer underneath was already correct — ``register_batch`` wraps
each attempt in its own transaction and its own ``except``, counts the
failure and carries on, per ``catalog.md`` § Promotion. The operator
threw that away.

``pipeline/operator/registration.py`` now returns a *verdict* instead:

.. list-table::
   :header-rows: 1

   * - Outcome
     - Exit
     - Meaning
   * - nothing failed
     - 0
     - the pass did its job
   * - some failed, some succeeded
     - **66**
     - partial: work is moving, triage the failures
   * - every attemptable item failed
     - **65**
     - total: systemic

Partial and total are different operator responses, so they are different
answers, and **neither stops the pass**. A failed item is recorded and
skipped, exactly as the granularity evidence asks.

The unit
********

``rapid_systems`` ``cloudformation/rapid-vpo-service.yaml`` — an SSM
Document plus a State Manager association (the reconciler's deployment
pattern) installing a Podman **Quadlet** unit (the ``rapid-postgres``
pattern). The generator turns ``rapid-vpo.container`` into a real
``rapid-vpo.service`` at ``daemon-reload``, which is what makes
``Restart=always`` and ``WantedBy`` mean something; that precedent was
set after an unsupervised podman container died to a routine reboot for
8.5 hours.

Two Quadlet specifics worth knowing: ``daemon-reload`` *creates* the
generated service rather than merely refreshing systemd's view, and there
is no ``systemctl enable`` — a generated unit is enabled through its own
``[Install]`` section, and ``enable`` would fail for want of a file to
symlink.

The service is **disabled by default**, one step beyond the reconciler's
reasoning: this service submits work, so an accidental enable is more
consequential than for anything else in the account. There is
deliberately no rehearsal parameter on the stack — a mode a deploy could
leave set is how a rehearsal flag becomes a production setting nobody
notices.

Verification, 2026-08-08
************************

.. list-table::
   :header-rows: 1

   * - Check
     - Result
   * - Operator unit tests, in-container on rapid-admin
     - 36 tests, **exit 0**
   * - Full operational suite, in-container
     - **1085 tests / 37 modules, PASS, exit 0**
   * - Rehearsal-cannot-submit refusal test + negative control
     - both pass; walk verified by hand against an ``if False:`` guard
   * - ``RAPID_VPO_DRY_RUN`` refused at startup
     - **exit 70**, naming the 5,057-child exhibit
   * - Unimplemented class asked to run
     - refused at the CLI, **exit 2**
   * - **Live rehearsal against the real database**
     - gathered **3,779 units**, cut **63 batches**, **0 submissions**,
       exit 0
   * - Queues before and after the live rehearsal
     - ``TOTAL_OPEN=0`` both times, by queue listing across all five open
       states
   * - Derived cadence live in the parameter tree
     - ``max-batch-size=60``, ``max-wait-seconds=60``
   * - Cadence observed firing
     - 62 of 63 batches cut on **size** at exactly 60 units — the size
       trigger reachable, which is what 500 had lost
   * - ``rapid-vpo-service`` stack deployed
     - exit 0, both the disabled branch (**VPO-ABSENT-OK**) and enabled
       (**VPO-INSTALL-OK**)
   * - ``rapid_systems`` ``validate.sh``
     - PASS, all layers, exit 0

The image, and what runs from it
================================

The service runs from ``rapid-pipeline:76da3e0-20260808``
(``sha256:a1dc4eb0…``), built by ``containers/rapid-pipeline/build.sh``
from smdc ``76da3e0`` over ``base-31237339531``. It is the first image
carrying ``pipeline.operator``: every earlier revision — including
``0e23431-20260808``, built from the commit this work branches *from* —
returns ``NO_OPERATOR`` for
``importlib.util.find_spec('pipeline.operator')``.

Verified **from the image itself, no bind mounts**: 38 operator tests
exit 0, and the full operational suite 1,085 tests / 37 modules PASS exit
0. Scan gate: Inspector reports 3 HIGH / 5 MEDIUM / 1 LOW — the same nine
CVEs by identity as the image already in production use, all base-layer.

.. list-table::
   :header-rows: 1

   * - Deployed check
     - Result
   * - Service active after convergence
     - ``active (running)``, **NRestarts=0**
   * - Supervised restart
     - PID 27092 → 27387, back to ``active (running)``, credential
       re-resolved and tree re-read, **NRestarts=0** after a further 20 s
   * - Live rehearsal from the deployed image
     - 3,779 gathered, 63 batches, **0 submissions**, exit 0; queues
       ``TOTAL_OPEN=0`` before and after
   * - Bounded-probe guard
     - width above the ceiling **refused exit 70**; width with no ceiling
       **refused exit 70**; rehearsal at ``--width 2`` capped to exactly 2
   * - Width-2 live probe
     - submitted one array job, ``submissions: 1``, exit 0

Four defects the live path found
================================

Everything below was found by running the real thing, and each is fixed
with a test that would have caught it.

**The service restart-looped when idle.** The stack's own defaults put
every class on ``hold``, the service logged "nothing to do", returned 0,
and ``Restart=always`` turned that into a restart every 15 s —
``NRestarts`` climbing while nominal. Exiting 0 is right for ``--once``
and wrong for a supervised service: every class held is a legitimate
operating state, and a health signal that fires under nominal operation
is not a health signal. The service now idles.

**Submissions carried no run id.** ``submit_units`` builds its manifest
with ``batch_id=run_id`` and ``publish_manifest`` refuses a manifest
without one, so the first probe died at "manifest has no batch_id".
The old operator always minted one per phase, so nothing reached that
guard until this operator replaced it. The id is now the accumulator's
own batch id — one manifest, one batch, one identity.

**The submission context was a rebuilt copy.** It put
``active_definition``'s raw dict where a ``SubmissionBinding`` belonged
and the probe died at ``binding.job_definition_arn``. It now delegates to
``submission_env``, which owns the contract: route → queue and
definition, family → its one ACTIVE revision, and the binding carrying
revision, digest and release identity. A wrong revision there makes the
reconciler report drift on every attempt, which is why that resolution
exists in one place.

**A live pass registered as a dry run.** With no registrar,
``run_registration`` passes ``dry_run=True``, so the first successful
probe reported ``would_register: 1087, registered: 0`` — decided
everything, wrote nothing. That is the defect the consumer already fixed
once (review finding #5), reintroduced one layer up by the operator that
replaced its caller. A live pass now builds the real registrar through
``production_registrar``, which binds it to the pass's own connection so
product rows and the watermark commit together; a rehearsal keeps None,
because a rehearsal that wrote registration rows would be a rehearsal
with effects.

Found live during this work
===========================

The first live rehearsal reached class dispatch and then died:

.. code-block:: text

   datearg = --start
   *** Error: Env. var. STARTDATETIME not set; quitting...
   exit 64

The gather step imported ``pipeline.virtualPipelineOperator`` to borrow
``mjd_window`` — and that module ran its whole startup at import, so the
import re-read the *new* operator's argv, took ``--start`` as the legacy
positional processing date, and demanded the environment interface this
restructure exists to retire.

Fixed twice over: the startup moved into ``_startup()`` called from the
``__main__`` block, so the module is importable without side effects
(every module-level name it binds declared ``global``, enumerated in full
— the phase logic reads them all by name); and the two helpers moved into
``pipeline/operator/gathering.py``, since the operator should not be
reaching into a script module for helpers. Both are covered by tests: one
imports the legacy module in a subprocess with a cleared environment and
fails if startup runs, the other asserts the operator does not import
helpers from it.

What the operator does not yet own
==================================

``pipeline/virtualPipelineOperator.py`` still holds the three phase
bodies. This work delivered the service shape, the declared classes, the
input, the rehearsal seam, the cadence and the registration granularity;
converting the phase logic itself is separate work, and until it lands
the operator borrows four things from that module — ``submission_env``,
``production_registrar``, ``active_definition`` and
``reference_window_override_for_run`` — deliberately, because each owns a
contract that must have exactly one implementation. Two of this
restructure's four live defects came from rebuilding such a contract
instead of borrowing it.

The payload the probe's children ran is the job definition's pinned
image, not the operator's. That is correct and unchanged: this
restructure changes the *submitter*, and moving the payload pin would
bump the Batch job-definition revision — deliberately left for the owner
by the environment-policy job, whose ``RAPID_JOB_DEFINITION_REV`` finding
this inherits.
