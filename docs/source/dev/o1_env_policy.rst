O1 — environment-variable policy enforcement
=============================================

**All six work items landed.** The environment no longer carries a
credential, a science-affecting value, or a silent default anywhere on the
operational path, and a checked-in script decides those three prohibitions
mechanically rather than leaving them to review.

The one-line state: **the policy is enforced by code and by a check that is
proved able to refuse, not by a convention.**

Scope, and where it came from
------------------------------

``rapid_plan`` ``research/operations-codesign-assembly.md`` § E, row "O1
env-policy enforcement", against the ratified policy text in §§ A/B/B2 —
folded into ``design/code-standards.md`` § Environment variables and
``design/compute.md`` § Payload contract / § Job definitions. This is that
row.

.. list-table::
   :header-rows: 1
   :widths: 4 40 56

   * - #
     - Item
     - Outcome
   * - 1
     - Connection helper: explicit interface; retire the environment writes
     - **Done.** ``connect`` takes ``endpoint=``/``credentials=``; the
       entrypoint and the reconciler pass what they hold. Proved live.
   * - 2
     - Region resolution per policy
     - **Done.** ``environment.resolve_region``; the reconciler's hardcoded
       ``us-east-1`` fallback is gone.
   * - 3
     - Software root fail-loud at every payload read site
     - **Done.** The two ``os.environ.get("RAPID_SW", "/code")`` reads in the
       stage modules now resolve through a raising accessor.
   * - 4
     - Retire the runtime job-definition-revision requirement
     - **Done.** The baked value is advisory; ``mark_started`` falls back to
       the row's own execution binding.
   * - 5
     - STARTREF/ENDREF deleted; window to release content + manifest override
     - **Done.** Manifest schema 3 carries the sole enumerated override.
   * - 6
     - Mechanical tail
     - **Done**, with one part left deliberately: see "What was not done".

What changed
------------

**The connection helper gained a parameter interface, and two callers
stopped writing the environment.** ``rapid_db_connect.connect`` takes an
``Endpoint`` and a ``Credentials``; what is not passed is read from the
process's own environment at its boundary, which is what a standalone
script still does. The deciding case was the reconciler, which resolved a
credential under its service role and then published it as ``DBUSER`` /
``DBPASS`` so the helper could read it back — a plaintext password in the
environment of everything the service execs. It now passes it. The payload
entrypoint did the same with the endpoint and the secret id, read from the
parameter tree; it now fetches the credential under the job role and passes
that too.

``Credentials.__repr__`` redacts the password. The default would print it
into any log line or traceback frame holding the structure, which is the
same exposure by another route.

**Region resolution is the policy's order, then a raise.**
``pipeline.runtime.environment.resolve_region`` resolves ``AWS_REGION``,
then ``AWS_DEFAULT_REGION``, then the SDK session, then raises naming the
variable. The reconciler's ``os.environ.get("AWS_DEFAULT_REGION",
"us-east-1")`` was the only production silent default, and it contradicted
the region-portability rationale stated in the master ``.ini`` itself: a
reconciler deployed anywhere else reconciled against a region holding none
of its work and reported nothing wrong.

**The software root fails loud at the two sites that defaulted it.**
``pipeline/stages/science.py`` and ``pipeline/stages/reference_image.py``
resolved ``RAPID_SW`` with a ``/code`` fallback at module scope while every
other operational read exited 64. They now call
``science_config.software_root()``, which raises. Per call rather than at
import, so importing a stage module for a test or a doc build does not
require the variable.

**The baked job-definition revision is advisory.** ``build_provenance`` no
longer requires ``RAPID_JOB_DEFINITION_REV``; ``Provenance.job_definition_rev``
defaults to ``None``; ``mark_started`` writes
``COALESCE(%s, binding_job_definition_rev::text)``. Provenance authority
for the executing revision is the submission-time execution binding, which
the submitter resolves from Batch. Two things this fixes beyond the policy:
no Containerfile in ``rapid_systems`` sets the variable, so the entrypoint
required something nothing supplied; and the Batch stack's job-definition
entry carries the definition NAME, not a revision — a field named for one
thing holding another, written into the ``job_definition_rev`` column. The
column now takes the authoritative value.

