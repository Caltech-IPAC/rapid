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


def submit_units(units, job_type, queue, job_definition, binding,
                 manifest_bucket, manifest_prefix, s3_client, batch_client,
                 execute, run_id=None, reason="vpo", job_name=None,
                 now=None, reference_observation_window=None):
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
    """
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    manifest = Manifest(units=list(units), batch_id=run_id, job_type=job_type,
                        reference_observation_window=(
                            reference_observation_window))
    batch = Batch(manifest=manifest, reason=reason)

    store = S3ManifestStore(manifest_bucket, prefix=manifest_prefix,
                            client=s3_client)

    # 1. Publish the manifest. Its checksum is part of the binding the rows
    #    carry, so it has to exist before they do.
    manifest_uri = publish_manifest(batch.manifest, store)

    bound = ExecutionBinding(
        job_definition_arn=binding.job_definition_arn,
        job_definition_rev=binding.job_definition_rev,
        image_digest=binding.image_digest,
        release_identity=binding.release_identity,
        manifest_checksum=batch.manifest.checksum(),
    )

    # 2. The rows, BEFORE SubmitJob. No scheduler job ids yet — nothing has
    #    assigned any.
    writer = AttemptWriter(execute)
    attempt_ids = _precreate(writer, batch.manifest, run_id, bound, moment,
                             execute=execute)

    # 3. Submit. A failure here leaves the rows as reconciliation cases, not
    #    orphans: they are correct, they simply never got a scheduler job.
    try:
        submission = submit_batch(
            batch=batch, job_queue=queue, job_definition=job_definition,
            store=store, client=batch_client, job_name=job_name,
            manifest_uri=manifest_uri)
    except Exception as exc:
        logger.error(
            "SubmitJob failed for run %s after %d attempt row(s) were "
            "pre-created; the rows remain as reconciliation cases and are "
            "classified at the submission-anchored horizon: %s",
            batch.manifest.batch_id, len(attempt_ids), exc)
        raise SubmissionFailed(
            f"SubmitJob failed for run {batch.manifest.batch_id} after "
            f"{len(attempt_ids)} attempt row(s) were pre-created: {exc}",
            run_id=batch.manifest.batch_id,
            attempt_ids=attempt_ids) from exc

    # 4. Backfill the child job ids the scheduler has now assigned.
    _bind_scheduler_jobs(writer, submission, attempt_ids)

    logger.info("submitted %s batch %s as job %s (%d children, %d rows)",
                job_type, submission.batch_id, submission.job_id,
                submission.array_size, len(attempt_ids))
    return submission, attempt_ids


def submit_gathered(units, job_type, queue, job_definition, binding,
                    manifest_bucket, manifest_prefix, s3_client, batch_client,
                    execute, run_id, max_batch_size=None, reason="vpo",
                    now=None, reference_observation_window=None):
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
            reference_observation_window=reference_observation_window))
    return results


def _precreate(writer, manifest, run_id, binding, moment, execute=None):
    """One logical job and one attempt row per array child, before SubmitJob.

    The logical_job_id MUST be the id the runtime will resolve with — the
    manifest unit's RUN-SCOPED key — because `resolve_attempt` claims the
    pre-created row by matching on it. A submitter that keys rows differently
    creates rows the runtime can never claim: every child then makes a second
    row, and every pre-created row is orphaned in `submitted` until a horizon
    classifies it. The two sides agreeing is the whole mechanism.

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
    """
    from observability.attempts import AttemptIdentity
    from submission.subjects import UnknownJobType, attempt_identity_fields

    attempt_ids = []
    for index, unit in enumerate(manifest.units):
        logical_job_id = unit.logical_job_key(manifest.batch_id,
                                              manifest.job_type)

        writer.create_logical_job(
            logical_job_id, manifest.batch_id, binding,
            job_type=manifest.job_type)

        try:
            identity_fields = attempt_identity_fields(manifest.job_type, unit)
        except UnknownJobType:
            identity_fields = {"exposure_id": unit.exposure, "sca": unit.sca,
                               "sky_tile": getattr(unit.facts, "rtid", None)}

        attempt_id = writer.create_submitted(
            AttemptIdentity(
                run_id=manifest.batch_id,
                logical_job_id=logical_job_id,
                **identity_fields),
            created_at=moment, submitted_at=moment,
            binding=binding)

        if execute is not None:
            _attach_work_unit(execute, manifest.job_type, unit,
                              attempt_id, moment)

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


def _attach_work_unit(execute, job_type, unit, attempt_id, moment):
    """Find-or-create this unit's work unit, transition it, and attach it.

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
        the attach-without-transition branch.

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
            # returning one work_unit_id.
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
                "winning unit %s for attempt %s",
                job_type, identity.input_scope, existing["work_unit_id"],
                attempt_id)
            work_unit_id = _transition_or_attach(
                execute, work_writer, existing, job_type,
                identity.input_scope, attempt_id, moment)
            _set_attempt_work_unit(execute, attempt_id, work_unit_id)
            return
        work_writer.transition_unit(
            work_unit_id, READY, SUBMITTED, writer=WRITER_ORCHESTRATOR,
            now=moment)
    else:
        work_unit_id = _transition_or_attach(
            execute, work_writer, existing, job_type, identity.input_scope,
            attempt_id, moment)

    _set_attempt_work_unit(execute, attempt_id, work_unit_id)


