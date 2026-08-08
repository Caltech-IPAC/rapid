Q9 — the fix round
==================

**Five defects, four of them found one Batch child at a time, and the
fifth cycle found the rest of the class with none.** Cycles 1–4 each cost
a submission cycle to discover: a science child reached one stage
further, raised a ``KeyError`` or a "tool not found", and the round
learned exactly one fact. Cycle 5 stopped doing that. A mechanical audit
of every science-configuration read against the release file finds the
whole class at once, and its verdict over the entire payload was two
keys — the same two the rev-24 probe had just spent two children to find.

The audit is now the gate: a test fails on any key the payload reads and
no configuration home provides. Re-running it with
``swarp_header_only`` renamed reports that defect too, so cycle 1 — which
failed 2,158 attempts of the first smoke run — is caught by it, without
submitting anything.

The round's real content is therefore two things: the five defects, and
the fact that the mechanism which found four of them was the wrong one.

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
     - **Fixed and live at revision 23.** RPM installed into a rebuilt
       base, callers moved off ``/code/c/bin``; the binary now runs. It
       exposed cycle 3 one line further on
   * - 3
     - ``bkgest_errcodes.h`` not shipped by ``rapid-cmodules``
     - **Fixed and proven live at revision 24.** ``rapid-cmodules``
       1.0.0-3 installs the runtime message catalogue and ``bkgest``'s
       ``-a`` points at it; the stage now reports
       ``Status Message 0x0000`` where it reported
       ``ERRCODE_FILE_NOT_FOUND``. It exposed a fourth defect one stage
       further on
   * - 4
     - ``gain_match`` reads two ``[gainmatch]`` keys that do not exist
     - **Found at the rev-24 probe, fixed in cycle 5.** Same class as
       cycle 1: science-path release content the code requires and
       ``pipeline.toml`` never carried
   * - 5
     - The whole dropped-key class, audited mechanically
     - **Fixed and proven at revision 25.** 397 key reads across 17
       sections resolved against the release; exactly two were provided
       by nothing, and they are cycle 4's. Restored from the master
       ``.ini``, value-preserving, and the audit is now a test

Counts against caps
-------------------

