.. _pipeline_execution:

RAPID Pipeline Execution
####################################################

Overview
************************************

The pipeline is **run by a supervised service, not by launch scripts**.

The Virtual Pipeline Operator (VPO) polls the operations database for ready
work, accumulates it, cuts batches on a cadence, and submits AWS Batch array
jobs. Each array child runs the payload entrypoint. The reconciler closes
out what Batch reports. The publisher delivers alerts. An operator asks for
work by stating **a window and a disposition for every operational class** —
not by running one script per phase.

.. note::

   This page previously documented a four-step procedure built on
   ``awsBatchSubmitJobs_launchSciencePipelinesForDateTimeRange.py``,
   ``parallelRegisterCompletedJobsInDB.py``,
   ``awsBatchSubmitJobs_launchPostProcPipelinesForProcDate.py`` and
   ``registerCompletedJobsInDBAfterPostProc.py``. Those scripts, and
   ``pipeline/virtualPipelineOperator.py``, **no longer exist**.
   :ref:`retired_four_step` below maps each old step onto what replaced it.

The design basis is ``design/operations.md`` § The Virtual Pipeline
Operator, ``design/compute.md`` § Submission, and ``design/security.md``
§ Job configuration. The restructure record — what each piece is and why it
has the shape it has — is :doc:`/dev/vpo_service`.

The processes
====================================

Five console entry points, declared in ``pyproject.toml`` and installed by
``pip install -e .``. Each is a new *name* for a ``main()`` that also still
runs as ``python3 -m <module>``.

.. list-table::
   :header-rows: 1
   :widths: 22 30 48

   * - Command
     - Module
     - Role
   * - ``rapid-operator``
     - ``pipeline.operator.service``
     - The VPO. Gathers ready work, cuts batches, submits Batch arrays,
       runs the registration pass.
   * - ``rapid-job``
     - ``pipeline.entrypoints.job``
     - The payload every AWS Batch array child runs.
   * - ``rapid-reconciler``
     - ``pipeline.reconciler.main``
     - Closes attempts from Batch state; writes terminal records.
   * - ``rapid-publisher``
     - ``pipeline.publisher.service``
     - Drains the alert outbox to the broker.
   * - ``rapidctl``
     - ``pipeline.operatorctl.main``
     - One-shot operator CLI: the audited surface for retries,
       terminations, supersession, garbage collection and views.

``rapid-operator``, ``rapid-reconciler`` and ``rapid-publisher`` are
long-running supervised services. ``rapid-job`` runs only inside a Batch
child. ``rapidctl`` is the only thing an operator invokes routinely by hand.

Configuration
************************************

Configuration is **two-tier**, and the split is enforced rather than
conventional.

**Per-invocation identifiers arrive in the environment.** For the services
that is an assumed-role ARN, a region and a database secret id; for a Batch
child it is which manifest and which array index.

**Everything else is read from the parameter tree** ``/rapid/pipeline`` at
startup, in one recursive ``get_parameters_by_path`` walk, and hashed into
the attempt record's configuration digest (``submission/startup.py``). Job
queues, job definitions, bucket names, cadence and science tuning all live
there. A job that cannot read its tree fails loudly at startup rather than
running on defaults, because a product whose configuration digest describes
configuration the job never used is worse than no product.

Environment read by ``rapid-operator``:

.. list-table::
   :header-rows: 1
   :widths: 34 66

   * - Variable
     - Meaning
   * - ``AWS_REGION`` / ``AWS_DEFAULT_REGION``
     - Resolved in that order, then the SDK session, then **raise**. Never
       silently defaulted.
   * - ``RAPID_ORCHESTRATOR_ROLE_ARN``
     - The role the operator chains into. Submission, the products bucket
       and the orchestrator secret are granted to this role, not to the
       host's instance role.
   * - ``RAPID_DB_SECRET_ID``
     - The **submitter's** database identity, ``rapid/db/service/orchestrator``.
       The tree's ``db/secret-id`` names the *pipeline* secret, which is the
       children's identity and is denied to the submitting role.
   * - ``DBSERVER`` / ``DBPORT`` / ``DBNAME``
     - Optional per-field overrides of the tree's ``db/server``, ``db/port``,
       ``db/name``. An explicitly-set variable wins so that an operator
       debugging against a replica is not silently returned to production.
   * - ``RAPID_VPO_POLL_SECONDS``
     - Poll interval; ``--poll-seconds`` overrides.
   * - ``RAPID_VPO_REHEARSE``
     - Equivalent to ``--rehearse``.

