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
class RunStartRegistryError(Exception):
    """`run start` was asked to submit under a run the registry refuses.

    Four cases, all refused BEFORE anything is gathered and before any
    audit row is written, because each of them means this command has no
    business submitting work at all (migration 121's ruling, "a scratch
    name without a `runs` row is refused"):

      * no `runs` row of this name — the run was never declared, so the
        submission would carry a name nothing in the registry accounts
        for, which is the pre-108 state the registry exists to end;
      * the row is `kind = 'production'` — production's submissions are
        the VPO's, which resolves the production run's key for itself
        (`production_run_key`); an operator starting one by hand would
        submit production work outside the operator service;
      * the row is `complete` — the operator has declared the run
        finished, and `completed_at` would become a lie;
      * the row is `archived` — its products have been demoted out of
        currency and new work under it would land beside them.

    Rendered by `_cmd_run_start` as `rapidctl: REFUSED — ...` with exit
    64, the same shape `RunStartEnvironmentError` already gets, so an
    operator sees one refusal vocabulary rather than two.
    """


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

#: The only run kind that may name its own job-definition family. A
#: scratch run is scoped to itself — its products are current only within
#: the run and never published — so pointing one at a measurement
#: definition changes nothing anybody else reads. A production run is the
#: published pipeline, and choosing its execution binding on a command
#: line is the exact class of thing that must not be possible by accident.
_FAMILY_OVERRIDE_KIND = "scratch"

#: The Batch job definitions a scratch run's phases submit to, by phase.
#: `rapid-batch.yaml` defines exactly two, mirroring the two pipeline-image
#: definitions' workload split: the science definition is prompt-class and
#: the bulk one carries every bulk-class phase. A phase absent from this map
#: falls through to the parameter tree, which is the right default for one
#: this milestone does not route (`statistics`, `merge-dedup`) -- better a
#: phase that submits to the tree's definition and is refused by IAM than one
#: silently routed to a definition nobody checked.
_SCRATCH_DEFINITIONS = {
    "science": "rapid-scratch-science",
    "reference": "rapid-scratch-bulk",
    "catalog-load": "rapid-scratch-bulk",
    "crossmatch": "rapid-scratch-bulk",
}

#: The only phase it may be named for. The probe definitions are
#: science-class; route validation in the container checks job type, class
#: and queue, so any other phase would be rejected there anyway — refusing
#: here turns a confusing late failure into a clear early one.
_FAMILY_OVERRIDE_PHASE = "science"


def _check_job_definition_family(conn, name, phase, job_definition_family):
    """Refuse a job-definition-family override that is not permitted.

    Reads `runs.kind` from the registry rather than trusting an argument:
    `run start` has no `--kind` flag, and the stored row is the authority
    on what a run IS. A run that does not exist is refused too — the
    override has no meaning without a row to check, and `run start` needs
    the run declared in any case.

    Raises `RunStartEnvironmentError`, which `_cmd_run_start` renders as
    `rapidctl: REFUSED — ...` and exits 64. Checked BEFORE the replay
    lookup and before any gathering, so a refused run neither reads the
    database for units nor records an action.
    """
    if job_definition_family is None:
        return
    if phase != _FAMILY_OVERRIDE_PHASE:
        raise RunStartEnvironmentError(
            "--job-definition-family is accepted only for --phase %s; got "
            "%r. The probe definitions are science-class, and route "
            "validation in the container would reject any other job type "
            "against them." % (_FAMILY_OVERRIDE_PHASE, phase))

    from pipeline.operatorctl.actions import run_row

    run = run_row(conn, name)
    if run is None:
        raise RunStartEnvironmentError(
            "run %r is not declared, so its kind cannot be checked and "
            "--job-definition-family cannot be accepted; declare it with "
            "`rapidctl run create --kind %s` first"
            % (name, _FAMILY_OVERRIDE_KIND))
    if run["kind"] != _FAMILY_OVERRIDE_KIND:
        raise RunStartEnvironmentError(
            "run %r is kind %r; --job-definition-family is accepted only "
            "for a %s run. A production run is the published pipeline, and "
            "its execution binding is not chosen on a command line."
            % (name, run["kind"], _FAMILY_OVERRIDE_KIND))


