"""``rapidctl run`` — the run registry's external-effect subcommands.

``run create`` and ``run archive`` are plain calls through ``actions.py``
into the migration-109 ``derived.*`` functions (see ``main.py``'s
``_cmd_run_create``/``_cmd_run_archive``) and need no module of their own,
matching every other database-only mutating command in this package.

The three subcommands here are different: their real effect is OUTSIDE this
database — AWS Batch submission, the scoped registrar's own transaction, a
work-unit state transition — so, per the ruling this module implements, each
follows ``batch.py``'s "act on the real side, then record once after"
pattern rather than a ``derived.*`` function that would have to pretend to
own an effect it cannot perform (109's own header makes exactly this
argument for why ``run start``/``register``/``release-dead-letters`` are
NOT backed by a migration-109 function). Every mutating entry point here
therefore ends the same way: call ``actions.record_external_action`` exactly
once, with the REAL outcome, never before the real work runs.

RUN MATCHING IS ALWAYS BY PREFIX. Every function here that scopes to a run
uses ``run_id LIKE name || '%'`` — never ``=`` — because a split submission
batch carries ``<name>-<n>`` (``pipeline.seams.submit_gathered``). Matching
by equality was the defect ``runs`` (migration 108) was written to end.
"""

import sys

from pipeline.operatorctl.actions import record_external_action
from pipeline.operatorctl.session import submission_role


# ---------------------------------------------------------------------------
# `run start` — gather and submit a phase under a run, in-process.
# ---------------------------------------------------------------------------
class RunStartEnvironmentError(Exception):
    """The environment `run start` needs to submit is not present.

    Raised INSTEAD of silently doing nothing or half-submitting: `run start`
    reuses `submission_env`'s own binding resolution (job-definition ARN,
    queue, buckets), and that function already fails loudly (exit 64) when
    `RAPID_IMAGE_DIGEST`/`RAPID_RELEASE_IDENTITY`/`RAPID_MANIFEST_BUCKET`
    or the parameter tree's queue/definition keys are missing — but it does
    that by calling `exit()` directly (module docstring's own complaint
    about `RAPIDDB.__init__`, repeated here: a library must not terminate
    the process). This type lets `_cmd_run_start` catch the gap and print
    the same operator-legible refusal every other typed error in this
    package gets, rather than the interpreter dying inside a subcommand.
    """


#: The four post-DB-chain phases `run start` has always known how to gather
#: for, and their (job_type, gatherer) pair — the SAME table
#: `pipeline.test.live_w9_ramp.PHASES` keeps, reproduced here rather than
#: imported because that module's `PHASES` also carries the two MJD-windowed
#: gatherers (`reference`/`science`) bound to environment-derived globals
#: (`START`/`END`) at import time, which would make importing it for the
#: table alone reach into that harness's own environment-parsing. These four
#: need no processing-date-window argument beyond `--proc-date` — see
#: `_cmd_run_start`'s handling of it below. `reference`/`science` are handled
#: separately in `gather_for_run`, below, because they need the MJD window
#: and submission-environment arguments this table's callers do not carry.
def _phase_table():
    from submission import gathering, routes
    return {
        "catalog-load": (routes.JOB_TYPE_CATALOG_LOAD,
                        gathering.gather_catalog_load_units),
        "crossmatch": (routes.JOB_TYPE_CROSSMATCH,
                      gathering.gather_crossmatch_units),
        "statistics": (routes.JOB_TYPE_STATISTICS,
                      gathering.gather_statistics_units),
        "merge-dedup": (routes.JOB_TYPE_MERGE_DEDUP,
                       gathering.gather_merge_dedup_units),
    }


#: The two MJD-windowed phases, added to `run start` (throughput-sitting
#: ruling, 2026-09-11) alongside the run-scoped resubmission-gate fix that
#: makes gathering for them under a CAMPAIGN run actually yield units. Kept
#: as a separate set rather than folded into `_phase_table` above because
#: their gatherers take a materially different argument shape (an MJD
#: window, an S3 client/bucket, a run id) that the four post-DB-chain
#: gatherers do not, and `gather_for_run` branches on membership here before
#: ever calling `_phase_table()`.
_WINDOWED_PHASES = ("reference", "science")


def _capped(units, cap):
    """The first `cap` units in gathering order — `live_w9_ramp._capped`'s
    identical logic, copied rather than imported for the same reason the
    phase table above is: importing that module pulls in its module-scope
    `START`/`END` environment reads, which this command has no reason to
    depend on.
    """
    out = []
    for unit in units:
        out.append(unit)
        if len(out) >= cap:
            break
    return out


