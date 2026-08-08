Hardening ahead of the smoke run
================================

**All six ratified items landed, and the boundary audit found seven more
defects of the family it was called for.** Six of the seven are live or one
call site from it; one is a design question, recorded and parked. Every fix
carries a regression test, and every regression test was proved by reverting
its fix and watching the test fail.

The one-line state: **the smoke run's technical gates were already met; these close the
known ways the run could have failed quietly.**

Scope, and where it came from
------------------------------

The disposition batch of 2026-08-06 (``rapid_plan`` ``decisions.md``
§ Batch payload co-design; ``migration/plan.md`` § The worker queue) ratified
a pre-smoke-run hardening worker with four items, plus two corrections. This is that
run.

.. list-table::
   :header-rows: 1
   :widths: 4 40 56

   * - #
     - Item
     - Outcome
   * - 1
     - Serialization-boundary audit
     - **Done.** Seven defects found, six fixed, one parked as a design
       question. Inventory and findings below.
   * - 2
     - Health counts only actionable-unclosed work
     - **Done.** Waiting attempts no longer count against health; a closure
       step that tried and failed still does.
   * - 3
     - Per-attempt log-group derivation, both queue-group read grants
     - **Done.** Derived from the attempt's own recorded job-definition
       binding. Both grants already existed; verified live.
   * - 4
     - Sibling-repo proposed→ratified marker flips
     - **Done.** Three flipped against named ratifications; the rest listed
       below and deliberately left alone.
   * - 5
     - Correction — the runner claim
     - **Done.** CI runs on GitHub-hosted runners; re-verified live.
   * - 6
     - Correction — the stale gate tail
     - **Done.** ``rapid-batch`` is deployed, not pending review.

The serialization-boundary audit
---------------------------------

Method
~~~~~~

The audit was scoped by the defect FAMILY recorded in
``review_disposition.rst``, not by file. Four shapes, each an instance of one
error — *a value crossing a serialization edge is read as something it is
not, or a missing fact is read as an answer*:

1. **Wrong type across the edge.** The ``'null'`` string sentinel that, once
   parameterized, made PostgreSQL parse ``a.rid IS NOT 'null'``.
2. **``None`` into something that requires a value.** ``ExecutionBinding(...,
   manifest_checksum=None)`` against a ``__post_init__`` that rejects every
   empty field.
3. **An absent remote attribute accepted via a weaker comparison.** An S3
   object with no ``ChecksumSHA256`` treated as identical because its LENGTH
   matched.
4. **A parameter accepted and ignored.** ``submission_env(job_type)``
   returning one queue/definition pair for every job type.

Boundaries inventoried
~~~~~~~~~~~~~~~~~~~~~~

Every point in the smdc payload/operational layer where a value crosses an
edge: environment variables, JSON and manifest serialization, DB parameter
binding, subprocess command construction, parameter-tree reads, S3 metadata,
and log/record field population.

Modules read in full: ``submission/`` (all six), ``pipeline/seams.py``,
``pipeline/virtualPipelineOperator.py``, ``pipeline/entrypoints/``,
``pipeline/runtime/``, ``pipeline/reconciler/``, ``pipeline/registration/``,
``observability/``.

The audit was run as two independent passes over disjoint halves of that
surface, and every finding was re-verified in this session against the actual
definitions — dataclass ``__post_init__`` bodies, function signatures, SQL
method bodies, the live DDL — before anything was changed. Several findings
were additionally reproduced by executing the code.

