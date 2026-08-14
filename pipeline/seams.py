"""The VPO's three seams onto the new chain.

The operations design owns the ground-up VPO; this co-design takes only the
touchpoints the payload forces. Those are exactly three, and they are here
rather than inline in `virtualPipelineOperator.py` so each is testable on its
own and so the VPO's own structure is left alone.

  submit_units      submission through the manifest/array layer, with attempt
                    rows pre-created BEFORE the children can start
  wait_for_completion   the completion wait, reading the attempt table the
                    reconciler maintains, with a stated timeout
  run_registration  registration invoked as the records consumer

What each replaces:

  submit_units      `run_tool([python, awsBatchSubmitJobs_launch*.py, ...])`
                    — a subprocess exec of a launcher that submitted one
                    `submit_job` per unit and wrote a per-job `.ini` to S3.
  wait_for_completion   a `while True` polling `describe_jobs` ONE JOB PER
                    CALL from a one-shot database snapshot, with no timeout —
                    a stuck job waited forever.
  run_registration  `run_tool([python, parallelRegisterCompletedJobsInDB.py,
                    date])` — the log-grep chain.
"""

import datetime
import logging
import time

from observability.attempts import (
    AttemptWriter, ExecutionBinding, LifecycleState)
from pipeline.intent.errors import is_unique_violation
from pipeline.intent.writer import (
    READY, SUBMITTED, WRITER_ORCHESTRATOR, WRITER_VALIDATION_INGEST,
    WorkUnitIdentity, WorkUnitWriter)
from submission.batching import Batch, batch_units
from submission.manifest import Manifest
from submission.submit import S3ManifestStore, publish_manifest, submit_batch

logger = logging.getLogger("rapid.seams")

#: How long the VPO waits for a batch to finish before handing it to
#: reconciliation and moving on. The old wait had no timeout at all: a stuck
#: job blocked the operator forever. This turns that into a bounded wait and a
#: reconciliation case — the reconciler will classify the stragglers within
#: its own horizons whether or not anyone is watching.
DEFAULT_COMPLETION_TIMEOUT = 6 * 60 * 60

#: Cadence of the completion poll. The attempt table is cheap to query and the
#: reconciler updates it every 60s, so there is nothing to gain from polling
#: faster than it writes.
DEFAULT_POLL_SECONDS = 60

#: Lifecycle states that mean the reconciler is done with an attempt.
#:
#: `missing_or_contradictory` belongs here. It is not an open state waiting to
#: resolve: it is the design's FINAL outcome for stores that disagree — the
#: reconciler has published the closure record, made its decision, and flagged
#: the attempt for a human. No ordinary transition follows it. Omitting it
#: meant a correctly-flagged attempt stayed "outstanding" forever, so the VPO
#: waited out the full six hours and raised `CompletionTimeout` over work the
#: reconciler had already finished deciding.
_TERMINAL = (
    LifecycleState.TERMINAL_AFTER_START.value,
    LifecycleState.TERMINAL_WITHOUT_START.value,
    LifecycleState.MISSING_OR_CONTRADICTORY.value,
)

_PROGRESS_SQL = (
    "SELECT lifecycle_state, count(*) FROM attempts"
    " WHERE run_id = %s GROUP BY lifecycle_state"
)


class CompletionTimeout(RuntimeError):
    """The batch did not finish inside the stated timeout.

    Not an error in the pipeline: a bounded wait expiring is the *designed*
    outcome for a stuck job. The caller records it and moves on; the
    reconciler owns what happens to the attempts.
    """

    def __init__(self, message, run_id=None, outstanding=None):
        super().__init__(message)
        self.run_id = run_id
        self.outstanding = outstanding


class SubmissionFailed(RuntimeError):
    """`SubmitJob` failed after the attempt rows were pre-created.

    Not a lost-work case and deliberately not a rollback: the rows exist and
    are correct, they simply have no scheduler job to point at. They sit in
    `submitted` with a NULL scheduler_job_id and are classified by the
    reconciler at the submission-anchored horizon — which is exactly the case
    that horizon was built for ("a pre-created child whose scheduler
    identifier never resolves is bounded by a submission-anchored horizon").

    Deleting the rows instead would be the wrong repair: it destroys the only
    evidence that work was intended, and it races a child that may in fact be
    running (a `submit_job` that times out on the client side may well have
    been accepted).
    """

    def __init__(self, message, run_id=None, attempt_ids=()):
        super().__init__(message)
        self.run_id = run_id
        self.attempt_ids = tuple(attempt_ids)


class SubmissionBookkeepingFailed(RuntimeError):
    """Batch accepted the job, but the post-call bookkeeping did not commit.

    A different failure mode than `SubmissionFailed`, and deliberately a
    different exception, because the right response is different. When
    `SubmitJob` itself fails the row-level state is unambiguous (nothing was
    accepted, or its acceptance is unknown and the protocol row says so) and
    the caller's only sound move is to leave the pre-created rows as
    reconciliation cases. Here `SubmitJob` **succeeded** — `submission.job_id`
    is real and Batch is, right now, running (or about to run) every array
    child — so `submit_job` must NEVER be called again for this run
    (`submission/protocol.py`'s module docstring, rule 3). What failed is
    purely OUR bookkeeping: marking the submission row `bound` and backfilling
    each child's `scheduler_job_id` onto its attempt row, which (finding 4,
    fix-txn-core) now commit together as one transaction so a partial
    backfill can never become the durable last word silently.

    Because that transaction never committed, the rows are exactly where they
    were before this call — `submitted`, no scheduler id, the `calling`
    marker (or nothing, on the pre-protocol degrade path) still recorded
    durably from step 2a. That is a SAFE state to retry the bookkeeping from:
    the reconciler's submission-anchored horizon already treats a row in this
    shape as a reconciliation case, and re-running the backfill (never the
    call) is exactly what a retry should do. `scheduler_job_id` is carried
    here specifically so a caller that wants to retry need not re-derive it
    from `submission.child_job_id`.
    """

    def __init__(self, message, run_id=None, scheduler_job_id=None,
                attempt_ids=()):
        super().__init__(message)
        self.run_id = run_id
        self.scheduler_job_id = scheduler_job_id
        self.attempt_ids = tuple(attempt_ids)


