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
   * - Prompt-queue packing at ramp width
     - **measured, 180-wide**
     - The CE scaled from 0 to **960 desired vCPU on 60
       ``m6a.4xlarge``** within ~2 minutes of submission — **3 children
       per host**, the same packing density the first run's 109-wide bulk
       step showed (~2.95/host), and bound by the 16 GiB memory
       reservation against 64 GiB hosts rather than by vCPU: 180 children
       at 4 vCPU each is 720 of the 960 provisioned.

       **The prompt queue changes family with width.** At width 2 it took
       a single ``r6i.2xlarge``; at 180 it took ``m6a.4xlarge`` — the
       same family the bulk queue selects. So the earlier "the two queues
       select different families" reading was a width artefact, not a
       queue property, and the MaxvCpus ratio cannot be set from the
       narrow measurement.

       **The hosts are CPU-idle while they work.** A sampled worker held
       ~19% of 16 vCPU under full load — three cores busy on a 16-core
       host, one per child, consistent with the single-threaded finding
       from the width-2 probe. So 13 of every 16 cores are unused, the
       16 GiB reservation is what caps density, and **the packing lever
       is memory, not vCPU**: the MaxvCpus numbers describe a ceiling the
       workload never approaches.

       The 540-wide step measured **the same ~19%** on its sampled
       worker. Packing density and per-host load are identical at three
       times the fan-out — the extra children arrive as more hosts, not
       as more pressure on each — which is what makes the 180-wide
       latency figures a fair predictor of the wider step's.
   * - Prompt-vs-bulk concurrency and packing → the 3,600/1,200 MaxvCpus ratio
     - **partial, superseded in part by the row above**
     - Bulk, 109-wide: 37 ``m6a.4xlarge``, ~2.95 children/host, 436 vCPU
       of 1,200, packing bound by the 16 GiB memory reservation. Prompt,
       2-wide: one ``r6i.2xlarge`` (8 vCPU / 64 GiB) took both children
       and reported **0 vCPU and 30 GiB remaining** — CPU-bound, the
       opposite of the bulk case. The two queues select different
       families and bind on different resources, so the ratio cannot be
       set from the bulk measurement alone.
   * - Memory-heavy packing across six families
     - **not measured, and not measurable as things stand**
     - Two families seen (``m6a.4xlarge`` bulk, ``r6i.2xlarge`` prompt),
       neither under contention. The blocker is now identified rather
       than merely unaddressed: **Batch workers do not run the
       CloudWatch agent**, so ``CWAgent/mem_used_percent`` has no
       datapoints for them — deliberate for ephemeral hosts, but it means
       memory headroom cannot be read from CloudWatch during a ramp. What
       is available is the reservation Batch itself enforces and the
       instance's own vCPU accounting; actual RSS per child needs either
       the agent on the worker AMI or the payload recording its own high
       water mark into the attempt record. Proposed, not ratified.
   * - Scratch I/O at 150 GiB gp3
     - **partial**
     - 20 GiB of 150 (14%) on a bulk host carrying 3 reference children.
       The science path's shape is unmeasured — it died at stage R.
   * - Spot-reclaim retry semantics
     - **not measured**
     - Spot stays DISABLED; no trial authorized.
   * - Per-SCA latency against the one-hour/95% target
     - **MEASURED at 180-wide — the number the smoke run existed to get**
     - 180 children, every one ``success`` / ``published``:

       * min **3,039 s** (50.7 min)
       * mean **3,419 s** (57.0 min)
       * **p95 3,604 s — 60.1 min**
       * max **3,721 s** (62.0 min)
       * wall clock, submission to last completion: **3,956 s** (65.9 min)

       **p95 lands 4 seconds over the 3,600 s target.** Not "close to" —
       over, by a margin far inside the noise of a single run, which is
       the honest way to state it: at this width the pipeline sits
       exactly on the target and does not clear it.

       The spread is narrow (min to max is 22 minutes, p95 only 5 minutes
       above the mean), so this is not a tail problem to be tuned away —
       it is where the whole distribution sits. And the ramp's contention
       cost is small: the uncontended width-2 probe ran 3,371 s and
       3,300 s, so 3 children per host over 60 hosts added roughly a
       minute to the mean. **The time is science, not scheduling.**
   * - Queue/startup/execution/publication intervals per SCA
     - **partial, superseded by the row above for the headline figure**
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
     - **measured at 180-wide and 540-wide; settled**
     - ``max_connections`` is 200 throughout.

       * 109-wide (first run): 23 backends, 7 RAPID
       * **180-wide: 31 backends (15.5%), 8 RAPID**
       * **540-wide: 32 backends (16.0%), 14 RAPID**

       **Tripling the fan-out cost one backend.** The pooler decouples
       connection count from concurrency essentially completely, so the
       connection budget does not bind the ramp and will not bind the
       full-scale run either. This row can be closed.
   * - Backup-window behaviour under load
     - **not measured**
     - No run intersected a window.
   * - EBS aggregate quota
     - **measured at rest AND under a 180-wide ramp**
     - 4,594 GiB against 51,200 GiB (``L-7A658B76``) at rest — **9.0%**.
       With the 180-wide step's 60 hosts up: **13,594 GiB, 26.6%** —
       ~9,000 GiB for 60 workers, i.e. ~150 GiB each, exactly the
       configured scratch. Extrapolating the same per-host figure to
       540-wide (180 hosts) gave ~31,600 GiB, ~62%.

       **The 540-wide step then measured 31,594 GiB — 61.7% — on 180
       hosts**, which is the projection to within 6 GiB. The scratch
       sizing is linear in host count and the quota is not the binding
       constraint at this width; it would become one somewhere above
       ~290 hosts.
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
   * - Where the science time actually goes — **the full path, measured**
     - **measured end to end, and the headline of the round**
     - The revision-26 probe ran **all twenty stages to publication**.
       Both children: ``rapid_outcome=success``,
       ``product_disposition=published``, no error category, **3,371 s
       and 3,300 s** — 56 and 55 minutes each, on an ``r6i.2xlarge``
       carrying only those two children.

       **Three stages are 92% of it**: ``naive_difference`` 1,077.0 s,
       ``catalog_zogy`` 1,054.5 s, ``catalog_sfft`` 1,001.5 s. The other
       seventeen total ~236 s, of which ``run_sfft`` is 78.7 s,
       ``prepare_zogy_inputs`` 68.8 s, ``upload_products`` 36.4 s and
       ``run_zogy`` 25.5 s. Everything before ``prepare_zogy_inputs`` —
       the thirteen stages that four probe cycles were spent debugging —
       is **25 s in total**.

       118 product objects were written for one unit.
   * - Where the science time goes, as it looked mid-run
     - **superseded by the row above; kept for the reasoning**
     - The stages after ``run_zogy`` are far heavier than the thirteen
       before it. Revision 26's probe spent **~103 s reaching**
       ``run_zogy`` and then more than **ten minutes** in the difference
       catalogues, logging nothing while photutils fits PSFs on a
       7,000×7,000 difference image ("Input data contains unmasked
       non-finite values", then silence). **The measured floor is at
       least 49 minutes** — that is how long the two children had been in
       this stage when this was written, still running, CPU still pinned;
       it is a lower bound, not the figure. Every earlier probe died
       before this point, so no previous measurement saw it. A per-SCA latency
       budget built on the pre-``run_zogy`` timings would be wrong by an
       order of magnitude, and the long silent stretch is also a
       liveness-monitoring problem: the log stream stops advancing while
       the job is perfectly healthy, so log freshness is not a health
       signal here.

       **This is the round's most consequential measurement.** A single
       SCA takes **56 minutes with no contention at all** — two children
       alone on an 8-vCPU host, one vCPU each. The one-hour/95% working
       target is therefore already 93% consumed by one uncontended unit,
       before any queueing, any cold start (add ~3 min), and any effect
       of running several hundred of these at once. The ramp cannot
       improve this: 180 and 540 add contention, they do not remove work.

       This is a science-and-sizing question for the owner, not something
       another fix cycle closes, and the three stages that hold 92% of
       the time are where any answer has to start. Worth noting that the
       target is a *working* one and the ratified acceptance contract
       lives in ``rapid_plan``; nothing here changes it, this only
       measures against it. Two children at width 2 on a dedicated host say nothing
       about what several hundred concurrent children do to the same
       stage — that is exactly what 180 and 540 are for.

       That the job was working rather than wedged is established from
       the host, not from the absence of an error: the ``r6i.2xlarge``
       carrying both children held a flat **~25.2% CPU for twenty
       minutes** — 2 of 8 vCPUs pinned, one per child. **The science path
       is single-threaded per child**, which is the fact that makes the
       packing question a memory question rather than a CPU one.
   * - Scale-up from zero at 540-wide
     - **measured**
     - Submission 08:49:22Z into a cold queue. **T+72 s** 79 hosts and
       1,280 vCPU; **T+3 min 17 s** 180 hosts and **2,880 vCPU** with 240
       running; **T+6 min 24 s** 492 of the first 500 running. The CE
       provisions in two visible waves (1,280 then 2,880) rather than one
       jump.

       **2,880 vCPU against MaxvCpus 3,600** — the 540-wide step reaches
       80% of the prompt queue's ceiling. It is the first measurement that
       puts a real number against that limit, and it says the current
       ceiling supports roughly 675 concurrent children at this packing,
       not appreciably more.
   * - Scale-up from zero at 180-wide
     - **measured**
     - Submission 07:41:13Z. **T+95 s** the CE had raised desired vCPU
       0 → 960; **T+2 min 39 s** 60 hosts existed and 100 children were
       STARTING; **T+4 min 43 s** 151 of 180 were RUNNING. So a
       180-child step is fully in flight inside five minutes of a cold,
       zero-capacity queue, and **all 180 were RUNNING at T+7 min 51 s**
       — the scheduler and the image pull are not the bottleneck at this
       width. Compare the ~3 min cold start for a single child: the
       marginal cost of 178 more children was under five minutes.
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

**The job definitions carry no timeout** (``describe-jobs`` reports
``timeout: null``), which was harmless while every child failed inside
four minutes and is not harmless now that the normal case is tens of
minutes. A child that genuinely hangs will hold its slot indefinitely,
and at 540-wide that is 540 slots. Setting an ``attemptDurationSeconds``
above the measured worst case — once the ramp establishes what that is —
is proposed, not ratified; the measurement has to come first, because a
timeout set below the real distribution manufactures exactly the mass
identical-failure class the ramp's stop condition watches for.

The 540-wide step, and where the record stops
-----------------------------------------------

540 children went out in two batches (500 + 40, cut by the default
``max_batch_size`` of 500). All 540 reached ``started`` with zero
failures; the CE provisioned 180 hosts and **2,880 of 3,600 MaxvCpus**,
EBS reached **61.7%**, and the pooler moved from 31 backends to 32.

**The session's AWS credentials then expired**, at 09:15:56Z, about 26
minutes into a step whose children take ~56 minutes. Re-authentication
needs a browser click or a device code that an unattended run cannot
supply, so every AWS read stops there. The last verified reading is
09:14:51Z: **500 running, 0 failed**.

Two things follow, and they are different. The step's *outcome* is
unrecorded here — nobody has read those 540 attempt records. But the step
itself is unaffected: Batch owns the children, the reconciler runs
in-account on the pinned digest, and the attempt records are written by
the application regardless of whether this session can see them. **The
work continues and completes without the observer.** Reading it back is
a query away for whoever holds credentials:

.. code-block:: sql

    select lifecycle_state, rapid_outcome, error_category,
           product_disposition, count(*)
      from attempts where run_id like 'q9-ramp540%'
     group by 1,2,3,4;

The 180-wide step's gate is complete and clean, so the ramp's second
gate is met on the record; the third is pending that read.

The exit criterion, and what is left of it
-------------------------------------------

smoke-run.md's exit needs a ramp step of several hundred concurrent jobs
with every attempt terminal and explained, products written, **and** the
drip phase holding the latency target.

**The first half is met.** The 180-wide step closed 180 of 180 at
``success`` / ``published``, every attempt terminal, no error category,
nothing unexplained, products in the bucket. That is a ramp step of
several hundred concurrent jobs completing cleanly, which no previous
session reached.

**The second half is not, and one part of it may not be reachable as
built.** The drip phase never ran — the 540-wide step was still in flight
when this session's credentials expired. And the latency target is the
harder problem: p95 at 180-wide is 3,604 s against 3,600 s. A drip phase
cannot fix that, because the time is science rather than queueing and the
distribution is narrow.

So the exit is **not** declared, and **no full-scale proposal follows**:
the ~42 h run remains the owner's call. But it is now a call with
measured numbers under it rather than none — which was the point of the
smoke run.

What this round changed, end to end: five blocking defects fixed and
proven live, the image current at **revision 26** with pins consistent at
five sites, the dropped-key class closed by a mechanical gate rather than
by one more probe, a science exposure processed to publication for the
first time, and the per-SCA latency measured at ramp width.

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

Closing the round: which clock the latency target is measured on
================================================================

The single most consequential number in this document was measured on
the wrong interval, and correcting it changes the verdict rather than
refining it.

**The previous segment reported p95 3,604 s at 180-wide, "4 seconds over
the 3,600 s target".** That figure is arithmetically right and measures
``started_at → ended_at`` — the time a child spends executing once a host
is already running it. Re-measured on the same 180 rows:

.. list-table::
   :header-rows: 1

   * - Clock
     - Mean
     - p95
     - Max
   * - ``submitted_at → ended_at`` (arrival to done)
     - 3,651 s
     - **3,837 s**
     - 3,957 s
   * - ``started_at → ended_at`` (execution only)
     - 3,420 s
     - 3,605 s
     - 3,722 s
   * - ``scheduler_started → stopped`` (container only)
     - 3,422 s
     - 3,606 s
     - 3,724 s
   * - ``scheduler_created → stopped`` (scheduler view)
     - 3,646 s
     - 3,831 s
     - 3,951 s

The two clocks differ by the ~231 s each SCA waits between being
submitted and having a host to run on.

**For a latency target under continuous arrival, the arrival clock is the
only defensible one.** ``smoke-run.md`` asks for "per-SCA latency against
the one-hour/95% working target", and an SCA that waits four minutes for
capacity is four minutes late no matter what the container's own clock
says. Measuring from ``started_at`` silently excludes exactly the delay
that continuous arrival exists to stress, and it is the interval a
queue-depth problem would show up in.

On the arrival clock, both ramp steps miss the target by much more than
four seconds:

.. list-table::
   :header-rows: 1

   * - Step
     - n
     - Mean
     - p95
     - Max
     - Within 3,600 s
   * - ``q9-ramp180``
     - 180
     - 3,651 s
     - 3,837 s
     - 3,957 s
     - **53 of 180 — 29%**
   * - ``q9-ramp540`` (both batches)
     - 540
     - 3,684 s
     - 4,022 s
     - 4,142 s
     - **174 of 540 — 32%**

Against a **95%** target, the pipeline delivers **29–32%**. The earlier
framing — "sits exactly on the target and does not clear it" — was a
consequence of the clock, not of the pipeline. The honest statement is
that the one-hour target is missed by a wide margin at both ramp widths,
and tripling the width from 180 to 540 cost only ~185 s at p95, which
says the shortfall is not a contention effect that a gentler arrival
pattern will remove.

This does not contradict the previous segment's diagnosis — that the time
is science rather than scheduling, with three stages holding 92% of it.
It sharpens it: the science alone (3,605 s at p95, execution-only) is
already over 3,600 s before a single second of queueing is counted. No
arrival pattern can fix a per-SCA cost that exceeds the target on its
own.

The backup window
-----------------

``smoke-run.md`` asks for backup-window behaviour under load. The plan is
``dailyplan``, rule ``dailyrule``, ``cron(0 5 * * ? *)`` in UTC with a
60-minute start window — confirmed against three consecutive completed
EFS jobs, all created at 05:00 UTC.

**No run in this segment intersected it.** The ramp steps ran
07:41–09:58 UTC and the drip 12:43–14:4x UTC; the window is 05:00–06:00
UTC. So the row is answered as *did not intersect* rather than measured
— an important distinction, because a 42-hour full-scale run cannot
avoid it and will cross it twice. Recording "no effect observed" from a
run that never overlapped the window would be exactly the kind of false
clean this round has been trying to eliminate.

Pooler draw under continuous arrival
------------------------------------

Measured mid-drip with 122 children in flight: **32 backends against a
``max_connections`` of 200**, one of them active. The breakdown is 8
reconciler, 13 unnamed, the rest one-per-payload
(``rapid-payload:<batch>:<index>``).

That is the same 31–32 seen at 180-wide and at 540-wide. **Three
different concurrency levels — 122, 180 and 540 — draw the same number
of backends**, which is the pooler doing exactly what it is for:
transaction pooling decouples backend count from child count, so
database sizing is not a function of fan-out. The question is closed for
any width the compute environments can reach.

Scale-up under arrival, rather than in one step
-----------------------------------------------

The ramp measured scale-up from cold in a single jump. The drip
exercises the case flight actually produces — capacity growing while
work is already running:

* wave 1 (60 children): CE desired **336 vCPU across 21 hosts**
* waves 1–2 (122 children): CE desired **656 vCPU across 42 hosts**

Roughly 3 children per host at both points, matching the reservation-
bound packing measured at 180- and 540-wide, and host CPU held at
**15.9% → 18.8%** — the same ~19% the ramp reported. Adding a second
wave while the first was mid-flight neither disturbed the packing nor
produced a queueing backlog: Batch scaled the environment underneath a
running population.

The overlap is measured, not assumed
------------------------------------

A drip is only a drip if wave *N* is still running when wave *N+1*
arrives; otherwise the cadence has produced six small bulk runs. Counted
from the attempt records — children submitted but not yet ended at the
moment each wave arrived:

.. list-table::
   :header-rows: 1

   * - Wave
     - Arrived (UTC)
     - In flight at arrival
   * - ``q9-drip-probe``
     - 12:48:16
     - 2
   * - ``q9-drip-w1``
     - 12:49:10
     - 62
   * - ``q9-drip-w2``
     - 12:59:23
     - 122
   * - ``q9-drip-w3``
     - 13:09:36
     - 182
   * - ``q9-drip-w4``
     - 13:19:47
     - 242

The population accumulates rather than turning over, which is the
condition the drip exists to create. Five arrivals in, nothing had
completed and every wave was still resident.

**Warm arrival costs ~10 seconds; cold arrival costs 2.5 minutes**, and
the warm figure held for every wave after the first:

.. list-table::
   :header-rows: 1

   * - Wave
     - Arrived
     - First start
     - Delay
   * - ``w1``
     - 12:49:10
     - 12:51:39
     - **2 m 29 s** (cold)
   * - ``w2``
     - 12:59:23
     - 12:59:34
     - 11 s
   * - ``w3``
     - 13:09:36
     - 13:09:45
     - 9 s
   * - ``w4``
     - 13:19:47
     - 13:19:56
     - 9 s

Only the first wave pays the ~3 min cold start that matches the 2026-08
baseline; every later arrival lands on hosts that already exist. That difference is the strongest argument in this document
for continuous operation over batched: under steady arrival, all but the
first wave pay no scheduling latency at all, and the queue interval that
pushes the ramp's arrival-clock p95 over the target largely disappears.

Memory: the row recorded as unmeasurable is measurable
------------------------------------------------------

The previous segment recorded memory per child as **"not measurable as
built — Batch workers run no CloudWatch agent"**. That is true of
CloudWatch and false of the conclusion: **the Batch worker AMI registers
with SSM**, so ``free`` and ``ps`` run on the workers directly. No agent,
no payload change, no new instrumentation.

Five live science workers under the drip, sampled together:

.. list-table::
   :header-rows: 1

   * - Host
     - Used / total
     - Children
     - Per-child RSS
   * - ``ip-10-100-161-8``
     - 3,033 / 62,924 MB
     - 3
     - 989, 987, 978 MB
   * - ``ip-10-100-170-175``
     - 3,002 / 62,924 MB
     - 3
     - 994, 981, 979 MB
   * - ``ip-10-100-166-5``
     - 3,018 / 62,924 MB
     - 3
     - 985, 980, 980 MB
   * - ``ip-10-100-171-92``
     - 3,036 / 62,924 MB
     - 3
     - 991, 988, 983 MB
   * - ``ip-10-100-162-28``
     - 2,988 / 62,924 MB
     - 3
     - 980, 978, 976 MB

**Per-child RSS is ~985 MB against a 16 GiB reservation — over-reserved
by roughly 16×**, and the figure is stable to ±1% across five
independent hosts, so it is the payload's steady draw rather than a
sampling artefact. Each host has ~59 GB of its 61 GB available.

This is the packing lever, quantified. Memory binds packing to 3 children
per host while CPU sits at ~19% and disk at 12% — but it binds on the
*reservation*, not on demand. The three constraints now read:

* **vCPU**: ~19% used — not binding
* **Disk**: 17 GiB of 150 (12%) — not binding
* **Memory**: 3 GB of 61 used, 48 GB reserved — **binding, and on a
  number ~16× the measurement**

Sizing the reservation to the measured draw is the single change with the
largest effect on cost per SCA, and it needs no science work. It is
proposed, not ratified: the right value is a decision about headroom for
the heaviest stage, and the ~985 MB figure here is a steady-state
average, not a high-water mark. A payload that recorded its own peak RSS
into the attempt record would settle the headroom question properly —
which remains the correct long-term fix, now for tuning rather than for
visibility.

Scratch I/O at the 150 GiB gp3 sizing
--------------------------------------

The previous segment could answer this only for a bulk host carrying
reference children (20 GiB of 150), because the science path died at
stage R. It now runs. On a science worker with three children resident:

* **17 GiB of 150 used (12%)**, 134 GiB free; ``/var/lib/docker`` is 33 GB
  of that footprint
* ``VolumeQueueLength`` peaks at **1.6** and otherwise sits near zero
* Write pattern: a ~3.1 GB/s average burst during image pull and startup,
  near-idle through the long PSF-fitting stages, then ~488 MB/s as
  products are written

The 150 GiB gp3 sizing is comfortable and the volume is not a bottleneck
at this packing. Instance-store reconsideration is not indicated by
anything measured here.

The three constraints, together
-------------------------------

Every sizing row in ``smoke-run.md``'s evidence table is now answered
from measurement rather than from projection, and they point the same
way:

.. list-table::
   :header-rows: 1

   * - Resource
     - Measured
     - Binding?
   * - vCPU
     - ~19% host CPU, identical at 122/180/540
     - No
   * - Disk
     - 17 GiB of 150 (12%), queue length ≤1.6
     - No
   * - Database
     - 32 backends of 200, identical at 122/180/540
     - No
   * - EBS aggregate
     - 10,744 GiB of 51,200 (21%) at 42 hosts
     - No
   * - Memory
     - ~985 MB per child against 16 GiB reserved
     - **Yes — on the reservation, not the draw**

**Nothing the smoke run measured is actually saturated.** The one
binding constraint binds on a declared number that overstates real
consumption by ~16×, which makes packing density a configuration
decision rather than a hardware one.

That reframes the cost question for the full-scale run: at the measured
draw the same hosts could carry substantially more children, and the
latency shortfall is unaffected either way because it is per-SCA science
time, not contention. Sizing and latency are now cleanly separable
problems.

The Q8 exit assessment
======================

``smoke-run.md`` states the criterion:

    a ramp step of several hundred concurrent jobs completes with every
    attempt terminal, records complete and explained, products written,
    and the drip phase holding the latency target.

Taken clause by clause, against measurement:

.. list-table::
   :header-rows: 1

   * - Clause
     - Verdict
     - Evidence
   * - Several hundred concurrent
     - **Met**
     - 540 concurrent at the ramp's third gate; 242 concurrent in the
       drip at its fourth arrival
   * - Every attempt terminal
     - **Met**
     - 180 of 180 and 540 of 540 ``terminal_after_start``; no
       non-terminal record in either step
   * - Records complete and explained
     - **Met**
     - Zero ``error_category`` across both ramp steps and, at the last
       reading, across all 242 drip children
   * - Products written
     - **Met**
     - Every attempt ``product_disposition = published``
   * - **Drip holding the latency target**
     - **NOT met**
     - The target is 95% within 3,600 s. The ramp delivers **29–32%**
       on the arrival clock, and the drip's own figure follows from a
       per-SCA execution cost that already exceeds 3,600 s before
       queueing

**Q8 does not exit.** Four of five clauses are met — and met properly,
which no previous session reached — but the fifth is the one the smoke
run exists to answer, and it is missed by a wide margin rather than
narrowly.

The reason is now precise, which is the useful part. Per-SCA execution
alone is **3,605 s at p95** against a 3,600 s target; ``naive_difference``,
``catalog_zogy`` and ``catalog_sfft`` hold 92% of it. Continuous arrival
removes essentially all of the queueing component — waves 2–4 started
9–11 s after arriving — so the drip does what a drip can do and cannot
reach the target, because no arrival pattern fixes a per-SCA cost that
exceeds the target on its own.

**No full-scale proposal follows from this run**, per the standing
instruction: the ~42 h run is the owner's call. What this segment adds to
that call is that the decision is no longer between "hit the target" and
"do not know" — it is a scoped science-performance question against three
named stages, with every infrastructure constraint measured and none of
them saturated.
