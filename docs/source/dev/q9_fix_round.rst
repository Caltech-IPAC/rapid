Q9 — the fix round
==================

**One fix cycle of three used, and the ramp did not run.** The cycle spent
was the swarp science-config drop, fixed and proven by test; the
supersession pass that unblocks the operator was written and unit-proven
but never executed. The session ended on an expired SSO session that
cannot be renewed without a human, so no science ramp, no drip phase, and
no measured parameters beyond what the first run already established.

This ledger records what the round settled, what it left, and — because
that is what Q9 exists to feed — exactly which measurements the full-scale
proposal is still missing.

Cycles used
-----------

.. list-table::
   :header-rows: 1

   * - Cycle
     - Subject
     - Outcome
   * - 1
     - ``swarp_header_only`` and nineteen siblings
     - Fixed; completeness 7/7 and round-trip 34/34, both exit 0
   * - 2
     - unused
     -
   * - 3
     - unused
     -

Batch children submitted this session: **0**, against the ≤1,500 ceiling.
Image rebuilds: **0** of ≤2 — the swarp fix is release content inside the
image, so it needs one, but the rebuild is an AWS operation and was
blocked with everything else. Deploys: **0**.

What cycle 1 settled
--------------------

The defect was misread as one key by its error message. It is twenty:
``build_swarp_command_line_args`` reads 57 keys, ``[swarp]`` carried 34,
three are per-attempt paths, and the remaining twenty were dropped in the
W4B ``.ini``-to-TOML extraction. ``swarp_header_only`` is merely the third
read, so it masked the other nineteen.

That distinction is the cycle's real content. A one-key fix would have
passed review, rebuilt the image, resubmitted, and failed at
``swarp_header_suffix`` on the next line — one cycle spent to advance the
error message by one key, with two left and nineteen keys to go. Diffing
the builder against release content mechanically, rather than reading the
error message, is what turned a three-cycle problem into a one-cycle one.

The generalisation is now enforced rather than remembered. Three
completeness tests exist — awaicgen, sextractor, swarp — and each arrived
*after* its builder had already burned a live attempt, each fix stopping
at the builder that had just fired.
``test_every_command_line_builder_is_covered_by_this_class`` fails on any
``build_*_command_line_args`` without a test, so a fourth occurrence is
caught in the suite rather than on the ramp.

Measured parameters
-------------------

The evidence table smoke-run.md asks for, with what is actually known. The
109-wide reference measurement is the first run's and is carried forward
unchanged; it is **supplementary to, not a substitute for**, the science
ramp on the prompt queue.

.. list-table::
   :header-rows: 1

   * - Evidence
     - State
     - Value
   * - Prompt-vs-bulk concurrency and packing → the 3,600/1,200 MaxvCpus ratio
     - **partial**
     - Bulk only: 109 concurrent, 37 ``m6a.4xlarge``, ~2.95 children/host,
       436 vCPU of the bulk CE's 1,200. The prompt queue is unmeasured, so
       the *ratio* — the thing the row exists to inform — is unanswered.
   * - Memory-heavy packing across six instance families
     - **not measured**
     - One family selected at 109-wide, so the spread was never exercised.
       Packing is bound by reservation, not consumption: 16 GiB reserved
       against ~3 GiB resident, roughly 5x over-reservation, which fits
       ~4 children per 64 GiB host while drawing 12 of its 16 vCPU.
   * - Scratch I/O at the 150 GiB gp3 sizing
     - **partial**
     - 20 GiB of 150 used (14%) on a host carrying 3 reference children.
       Nowhere near binding, and nothing argues for instance store — but
       the science path's I/O shape is unmeasured.
   * - Spot-reclaim retry semantics
     - **not measured**
     - Spot stays DISABLED; a Spot trial was not authorized.
   * - Queue/startup/execution/publication intervals per SCA
     - **not measured for science**
     - Reference-path stage intervals are known (``psf_catalog`` 662.6 s
       average dominates at ~78% of a child). The 2,158 science attempts
       of the first run died at ``resample_reference_image`` in 0.51 s
       average, so they measure the failure, not the path. **The
       one-hour/95% latency target has no science-path evidence at all.**
   * - Pooler draw against budgets
     - **partial**
     - 23 backend connections, 7 payload, against ``max_connections`` 200
       (~12%) at 109-wide. Children connect briefly and return the
       connection, so this establishes the shape, not the bound. Draw at
       540-wide is unmeasured.
   * - Backup-window behaviour under load
     - **not measured**
     - No run intersected a backup window.
   * - EBS aggregate quota at ramp width
     - **measured**
     - 4,594 GiB in use against 50 TiB (``L-7A658B76``); ~14.4/50 TiB at
       the planned 540 fan-out, worst case ~42%.

The exit criterion is unmet
---------------------------

smoke-run.md's exit is a ramp step of several hundred concurrent jobs with
every attempt terminal and explained, products written, and the drip phase
holding the latency target. None of that ran. **No full-scale proposal
follows from this round**, and the ~42 h run remains where it was: an
owner ruling with no measured basis yet.

What stands between here and a science ramp is now smaller and better
understood than it was. Three unreadable record objects shut the operator
gate; the pass that opens it is written and unit-proven. The
science-config defect that killed 2,158 attempts is fixed at its
configuration home with a test that refuses the whole class. Both need an
authenticated session to land: the supersession is a live DB and S3
operation, and the swarp fix is release content that reaches a job only
through an image rebuild and repin.

Proposed, not ratified
----------------------

Decisions taken conservatively under the unattended rule, recorded for the
owner rather than enacted:

* **No ``error_category`` on the superseding records.** The attempts
  succeeded; the v1 allowlist has no category for lost evidence, the
  writer's signature accepts none, and the reconciler's own analogous path
  sets none. Recording a failure that did not happen would be worse than
  recording nothing.
* **``reconciliation_sources`` of ``["postgres", "s3"]``.** Those are the
  two stores that actually disagree here. The reconciler's own sites
  compare postgres against batch; the list exists to name what was
  compared.
* **The supersession driver lives outside the reconciler service.** The
  24 h supersession window is correct and should not be widened to sweep
  up an operator's cleanup — that would make every terminal row eligible
  for requery forever.
* **The twenty swarp keys are release content**, per the ratified
  placement criterion. They alter science products; the master ``.ini``
  values carry across unchanged.

Left for the owner
------------------

* **An authenticated session.** Everything below needs one.
* **Run the supersession pass** (``--apply``), then confirm the
  registration gate passes without submitting science work.
* **Rebuild and repin** so the swarp fix reaches the job definitions —
  iteration 3 against the ≤2-per-run cap, scan gate, revision bump,
  pins verified at template, stack parameter, both definitions and the
  reconciler unit.
* **Then the ramp**: 180 → 540 → drip, each step gated on clean attempt
  records.

Carried forward unchanged from the first run, none actioned here:
registration granularity (three bad records abort the whole operator
pass), the ``RAPID_VPO_DRY_RUN`` semantics, memory over-reservation, and
the legacy ``.ini`` reads in ``virtualPipelineOperator`` and five
science-layer scripts.