def submit_units(units, job_type, queue, job_definition, binding,
                 manifest_bucket, manifest_prefix, s3_client, batch_client,
                 execute, run_id=None, reason="vpo", job_name=None,
                 now=None, reference_observation_window=None,
                 protocol_commit=None):
    """Submit one array job for `units`, with its attempt rows pre-created.

    ORDER MATTERS, and it is the reason this is one function rather than two
    calls the VPO makes in sequence: the rows are created BEFORE `submit_job`
    returns children that could start. A child that starts before its row
    exists finds no logical job, cannot copy an execution binding, and is
    flagged `missing_or_contradictory` by the resolver — correct behaviour on
    the resolver's part, and a self-inflicted wound on the submitter's.

    That was the stated contract and the code did the opposite (review finding
    #2): `submit_batch` ran first and `_precreate` after it, so the race this
    function exists to prevent was live. The order below is now the documented
    one, and the ordering itself is asserted by a test.

    **The scheduler job ids therefore arrive second, and that is the point.**
    A row cannot carry a child job id that Batch has not assigned yet, so the
    rows are created without one and backfilled after `SubmitJob` returns —
    the ordering `observability.attempts` was built for and documents ("Array
    children are rows at submission time... then backfilled by
    `backfill_scheduler_job_ids`. That ordering is the point: a child whose
    identifier never resolves is left as a detectable reconciliation case
    rather than never existing at all").

    The manifest is published first, before the rows, because the rows carry
    its checksum in their execution binding — an attempt must always know
    exactly which manifest it was submitted under.

    `reference_observation_window` is the sole enumerated science override
    (design/compute.md § Job definitions), carried into the manifest here so
    it is checksummed with it and bound into every attempt row — which is
    what "recorded by construction" means, and what lets a promotion gate
    refuse a product built under one. None means no override: the window's
    authoritative value is release content.

    `protocol_commit` is a zero-argument callable that COMMITS the caller's
    transaction, and it is called TWICE, at the two instants this function's
    own transactional shape requires a boundary (fix-txn-core, findings 2-4 —
    see `pipeline.operator.service._execute_factory`, which is what actually
    supplies it on the live path: `ConnectionExecutor(conn,
    autocommit_each=False)`, so nothing here commits unless this function
    says so):

    1. BEFORE `submit_batch`, between marking the submission record `calling`
       and calling Batch (rule 7, brief C1, unchanged from the original
       reasoning). The submission record's `calling` state must be DURABLE at
       the instant `submit_job` goes out: a row saying "a request is in
       flight" is worthless if it is still uncommitted when the process dies,
       because the pass that later finds the wreckage sees `prepared` and
       confidently concludes no call was made — a wrong answer stated with
       certainty, which is worse than the no-answer this protocol replaces.
       This same commit is also what makes finding 2's fix real rather than
       cosmetic: with `autocommit_each=False`, EVERYTHING from the work-unit
       advisory lock through the CAS transition through the `unit_events` row
       through the attempt's `work_unit_id` FK attachment is now one
       transaction, and this is the point at which all of it becomes durable
       — before Batch is ever called, exactly where rule 7 already required a
       boundary for a different reason. Two findings, one commit.

       That is NOT a transaction spanning `SubmitJob` — it is the mechanism
       by which none does. Rule 7's first clause ("No transaction spans
       SubmitJob") still holds and is strengthened: the transaction is
       deliberately CLOSED before the call, leaving nothing open across it.

    2. AFTER step 4 (backfilling the scheduler job ids Batch assigned),
       before this function returns (finding 4). Marking the submission
       `bound` and backfilling every child's `scheduler_job_id` must commit
       TOGETHER or not at all: `_bind_scheduler_jobs` now raises
       `SubmissionBookkeepingFailed` rather than warning when the backfill
       count comes up short, and because nothing between the first commit
       and here has committed yet, that raise leaves the connection's
       transaction uncommitted — so the caller's eventual rollback (or the
       connection simply closing without a commit) discards the `bound`
       write along with the failed backfill, rather than leaving `bound`
       durable with orphaned children. See `_bind_scheduler_jobs` and
       `SubmissionBookkeepingFailed` for why this is a safe, retryable state
       and specifically NOT a reason to call `submit_job` again.

    None means "do not commit", leaving the protocol's writes in the caller's
    transaction. Every caller predating this brief passes nothing and keeps
    its existing transaction shape; what it forgoes is the durability
    guarantee both commits above exist for, which is no worse than the
    pre-protocol behaviour such a caller already had.

    **STEP 0, BEFORE THE MANIFEST EXISTS: WORK-UNIT AUTHORIZATION (finding
    1, fix-state-gate).** `_authorize_units` decides, PER UNIT, whether this
    call is the one entitled to submit it — the `ready -> submitted` CAS
    winner, or the creator of a fresh unit — and only units it authorizes
    survive into the manifest. Everything else in this function used to run
    unconditionally against whatever `units` it was handed: a re-gathered
    subject whose work unit was already `submitted` (a second operator
    replica, a stale gathered list) or `blocked`/`complete`/`failed` under
    the mutation API still got an attempt row and still rode into
    `submit_batch`, because `_attach_work_unit`'s non-ready branch attached
    without transitioning but never told its caller "this one is not
    yours". The CAS in `WorkUnitWriter.transition_unit` always guaranteed
    only one WINNER of `ready -> submitted`; it never stopped every OTHER
    caller from submitting anyway.

    This has to run before `Manifest(...)` below, not after: a manifest's
    unit list and checksum are fixed at construction (`Manifest.units` is a
    tuple), so an unauthorized unit cannot be dropped once the manifest
    exists without republishing a different manifest under the same
    identity — which the manifest store already refuses. Filtering first
    means the array job Batch actually runs never contained the
    unauthorized unit at all, rather than containing it and being asked to
    un-contain it after the fact.

    `execute is None` (predates the intent-layer ruling, IR-13) skips
    authorization entirely and submits every unit unfiltered — the same
    posture `_precreate`'s own `execute=None` branch already has for
    attaching work units, extended here for the same reason: a caller with
    no intent-layer connection has no work_units table to authorize against.
    """
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    units = list(units)
    work_unit_ids = None
    if execute is not None:
        authorized, work_unit_ids = _authorize_units(
            execute, job_type, units, moment)
        if len(authorized) != len(units):
            logger.info(
                "run %s: %d of %d gathered %s unit(s) were not authorized "
                "for submission (their work unit is not 'ready') and are "
                "excluded from this manifest", run_id, len(authorized),
                len(units), job_type)
        units = authorized
    manifest = Manifest(units=units, batch_id=run_id, job_type=job_type,
                        reference_observation_window=(
                            reference_observation_window))
    batch = Batch(manifest=manifest, reason=reason)

    store = S3ManifestStore(manifest_bucket, prefix=manifest_prefix,
                            client=s3_client)

    # 1. Publish the manifest. Its checksum is part of the binding the rows
    #    carry, so it has to exist before they do.
    manifest_uri = publish_manifest(batch.manifest, store)

    # THE RELEASE COMES FROM THE ADMISSION, NOT FROM THIS PROCESS'S
    # ENVIRONMENT (rule 18, brief H2).
    #
    # `binding.release_identity` is `RAPID_RELEASE_IDENTITY` as the submitting
    # process happens to have it (`pipeline/operator/submission.py:274`). Work
    # derived from an admission must instead be pinned to the release THAT
    # ADMISSION was made under — that is what makes "rollback changes only the
    # release used for future admissions" mean anything, because otherwise the
    # release a piece of work carries is a property of whichever process
    # submitted it.
    #
    # `binding_release_for_units` reads the admitted release for this
    # manifest's units and reconciles it with the environment's. A
    # DISAGREEMENT IS REFUSED LOUDLY rather than resolved in favour of either:
    # preferring the environment is the defect being repaired, and preferring
    # the admission silently would hide a submitter running the wrong image.
    # It also refuses a manifest whose units were admitted under two different
    # releases — one manifest carries one release, the submission-side half of
    # the linearization the admission side guarantees by reading the pointer
    # once.
    #
    # `require_stamp=False` because work admitted BEFORE DRAFT 051 landed
    # carries no stamp and must still be submittable; the fallback is explicit
    # and is logged by the reconciler rather than being a silent default.
    release_identity = binding.release_identity
    if execute is not None:
        from pipeline.intent.admission_release import (
            binding_release_for_units, stamp_schema_present)
        if stamp_schema_present(execute):
            release_identity = binding_release_for_units(
                execute, _admission_units_of(batch.manifest),
                binding.release_identity, require_stamp=False)

    bound = ExecutionBinding(
        job_definition_arn=binding.job_definition_arn,
        job_definition_rev=binding.job_definition_rev,
        image_digest=binding.image_digest,
        release_identity=release_identity,
        manifest_checksum=batch.manifest.checksum(),
    )

    # 2. The rows, BEFORE SubmitJob. No scheduler job ids yet — nothing has
    #    assigned any. `work_unit_ids` (step 0) carries the ALREADY-DECIDED
    #    work_unit_id for each surviving unit, in manifest order — see
    #    `_precreate`'s docstring for why it now ATTACHES to a decision made
    #    here rather than deciding again itself.
    writer = AttemptWriter(execute)
    attempt_ids = _precreate(writer, batch.manifest, run_id, bound, moment,
                             execute=execute, work_unit_ids=work_unit_ids)

    # 2a. THE SUBMISSION RECORD (rule 7, brief C1). Opened `prepared` — the
    #     manifest exists and the rows exist, but nothing has been asked of
    #     Batch, so a crash here loses nothing because no job can exist. The
    #     protocol WRAPS the ordering above rather than replacing it: steps
    #     1-4 and their reasoning are untouched.
    #
    #     Degrades to the pre-protocol path when DRAFT migration 044 is not
    #     applied. That is what lets this branch run against the deployed
    #     schema while the change request is pending — the submission still
    #     happens, it simply has no durable record, exactly as before.
    submission_id = _open_submission(
        execute, batch=batch, job_name=job_name, queue=queue,
        job_definition=job_definition, manifest_uri=manifest_uri,
        binding=bound, attempt_ids=attempt_ids, moment=moment,
        commit=protocol_commit)

    # 3. Submit. A failure here leaves the rows as reconciliation cases, not
    #    orphans: they are correct, they simply never got a scheduler job.
    try:
        submission = submit_batch(
            batch=batch, job_queue=queue, job_definition=job_definition,
            store=store, client=batch_client, job_name=job_name,
            manifest_uri=manifest_uri)
    except Exception as exc:
        # THE CALL'S OUTCOME IS AMBIGUOUS, AND THAT IS NOW RECORDED rather
        # than inferred later from a NULL scheduler id and a stopwatch. The
        # row moves to `unknown`, and a resolution pass answers it by
        # positively re-querying Batch for the job name — never by calling
        # submit_job again for this submission.
        _mark_submission_unknown(execute, submission_id, exc,
                                 commit=protocol_commit)
        logger.error(
            "SubmitJob failed for run %s after %d attempt row(s) were "
            "pre-created; the rows remain as reconciliation cases and the "
            "submission record carries the ambiguity for identity re-query: "
            "%s", batch.manifest.batch_id, len(attempt_ids), exc)
        raise SubmissionFailed(
            f"SubmitJob failed for run {batch.manifest.batch_id} after "
            f"{len(attempt_ids)} attempt row(s) were pre-created: {exc}",
            run_id=batch.manifest.batch_id,
            attempt_ids=attempt_ids) from exc

    # 3a. The call returned an id: the happy path's end.
    _mark_submission_bound(execute, submission_id, submission.job_id)

    # 4. Backfill the child job ids the scheduler has now assigned.
    #
    #    ATOMIC WITH STEP 3a (finding 4, fix-txn-core).
    #    `_mark_submission_bound` and `_bind_scheduler_jobs` used to be two
    #    independently-autocommitted writes: the submission could land
    #    durably `bound` while the backfill that was supposed to follow it
    #    silently warned and left children unaddressable by scheduler id
    #    forever. With `execute` now running inside one uncommitted
    #    transaction (finding 2) and `_bind_scheduler_jobs` raising rather
    #    than warning on a short backfill (below), neither write is durable
    #    until the commit two lines down — so a backfill failure here leaves
    #    BOTH uncommitted, and `bound` never becomes the durable last word
    #    over an incomplete backfill.
    _bind_scheduler_jobs(writer, submission, attempt_ids)

    if protocol_commit is not None:
        # DURABLE BEFORE RETURNING (finding 4). Without this, a caller that
        # commits only once — at the pre-Batch boundary `_open_submission`
        # already uses — would have `bound` and every backfilled
        # scheduler_job_id sitting in the SAME open transaction this
        # function's caller may or may not ever commit, which reopens
        # exactly the gap this finding closes: Batch has unambiguously
        # accepted the job, and that fact is not yet safe from a crash.
        protocol_commit()

    logger.info("submitted %s batch %s as job %s (%d children, %d rows)",
                job_type, submission.batch_id, submission.job_id,
                submission.array_size, len(attempt_ids))
    return submission, attempt_ids


