Q8 — the smoke run
==================

**The run stopped short of its exit criteria.** Phase A prep completed and
was verified; the reference phase passed at full population; a science step
at width 109 passed; then an operator process that was believed dead
submitted the remainder of the science population in one 35-second burst,
breaching the run's submission cap by roughly 3.5x. The run was stopped on
that breach and on the failures the burst produced.

What the overrun bought is a real defect of exactly the class this run
exists to find, and a stress test of the attempt-record machinery that no
planned ramp would have applied.

The one-line state: **prep is live and correct; the reference phase and one
science width are proven; the science ramp is unrun; a science-path
configuration defect is open and unfixed.**

Prep (Phase A)
--------------

Four gates, all verified before any submission.

**Temporal-type refusal.** ``load_with_digest``'s digest canonicalizes with
``json.dumps(default=str)``, so an unquoted TOML date and the same date
quoted produced one digest for two materially different configurations
(register § Science-config digest temporal types, ADOPTED 2026-08-07). The
refusal landed in ``load()`` — the single ingest point, so every caller is
covered — walking nested tables and arrays and raising ``ConfigError``
naming the offending key in dotted form.

Operational suite: **1,008 tests across 35 modules, PASS, exit 0**, from a
998-test baseline. Ten added, none removed, none skipped. Verified in-image
that the refusal is present, that the shipped release still loads through
it, and that ``forced_photometry.d_earliest`` is a ``str``.

**Image rebuild and repin.** Two build iterations against a cap of three.

.. list-table::
   :header-rows: 1

   * - Iteration
     - smdc
     - Tag
     - Digest
   * - 1
     - ``ef36940``
     - ``ef36940-20260807``
     - ``sha256:31a9fa9d…``
   * - 2
     - ``1b86a3f``
     - ``1b86a3f-20260807``
     - ``sha256:89223d4b…``

Iteration 2 was required by a defect iteration 1 exposed (below). Scan gate
on both: Inspector2 ``ACTIVE/SUCCESSFUL``, findings **CVE-identical** to the
digest replaced — 0 CRITICAL, 3 HIGH, 5 MEDIUM, 1 LOW, all base-inherited
from the unchanged ``base-30984903893``. No new HIGH/CRITICAL over baseline,
so the gate condition was empty at both.

Both job definitions and the reconciler service repinned to iteration 2:
**revisions 21**, reconciler unit file on the same digest, service active
and polling with ``errors: 0``.

**The coadd-inputs bucket-policy removal.** Applied — and a correction
belongs in the record. It was reported deployed once before it was: the
template had been edited and validated, but ``rapid-storage-buckets`` was
never deployed. The policy was still attached to ``rapid-orchestrator-role``
at that point. Now genuinely removed: one ``Remove`` in the changeset, stack
``UPDATE_COMPLETE``, policy deleted, and the role left carrying only
``rapid-db-service-orchestrator-read``.

**Gate checks.** EBS aggregate: 150 GiB per host, ~68 hosts at the planned
540-child fan-out (memory binds packing), plus 4,594 GiB already in use —
**~14.4 of 50 TiB, 29%**; worst case ~42%. Staged input: bucket parameter
applied, ``g0001/`` present, **5,166 L2 files** registered — the cited
science width. Pooler reachable. Attempt records flowing.

Capacity preflight confirmed at point of use: **5,000 on-demand vCPU**, live
read, against 2,160 required at the 540 step.

Two defects found in prep
--------------------------

**Every rapid-batch deploy silently re-enabled both Spot compute
environments.** The template declared ``State: ENABLED`` because AWS Batch
refuses to *create* a CE disabled; the live environments were disabled
post-create, and the pair sat in deliberate drift (L-batch-spot-state).
The consequence nobody had recorded: an update re-asserts the template's
value, with **no diff in the changeset** — ``State`` was unchanged in the
template, so nothing appeared to review. Observed live: a deploy touching
only the image digest returned both CEs to ENABLED. No capacity launched
(desired vCPUs 0, zero registered ECS instances, both queues order
on-demand first), but that is an ordering consequence, not a safety
property.

