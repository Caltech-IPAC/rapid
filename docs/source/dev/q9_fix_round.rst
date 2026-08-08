Q9 — the fix round
==================

**One fix cycle of three used; the ramp did not run.** The swarp
science-config drop is fixed, live in the image, and proven by a real
science attempt getting past it. The ramp stopped at a width-2 probe that
found the next defect of the same class — a missing binary whose fix is a
base-image rebuild, outside this lane's authorization.

The round's real content is that two blocking defects were found and
located precisely, one of them fixed, at a cost of **two Batch children**.

Cycles used
-----------

.. list-table::
   :header-rows: 1

   * - Cycle
     - Subject
     - Outcome
   * - 1
     - ``swarp_header_only`` and nineteen siblings
     - **Fixed and proven live**: ``resample_reference_image`` succeeded
       at 7.30 s on both probe children, where it had failed 2,158
       attempts at 0.51 s
   * - 2
     - ``bkgest`` and ``cforcepsfaper`` unreachable
     - **Code half fixed and merged; image half blocked on disk.** The
       base build installed ``rapid-cmodules`` successfully and then
       died writing its final layer — rapid-admin has no room
   * - 3
     - unused
     -

Counts against caps
-------------------

.. list-table::
   :header-rows: 1

   * - Cap
     - Used
   * - Batch children (≤1,500)
     - **2**
   * - Fix cycles (≤3)
     - 1 fixed, 1 root-caused
   * - Application image rebuild + repin (≤2)
     - **1** (revision 22, the swarp fix)
   * - Base image rebuild (≤1)
     - **1 attempt, no artifact** — three tries, none reaching a pushed
       image; the last installed the RPM and died writing its layer
   * - Deploys per stack (≤2)
     - rapid-batch 1, rapid-reconciler-service 1

What cycle 1 settled, and what it proved
-----------------------------------------

The defect was misread as one key by its own error message. It is twenty:
the builder reads 57 keys, ``[swarp]`` carried 34, three are per-attempt
paths. ``swarp_header_only`` is only the third read, so it masked the
other nineteen — a one-key fix would have failed at
``swarp_header_suffix`` and spent a cycle advancing the error message by
one key. Diffing the builder against release content mechanically, rather
than reading the message, turned a three-cycle problem into a one-cycle
one.

The generalisation is enforced rather than remembered:
``test_every_command_line_builder_is_covered_by_this_class`` fails on any
``build_*_command_line_args`` without a completeness test. All three
existing tests had arrived reactively, each after its builder burned a
live attempt, each stopping at the builder that had just fired.

**The proof is a live attempt, not a test.** Both probe children ran
``resample_reference_image`` to success in 7.30 s.

Cycle 2 — the defect the probe found
--------------------------------------

``subtract_background``, ``tool_failure``, both children:
``tool not found: '/code/c/bin/bkgest'``.

Established from records and artifacts, not inference:

* ``bkgest`` is one of RAPID's eight in-house C binaries. The application
  image excludes ``c/`` from its source archive by design; ``build.sh``
  states "NO C build step exists in this image — there is nothing left
  to build."
* That rests on ``rapid-cmodules`` being installed in the base image.
  The RPM **is built and published** — ``rapid-cmodules-1.0.0-2.el10``,
  in the yum repo since 2026-08-04 — and ``comps.xml`` marks it
  **mandatory** in the ``rapid-pipeline`` group.
* It is **not installed in the live base image**: seven ``rapid-*`` RPMs
  are present and ``rapid-cmodules`` is not. The base was pushed
  2026-08-05, a day after the RPM was published, so this is not a timing
  gap.

The build's RPM-closure coverage check was reasoned from the comps group
rather than measured in the artifact. Same shape as the swarp drop: a
declared-complete mapping nothing verified against what shipped.

Pre-existing — the previous rev-21 image is equally without ``bkgest``.
It was invisible only because ``swarp_header_only`` failed four stages
earlier. ``cforcepsfaper`` is missing for the same reason and will fail
the forced-photometry path identically.

Installing the RPM was necessary but not sufficient
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The RPM installs to ``/opt/rapid/bin``. The callers named
``/code/c/bin`` — the retired from-source build's location — so shipping
``rapid-cmodules`` alone would have left both binaries exactly as
unreachable, and cost a fix cycle to discover. The spec says as much in
its own header: it "replaces the app repo's from-source build
(``rapid/c/builds/build_inside_container.sh``, which builds these 8
binaries into ``/code/c/bin``)". The packaging moved; the callers did
not.