def submit_gathered(units, job_type, queue, job_definition, binding,
                    manifest_bucket, manifest_prefix, s3_client, batch_client,
                    execute, run_id, max_batch_size=None, reason="vpo",
                    now=None, reference_observation_window=None,
                    protocol_commit=None):
    """Batch a gathered unit list and submit every batch. The VPO's entry.

    `submit_units` submits ONE array job, which is the right unit of work for
    it: one manifest, one binding, one set of rows. But a gathering pass
    returns however many units are ready, and the array ceiling is a hard
    limit — so something has to cut the list and submit each piece. That
    something was missing, which is part of why nothing in production called
    `gather_*`, `batch_units` or `submit_units` at all.

    Each batch gets its own run-scoped identity (`<run_id>-<n>` where there is
    more than one), because two batches are two manifests and a manifest's
    identity is what its children resolve their unit by. Reusing one identity
    across batches would put two different unit lists under one manifest key —
    which the manifest store now refuses outright.

    `protocol_commit` (finding 3, fix-txn-core) is forwarded to EVERY
    `submit_units` call unchanged — this function does not call it itself.
    Before fix-txn-core this parameter did not exist here at all, which meant
    `submit_units`'s `protocol_commit` slot was unreachable from the one
    caller (`pipeline.operator.submitters.LiveSubmitter.submit`) that goes
    through this function: `submit_gathered` is `LiveSubmitter`'s entire
    entry point, so a parameter this function did not accept and forward was
    a parameter the live path could never supply, however carefully
    `submit_units` itself documented it. Each batch this loop submits is a
    separate array job with its own pre-Batch and post-Batch commit points
    (see `submit_units`'s docstring); passing the SAME `protocol_commit`
    callable to every iteration is correct because it is bound to the one
    connection this whole gathering pass shares (`LiveSubmitter.submit`
    opens exactly one `_execute_factory()` context for every batch a poll
    cuts), and each `submit_units` call commits it at its own two boundaries
    regardless of how many other batches share the connection.

    Returns the list of (submission, attempt_ids) pairs, one per batch. A
    batch that fails to submit raises: its rows remain as reconciliation
    cases, and continuing to the next batch would hide that from the operator.
    """
    units = list(units)
    if not units:
        logger.info("nothing ready to submit for job_type=%s", job_type)
        return []

    kwargs = {} if max_batch_size is None else {"max_batch_size": max_batch_size}
    batches = batch_units(units, **kwargs)
    logger.info("submitting %d %s unit(s) in %d batch(es)",
                len(units), job_type, len(batches))

    results = []
    for index, batch in enumerate(batches):
        batch_run_id = run_id if len(batches) == 1 else f"{run_id}-{index}"
        results.append(submit_units(
            batch.manifest.units, job_type=job_type, queue=queue,
            job_definition=job_definition, binding=binding,
            manifest_bucket=manifest_bucket, manifest_prefix=manifest_prefix,
            s3_client=s3_client, batch_client=batch_client, execute=execute,
            run_id=batch_run_id, reason=reason, now=now,
            # Every batch of one gathering pass carries the same override:
            # they are one submission cut by the array ceiling, not runs
            # under different windows.
            reference_observation_window=reference_observation_window,
            protocol_commit=protocol_commit))
    return results