def production_run_key(conn):
    """The `runs.run_id` of the one live production run, or None.

    THE VPO'S HALF of migration 121's ruling that "production passes
    declare their run": the operator service submits continuously and has
    no `--name` to read, so it resolves the production run's key from the
    registry itself, once per pass, and carries it on every submission it
    makes. That is what stops production's work being the one population
    with no run row behind it.

    None in two DIFFERENT situations, both deliberately treated the same
    way by the caller (a WARN and an unkeyed submission, never a refusal —
    the VPO must keep processing prompt data whatever the registry says):

      * NO non-archived production run exists. Nothing to point at.
      * MORE THAN ONE exists. There is no basis in the registry for
        choosing between them, and picking one — the newest, the
        lowest-numbered — would attribute production's work to a run by an
        arbitrary rule that no operator stated. An ambiguous answer is
        reported as no answer.

    `archived` is excluded rather than `complete`: a completed production
    run whose successor has not yet been declared is still the run whose
    products are current, and attributing today's work to it is more
    truthful than attributing it to nothing.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT run_id FROM runs"
            " WHERE kind = 'production' AND state <> 'archived'"
            " ORDER BY run_id")
        rows = cur.fetchall()
    if len(rows) == 1:
        return rows[0][0]
    return None


def _bind_registry_row(conn, name):
    """The `runs` row `run start` is about to submit under, or refuse.

    Returns the row (a dict, `actions.run_row`'s shape). Raises
    `RunStartRegistryError` for each of the four refusals that type's own
    docstring enumerates — the absent row, the production kind, and the
    two terminal states — with a message naming what to do about it.

    READ-ONLY. The transition to `running` is a separate, audited call
    (`derived.start_run`) made only on an apply and only after gathering
    has produced something to submit; this function's job is to decide
    whether there is any point gathering at all.
    """
    from pipeline.operatorctl.actions import run_row

    run = run_row(conn, name)
    if run is None:
        raise RunStartRegistryError(
            "run %r is not declared: no row in the run registry. A run must "
            "be declared before work can be submitted under it, so that "
            "every submission has an owner, a purpose and a provenance — "
            "declare it first with `rapidctl run create --name %s "
            "--kind scratch --purpose <why> --reason <why> --apply`"
            % (name, name))
    if run["kind"] == "production":
        raise RunStartRegistryError(
            "run %r is kind 'production'; production's submissions are the "
            "operator service's, which resolves the production run's key "
            "for itself. Starting one by hand would submit production work "
            "from outside the operator service" % name)
    if run["state"] in ("complete", "archived"):
        raise RunStartRegistryError(
            "run %r is %s and accepts no further work%s. Declare a new run "
            "for new work" % (
                name, run["state"],
                "; its products have been demoted out of currency"
                if run["state"] == "archived"
                else " — its completed_at has already been recorded"))
    return run


def next_submission_seq(conn, run_key):
    """The next submission ORDINAL for the run keyed `run_key`.

    `1 + max(seq)` over the run's prior `submissions` rows, or 0 when it
    has none — where `seq` is the integer suffix of a row's `run_id`, the
    `<name>-<n>` this ordinal itself produces. Read from the SUBMISSIONS
    TABLE rather than counted in memory because a ramp's steps are
    separate processes, minutes or hours apart: the only thing that knows
    how many batches a run has already submitted is the database.

    **READ BY KEY, NOT BY PREFIX, AND THAT IS THE POINT.** A prefix read
    would also match a DIFFERENT run whose name this one's name is a
    prefix of — which 108's own `create_run` refuses to create, so it
    cannot happen today — but more importantly it would match rows written
    before 121 under the same name with no key, and those are exactly the
    rows whose ordinal this function must not inherit: a pre-121 run's
    single batch is named `<name>` with no suffix at all, so there is no
    ordinal in it to continue from. A run with no keyed submissions starts
    at 0, which is the correct answer for the first step of a run started
    through this code path.

    `run_key is None` returns 0 for the same reason: nothing to read.
    """
    if run_key is None:
        return 0
    with conn.cursor() as cur:
        # The suffix is parsed in SQL rather than by fetching every row and
        # parsing in Python: a run with many batches would otherwise pull
        # its whole submission history across the wire to compute one
        # integer. `split_part(run_id, '-', ...)` cannot be used — a run
        # NAME may itself contain hyphens (`ramp-proof-20260914`), so the
        # ordinal is the segment after the LAST one.
        cur.execute(
            "SELECT max(NULLIF(regexp_replace(run_id, '^.*-', ''), '')"
            "           ::bigint)"
            "  FROM submissions"
            " WHERE run_key = %s AND run_id ~ '-[0-9]+$'",
            [run_key])
        row = cur.fetchone()
    highest = row[0] if row else None
    return 0 if highest is None else int(highest) + 1


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
                   fids=None, reference_set_id=None, psf_set_id=None,
                   reference_image_id=None):
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

    `reference_set_id` IS A THIRD FACT, and does NOT share that value
    (migration 126). It is the set this run differences against, read once
    from the `runs` row by `start_run_audited` and passed here — never
    derived from `run_name`, because the entire point of sets is that a run
    may read a set another run built. `psf_set_id` is that set's declared
    PSF source, resolved with it.

    `reference_image_id` reaches `gathering.gather_science_units` ALONE,
    never `gather_reference_units` — `start_run_audited` already refuses it
    for any phase but `science` before this function is ever called, so
    the `phase == "reference"` branch below has no use for it and is not
    given it; this is enforcement-by-omission of a check made once,
    upstream, not a second copy of that refusal.
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
                run_scope=run_name,
                reference_set_id=reference_set_id, psf_set_id=psf_set_id)
            job_type = routes.JOB_TYPE_REFERENCE_IMAGE
        else:
            units = gathering.gather_science_units(
                dbh, start, end, start_mjdobs=start_mjd, end_mjdobs=end_mjd,
                min_images_to_coadd=min_coadd, fids=fids,
                run_scope=run_name,
                reference_set_id=reference_set_id, psf_set_id=psf_set_id,
                reference_image_id=reference_image_id)
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


def _resolve_submission_env(job_type, lane=None, job_definition_family=None):
    """`submission_env(job_type, lane=lane)`, with its `exit(64)` refusals translated
    to `RunStartEnvironmentError` — the one place that translation happens
    (see `RunStartEnvironmentError`'s own docstring for why it cannot
    happen inside `submission_env` itself). Shared by `submit_run` and, as
    of the two MJD-windowed phases, `_cmd_run_start` — `reference`
    gathering needs this SAME context's `s3_client`/`manifest_bucket`
    before it ever gathers a single unit (its coadd-input publish step, not
    only its eventual submission), so a dry run for that phase must resolve
    it too, never only the eventual `--apply`.

    `lane` is the run's chosen Batch lane (`prompt`/`bulk`, None for the
    job type's default) and reaches `submission_env` unchanged. It is
    resolved here rather than later because the queue it selects is part
    of the binding the audit records: a dry run must show the lane it
    would actually submit to.
    """
    from pipeline.operator.submission import submission_env

    try:
        return submission_env(job_type, lane=lane,
                              job_definition_family=job_definition_family)
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


def _run_envelope(run, lane=None):
    """The run's execution envelope, with `--lane` applied (migration 122).

    Returns a mapping of the four values, or None where the run row carries
    no envelope at all — a row written before 122, against a database that
    has not yet taken it.

    **`lane` OVERRIDES, IT DOES NOT PERSIST.** `run start --lane` chooses
    where THIS submission goes; it does not rewrite the run. The two are
    deliberately different verbs: a flag on one command changing what every
    later submission of the run does would make the run row a record of the
    last command rather than of the run, and `derived.update_run_envelope` is
    the thing that changes what a run is set to.

    **The timeout is NOT re-derived when the lane is overridden.** A run
    created on the bulk lane carries bulk's 43200 s, and sending one batch to
    the prompt lane does not shorten it to 14400: the run's own budget is
    what the operator set, and silently tightening it because a batch went
    somewhere faster could kill work the run was entitled to finish. The lane
    chooses the queue; the timeout stays the run's.
    """
    if run is None or run.get("lane") is None:
        return None
    return {
        "lane": lane or run.get("lane"),
        "retry_attempts": run.get("retry_attempts"),
        "retry_wallclock_s": run.get("retry_wallclock_s"),
        "attempt_timeout_s": run.get("attempt_timeout_s"),
    }


def submit_run(conn, name, job_type, units, reason, context=None,
              work_unit_run_id=None, lane=None, run_key=None,
              envelope=None,
              submission_seq=None, science_overlay=None):
    """Submit `units` under `name`, through the SAME production path
    `live_w9_ramp` uses: `submission_env` for the binding, `pipeline.seams.
    submit_gathered` for the submission itself. Nothing here reimplements
    either — this is the in-process replacement the task ruling calls for,
    run under whatever role `rapidctl` itself already holds (`operator_
    session`'s `SET ROLE`), not a re-derived STS/podman launch.

    `science_overlay`, when given, is passed straight through to
    `seams.submit_gathered` — see that function's and `submit_units`'s own
    docstrings for what it is and how it is bound into the manifest. This
    function has no registry access of its own beyond `conn`; the decision
    of WHETHER a run's overlay applies (kind `scratch`, row's
    `config_overlay` non-empty) is the caller's — `start_run_audited`
    already holds the bound row `_bind_registry_row` returned and resolves
    it there rather than this function re-querying a row it was not
    otherwise given. `None` (every caller but that one) submits exactly as
    before this parameter existed.

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
    — judges the scratch run's own gathered units already claimed and submits
    nothing (observed live 2026-09-11, scratch run
    `awaicgen54-proof-20260911`: GATHER returned 109 run-scoped units,
    submission created zero work_units rows).

    **`work_unit_run_id`, when explicitly given, overrides that default
    (release-scope ruling, 2026-09-11) — for the one case a declared run
    RESUBMITS another run's work units rather than gathering its own.** A
    stranded-unit reconciler releases a prior run's dead-lettered units
    (`ready`, still carrying THAT run's `work_units.run_id`) and something
    must then resubmit them; doing so under a fresh run name would, by the
    `name`-defaulting behaviour above, look them up under the NEW name,
    find nothing, and mint duplicate fresh units — orphaning the released
    ones in `ready` forever, the exact opposite of the intent. Passing the
    prior run's name here as `work_unit_run_id` (while `name`/`run_id`
    stays this submission's own, separately-queryable identity) makes
    `seams._decide_work_unit` find those released units and transition
    them `ready -> submitted` instead. See `seams.submit_gathered`'s own
    docstring for why `run_id` and `work_unit_run_id` are kept as two
    genuinely separate facts rather than one collapsing into the other:
    `run_id` is whose submission this is — the attempts and artifacts it
    authors; `work_unit_run_id` is whose work units it claims. For an
    ordinary run start these are the same value, so this parameter stays
    at its `None` default and `work_unit_run_id` falls back to `name`
    exactly as before this parameter existed.

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
        # `lane` is only consulted on this path. When a caller supplies
        # `context` it has already resolved the lane's queue into it —
        # re-reading the lane here would be a second, possibly
        # disagreeing, resolution of a decision already made.
        context = _resolve_submission_env(job_type, lane=lane)

    if not units:
        return []

    if work_unit_run_id is None:
        work_unit_run_id = name

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
            reason=reason, work_unit_run_id=work_unit_run_id,
            run_key=run_key, submission_seq=submission_seq,
            envelope=envelope, science_overlay=science_overlay,
            protocol_commit=conn.commit)


def start_run_audited(conn, idempotency_key, name, phase, reason,
                      proc_date=None, cap=None, dry_run=True,
                      policy_citation=None, out=None,
                      window_start=None, window_end=None, fids=None,
                      work_unit_run_id=None, lane=None,
                      job_definition_family=None,
                      reference_image_id=None):
    """Gather, (maybe) submit, and audit `run start`. Returns `(result,
    scope)` — the same shape `terminate_jobs_audited` returns, for the same
    reason: the CLI renders both through the identical `render_plan` call.

    `work_unit_run_id`, when given, is threaded straight through to
    `submit_run` — see that function's docstring for the resubmit-a-prior-
    run's-released-units case it exists for. `None` (the default) keeps
    today's behaviour: `submit_run` scopes work units to this run's own
    `name`.

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

    `job_definition_family` submits this run under a NAMED Batch job
    definition family instead of the one the parameter tree gives the
    phase. It exists for measurement: the memory profile's two probe
    definitions run the science code at a different memory ceiling under
    an instrumented image, and reaching them by repointing
    `batch/job-definition-science` would change production as a side
    effect of taking a measurement.

    **Two conditions, both refused loudly, checked HERE and nowhere else.**
    The run's stored `kind` must be `scratch` and the phase must be
    `science`. The kind check is the one that matters: a production run is
    the published pipeline, and pointing it at a definition chosen on a
    command line is exactly the class of thing that must not be possible
    by accident. It reads `runs.kind` rather than trusting an argument —
    `run start` has no `--kind`, and the registry row is the authority on
    what a run IS. The phase check follows from what the probe definitions
    are: science-class definitions, rejected by route validation for any
    other job type, so allowing the flag elsewhere would only produce a
    confusing failure later instead of a clear one now.

    It is the FIRST run-level execution attribute carried at `run start`
    that is not a gathering parameter — the run-envelope brief that follows
    persists such attributes on `runs`; until then it lives in the audit
    scope and detail below, and in the execution binding the attempt row
    records. That binding is what makes provenance exact regardless: the
    attempt carries the versioned ARN actually submitted, so what ran is
    readable from the row without consulting this argument at all.

    `lane` is which Batch lane to submit to — `prompt` or `bulk`, None for
    the job type's default (bulk, since the two-lane change 2026-09-13).
    It reaches `submission_env` through `_resolve_submission_env`, which
    is what actually selects the queue, and it is part of the AUDIT SCOPE
    below: the lane is a chosen execution attribute of the run, so two
    runs of the same name and phase on different lanes are different
    actions and must not replay onto one another. The run-envelope work
    persists it as a `runs` column; here it lives in the scope string and
    in the binding the submission records.

    `reference_image_id` pins every unit this call gathers to ONE
    `refimages.rfid` instead of each unit resolving its own (field,
    filter)-scoped reference through `_best_reference`. **REFUSED unless
    `phase == "science"`**, checked HERE, before anything is gathered and
    before the registry binding below: a `reference` phase's whole job is
    to BUILD a reference image, so a phase already building one cannot
    also be told to reuse one — one of the two would silently be ignored,
    and refusing early says which rather than leaving an operator to infer
    it from which branch `resolve_reference_image` took. Unlike
    `job_definition_family` this carries no `kind == scratch` restriction:
    the task that names it is reusing a specific, already-built reference
    across a run's units, which is exactly as legitimate for a production
    run as for a scratch one, and nothing about pinning a reference lets a
    run claim science content its image digest does not cover — the
    reference itself is still a real, registered `refimages` row.

    It joins the audit scope for the same reason `lane` and
    `job_definition_family` do: a run of the same name and phase started
    with a different pinned reference is a DIFFERENT action from one
    started without the pin, or with a different one, and must not replay
    onto either.
    """
    out = out or sys.stdout
    scope = "run:%s:phase=%s" % (name, phase)
    # The lane joins the scope whenever one was named. `lane=None` — the
    # in-process default, and what every pre-lane caller passes — keeps
    # the scope string it was written with, so no historical idempotency
    # key is stranded by this change. The CLI names its default
    # explicitly (`--lane` defaults to "bulk"), so a run started through
    # `rapidctl` always records which lane it chose rather than leaving
    # the reader to infer it.
    if lane is not None:
        scope += ":lane=%s" % lane
    # The overridden family joins the scope for the same reason the lane
    # does: a run of the same name and phase submitted under a different
    # job definition is a DIFFERENT action, and must not replay onto the
    # one before it. The memory profile's whole design is two runs over
    # the same window and filters differing only in the definition — if
    # the family were absent from the scope, the second would look like a
    # replay of the first and submit nothing.
    if job_definition_family is not None:
        scope += ":job-definition-family=%s" % job_definition_family
    if reference_image_id is not None:
        scope += ":reference-image-id=%s" % reference_image_id

    _check_job_definition_family(conn, name, phase, job_definition_family)

    # CHECKED HERE, before the registry binding and before anything is
    # gathered — the same placement `_check_job_definition_family` uses and
    # for the same reason: a refused `run start` writes no audit row at
    # all, and refusing after gathering has already run would make the
    # refusal look like it cost something it did not.
    if reference_image_id is not None and phase != "science":
        raise RunStartEnvironmentError(
            "--reference-image-id is accepted only for --phase science; "
            "got %r. A %r phase's job is to BUILD a reference image, so it "
            "cannot also be told to reuse one — one of the two would "
            "silently be ignored" % (phase, phase))

    # THE REGISTRY BINDING (migration 121's ruling), BEFORE THE REPLAY
    # LOOKUP AND BEFORE ANY GATHERING. A run this command may not submit
    # under is refused with no audit row at all — the refusal is not a
    # mutation and must not look like one in the ledger. That is also why
    # it sits ahead of `_replay_lookup`: a replayed key for a run that has
    # since been completed must still refuse, rather than returning the
    # earlier success and implying a submission that will not happen.
    run = _bind_registry_row(conn, name)
    run_key = run["run_id"]
    submission_seq = next_submission_seq(conn, run_key)

    # A SCRATCH RUN ROUTES TO THE SCRATCH JOB DEFINITIONS, AUTOMATICALLY.
    #
    # A route names an SSM parameter path, not a definition name
    # (`submission/routes.py`: `Route(JOB_TYPE_SCIENCE, ...,
    # "batch/job-definition-science", ...)`), and
    # `pipeline/operator/submission.py` reads the name out of the parameter
    # tree at submit time. That tree is deployment-wide, so every run --
    # production or scratch -- resolved to `rapid-pipeline-science` /
    # `rapid-pipeline-bulk`. A scratch run submitting to production's
    # definition is exactly what the scratch tier exists to prevent, and it
    # is not a theoretical worry: the first live `run start --apply` under a
    # scratch run tried to submit to
    # `job-definition/rapid-pipeline-science:85` and was refused by the
    # ScratchSubmitter policy, which scopes SubmitJob to `rapid-scratch-*`.
    # The IAM fence caught the routing bug; this is the routing fix.
    #
    # REUSING THE FAMILY OVERRIDE RATHER THAN ADDING A MECHANISM.
    # `submission.resolve_submission_binding` already accepts a
    # `job_definition_family` that replaces the tree's value for one
    # submission, and its gate already lives HERE, beside the run row whose
    # `kind` it reads. Defaulting that family for a scratch run is therefore
    # the same one-line substitution the probe definitions already use, and
    # it leaves production's resolution byte-for-byte alone: a production run
    # passes no family and reads the tree exactly as before.
    #
    # AN EXPLICIT --job-definition-family STILL WINS. `_check_job_definition_
    # family` has already refused it for anything but a scratch science
    # phase, so a caller who named one is a scratch run naming a probe
    # definition deliberately; overriding their choice here would silently
    # undo the measurement they asked for.
    #
    # READS `run["kind"]` OFF THE ROW `_bind_registry_row` JUST BOUND, not a
    # second `run_row` query: that row IS this function's own return value
    # (`actions.run_row`'s shape, validated), so re-fetching it here was a
    # redundant read that also ran BEFORE the registry binding — reaching a
    # live database a beat earlier than the function's own contract places
    # its first read, and doing so unconditionally on every call that names
    # no explicit family (i.e. almost every call, production included).
    if job_definition_family is None and run["kind"] == _FAMILY_OVERRIDE_KIND:
        # The workload class the route would have used. `science` is
        # prompt-class and `reference` bulk-class (`submission/routes.py`
        # header: "science is prompt-class ... the reference-image
        # definition's command names the bulk class"), and the two
        # scratch definitions mirror that split one for one.
        job_definition_family = _SCRATCH_DEFINITIONS.get(phase)
        if job_definition_family is not None:
            scope += ":job-definition-family=%s" % job_definition_family

    # THE RUN'S EXECUTION ENVELOPE (migration 122), read from the row just
    # bound. `--lane` overrides the run's stored lane for THIS submission and
    # nothing else: the run row is not rewritten, because the flag chooses
    # where one batch goes, while `derived.update_run_envelope` is what
    # changes what the run is set to.
    #
    # A run row without the columns falls back to the deployment defaults and
    # SAYS SO in the audit reason, rather than failing: `run start` refusing a
    # missing row is the run-identity brief's item, and a row that predates
    # 122 is a different thing from a row that is absent.
    envelope = _run_envelope(run, lane)
    if envelope is None:
        detail_envelope_note = (
            "no envelope on the run row (predates migration 122); "
            "the job definition's own timeout and retry rows apply")
    else:
        detail_envelope_note = None

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
        context = _resolve_submission_env(
            job_type_for_env, lane=lane,
            job_definition_family=job_definition_family)

    # THE RUN'S DECLARED SET, READ ONCE FROM THE ROW ALREADY BOUND, and
    # REFUSED if a windowed phase has none.
    #
    # Read once here rather than per unit: a submission differences every one
    # of its units against one set, and re-reading per unit would let an
    # operator moving the default part-way through split a single submission
    # across two sets — a subtler version of the "current at gather time"
    # defect migration 126 exists to remove.
    #
    # The refusal covers `reference` and `science` alone (`_WINDOWED_PHASES`);
    # the four post-database-chain phases read no reference and would be
    # refused for a fact they never use. A scratch run with no set is a
    # DEFECT rather than a default: `derived.create_run` stores one on every
    # run it writes, so a NULL here means the row predates 127 or was written
    # around it, and gathering it against a guessed set would be exactly the
    # silent substitution this brief removes.
    reference_set_id = run.get("reference_set_id")
    psf_set_id = run.get("psf_set_id") or reference_set_id
    if windowed and reference_set_id is None:
        raise RunStartRegistryError(
            "run %r declares no reference set, so there is nothing to say "
            "which references its %s phase should difference against. Every "
            "run created since migration 127 stores one; a run without one "
            "predates it. Declare a set on a new run with `rapidctl run "
            "create --reference-set NAME`, or `rapidctl refset list` to see "
            "what exists." % (name, phase))
    if windowed:
        print("  reference set: %s (id %s, PSFs from set %s)"
              % (run.get("reference_set"), reference_set_id, psf_set_id),
              file=out)

    try:
        job_type, units = gather_for_run(
            dbh, phase, proc_date=proc_date, cap=cap, window=window,
            run_name=name,
            s3_client=context["s3_client"] if context else None,
            job_bucket=context["manifest_bucket"] if context else None,
            fids=fids, reference_set_id=reference_set_id,
            psf_set_id=psf_set_id, reference_image_id=reference_image_id)
    except KeyError:
        table = ("catalog-load", "crossmatch", "statistics", "merge-dedup",
                 "reference", "science")
        raise ValueError(
            "unknown phase %r; run start knows %s" % (phase, ", ".join(table))
        ) from None

    detail = {"phase": phase, "job_type": job_type, "gathered": len(units),
              "cap": cap, "proc_date": proc_date,
              "window_start": window_start, "window_end": window_end,
              # The registry facts this submission binds to, recorded on
              # every run_start row so "which registry row did this
              # submission belong to, and which step of it was it" is
              # answerable from the ledger alone.
              "run_key": run_key, "submission_seq": submission_seq,
              "run_state_before": run["state"]}
    # THE ENVELOPE THIS SUBMISSION CARRIED (migration 122), in the audit row
    # as well as on `submissions`. The two answer different questions: the
    # submission row says what one dispatched job was given, the audit row
    # says what the operator's ACTION decided — including, when the run row
    # carried no envelope, that it decided nothing and the job definition's
    # own values applied.
    if envelope is not None:
        detail["envelope"] = dict(envelope)
    elif detail_envelope_note is not None:
        detail["envelope"] = detail_envelope_note
    # Only recorded when it differs from `name` -- an audit row naming a
    # work-unit scope that is just this run's own identity says nothing an
    # ordinary run start didn't already say via `scope` above, and
    # inventing one for every ordinary run would bury the case this field
    # exists to answer: which run's units did an UNUSUAL submission claim.
    if work_unit_run_id is not None and work_unit_run_id != name:
        detail["work_unit_run_id"] = work_unit_run_id
    # Recorded whenever one was named, and the RESOLVED ARN beside it. The
    # family is what the operator asked for; the ARN is what Batch actually
    # bound, revision included. An audit row that carried only the family
    # would leave a reader unable to say which revision ran, which is the
    # whole reason the binding is resolved once and reused rather than
    # re-read at submission.
    if job_definition_family is not None:
        detail["job_definition_family"] = job_definition_family
        if context is not None:
            detail["job_definition_arn"] = context["job_definition"]
    # Recorded whenever named, for the same reason the family override is:
    # a reader of the ledger should see which units were pinned to a
    # specific reference rather than having to re-derive it from the
    # scope string.
    if reference_image_id is not None:
        detail["reference_image_id"] = reference_image_id

    if dry_run:
        print("[dry-run] would gather %d unit(s) for phase=%s (job_type=%s)"
             % (len(units), phase, job_type), file=out)
        # THE ROW IS REPORTED AND NOT TRANSITIONED. `contract.py`'s rule
        # that the plan shown IS what the apply will act on, minus the
        # writing — so the operator sees which registry row was bound, the
        # state it is in NOW, and which batch identity the apply would
        # mint, without the rehearsal moving the run to `running`.
        print("[dry-run] run %s: run_id=%s state=%s, next batch would be "
              "%s-%d" % (name, run_key, run["state"], name, submission_seq),
              file=out)
        result = record_external_action(
            conn, idempotency_key, "run_start", scope, reason,
            dry_run=True, rows_affected=0, detail=detail,
            policy_citation=policy_citation)
        return result, scope

    # `created`/`running` -> `running`, BEFORE the submission and in the
    # same transaction as it (migration 121). Before, because a run whose
    # first batch is in Batch while its row still says `created` is a run
    # the registry is lying about; and in the same transaction, because
    # `submit_run` runs under `ConnectionExecutor(conn,
    # autocommit_each=False)` and commits only at `submit_units`'s own two
    # protocol boundaries — so this write becomes durable exactly when the
    # submission it accompanies does, and a submission that never happens
    # leaves the row where it was.
    #
    # Idempotent from `running` by `derived.start_run`'s own contract,
    # which is what makes a ramp step legal: the second step of a run is
    # this same call on an already-`running` row, and it must be a no-op
    # on the row rather than a refusal.
    from pipeline.operatorctl.actions import start_run as _start_run_fn
    _start_run_fn(conn, idempotency_key + ":state", name, reason,
                  dry_run=False, policy_citation=policy_citation)

    # THE OVERLAY IS PASSED ONLY FOR A SCRATCH RUN WHOSE ROW ACTUALLY
    # CARRIES ONE — never for production, and never as an empty dict.
    # `run["kind"]` comes from the row `_bind_registry_row` already bound
    # above (`run`, `run_key = run["run_id"]`); reusing it here rather than
    # re-querying is the same reasoning `_check_job_definition_family`
    # already uses for the identical check just above in this file. Kind
    # is the gate, not the overlay's presence, so that a scratch run with
    # no `--set` at `run create` submits with `science_overlay=None` and a
    # production run's manifest is untouched by this parameter existing at
    # all, whatever `config_overlay` happens to hold on its row (which
    # `_RUN_ROW` does not even select — production is never scratch, so
    # this branch never reads it for one).
    #
    # `config_overlay` ITSELF IS A SEPARATE QUERY, not a field on `run`:
    # `actions._RUN_ROW` does not select it (checked: its column list stops
    # at the reference-set join), and this file's own repo-isolation rule
    # keeps `actions.py` off limits to a `seams.py`/`run.py` change. One
    # extra single-column read, gated the same way the parameter it feeds
    # is, costs nothing a production run would ever pay.
    science_overlay = None
    if run["kind"] == _FAMILY_OVERRIDE_KIND:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT config_overlay FROM runs WHERE run_id = %s",
                (run["run_id"],))
            row = cur.fetchone()
        # psycopg2 decodes a `jsonb` column to a Python object (dict, here)
        # on read, exactly as `main.py` writes it with `%s::jsonb` and
        # `json.dumps` on the way in — no `json.loads` anywhere in this
        # codebase unpacks a jsonb column a second time (checked against
        # every other `::jsonb` site in `actions.py`), so `overlay` below
        # is already a `dict` or `None`, never a JSON string to parse.
        overlay = row[0] if row else None
        if overlay:
            # Falsy (None or {}) stays None: an empty overlay is "no
            # override" exactly as `manifest.py`'s own
            # `_validate_science_overlay` treats it, and `submit_units`'s
            # docstring is explicit that None is what "no override" means
            # to the manifest it builds.
            science_overlay = overlay

    results = submit_run(conn, name, job_type, units, reason, context=context,
                         work_unit_run_id=work_unit_run_id, lane=lane,
                         run_key=run_key, submission_seq=submission_seq,
                         envelope=envelope, science_overlay=science_overlay)
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

    # UNDER `submission_role` ON AN APPLY, for the same reason `submit_run`
    # and `_release_one` are -- THE SAME IDENTITY DEFECT, reached through a
    # fourth entry point. Registration performs the PIPELINE's own work: it
    # inserts product rows and advances each attempt's watermark with
    # `UPDATE attempts`, and the operate tier holds only `rapid_read`:
    #
    #     has_table_privilege('rapid_operator', 'attempts', 'UPDATE') = false
    #     has_table_privilege('rapid_admin',    'attempts', 'UPDATE') = true
    #
    # so every attempt failed with `permission denied for table attempts`
    # at the watermark write -- found live on 2026-09-11 registering the
    # acceptance run, 1,557 attempts in, having registered nothing.
    #
    # A DRY RUN STAYS ON THE OPERATE TIER. It writes nothing, so widening
    # for it would hand the read-only rehearsal a write-capable identity
    # for no reason -- and the rehearsal being unable to write is part of
    # what makes it a rehearsal.
    #
    # The audit row below is written OUTSIDE the switch, so the ledger
    # keeps naming the human, exactly as `release_dead_letters_audited`
    # does.
    if dry_run:
        run, rows = run_scoped_registration(
            conn, run_id_prefix=name, dry_run=dry_run,
            records_bucket=records_bucket, s3_client=s3_client)
    else:
        with submission_role(conn):
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
# `run reconcile-stranded` — Batch-discovery release for units a scratch
# run's array children died on before ever reaching the dead-letter shape
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
#: `blocked_reason` is selected, not filtered in SQL, so a unit parked for a
#: reason this reconciler must not release is EXCLUDED VISIBLY rather than
#: silently absent — "which units did you decline, and why" is a question the
#: caller has to be able to answer, and a row the query never returned cannot
#: answer it.
_STRANDED_UNITS_SQL = (
    "SELECT w.work_unit_id, w.state, w.blocked_reason,"
    "       array_agg(DISTINCT a.scheduler_job_id) FILTER"
    "         (WHERE a.scheduler_job_id IS NOT NULL),"
    "       bool_or(a.rapid_outcome = 'success')"
    " FROM work_units w"
    " JOIN attempts a ON a.work_unit_id = w.work_unit_id"
    " WHERE a.run_id LIKE %s"
    "   AND w.state IN ('submitted', 'blocked')"
    " GROUP BY w.work_unit_id, w.state, w.blocked_reason"
    " ORDER BY w.work_unit_id"
)