def gather_for_run(dbh, phase, proc_date=None, cap=None, window=None,
                   run_name=None, s3_client=None, job_bucket=None,
                   fids=None):
    """Gather units for `phase`, capped, via the SAME `submission.gathering`
    functions the VPO and `live_w9_ramp` call. Returns `(job_type, units)`.

    Raises `KeyError` for an unknown phase — `_cmd_run_start` turns that
    into the same operator-legible refusal every other bad-argument path in
    this package uses.

    `window`, `run_name`, `s3_client`, `job_bucket` and `fids` serve ONLY
    `phase in ("reference", "science")` — the four post-DB-chain phases
    ignore them entirely, matching `live_w9_ramp`'s own PHASES table where
    the two MJD-windowed gatherers are called with a different argument
    shape than the other four (see that module's `main()`).

    THE RUN SCOPE PASSED TO THE RESUBMISSION GATE IS `run_name` ITSELF. For
    `reference`, `run_name` is passed to `gathering.gather_reference_units`
    as BOTH `run_id` (the coadd-input publish-key prefix, required) and
    `run_scope` (the gate's run scope, new) — the two are different facts
    that happen to share a natural value here: a run gathering its own
    phase authors its own artifacts under its own name AND wants to be
    gated only on its own prior work, never on a different run's or
    production's. See `gathering.gather_reference_units`'s own docstring
    for why they remain two parameters rather than one.
    """
    if phase in _WINDOWED_PHASES:
        from submission import gathering, routes

        if window is None:
            raise ValueError(
                "phase %r requires --window-start/--window-end" % phase)
        if run_name is None:
            raise ValueError("phase %r requires a run name" % phase)
        start, end, start_mjd, end_mjd, min_coadd = window

        if phase == "reference":
            if s3_client is None or job_bucket is None:
                raise ValueError(
                    "phase 'reference' requires the submission "
                    "environment's s3_client and manifest bucket")
            units = gathering.gather_reference_units(
                dbh, start, end, start_mjdobs=start_mjd, end_mjdobs=end_mjd,
                min_images_to_coadd=min_coadd, s3_client=s3_client,
                job_bucket=job_bucket, run_id=run_name, fids=fids,
                run_scope=run_name)
            job_type = routes.JOB_TYPE_REFERENCE_IMAGE
        else:
            units = gathering.gather_science_units(
                dbh, start, end, start_mjdobs=start_mjd, end_mjdobs=end_mjd,
                min_images_to_coadd=min_coadd, fids=fids,
                run_scope=run_name)
            job_type = routes.JOB_TYPE_SCIENCE

        units = list(units)
        if cap is not None:
            units = _capped(units, cap)
        return job_type, units

    job_type, gatherer = _phase_table()[phase]
    if proc_date is not None:
        units = list(gatherer(dbh, proc_date))
    else:
        units = list(gatherer(dbh))
    if cap is not None:
        units = _capped(units, cap)
    return job_type, units


def _resolve_submission_env(job_type):
    """`submission_env(job_type)`, with its `exit(64)` refusals translated
    to `RunStartEnvironmentError` — the one place that translation happens
    (see `RunStartEnvironmentError`'s own docstring for why it cannot
    happen inside `submission_env` itself). Shared by `submit_run` and, as
    of the two MJD-windowed phases, `_cmd_run_start` — `reference`
    gathering needs this SAME context's `s3_client`/`manifest_bucket`
    before it ever gathers a single unit (its coadd-input publish step, not
    only its eventual submission), so a dry run for that phase must resolve
    it too, never only the eventual `--apply`.
    """
    from pipeline.operator.submission import submission_env

    try:
        return submission_env(job_type)
    except SystemExit as exc:
        # `submission_env` calls `exit(64)` on missing environment/tree keys
        # rather than raising — see the class docstring. A `SystemExit`
        # raised from inside a function call (as opposed to top-level
        # interpreter exit) IS catchable, so this is the one place that
        # translation happens, at the single call site that can reach it.
        raise RunStartEnvironmentError(
            "the submission environment required for run start is "
            "incomplete (see the printed *** Error above); run start "
            "cannot gather a binding without it") from exc


def submit_run(conn, name, job_type, units, reason, context=None):
    """Submit `units` under `name`, through the SAME production path
    `live_w9_ramp` uses: `submission_env` for the binding, `pipeline.seams.
    submit_gathered` for the submission itself. Nothing here reimplements
    either — this is the in-process replacement the task ruling calls for,
    run under whatever role `rapidctl` itself already holds (`operator_
    session`'s `SET ROLE`), not a re-derived STS/podman launch.

    `context`, when given, is a `submission_env(job_type)` result the
    caller already resolved — `_cmd_run_start` passes the SAME context a
    windowed phase's dry-run gather already resolved, rather than this
    function resolving a second one (`active_definition`'s own AWS Batch
    call is not free to repeat). `None` (the default, and what every
    non-windowed phase passes) resolves it here, exactly as before this
    parameter existed.

    Raises `RunStartEnvironmentError` if `submission_env` cannot resolve a
    binding — see `_resolve_submission_env`.

    **`work_unit_run_id=name` (throughput-sitting ruling, 2026-09-11).**
    This is THE call site that must opt a submission into run-scoped
    work-unit identity — `rapidctl run start --apply` is the one live path
    that submits under a DECLARED, registered run rather than an ordinary
    VPO poll, and `name` here is that run's bare `runs.name`, never a
    batch-suffixed id (`seams.submit_gathered` mints those internally per
    array-job batch and never lets one leak into `work_unit_run_id`; see
    that function's own docstring). Without this, `seams._decide_work_unit`
    looks up work units with `run_id=None` regardless of which run called
    it, finds PRODUCTION's row for any (job_type, input_scope) production
    has already processed, and — because that row is `state='complete'`
    — judges the campaign's own gathered units already claimed and submits
    nothing (observed live 2026-09-11, campaign run
    `awaicgen54-proof-20260911`: GATHER returned 109 run-scoped units,
    submission created zero work_units rows).

    **RUNS UNDER `submission_role()`, NOT THE BARE OPERATOR SESSION
    (identity-fix ruling, 2026-09-11).** `rapid_operator` (and the agent
    tier) hold only SELECT — creating a work unit needs INSERT/UPDATE on
    `work_units`, which the operate tier deliberately does not have (see
    `submission_role()`'s own docstring for why the widening happens here
    and not on `rapid_operator` itself). Only THIS INSERT runs under the
    widened role: `start_run_audited` calls this function and then
    `record_external_action` outside of it, so the audited ledger row is
    written back under the operate tier the session actually assumed —
    `session_user`, what lands in `derived.write_mutation_audit`, is
    never affected by the role switch either way, but keeping the switch
    scoped to only this call is what keeps every OTHER submission-adjacent
    statement running under the narrow tier this module otherwise
    insists on.
    """
    from pipeline import seams
    from database.modules.utils.rapid_db_connect import ConnectionExecutor

    if context is None:
        context = _resolve_submission_env(job_type)

    if not units:
        return []

    with submission_role(conn):
        # `autocommit_each=False` + `protocol_commit=conn.commit`: the
        # same fix-txn-core fix `pipeline.operator.service._execute_
        # factory` carries for the VPO path — read that docstring before
        # touching this. Without both together this call site has the
        # exact defect that fix repairs (four independently-committed
        # writes with no atomicity across them).
        executor = ConnectionExecutor(conn, autocommit_each=False)
        return seams.submit_gathered(
            units, job_type=job_type, queue=context["queue"],
            job_definition=context["job_definition"],
            binding=context["binding"],
            manifest_bucket=context["manifest_bucket"],
            manifest_prefix=context["manifest_prefix"],
            s3_client=context["s3_client"],
            batch_client=context["batch_client"],
            execute=executor.execute, run_id=name,
            reason=reason, work_unit_run_id=name,
            protocol_commit=conn.commit)