def _admission_units_of(manifest):
    """The admission grains this manifest's units derive from (rule 18).

    Only EXPOSURE/SCA-GRAIN units have an admission to look up: `exposure`
    and `sca` are real identity there and nowhere else
    (`submission/payloads.py:242-257`). A crossmatch unit carries a proc_date
    and a field, which name no admitted file — so it contributes nothing here
    rather than being coerced into a shape it does not have, which is the
    sentinel-carrier defect rule 11 removed.

    Returns `("l2file", expid, sca)` tuples: the L2 grain, because that is the
    grain a science unit's inputs were actually admitted at. The exposure
    grain's release is the same one by construction — both are stamped from
    the same pointer read in one admission run — so looking up the finer grain
    costs nothing and is more precise.
    """
    units = []
    for unit in getattr(manifest, "units", ()) or ():
        payload = getattr(unit, "payload", None)
        exposure = getattr(payload, "exposure", None)
        sca = getattr(payload, "sca", None)
        if exposure is None or sca is None:
            continue
        units.append(("l2file", exposure, sca))
    return units


def _precreate(writer, manifest, run_id, binding, moment, execute=None,
               work_unit_ids=None):
    """One logical job and one attempt row per array child, before SubmitJob.

    The logical_job_id MUST be the id the runtime will resolve with — the
    manifest unit's RUN-SCOPED key — because `resolve_attempt` claims the
    pre-created row by matching on it. A submitter that keys rows differently
    creates rows the runtime can never claim: every child then makes a second
    row, and every pre-created row is orphaned in `submitted` until a horizon
    classifies it. The two sides agreeing is the whole mechanism.

    **RECORDS ITS OWN ARRAY POSITION (migration 060, closing finding 4's
    FOUND-recovery gap).** The loop below already enumerates `manifest.units`
    in the exact order `_bind_scheduler_jobs` later re-derives Batch child ids
    from (`<parent job id>:<index>`) — that order IS the array order, fixed
    once the manifest is checksummed. `array_index=index` persists that same
    position onto the row via `create_submitted`, so a FOUND recovery running
    later, in a different process, with only a `submissions` row in scope
    (`submission.protocol.resolve`), can re-derive it too, rather than only
    the in-memory loop that created the rows ever knowing it.

    **The key is run-scoped (review finding #3).** It used to be `unit.key` —
    exposure/SCA — which is a global identity: `logical_jobs` has a global
    primary key on it, so reprocessing one exposure/SCA under a second run hit
    the `ON CONFLICT DO NOTHING` and silently retained the FIRST run's
    execution binding. A scheduler retry then copied that stale manifest,
    image, release and run identity onto a row belonging to the new run. The
    key now carries the run, so two runs over one exposure/SCA are two logical
    jobs, which is what they are.

    No `scheduler_job_id` is passed: Batch has not assigned one yet, and that
    is precisely why this runs before `SubmitJob`. `_bind_scheduler_jobs`
    fills them in afterwards.

    **TYPED IDENTITY** (co-design ruling 2). The logical-job key and the
    attempt row's identifier columns both derive from the manifest's job
    type's DECLARED SUBJECT (`submission.subjects`) rather than from
    `unit.key` alone — a crossmatch manifest's units carry the same
    exposure/SCA-shaped carrier for every field of one processing date, so
    keying on `unit.key` would collide them exactly as the accumulator's
    dedup used to (see `manifest.ProcessingUnit.dedup_key`'s docstring).
    Job types the typed-identity registry does not cover fall back to the
    exposure/SCA identity every job type used before this ruling — only
    `UnknownJobType` (the registry has no declaration at all for this job
    type) is caught for that fallback; a KNOWN job type's unit missing one
    of its declared components is a real defect and propagates rather than
    being silently absorbed into the fallback shape.

    **INTENT-LAYER ATTACHMENT (integration review ruling 13, v1).** Every
    new operational attempt gets a `work_unit_id`, per design/operations.md
    § Workflow schema ("attempts gain one FK to their work unit") and the
    task brief's v1 rule ("required on new operational attempts"). `execute`
    is the SAME injected callable this whole submission path already uses —
    passing it through (rather than instantiating a second, differently
    wired writer) is what lets `attempts.work_unit_id` and the intent
    layer's own rows land through one connection, in the caller's one
    transaction, exactly as `logical_jobs`/`attempts` already do.

    `execute=None` is accepted and treated as "skip the intent layer for
    this call" rather than raising, because every caller of `_precreate`
    predating the intent-layer ruling passes no `execute`, and a hard
    requirement would break them at the seam's exact boundary rather than at
    a considered migration.

    **THE SECOND REASON IS GONE, AND WITH IT THE FK GUARD** (rule 12 repair).
    This docstring used to give a second justification: no job type had a
    loaded `workflow_definitions` row, because migration 039's
    `derived.load_workflow_definition` had no caller anywhere, so
    `_attach_work_unit` swallowed a definition-FK violation to keep the seam
    working. That made the intent layer silently optional — the FK was doing
    the work a deployment step should do, and a message-substring match was
    deciding what the FK meant.

    The loader now HAS a caller (`pipeline.intent.definitions`, invoked as a
    deployment step) and the startup completeness check refuses to run a work
    stream whose definition is not loaded. A definition-FK violation at
    work-unit creation is therefore no longer an expected steady state — it
    means the deployment step did not run for an enabled stream, which is a
    hard error and propagates as one. Nothing here classifies a database
    error by message text any more; see `pipeline.intent.errors`.

    **ATTACHES TO A DECISION MADE EARLIER; NO LONGER DECIDES (finding 1,
    fix-state-gate).** `work_unit_ids` is the per-unit `work_unit_id` list
    `submit_units` computed via `_authorize_units` BEFORE the manifest — and
    therefore before this loop — ever existed: authorization has to run
    before an unauthorized unit can be excluded from the manifest at all
    (see `submit_units`'s own docstring, "STEP 0"), so by the time this
    function runs, every unit in `manifest.units` is already a known
    ready->submitted CAS winner or fresh-unit creator with a settled
    `work_unit_id`. This loop just writes that id onto the new attempt row
    (`_set_attempt_work_unit`) rather than re-running the find-or-create/CAS
    dispatch `_attach_work_unit` used to run here — running it a second time
    per unit would be redundant work at best and, worse, a second
    read-modify-write against a row the authorization pass may have already
    moved, racing against itself.

    `work_unit_ids=None` (matching `execute=None`) skips attachment
    entirely — the caller had no intent-layer connection to authorize
    against in the first place, so there is nothing to attach.
    """
    from observability.attempts import AttemptIdentity
    from submission.subjects import attempt_identity_fields

    attempt_ids = []
    for index, unit in enumerate(manifest.units):
        logical_job_id = unit.logical_job_key(manifest.batch_id,
                                              manifest.job_type)

        writer.create_logical_job(
            logical_job_id, manifest.batch_id, binding,
            job_type=manifest.job_type)

        # NO EXPOSURE/SCA FALLBACK REMAINS (rule 11). This used to catch
        # `UnknownJobType` and write `exposure_id`/`sca` off the unit — safe
        # only while every unit carried that pair regardless of grain, which
        # is exactly the sentinel-carrier defect the typed payloads removed.
        # For a field-grained unit the fallback would now raise, and writing
        # a field number into `exposure_id` is the thing co-design ruling 2
        # names by name. A job type with no declared subject cannot build a
        # payload and so cannot reach this loop at all; if one somehow does,
        # the raise is the correct outcome.
        identity_fields = attempt_identity_fields(manifest.job_type, unit)

        attempt_id = writer.create_submitted(
            AttemptIdentity(
                run_id=manifest.batch_id,
                logical_job_id=logical_job_id,
                **identity_fields),
            created_at=moment, submitted_at=moment,
            binding=binding, array_index=index)

        if execute is not None and work_unit_ids is not None:
            _set_attempt_work_unit(execute, attempt_id, work_unit_ids[index])

        attempt_ids.append(attempt_id)
    return attempt_ids


