W8: the failure-injection battery, and what it proved
=====================================================

:Status: run against the live stack 2026-08-06, ``W8-BATTERY-OK``, 33/33
:Harness: ``pipeline/reconciler/test/live_w8_battery.py``
:Image: ``sha256:8c10d1e3…`` (smdc ``9be19d4``), the digest both job
        definitions and the reconciler service are pinned to

This is the acceptance gate the Batch payload co-design names in its W8 row:
deterministic failure semantics proven *before* scale, against the real
schema, the real resolver, real S3 create-once semantics and real Batch.

Why it is not a unit test — the property being tested is a property of those
systems, and a stub cannot stand in for any of them:

* a stub cannot **refuse a state**; the lifecycle constraints can, and four
  cases below turn on a transition being rejected by the database;
* a stub cannot enforce **create-once**; S3's conditional write can, and the
  supersession and idempotency cases turn on exactly that;
* a stub cannot **lose a race**; two writers against one row can.

Everything the battery writes is additive, under a run id stamped with the
UTC time it started. Nothing was deleted. The rows and records it left are
legitimate attempt records — they describe attempts that really were made.


The table
---------

.. list-table::
   :header-rows: 1
   :widths: 3 22 34 41

   * - #
     - Case
     - Mechanism (what breaks without it)
     - Evidence
   * - 1
     - Tool exits 1
     - As-is finding #1: a science job could not fail, because only exit
       ``>= 64`` propagated. If ``run_tool`` swallows a nonzero exit, the
       fail-loud posture is decorative.
     - ``ToolError``, ``returncode=1``, category ``tool_failure``
   * - 2
     - Missing binary
     - Finding #2: an uncaught ``FileNotFoundError`` escapes the taxonomy
       entirely, so the record carries ``internal_error`` — or no record —
       for what is squarely a tool failure.
     - ``ToolError``, ``returncode=127``, category ``tool_failure``; NOT a
       bare ``FileNotFoundError``
   * - 3a
     - Wrong workload class
     - The route matrix binds job type, queue, definition and DB lane into
       one validated tuple. If a science manifest could run on the bulk
       definition, three independently selectable facts are back.
     - ``RouteError``: "job type 'science' runs on the prompt class, but
       this job definition's…"
   * - 3b
     - Wrong queue
     - The queue is a submit-time parameter Batch does not bind to the
       definition, so the runtime must check it too.
     - ``RouteError`` naming ``batch/queue-prompt`` and the queue given
   * - 3c
     - Right route accepted
     - A rejection case that passes because a *key* is missing proves
       nothing. The happy path is what shows the keys resolve.
     - accepted, ``lane=transaction``
   * - 3d
     - Reference-image is bulk
     - The matrix's second row, checked in the same shape as the first.
     - ``RouteError`` on the prompt class
   * - 4
     - Manifest type vs definition class
     - The entrypoint's startup check is the second line of defence; both
       must hold or reference-image work reaches the prompt queue.
     - ``RouteError`` from ``Manifest.validate_for``
   * - 5
     - Snapshot persistence fails
     - Digest and snapshot key are bound in the SAME write that marks
       started, so there is no bound-but-unpersisted state. A swallowed
       failure yields attempts whose digest describes configuration nobody
       can reconstruct.
     - ``RecordsError``, raised before any work
   * - 6a
     - Record create-once
     - Idempotent-by-identity: the reconciler must replay termination after
       a crash without double-writing.
     - identical checksum across a replay
   * - 6b
     - Replay keeps the published record
     - A replay is deliberately NOT byte-identical (a later ``ended_at``),
       so comparing content and raising would misdiagnose the ordinary
       crash-and-retry case; overwriting would mutate an immutable record.
     - first record's bytes survive; its checksum is what the caller gets;
       ``created=False``
   * - 6c
     - Identity collision raises
     - A *different* attempt deriving this key means two attempts share an
       identity and one is about to lose its account.
     - ``RecordsError`` naming the key
   * - 7a
     - Retention stamped
     - The bundle key is classification-neutral, so retention rides on a
       tag the reconciler stamps at classification time.
     - ``{'retention-class': 'success', 'attempt-id': '1',
       'producing-release': 'rapid-w8-battery'}``
   * - 7b
     - Monotonic retag, full set
     - The S3 tagging API replaces the WHOLE tag set, so a correction must
       rewrite the canonical set — and the case verifies the whole set, not
       the retention tag alone.
     - all three tags correct after the correction to ``failure``
   * - 7c
     - Shortening retag refused
     - A reclassified failure must never inherit the success expiry.
     - the success-ward rewrite returns ``None``; live class still
       ``failure``
   * - 8
     - Unreadable tags raise
     - Finding #16: converting a read failure into "no tags" let a
       transient error replace failure-class retention with the shorter
       success expiry — and terminal rows are outside the open set, so
       nothing would notice.
     - ``TagsUnreadable``, not ``None``
   * - 9
     - Kill before the started CAS
     - ``terminal_without_start`` must be REACHABLE. No work happened, by
       construction, because snapshot and started are one write.
     - ``lifecycle_state=terminal_without_start``, ``started_at`` NULL
   * - 10
     - Kill after started, before work
     - The started row carries digest and snapshot key, so the attempt is
       fully reconstructible — the point of binding them together.
     - ``state=started``, snapshot and digest both bound
   * - 11
     - Started CAS matches once
     - Two writers (a late runtime, a reconciler) must not both start one
       row. The rowcount contract needs a real driver.
     - second ``mark_started`` raises ``AttemptNotFound``
   * - 12
     - Crash between record and row
     - The S3 record is written BEFORE the application-closed CAS, so a
       crash between them leaves a started row beside a valid record. The
       reconciler materializes the row FROM it, values verbatim, marked.
     - ``started -> application_closed``,
       ``reconciler_materialized=True``
   * - 13
     - No retry on clean application failure
     - Scheduler-SUCCEEDED with application-failure is the representable
       combination the schema was built for. A nonzero exit here would burn
       retries re-running work that fails identically.
     - ``application_intended_exit=0``, outcome ``failure``, category
       ``tool_failure``
   * - 14
     - Reconciler category refused to the application
     - **Defect found and fixed.** ``_validate_error_category`` checked the
       UNION, so ``mark_application_closed`` accepted
       ``scheduler_reclaimed`` — an application authoring an observation
       only the scheduler observer can make. "No field has two writers"
       held by convention alone on that path.
     - ``ValueError``: "'scheduler_reclaimed' is reconciler-authored and
       cannot be set by an application-closed transition"
   * - 15
     - Registration refuses application failures
     - Refused "by taxonomy, not by exit-code folklore".
     - 1 failed attempt in the run, 0 selected as registrable
   * - 16
     - Supersession, consumer selection
     - The application writes sequence 0; only the reconciler writes
       higher. Keys sort by zero-padded sequence, so the consumer's
       selection is ``max``.
     - highest is ``seq-0001.json``, outcome ``failure``
   * - 17
     - Reconciler record is a complete snapshot
     - Every reconciler record folds in the predecessor's facts, so the
       highest-sequence record alone is the full account and consumers
       never chain-fold.
     - the superseding record carries attempt, run and logical-job identity
   * - 18
     - Checksum-invalid predecessor
     - "Validated by key and checksum — never by mere presence." Trusting
       presence folds corrupt application facts into a canonical snapshot.
     - rejected, reason ``identity_mismatch``
   * - 19
     - **A real poll cycle**
     - Everything above proves a property in isolation; this proves the
       service composes them against systems that can refuse, race and time
       out. W6b routed this case here explicitly: "W8 owns running it on
       the orchestrator host."
     - ``{'open': 77, 'observed': 7, 'classified': 0, 'skipped': 43,
       'deferred': 34, 'errors': 0}``
   * - 20
     - Health clean after a good poll
     - A service that cannot report health cannot be supervised.
     - ``healthy=True``, 0 consecutive failures
   * - 21
     - Health flips on consecutive failures
     - The FIXED behaviour. A reconciler reporting healthy while every poll
       failed is worse than an absent one — the unit never restarts and
       nothing notices. Injected by revoking the scheduler client, which is
       what a partition or a revoked grant really takes away.
     - healthy by poll ``[True, True, False, False]``, threshold 3
   * - 22
     - Watermark is a sequence
     - A boolean cannot express "registered at 0, and there is now a 1",
       which is what makes "reprocesses on a later supersession" sayable.
     - ``registered_record_sequence`` is ``integer``
   * - 23
     - Cutover backlog (N/A)
     - "Not applicable" has to be SHOWN, or it is indistinguishable from
       "not checked".
     - 0 pre-August unregistered terminal rows; the legacy submitter is
       deleted, so no new ones can appear
   * - 24
     - No log-grep or done-files
     - A surviving log-grep path is a second outcome authority, the one
       thing the design forbids outright.
     - AST scan of the production modules: no executable literal survives.
       The only mentions are docstrings recording the deletion — and the
       case parses rather than greps precisely so it does not report the
       explanation as the offence.
   * - 25
     - Reconciler-first on a never-started attempt
     - "The reconciler closes EVERY attempt." Without it an attempt simply
       stops being accounted for.
     - sequence 1, ``reconciler_first=True``, category
       ``scheduler_provisioning`` — machine-readable, never a null
   * - 26
     - Reconciler-first carries the binding
     - The submission-time binding is copied at row creation, so a record
       for an attempt that never ran still knows its definition and image.
     - the image digest survives into the record