def start_run_audited(conn, idempotency_key, name, phase, reason,
                      proc_date=None, cap=None, dry_run=True,
                      policy_citation=None, out=None,
                      window_start=None, window_end=None, fids=None):
    """Gather, (maybe) submit, and audit `run start`. Returns `(result,
    scope)` — the same shape `terminate_jobs_audited` returns, for the same
    reason: the CLI renders both through the identical `render_plan` call.

    THE DRY RUN GATHERS FOR REAL AND SUBMITS NOTHING — `contract.py`'s own
    rule ("the plan shown IS the answer the apply will act on, minus the
    writing"), applied here as: `gather_for_run` always runs against the
    real database; `submit_run` is only ever called when `dry_run` is
    False. `RunStartEnvironmentError` is deliberately allowed to propagate
    on a dry run too — a caller whose environment cannot submit should see
    that in the rehearsal, not discover it for the first time on `--apply`.

    `window_start`/`window_end` are REQUIRED for `phase in ("reference",
    "science")` and ignored otherwise — the four post-DB-chain phases take
    no window argument, matching `live_w9_ramp.PHASES`'s own split. Given
    as the same "YYYY-MM-DD HH:MM:SS" strings `W9_START`/`W9_END` take;
    converted through `pipeline.operator.gathering.mjd_window` and
    `min_images_to_coadd` — the SAME two helpers `live_w9_ramp` calls, not
    reimplemented here.

    `fids` selects FILTERS, not fields — see `_cmd_run_start`'s own
    argument help text for why a gather cannot be narrowed to a handful of
    fields at all.
    """
    out = out or sys.stdout
    scope = "run:%s:phase=%s" % (name, phase)

    replay = _replay_lookup(conn, idempotency_key, "run_start", scope)
    if replay is not None:
        return replay, scope

    from database.modules.utils import rapid_db as db
    dbh = db.RAPIDDB()
    if dbh.exit_code >= 64:
        raise RunStartEnvironmentError(
            "database handle unusable: exit_code=%s" % dbh.exit_code)

    windowed = phase in _WINDOWED_PHASES
    window = None
    context = None
    if windowed:
        if window_start is None or window_end is None:
            raise ValueError(
                "phase %r requires --window-start and --window-end" % phase)
        from pipeline.operator.gathering import mjd_window, min_images_to_coadd
        start_mjd, end_mjd = mjd_window(window_start, window_end)
        window = (window_start, window_end, start_mjd, end_mjd,
                  min_images_to_coadd())

        # The submission environment is resolved HERE, before gathering —
        # not only before the eventual `--apply` submit — because
        # `reference` gathering itself needs `s3_client`/`manifest_bucket`
        # to publish each unit's coadd-input list (see `gather_for_run`'s
        # docstring). Resolved ONCE and reused by `submit_run` below on
        # `--apply`, rather than a second `active_definition` Batch call.
        from submission import routes
        job_type_for_env = (routes.JOB_TYPE_REFERENCE_IMAGE
                            if phase == "reference"
                            else routes.JOB_TYPE_SCIENCE)
        context = _resolve_submission_env(job_type_for_env)

    try:
        job_type, units = gather_for_run(
            dbh, phase, proc_date=proc_date, cap=cap, window=window,
            run_name=name,
            s3_client=context["s3_client"] if context else None,
            job_bucket=context["manifest_bucket"] if context else None,
            fids=fids)
    except KeyError:
        table = ("catalog-load", "crossmatch", "statistics", "merge-dedup",
                 "reference", "science")
        raise ValueError(
            "unknown phase %r; run start knows %s" % (phase, ", ".join(table))
        ) from None

    detail = {"phase": phase, "job_type": job_type, "gathered": len(units),
              "cap": cap, "proc_date": proc_date,
              "window_start": window_start, "window_end": window_end}

    if dry_run:
        print("[dry-run] would gather %d unit(s) for phase=%s (job_type=%s)"
             % (len(units), phase, job_type), file=out)
        result = record_external_action(
            conn, idempotency_key, "run_start", scope, reason,
            dry_run=True, rows_affected=0, detail=detail,
            policy_citation=policy_citation)
        return result, scope

    results = submit_run(conn, name, job_type, units, reason, context=context)
    total_children = sum(len(attempt_ids) for _sub, attempt_ids in results)
    detail["batches"] = len(results)
    detail["children"] = total_children
    for submission, attempt_ids in results:
        print("submitted %s (%d children)" % (submission.job_id,
                                               len(attempt_ids)), file=out)

    result = record_external_action(
        conn, idempotency_key, "run_start", scope, reason, dry_run=False,
        rows_affected=total_children, detail=detail,
        policy_citation=policy_citation)
    return result, scope