def _input_scope_for(job_type, unit):
    """The `work_units.input_scope` string for one manifest unit.

    **DELEGATES to `submission.subjects.build_input_scope`** (IR-13-a),
    which now owns the v1 stringification decision this function
    originally made: drop the declared-subject tuple's leading `job_type`
    element (work_units carries job_type in its own column) and join the
    rest with `/`, matching the delimited shape `ProcessingUnit.key`/
    `logical_job_key` already use elsewhere in this file
    (`"run-1:science/90000/1"`). Moved to `submission.subjects` rather than
    left here because the campaign gatherer (`submission.gathering.
    gather_campaign_units`) and the mock transformer's W2-fallback creator
    (`pipeline.mock.transformer.create_mock_campaign_from_staged`) both
    need to PARSE this same grammar back to `(exposure, sca)` at gather
    time, and a parser sharing a module with its own builder is what keeps
    the two directions from drifting into two grammars — see
    `submission.subjects.parse_exposure_sca_scope`. This wrapper survives
    only so every existing caller in this file keeps its short local name.
    """
    from submission.subjects import build_input_scope

    return build_input_scope(job_type, unit)


def _authorize_units(execute, job_type, units, moment):
    """Decide, PER UNIT, whether this call may submit it (finding 1).

    The gate `_attach_work_unit` never had: that function ran the SAME
    find-or-create/CAS dispatch this one runs, but its non-ready branch
    (`_transition_or_defer`, below) ATTACHED the attempt's FK regardless of
    what it found — `submitted` (another replica, or a stale gathered list
    re-offering a unit already in flight), `blocked`/`complete`/`failed`
    under the mutation API, all attached exactly like a fresh `ready` unit
    did. The CAS in `WorkUnitWriter.transition_unit` guaranteed only one
    WINNER of `ready -> submitted`; nothing stopped every LOSER — or every
    caller that was never in the race at all, because the unit was already
    settled before this poll started — from being handed an attempt row and
    riding into `submit_batch` anyway.

    Called from `submit_units` BEFORE the manifest exists (see that
    function's own docstring, "STEP 0"), so an unauthorized unit can be
    dropped from `units` before it is ever checksummed into a manifest or
    given an attempt row — this is the FIRST-LINE gate finding 5's
    `state = 'ready'` gathering-SQL predicate is meant to make rare, and the
    LAST-LINE one that closes what a stale gathered list or a second
    operator replica can still slip past it.

    Returns `(authorized_units, work_unit_ids)`: `authorized_units` is the
    subset of `units` (in the SAME relative order) this call may submit,
    and `work_unit_ids[i]` is the settled `work_unit_id` for
    `authorized_units[i]` — carried forward so `_precreate` attaches to a
    decision already made rather than re-deciding (see its own docstring).
    A unit dropped here is logged at INFO with its work unit's state and
    otherwise silently excluded: this is the ordinary "someone else already
    owns this" outcome the gate exists to produce, not an error.
    """
    authorized_units = []
    work_unit_ids = []
    for unit in units:
        work_unit_id, ok = _decide_work_unit(execute, job_type, unit, moment)
        if ok:
            authorized_units.append(unit)
            work_unit_ids.append(work_unit_id)
    return authorized_units, work_unit_ids


def _attach_work_unit(execute, job_type, unit, attempt_id, moment):
    """Find-or-create this unit's work unit, decide, and attach if authorized.

    A thin wrapper over `_decide_work_unit` kept for its own direct callers
    (`pipeline.mock.transformer`'s campaign-unit path, and this module's
    test suite) that need "decide and attach" as one call rather than the
    two-phase decide-then-attach `submit_units`/`_precreate` now use. See
    `_decide_work_unit` for the full find-or-create/CAS/race-tolerance
    contract; this function adds only the attempt-row attachment
    (`_set_attempt_work_unit`) `_decide_work_unit` itself does not do.

    **NOT AUTHORIZED MEANS NOT ATTACHED (finding 1).** This used to attach
    unconditionally — `_transition_or_attach`'s non-ready branch attached
    the attempt's FK to a `submitted`/`blocked`/`complete`/`failed` unit
    exactly as it attached to a fresh `ready` one. A caller reaching this
    function with a unit whose work unit is not authorized for submission
    gets no FK write; see `_decide_work_unit` for what "not authorized"
    means and why silently skipping it here (rather than raising) is
    correct — an existing caller of THIS function that wants the old
    submit-regardless behaviour never existed on the live path, which now
    goes through `_authorize_units`/`_precreate` instead.
    """
    work_unit_id, ok = _decide_work_unit(execute, job_type, unit, moment)
    if ok:
        _set_attempt_work_unit(execute, attempt_id, work_unit_id)
    return work_unit_id