#: The one blocked reason this reconciler may release, matching
#: `_RELEASE_CANDIDATES_SQL`'s own predicate rather than choosing a second
#: policy. The retry policy also parks units under
#: `application_failure:input_missing`, which is a park-until-the-input-
#: arrives condition: a FAILED Batch child and no successful attempt say
#: nothing about whether the input arrived, so releasing such a unit re-runs
#: work that fails the same way. Measured on the live acceptance run, all
#: 1,485 blocked units carry `internal_error`, so this constraint changes
#: nothing about that release — it stops a LATER run's parked units being
#: swept in by a reconciler that widened which rows it selects.
_RELEASABLE_BLOCKED_REASON = "application_failure:internal_error"


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
      released; checked FIRST, see below) or `"batch_child_not_failed"`
      plus `scheduler_job_id` (some Batch child of this unit's attempts is
      not FAILED — SUCCEEDED, or still in flight; the retry case, 1,604
      live instances of it).

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
    for (work_unit_id, state, blocked_reason,
         scheduler_job_ids, has_success) in rows:
        scheduler_job_ids = scheduler_job_ids or []
        not_failed = [sid for sid in scheduler_job_ids
                     if fate.get(sid) != "FAILED"]
        # A unit with NO Batch child at all cannot satisfy "every child
        # FAILED" -- it satisfies it VACUOUSLY, which is the opposite of
        # evidence. There is no child, so there is nothing saying the work
        # failed, and releasing it would re-run work whose fate is simply
        # unknown to Batch. Tested FIRST, before the emptiness can be
        # mistaken for agreement. Measured on the live acceptance run:
        # zero units are in this state, so this guards a hazard rather
        # than fixing a live miss -- but the reconciler is about to act on
        # thousands of units, and a vacuous truth is a bad thing to have
        # standing between it and them.
        if not scheduler_job_ids:
            excluded.append({
                "work_unit_id": work_unit_id,
                "reason": "no_batch_child",
            })
            continue
        # `has_success` is tested BEFORE `not_failed` — a unit failing
        # BOTH tests is excluded either way, so this order changes no
        # unit's candidate/excluded classification, only which reason is
        # REPORTED for a unit failing both. "This unit's work already
        # succeeded" is the more informative account than "a Batch child
        # of it hasn't failed yet" when both are true, so it is surfaced
        # first rather than being hidden behind the other reason.
        if has_success:
            excluded.append({
                "work_unit_id": work_unit_id,
                "reason": "successful_sibling_attempt",
            })
            continue
        if not_failed:
            excluded.append({
                "work_unit_id": work_unit_id,
                "reason": "batch_child_not_failed",
                "scheduler_job_id": not_failed[0],
            })
            continue
        # A blocked unit is releasable only under the reason
        # `release-dead-letters` itself releases; see
        # `_RELEASABLE_BLOCKED_REASON` for why a differently-parked unit
        # must not be swept in. A `submitted` unit carries no reason at
        # all and is unaffected -- which matters, because those are the
        # majority of the real candidate set.
        # The literal, not an import of `pipeline.intent.writer.BLOCKED`:
        # this module keeps that import function-local (see
        # `_release_one`), and the same literal is what the SQL above
        # selects on, so the two agree by sharing one spelling.
        if state == "blocked" and blocked_reason != _RELEASABLE_BLOCKED_REASON:
            excluded.append({
                "work_unit_id": work_unit_id,
                "reason": "blocked_reason_not_releasable",
                "blocked_reason": blocked_reason,
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