# ---------------------------------------------------------------------------
# `run register` — the scoped registrar, wrapped under the mutation contract.
# ---------------------------------------------------------------------------
def register_run_audited(conn, idempotency_key, name, reason, dry_run=True,
                         records_bucket=None, policy_citation=None,
                         s3_client=None, out=None):
    """Call `pipeline.registration.scoped.run_scoped_registration` scoped to
    `name`'s prefix, then record the outcome once, after — the survey's
    §5 finding is that this entry point is not wired through `mutation_
    audit`/idempotency at all today, so this function is that wiring, not a
    reimplementation: `run_scoped_registration` itself is untouched.
    """
    out = out or sys.stdout
    scope = "run:%s:register" % name

    replay = _replay_lookup(conn, idempotency_key, "run_register", scope)
    if replay is not None:
        return replay, scope

    from pipeline.registration.scoped import run_scoped_registration

    run, rows = run_scoped_registration(
        conn, run_id_prefix=name, dry_run=dry_run,
        records_bucket=records_bucket, s3_client=s3_client)

    counts = run.as_dict()
    detail = {"run_id_prefix": name, "scope_size": len(rows), "counts": counts}
    print("register: scope_size=%d %s" % (len(rows), counts), file=out)

    rows_affected = 0 if dry_run else counts.get("registered", 0)
    result = record_external_action(
        conn, idempotency_key, "run_register", scope, reason,
        dry_run=dry_run, rows_affected=rows_affected, detail=detail,
        policy_citation=policy_citation)
    return result, scope


# ---------------------------------------------------------------------------
# `run release-dead-letters` — d8_release_blocked.py, brought into rapidctl.
# ---------------------------------------------------------------------------
# The candidate query and the transition it drives are `d8_release_blocked.
# py`'s own (`rapid_plan/migration/socsim-20260821-replica/artifacts/`,
# read-only per the repo-isolation rule): attempts dead-lettered under a
# run's prefix, `started_at IS NULL`, whose work unit is BLOCKED specifically
# by the application-failure edge — never any other blocked_reason, so a
# differently-blocked unit sharing the prefix is not swept in by accident.
_RELEASE_CANDIDATES_SQL = (
    "SELECT a.attempt_id, a.work_unit_id, a.logical_job_id"
    " FROM attempts a"
    " JOIN work_units w ON w.work_unit_id = a.work_unit_id"
    " WHERE a.run_id LIKE %s"
    "   AND a.lifecycle_state = 'missing_or_contradictory'"
    "   AND a.started_at IS NULL"
    "   AND w.state = 'blocked'"
    "   AND w.blocked_reason = 'application_failure:internal_error'"
    " ORDER BY a.attempt_id"
)


def find_dead_letter_candidates(conn, name):
    """The (attempt_id, work_unit_id, logical_job_id) rows a release would
    act on for run `name` — read-only, matched by prefix like every other
    run reader.
    """
    with conn.cursor() as cur:
        cur.execute(_RELEASE_CANDIDATES_SQL, ["%s%%" % name])
        rows = cur.fetchall()
    return [{"attempt_id": r[0], "work_unit_id": r[1], "logical_job_id": r[2]}
            for r in rows]


def _release_one(conn, candidate, reason):
    """BLOCKED -> READY for one candidate, through `WorkUnitWriter` — never
    a handwritten UPDATE, so the CAS guard, the transition-graph check and
    the `unit_events` row all happen inside the one call that owns them.

    **UNDER `submission_role`, for the same reason `submit_run` is.** The
    transition goes through `derived.transition_work_unit`, and the
    operate tier does not hold EXECUTE on it:

        has_function_privilege('rapid_operator',    …) = false
        has_function_privilege('rapid_admin',       …) = true
        has_function_privilege('rapid_orchestrator',…) = true

    So every release failed with `permission denied for function
    transition_work_unit` — found live on 2026-09-11 trying to release the
    units a pooler outage had dead-lettered, and the same defect shape as
    the one that stopped `run start --apply`: a command that records an
    operator's decision while performing the pipeline's own work, running
    all of it under the read-mostly identity that exists for the recording
    half.

    The audit row is still written by `release_dead_letters_audited` after
    this returns, outside the switch, so the ledger keeps naming the human.
    """
    from pipeline.intent.writer import (BLOCKED, READY, WRITER_MUTATION_API,
                                        WorkUnitWriter)
    from database.modules.utils.rapid_db_connect import ConnectionExecutor
    # `submission_role` is imported at module scope (see the top of this
    # file), deliberately: a function-local import here would bypass the
    # module attribute the tests patch to spy on the switch.
    writer = WorkUnitWriter(ConnectionExecutor(conn).execute)
    with submission_role(conn):
        writer.transition_unit(candidate["work_unit_id"], BLOCKED, READY,
                               writer=WRITER_MUTATION_API, reason=reason)