def _decide_work_unit(execute, job_type, unit, moment):
    """Find-or-create this unit's work unit, and decide if it is submittable.

    **THE FIND-OR-CREATE SHAPE (task brief: document the exact SQL shape).**
    `WorkUnitWriter.find_current_unit` issues one SELECT against the
    partial unique index's own predicate
    (`WHERE job_type = %s AND input_scope = %s AND superseded_by_unit_id
    IS NULL`). Three outcomes:

    1. No row: this call is the creator. `create_work_unit(..., writer=
       WRITER_VALIDATION_INGEST, state='ready')` INSERTs, then this same
       call immediately transitions ready->submitted under
       writer='orchestrator' — two unit_events, two writer identities, one
       Python call (see the two-event reasoning inline below) — and if TWO
       callers race here, the second's INSERT hits migration 036's partial
       unique index and raises a database conflict, which this function
       does NOT catch (see below for why).
    2. A row exists, state='ready' and campaign_id IS NULL: the ordinary
       arrival-driven case — this call transitions it ready->submitted
       under writer=orchestrator.
    3. A row exists, state='ready' and campaign_id IS NOT NULL: the
       campaign-scoped case (the mock harness, part 5) — the SAME
       transition fires; nothing here needs to know it is campaign work,
       because "one lookup covers both the plain arrival case and the
       'unit was pre-created by campaign staging' case naturally" is
       exactly right — a ready row is a ready row regardless of who
       created it.

    **WHY NOT `ON CONFLICT DO NOTHING RETURNING` IN ONE STATEMENT.** The
    task brief is right that there is no simple upsert-and-transition here:
    an upsert only tells the caller whether ITS row was the one that landed
    (`RETURNING` on a no-op conflict returns zero rows), never the WORK
    UNIT ID of whichever row is actually current — and even if it did, the
    unit_events row the transition requires cannot be derived from an
    INSERT's own RETURNING clause, because "transitioned" and "created" are
    different events with different `from_state`s. SELECT-then-INSERT is
    therefore issued as two round trips deliberately, not as a shortcut
    that was skipped.

    **RACE TOLERANCE — NOW ACTUALLY IMPLEMENTED (rule 6 repair).** This
    docstring previously described a re-SELECT-after-conflict resolution and
    even cited a test asserting it; neither existed. The only `except` was
    the FK-substring guard below, so a genuine two-caller race on the same
    (job_type, input_scope) let migration 036's partial-unique violation
    propagate out of the loser's call and — through the operator loop's
    single shared try/except — abort that whole poll pass, including the
    unrelated work streams later in it.

    The claim-or-create is now closed properly: `create_work_unit`'s INSERT
    is attempted, and a unique violation means *the other racer won*, which
    is a success for this caller's purpose — the row it needed exists. It
    re-SELECTs the winning row and proceeds down the same path an ordinary
    pre-existing unit takes, so BOTH racers return the SAME work_unit_id.

    **EXACTLY ONE ready->submitted TRANSITION (rule 6's exclusivity half).**
    Resolving both racers to one id must not authorize two submissions of
    it. The re-SELECT path deliberately does NOT assume the winner left the
    row in `ready`: it re-reads the state and reuses the state dispatch
    below, so whichever racer arrives second finds either

      * `ready`  — the winner created but has not yet transitioned; this
        caller's `transition_unit` is CAS-guarded (`WHERE state = 'ready'`)
        so if the winner transitions first, this one raises
        `WorkUnitNotFound` and does not double-submit; or
      * `submitted` — the winner already transitioned, and this caller takes
        the not-authorized branch (finding 1) rather than attaching to it.

    In both orderings exactly one ready->submitted event fires. The CAS in
    `WorkUnitWriter.transition_unit` is what makes that true under real
    concurrency, not the ordering of the two SELECTs.

    **CLASSIFICATION IS BY SQLSTATE, NOT MESSAGE TEXT (rule 12).** The
    unique violation is recognized by SQLSTATE 23505 via
    `pipeline.intent.errors`, never by matching words in an exception
    message. The FK-substring guard that used to live here — which
    swallowed a missing workflow_definitions row and made the whole intent
    layer silently optional — is gone: definitions are loaded by an
    explicit deployment step (`pipeline.intent.definitions`) and verified by
    the startup completeness check, so a missing definition at work-unit
    creation is now a hard error, as it should always have been. A
    foreign-key violation here propagates.

    Returns `(work_unit_id, authorized)`. `authorized` is True only for the
    `ready -> submitted` CAS winner (fresh-unit creator, or the transitioner
    of a pre-existing ready unit — see `_transition_or_defer`); every other
    outcome returns the settled `work_unit_id` (for logging/diagnostics)
    alongside `authorized=False`, and callers (`_authorize_units`,
    `_attach_work_unit`) must not create an attempt or a Batch submission
    for it (finding 1).
    """
    identity = WorkUnitIdentity(
        job_type=job_type, input_scope=_input_scope_for(job_type, unit),
        operational_class=_operational_class_for(job_type),
        definition_version=1)
    work_writer = WorkUnitWriter(execute)

    existing = work_writer.find_current_unit(
        identity.job_type, identity.input_scope)

    if existing is None:
        # THE ARRIVAL-DRIVEN CASE: no upstream validation/ingest stage
        # created a unit ahead of submission, so THIS call is treated as
        # that creator (module docstring, part 1) — a fresh unit is
        # created ready, then immediately transitioned to submitted in
        # the same call, as TWO separate unit_events under two writer
        # identities (task brief: "Use writer='validation_ingest' for
        # the creation event and writer='orchestrator' for the
        # ready->submitted transition event"), even though both fire
        # from one Python call — the design's writer-exclusivity rule
        # is about WHO the design attributes each transition CLASS to,
        # not about how many Python calls separate them in this v1.
        try:
            work_unit_id = work_writer.create_work_unit(
                identity, writer=WRITER_VALIDATION_INGEST, state=READY,
                now=moment)
        except Exception as exc:  # noqa: BLE001 - re-raised unless 23505
            if not is_unique_violation(exc):
                raise
            # WE LOST THE RACE, AND THAT IS A SUCCESS (rule 6). Between our
            # SELECT and our INSERT another transaction created the current
            # unit for this exact (job_type, input_scope) and committed;
            # migration 036's partial unique index refused ours. The row we
            # needed exists — so re-SELECT it and fall through to the SAME
            # state dispatch a pre-existing unit takes. Both racers end up
            # returning one work_unit_id, and AT MOST one of them is
            # authorized (finding 1) — see `_transition_or_defer`.
            existing = work_writer.find_current_unit(
                identity.job_type, identity.input_scope)
            if existing is None:
                # A unique violation whose winning row cannot then be found
                # is not a race — it is a contradiction (the winner rolled
                # back after we saw its conflict, or the uniqueness we
                # violated is not the one we re-SELECT on). Never retried
                # blindly: raise the original error rather than loop.
                raise
            logger.info(
                "lost the work-unit claim race for %s/%s; resolved to the "
                "winning unit %s", job_type, identity.input_scope,
                existing["work_unit_id"])
            return _transition_or_defer(
                execute, work_writer, existing, job_type,
                identity.input_scope, moment)
        work_writer.transition_unit(
            work_unit_id, READY, SUBMITTED, writer=WRITER_ORCHESTRATOR,
            now=moment)
        return work_unit_id, True

    return _transition_or_defer(execute, work_writer, existing, job_type,
                                identity.input_scope, moment)


def _transition_or_defer(execute, work_writer, existing, job_type,
                         input_scope, moment):
    """Transition a pre-existing READY unit, or defer to whoever owns it.

    Factored out of `_decide_work_unit` because the race-loser path needs
    exactly this dispatch (rule 6: "attaching to a unit already `submitted`
    must not authorize a second submission path") and duplicating it was how
    the two branches would drift. Returns `(work_unit_id, authorized)`.

    **SUBMISSION OWNERSHIP STAYS EXCLUSIVE, AND SO DOES ATTACHMENT (finding
    1).** Renamed from `_transition_or_attach`: the `state != READY` branch
    used to attach the attempt's FK anyway ("attach without transitioning
    rather than issuing an illegal edge"), which is exactly the gap finding
    1 closes — a unit already `submitted`/`blocked`/`complete`/`failed`
    is not this caller's to submit, so this function now DEFERS (returns
    `authorized=False`) rather than attaching, and the caller
    (`_authorize_units`) excludes the unit from the manifest entirely. The
    READY branch's `transition_unit` is CAS-guarded on `state = 'ready'`, so
    two callers that both read `ready` still yield exactly one transition —
    the loser raises `WorkUnitNotFound` from the CAS rather than firing a
    duplicate (that raise propagates uncaught, same as before; a caller
    racing this closely is a genuine concurrent-CAS collision, not the
    already-settled-state case this function's dispatch otherwise handles).
    """
    work_unit_id = existing["work_unit_id"]
    if existing["state"] != READY:
        # Already submitted (a retry re-running gathering for a unit whose
        # work unit is mid-flight), parked `blocked` by retry policy,
        # closed `complete`/`failed`, or `quarantined` — not this caller's
        # to submit. DEFER: no FK attach, no transition, no attempt for it.
        logger.info(
            "work unit %s for %s/%s is %s, not ready; not authorized for "
            "submission by this pass", work_unit_id, job_type, input_scope,
            existing["state"])
        return work_unit_id, False
    # THE CAMPAIGN-STAGED CASE (and any other pre-created-ready unit): a unit
    # already exists in 'ready', created by another writer (the mock
    # transformer, part 5, or a genuine upstream validation/ingest stage this
    # v1 does not yet have) — this call only transitions it, under
    # writer='orchestrator', never re-creating.
    work_writer.transition_unit(
        work_unit_id, READY, SUBMITTED, writer=WRITER_ORCHESTRATOR, now=moment)
    return work_unit_id, True