**The code half is fixed and merged.** Both call sites now use the bare
name, resolved against the ``PATH`` the Containerfile owns — the same way
``swarp`` and ``sextractor`` have always been found.

``bkgest``'s ``-a`` argument went with it, and that is a second defect in
the same call rather than tidying. It is the OPTIONAL ancillary *file*
("``-a <ancillary_file_path> (Optional)``",
``bkgest_parse_namelist.c``), and it was being handed
``SOFTWARE_ROOT() + "/c/include"`` — a directory, not a file, and one
that exists in no image and on no branch of this repo.

The test walks ``pipeline/`` and ``modules/`` for the retired path rather
than asserting about the two known sites, so a ninth caller fails in the
suite instead of on a ramp. Proven by refusal: restoring the ``bkgest``
path fails two tests, restoring the ``cforcepsfaper`` path fails one,
restoring the bogus ``-a`` fails one, and the fixed tree passes.

The image half is blocked on disk
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The base rebuild ran and **the diagnosis is confirmed**: the group
install resolved and installed ``rapid-cmodules-1.0.0-2.el10`` alongside
the other sixteen packages, "Complete!". The build then failed writing
its final layer — ``no space left on device``.

rapid-admin has a 64 G root with ~14 G free at rest, and carries **47
cached pipeline image tags spanning 12 days: 24.25 GB of images plus
13.44 GB of unused volumes, all reclaimable, none active**. A pipeline
image is ~5 GB, so a build needs more headroom than the host has while
that cache sits there.

Three attempts, each failing later than the last as the staging set
shrank: the whole published repo (3.4 G staged) died at the COPY; the
``rapid-*`` subset died at the COPY; the comps-group-scoped newest-build
set (38 packages, every mandatory member covered) got all the way through
the group install and died committing the layer.

What this session reclaimed is its own: the staged RPM trees and the
untagged layers from its failed builds. The 24.25 GB of tagged images
belongs to earlier sessions' builds. They are local caches of images that
exist immutably in ECR, so pruning them is recoverable — but it is a bulk
deletion this lane did not have a go for, and the authorization covered a
base rebuild rather than a cache purge. Recorded as proposed rather than
taken.

Nothing partial was left behind: no ``base-q9cmodules-*`` tag reached
ECR, no build container survives, and the host is back at its 79%
baseline.

Measured parameters
-------------------

The evidence table with what is now known. The 109-wide reference
measurements are the first run's, carried forward, and remain
**supplementary to — not a substitute for —** the science ramp.

.. list-table::
   :header-rows: 1

   * - Evidence
     - State
     - Value
   * - Prompt-vs-bulk concurrency and packing → the 3,600/1,200 MaxvCpus ratio
     - **partial, first prompt-queue data**
     - Bulk, 109-wide: 37 ``m6a.4xlarge``, ~2.95 children/host, 436 vCPU
       of 1,200, packing bound by the 16 GiB memory reservation. Prompt,
       2-wide: one ``r6i.2xlarge`` (8 vCPU / 64 GiB) took both children
       and reported **0 vCPU and 30 GiB remaining** — CPU-bound, the
       opposite of the bulk case. The two queues select different
       families and bind on different resources, so the ratio cannot be
       set from the bulk measurement alone.
   * - Memory-heavy packing across six families
     - **not measured**
     - Two families seen (``m6a.4xlarge`` bulk, ``r6i.2xlarge`` prompt),
       neither under contention.
   * - Scratch I/O at 150 GiB gp3
     - **partial**
     - 20 GiB of 150 (14%) on a bulk host carrying 3 reference children.
       The science path's shape is unmeasured — it died at stage R.
   * - Spot-reclaim retry semantics
     - **not measured**
     - Spot stays DISABLED; no trial authorized.
   * - Queue/startup/execution/publication intervals per SCA
     - **partial**
     - Cold-start measured end to end on the prompt queue: submission
       00:38:54 → CE scale-up from zero → instance running 00:40:15 →
       first stage 00:42:07. **~3 min 13 s cold start**, matching the
       2026-08 baseline. Science stage times through
       ``resample_reference_image``: 7.30 s resample, 4.36 s
       ``science_image_catalog``, 2.36 s ``resolve_reference_image``,
       1.81 s ``science_image_statistics``, sub-second for the rest.
       **The one-hour/95% target still has no full-path evidence** — no
       science child has reached publication.
   * - Pooler connection draw
     - **partial**
     - 23 backends, 7 payload, against ``max_connections`` 200 (~12%) at
       109-wide. Unmeasured at ramp width.
   * - Backup-window behaviour under load
     - **not measured**
     - No run intersected a window.
   * - EBS aggregate quota
     - **measured**
     - 4,594 GiB against 50 TiB (``L-7A658B76``), re-run at submission
       time. ~14.4/50 TiB at the planned 540 fan-out.