def release_dead_letters_audited(conn, idempotency_key, name, reason,
                                 expected_state=None, dry_run=True,
                                 policy_citation=None, out=None):
    """Resolve candidates, (maybe) release each one, and audit once, after.

    `expected_state` is `{"candidates": n}`, the same "world hasn't moved"
    shape `retry_parked_attempts`/`repair_refused_outbox_rows` already use
    — checked BEFORE any transition is attempted, exactly like those two.
    A CAS miss or an illegal-edge error on one candidate does not abort the
    rest (`d8_release_blocked.release_candidates`'s own per-candidate
    independence, reproduced here): each is attempted, its own outcome
    recorded in `detail`, and the loop continues.
    """
    from pipeline.operatorctl.contract import ExpectedStateMismatch

    out = out or sys.stdout
    scope = "run:%s:release-dead-letters" % name

    replay = _replay_lookup(conn, idempotency_key, "run_release_dead_letters",
                            scope)
    if replay is not None:
        return replay, scope

    candidates = find_dead_letter_candidates(conn, name)

    if expected_state is not None and "candidates" in expected_state:
        if expected_state["candidates"] != len(candidates):
            raise ExpectedStateMismatch(
                "expected-state mismatch: caller expected %s candidate(s), "
                "found %s" % (expected_state["candidates"], len(candidates)))

    detail = {"run_id_prefix": name, "candidate_count": len(candidates)}

    if dry_run:
        for c in candidates:
            print("[dry-run] would release work_unit_id=%s (attempt_id=%s)"
                 % (c["work_unit_id"], c["attempt_id"]), file=out)
        result = record_external_action(
            conn, idempotency_key, "run_release_dead_letters", scope, reason,
            expected_state=expected_state, dry_run=True, rows_affected=0,
            detail=detail, policy_citation=policy_citation)
        return result, scope

    released = []
    failures = []
    for candidate in candidates:
        try:
            _release_one(conn, candidate, reason)
        except Exception as exc:                      # noqa: BLE001
            conn.rollback()
            failures.append({"work_unit_id": candidate["work_unit_id"],
                             "attempt_id": candidate["attempt_id"],
                             "error": str(exc)})
            print("FAILED to release work_unit_id=%s: %s"
                 % (candidate["work_unit_id"], exc), file=out)
            continue
        conn.commit()
        released.append(candidate["work_unit_id"])
        print("released work_unit_id=%s (attempt_id=%s)"
             % (candidate["work_unit_id"], candidate["attempt_id"]), file=out)

    if failures:
        detail["failures"] = failures
    detail["released_work_unit_ids"] = released[:200]

    result = record_external_action(
        conn, idempotency_key, "run_release_dead_letters", scope, reason,
        expected_state=expected_state, dry_run=False,
        rows_affected=len(released), detail=detail,
        policy_citation=policy_citation)
    return result, scope


# ---------------------------------------------------------------------------
# `run reconcile-stranded` — Batch-discovery release for units a campaign's
# array children died on before ever reaching the dead-letter shape
# `release-dead-letters` looks for (`w.blocked_reason =
# 'application_failure:internal_error'`, `a.started_at IS NULL`): a child
# that died at container start never wrote that row at all.
#
# THE CORRECTNESS RULE (live-data finding, 2026-09-11). Reconciling the
# acceptance run's 7,380 array children against the database found Batch
# child fate is NOT authoritative for pipeline success in EITHER direction:
#
#   * 1,604 units: Batch child SUCCEEDED, no successful attempt row — the
#     child was RETRIED by Batch; an earlier try's failure is what the
#     attempt row recorded, while a LATER try (a different Batch child,
#     invisible to a query that only checks the one child a work unit's
#     `scheduler_job_id` names) succeeded.
#   * 64 units: Batch child FAILED, but a successful attempt row EXISTS —
#     the application published its products and only the container's own
#     teardown exited nonzero (verified live on one child: 3 Batch tries,
#     all exit 70, `rapid_outcome=success, product_disposition=published`
#     on the attempt row throughout).
#
# So Batch fate is the DISCOVERY mechanism (it finds candidates a query
# scoped to `blocked_reason` cannot see), and the database's own attempt
# rows are the AUTHORITY (they are what decides whether releasing is safe).
# A unit qualifies only when BOTH hold: every Batch child of its attempts
# reports FAILED, AND it has no attempt with `rapid_outcome = 'success'`.
# Dropping the second half would release the 64-unit cell above — completed
# work — back to READY, resubmitting something that already happened.
#
# The live candidate set under this rule was 2,664 units, in TWO source
# states — 2,412 `submitted`, 252 `blocked` — not only `blocked` as
# `release-dead-letters` assumes; `_TRANSITION_GRAPH` admits both
# `(BLOCKED, READY)` and `(SUBMITTED, READY)` with no writer restriction
# (`pipeline.intent.writer`), so both are ordinary forward edges this
# module can drive through `WorkUnitWriter` exactly as `_release_one` does.
# ---------------------------------------------------------------------------
#: Batch's documented ceiling per `list_jobs` page is handled by the
#: paginator; this is the reconciler's OWN wave ceiling — how many
#: work units `wave_split` puts in one release wave — matching the
#: `--array-size` ceiling AWS Batch itself imposes on one array submission,
#: so a wave never asks the fleet to run more children than one array could
#: ever hold in the first place.
DEFAULT_WAVE_SIZE = 4000