def _transition_or_attach(execute, work_writer, existing, job_type,
                          input_scope, attempt_id, moment):
    """Transition a pre-existing unit ready->submitted, or just attach to it.

    Factored out of `_attach_work_unit` because the race-loser path needs
    exactly this dispatch (rule 6: "attaching to a unit already `submitted`
    must not authorize a second submission path") and duplicating it was how
    the two branches would drift. Returns the work_unit_id either way.

    **SUBMISSION OWNERSHIP STAYS EXCLUSIVE.** The `state != READY` branch
    attaches the attempt's FK and issues no transition, so nothing here can
    produce a second ready->submitted event for a unit already submitted. The
    READY branch's `transition_unit` is CAS-guarded on `state = 'ready'`, so
    two callers that both read `ready` still yield exactly one transition —
    the loser raises `WorkUnitNotFound` from the CAS rather than firing a
    duplicate.
    """
    work_unit_id = existing["work_unit_id"]
    if existing["state"] != READY:
        # Already submitted (a retry re-running _precreate for a unit whose
        # work unit is mid-flight), parked `blocked` by retry policy, or
        # otherwise not workable right now — attach without transitioning
        # rather than issuing an illegal edge. The attempt still gets its FK;
        # the work unit's own state is left to whichever writer owns it.
        logger.debug(
            "work unit %s for %s/%s is %s, not ready; attaching attempt %s "
            "without a transition",
            work_unit_id, job_type, input_scope, existing["state"], attempt_id)
        return work_unit_id
    # THE CAMPAIGN-STAGED CASE (and any other pre-created-ready unit): a unit
    # already exists in 'ready', created by another writer (the mock
    # transformer, part 5, or a genuine upstream validation/ingest stage this
    # v1 does not yet have) — this call only transitions it, under
    # writer='orchestrator', never re-creating.
    work_writer.transition_unit(
        work_unit_id, READY, SUBMITTED, writer=WRITER_ORCHESTRATOR, now=moment)
    return work_unit_id


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


def _bind_scheduler_jobs(writer, submission, attempt_ids):
    """Backfill the child job ids Batch assigned, after SubmitJob returned.

    Batch names array children `<parent>:<index>`, so the ids are derivable
    from the parent and the index. A single-unit batch is a plain job (Batch
    rejects arraySize 1) and its one row takes the parent id itself.

    The backfill is guarded in SQL (`WHERE scheduler_job_id IS NULL`), so a
    replayed submission cannot overwrite an id already recorded, and the
    writer raises if it cannot verify the row count — an unverifiable backfill
    is not a backfill.
    """
    assignments = []
    for index, attempt_id in enumerate(attempt_ids):
        child = submission.child_job_id(index) if submission.array_size > 1 \
            else submission.job_id
        assignments.append((attempt_id, child))

    updated = writer.backfill_scheduler_job_ids(assignments)
    if updated != len(assignments):
        # Not fatal: the rows exist and the reconciler can still find them by
        # logical job. But it means some child is unaddressable by scheduler
        # id until it resolves itself, which is worth saying loudly.
        logger.warning(
            "backfilled %d of %d scheduler job ids for run %s; the remainder "
            "are reconciliation cases until their runtimes resolve them",
            updated, len(assignments), submission.batch_id)
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