No migration was needed. The DDL requires ``job_definition_rev IS NOT NULL``
at ``started``, and the COALESCE satisfies it from a column the row already
carries.

**The reference-image observation window is release content with one
override carrier.** ``STARTREFIMMJDOBS`` / ``ENDREFIMMJDOBS`` are deleted
from the operational path. ``get_overlapping_l2files`` takes the window as
parameters; ``cdf/science/pipeline.toml`` ``[ref_image]`` holds the
authoritative pair; ``submission.gathering.reference_observation_window``
resolves override-then-release; and manifest schema 3 carries
``overrides.reference_observation_window`` as the sole enumerated field.
An override changes the manifest checksum, which is what makes "recorded by
construction" true — the checksum is bound into every attempt row, so a
promotion gate can refuse a product built under one.

The environment path had a second defect worth recording: setting only
``STARTREFIMMJDOBS`` was a caught error, but setting neither silently
produced ``[0.0, mjdobs)`` — a different window from either, chosen by
absence. ``ReferenceObservationWindow`` requires both ends and refuses an
empty interval.

**The schema bump is a deployment fact, checked rather than assumed.**
``Manifest.SCHEMA_VERSION`` is 3, and ``from_dict`` refuses any other
version rather than guessing the layout — so an image carrying this change
cannot read a version-2 manifest. Version-2 manifests do exist in
``s3://roman-rapid-products/submissions/``, written by the Q8 smoke run.
They belong to finished runs: at the time of this change there were no
``RUNNING`` jobs on ``rapid-queue`` (checked, read-only), so no in-flight
child could meet the refusal, and a new submitter writes only version 3.
The refusal is loud either way, which is the safe direction for a
disagreement about a layout — but the safe direction is not the same as no
consequence, and re-driving an old manifest requires the old image.

What an adversarial pass found
-------------------------------

The diff was reviewed against itself before merge. Three findings, all
fixed here, and all of a kind the test suite could not have caught because
each one only shows up on a failure path or under an operator action:

1. **A missing region was reported as a credential failure.**
   ``resolve_region()`` was called inside the entrypoint's credential
   ``try/except``, so an unset region raised ``ConfigError`` and came back
   out as "could not resolve the database credential ... under the job
   role" — sending an operator to Secrets Manager and IAM for a problem
   that is neither. The resolution is hoisted above the ``try``, and the
   secret's own malformed-payload case gets its own message naming the
   missing key.

2. **The reconciler's endpoint precedence was inverted.** The rewritten
   ``_database_endpoint`` read ``parameters.get(...) or os.environ.get(...)``
   — tree first — while the comment three lines above it, and the code it
   replaced, both promised the opposite. Failure mode: an operator sets
   ``DBSERVER`` to a replica, restarts, and is silently connected to
   production. Fixed, and pinned by ``pipeline/reconciler/test/test_main.py``,
   a module that did not exist before because this file's startup work used
   to be environment mutation rather than values a test can assert on.

3. **``CRDS_PATH`` was given a compiled-in default of ``/tmp/crds_cache``.**
   That is the shape this row fails loud on everywhere else, and the
   plausible value was worse than none: a Batch container's ``/tmp`` does
   not survive the attempt, so every retry would have silently re-downloaded
   the reference set while looking configured. Only ``CRDS_SERVER_URL``
   keeps a fallback — there is one right answer for Roman — and the policy
   check's carve-out narrowed to match, so a future write of ``CRDS_PATH``
   fails it.

Validation
----------

The operational suite, in the pipeline image on rapid-admin via SSM
(``scripts/run-operational-tests-on-rapid-admin.sh``; team policy puts
containers there, never the laptop):

.. code-block:: console

    $ ./scripts/run-operational-tests-on-rapid-admin.sh
    1028 tests across 35 modules
    RESULT: PASS
    EXIT=0

Baseline on ``smdc`` was 998. Thirty tests added, none removed, none
skipped.

The policy check, which decides the three mechanical prohibitions over the
operational path:

.. code-block:: console

    $ ./scripts/check-env-policy.sh
    ENV-POLICY-OK
    EXIT=0