#: Batch states that mean a child has not yet reached a terminal outcome —
#: the drain check waits for this set to be empty before the next wave
#: releases, so two waves' worth of work never contends for the queue at
#: once.
_IN_FLIGHT_STATES = ("RUNNABLE", "RUNNING")


def batch_child_fate(batch_client, array_job_ids):
    """`{child_job_id: "SUCCEEDED"|"FAILED"}` for every terminal child of
    every array in `array_job_ids`.

    Uses `list_jobs(arrayJobId=..., jobStatus=...)` — the discovery call
    shape confirmed live, 2026-09-11: `describe_jobs` on an array job
    returns only that array's own rolled-up `statusSummary`, never a
    per-child row, so listing each terminal status separately per array is
    the only way to recover which child landed where. Paginated via the
    SAME `get_paginator("list_jobs")` idiom `aws/terminate_batch_jobs.
    list_jobs` and `pipeline.operatorctl.batch._list_all` already use for
    this client — not reimplemented here, just applied per (array, status)
    pair instead of per (queue, status).

    `batch_client` is INJECTED, matching `placement_lines`'s own
    `session_factory` and `_resolve_submission_env`'s `context["batch_
    client"]`: this function issues no AWS call of its own construction,
    so the stub tier drives it with no boto3 client at all.

    A child reported neither SUCCEEDED nor FAILED (still in flight) is
    simply absent from the returned mapping — callers that need "has this
    array finished" ask that separately, via `waves_in_flight`, rather than
    this function inventing a third value for "not terminal yet".
    """
    fate = {}
    for array_job_id in array_job_ids:
        for status in ("SUCCEEDED", "FAILED"):
            paginator = batch_client.get_paginator("list_jobs")
            for page in paginator.paginate(arrayJobId=array_job_id,
                                           jobStatus=status):
                for job in page["jobSummaryList"]:
                    fate[job["jobId"]] = status
    return fate


_ARRAY_JOB_IDS_SQL = (
    "SELECT DISTINCT split_part(scheduler_job_id, ':', 1)"
    " FROM attempts"
    " WHERE run_id LIKE %s AND scheduler_job_id IS NOT NULL"
)


def array_job_ids_for_run(conn, name):
    """The distinct array job ids a run's attempts were submitted under,
    read from `attempts.scheduler_job_id` (`<arrayJobId>:<childIndex>`,
    one-to-one onto a Batch array child) — never a hand-kept file of ids,
    so this works for any run the database has attempts for, not only ones
    someone remembered to list somewhere.
    """
    with conn.cursor() as cur:
        cur.execute(_ARRAY_JOB_IDS_SQL, ["%s%%" % name])
        return [r[0] for r in cur.fetchall() if r[0]]


# `w.state IN ('submitted', 'blocked')`, not only 'blocked': the live
# candidate set was 2,412 `submitted` units against 252 `blocked` — the
# brief this module implements assumed all-`blocked` and live data said
# otherwise, so both source states are read here, each candidate carrying
# its OWN `state` so the release step below transitions it from wherever it
# actually is, never a hard-coded `from_state`.
_STRANDED_UNITS_SQL = (
    "SELECT w.work_unit_id, w.state,"
    "       array_agg(DISTINCT a.scheduler_job_id) FILTER"
    "         (WHERE a.scheduler_job_id IS NOT NULL),"
    "       bool_or(a.rapid_outcome = 'success')"
    " FROM work_units w"
    " JOIN attempts a ON a.work_unit_id = w.work_unit_id"
    " WHERE a.run_id LIKE %s"
    "   AND w.state IN ('submitted', 'blocked')"
    " GROUP BY w.work_unit_id, w.state"
    " ORDER BY w.work_unit_id"
)