Fixed by declaring ``State: DISABLED`` in the template, so an update
converges to the intent. The API's create-time refusal becomes a documented
rebuild step — create ENABLED, then update to DISABLED — paid once at
rebuild rather than silently reversed at every deploy. **Verified: a full
subsequent deploy left both Spot CEs DISABLED.**

**The coadd-input list was written to a legacy bucket.**
``gather_reference_units`` was handed ``job_info_s3_bucket_base`` from the
master ``.ini`` — ``rapid-pipeline-files``, an IMSS-era bucket this account
does not carry — so every reference unit failed at ``PutObject`` with
``AccessDenied``. The key was already correct
(``submissions/<run_id>/coadd-inputs/``) and the submitting identity is
granted exactly ``roman-rapid-products/submissions/*``: the grant was right
and the bucket was wrong. Fixed to take the bucket from the submission
context's ``manifest_bucket``, so the coadd-input list sits beside the
manifest that cites it. One value, one home.

The VPO database credential
---------------------------

The operator could not fetch a database credential, which blocked every
phase. Probed rather than inferred:

.. list-table::
   :header-rows: 1

   * - Probe
     - Result
   * - Secret exists, correct shape
     - ``rapid/db/service/pipeline`` → ``{username, password}``
   * - Readable by SSO admin
     - yes
   * - Readable by ``rapid-admin-instance-role``
     - **AccessDenied**
   * - Readable by ``rapid-orchestrator-role``
     - **AccessDenied**
   * - AWS credentials reach the container
     - yes, as the instance role

The denial was correct behaviour, not a gap: the design assigns the
orchestrator **its own** credential, ``rapid/db/service/orchestrator``, and
correctly denies it the pipeline's. Resolution needed **no IAM change** —
the ``rapid-db-service-orchestrator-read`` policy already granted
``GetSecretValue`` on exactly that one secret. The operator dispatch chains
into ``rapid-orchestrator-role`` and reads its own secret.

Verified alongside: migration 016 applied 2026-08-06 13:06;
``rapid_orchestrator`` has login; grants present through
``rapid_pipeline_write`` membership (``attempts`` INSERT/SELECT/UPDATE,
``attempt_stages`` INSERT/SELECT).

The reference phase — PASSED
-----------------------------

Run ``vpo-2026-08-07-refimage-163453``, array ``45541b00``, one array at the
whole population: **109 children, 109 SUCCEEDED, 0 FAILED**.

All 109 attempts terminal as ``application_closed`` / ``success`` /
``published``, ``error_category`` NULL. Zero unexplained records.

Provenance chain verifiable end to end: release identity ``q8-smoke``,
source ``1b86a3f``, container ``sha256:89223d4b``, definition
``rapid-pipeline-bulk``. Reference construction routes to **bulk** by the
adopted queue mapping; smoke-run.md's "through the prompt queue" describes
its own two science-shaped phases.

Concurrency and packing
~~~~~~~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1

   * - Measure
     - Value
   * - Peak concurrent RUNNING
     - 109 — every child placed at once
   * - Registered container instances
     - 37
   * - Children per instance
     - ~2.95
   * - Instance family
     - ``m6a.4xlarge`` only (16 vCPU / 64 GiB), 37 of 37
   * - vCPU draw at peak
     - 436 against the bulk CE's 1,200

Packing is bound by the **memory reservation, not consumption**: at 16 GiB
reserved per child, 4 children exhaust a 64 GiB host while drawing 12 of its
16 vCPU. Observed resident use was ~3 GiB on a host carrying 3 children —
roughly **5x over-reservation**. Right-sizing would fit materially more
children per host on the same fleet. A job-definition question; recorded,
not acted on.

Only one instance family was selected, so the six-family spread the
evidence table asks about is not exercised.

Pooler draw
~~~~~~~~~~~

Sampled at 109-wide concurrency: **23 backend connections, 7 of them
payload**, against ``max_connections`` 200 — ~12% of the ceiling. The
transaction-mode pooler multiplexes rather than opening a backend per child,
which is the property the 6432 lane exists to provide. Payload connections
carry a per-attempt ``application_name``
(``rapid-payload:<array-job>:<child>``), so draw is attributable to
individual attempts.

Children connect briefly and return the connection, so instantaneous draw
understates peak contention. This establishes the shape, not the bound.

Scratch I/O
~~~~~~~~~~~

On a host carrying 3 concurrent children: **20 GiB of 150 GiB used (14%)**,
131 GiB free; memory 3 GiB of 61; buff/cache 17 GiB. The 150 GiB gp3 sizing
is nowhere near binding for reference construction, and nothing here argues
for instance store.

Interval decomposition
~~~~~~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1

   * - Stage
     - avg s
     - max s
   * - ``psf_catalog``
     - 662.6
     - 706.8
   * - ``build_reference_image``
     - 143.4
     - 148.3
   * - ``upload_products``
     - 6.7
     - 6.9
   * - ``coverage_and_uncertainty_statistics``
     - 4.5
     - 4.9
   * - ``sextractor_catalog``
     - 3.9
     - 8.6
   * - ``image_statistics``
     - 2.1
     - 2.1
   * - ``add_header_keywords``
     - 0.5
     - 0.5
   * - ``measure_fwhm``
     - 0.1
     - 0.1
   * - ``download_reference_psf``
     - 0.1
     - 0.2

``psf_catalog`` dominates at ~78% of a child's stage time and 4.6x the next
stage — the reference phase's critical path, and why a child runs ~20 min
rather than ~4.

The tight avg/max spread on both heavy stages says the fleet was **not
contended** at this width: hosts were not stealing time from each other, so
the packing figures above are a clean measurement rather than a
load-distorted one.

Registration and coverage
--------------------------

The reconciler classified all 109 only **after** the array reached terminal
(``waiting`` 109 → 0 across seven polls, ``errors: 0`` throughout).

**An operational lesson worth stating:** registration run before reconciler
closure finds zero candidates and reports a vacuous success. ``candidates()``
selects on the reconciler having closed and published a closure record; a
"0 registered, no errors" result is indistinguishable from a real pass
unless the ordering is known. Registration must follow classification.

Registration then ran as a production pass. It also swept older
reconciled-but-unregistered ``w9-ramp`` attempts — correct by contract, and
the designed behaviour proving itself: reconciler-closed work registers
whenever registration next runs. ``mark_registered`` advances a monotonic
``record_sequence`` under a CAS that cannot move backwards, which is what
makes replay safe.

Result: ``refimages`` **1 → 327** rows, all ``status=1``, across 109 distinct
fields and 2 filters; 327 attempts carry ``registered_at``.

Coverage check — PASSED
~~~~~~~~~~~~~~~~~~~~~~~

Matched on the key differencing actually uses, field **and** filter:

.. list-table::
   :header-rows: 1

   * - Ramp step
     - Children
     - field+filter pairs
     - Covered
   * - 1
     - 18
     - 18
     - 18/18
   * - 2
     - 180
     - 108
     - 108/108
   * - 3
     - 540
     - 108
     - 108/108

Complete at every step. No no-reference boundary risk.

The StageTwo overrun
--------------------

Timeline, from records rather than recollection:

.. list-table::
   :header-rows: 1

   * - Time (UTC)
     - Event
   * - 16:34
     - Dry-run VPO launched for run ``163453``
   * - 16:37
     - Reference array starts; 109 children
   * - ~17:20
     - Reference array SUCCEEDED 109/109
   * - ~17:23
     - Same process continues into science StageOne; 109 children
   * - **17:26:14–17:26:49**
     - **StageTwo submits 5,057 children in 11 batches — a 35-second burst**
   * - ~17:47
     - VPO process (pid 614538, alive 1h26m) killed; queues drained

**The submission was complete roughly 20 minutes before the kill.** The kill
was effective at the moment it ran — no StageTwo work was pending
afterwards, both queues empty, no operator process — but the burst was long
finished. Nothing about the kill's timing could have prevented it once the
process was left running.

The cause is the operator's pass structure: reference → wait → register →
science StageOne → science StageTwo, where StageTwo submits the **remainder**
of the science population. A process believed to have ended with its
reference array had two more submitting stages ahead of it.

Two compounding factors, both recorded rather than softened:

* ``RAPID_VPO_DRY_RUN`` gates **only registration**, not submission. A
  "rehearsal" switch that still submits real Batch work is a trap; the flag
  was set for the entire run.
* The hypothesis that the process would die at its own registration failure
  was wrong for the dry-run path. It did not die; it ran on.

**The queue-state-versus-attempt-table lesson.** Immediately after the kill,
both Batch queues read zero in every non-terminal state, and that was
reported as "no StageTwo array was submitted." The queues were telling the
truth about a different question: the jobs had already run and drained. The
**attempt table was the system of record** — 5,057 rows, every one carrying
a ``scheduler_job_id``, so submitted rather than merely pre-created. Drained
queues answer "what is pending now", never "what was submitted".

Cap accounting
~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1

   * - Population
     - Children
   * - Reference phase
     - 109
   * - Science StageOne
     - 109
   * - Science StageTwo
     - 5,057
   * - **Total**
     - **5,275**

Against a cap of **≤1,500** — exceeded by ~3.5x.

The defect StageTwo found
--------------------------

Every one of the 2,158 attempts that reached a terminal application state
failed identically, at one stage:

.. code-block:: text

    stage:          resample_reference_image
    error_category: internal_error
    error_message:  'swarp_header_only'

Every preceding stage succeeded on all 2,158 — ``download_inputs``,
``gunzip_science_image``, ``reformat_science_image``,
``science_image_catalog``, ``science_image_statistics``,
``measure_reference_fwhm``, ``resolve_reference_image`` all ``success``;
``inject_fake_sources`` ``skipped``.

``swarp_header_only`` is a ``KeyError`` for a science-configuration key
present in **neither** ``cdf/science/pipeline.toml`` ``[swarp]`` **nor** the
master ``.ini``. It is the same class as the W4B config migration's other
drops — the awaicgen geometry keys and the eleven sextractor keys, both
found by the W9 ramp: a key a stage reads that the migration did not carry
across.

**Structurally invisible to reference-phase testing.**
``resample_reference_image`` is a science-path stage. The reference phase
passed 109/109 and could never have reached it. This is precisely the class
of defect a science-width run exists to surface, and the overrun bought it
at no extra cost beyond the cap breach itself.

**Not fixed.** Fixing it is a Q9 fix round, outside this run's
authorization.

Stage intervals for the 2,158 that ran
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1

   * - Stage
     - avg s
     - max s
   * - ``science_image_catalog``
     - 6.30
     - 33.46
   * - ``resolve_reference_image``
     - 1.74
     - 8.73
   * - ``science_image_statistics``
     - 1.43
     - 1.98
   * - ``download_inputs``
     - 0.62
     - 7.94
   * - ``resample_reference_image``
     - 0.51
     - 0.72
   * - ``gunzip_science_image``
     - 0.46
     - 1.67
   * - ``reformat_science_image``
     - 0.37
     - 0.85
   * - ``measure_reference_fwhm``
     - 0.04
     - 0.18

Science-path stages are **two orders of magnitude cheaper** than reference
construction's ``psf_catalog`` (663 s). The failing stage fails fast — 0.51 s
average — so the population died cheaply rather than burning compute.

Population shapes
~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1

   * - lifecycle_state
     - outcome
     - category
     - count
   * - ``missing_or_contradictory``
     -
     -
     - 1,839
   * - ``terminal_after_start``
     - failure
     - ``internal_error``
     - 1,719
   * - ``submitted``
     -
     -
     - 1,060
   * - ``application_closed``
     - failure
     - ``internal_error``
     - 439

The 1,060 ``submitted`` and 1,839 ``missing_or_contradictory`` are children
whose Batch jobs were killed or never ran to completion as the process died
and the queues drained — abrupt-loss shapes the schema is built to
represent, not new defects.

The observability verdict
--------------------------

The records machinery was handed a stress test no planned ramp would have
applied: **5,275 submissions**, an unplanned 5,057-child burst, a killed
operator mid-flight, and a whole population failing at one stage.

**5,663 of 5,663 attempts in the table are classified. Zero unclassified,
zero unexplained.** Every failure carries a category from the v1 allowlist;
every abrupt loss carries a lifecycle state that says so; every attempt that
ran carries stage records with per-stage outcomes and durations. Triage of
the 2,158 failures ran entirely through records and error categories —
attempt table, stage table, terminal record in S3 — with no log archaeology
at any point.

The record shape held under conditions it was never designed for. That is a
stronger result than a clean ramp would have produced.

Live state at stop
------------------

* ``rapid-queue-prompt`` and ``rapid-queue-bulk``: 0 in every non-terminal
  state
* No ``virtualPipelineOperator`` process on rapid-admin
* Both Spot CEs DISABLED; both on-demand CEs ENABLED/VALID
* Job definitions ``rapid-pipeline-science`` and ``rapid-pipeline-bulk``
  revision 21 on ``sha256:89223d4b``
* Reconciler service active on the same digest
* Template, deployed stack parameter and both definitions agree on the pin

Nothing is running and nothing is pending.

What is unrun
-------------

* The science ramp at 18 → 180 → 540 on the prompt queue. The step-1 launch
  submitted nothing: it aborted in the operator's registration phase.
* The continuous-arrival drip phase.
* Consequently: the latency target against the one-hour/95% working figure,
  per-SCA interval decomposition for the science path at width, pooler draw
  at 540-wide, and backup-window behaviour under load. The evidence table's
  concurrency and packing rows are answered only by the 109-wide bulk
  reference measurement, which is supplementary to — not a substitute for —
  the science ramp on prompt.

Open, not actioned
------------------

* **``swarp_header_only``** — the science-path configuration key above. Q9
  fix round.
* **Seven stale ``fixd-chain`` records.** Attempts from 2026-08-06 carry
  reconciler closure records pointing at S3 keys that no longer exist
  (``NoSuchKey`` on ``seq-0001.json``), most likely because the 2026-08-06
  evening scratch cleanup removed test-era record objects. Because the
  operator runs registration before submission, and registration exits 65
  on any failure, **every operator invocation aborts before submitting**
  until these are resolved. Resolution by designed supersession — appending
  superseding closure records classifying them ``missing_or_contradictory``,
  evidence lost — was authorized but is moot for this run and was not
  performed.
* **Registration granularity.** Seven bad records abort the entire operator
  pass. The blast radius of one unreadable record is the whole run.
* **The ``RAPID_VPO_DRY_RUN`` semantics.** A rehearsal flag that suppresses
  only registration while submitting real work.
* **Memory over-reservation.** 16 GiB reserved against ~3 GiB resident.
* **Legacy ``.ini`` reads.** ``virtualPipelineOperator`` still reads
  ``job_info_s3_bucket_base`` at module scope, and five science-layer
  scripts read the same legacy value. Only the submission path's use was
  fixed.

Exit
----

The run does **not** meet smoke-run.md's exit criteria. A ramp step of
several hundred concurrent jobs did not complete with every attempt terminal
and explained, and the drip phase did not run. No full-scale proposal
follows from this run.

What it does establish: prep is live and correct at revision 21; reference
construction works at full population with complete coverage for the science
ramp; the records machinery holds under chaos; and one science-path
configuration defect stands between here and a science ramp.

The resumption — 2026-08-07
============================

Resumed under the drive-to-workable-system ruling. **The ramp did not
restart: the run ended on an expired SSO session before any submission.**
Batch children submitted: **0 of the ≤1,500 ceiling.** What the session did
land is the two code fixes that stood in front of the ramp, both proven by
test rather than by live run.

Handoff verification — every claim held
----------------------------------------

Verified against live state before any action, because a stale handoff
voids the launch.

.. list-table::
   :header-rows: 1

   * - Claim
     - Probe
     - Result
   * - Queues empty
     - ``list-jobs``, 2 queues × 5 non-terminal states
     - 0 in all ten, exit 0
   * - No VPO process
     - ``ps`` on rapid-admin via SSM
     - none; only the reconciler and the archive sink
   * - Spot CEs DISABLED
     - ``describe-compute-environments``
     - both DISABLED/VALID, both on-demand ENABLED/VALID, desired 0
   * - Job definitions at rev 21
     - ``describe-job-definitions``
     - science and bulk both rev 21 on ``sha256:89223d4b``
   * - Pin consistent at all sites
     - template, stack parameter, both definitions
     - all three agree
   * - Reconciler healthy
     - ``systemctl status`` via SSM
     - active on the same digest, ``errors: 0``
   * - The 7 ``fixd-chain`` records
     - SQL on rapid-db through the pooler
     - attempt_ids 123–129 present
   * - Their S3 evidence gone
     - ``head-object`` × 7
     - all 404

One refinement to the record. The seven rows carry the dangling reference
in **``terminal_record_key``**; ``closure_record_key`` is NULL and
``reconciler_materialized`` is false on all seven. The effect is as
described — registration cannot read the evidence — but the supersession
must *create* the closure account rather than replace one. Absence is
object-level: ``attempts/records/`` still holds every other run's prefix.

And the blast radius is narrower than seven. Registration selects on
``lifecycle_state`` in ``terminal_after_start``/``terminal_without_start``
**and** ``terminal_record_sequence >= 1``, so of the seven only **126, 127
and 128** are candidates: 123–125 sit at sequence 0, and 129 is already
registered. Three unreadable objects abort every operator invocation.

The pre-B EBS check
-------------------

Re-run at resumption: **4,594 GiB in use against the 50 TiB gp3 quota**
(``L-7A658B76``, read live). Unchanged from the first run's figure, and
nowhere near binding at the 540 step.

Fix cycle 1 — the swarp keys
-----------------------------

``swarp_header_only`` was never one key. Diffing
``build_swarp_command_line_args`` against release content mechanically:
**57 keys read, 34 configured, 3 supplied at runtime, 20 missing.** The
failing key is only the builder's *third* read, which is why it stood in
front of the other nineteen — fixing it alone would have bought one
attempt and then raised ``KeyError: 'swarp_header_suffix'``, burning a fix
cycle to learn nothing new.

All twenty land in ``cdf/science/pipeline.toml`` ``[swarp]``, values taken
from the master ``.ini`` where they had been correct all along: the loss
was entirely in the W4B extraction. They are release content by the
ratified criterion — ``HEADER_ONLY=Y`` makes SWarp write only a ``.head``
and skip resampling, so "can this value alter a science product" is not a
close call. All twenty round-trip with **no new exemption**; the remaining
three unconfigured keys are the per-attempt paths carrying
``fill_in_by_launch_script``, correctly absent.

Two tests, and the second is the one that matters. The first walks the
swarp builder as the awaicgen and sextractor tests walk theirs. The
second, ``test_every_command_line_builder_is_covered_by_this_class``,
fails on any ``build_*_command_line_args`` that has no completeness test.
Each of the three completeness tests had arrived reactively, after its
builder burned a live attempt, and each fix stopped at the builder that
had just fired — so the next uncovered builder was always one live failure
away. This is the third occurrence of the class; the enumeration now
closes instead of being extended a fourth time.

**Proven by refusal, not by passing.** Removing one key fails naming it;
removing all twenty fails with exactly twenty subtest failures; restoring
passes; an added bogus builder fails the coverage test. Completeness 7/7
exit 0, round-trip 34/34 exit 0, with no new exemption.

The supersession pass — written and tested, not run
----------------------------------------------------

``pipeline/reconciler/supersede_lost_evidence.py``. The reconciler already
supersedes — ``publish_closure_record`` climbs to the next free sequence
and the highest sequence is the full account — but it will not revisit
these rows: its requery is bounded to terminal rows whose *scheduler facts
changed*, inside a 24 h window set by Batch's own retention, and these
attempts are a day past it with nothing new to learn. That bound is
correct. Widening it so an operator's cleanup could be swept up would make
every terminal row eligible forever, so the driver belongs outside the
service.

Per attempt it appends a reconciler-first closure record at the next free
sequence citing the absent object as its rejected predecessor with reason
``absent`` — that is where "evidence lost" is recorded — then calls
``mark_missing_or_contradictory``. **The flag is what clears the gate**:
that state is deliberately absent from ``RECONCILED_STATES``, so a flagged
attempt stops being a registration candidate. Append-only; the stale key
stays on the row because it is the evidence of what was lost.

No ``error_category`` is set. These attempts succeeded — ``rapid_outcome``
is ``success`` on every one — the v1 allowlist has no category for lost
evidence, and the reconciler's own analogous path sets none either. The
writer's signature settles it: ``mark_missing_or_contradictory`` takes no
such parameter.

Absence is re-verified per attempt immediately before writing, and only
``head`` returning None counts; a store fault defers. Ten tests, exit 0,
with doubles that can refuse — a scripted fault, a readable record, a row
citing no key, and a dry run each assert that **nothing** was written to
either store.

Why the run stopped
-------------------

The SSO session expired between the last successful probe and the
supersession run, and would not renew: ``aws sso login`` timed out at the
browser flow, and ``--use-device-code`` returned a code needing a human to
enter it. Every SSO profile on the host reads
``Token has expired and refresh failed``; the one live credential belongs
to a different account and is out of scope. An unattended session cannot
clear an interactive authentication prompt.

The supersession script aborted at its own account check, before staging
code or issuing any SSM command — nothing was launched, and nothing needs
cleaning up.

State at that pause
-------------------

Queues empty, no VPO process, both Spot CEs DISABLED, definitions at rev
21, reconciler active with ``errors: 0``. The seven ``fixd-chain``
records were still stale, so the operator gate was still shut.

Authentication restored — the run continued
============================================

The supersession, the gate proof, the rebuild and the repin all landed.
The ramp did not: a width-2 probe found the next defect in the same
class, and it is one this session is not authorized to fix.

The supersession, and what proving the gate found
--------------------------------------------------

The tool's first live run failed at ``DBCredentialError``, which is the
W8 lesson repeating: ``resolve_credentials`` is the boundary read and
resolves under the *ambient* role, which inside the container is the
instance role — deliberately denied the orchestrator secret. Passing the
role ARN in is not enough; something must assume it. Fixed by reusing the
service's own helpers rather than opening a third credential path, and
the S3 client takes the same session (the instance role has no grant on
the records bucket either, so every absence check would have deferred).

Applied to the three ``fixd-chain`` candidates: each superseded at
sequence 2, the published body carrying ``reconciler_first``,
``reconstructed``, ``rejected_predecessor.reason = absent`` and no
``error_category``. Attempt total unchanged at 5,663 — append-only held,
and all seven rows still cite their original keys.

**Then the gate proof earned its place.** A dry-run registration pass over
all 2,333 candidates — reading each cited record from S3 exactly as a real
pass does, registering nothing — found **eleven more** attempts whose
record objects are equally absent: eight ``w1-live`` and three
``fixc-crash``, all from the same 2026-08-06 cleanup. The handoff's
"seven records" was an undercount of its own defect class. Superseding the
named seven and proceeding would have hit exit 65 on the first operator
invocation, after the ramp was already running.

All fourteen superseded. Re-proof: **2,322 candidates, 2,322 readable,
zero unreadable, probe exit 0.** The gate is open.

Rebuild and repin — revisions 22
---------------------------------

One rebuild iteration of the two allowed. Tag ``2ffb936-20260808``,
digest ``sha256:5148f0fe…``, from smdc ``2ffb936`` over the unchanged
``base-30984903893``.

Scan gate: **CVE-identical** to the digest replaced — 0 CRITICAL, 3 HIGH,
5 MEDIUM, 1 LOW, every one base-inherited. Verified by diffing the
vulnerability IDs, not the severity counts: equal counts can hide a swap.

The swarp fix was proven **inside** the digest rather than inferred from
commit order: 57 builder reads against 54 configured keys leaves only the
three per-attempt paths, ``swarp_header_only`` reads ``'N'``, and the
missing set is empty.

Pins consistent at all five sites: template default, deployed stack
parameter, both job definitions at **revision 22**, and the reconciler
service — its unit and its running container on the same digest,
connected as ``rapid_orchestrator``, polling with ``errors: 0``. **Both
Spot CEs stayed DISABLED across both deploys**, which is the template's
declared ``State`` converging as intended.

The width-2 probe, and why the ramp stopped
--------------------------------------------

Two children, job definition revision 22, prompt queue. **Submissions
this session: 2 of the ≤1,500 ceiling.**

The swarp fix works. ``resample_reference_image`` **succeeded** on both
children at 7.30 s — the stage that had killed 2,158 attempts at 0.51 s.
Every stage before it succeeded too.

The pipeline then died at the very next stage:

.. code-block:: text

    stage:          subtract_background
    error_category: tool_failure
    error_message:  tool not found: '/code/c/bin/bkgest' —
                    is it installed and on PATH?

``bkgest`` is one of RAPID's own eight in-house C binaries. It is absent
from the image, and the root cause is not in this repo:

* The application image excludes ``c/`` from its source archive **by
  design** — the C tools are meant to arrive as an RPM, not be compiled
  here. ``build.sh`` states it outright: "NO C build step exists in this
  image — there is nothing left to build."
* That claim rests on ``rapid-cmodules`` being installed in
  ``rapid-pipeline-base``. The RPM **exists and is published** —
  ``rapid-cmodules-1.0.0-2.el10``, in the yum repo since 2026-08-04 — and
  ``comps.xml`` lists it **mandatory** in the ``rapid-pipeline`` group.
* It is **not installed in the live base image**. The base carries seven
  ``rapid-*`` RPMs; ``rapid-cmodules`` is not among them. The base was
  pushed 2026-08-05, a day *after* the RPM was published, so this is not
  a timing gap — the base build did not install the group it declares.

The build's RPM-closure coverage check was reasoned from the comps group
rather than measured in the image, and the two disagree. That is the same
shape as the swarp drop: a declared-complete mapping that nothing
verified against the artifact.

**Pre-existing, not caused by this rebuild** — the previous rev-21 image
is equally without ``bkgest`` (verified directly). It was invisible
because the science path never reached stage R: ``swarp_header_only``
stood four stages earlier. Fixing one defect exposed the next, which is
what a width-2 probe is for. Committing 180 children first would have
bought 180 identical failures.

``cforcepsfaper``, the forced-photometry binary, is missing for the same
reason and will fail the same way when that path runs.

Live state at stop
------------------

* Both queues 0 in every non-terminal state; the probe drained, and both
  its attempts closed ``terminal_after_start`` / ``failure`` /
  ``tool_failure`` — zero non-terminal, zero unexplained, zero flagged
* No VPO process on rapid-admin — the rogue-VPO guard never had to fire,
  because no VPO was ever started
* Both Spot CEs DISABLED; both on-demand ENABLED/VALID
* Job definitions at **revision 22** on ``sha256:5148f0fe``
* Reconciler active on the same digest, ``errors: 0``
* Template, stack parameter, both definitions and the reconciler agree

Nothing is running and nothing is pending.