Observability
-------------

Two attempts, two failures, **zero unexplained**: both carry
``tool_failure`` from the v1 allowlist, both carry complete per-stage
records with durations, and the failing stage carries the tool's own
argv and message. Triage ran entirely through the attempt table, the
stage table and the terminal record in S3 — **no log archaeology at any
point**. The record shape did its job on a defect nobody had predicted.

Both attempts reached **``terminal_after_start`` / ``failure`` /
``tool_failure``** under the reconciler running on the new digest: zero
non-terminal, zero unexplained, zero flagged. The full path from an
application-authored failure through reconciler classification closed
cleanly, which is the first end-to-end exercise of that path since the
repin.

The exit criterion is unmet
---------------------------

smoke-run.md's exit needs a ramp step of several hundred concurrent jobs
with every attempt terminal and explained, products written, and the drip
phase holding the latency target. No ramp step ran. **No full-scale
proposal follows**, and the ~42 h run remains the owner's call with no
measured basis yet.

What the round did change: the operator gate is open and proven open, the
image is current at revision 22 with pins consistent at five sites, one
of the two blocking defects is fixed and proven live, and the second is
located precisely enough to fix in one bounded step.

Proposed, not ratified
----------------------

* **Reclaim rapid-admin's image cache** — 24.25 GB of tagged images and
  13.44 GB of unused volumes, 47 pipeline tags over 12 days, none active
  and all present in ECR. This is what blocks the base rebuild, and at
  ~5 GB per image and a build most sessions, the host returns to this
  state within a few builds however tonight's rebuild is finished. A
  scheduled prune is the durable form.
* **Measure the base's RPM set against ``comps.xml`` in CI**, at the
  artifact rather than in a comment. The coverage check that asserted
  "nothing left to build" was right about intent and wrong about the
  image, and nothing compared the two. Run once inline this session
  against the live artifact, which is how the gap was confirmed.
* **Bound the base build's staging set** to the comps group's newest
  builds rather than the whole published repo. The repo carries four
  ``rapid-python`` revisions at ~717 MB each and workstation packages the
  group never references; scoping it cut the staged tree from 3.4 G to
  38 packages and was the difference between failing at the COPY and
  reaching the layer commit.
* Carried from the earlier pause, all accepted as proposed: no
  ``error_category`` on superseding records;
  ``reconciliation_sources = ["postgres", "s3"]``; the supersession
  driver outside the reconciler service; the twenty swarp keys as
  release content.

Left for the owner
------------------

1. **Free space on rapid-admin**, then finish the base rebuild. The
   build is proven correct up to its final layer write — the group
   install put ``rapid-cmodules`` in place — so this is a rerun, not a
   redesign. Roughly 10 GB of headroom is enough.
2. **Rebuild the application image on the new base** and repin to
   revision 23. ``build.sh`` live-resolves the newest ``base-*`` tag by
   push time, so no pin edit is needed; the code half of the fix is
   already on ``smdc``.
3. **A width-2 probe on revision 23** — both children must clear
   ``subtract_background`` and reach terminal with clean records.
4. Then the ramp: 180 → 540 → drip, each step gated on clean attempt
   records. The mechanics are proven — the runner submits a bounded,
   capped array with no VPO involved.
5. A width-2 probe before each widening. It cost two children and found
   a defect that would have cost 180.

Carried forward unactioned: registration granularity (a handful of bad
records aborts the whole operator pass — now demonstrated at fourteen),
the ``RAPID_VPO_DRY_RUN`` semantics, memory over-reservation, and the
legacy ``.ini`` reads.