**Its negative control is the reason to believe it.** The first version of
that script dropped every line containing a string literal — nearly every
line of real code — and reported a clean tree with a deliberate two-rule
violation sitting in it. A checker that cannot fail verifies nothing, so
the refusal path now runs on every invocation against a probe the script
writes and removes, and the run fails unless all five checks catch it and
prose reaches none of them.

The live probe for the interface the unit suite can only stub
(``database/modules/utils/test/run-explicit-interface-on-rapid-db.sh``, on
rapid-db because that is the host whose instance role may read the secret):

.. code-block:: console

    $ ./database/modules/utils/test/run-explicit-interface-on-rapid-db.sh
    PASS  explicit/connected-with-an-empty-environment
    PASS  explicit/no-password-in-environment
    PASS  boundary/refuses-an-empty-environment
    PASS  boundary/still-connects-from-the-environment
    LIVE-EXPLICIT-OK
    EXIT=0

It clears the environment around the explicit connection, so a connection
that succeeded by falling back to the boundary read cannot pass it. It
writes nothing — two connections and three ``SELECT``\ s.

``rapid_systems``:

.. code-block:: console

    $ ./cloudformation/validate.sh --local
    validate.sh --local: PASS — all local layers ran
    EXIT=0

What was not done, and why
---------------------------

**The ``[AWS_BATCH]`` block was retired in place, not deleted.** Its sole
remaining reader is ``aws/list_aws_batch_jobs.py``, an operator query tool
explicitly outside the policy's scope. Deleting the block breaks that tool;
the block's actual defect was claiming to be the operational source when
the SSM parameter tree became authoritative, so the header now says so and
names the condition for deleting it. The stale ``min_n_images_to_coadd``
row beside it WAS deleted — release content is its single home, and the
test that compared the two now asserts the ``.ini`` copy stays gone.

**``RAPID_JOB_DEFINITION_REV`` is still set by the Batch job definitions.**
Removing an ``Environment`` entry registers a new revision of both
definitions, and the rev-21 pin record is Q8 evidence that must not move.
Nothing requires the value now, so leaving it costs nothing; removing it
belongs to whatever change next has cause to register a revision. Recorded
in ``rapid-batch.yaml`` as proposed.

**The VPO has no operator input for the override yet.**
``reference_window_override_for_run()`` returns ``None`` and is the seam an
operator-input surface fills. Deliberately not an environment variable —
that is the path this row deleted — and the VPO's operator interface is O4's
scope. An override today is set by a caller constructing a
``ReferenceObservationWindow`` and passing it to ``submit_units``.

**The orchestrator's four environment writes remain.**
``JOBPROCDATE``, ``MAKEREFIMAGESFLAG``, ``STARTDATETIME``, ``ENDDATETIME``
are the policy's one named temporary exception — the orchestrator's
interface to its four post-DB subprocesses — which expires when those
become bulk-queue job types (O3). ``check-env-policy.sh`` allows exactly
these four by name, so the policy's "no new environment transport may be
added under it" is enforced rather than stated: a fifth fails the check.

**The standalone analysis script keeps its own window variables.**
``analyzeSciencePipelineProductsForDateTimeRangeWithRefImageWindow.py`` is
invoked by nothing and is outside the operational path; its reads are its
own arguments and are already fail-loud. It is excluded from the policy
check BY NAME rather than by pattern, so promoting it into the pipeline
fails the check until its window is converted.

For the design authority
-------------------------

Two findings that belong to the ratified text rather than to this code:

1. **``RAPID_JOB_DEFINITION_REV`` never carried a revision.**
   ``rapid_systems`` ``cloudformation/rapid-batch.yaml`` sets it to the
   definition NAME (``rapid-pipeline-science`` / ``rapid-pipeline-bulk``),
   deliberately and with a comment explaining why, and that string was
   written into the ``job_definition_rev`` column of every started attempt.
   O1 makes it harmless — the column now takes the binding's revision —
   but any attempt row written before this change carries a name where the
   schema says revision.

2. **The reference window's silent default was a third behaviour.** With
   neither variable set the window was ``[0.0, mjdobs)``, not the
   "everything ever observed" the gathering layer separately asked for with
   its own sentinel. Two callers of one query disagreed about what "no
   window" meant. Release content now states one pair and both callers read
   it.
