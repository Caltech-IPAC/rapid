W9: current state, as the next worker inherits it
==================================================

An inventory, not a narrative. W9 was scoped as the validation ramp —
the session's stated purpose — and **the ramp did not run**. This
document records what W9 did land, what it found, and what the next
worker inherits, so that the ramp can be picked up without re-deriving
any of it.

Supersedes ``w9prep_state_summary.rst``'s "What is still owed" list
where noted; leaves the rest of that document untouched.

Why the ramp did not run
-------------------------

Two different blockers, on two different runs. The first is resolved and
recorded here only so the second is not mistaken for it.

Run 1 (superseded): SSO expiry
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``aws sts get-caller-identity --profile rapid-admin`` returned exit 255,
``Token has expired and refresh failed``. Both ``aws sso login`` flows
(browser and ``--use-device-code``) block on a human consent an
unattended worker cannot supply. **No longer true**: ``rapid-admin`` is
warm on the current run, the SMDC account confirmed, exit 0.

Run 2 (current): the IMSS profile is ``ask``-gated, and the ramp's input is behind it
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``~/.claude/settings.json`` carries, in its ``ask`` list::

    "Bash(aws * --profile imss*)",
    "Bash(aws * --profile=imss*)"

That pattern matches **every** AWS call against ``imss-rapid-ro``,
read-only shapes included — there is no carve-out for ``sts
get-caller-identity`` or ``s3api list-objects-v2``. It is a deliberate
global rule, not a misconfiguration to be repaired in passing.

Eight consecutive unattended workers died on it, each hanging on an
approval prompt no one was present to answer: ``bcc79510``,
``a72165de``, ``90bd4263``, ``f48f18a5``, ``1e22379f``, ``c3387092``,
``7c90f4be`` (all on ``sts get-caller-identity``, at 60/90/120 s
timeouts), and ``16a23f43`` — which reached the real work command,
``aws s3api list-objects-v2 --bucket rapid-pipeline-files --prefix
refimage_psfs/ --profile imss-rapid-ro``, and hung there.

The delegation prompt asserted these shapes had been allow-listed and
that the credentials were "warm and verified". The credentials may well
be valid; that was never the failure. **The gate is the failure**, and
it is in the ``ask`` list where a supervisor put it.