def find_stranded_candidates(conn, name, batch_client):
    """Candidates and exclusions for run `name`'s stranded-unit reconciler.

    Returns `(candidates, excluded)`:

    * `candidates` — one dict per releasable unit: `work_unit_id`, `state`
      (its CURRENT state — `submitted` or `blocked` — so the release step
      transitions from the right `from_state`), and `reason`.
    * `excluded` — one dict per unit that had a Batch-FAILED sibling but
      did NOT qualify, each carrying `work_unit_id` and a machine-readable
      `reason`: `"successful_sibling_attempt"` (a `rapid_outcome='success'`
      attempt exists — THE review question's exact case, this is the
      check that stops a unit whose work actually completed from being
      released) or `"batch_child_not_failed"` plus `scheduler_job_id`
      (some Batch child of this unit's attempts is not FAILED — SUCCEEDED,
      or still in flight; the retry case, 1,604 live instances of it).

    Every unit read here has at least one non-NULL `scheduler_job_id`
    (the SQL's JOIN + `FILTER` guarantee that); a unit with none is
    invisible to Batch discovery entirely and is not returned in either
    list — `release-dead-letters`' own candidate query is the path for a
    unit that never reached Batch at all.
    """
    array_job_ids = array_job_ids_for_run(conn, name)
    fate = batch_child_fate(batch_client, array_job_ids)

    with conn.cursor() as cur:
        cur.execute(_STRANDED_UNITS_SQL, ["%s%%" % name])
        rows = cur.fetchall()

    candidates = []
    excluded = []
    for work_unit_id, state, scheduler_job_ids, has_success in rows:
        scheduler_job_ids = scheduler_job_ids or []
        not_failed = [sid for sid in scheduler_job_ids
                     if fate.get(sid) != "FAILED"]
        if not_failed:
            excluded.append({
                "work_unit_id": work_unit_id,
                "reason": "batch_child_not_failed",
                "scheduler_job_id": not_failed[0],
            })
            continue
        if has_success:
            excluded.append({
                "work_unit_id": work_unit_id,
                "reason": "successful_sibling_attempt",
            })
            continue
        candidates.append({
            "work_unit_id": work_unit_id,
            "state": state,
            "reason": "all_batch_children_failed_no_successful_attempt",
        })
    return candidates, excluded


def _release_stranded_one(conn, candidate, reason):
    """`candidate["state"] -> READY` for one stranded candidate, through
    `WorkUnitWriter` — the same shape `_release_one` uses for the
    dead-letter path, generalized to take the FROM-state rather than
    hard-coding `BLOCKED`, since a stranded candidate may currently be
    `submitted` OR `blocked` (see `find_stranded_candidates`).

    Runs under `submission_role`, for the identical reason `_release_one`
    does: the transition goes through `derived.transition_work_unit`, and
    the operate tier does not hold EXECUTE on it. `submission_role` is
    imported at module scope so a function-local import cannot bypass the
    module attribute the tests patch to spy on the switch.
    """
    from pipeline.intent.writer import (BLOCKED, READY, SUBMITTED,
                                        WRITER_MUTATION_API, WorkUnitWriter)
    from database.modules.utils.rapid_db_connect import ConnectionExecutor
    writer = WorkUnitWriter(ConnectionExecutor(conn).execute)
    from_state = BLOCKED if candidate["state"] == "blocked" else SUBMITTED
    with submission_role(conn):
        writer.transition_unit(candidate["work_unit_id"], from_state, READY,
                               writer=WRITER_MUTATION_API, reason=reason)


def wave_split(candidates, max_wave=DEFAULT_WAVE_SIZE):
    """`candidates` split into waves of at most `max_wave` each, in order —
    plain slicing, kept as its own function so a caller (and a test) can
    assert wave BOUNDARIES without also driving a release or a drain check.
    """
    return [candidates[i:i + max_wave]
            for i in range(0, len(candidates), max_wave)]


def wave_in_flight(batch_client, array_job_ids):
    """True if any child of `array_job_ids` is RUNNABLE or RUNNING.

    The drain check between waves — its own injectable function, per the
    brief, rather than folded into a release loop: a caller waiting for a
    wave to drain calls this on a poll interval, and a test can assert it
    is CONSULTED between waves without also faking a whole sleep loop.
    Terminal-only states (SUCCEEDED/FAILED) and pre-runnable queueing
    states (SUBMITTED/PENDING/STARTING) are deliberately not polled here —
    STARTING is close enough to RUNNING that treating it as still in
    flight would rarely change an operator's decision, but the two states
    that unambiguously mean "occupying the fleet's compute right now" are
    the ones this gate exists to wait out.
    """
    for array_job_id in array_job_ids:
        for status in _IN_FLIGHT_STATES:
            paginator = batch_client.get_paginator("list_jobs")
            for page in paginator.paginate(arrayJobId=array_job_id,
                                           jobStatus=status):
                if page["jobSummaryList"]:
                    return True
    return False