def _set_attempt_work_unit(execute, attempt_id, work_unit_id):
    """Attach `work_unit_id` to an already-created attempt row.

    A plain UPDATE, not part of `AttemptWriter`: the column exists
    (migration 036) but no `AttemptWriter` method writes it, because it is
    written exactly once, here, immediately after `create_submitted`
    returns the row's id — there is no lifecycle transition of the
    ATTEMPT associated with gaining a work unit, only a fact being filled
    in on a row that already exists.
    """
    execute("UPDATE attempts SET work_unit_id = %s WHERE attempt_id = %s",
           [work_unit_id, attempt_id])


def _operational_class_for(job_type):
    """The operational class a work unit for this job type declares.

    **v1 JUDGMENT CALL, stated precisely.** `pipeline.operator.classes`
    declares only two of the (soon five) classes with a job_type mapping
    (prompt-processing -> science, reference-construction ->
    reference-image); `pipeline.operator.gathering.REGISTRY` — built for
    co-design ruling 1 — is the complete map that actually exists in this
    codebase, covering every job type the live operator gathers (science,
    reference-image, the six post-DB chain types, and alert-production),
    ALL of which it assigns to PROMPT_PROCESSING except reference-image.
    That is ruling 1's call, not this ruling's, and this function reuses
    it rather than re-deriving a second, possibly disagreeing mapping —
    single source of truth for "which class does this job type run under".

    `registration` and `reprocessing` are route-vocabulary job types with
    no entry in that registry at all (registration is invoked directly,
    never gathered; reprocessing is declared-not-implemented). Both fall
    back to PROMPT_PROCESSING here as the least-wrong default — arbitrary,
    and named as such — since work_units.operational_class is NOT NULL and
    this v1 has no better answer for either; a future ruling that declares
    an operational class for registration or reprocessing corrects this
    rather than this function guessing further.
    """
    from pipeline.operator import classes as opclasses
    from pipeline.operator.gathering import _BY_JOB_TYPE

    entry = _BY_JOB_TYPE.get(job_type)
    if entry is not None:
        return entry[0]
    return opclasses.PROMPT_PROCESSING


def operational_class_for(job_type):
    """Public accessor for `_operational_class_for`.

    `submission.gathering._blocked_identity` already needs this exact
    mapping — the module docstring there requires it be the SAME function
    the submission path uses, so a blocked unit's identity and a submitted
    unit's identity never diverge under migration 036's partial unique
    index — and was reaching across the package boundary to a
    single-underscore name to get it. A caller outside this module poking a
    private symbol is the thing to fix, not a thing to route around with a
    second copy of the docstring above's judgment call.
    """
    return _operational_class_for(job_type)


def _open_submission(execute, *, batch, job_name, queue, job_definition,
                     manifest_uri, binding, attempt_ids, moment, commit=None):
    """Open the submission record and mark it CALLING. Returns its id or None.

    Returns None — and does nothing at all — when DRAFT migration 044 is not
    applied, which is the deployed state until the change request lands. The
    probe is `protocol.is_available`, asking the catalog rather than trying a
    write and interpreting the failure: "the table is not deployed" and "the
    write went wrong" are different facts and must not arrive as one.

    THE COMMIT SITS BETWEEN `calling` AND THE CALLER'S `submit_batch`, which
    is the entire reason this helper exists as a separate step rather than
    being folded into the caller's flow — see `submit_units`'s docstring on
    `protocol_commit` for why durability at that exact instant is what makes
    the record worth having.

    **THE "PROTOCOL FAILURE NEVER BLOCKS A SUBMISSION" RULE IS NARROWED HERE
    (finding 3, fix-txn-core), and precisely: it now means "the protocol
    being ABSENT never blocks a submission," not "a protocol write failing
    partway through never blocks a submission."** Those used to be one
    `except Exception` and one return value, which is exactly the conflation
    `is_available`'s own docstring warns against one level up: "the table is
    not deployed" and "the write went wrong" are different facts, and this
    function used to answer both of them with the same `None`.

    The distinction matters because the two failures leave the world in
    different states. DRAFT 044 not being applied at all means NOTHING was
    attempted — there is no partial row, nothing to resolve, and degrading to
    the pre-protocol behaviour costs nothing beyond the diagnosis this
    protocol adds. That case is still caught here, still logged at debug, and
    still returns None exactly as before — see the `is_available` branch
    below, which is now the ONLY thing this function still catches and
    swallows.

    A failure PARTWAY THROUGH `prepare` / `attach_attempts` / `mark_calling`
    is a different animal once the table demonstrably exists: it leaves a
    `prepared` row that is EITHER not yet committed (harmless — the whole
    attempt, prepare included, rolls back with everything else this
    transaction touched, because nothing commits until the call below) OR,
    if a caller ever changes the commit boundary to sit before this returns,
    a row that IS durably `prepared` but never wired to its attempts and
    never marked `calling`. `PREPARED` rows are excluded from
    `protocol.open_submissions()` (`_OPEN_SQL` only selects `calling`/
    `unknown`) — so a half-written `prepared` row that somehow survives is
    invisible to every resolution pass, permanently, which is worse than no
    protocol row at all. The only response that cannot produce that orphan
    is to make the failure loud enough that it aborts BEFORE `submit_batch`
    runs, so a genuinely uncommittable local transaction (this function's
    caller now owns one real transaction per finding 2) rolls the partial
    `prepare` back along with everything else in it, and Batch is never
    called against a manifest whose bookkeeping this process already knows
    is broken.

    So: `prepare`, `attach_attempts` (now HARD-CHECKED — its return is the
    row count `execute` reports, and a count that does not equal
    `len(attempt_ids)` is exactly as much a failure as an exception, because
    it means some pre-created attempt row was not linked to this submission)
    and `mark_calling` run OUTSIDE the try/except below and PROPAGATE
    whatever they raise — `protocol.SubmissionProtocolError` for the
    attach-count mismatch (matching the vocabulary `submission.protocol`
    already defines for "the protocol itself is broken"), or whatever
    `prepare`/`mark_calling` themselves raise on a real database error. No
    caller in this codebase currently catches `SubmissionProtocolError`
    (checked at fix-txn-core time), so today this reaches `submit_units`'s
    caller — `pipeline.operator.operator.run_pass`, uncaught — and from
    there the operator service's own poll-failure-threshold restart
    machinery, which is exactly the "hard abort, not a retry-in-place"
    response finding 3 asks for: a poll that cannot durably open its
    submission record does not get to call Batch this cycle, and the next
    poll (or the next process, after a restart) tries again from scratch
    with fresh manifest/rows/protocol row, never from this half-written one.
    """
    from submission import protocol

    if not protocol.is_available(execute):
        # THE ONE CASE STILL CAUGHT AND SWALLOWED: the protocol is genuinely
        # ABSENT (DRAFT 044 not applied), which is a real, expected, steady
        # state until the change request lands — not a partial write. See the
        # docstring's "narrowed here" paragraph for why this is the only
        # branch that keeps the old degrade-and-proceed behaviour.
        logger.debug(
            "the submissions table is absent (DRAFT 044 not applied); "
            "submitting without a durable submission record")
        return None

    submission_id = protocol.prepare(
        execute,
        run_id=str(batch.manifest.batch_id),
        job_type=batch.manifest.job_type,
        job_name=job_name or f"rapid-{batch.manifest.batch_id}",
        job_queue=queue,
        job_definition=job_definition,
        manifest_checksum=binding.manifest_checksum,
        manifest_uri=manifest_uri,
        array_size=batch.manifest.array_size,
        now=moment)

    attached = protocol.attach_attempts(execute, submission_id, attempt_ids)
    if attached != len(attempt_ids):
        # THE HARD CHECK finding 3 adds: attach_attempts's own docstring
        # already guards against overwriting a differently-owned row
        # (`WHERE ... submission_id IS NULL`), but a caller that never
        # checked its return could not tell "every attempt linked" from
        # "some attempt was already claimed by something else" — a
        # PREPARED row that is wired to fewer attempts than it claims is
        # exactly the half-written state the docstring above says must not
        # be allowed to reach `submit_batch`.
        raise protocol.SubmissionProtocolError(
            f"submission for run {batch.manifest.batch_id} attached "
            f"{attached} of {len(attempt_ids)} pre-created attempt row(s); "
            f"the submission record would be PREPARED but not fully wired "
            f"to its attempts, which no resolution pass can find "
            f"(PREPARED rows are outside protocol.open_submissions()) — "
            f"aborting before SubmitJob is called rather than proceeding "
            f"with a bookkeeping row this protocol can never resolve")

    protocol.mark_calling(execute, submission_id, now=moment)

    if commit is not None:
        # DURABLE BEFORE THE CALL. Without this the `calling` marker is
        # invisible to the only reader that matters — the pass that finds the
        # wreckage after this process stops existing.
        commit()
    return submission_id


