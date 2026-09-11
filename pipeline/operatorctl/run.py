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


#: The phases `run start` knows how to gather for, and their (job_type,
#: gatherer) pair — the SAME table `pipeline.test.live_w9_ramp.PHASES` keeps,
#: reproduced here rather than imported because that module's `PHASES` also
#: carries the two MJD-windowed gatherers (`reference`/`science`) bound to
#: environment-derived globals (`START`/`END`) at import time, which would
#: make importing it for the table alone reach into that harness's own
#: environment-parsing. `run start` is deliberately scoped to the phases
#: that need no processing-date-window argument beyond what its own CLI
#: flags supply — see `_cmd_run_start`'s `--proc-date` handling below.
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


def gather_for_run(dbh, phase, proc_date=None, cap=None):
    """Gather units for `phase`, capped, via the SAME `submission.gathering`
    functions the VPO and `live_w9_ramp` call. Returns `(job_type, units)`.

    Raises `KeyError` for an unknown phase — `_cmd_run_start` turns that
    into the same operator-legible refusal every other bad-argument path in
    this package uses.
    """
    job_type, gatherer = _phase_table()[phase]
    if proc_date is not None:
        units = list(gatherer(dbh, proc_date))
    else:
        units = list(gatherer(dbh))
    if cap is not None:
        units = _capped(units, cap)
    return job_type, units


def submit_run(conn, name, job_type, units, reason):
    """Submit `units` under `name`, through the SAME production path
    `live_w9_ramp` uses: `submission_env` for the binding, `pipeline.seams.
    submit_gathered` for the submission itself. Nothing here reimplements
    either — this is the in-process replacement the task ruling calls for,
    run under whatever role `rapidctl` itself already holds (`operator_
    session`'s `SET ROLE`), not a re-derived STS/podman launch.

    Raises `RunStartEnvironmentError` if `submission_env` cannot resolve a
    binding — translating that function's own `exit(64)` calls is NOT
    possible (they terminate the process before this function gets a
    chance), so this wraps ONLY the failure mode `submission_env` raises
    rather than exits: a parameter tree fetch failure. The exit()-based
    refusals are a pre-existing gap in `submission_env` itself, out of
    scope for this command — see the ledger for why reworking that function
    was not attempted tonight.
    """
    from pipeline.operator.submission import submission_env
    from pipeline import seams
    from database.modules.utils.rapid_db_connect import ConnectionExecutor

    try:
        context = submission_env(job_type)
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

    if not units:
        return []

    return seams.submit_gathered(
        units, job_type=job_type, queue=context["queue"],
        job_definition=context["job_definition"], binding=context["binding"],
        manifest_bucket=context["manifest_bucket"],
        manifest_prefix=context["manifest_prefix"],
        s3_client=context["s3_client"], batch_client=context["batch_client"],
        execute=ConnectionExecutor(conn).execute, run_id=name, reason=reason)


def start_run_audited(conn, idempotency_key, name, phase, reason,
                      proc_date=None, cap=None, dry_run=True,
                      policy_citation=None, out=None):
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

    try:
        job_type, units = gather_for_run(dbh, phase, proc_date=proc_date,
                                         cap=cap)
    except KeyError:
        from submission import routes
        table = ("catalog-load", "crossmatch", "statistics", "merge-dedup")
        raise ValueError(
            "unknown phase %r; run start knows %s" % (phase, ", ".join(table))
        ) from None

    detail = {"phase": phase, "job_type": job_type, "gathered": len(units),
              "cap": cap, "proc_date": proc_date}

    if dry_run:
        print("[dry-run] would gather %d unit(s) for phase=%s (job_type=%s)"
             % (len(units), phase, job_type), file=out)
        result = record_external_action(
            conn, idempotency_key, "run_start", scope, reason,
            dry_run=True, rows_affected=0, detail=detail,
            policy_citation=policy_citation)
        return result, scope

    results = submit_run(conn, name, job_type, units, reason)
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
    """
    from pipeline.intent.writer import (BLOCKED, READY, WRITER_MUTATION_API,
                                        WorkUnitWriter)
    from database.modules.utils.rapid_db_connect import ConnectionExecutor
    writer = WorkUnitWriter(ConnectionExecutor(conn).execute)
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