def reconcile_stranded_audited(conn, idempotency_key, name, reason,
                               batch_client, expected_state=None,
                               dry_run=True, policy_citation=None, out=None,
                               max_wave=DEFAULT_WAVE_SIZE):
    """Resolve stranded-unit candidates, (maybe) release them in waves, and
    audit once, after — mirroring `release_dead_letters_audited`'s shape
    (replay lookup, `expected_state` check, dry-run branch that prints
    what it WOULD do, `record_external_action` after) under a DISTINCT
    action name (`run_reconcile_stranded`) so its audit rows are never
    confused with `run_release_dead_letters`'s.

    `expected_state` is `{"candidates": n}`, checked BEFORE any release is
    attempted, exactly like `release_dead_letters_audited`'s own check.
    Per-candidate independence: one failure records its own outcome in
    `detail["failures"]` and the loop continues, matching that function
    and `d8_release_blocked.release_candidates` before it.
    """
    from pipeline.operatorctl.contract import ExpectedStateMismatch

    out = out or sys.stdout
    scope = "run:%s:reconcile-stranded" % name

    replay = _replay_lookup(conn, idempotency_key, "run_reconcile_stranded",
                            scope)
    if replay is not None:
        return replay, scope

    candidates, excluded = find_stranded_candidates(conn, name, batch_client)

    if expected_state is not None and "candidates" in expected_state:
        if expected_state["candidates"] != len(candidates):
            raise ExpectedStateMismatch(
                "expected-state mismatch: caller expected %s candidate(s), "
                "found %s" % (expected_state["candidates"], len(candidates)))

    detail = {"run_id_prefix": name, "candidate_count": len(candidates),
              "excluded_count": len(excluded), "excluded": excluded[:200]}

    if dry_run:
        for c in candidates:
            print("[dry-run] would release work_unit_id=%s (from %s)"
                 % (c["work_unit_id"], c["state"]), file=out)
        for x in excluded:
            print("[dry-run] excluding work_unit_id=%s: %s"
                 % (x["work_unit_id"], x["reason"]), file=out)
        result = record_external_action(
            conn, idempotency_key, "run_reconcile_stranded", scope, reason,
            expected_state=expected_state, dry_run=True, rows_affected=0,
            detail=detail, policy_citation=policy_citation)
        return result, scope

    array_job_ids = array_job_ids_for_run(conn, name)
    released = []
    failures = []
    for wave_index, wave in enumerate(wave_split(candidates, max_wave)):
        if wave_index > 0 and wave_in_flight(batch_client, array_job_ids):
            print("wave %d: prior wave still has children in flight; "
                 "stopping before releasing further waves" % wave_index,
                 file=out)
            for candidate in wave:
                failures.append({"work_unit_id": candidate["work_unit_id"],
                                 "error": "prior wave not drained"})
            continue
        for candidate in wave:
            try:
                _release_stranded_one(conn, candidate, reason)
            except Exception as exc:                  # noqa: BLE001
                conn.rollback()
                failures.append({"work_unit_id": candidate["work_unit_id"],
                                 "error": str(exc)})
                print("FAILED to release work_unit_id=%s: %s"
                     % (candidate["work_unit_id"], exc), file=out)
                continue
            conn.commit()
            released.append(candidate["work_unit_id"])
            print("released work_unit_id=%s (from %s)"
                 % (candidate["work_unit_id"], candidate["state"]), file=out)

    if failures:
        detail["failures"] = failures
    detail["released_work_unit_ids"] = released[:200]

    result = record_external_action(
        conn, idempotency_key, "run_reconcile_stranded", scope, reason,
        expected_state=expected_state, dry_run=False,
        rows_affected=len(released), detail=detail,
        policy_citation=policy_citation)
    return result, scope


# ---------------------------------------------------------------------------
# `run status --placement` — best-effort container-instance listing.
# ---------------------------------------------------------------------------
def placement_lines(queue, region=None, profile=None, session_factory=None):
    """One line per container instance behind `queue`'s compute environment:
    instance id, type, running task count. DEGRADES CLEANLY: any AWS error
    (no permission, wrong region, ECS not reachable) is caught and reported
    as a single advisory line rather than raising — `run status`'s core
    tally is a database read and must not fail because AWS is unreachable
    from wherever rapidctl happens to run, matching the late-import,
    fails-soft discipline `_cmd_terminate_batch`'s AWS path already
    establishes for this package (survey §7).
    """
    try:
        if session_factory is not None:
            session = session_factory()
        else:
            import boto3
            session = boto3.Session(region_name=region, profile_name=profile)
        batch = session.client("batch")
        queues = batch.describe_job_queues(jobQueues=[queue])["jobQueues"]
        if not queues:
            return ["(placement: job queue %r not found)" % queue]
        lines = []
        ecs = session.client("ecs")
        for ce in queues[0].get("computeEnvironmentOrder", []):
            ce_name = ce["computeEnvironment"].split("/")[-1]
            ce_detail = batch.describe_compute_environments(
                computeEnvironments=[ce["computeEnvironment"]]
            )["computeEnvironments"]
            if not ce_detail:
                continue
            cluster = ce_detail[0].get("ecsClusterArn")
            if not cluster:
                continue
            arns = ecs.list_container_instances(cluster=cluster)[
                "containerInstanceArns"]
            if not arns:
                continue
            instances = ecs.describe_container_instances(
                cluster=cluster, containerInstances=arns
            )["containerInstances"]
            for inst in instances:
                attrs = {a["name"]: a.get("value")
                        for a in inst.get("attributes", [])}
                lines.append(
                    "%s  %s  running_tasks=%s  ce=%s"
                    % (inst.get("ec2InstanceId", "?"),
                       attrs.get("ecs.instance-type", "?"),
                       inst.get("runningTasksCount", "?"), ce_name))
        return lines or ["(placement: no container instances found)"]
    except Exception as exc:                          # noqa: BLE001
        return ["(placement: unavailable — %s: %s)"
               % (type(exc).__name__, exc)]


def _replay_lookup(conn, idempotency_key, action_class, target_scope):
    """Read-only replay check via `derived.mutation_replay`, or None.

    Identical in shape to `batch._replay_lookup` — repeated rather than
    imported because that function is private to `batch.py` and this
    module's replay checks are for different `action_class` values; the six
    lines are cheaper to duplicate than to make cross-module-private.
    """
    from pipeline.operatorctl.contract import call_function
    return call_function(
        conn, "SELECT derived.mutation_replay(%s, %s, %s)",
        (idempotency_key, action_class, target_scope))