def _mark_submission_bound(execute, submission_id, scheduler_job_id):
    """CALLING -> BOUND, best-effort: the call returned an id."""
    if submission_id is None:
        return
    from submission import protocol

    try:
        protocol.mark_bound(execute, submission_id, scheduler_job_id)
    except Exception as exc:  # noqa: BLE001 - the work succeeded regardless
        logger.warning(
            "submission %s was not marked bound (%s); a resolution pass will "
            "find the job by identity re-query and record it FOUND, which is "
            "the same conclusion by the same evidence",
            submission_id, exc)


def _mark_submission_unknown(execute, submission_id, exc, commit=None):
    """CALLING -> UNKNOWN: the call's outcome could not be judged.

    Committed if the caller supplied a commit, for the same durability reason
    the `calling` write is: this runs on the failure path, where the process
    may be about to lose whatever it has not written down.
    """
    if submission_id is None:
        return
    from submission import protocol

    try:
        protocol.mark_unknown(
            execute, submission_id,
            detail=f"{type(exc).__name__}: {exc}")
        if commit is not None:
            commit()
    except Exception as inner:  # noqa: BLE001 - never mask the real failure
        logger.warning(
            "submission %s could not be marked unknown (%s); its row stays "
            "`calling`, which a resolution pass treats as equally ambiguous "
            "and resolves the same way", submission_id, inner)


def _bind_scheduler_jobs(writer, submission, attempt_ids):
    """Backfill the child job ids Batch assigned, after SubmitJob returned.

    Batch names array children `<parent>:<index>`, so the ids are derivable
    from the parent and the index. A single-unit batch is a plain job (Batch
    rejects arraySize 1) and its one row takes the parent id itself.

    The backfill is guarded in SQL (`WHERE scheduler_job_id IS NULL`), so a
    replayed submission cannot overwrite an id already recorded, and the
    writer raises if it cannot verify the row count — an unverifiable backfill
    is not a backfill.

    **A SHORT BACKFILL NOW RAISES (finding 4, fix-txn-core) — it used to only
    `logger.warning`.** That was silent data loss with a paper trail nobody
    reads: a submission durably `bound`, with one or more array children
    permanently unaddressable by scheduler id, discoverable only by grepping
    logs for a warning that carries no alerting weight. `submit_units` now
    calls this BEFORE its post-Batch commit (see that function's docstring on
    `protocol_commit`'s second call), so a raise here reaches the caller with
    nothing yet durable — `bound` and every already-backfilled id in this
    same batch stay uncommitted and are discarded together, which is what
    makes retrying the bookkeeping (never the call) a sound response. See
    `SubmissionBookkeepingFailed` for the exact contract a caller retries
    against.
    """
    assignments = []
    for index, attempt_id in enumerate(attempt_ids):
        child = submission.child_job_id(index) if submission.array_size > 1 \
            else submission.job_id
        assignments.append((attempt_id, child))

    updated = writer.backfill_scheduler_job_ids(assignments)
    if updated != len(assignments):
        raise SubmissionBookkeepingFailed(
            f"backfilled {updated} of {len(assignments)} scheduler job ids "
            f"for run {submission.batch_id}, job {submission.job_id}: Batch "
            f"has already accepted this job and it must not be resubmitted, "
            f"but the child scheduler-id bookkeeping did not complete and "
            f"has not been committed. Retry the backfill for this "
            f"submission; do not call submit_job again for it.",
            run_id=submission.batch_id, scheduler_job_id=submission.job_id,
            attempt_ids=attempt_ids)
    return updated


def wait_for_completion(conn, run_id, timeout=DEFAULT_COMPLETION_TIMEOUT,
                        poll_seconds=DEFAULT_POLL_SECONDS, sleep=time.sleep,
                        monotonic=time.monotonic, on_poll=None):
    """Wait until every attempt of `run_id` is reconciler-terminal.

    Reads the attempt table, not Batch. Three reasons the table is the right
    source and `describe_jobs` is not:

    - The reconciler already maintains it, and it is the *reconciled* view: an
      attempt is terminal here only once its scheduler truth is known.
    - It is one query for the whole batch, where the old wait made one
      `describe_jobs` call per job per poll.
    - It survives the scheduler forgetting. Batch drops jobs from its history
      eventually; the attempt row is permanent.

    Raises `CompletionTimeout` when the timeout expires. That is a designed
    outcome, not a failure: the caller records it and moves on, and the
    reconciler classifies the stragglers on its own horizons.
    """
    deadline = monotonic() + timeout

    while True:
        counts = _progress(conn, run_id)
        outstanding = sum(n for state, n in counts.items()
                          if state not in _TERMINAL)
        if on_poll is not None:
            on_poll(counts, outstanding)

        if not counts:
            logger.warning("no attempt rows for run %s; nothing to wait for",
                           run_id)
            return counts
        if outstanding == 0:
            logger.info("run %s complete: %s", run_id, counts)
            return counts

        if monotonic() >= deadline:
            raise CompletionTimeout(
                f"run {run_id} still has {outstanding} attempt(s) outstanding "
                f"after {timeout}s: {counts}. This is a reconciliation case — "
                f"the reconciler classifies them on its own horizons.",
                run_id=run_id, outstanding=outstanding)

        sleep(poll_seconds)


def _progress(conn, run_id):
    with conn.cursor() as cur:
        cur.execute(_PROGRESS_SQL, (run_id,))
        counts = {state: count for state, count in cur.fetchall()}
    conn.rollback()
    return counts


def run_registration(conn, register=None):
    """Invoke registration as the records consumer.

    A function call, not a subprocess: the four scripts this replaces had no
    importable entry point at all — each was a `__main__` block — which is
    precisely why the VPO had to exec them and why their exit codes were the
    only channel back. The run's counts are returned directly.
    """
    from pipeline.registration import candidates, register_batch

    rows = candidates(conn)
    logger.info("registration: %d reconciled attempt(s) to consider", len(rows))
    # A caller with no registrar gets a DECISION pass, asked for explicitly
    # (review finding #5): omitting the callback used to become a dry run
    # whose decisions were reported as registrations.
    return register_batch(conn, rows, register=register,
                          dry_run=register is None)