A third defect, found by the RUNNING service
--------------------------------------------

Worth separating from the battery's two, because nothing in the battery
found it and nothing could have: it appeared only once the reconciler was
running as a service, against rows a previous step had left in a particular
state.

``mark_missing_or_contradictory`` moved a row's lifecycle state but left
``reconciler_materialized`` at whatever it already was. Migration 013's
``attempts_reconciler_materialized_check`` permits that flag ONLY in
``application_closed`` or ``terminal_after_start`` — so a row that had been
legitimately materialized from its record, and whose scheduler observation
later disagreed, hit ``CheckViolation`` on **every** poll: permanently
unclassifiable, and counted as a poll error forever.

The service's own log is what surfaced it — ``poll: {… 'errors': 3}``, then
``'errors': 4`` as more rows reached that state. A stub cannot refuse a
state; a constraint can, which is why the unit suite was silent.

Fixed by clearing the flag with the transition, which is also the honest
value: it says "this row's application facts were projected from the record
by another writer", and a row being flagged missing-or-contradictory is
exactly the case where that projection is no longer what the row asserts.

**Proven live, not just unit-tested.** The service was stopped, the fixed
tree staged over the pinned image, and four consecutive cycles run against
the same rows that had been failing: ``errors: 0`` on every one, and zero
``CheckViolation`` in the log. The service was then restarted on the
deployed image. Like the tessellation fix, it is committed and pushed but
not yet in an image — the same rebuild picks up both.