Findings
~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 4 30 66

   * - #
     - Where
     - What, and what was done
   * - 1
     - ``virtualPipelineOperator.py`` (the completion wait)
     - **Family 3, and the most serious.** ``wait_for_submitted`` read
       ``getattr(submission, "run_id", None)``. ``Submission`` has never had
       a ``run_id`` — its run-scoped identity is ``batch_id``, which is what
       ``seams._precreate`` stamps into the attempt row's ``run_id`` column
       and what ``wait_for_completion`` queries. So the getattr returned
       None on EVERY submission: every batch was skipped with a warning and
       the operator proceeded to registration over jobs that were still
       running. That is the failure round-3 finding #3 fixed, reintroduced
       by reading the right value under the wrong name.

       Proved by executing ``dataclasses.fields(Submission)``: the seven
       fields are ``batch_id, job_id, job_name, array_size, manifest_uri,
       manifest_checksum, manifest``, and ``hasattr(Submission, 'run_id')``
       is False.

       **Why the tests missed it:** the test defined its own ``Submission``
       stub with a ``run_id`` attribute, docstringed "just the attribute
       ``wait_for_submitted`` reads off a real submission" — a double that
       asserted the belief instead of checking it. Fixed to build the real
       frozen dataclass. Reverting the production line now fails two tests
       that used to pass.
   * - 2
     - ``runtime/boundaries.py`` ``head()``
     - **Family 3, root cause of three consumers.** S3 omits
       ``ChecksumSHA256`` for any object written without one, written with a
       different algorithm, or uploaded multipart (whose stored value is a
       composite of part digests, not the object's SHA-256). ``head()``
       returned ``{"checksum": None}`` for all of them, and three consumers
       read that None as a verdict — see 2a–2c.
   * - 2a
     - ``boundaries.put_if_absent``
     - Compared ``existing["checksum"] == digest``, never true against
       None, so a crash-recovery replay of BYTE-IDENTICAL content raised
       ``StorageError`` claiming "two writers under one attempt identity" —
       permanently. That is the create-once path the whole termination
       protocol rests on, and the attempt could never re-run to completion.
       **Reproduced in-session** against a client whose HEAD omits the
       checksum. Now fetches the bytes and compares them, which is the only
       thing that settles it.
   * - 2b
     - ``runtime/termination.upload_bundle``
     - The replay branch propagated the None into ``bundle_checksum``, a
       field typed ``str`` — so the terminal record cited a bundle by a null
       checksum, unverifiable by any consumer. Now computes the digest from
       the bytes.
   * - 2c
     - ``reconciler/closure.read_predecessor``
     - ``if stored and stored != computed`` skipped the comparison entirely
       on a falsy stored value: validation by mere presence, which the
       function's own docstring says it never does. Absence is now logged as
       what it is, and the body is still identity-checked.
   * - 3
     - ``observability/attempts.mark_terminal_without_start``
     - **Family 2.** Accepted ``scheduler_state=None`` and issued the
       UPDATE. Migration 017's CHECK requires ``scheduler_state IS NOT
       NULL`` and the signature types the parameter ``str`` with no default,
       but ``_validate_scheduler_state`` tolerates None BY DESIGN — that is
       how ``record_scheduler_observation`` withholds a state on a submitted
       row — so it could not enforce it. PostgreSQL refused the row, the
       reconciler counted a per-row error and retried the identical
       statement every poll: a permanently stuck attempt diagnosed by a
       constraint name rather than the missing fact. Reachable, because
       ``observation.state`` is ``job.get("status")``. Now refused at the
       write boundary with a message naming the fact.
   * - 4
     - ``reconciler/retention.retention_class_for``
     - **Family 3.** ``scheduler_state in (None, "SUCCEEDED")`` admitted
       silence as agreement. So an attempt claiming success whose scheduler
       identity never resolved — the contradictory case the reconciler flags
       for a human — had its diagnostics filed under the SHORTER expiry, on
       the one path (``_reconcile_unresolved`` → ``_stamp_bundle`` with no
       observation) where the bundle is the only account that exists. Only
       ``"SUCCEEDED"`` agrees now.
   * - 5
     - ``runtime/termination.terminal_record_key``
     - **Family 1, latent.** Formats its sequence ``:04d``.
       ``attempts.terminal_record_sequence`` is a numeric column and
       psycopg2 hands numerics back as ``Decimal``, which passes the
       negativity guard and then fails with "invalid format string". Coerced
       once, after the guard. Fourth Decimal-from-psycopg2 defect here.
   * - 6
     - ``reconciler/reconstruction.build_reconstructed_bundle``
     - **Family 1, latent-but-plumbed.** Serialized its manifest with
       ``default=str``, which retypes ``attempt_stages.duration_ms``
       (``numeric NOT NULL`` → ``Decimal``) to a STRING under every consumer
       that reads it. That is the exact failure ``termination._json_default``
       's own docstring warns against; the fix had been applied to
       ``ClosureRecord.to_bytes`` and missed here. Now uses the shared
       coercion policy.
   * - 7
     - ``submission/gathering.py``
     - **Family 2.** ``float(facts.mjdobs)`` on a fact ``UnitFacts``
       documents as optional and ``science_facts`` builds with
       ``_maybe_float`` — which exists precisely to let a NULL
       ``L2Files.mjdobs`` through. Only ``rid`` was guarded, so the failure
       was a bare ``TypeError`` from inside a three-argument call, naming
       neither the field nor the unit. Guard extended to the facts actually
       dereferenced, naming every missing one at once.

The doubles that could not refuse
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Findings 1 and 2 shared a cause beyond the code: **no test could express the
state that breaks them.**

``InMemoryObjectStore`` and the S3 adapter's fake client both always produced
a checksum, so real S3's routine absence was unreachable from any test. Both
can now express it, and the in-memory store decides through ``head`` exactly
as ``S3ObjectStore`` does rather than comparing its own stored digest — which
kept it honest by construction and blind to the case the real one has to
handle.

This is the code-standards rule ratified in the same batch: **test doubles
must be able to refuse.** A double that grants an attribute the real object
lacks, or that cannot represent a value the real service returns, blesses the
defect it was written to catch.

Health semantics
----------------

The ratified disposition: the reconciler's health surface counts only
**actionable-unclosed** work.

``poll_once`` scored ``classified + deferred + errors`` as work attempted,
and treated a poll that attempted something and closed nothing as
unproductive. But ``"deferred"`` covered three states that are not failures
to work — an attempt still RUNNING, one terminal but inside its grace
horizon, and an unresolved child inside its submission-anchored horizon. The
reconciler owes those nothing yet.

W9 ran the reconciler at ``NRestarts=15`` for exactly this reason: during a
ramp step's first ten minutes every poll saw nothing but horizon-deferrals
and was scored as having failed to close them, tripping a check meant for a
service that cannot work.

Those three now return a distinct ``waiting`` outcome that health does not
count. A closure step that TRIED and failed still returns ``deferred``, still
increments ``_closure_failures``, and still degrades health on the same
threshold. Both halves are pinned by test:

.. code-block:: text

    ActionableWorkHealthTests
      test_a_full_step_of_waiting_attempts_never_degrades_health
      test_a_persistent_closure_failure_still_degrades_health

The first drives eighteen children — a ramp step's first poll exactly — for
twice the threshold in polls, and asserts ``waiting == 18``,
``deferred == 0``, ``consecutive_unproductive_polls == 0`` and
``healthy``. Before the change that run tripped the threshold.

.. code-block:: console

    $ python3 -m unittest pipeline.reconciler.test.test_service
    Ran 71 tests in 0.027s
    OK
    EXIT=0

Per-attempt log-group derivation
---------------------------------

Live state, re-verified this run:

.. code-block:: console

    $ aws ssm get-parameters-by-path --path /rapid/pipeline/logs --recursive
    (empty — logs/job-log-group did not exist)

    $ aws logs describe-log-groups
    /aws/batch/job
    /rapid/batch/rapid-queue-bulk
    /rapid/batch/rapid-queue-prompt

So the reconciler fell back to ``/aws/batch/job``, which holds no RAPID job
logs — and which the orchestrator cannot read:

.. code-block:: console

    $ aws iam simulate-principal-policy \
        --policy-source-arn .../rapid-orchestrator-role \
        --action-names logs:GetLogEvents \
        --resource-arns .../log-group:/aws/batch/job
    "D": "implicitDeny"

Every reconstruction that needed a log therefore got nothing, silently.

**No parameter value could have fixed it.** The two class-fixed job
definitions log to two different groups, so whichever one a single parameter
named, attempts of the other class would be read from a group that does not
hold their streams:

.. code-block:: console

    $ aws batch describe-job-definitions --status ACTIVE
    rapid-pipeline-science  rev 19  ->  /rapid/batch/rapid-queue-prompt
    rapid-pipeline-bulk     rev 19  ->  /rapid/batch/rapid-queue-bulk

The derivation therefore reads the attempt's own
``binding_job_definition_arn``, which the row already carries. That is the
fact at its source — the job definition is what names ``awslogs-group`` in
``rapid-batch.yaml`` — so nothing infers a workload class and **no schema
change is needed**. The revision is dropped because every revision of one
definition logs to the same group. A row with no binding, or naming a
definition the tree does not map, falls back and says so.

Both grants
~~~~~~~~~~~

Already present, and verified rather than assumed:
``rapid-orchestrator-role``'s ``BatchSafetyStreamRead`` statement covers
``/rapid/batch/*``, which is both groups.

.. code-block:: console

    $ ... --resource-arns .../log-group:/rapid/batch/rapid-queue-prompt
    "D": "allowed"
    $ ... --resource-arns .../log-group:/rapid/batch/rapid-queue-bulk
    "D": "allowed"

No IAM change rode with this item.

Deployment
~~~~~~~~~~

Two parameters added to ``rapid-pipeline-params.yaml``, keyed beside the
definitions they belong to. **1 of the 2 permitted deploy attempts.**

.. code-block:: console

    $ ./cloudformation/validate.sh --local
    validate.sh --local: PASS — all local layers ran
    EXIT=0

    $ ./validate.sh rapid-pipeline-params.yaml rapid-pipeline-params
    "Status": "CREATE_COMPLETE"
    Add JobLogGroupBulk / Add JobLogGroupPrompt   (no replacements)
    EXIT=0

    $ ./deploy-stack.sh rapid-pipeline-params rapid-pipeline-params.yaml
    Successfully created/updated stack - rapid-pipeline-params
    EXIT=0

End-to-end against the live tree — reading the real parameters and resolving
a real job-definition ARN of each class:

.. code-block:: console

    derived map from the LIVE tree:
      rapid-pipeline-bulk     -> /rapid/batch/rapid-queue-bulk
      rapid-pipeline-science  -> /rapid/batch/rapid-queue-prompt
    service-wide fallback: /aws/batch/job
    per-attempt resolution:
      rapid-pipeline-science  -> /rapid/batch/rapid-queue-prompt
      rapid-pipeline-bulk     -> /rapid/batch/rapid-queue-bulk
    distinct groups resolved: 2
    PASS: two classes, two groups, neither the unreadable fallback
    EXIT=0

Marker flips
------------

Flipped — each against an explicit naming in the register's
§ Batch payload co-design disposition list:

.. list-table::
   :header-rows: 1
   :widths: 34 66

   * - Marker
     - The ratification it was verified against
   * - ``rapid_systems`` ``D-tessellation-computed``
     - "the tessellation-computed form superseding the bake in the sibling
       record" → RATIFIED 2026-08-06.
   * - ``rapid_systems`` ``D-tessellation-baked``
     - Same clause: its supersession was ratified with the record that
       supersedes it → SUPERSEDED, no ratification of its own needed.
   * - ``rapid`` ``review_disposition.rst`` ``cattype``
     - "the cattype vocabulary (1=SExtractor, 2=PhotUtils — verified against
       the deleted monolith in git history)" → RATIFIED 2026-08-06.

Also updated, because they are this run's own work rather than markers
awaiting someone: the three ``w9_state_summary.rst`` rows for the boundary
audit, the health check and the log-tail group now read **ratified; DONE**.

**Left as proposed, deliberately.** No named ratification covers these, so
they stay as they are — the instruction is to flip only what the register or
the plan explicitly names:

* ``rapid_systems`` ``docs/questions.md`` — the wait-escalate matrix, the
  reconciler IAM grant, the service-SG split and scoped Glue grant,
  ``PROPOSED-012-attempt-record-grants.sql``, the RPM respin, the
  startup-fetch cache, the alert-archive and shared-role decision records,
  the launch-readiness contract.
* ``rapid_systems`` history records — ``kafka/topic``'s ``.v1`` suffix, the
  ``submission/max-*`` starting values, the pooler timeout starting values,
  the ``rapid-reconciler-service`` rev-12 redeploy.
* ``rapid`` ``config_homes.rst`` — the PROPOSED parameter values,
  ``db/server``, and the W4 line at 252.
* ``rapid`` ``w6_completion_evidence.rst`` / ``w6b_state_summary.rst`` — the
  IAM grant left proposed for want of a single host that can make it.
* ``rapid`` ``w8_battery.rst`` / ``w9prep_state_summary.rst`` — the rebuild
  pickup and the follow-up authorized to touch the reconciler stack.
* ``rapid`` ``review_disposition.rst`` line 199 and 855 — the standardisation
  decision and the "full W8 battery cannot complete from rapid-db" note, both
  addressed to the owner.

One item is ratified but NOT applied: **removal of the ineffective
coadd-inputs bucket policy** (``rapid-storage-buckets.yaml``
``InputsGbtdsSimCoaddInputsPolicy``). Deleting a live IAM policy is outside
this run's authority, which covers marker flips rather than resource
deletions. The template comment now records it as ratified-and-pending rather
than as a W9 authority gap. It is a one-resource removal plus a redeploy and
needs no code change — nothing reads the grant, which is the point.

Corrections
-----------

The runner claim
~~~~~~~~~~~~~~~~

``w9_state_summary.rst`` recorded "still zero registered self-hosted runners;
the promoter cannot be retried". The count was right and the inference was
wrong. Re-verified live 2026-08-07:

.. code-block:: console

    $ gh api repos/Caltech-IPAC/rapid/actions/runners
    {"total_count":0,"runners":[]}

    $ grep -n "runs-on" rapid_systems/.github/workflows/*.yml
    validate.yml:36:    runs-on: ubuntu-latest
    build-postgres.yml:34:    runs-on: ubuntu-latest
    auto-bump-pins.yml:45:    runs-on: ubuntu-latest
    build-rpms.yml:  (six jobs)  runs-on: ubuntu-latest

Every workflow is GitHub-hosted. The count is zero because no self-hosted
runner is wanted, not because one is missing, and the promoter is retriable
like any other workflow. The pooler half of that row stands and its closure
is road-map step 2b.

Observed while verifying, and NOT part of this scope: the most recent
``build-rpms`` run (#31122582925, 2026-08-06T17:15Z) shows ``failure``, but
all seven of its jobs are ``cancelled`` or ``skipped`` — a cancellation, not
a build failure. ``cancel-in-progress`` is ``false`` for that workflow, so it
was not auto-superseded. The last run that actually executed is the
13:24Z success. Recorded here because "all workflows green" is the
ratification's premise and this is the one run that does not read that way at
a glance.

The stale gate tail
~~~~~~~~~~~~~~~~~~~

``rapid_systems/cloudformation/README.md``'s Batch row ended "Deployment
pending review gate". Retired — the stack is live:

.. code-block:: console

    $ aws cloudformation describe-stacks --stack-name rapid-batch
    rapid-batch  UPDATE_COMPLETE  2026-08-07T02:35:04Z

Parked — a design question
---------------------------

**The science-configuration digest cannot distinguish a date from its string
spelling** (``pipeline/runtime/science_config.py``). ``load_with_digest``
canonicalizes with ``json.dumps(..., default=str)``, and TOML natively
produces ``datetime``/``date``/``time``. Two consequences, both verified by
execution:

* A ``date`` in a science parameter reaches stages as a ``str``.
* ``digest({"when": date(2026,1,1)})`` and ``digest({"when": "2026-01-01"})``
  return the **identical** hex digest.

The module's docstring calls that digest "a direct, checkable statement of
what the science configuration *was*", and two materially different
configurations collide to one identity.

**Why this is parked and not fixed.** Changing the encoder changes the digest
for any release whose TOML carries a temporal value, so previously-recorded
``science_config_digest`` values would stop reproducing. That is a change to
an adopted provenance contract, which is the owner's call, not this run's.

The options, for the ruling:

1. **Canonicalize temporal types explicitly** (ISO-8601 with a type tag) —
   correct, and invalidates existing digests for any release carrying one.
2. **Refuse temporal types at load** — the digest stays honest and no
   existing digest moves, at the cost of forbidding a TOML type.
3. **Leave it**, and record that the digest is a digest of the *rendered*
   configuration rather than the configuration.

Cheap to fix now if no shipped ``cdf/science/pipeline.toml`` carries a
date/time, and expensive later. This run did not survey the release content
to answer that, because the answer does not change whose decision it is.

Validation
----------

A repo suite for the operational layer did not exist; ``scripts/run-
operational-tests.sh`` is it. Each module runs in **its own interpreter** —
several install third-party stubs into ``sys.modules`` at import time, so
collecting them into one pytest process produces ~77 failures that belong to
no module.

Baseline, on a detached checkout of ``smdc`` at ``c81cd03`` with the same
interpreter, so that every failure on the branch is attributable:

.. code-block:: console

    $ ./scripts/run-operational-tests.sh <python>
    982 tests across 35 modules
    RESULT: PASS
    EXIT=0

Final, on ``preq8-hardening``:

.. code-block:: console

    $ ./scripts/run-operational-tests.sh <python>
    998 tests across 35 modules
    RESULT: PASS
    EXIT=0

Sixteen tests added, no test removed, nothing skipped.

``rapid_systems``:

.. code-block:: console

    $ ./cloudformation/validate.sh --local
    validate.sh --local: PASS — all local layers ran
    EXIT=0

No Batch job was submitted
---------------------------

The cap allowed one verification array of ≤18 children. None was run, and
none was needed: every payload change here is decidable without submitting
anything.

* The completion-wait fix is decidable at the attribute boundary — what
  ``Submission`` carries is a fact about the class, proved by reading its
  fields, and the test now constructs the real one.
* The health change is a pure function of a poll summary, driven directly at
  eighteen-child width.
* The log-group derivation is a pure function of the row's recorded binding,
  proved against the LIVE parameter tree and the LIVE job definitions.
* The remaining fixes are unit-level and each has a test that fails when its
  fix is reverted.

Submitting jobs would have proven the same facts more slowly and less
precisely — the same argument FixE's own live-evidence section makes. The
image was therefore **not repinned**: the cap permits a repin only where code
changes require a verification job, and none did.