.. list-table::
   :header-rows: 1

   * - Cap
     - Used
   * - Batch children (≤1,500)
     - **12** — six width-2 probes. Two of the twelve bought nothing:
       they were submitted by a probe harness that skipped registration,
       and the gate refused them at startup (see "The registration gate
       refused a probe" below). Cycle 5 found its defect with **none**
   * - Fix cycles
     - **6.** The ≤3 cap governed the first three; it was lifted for the
       resumption, which spent cycles 5 and 6
   * - Application image rebuild + repin (≤2 per session)
     - **5 across the round** — revisions 22 (swarp), 23 (cmodules base),
       24 (catalogue), 25 (gainmatch keys), 26 (reference PSF). The
       resumption used 2 of its 2, both first-attempt successes
   * - Base image rebuild
     - **2 artifacts** — one by hand (cycle 2, after three attempts lost
       to disk), one by the promoter (cycle 3, off rapid-admin entirely).
       The resumption rebuilt no base: both its fixes were application
       content over the same ``base-31237339531``
   * - Deploys per stack (≤2)
     - rapid-batch 2, rapid-reconciler-service 2 in the resumption (one
       per repin), 1 each in the earlier sessions

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

The image half needed disk before it needed anything else
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The base rebuild confirmed the diagnosis on its first run: the group
install resolved and installed ``rapid-cmodules-1.0.0-2.el10`` alongside
the other sixteen packages, "Complete!" — so comps.xml and the RPM were
always right and the live base simply never carried it. That build then
died writing its final layer, ``no space left on device``, as did two
before it.

rapid-admin has a 64 G root and carried **47 cached pipeline image tags
spanning 12 days — 24.25 GB of images plus 13.44 GB of unused volumes,
all reclaimable, none active** — against a ~5 GB image. Three attempts
failed progressively later as the staging set shrank: the whole published
repo (3.4 G staged) at the COPY, the ``rapid-*`` subset at the COPY, and
the comps-group-scoped newest-build set (38 packages, every mandatory
member covered) at the layer commit.

**The cache prune, once authorized, was the whole fix.** Every removal
was gated on the tag resolving in ECR first, so each is recoverable by
re-pull: 42 tags removed, 3 skipped as untagged and not provably
re-pullable, 2 kept because the running reconciler needed them, and the
volume set cleared. Free space went 14 G → 27 G. The identical build then
succeeded.

**Base:** ``base-q9cmodules-*``, ``sha256:0b35fda6…`` — the digest is the
identity; the tag carries a build timestamp whose digit run trips the
repo's twelve-digit account-number guard, so it is elided rather than
allowlisted.
**Application:** ``17a69c3-20260808``, ``sha256:311741ab…``, from smdc
``17a69c3`` over that base.

Verified in both digests rather than inferred from commit order:
``rapid-cmodules`` installed, ``bkgest`` and ``cforcepsfaper`` present,
executable, resolvable on ``PATH``, and loading with no missing shared
libraries, and ``/code/c/bin`` absent as designed.

**The comps-vs-artifact audit passes**: all fourteen mandatory members of
the ``rapid-pipeline`` group compared against the built image's
``rpm -qa``, zero missing. That is the check whose absence let the
original gap survive, and running it is how the fix was confirmed rather
than assumed.

Scan gate on both images: CVE-identical to the digests they replace,
diffed by vulnerability ID rather than severity count. The application
image's first gate read was **discarded as vacuous** — the scan was still
``PENDING`` and returned an empty finding list, which is not a clean
result. Re-read once ``ACTIVE``: nine findings on both sides, identical.

Pins consistent at all five sites, both job definitions at **revision
23**, and both Spot CEs stayed DISABLED across both deploys.

The rev-23 probe: the binary runs, and finds a third defect
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Two children, job definition revision 23. **Submissions: 4 of the
≤1,500 ceiling.**

``bkgest`` is now found and executed. The stage that had failed instantly
at 0.00 s with "tool not found" now **runs for 2.74 s** and the tool
prints its own banner, its parameters, and "A total of 0 NaN's were
produced in the results. Processing time: 2.724496 seconds". The image
half of the fix works.

It still exits 255, for a third reason in the same chain:

.. code-block:: text

    *** BKE_log_writer: Could not open bkgest_errcodes.h
    bkgest Status Message      0xefff
    ERRCODE_FILE_NOT_FOUND from Function 0x0000: LOG_WRITER
    Ancillary Data-File Path = .

``bkgest_errcodes.h`` is not a compile-time header despite its name: it
is a **runtime message catalogue**, read by ``bkgest_log_writer.c`` to
turn status codes into text. That function is what the ``-a`` ancillary
path was for, and removing ``-a`` as a bogus argument was half right and
half wrong — the value it carried (``/c/include``) was indeed a
non-existent path, but the argument itself is load-bearing, and without
it the path defaults to ``.``, where the file also is not.

The consequence is out of proportion to the cause, and worth stating
precisely: **the science computation completed**. The failure is in the
message lookup afterwards, which sets ``I_status`` and so becomes a
non-zero exit; the stage is failed by its error reporter rather than by
its arithmetic.

The file exists in the repo at ``c/src/bkgest/bkgest_errcodes.h``, and
**``rapid-cmodules`` ships binaries only** — ``rpm -ql`` lists the eight
executables and no data files, and the image carries the catalogue
nowhere. The application image excludes ``c/`` by design, so neither home
provides it. That is a packaging gap in the RPM, and closing it properly
means rebuilding and republishing ``rapid-cmodules`` with the catalogue
installed beside the binaries and ``-a`` restored to point at it —
outside what this session was authorized to do.

Records again did the work: the tool's full stdout was in the attempt's
diagnostics bundle, the failing stage carried its argv and exit code, and
nothing needed log archaeology. Both attempts carry ``tool_failure`` from
the v1 allowlist; zero unexplained, zero flagged.

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
     - **measured, re-run 2026-08-08**
     - 4,594 GiB against 51,200 GiB (``L-7A658B76``) — **9.0%**,
       unchanged from the earlier reading. ~14.4/50 TiB at the planned
       540 fan-out, so the quota does not bind the ramp.
   * - Per-stage science timings, revision 25 (13 stages to ``run_zogy``)
     - **measured**
     - ``prepare_zogy_inputs`` 77.37 s dominates; ``resample_reference_image``
       6.68 s, ``gain_match`` 6.53 s, ``science_image_catalog`` 3.88 s,
       ``subtract_background`` 2.86 s, ``resolve_reference_image`` 2.21 s,
       ``science_image_statistics`` 1.69 s, ``download_inputs`` 0.55 s,
       ``gunzip_science_image`` 0.48 s, ``reformat_science_image`` 0.49 s,
       ``normalize_science_psf`` 4.8 ms, ``measure_reference_fwhm`` 23.8 ms,
       ``inject_fake_sources`` skipped. **~103 s of science before the
       failing stage**, on a warm container.
   * - Where the science time actually goes, past ``run_zogy``
     - **partial, and the surprise of the round**
     - The stages after ``run_zogy`` are far heavier than the thirteen
       before it. Revision 26's probe spent **~103 s reaching**
       ``run_zogy`` and then more than **ten minutes** in the difference
       catalogues, logging nothing while photutils fits PSFs on a
       7,000×7,000 difference image ("Input data contains unmasked
       non-finite values", then silence). Every earlier probe died before
       this point, so no previous measurement saw it. A per-SCA latency
       budget built on the pre-``run_zogy`` timings would be wrong by an
       order of magnitude, and the long silent stretch is also a
       liveness-monitoring problem: the log stream stops advancing while
       the job is perfectly healthy, so log freshness is not a health
       signal here.

       **This is the single most consequential measurement of the round
       for the one-hour/95% target**, and it is the one the ramp exists to
       pin down. Two children at width 2 on a dedicated host say nothing
       about what several hundred concurrent children do to the same
       stage — that is exactly what 180 and 540 are for.

       That the job was working rather than wedged is established from
       the host, not from the absence of an error: the ``r6i.2xlarge``
       carrying both children held a flat **~25.2% CPU for twenty
       minutes** — 2 of 8 vCPUs pinned, one per child. **The science path
       is single-threaded per child**, which is the fact that makes the
       packing question a memory question rather than a CPU one.
   * - Cold start, prompt queue
     - **measured twice**
     - Submission 06:22:08 → first stage 06:25:15 — **3 min 07 s**,
       matching the 2026-08 baseline of ~3 min 13 s. The second probe's
       instance took longer to reach RUNNING (a fresh host pulling a
       2.38 GB image), so the pull is the variable part, not the
       scale-up.

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

Cycle 3: the catalogue ships, and the stage clears
--------------------------------------------------

``rapid-cmodules`` 1.0.0-3 installs ``bkgest_errcodes.h`` to
``/opt/rapid/share/bkgest/``, and ``science.py`` passes ``-a`` at that
directory. Both halves were needed: cycle 2 had dropped the argument on
finding its value named a path in no image — right about the value, wrong
about the argument, since omitting it only falls back to the default
``"."``, which has no catalogue either.

Published through the promoter, not by hand: the RPM is signed and
published (96,144 bytes against release 2's 94,191 — the catalogue's
weight), the base image is built in-account, and rapid-admin's disk is not
in the path at all. Releases 1, 2 and 3 coexist; the immutable-NEVRA rule
held.

**The rev-24 probe proves it in the log**, which is worth quoting because
it is the exact line that failed:

.. code-block:: text

    run: bkgest -a /opt/rapid/share/bkgest -i ...
    Ancillary Data-File Path = /opt/rapid/share/bkgest
    bkgest Status Message      0x0000
    A total of        0   NaN's were produced in the results.
    Program bkgest, version 1.3, terminated.

Revision 23 printed ``Ancillary Data-File Path = .`` and
``ERRCODE_FILE_NOT_FOUND`` at the same point, and exited 255.
``subtract_background`` now passes and the pipeline reaches the next
stage.

What a submission needs, established by running it
---------------------------------------------------

The resumption's probes go through one harness
(``scripts/q8_ramp_probe.py``) rather than an ad-hoc invocation each
time, and getting it to run documented five facts the next operator
would otherwise rediscover:

* ``gather_science_units`` takes a **RAPIDDB handle**, not a raw
  connection. The handle borrows the caller's connection, which is the
  mode that neither commits nor calls ``exit()`` from inside library
  code.
* The database endpoint comes from the **parameter tree**, read once and
  passed explicitly. Exporting ``DBSERVER``/``DBPORT``/``DBNAME`` would
  put operational configuration in a second home, which the reader's own
  error message exists to prevent.
* The submitter's database identity is the **orchestrator** secret, not
  the tree's ``db/secret-id``. That names the *pipeline* secret — the
  identity the Batch children run as — and the submitting role is denied
  it. Probed rather than inferred: ``READABLE
  rapid/db/service/orchestrator``, ``DENIED rapid/db/service/pipeline``.
* ``submission_env`` requires ``RAPID_IMAGE_DIGEST``,
  ``RAPID_RELEASE_IDENTITY`` and ``RAPID_MANIFEST_BUCKET``, and the image
  sets none of them — they are what every attempt row records to be
  reproducible, so they are supplied per submission. Manifests go to
  ``roman-rapid-products/submissions/``, which is where the earlier
  probes' manifests demonstrably are.
* ``virtualPipelineOperator`` is a **script**: importing it runs a module
  body that reads ``STARTDATETIME``/``ENDDATETIME`` and ``sys.argv[1]``
  and exits 64 when they are unset. Its preconditions are satisfied
  across the import rather than by copying ``submission_env`` out of it —
  a copy would be a second home for the route-and-binding resolution,
  which is the class of defect this round keeps finding.

The width cap is enforced before anything is submitted, and the dry run
proves it on the exact population that caused the incident: the gathering
pass returns **5,057 units** — the number the rogue VPO once put on a
queue in 35 seconds — and the harness caps to the stated width, logs the
5,055 it dropped, and submits nothing. A silent cap reads exactly like a
complete run, so the count that was dropped is always stated.

One operational trap is worth recording because it cost a probe: SSM's
command output is capped at 24 KB and **truncates from the end**, which
is exactly where the submission result is. A run that failed after the
cap looked identical to one that succeeded. The harness now writes its
full log to a file on the host and echoes only the summary lines.

The registration gate refused a probe, correctly
------------------------------------------------

The first rev-24 probe attempt died at startup on both children, exit 70,
before any science:

.. code-block:: text

    attempt 5673 resolved to a missing_or_contradictory row: Batch knows
    about job ecf3cef5-…:0 but no logical job …:12/7 was ever recorded

Not a defect. The probe harness submitted with a raw ``SubmitJob``,
skipping the ``logical_jobs`` and attempt rows that
``pipeline.seams.submit_units`` pre-creates **before** submitting — and
the runtime's resolver claims a pre-created row by its run-scoped id.
``submit_units``'s own docstring names the failure exactly: "correct
behaviour on the resolver's part, and a self-inflicted wound on the
submitter's." The gate declined to run science it could not attribute,
which is what it is for. Two children spent, no cycle consumed.

Re-submitting through the seam also documented three access facts, each
probed rather than assumed: rapid-admin's instance role has no
``s3:GetObject`` or ``s3:PutObject`` on ``roman-rapid-products`` and no
``GetSecretValue`` on the orchestrator DB secret.
``rapid-orchestrator-role`` holds all three and its trust policy names
role-chaining from the admin host explicitly. The credentials must be in
the process environment before boto3 builds its default session —
assuming the role from inside Python leaves the DB lookup on the host
identity, and it is denied.

Cycle 4 is found, and the budget is spent
-----------------------------------------

Both rev-24 children cleared ``subtract_background`` and then failed one
stage later, identically:

.. code-block:: text

    stage failed: gain_match after 0.3ms (KeyError: 'verbose')
      File "pipeline/differenceImageSubs.py", line 200
        verbose = int(gainmatch_dict['verbose'])

``[gainmatch]`` in ``cdf/science/pipeline.toml`` carries thirteen keys.
``gainMatchScienceAndReferenceImages`` reads seven, of which **two —
``verbose`` and ``upload_intermediate_products`` — are in no
configuration home at all.** That is the same class as cycle 1's
``swarp_header_only``: science-path release content the code requires and
the release never carried, invisible until execution reaches the stage.

**Batch reported these children SUCCEEDED.** They are not: the attempts
closed ``application_closed`` / ``rapid_outcome=failure`` /
``error_category=internal_error``, and the application exits 0 by design
after recording its own outcome, so the record is authoritative and the
scheduler's status is not. Reading the Batch status as the answer is
precisely the mistake the attempt-record design exists to prevent.

The fix-round budget is exhausted at three cycles, so this one is
recorded and not attempted. Re-plan is the owner's.

What the ramp needs, now that a child gets this far
-----------------------------------------------------

The ramp's mechanics are settled and its gates are unchanged — width-2
probe, then 180, then 540, each gated on that step's attempt records, and
a width-2 probe before each widening. What the revision-26 probe changes
is the **time budget** for running them.

Every earlier probe resolved in about four minutes because it failed
early. A child that reaches the difference catalogues takes at least an
order of magnitude longer, and the drip phase then has to run long enough
to say something about steady-state arrival. Planning the ramp on the
old four-minute figure would under-book it badly.

The submission side is ready: ``scripts/q8_ramp_probe.py`` takes the
width as an argument, so 180 and 540 are the same command with a
different number, and the default ``max_batch_size`` of 500 means 180 is
one array while 540 cuts into two — worth knowing before reading the
run-id suffixes, since ``submit_gathered`` re-scopes each batch to
``<run_id>-<n>``.

The exit criterion is unmet
---------------------------

smoke-run.md's exit needs a ramp step of several hundred concurrent jobs
with every attempt terminal and explained, products written, and the drip
phase holding the latency target. **No ramp step ran** — the gate before
the first widening is a clean width-2 probe, and the probe was not clean.
**No full-scale proposal follows**, and the ~42 h run remains the owner's
call with no measured basis yet.

What the round did change: the operator gate is open and proven open, the
image is current at **revision 24** with pins consistent at five sites,
**all three** blocking defects the round set out to fix are fixed and
proven live, and the fourth is located to the line.

Proposed, not ratified
----------------------

* **Audit every science stage's config reads against the three homes,
  once, mechanically.** Cycle 1 was twenty dropped ``[swarp]`` keys;
  cycle 4 is two dropped ``[gainmatch]`` keys; both were found by a
  science child reaching the stage. A test that walks the stage sources
  for ``*_dict['key']`` reads and compares them against
  ``pipeline.toml`` would have found both at once, before any Batch
  child was submitted. This is the single highest-value item on this
  list — the ramp cannot be trusted while the same class keeps surfacing
  one stage at a time.
* **Pre-flight the science path without Batch.** Four probes have now
  spent eight children to find four defects, each one stage further
  along. A local or single-container dry pass over one unit would find
  the next such defect in seconds rather than in a submission cycle.
* **``rapid-ccfits`` fetches from heasarc.gsfc.nasa.gov at build time**
  and timed out on all four retries, twice tonight, costing two full CI
  cycles. The same URL returned HTTP 200 in under two seconds from the
  laptop each time, so this is a runner-to-GSFC path flake, not an
  outage. Every other package in the stack fetches from a pinned local
  or vendored source.
* **A scheduled prune for rapid-admin's image cache.** Tonight's
  reclamation was one-off: 42 tags removed, 14 G → 27 G. At ~5 GB per
  image and a build most sessions, the host returns to a wedged state
  within a few builds.
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

1. **The ruling on cycle 4.** ``[gainmatch]`` needs ``verbose`` and
   ``upload_intermediate_products``, or the code needs to stop reading
   them. It is release content by the ratified criterion — both can
   alter a science product — so the conservative reading is that they
   belong in ``pipeline.toml``. The fix-round budget is spent, so this
   is a re-plan rather than a fourth cycle.
2. **Decide whether to keep fixing one stage at a time.** Four probes,
   four defects, each one stage further down the same path, every one a
   thing a mechanical check could have found without submitting
   anything. The "Proposed" list's first two items are that check; the
   alternative is discovering stage five the same way.
3. **A width-2 probe after the cycle-4 fix**, both children reaching
   terminal with ``rapid_outcome=success``. Note the bar: not Batch
   ``SUCCEEDED``, which these children reported while failing.
4. Only then the ramp: 180 → 540 → drip, each step gated on clean
   attempt records, width-2 probe before each widening. The submission
   mechanics are now proven through ``submit_units`` with registration
   intact — a bounded, capped array with no VPO involved.

Carried forward unactioned: registration granularity (a handful of bad
records aborts the whole operator pass — now demonstrated at fourteen),
the ``RAPID_VPO_DRY_RUN`` semantics, memory over-reservation, and the
legacy ``.ini`` reads.