Database credentials are fetched from Secrets Manager **fresh at each
connection open**, never exported into the environment and never cached for
the life of the process, so a rotated secret takes effect on the next
connection rather than the next restart. ``DBUSER``/``DBPASS`` and
``AWS_ACCESS_KEY_ID``/``AWS_SECRET_ACCESS_KEY`` are not used in this
deployment.

.. warning::

   ``RAPID_VPO_DRY_RUN`` is **retired and refused at startup** (exit 70).
   It suppressed only registration writes while leaving submission on the
   path unconditionally, and a run believing itself a dry run submitted
   5,057 real children in 35 seconds on 2026-08-07. Use ``--rehearse``,
   which holds no submitting capability at all. See :doc:`/dev/smoke_run`.

Startup gates
====================================

Before the first poll, the operator opens one short read-only connection and
runs three fail-closed checks, in this order:

1. **Schema contract** — the migrations this build's SQL requires are
   applied. Checked first deliberately: run against a database missing them,
   the next check fails as a missing *row* when what is missing is a whole
   migration.
2. **Application contract** — the release identity and image digest the
   deployed unit is running under.
3. **Work-stream completeness** — every enabled stream has a workflow
   definition. Loading definitions is an operator action, never something a
   service does to itself.

Any of the three failing is a start failure (exit 70). This is why an
operator that cannot start is usually a deployment or migration problem, not
a code problem.

What the operator is asked for
************************************

The input is a **window** plus a **disposition per declared class**
(``pipeline/operator/inputs.py``). ``--start``/``--end`` are required
ISO-8601 datetimes, UTC where no offset is given; there is no default
window. The old operator took a processing *date* defaulting to "today in
Pacific time", which is how a run against staged 2027 inputs gathered
nothing while still reporting 109 (field, filter) pairs.

Dispositions are ``run``, ``hold``, or ``declared-not-implemented``. Every
class the invocation does not name defaults to ``hold`` — silence about a
class means "not this pass", never "whatever the code does by default". An
invocation is therefore a complete statement about all five classes.

.. list-table:: The five declared operational classes
   :header-rows: 1
   :widths: 28 14 58

   * - Class
     - State
     - Notes
   * - ``prompt-processing``
     - implemented
     - Fans out to eight job types: ``science``, the six post-DB chain
       types, and ``alert-production``.
   * - ``reference-construction``
     - implemented
     - One job type, ``reference-image``.
   * - ``test``
     - implemented
     - Campaign gathering. Submits under the science route (v1
       restriction); its registry key is deliberately distinct so it
       cannot collide with prompt processing's own science row.
   * - ``historical-backfill``
     - declared, not implemented
     - Belongs to the failure-path design as the resume mechanism of the
       pending state, which is not designed yet.
   * - ``release-reprocessing``
     - declared, not implemented
     - Belongs to the release machinery, which is not built yet.

Asking an unimplemented class to ``run`` is refused at the CLI (exit 2).
``declared-not-implemented`` exists so an operator can record that state
without it reading as a decision they made.

The class axis gates *which* classes run; the gatherer registry
(``pipeline/operator/gathering.py``) is what a running class fans out to.
One ``Operator`` is built per job type, each with its own accumulator and
submission context, because a batch is homogeneous in its validated route —
one job type, one queue, one definition per array submission.

The route matrix
====================================

Queue and job-definition *names* are not in the code; the route names the
parameter-tree key that holds them (``submission/routes.py``).