No workaround was attempted, deliberately — the same reasoning run 1
applied to SSO. The available bypass here is concrete and was rejected:
the profile's static keys sit in ``~/.aws/credentials``, and exporting
them as ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` would evade
the ``--profile imss*`` pattern match entirely while performing exactly
the access the gate exists to mediate. A ``yolo`` grant suspends
ordinary approval gates; it does not convert an ``ask``-listed
credential boundary into an open one.

Why that blocks the ramp specifically
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The ramp submits real g0001 **science** work, and science has no input
without PSFs. W8 established the chain (``w8_battery.rst``, "What could
NOT be proven"): ``science`` requires both a PSF and a reference image;
``reference_image.download_reference_psf`` requires ``psf_uri``, which
comes from the ``PSFs`` table; ``PSFs`` and ``RefImages`` are both
empty.

Corroborated live this run, independently of that note::

    aws s3 ls s3://roman-rapid-references/ --profile rapid-admin
    (no output, exit 0 — the bucket is empty)

So the sole authorized source of the PSF set is the IMSS carry, and the
IMSS carry is behind the gate above. Items 1 (PSF carry), 3's live
end-to-end registration, and 4 (the ramp) are all downstream of it.

A caution for whoever picks this up: ``rapid-pipeline-queue`` **does not
exist**. Queries against it return ``0`` / ``[]`` at exit 0 — a silent
false clean. The real queues are ``rapid-queue-bulk`` and
``rapid-queue-prompt``; both were verified genuinely quiet (zero
RUNNING) this run.

**No AWS state was mutated on either run.** No Batch submissions, no S3
writes, no DB rows, no stack updates, no job-definition revisions.

What W9 did land
-----------------

The round-5 P1 — the one W9 item that is pure source and needs no
credentials — is fixed, tested and committed.

The execution binding names what actually ran
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``submission_env`` read the job-definition FAMILY from the parameter
tree (``rapid-pipeline-science``), submitted that **bare name**, and
recorded it as ``job_definition_arn`` beside a revision taken from a
process-wide ``RAPID_JOB_DEFINITION_REV``.

Two independent defects in one path:

1. **One revision, two families.** ``rapid-pipeline-science`` and
   ``rapid-pipeline-bulk`` revise on their own schedules. A single
   process-wide integer cannot describe both, so whichever value the
   environment held, at least one route class recorded a revision it did
   not run under.
2. **The bare name is unpinned.** Batch resolves a family name to
   whichever revision is ACTIVE at the instant of submission, and
   nothing recorded which one it picked.

This is not bookkeeping. ``ExecutionBinding.definition_identity``
synthesizes ``<name>:<rev>`` when the recorded ARN carries no revision
suffix — which, on the production path, was always — and the reconciler
compares its observation of the real job against it
(``service.py:625``). So the recorded identity disagreed with the job
that actually ran, and **drift was recorded against attempts that were
correct**. Demonstrated rather than argued::

    observed (real ARN) -> 'rapid-pipeline-science:14'
    expected (synth)    -> 'rapid-pipeline-science:7'
    EQUAL? False

At ramp scale that is one false positive per attempt, against a gate
that requires zero unexplained terminal records. **The ramp would have
been measuring this defect.** Running it before the fix would have
produced a step-1 gate failure whose cause was the instrument, not the
system under test.

``active_definition`` now resolves the family to its one ACTIVE
revisioned ARN at env build — selecting by exact name, refusing
ambiguity rather than guessing, taking the last (highest) ACTIVE
revision as a bare-name submission would have. The same versioned ARN is
both submitted and recorded, so the two cannot drift apart. The revision
is resolved, never declared.

Commit ``0da2ff3``.

The test that blessed the bare name
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``test_the_binding_recorded_is_the_definition_submitted_to`` asserted
only that submitted == recorded — which two identical **bare** names
satisfy trivially, both unpinned. It now asserts submitted == recorded
== a *versioned* ARN, checks each family against its own resolved
revision, and leaves the stale ``RAPID_JOB_DEFINITION_REV`` set to a
deliberately wrong ``7`` so that any code still reading it fails loudly
instead of passing by coincidence.

Four cases added: per-family revisions differ; the describe call is
filtered by exact name and ACTIVE status; the recorded identity equals
what the reconciler will observe; and ambiguous / absent families are
refused.

**Mutation-checked.** Restoring the bare-name path fails 7 assertions;
the fixed path is ``OK`` on all 22.

Why the whole routing test class was silently not running
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Found while diffing suite results against the ``smdc`` baseline:
``SubmissionEnvRoutingTests`` **errored in its entirety** under
``python3 -m unittest discover`` at baseline — six tests — while passing
when the module was run alone. Cause: ``submission_env`` eagerly built a
real ``boto3.client('s3')`` it never used for the resolution, which
raises with no region in scope.

That is why the weak assertion above survived unnoticed: in any
full-suite context, the tests that would have questioned it were not
executing. Both clients are now injected, and resolving a binding needs
neither credentials nor a region.

Suite counts, laptop interpreter, ``discover`` from the repo root:

===================  =======  ==========  ========
Tree                 tests    failures    errors
===================  =======  ==========  ========
``smdc`` baseline    661      28          23
``w9-ramp``          666      28          13
===================  =======  ==========  ========

Zero tests fail in ``w9-ramp`` that do not also fail at baseline
(``comm -13`` on the sorted failure sets is empty). The error drop
23 → 13 is the six routing tests plus four others that hit the same
eager-client problem, now actually running.

The remaining 28 failures / 13 errors are pre-existing and
environmental: all 50 occurrences trace to ``module 'psycopg2.sql' has
no attribute 'SQL'`` — a stub psycopg2 on the laptop interpreter.
Identical at baseline; not W9's to fix, and precisely why RAPID policy
runs suites in-image on rapid-admin rather than on the laptop.

Dead code in the reconciler's drift comparison
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``_definition_identity`` carried eight unreachable lines below its
``return`` — a copy of ``_enum``'s body, referencing ``value`` and
``enum_class``, which the function does not take. Confirmed dead by AST
(statements after the ``Return`` node), not by eye. Removed; behaviour
unchanged, since the lines could never execute.

Note the docstring still promises ``<name>:<revision>`` normalization
while the body only strips the ARN path prefix. That is now *correct in
practice* because the binding records a versioned ARN — but it is a
latent trap if anything ever reintroduces a bare name, and it is left
as-is rather than rewritten, because changing the comparison semantics
belongs with the VPO co-design, not with a blocked ramp.

Commit ``3b5f521``.

What is still owed
-------------------

Everything W9 was scoped to do beyond item 0, all AWS-gated, all
inherited unchanged:

* **The validation ramp** (18 → 90 → 270 array children of real g0001
  work through the production VPO path), with its per-step gates and
  the latency/throughput evidence doc ``w9_ramp.rst``. This is the
  road map's next input: "VPO / operations co-design — fed by the W9
  ramp's evidence." Owner: next W9-equivalent worker, with credentials.
* **The PSF carry** — locate ``refimage_psfs/`` in IMSS
  ``rapid-pipeline-files``, copy to the SMDC destination, sha-pin
  provenance in ``rapid_systems``, register in the PSFs table. Still
  blocking the science / reference-image / post-process live proofs.
* **rev-14** — rebuild at smdc tip sweeping FixD/FixE **and now the two
  fixes above**, scan gate, definition revision with quiesce first.
  Deliberately **not** built this run; see "Why rev-14 was not built"
  below.
* **The reconciler-service stack's own image pin** — this entry is
  **stale as written and is corrected here.** The stack is *not* a
  revision behind. Observed live 2026-08-06::

      aws cloudformation describe-stacks --stack-name rapid-reconciler-service
      ImageRef          ...rapid-pipeline@sha256:3bcd8978...  (= rev-13 digest)
      ReconcilerEnabled true
      StackStatus       UPDATE_COMPLETE   2026-08-06T18:50:33Z

  Definitions ``rapid-pipeline-science`` and ``rapid-pipeline-bulk``
  (both rev 13) carry that same digest. The live system is therefore
  **self-consistent across all three pins, with the reconciler
  enabled** — W9prep evidently closed this and the note was not updated.
  The full-parameter-set caution still stands for any future update: a
  partial update reverted ``ReconcilerEnabled`` on 2026-08-06.
* **Battery closure** — the cases touched since W8, the owed
  scheduler-retry case forced against real ``AttemptDetail``, and a real
  end-to-end registration once PSFs exist.
* **The pooler RPM promoter publish**, blocked on the CI
  runner-registration gap. Owner: road map item 2 (CI/runner +
  credential chain), date-bound — the PAT expires 2026-09-02.

Note that rev-14's scope has *grown* by this session's two commits: any
rebuild must now carry them, and the ramp should not be run on an image
that predates the binding fix.

Why rev-14 was not built
-------------------------

rev-14 was mechanically available on the current run — ``rapid-admin``
warm, the build host ``i-0ce2eebb8133ab63d`` ``running`` and SSM
``Online``, both queues quiesced, and the rebuild explicitly authorized
(one build, ≤2 iterations, scan gate). It was **not** built, as a
recorded conservative decision under the unattended decision rule.

The reasoning, so it can be overridden knowingly rather than re-derived:

* rev-14's purpose in the W9 scope is to be *the image the ramp runs
  on*. With the ramp PSF-blocked and the battery's live cases blocked
  with it, a rev-14 would be deployed and then exercised by nothing.
* The deploy is not a single reversible act. It revises both job
  definitions, updates the reconciler-service stack, and re-runs the
  association — three coupled pins that are, right now, **mutually
  consistent and enabled** (evidence above). The known failure mode is
  live and recent: a partial update reverted ``ReconcilerEnabled``
  earlier the same day.
* Trading a coherent, understood "source fixed, deployment pending"
  state for an "deployment changed, unvalidated" one — unattended,
  with no ramp to catch a regression, inside the launch window — is
  the worse of the two handoffs.

The binding fix is committed and on ``smdc``; nothing is lost by
building rev-14 in the same session that can actually run the ramp,
which is the session that has IMSS access. **Recommended: build rev-14
and run the ramp together, once the PSF carry is unblocked.**

What would unblock the next run
---------------------------------

One decision by the owner, not more engineering. Either:

1. **Move the read-only IMSS shape from ``ask`` to ``allow``** in
   ``~/.claude/settings.json`` — narrowly, e.g. ``Bash(aws s3api
   list-objects-v2 --bucket rapid-pipeline-files *)`` and the
   corresponding ``s3 cp``/``sync`` for ``refimage_psfs/`` — leaving the
   blanket ``aws * --profile imss*`` gate otherwise intact; or
2. **Perform the carry themselves** (or with a supervised session
   present to answer the prompt), landing the PSF set in the SMDC
   destination and registering it, after which the ramp needs no IMSS
   access at all.

Option 2 is the smaller change to the security posture; option 1 is the
one that makes future unattended ramps repeatable. Either way the
downstream work — sha-pinned provenance in ``rapid_systems``,
registration in ``PSFs``, rev-14, the battery's live cases, and the
ramp — is unblocked and already specified.

Ephemeral state left behind
-----------------------------

None on AWS — nothing was created there, on either run. Locally: run 1
left scratch logs and a backup copy of ``virtualPipelineOperator.py``
under its own job ``tmp`` directory, removed with the job. Run 2 wrote
one scratch probe script under the delegate job directory and removed
it (verified by ``ls``); it created no AWS resources and made no
mutating call of any kind. No files were written to ``/tmp`` shared
space, no scripts installed persistently, and the two worktrees are
removed at session end with their branches merged to base.