Two defects the battery found
-----------------------------

**The application could author a reconciler-only error category** (case 14).
``mark_application_closed`` validated against ``ERROR_CATEGORIES``, the union
of both halves of the vocabulary, so an application could write
``scheduler_reclaimed`` or ``scheduler_provisioning`` — observations only the
scheduler observer makes. Fixed with an application-side allowlist; the
reconciler's own writes and the schema's foreign key still use the union,
which is correct for both.

**Two battery cases were wrong before the code was** — recorded because a
probe that passes for the wrong reason is worse than one that fails:

* ``3b`` first passed because the parameter keys were invented
  (``batch/prompt-queue``, not the tree's ``batch/queue-prompt``), so the
  wrong-queue case was really exercising "the tree does not carry this key".
  The keys now come from ``route_for``, and ``3c`` asserts the happy path
  immediately after, which is what shows they resolve.
* ``24`` first failed on a docstring describing the deleted chain, then on
  ``test_consumer.py``'s own banned-list literals — the repo's structural
  assertion of the same property. It now parses the AST of production
  modules only.


What the live proof ran, and what it found
------------------------------------------

One registration job was submitted through the **production seam**
(``seams.submit_units``) on the prompt queue — job
``272a9367-e6d0-4886-b8e6-399d48a2fc8b``, run ``w8-live-20260806T170745Z``.
The submitter's log records the ordering FixA's finding #2 fixed, in the
order the design requires:

1. manifest published to ``s3://roman-rapid-products/submissions/…``,
   checksum ``abd3cbfe…``;
2. logical job recorded with the image digest;
3. **attempt row 108 created** — before ``SubmitJob``;
4. the array job submitted (1 child, ``rapid-queue-prompt``);
5. scheduler job ids backfilled.

The container then got remarkably far, and every step of it is the redesign
working:

* route validated — ``job_type=registration class=prompt
  queue=rapid-queue-prompt lane=transaction``;
* the **pre-created row claimed** at application index 1 ("claimed
  pre-created row");
* the configuration snapshot persisted and **bound in the started CAS**.

Then it died: ``ImportError: cannot import name 'RomanTessellation'``, exit
70, classified ``internal_error``. The reconciler picked the row up and
recorded the scheduler's ``FAILED``.

**That is a real defect, and the live proof is what found it.**
``tessellation_provenance`` imported a class name that does not exist — W7's
class is ``RomanTessellationClosedForm`` — so *every job of every type* would
have failed identically before running a stage. Nothing caught it because
the import is function-local and every unit suite stubs that module, so the
import resolved against a stub that answers to anything. Fixed, with a
regression test that deliberately reaches past the stubs to the real module.

The failure is also, in its way, the protocol's own proof: an unrecordable
error produced exit 70, a started row with its snapshot bound, and a
scheduler observation — exactly the sequence the design specifies for a job
that dies before it can record its own account.

**The fix is proven in the image, but the image does not carry it.** W8's
rebuild budget is two iterations and both were spent (the tessellation
retirement, then the reconciler and test-collection fixes). Rather than take
a third, the fix was proven the same way the reconciler's was — staged over
the pinned image on rapid-admin:

* ``RomanTessellationClosedForm`` resolves, and carries ``check_version``;
* ``get_rtid(11.1, -43.8)`` returns **5321355**, the value the 2024
  conversion note works through by hand and W6b's state summary cites — so
  the closed form is semantically right, not merely importable;
* ``tessellation_provenance`` imports the closed-form class.

A second live job was NOT submitted. Running one would have meant pushing a
scratch image and creating a job-definition revision pinned to it — live
state beyond what this job is authorized to create, for a marginal gain,
since the first job already proved submission ordering, the claim, the
snapshot binding and reconciliation. **Proposed:** the next rebuild picks up
``df214ff`` and one registration job is submitted against it, which closes
the lifecycle to ``application_closed`` and a terminal record.


The pooler RPM: still not published
-----------------------------------

W6b's dnf-transaction hypothesis is **applied but untested**, and the one
authorized promoter run was consumed without exercising it.

The hypothesis, from W6b: ``rpms/smoke-test.sh`` installed everything in one
dnf transaction, and that list contained both ``rapid-release`` and three
third-party repo-definition packages. Installing a repo definition
mid-transaction lands a new ``.repo`` file, dnf imports that repo's GPG key,
and the cache directory the pending packages were downloaded into is
invalidated underneath the running transaction — ``rapid-release`` being
simply the first file it then cannot find. The fix moves those three into
their own transaction after the rest, the same ordering fix as
``rapid-fleet-config`` already being last.

The run itself failed in **75 seconds**, nowhere near the ~50 minutes a
smoke-test failure takes, and for a reason that has nothing to do with the
hypothesis::

    FAIL: a newer build-rpms.yml run is in flight — deferring to it rather
    than racing: 31122582925#1 (queued)

That is the promoter's own guard behaving correctly. The push carrying the
smoke-test fix triggered ``build-rpms.yml`` at 17:15:13Z; the promoter
started at 17:14:58Z and deferred to it. The two raced by fifteen seconds.

**And that CI run could never have gone green, for a reason worth recording
separately: the repository has ZERO registered self-hosted runners.**
``build-rpms.yml``'s jobs require them, so both the original run and the
rerun sat exactly 15 min 02 s — ``scope`` and ``lint`` from 17:39:21Z to
17:54:23Z on the second attempt — with **no step ever recorded**, and were
then cancelled by GitHub's runner-acquisition timeout. Every downstream job
was skipped. Nothing in the workflow executed on either attempt.

This is not transient contention, and waiting longer would not have helped:
``gh api repos/…/actions/runners`` reports ``total_count: 0``. The promoter
defers to any in-flight run and refuses to publish while main is red, so
until a runner is registered, **the promoter cannot be retried at all** —
the smoke-test hypothesis is untestable, not merely untested.

**Verdict: not proven, not disproven.** The consequence is unchanged from
W6b: ``rapid-pgbouncer`` 1.0-4 is still unpublished, rapid-db still runs
1.0-2, ``rpm -V`` still reports ``S.5....T.`` on
``/etc/pgbouncer/pgbouncer.rapid.ini``, and the three ``.bak`` files stay in
``/etc/pgbouncer`` because the package still does not match the live file.

**Proposed:** re-run the promoter once ``build-rpms.yml`` #31122582925 is
green, with no push in flight. Nothing else is needed — the fix is committed.

One thing W8 did establish that changes the urgency: the pooler line for
``rapid_orchestrator`` is **not** a blocker. The reconciler authenticates
through the pooler on 6432 today, with 1.0-2 installed and no per-user line
at all, because ``auth_query`` resolves it. The RPM's users line is a
pool-sizing refinement, not a gate.


What could NOT be proven, and why
---------------------------------

**The science, reference-image and post-process live jobs are blocked on
data, not on this layer.** The g0001 population is fully registered — 5,166
``l2files`` with matching ``l2filemeta``, fid 8, 109 distinct fields, ~48
frames per field — but ``PSFs`` and ``RefImages`` are both **empty**, and no
PSF artifact exists in the products bucket or the staged-input bucket.

* ``reference_image.download_reference_psf`` requires ``psf_uri``, from
  ``PSFs``;
* ``build_reference_image`` requires ``coadd_inputs_uri``, a CSV the deleted
  launcher used to build;
* ``science`` requires both a PSF and a reference image.

So the first stage of the first job type has no input. Producing PSFs is
science work, outside W8's scope and authorization. Registration is the one
type this database can support today, which is why it is the type that ran.

The blocking item for the remaining three is therefore: **a PSF set and a
first reference image for g0001**. Once those exist, the per-type proof is
the same submission this ran, with the job type changed.