.. list-table::
   :header-rows: 1
   :widths: 30 14 20 18 18

   * - Job type
     - Class
     - Queue parameter
     - DB lane
     - ppid
   * - ``science``
     - prompt
     - ``batch/queue-prompt``
     - transaction
     - 15
   * - ``reference-image``
     - bulk
     - ``batch/queue-bulk``
     - transaction
     - 12
   * - ``registration``
     - prompt
     - ``batch/queue-prompt``
     - transaction
     - —
   * - ``reprocessing``
     - bulk
     - ``batch/queue-bulk``
     - transaction
     - 15
   * - ``catalog-load``
     - bulk
     - ``batch/queue-bulk``
     - **session**
     - —
   * - ``crossmatch``
     - bulk
     - ``batch/queue-bulk``
     - **session**
     - —
   * - ``statistics``
     - bulk
     - ``batch/queue-bulk``
     - transaction
     - —
   * - ``merge-currency-sweep``
     - bulk
     - ``batch/queue-bulk``
     - transaction
     - —
   * - ``source-currency-sweep``
     - bulk
     - ``batch/queue-bulk``
     - transaction
     - —
   * - ``merge-dedup``
     - bulk
     - ``batch/queue-bulk``
     - transaction
     - —
   * - ``alert-production``
     - prompt
     - ``batch/queue-prompt``
     - transaction
     - —

Only ``catalog-load`` and ``crossmatch`` hold the budgeted session lane;
everything else runs brief transactions whatever its scan cost.
``registration`` and ``reprocessing`` carry routes but are not gathered by
the operator — registration runs as a pass inside the operator itself (see
below), and reprocessing waits on the release machinery.

Running a pass
************************************

Run the operator on ``rapid-admin``, inside the pipeline container, never
from a laptop: the admin instance role has no ``GetObject``/``PutObject`` on
the products bucket and no ``GetSecretValue`` on the orchestrator secret, and
the dependency set the services import lives in the image rather than on any
host. Team policy puts containers on ``rapid-admin`` — never a laptop, never
``rapid-rusholme``.

Starting a pipeline container
====================================

In production nothing below is done by hand: the services run as supervised
Podman Quadlet units installed from ``rapid_systems`` (see *Continuous
operation*). This section is for the by-hand container — a rehearsal, a
bounded pass, ``rapidctl``, or a suite run against a working tree.

The host
------------------------------------

``rapid-admin``, reached by SSM ``AWS-RunShellScript``. It is a **shared**
host: name every container you start after your own run, ``--rm`` it, and
never touch a container you did not start — in particular never
``podman rm -f`` a service container, which under ``Restart=always`` is a
restart rather than a stop.

The image, and the login
------------------------------------

The operational image is the unified application image, built and
digest-pinned from the **infrastructure repository**
(``containers/rapid-pipeline/build.sh`` in ``rapid_systems``), and held in the
private ECR repository ``rapid-pipeline``. Run it **by digest** — the same
digest the Batch job definitions and the service units are pinned to. The tag
form (``rapid-pipeline:76da3e0-20260808``) is for reading, not for running.

Derive the account id at run time. Do not write it into anything in this
repository: ``Caltech-IPAC/rapid`` is public, and ``.githooks/pre-push``
hard-blocks the SMDC account number with no allowlist.

