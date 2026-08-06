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

**AWS SSO authentication could not be established, and every remaining
W9 item requires it.**

``aws sts get-caller-identity --profile rapid-admin`` returned exit 255,
``Token has expired and refresh failed``, at session start and on every
retry through the session. Re-authentication was attempted four times:

* ``aws sso login --profile rapid-admin`` — twice (120 s and 240 s
  windows), both exit 124. The command opens a browser and blocks on a
  human completing the consent screen.
* ``aws sso login --profile rapid-admin --use-device-code --no-browser``
  — twice. Both printed a device code and blocked identically; the code
  requires a human to visit the verification URL and approve.

Both SSO flows are interactive **by design**: the authorization step is
a human consent, and an unattended worker has no way to supply it. The
browser-automation route was also unavailable (the Chrome extension
advertised its tools but reported "Browser extension is not connected").

No workaround was attempted, deliberately. Reaching for long-lived
access keys or another credential source to bypass an unfinished consent
would be routing around an authentication gate, which is exactly what
the approval rules forbid — and would have been a worse outcome than a
blocked run.

**No AWS state was mutated.** No Batch submissions, no S3 writes, no DB
rows, no stack updates, no job-definition revisions. The account is
exactly as ``w9prep`` left it: rev-13 on both definitions, the
reconciler service still on its own rev-12 pin.

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
* **The reconciler-service stack's own image pin**, still one revision
  behind the fix it needs (carried from ``w9prep``). The full parameter
  set must be pinned explicitly: a partial update reverted
  ``ReconcilerEnabled`` on 2026-08-06.
* **Battery closure** — the cases touched since W8, the owed
  scheduler-retry case forced against real ``AttemptDetail``, and a real
  end-to-end registration once PSFs exist.
* **The pooler RPM promoter publish**, blocked on the CI
  runner-registration gap. Owner: road map item 2 (CI/runner +
  credential chain), date-bound — the PAT expires 2026-09-02.

Note that rev-14's scope has *grown* by this session's two commits: any
rebuild must now carry them, and the ramp should not be run on an image
that predates the binding fix.

Ephemeral state left behind
-----------------------------

None on AWS — nothing was created there. Locally: scratch logs and a
backup copy of ``virtualPipelineOperator.py`` used for the mutation
check were written under this job's own ``tmp`` directory, which is
removed with the job. No files were written to ``/tmp`` shared space, no
scripts installed persistently, and the two worktrees are removed at
session end with their branches merged to base.