.. code-block::

   ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
   REGISTRY=${ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com
   IMAGE=${REGISTRY}/rapid-pipeline@sha256:<digest>

   aws ecr get-login-password --region us-east-1 \
       | podman login --username AWS --password-stdin "$REGISTRY"
   podman pull "$IMAGE"

The ECR login is not optional. The repository is private, and a ``podman run``
against it without a current login fails with ``authentication required`` —
the first of the three defects that stood between the reconciler unit being
enabled and it running, fixed there by an ``ExecStartPre`` login.

The digest currently pinned by the in-repository runners is the default
``IMAGE`` in ``scripts/run-operational-tests-on-rapid-admin.sh``; the deployed
services carry their own, pinned by their stacks.

The entrypoint
------------------------------------

The image sets ``ENTRYPOINT ["bash"]``, so an interactive shell takes **no**
trailing ``bash``:

.. code-block::

   podman run -it --rm --name "rapid-probe-$RUN_ID" \
       -e AWS_DEFAULT_REGION=us-east-1 "$IMAGE"

and a one-shot either overrides the entrypoint (``--entrypoint=""``, then the
command) or passes bash its own arguments (``-c '<script>'``). A command
appended after the image with neither is an argument *to bash* — which is how
a Batch ``command`` override once ran as a filename bash could not find.

What the container needs in its environment
--------------------------------------------

.. list-table::
   :header-rows: 1
   :widths: 34 66

   * - Variable
     - Why it has to be there
   * - ``AWS_DEFAULT_REGION``
     - ``resolve_region`` raises rather than defaulting, and nothing inside
       the image supplies it.
   * - ``RAPID_ORCHESTRATOR_ROLE_ARN``
     - the role the operator chains into; submission, the products bucket and
       the orchestrator secret are granted to it, not to the host.
   * - ``RAPID_DB_SECRET_ID``
     - ``rapid/db/service/orchestrator`` for the operator. A payload
       container uses ``rapid/db/service/pipeline`` instead.
   * - ``DBSERVER`` / ``DBPORT`` / ``DBNAME``
     - only to override the parameter tree per field. ``DBPORT`` is **6432**,
       the pgbouncer pooler — 5432 is not reachable off-host by design.
   * - ``RAPID_SW``
     - only when running a mounted working tree. The software root is
       fail-loud; nothing may default it to ``/code``.

**No credentials are passed in.** boto3 inside the container picks up
``rapid-admin``'s instance role from IMDS and chains from there.
``AWS_ACCESS_KEY_ID``/``AWS_SECRET_ACCESS_KEY`` and ``DBUSER``/``DBPASS`` are
not used in this deployment, and the database password never enters the
environment at all — it is fetched fresh at each connection open and passed to
``connect``, so nothing the process execs inherits it.

A one-shot rehearsal container
------------------------------------

.. code-block::

   podman run --rm --entrypoint="" --name "vpo-rehearse-$RUN_ID" \
       -e AWS_DEFAULT_REGION=us-east-1 \
       -e RAPID_ORCHESTRATOR_ROLE_ARN="$ORCH_ROLE_ARN" \
       -e RAPID_DB_SECRET_ID=rapid/db/service/orchestrator \
       "$IMAGE" \
       rapid-operator \
           --start 2027-10-01T00:00:00 --end 2027-10-08T00:00:00 \
           --reference-construction run \
           --prompt-processing hold \
           --test hold \
           --historical-backfill declared-not-implemented \
           --release-reprocessing declared-not-implemented \
           --once --rehearse

``rapidctl`` is the same shape with a different command. Where a console
script is not on ``PATH`` in the image you are running,
``python3.11 -m pipeline.operator.service`` is the identical ``main()``.

Drop ``--rehearse`` only after reading *A bounded live pass* below: that
container submits real work, and the width pair is what bounds it.

Running a working tree instead of the baked code
--------------------------------------------------

Mount it, set ``RAPID_SW`` at the mount point, and work there:

.. code-block::

   podman run --rm --entrypoint="" -v "$STAGE/repo":/w:Z -w /w \
       -e RAPID_SW=/w -e AWS_DEFAULT_REGION=us-east-1 \
       "$IMAGE" ./scripts/run-operational-tests.sh python3.11

``:Z`` relabels the mount for SELinux; without it the container reads nothing.
``scripts/run-operational-tests-on-rapid-admin.sh`` is the complete worked
example — it tars the tree, stages it through the build-artifacts bucket with
a checksum verified on the far side, and runs exactly this over SSM.

Networking, and cleaning up
------------------------------------

Default networking is correct on ``rapid-admin``: the container reaches the
AWS APIs and reaches ``rapid-db`` through the pooler on 6432.
``--network host`` is needed only when the pooler is on the *same* host as
the container, which is the case for the runners that execute on ``rapid-db``
itself.

``--rm`` disposes of the container; the image layers stay. ``rapid-admin`` has
a 64 G root and has been found carrying 47 cached pipeline image tags at once,
so remove a digest pulled for a one-off rather than leaving it for whoever
next meets a full disk.

The supervised units, for contrast
------------------------------------

``rapid-vpo.container`` is a Quadlet file; ``daemon-reload`` *generates*
``rapid-vpo.service`` from it, which is what makes ``Restart=always`` and
``WantedBy`` mean anything. There is no ``systemctl enable`` — a generated
unit is enabled through its own ``[Install]`` section — and the VPO is
disabled by default, because this is the service that submits work. Inspect
and cycle it with ``systemctl status rapid-vpo`` or
``systemctl restart rapid-vpo``, never with ``podman`` directly. The reconciler is deployed the
same way, by the ``rapid-reconciler-service`` stack. Design record:
:doc:`/dev/vpo_service`.

Rehearsal
====================================

A rehearsal gathers, accumulates and cuts batches, and reports what it
*would* have submitted. It **cannot** submit: ``RehearsalSubmitter`` holds
no Batch client, has no import of the submission seam, and is deliberately
not a subclass of ``LiveSubmitter`` — inheritance would put the submitting
method one ``super()`` call from reachable. This is a structural property,
verified by a test that walks the code objects reachable from the class,
not a flag that is checked at the right moment.

.. code-block::

   rapid-operator \
       --start 2027-10-01T00:00:00 --end 2027-10-08T00:00:00 \
       --reference-construction run \
       --prompt-processing hold \
       --test hold \
       --historical-backfill declared-not-implemented \
       --release-reprocessing declared-not-implemented \
       --once --rehearse

A rehearsal registers nothing either: it has no connection factory and no
registrar, because a rehearsal that wrote registration rows would be a
rehearsal with effects.

A bounded live pass
====================================

``--width`` caps the units each job-type operator gathers before they reach
the accumulator. It **requires** ``--max-width``, and a width above the
stated ceiling is **refused, never clamped** — clamping would submit
something under a number the caller did not choose. The cap is checked
before anything is gathered and before any client is built, and every drop
is logged with its count, because a silent cap reads exactly like a
complete run.

.. code-block::

   rapid-operator \
       --start 2027-10-01T00:00:00 --end 2027-10-08T00:00:00 \
       --reference-construction run \
       --prompt-processing hold \
       --test hold \
       --historical-backfill declared-not-implemented \
       --release-reprocessing declared-not-implemented \
       --once --width 18 --max-width 18

``--once`` runs a single pass and exits with the worst registration verdict
it saw. ``--force-cut`` cuts whatever has accumulated without waiting for
the cadence triggers, which a bounded probe generally wants — otherwise a
sub-cadence batch sits in the accumulator until the age trigger fires.

A width above the available population is not an error: the gathered list is
truncated, so a cap of 270 against a population of 109 simply submits 109.

Continuous operation
====================================

Without ``--once`` the operator polls, holding its accumulator across polls
so that batches are cut by size or age as work arrives, rather than by the
array ceiling alone. The cadence comes from the parameter tree; it has run
live at ``max-batch-size=60``, ``max-wait-seconds=60``.

A service with no class on ``run`` **idles quietly; it does not exit**.
Exiting 0 there is right for ``--once`` and wrong for a supervised service:
``hold`` on every class is a legitimate operating state and the deployed
stack's own default, and a unit that exits cleanly under ``Restart=always``
turns "nothing to do" into a restart loop — observed live on 2026-08-08,
restarting every 15 seconds while nominal.

In production the operator is not started by hand at all. It runs as a
Podman Quadlet unit installed by an SSM Document plus State Manager
association in the ``rapid_systems`` repository, and the service is
**disabled by default** — one step beyond the reconciler's own default,
because this is the service that submits work. There is deliberately no
rehearsal parameter on the stack: a mode a deploy could leave set is how a
rehearsal flag becomes a production setting nobody notices.

What a Batch child does
************************************

Each array child runs ``rapid-job --class {prompt|bulk}``. The class is
fixed by the job definition's command and has no default: a definition whose
command lost its discriminator would otherwise run as whichever class the
default named, which is the misconfiguration the route matrix exists to
catch.

The child identifies itself from the environment the submitter set —
``RAPID_MANIFEST_URI``, ``RAPID_MANIFEST_CHECKSUM``, ``RAPID_BATCH_ID``, and
Batch's own ``AWS_BATCH_JOB_ARRAY_INDEX`` — then:

1. Fetches the manifest and **verifies its checksum**. A mismatch means this
   child is reading a different manifest than the one that sized its array,
   so its array-index binding cannot be trusted.
2. Reads ``/rapid/pipeline`` and computes the configuration digest.
3. Validates the full route before any row is touched, so a submission with
   an invalid route never produces an attempt.
4. Resolves its own processing unit by array index and runs it.
5. Terminates through the termination protocol, which records the outcome.

The attempt and ``logical_jobs`` rows are pre-created by the submission seam
**before** the scheduler can start a child. A submitter that bypasses the
seam creates children the runtime's resolver cannot attribute, and the
registration gate refuses them at startup with exit 70.

Exit codes: ``0`` recorded, ``70`` unrecordable — nothing could be written,
so the failure goes to the safety stream (CloudWatch) and Batch reports
FAILED rather than SUCCEEDED over a job with no account.

Closing the loop
************************************

**Registration** is a pass the operator runs itself, once per poll — a
global sweep of every outstanding registration candidate, not a separate
submission and not scoped to a job type. Only the first operator of each
running class carries a registrar, so eight job-type operators do not run
the identical global sweep eight times a poll. Product rows and the
registration watermark commit in **one transaction** on one connection.

Its verdict is an exit code rather than a stop:

.. list-table::
   :header-rows: 1
   :widths: 30 12 58

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

Neither stops the pass. A failed item is recorded and skipped.

**The reconciler** (``rapid-reconciler``) polls Batch and closes attempts,
writing terminal records to the records bucket. It chains into
``RAPID_RECONCILER_ROLE_ARN`` and takes ``RAPID_RECONCILER_POLL_SECONDS``.

**The publisher** (``rapid-publisher``) drains the alert outbox to the
broker — the only component that talks to the broker at all. It chains into
``RAPID_PUBLISHER_ROLE_ARN``, takes ``RAPID_PUBLISHER_POLL_SECONDS``, and
reads its broker list and internal-topic prefixes from the tree.

Both preflight the schema on their own connection before their first poll,
so a schema mismatch is a start failure rather than a per-cycle exception.

Service exit codes
====================================

Shared vocabulary across the three services: ``0`` stopped cleanly, ``70``
could not start, ``71`` was working and stopped being able to (the
supervisor should restart it). ``66``/``65`` are the operator's registration
verdicts above.

Observing and intervening
************************************

``rapidctl`` is the operator surface: routine operation reaches the audited
mutation functions through a constrained procedure instead of handwritten
SQL at a ``psql`` prompt. Mutating subcommands require ``--reason`` and are
recorded in the mutation ledger; most take ``--apply`` and are a preview
without it.

Views (read-only):

.. code-block::

   rapidctl attempts --state <state>       # attempts in a state
   rapidctl show-attempt <attempt_id>      # one attempt in full
   rapidctl work-units [--state ...]       # work units
   rapidctl unit-events <work_unit_id>     # one unit's event history
   rapidctl unreconciled                   # what the reconciler has not closed
   rapidctl audit --limit 20               # the tail of the mutation ledger

Interventions:

.. list-table::
   :header-rows: 1
   :widths: 36 64

   * - Subcommand
     - What it does
   * - ``retry-parked``
     - Re-attempt parked work for a run, bounded by ``--max-attempts``.
   * - ``terminate-batch-jobs``
     - Terminate jobs on a queue in the named states.
   * - ``supersede-lost-evidence``
     - Supersede a run prefix whose evidence is unrecoverable.
   * - ``repair-refused-outbox``
     - Repair refused alert-outbox rows for a release identity.
   * - ``set-admission-release``
     - Move the admission release identity, guarded by ``--expect-current``.
   * - ``add-problem-category``
     - Declare a new problem category.
   * - ``break-glass open|close|reconcile``
     - The audited emergency session: opened loudly with a target scope,
       closed with the tables touched and changes made, and reconciled with
       the sweep's outcome.
   * - ``gc-compute-plan``, ``gc-plan``, ``gc-recompute-plan``, ``gc-approve-plan``, ``gc-execute-plan``
     - Garbage collection as a proposed, reviewed, approved and then executed
       plan — never a direct delete. ``--max-deletions`` is required.

Several subcommands take ``--expect-candidates``, ``--expect-jobs`` or
``--expect-absent``: the caller states what they believe they are acting on,
and a mismatch refuses rather than proceeds.

.. _retired_four_step:

What replaced the four-step procedure
****************************************

.. list-table::
   :header-rows: 1
   :widths: 44 56

   * - Old step
     - Now
   * - 1. ``awsBatchSubmitJobs_launchSciencePipelinesForDateTimeRange.py``
       with ``STARTDATETIME``/``ENDDATETIME``
     - ``rapid-operator --start ... --end ... --prompt-processing run``.
       The window is an argument, not an environment variable, and is
       required.
   * - 2. ``parallelRegisterCompletedJobsInDB.py <procdate>``
     - The operator's registration pass, run once per poll. Not a separate
       invocation and not keyed on a processing date.
   * - 3. ``awsBatchSubmitJobs_launchPostProcPipelinesForProcDate.py``
       with ``JOBPROCDATE``
     - The six post-DB job types, gathered by prompt processing's own
       fan-out in dependency order. Not a separate phase.
   * - 4. ``registerCompletedJobsInDBAfterPostProc.py <procdate>``
     - Also the registration pass; the reconciler closes attempts
       independently.
   * - Manually watching the AWS Batch console
     - ``rapidctl attempts`` / ``work-units`` / ``unreconciled``, and the
       reconciler's own error counters.

The processing date has not disappeared as a *product* concept — products
are still organized in S3 by :doc:`processing date </prod/products>` — but
it is no longer how work is selected. A date is not a window, and one date
said nothing about *which* work; the operator now takes both.

Observed performance
************************************

The numbers below are from the validation ramp and smoke run, not the
retired procedure. Full evidence: :doc:`/dev/w9_ramp`,
:doc:`/dev/smoke_run`, :doc:`/dev/vpo_service`.

**The validation ramp** ran three steps of real g0001 work through the
production VPO path: 18, then 90, then 109 children — **217 of 217
succeeded**, zero failures, zero unexplained records. Mean per-child
latency was 1310.6 s, 1309.5 s and 1323.3 s across the three steps. Step 3
asked for 270 and ran 109 because the staged subset holds 109 ready
reference units in its window, not because anything failed.

**The reference phase** ran one array at the whole population: 109 children,
109 SUCCEEDED, every child placed at once across 37 registered
``m6a.4xlarge`` container instances (~2.95 children per instance), drawing
436 vCPU against the bulk compute environment's 1,200.

**Cadence** was observed firing correctly at ``max-batch-size=60``,
``max-wait-seconds=60``: a live rehearsal gathered 3,779 units and cut 63
batches, 62 of them on the **size** trigger at exactly 60 units — the
trigger that a previous ceiling of 500 had made unreachable.

Testing before you run
************************************

.. code-block::

   pip install -e '.[test]'
   RAPID_SW="$PWD" scripts/run-operational-tests.sh    # stub tier, no database
   scripts/run-contract-tests.sh                       # PostgreSQL-backed tier

The stub tier runs each test module in its own interpreter: several install
third-party stubs into ``sys.modules`` at import time, and collecting them
into one process produces failures that belong to no module. See
``pipeline/contract/README.md`` for the contract tier.
