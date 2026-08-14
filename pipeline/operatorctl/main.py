"""``rapidctl`` — the command-line entry point.

THIS MODULE OWNS EXITING, and it is the only one in the package that
does. Rule 17's finding is that library code must not terminate the
process; the corollary is that something must, and that something is the
entrypoint. Every module below this one raises; ``main`` catches, prints
an operator-legible account, and returns the code.

THE SHAPE OF EVERY MUTATING SUBCOMMAND is fixed by ``contract.py`` and
uniform on purpose:

    rapidctl <action> <scope args> --reason '...' [--idempotency-key K]
             [--expect ...] [--apply]

Without ``--apply`` the command runs the action's dry run — the real
function against the real population — and prints the plan it would
execute. With ``--apply`` it performs it exactly once under the key.
The safe call is the short one.
"""

import argparse
import sys

from pipeline.intent.application_contract import ApplicationContractUnmet
from pipeline.intent.schema_contract import SchemaContractUnmet
from pipeline.operatorctl import actions
from pipeline.operatorctl.contract import (OperatorError, new_idempotency_key,
                                           render_plan)
from pipeline.operatorctl.session import (OperatorSessionError,
                                          break_glass_role, operator_session)

EXIT_OK = 0
EXIT_USAGE = 2
# 64 is the repo's established "cannot connect / configuration is wrong"
# code (`rapid_db.py`'s documented contract, and what the legacy call sites
# tested for). Reused here so an operator reading an exit code from a wrapper
# script sees the same meaning they already know.
EXIT_CONFIG = 64
EXIT_NOT_AUTHORIZED = 77


def _mutation_arguments(parser, scope_help):
    """The four contract arguments every mutating subcommand carries.

    Added by one function rather than repeated per subcommand, so a new
    action cannot accidentally ship without a mandatory reason or with a
    default that makes ``--apply`` implicit.
    """
    parser.add_argument("--reason", required=True,
                        help="why this action is being taken. Mandatory: it "
                             "is recorded in the audit ledger and there is "
                             "no default (a defaulted reason is the API "
                             "supplying one for the operator)")
    parser.add_argument("--idempotency-key", default=None,
                        help="makes this action performable exactly once. "
                             "Minted if omitted and printed in the result; "
                             "re-run with the SAME key to replay rather than "
                             "repeat")
    parser.add_argument("--apply", action="store_true",
                        help="perform the action. Without it the command "
                             "runs the real dry run and prints the plan")
    parser.add_argument("--policy-citation", default=None,
                        help="the versioned policy authorizing this action, "
                             "for an automated or policy-driven caller")
    parser.set_defaults(_scope_help=scope_help)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="rapidctl",
        description="The operator surface for RAPID's audited mutation API. "
                    "Every mutating action takes a mandatory reason, is "
                    "dry-run by default, and writes one row to "
                    "derived.mutation_audit per real invocation.")
    sub = parser.add_subparsers(dest="command", required=True)

    # --- operate tier ------------------------------------------------------
    retry = sub.add_parser(
        "retry-parked",
        help="release parked attempts within a run_id scope",
        description="Release parked (application-failed, terminal) attempts "
                    "for resubmission. Records the release decision; "
                    "submission itself stays with the orchestrator, its "
                    "single writer.")
    retry.add_argument("--run-id", required=True,
                       help="the mandatory scope — there is no unscoped retry")
    retry.add_argument("--max-attempts", type=int, default=50,
                       help="bound on the population (default 50)")
    retry.add_argument("--expect-candidates", type=int, default=None,
                       help="the candidate count seen in the dry run. The "
                            "apply REFUSES if the population has moved since")
    _mutation_arguments(retry, "attempts in a run")
    retry.set_defaults(func=_cmd_retry_parked)

    repair_outbox = sub.add_parser(
        "repair-refused-outbox",
        help="move REFUSED alert_outbox rows for one release back to PENDING",
        description="Repair after an operator has fixed the external "
                    "condition (broker ACL, client/broker version) that "
                    "terminalized delivery — the classification itself is "
                    "unchanged, and the repaired rows re-enter the ordinary "
                    "PENDING flow to be claimed, authorized and sent by the "
                    "next publisher cycle exactly as any other PENDING row.")
    repair_outbox.add_argument("--release-identity", required=True,
                               help="the mandatory scope — there is no "
                                    "unscoped repair")
    repair_outbox.add_argument("--max-rows", type=int, default=200,
                               help="bound on the population (default 200)")
    repair_outbox.add_argument("--expect-candidates", type=int, default=None,
                               help="the REFUSED count seen in the dry run. "
                                    "The apply REFUSES if the population has "
                                    "moved since")
    _mutation_arguments(repair_outbox, "REFUSED outbox rows for a release")
    repair_outbox.set_defaults(func=_cmd_repair_refused_outbox)

    category = sub.add_parser(
        "add-problem-category",
        help="extend the problems-taxonomy vocabulary",
        description="Add a problem category. The audit history is the "
                    "vocabulary's versioning, so the added row points back "
                    "at the audit id that introduced it.")
    category.add_argument("--category", required=True)
    category.add_argument("--description", default=None)
    category.add_argument("--expect-absent", action="store_true",
                          help="refuse if the category already exists, "
                               "instead of reporting a no-op success")
    _mutation_arguments(category, "a problem category")
    category.set_defaults(func=_cmd_add_category)

    # --- wrapped ad-hoc tools (G3) ----------------------------------------
    terminate = sub.add_parser(
        "terminate-batch-jobs",
        help="terminate AWS Batch jobs, under the mutation contract",
        description="Terminate Batch jobs in the given states. The action's "
                    "target is AWS, but the operator action is recorded in "
                    "the same ledger as every database mutation — the "
                    "ledger records operator actions, not only database "
                    "changes.")
    terminate.add_argument("--queue", required=True, help="job queue name or ARN")
    terminate.add_argument("--states", nargs="+", default=["RUNNING"],
                           metavar="STATE",
                           help="job states to terminate (default: RUNNING)")
    terminate.add_argument("--expect-jobs", type=int, default=None,
                           help="the job count seen in the dry run; the "
                                "apply refuses if the queue has moved since")
    terminate.add_argument("--region", default=None)
    terminate.add_argument("--profile", default=None)
    _mutation_arguments(terminate, "Batch jobs in a queue")
    terminate.set_defaults(func=_cmd_terminate_batch)

    supersede = sub.add_parser(
        "supersede-lost-evidence",
        help="supersede attempts whose cited record object is gone",
        description="Append superseding closure records for attempts whose "
                    "cited record object no longer exists. Wraps the "
                    "reconciler tool, keeping its per-row precondition "
                    "re-check and publish-before-flag ordering, and adds the "
                    "captured reason and durable audit row it lacked.")
    supersede.add_argument("--run-prefix", required=True,
                           help="run_id prefix to consider, e.g. 'fixd-chain-'")
    _mutation_arguments(supersede, "attempts under a run prefix")
    supersede.set_defaults(func=_cmd_supersede)

    # --- break-glass -------------------------------------------------------
    bg = sub.add_parser(
        "break-glass",
        help="the three break-glass events",
        description="For the API's own failure modes only — never a faster "
                    "path. None of the three is dry-runnable: the event IS "
                    "the mutation.")
    bgsub = bg.add_subparsers(dest="event", required=True)

    bg_open = bgsub.add_parser("open", help="open a session, loudly")
    bg_open.add_argument("--reason", required=True)
    bg_open.add_argument("--target-scope", required=True)
    bg_open.set_defaults(func=_cmd_bg_open)

    bg_close = bgsub.add_parser("close", help="close explicitly")
    bg_close.add_argument("--open-audit-id", type=int, required=True)
    bg_close.add_argument("--reason", required=True)
    bg_close.add_argument("--tables-touched", required=True,
                          help="mandatory: closing without it is not closing")
    bg_close.add_argument("--changes", required=True,
                          help="mandatory: what was actually changed")
    bg_close.set_defaults(func=_cmd_bg_close)

    bg_rec = bgsub.add_parser("reconcile", help="record the sweep's outcome")
    bg_rec.add_argument("--open-audit-id", type=int, required=True)
    bg_rec.add_argument("--reason", required=True)
    passed = bg_rec.add_mutually_exclusive_group(required=True)
    passed.add_argument("--passed", dest="passed", action="store_true")
    passed.add_argument("--failed", dest="passed", action="store_false")
    bg_rec.set_defaults(func=_cmd_bg_reconcile)

    # --- read-only ---------------------------------------------------------
    unrec = sub.add_parser(
        "unreconciled", help="region 7: break-glass sessions not yet cleared",
        description="Hard target 0. A row here is an open break-glass "
                    "session lacking a close, a passing reconciliation, or "
                    "both.")
    unrec.set_defaults(func=_cmd_unreconciled)

    audit = sub.add_parser("audit", help="the tail of the mutation ledger")
    audit.add_argument("--limit", type=int, default=20)
    audit.set_defaults(func=_cmd_audit)

    attempts_view = sub.add_parser(
        "attempts",
        help="list attempts by lifecycle state and age",
        description="The coarse panel: every attempt in one lifecycle "
                    "state older than a bound. Read-only — no reason, no "
                    "idempotency key, nothing here mutates. A row flagged "
                    "'success+none' reported success with no product "
                    "recorded; see `show-attempt` for the joined detail.")
    attempts_view.add_argument("--state", required=True,
                               help="the exact lifecycle_state to match "
                                    "(e.g. terminal_after_start, "
                                    "application_closed, submitted)")
    attempts_view.add_argument(
        "--older-than", dest="older_than", required=True,
        help="age bound, e.g. '2h', '3d', '30m', or a bare integer of "
             "seconds. Measured from the row's own latest timestamp "
             "(ended_at, else started_at, else submitted_at)")
    attempts_view.set_defaults(func=_cmd_attempts)

    show_attempt = sub.add_parser(
        "show-attempt",
        help="the joined detail view for one attempt",
        description="One attempt, its terminal-record columns, its "
                    "attempt_stages rows, its registration_outcome, its "
                    "work unit and its most recent submission — the "
                    "operator's single place to look rather than five "
                    "separate queries.")
    show_attempt.add_argument("attempt_id", type=int)
    show_attempt.set_defaults(func=_cmd_show_attempt)

    # --- release pointer (H2) ---------------------------------------------
    release = sub.add_parser(
        "set-admission-release",
        help="switch the release future admissions are stamped with",
        description="Point future admissions at a release. Affects ONLY "
                    "admissions made after the change: it never rewrites an "
                    "existing admission's stamp and never touches in-flight "
                    "work, which is exactly rule 18's rollback clause. The "
                    "named release must already be registered.")
    release.add_argument("--release-identity", required=True)
    release.add_argument("--expect-current", default=None,
                         help="the release the dry run showed as current. "
                              "The apply REFUSES if the pointer has moved "
                              "since")
    _mutation_arguments(release, "the admission release pointer")
    release.set_defaults(func=_cmd_set_release)

    # --- GC (H3/H4) --------------------------------------------------------
    gc_compute = sub.add_parser(
        "gc-compute-plan",
        help="compute a recorded GC plan from a pinned inventory",
        description="Pass 1 of the two-pass process (§4.11 steps 1-4): read a "
                    "pinned S3 inventory, anti-join it against every "
                    "reference surface, apply the five-clause candidate rule, "
                    "and record a checksummed plan. Dry-run by default, and "
                    "THE DRY RUN DOES THE REAL WORK — real inventory, real "
                    "reference queries, real anti-join — it simply does not "
                    "write the plan.")
    gc_compute.add_argument("--inventory", required=True,
                            help="path to the pinned inventory report (JSON "
                                 "lines of {bucket,key,version_id,size,"
                                 "last_modified}, or a manifest naming them)")
    gc_compute.add_argument("--inventory-id", required=True,
                            help="the inventory's own identity, recorded on "
                                 "the plan")
    gc_compute.add_argument("--inventory-taken-at", required=True,
                            help="when the snapshot was taken (ISO-8601). "
                                 "An inventory older than --freshness is "
                                 "refused")
    gc_compute.add_argument("--freshness", type=int, default=None,
                            help="maximum inventory age in seconds. NO "
                                 "DEFAULT: an unbounded staleness check is "
                                 "not a check")
    gc_compute.add_argument("--bucket", action="append", default=None,
                            metavar="BUCKET",
                            help="declared bucket scope; repeatable. In this "
                                 "package the declared scope is the products "
                                 "bucket alone")
    gc_compute.add_argument("--prefix", action="append", default=None,
                            metavar="PREFIX", help="declared key prefixes")
    gc_compute.add_argument("--horizon-seconds", type=int, default=None,
                            help="the safety horizon. WITH NONE CONFIGURED "
                                 "THE PLAN DELETES NOTHING and says why — "
                                 "there is deliberately no default that "
                                 "permits deletion")
    gc_compute.add_argument("--horizon-provenance", default=None,
                            help="where the horizon came from. A horizon "
                                 "without a stated provenance is a guess "
                                 "wearing a number")
    gc_compute.add_argument("--max-deletions", type=int, required=True,
                            help="the plan's bound. A plan exceeding it is "
                                 "REFUSED at computation, never truncated at "
                                 "execution")
    gc_compute.add_argument("--allow-class", action="append", default=None,
                            metavar="CLASS",
                            help="add an object class to the deletable-class "
                                 "allowlist for this plan. Opt-in and empty "
                                 "by default: with none given the plan "
                                 "deletes nothing, which is a conforming "
                                 "outcome")
    _mutation_arguments(gc_compute, "a GC candidate set")
    gc_compute.set_defaults(func=_cmd_gc_compute)

    gc_execute = sub.add_parser(
        "gc-execute-plan",
        help="execute an approved GC plan",
        description="Pass 2 (§4.11 step 6): delete the EXACT object versions "
                    "an approved plan recorded, in bounded batches, behind "
                    "the fence. Every item is re-verified immediately before "
                    "its delete; anything whose version moved, whose fence "
                    "cannot be taken, or which became referenced is skipped "
                    "and reported while the run continues.")
    gc_execute.add_argument("--plan-id", type=int, required=True)
    _mutation_arguments(gc_execute, "an approved GC plan")
    gc_execute.set_defaults(func=_cmd_gc_execute)

    gc_recompute = sub.add_parser(
        "gc-recompute-plan",
        help="run pass 2's recomputation against a second pinned inventory",
        description="§4.11 step 5, which is MANDATORY rather than optional: "
                    "after the horizon elapses the anti-join is recomputed "
                    "against a SECOND pinned inventory, and only candidates "
                    "absent in BOTH passes survive. Anything that reappeared "
                    "is excluded by status; its row and the plan checksum are "
                    "untouched.")
    gc_recompute.add_argument("--plan-id", type=int, required=True)
    gc_recompute.add_argument("--inventory", required=True)
    gc_recompute.add_argument("--inventory-id", required=True)
    gc_recompute.add_argument("--inventory-taken-at", required=True)
    gc_recompute.add_argument("--freshness", type=int, default=None)
    _mutation_arguments(gc_recompute, "a computed GC plan")
    gc_recompute.set_defaults(func=_cmd_gc_recompute)

    gc_approve = sub.add_parser(
        "gc-approve-plan",
        help="approve a recomputed GC plan for execution",
        description="Approval is a DISTINCT recorded act with its own actor. "
                    "A plan cannot reach EXECUTING without both a recorded "
                    "recomputation and a recorded approval — that is the "
                    "two-pass requirement. Self-approval by the computing "
                    "actor is permitted in this single-operator system and "
                    "is recorded as such.")
    gc_approve.add_argument("--plan-id", type=int, required=True)
    _mutation_arguments(gc_approve, "a GC plan")
    gc_approve.set_defaults(func=_cmd_gc_approve)

    gc_show = sub.add_parser(
        "gc-plan", help="show a recorded GC plan and its items",
        description="The plan as recorded, with its candidate checksum, its "
                    "horizon and provenance, and its items by status.")
    gc_show.add_argument("--plan-id", type=int, required=True)
    gc_show.set_defaults(func=_cmd_gc_show)

    return parser


# ---------------------------------------------------------------------------
# Subcommand bodies. Each takes an open operator session and returns an exit
# code; none of them exits.
# ---------------------------------------------------------------------------
def _cmd_retry_parked(conn, args, out):
    expected = ({"candidates": args.expect_candidates}
                if args.expect_candidates is not None else None)
    key = args.idempotency_key or new_idempotency_key("retry")
    result = actions.retry_parked_attempts(
        conn, key, args.run_id, args.reason, expected_state=expected,
        max_attempts=args.max_attempts, dry_run=not args.apply,
        policy_citation=args.policy_citation)
    print(render_plan("scoped_retry",
                      "attempts:run_id=%s:limit=%s" % (args.run_id,
                                                       args.max_attempts),
                      args.reason, key, result, args.apply), file=out)
    return EXIT_OK


def _cmd_repair_refused_outbox(conn, args, out):
    expected = ({"candidates": args.expect_candidates}
                if args.expect_candidates is not None else None)
    key = args.idempotency_key or new_idempotency_key("repair-outbox")
    result = actions.repair_refused_outbox_rows(
        conn, key, args.release_identity, args.reason,
        expected_state=expected, max_rows=args.max_rows,
        dry_run=not args.apply, policy_citation=args.policy_citation)
    print(render_plan(
        "repair_refused_outbox",
        "alert_outbox:release_identity=%s:limit=%s" % (args.release_identity,
                                                        args.max_rows),
        args.reason, key, result, args.apply), file=out)
    return EXIT_OK


def _cmd_add_category(conn, args, out):
    expected = {"already_present": False} if args.expect_absent else None
    key = args.idempotency_key or new_idempotency_key("category")
    result = actions.add_problem_category(
        conn, key, args.category, args.description, args.reason,
        expected_state=expected, dry_run=not args.apply,
        policy_citation=args.policy_citation)
    print(render_plan("problem_vocabulary_extend",
                      "problem_categories:%s" % args.category,
                      args.reason, key, result, args.apply), file=out)
    return EXIT_OK


def _cmd_terminate_batch(conn, args, out):
    # Imported here, not at module scope: the read-only and database-only
    # subcommands must not require boto3 to be installed, and this is the
    # only path that talks to AWS.
    from pipeline.operatorctl.batch import terminate_jobs_audited
    key = args.idempotency_key or new_idempotency_key("batch-terminate")
    expected = ({"jobs": args.expect_jobs}
                if args.expect_jobs is not None else None)
    result, scope = terminate_jobs_audited(
        conn, key, args.queue, args.states, args.reason,
        expected_state=expected, dry_run=not args.apply,
        region=args.region, profile=args.profile,
        policy_citation=args.policy_citation, out=out)
    print(render_plan("external_batch_terminate", scope, args.reason, key,
                      result, args.apply), file=out)
    return EXIT_OK


def _cmd_supersede(conn, args, out):
    from pipeline.operatorctl.supersede import supersede_audited
    key = args.idempotency_key or new_idempotency_key("supersede")
    result, scope = supersede_audited(
        conn, key, args.run_prefix, args.reason, dry_run=not args.apply,
        policy_citation=args.policy_citation, out=out)
    print(render_plan("external_evidence_supersede", scope, args.reason, key,
                      result, args.apply), file=out)
    return EXIT_OK


def _cmd_bg_open(conn, args, out):
    with break_glass_role(conn) as bg_conn:
        audit_id = actions.break_glass_open(bg_conn, args.reason,
                                            args.target_scope)
    print("BREAK-GLASS OPEN", file=out)
    print("  open audit id : %s" % audit_id, file=out)
    print("  target scope  : %s" % args.target_scope, file=out)
    print("  reason        : %s" % args.reason, file=out)
    print("", file=out)
    print("  This session is visible in `rapidctl unreconciled` from now",
          file=out)
    print("  until it is BOTH closed and reconciled with a pass.", file=out)
    return EXIT_OK


def _cmd_bg_close(conn, args, out):
    with break_glass_role(conn) as bg_conn:
        audit_id = actions.break_glass_close(
            bg_conn, args.open_audit_id, args.reason, args.tables_touched,
            args.changes)
    print("BREAK-GLASS CLOSE", file=out)
    print("  close audit id : %s" % audit_id, file=out)
    print("  closes         : %s" % args.open_audit_id, file=out)
    print("  tables touched : %s" % args.tables_touched, file=out)
    return EXIT_OK


def _cmd_bg_reconcile(conn, args, out):
    with break_glass_role(conn) as bg_conn:
        audit_id = actions.break_glass_reconcile(
            bg_conn, args.open_audit_id, args.reason, args.passed)
    print("BREAK-GLASS RECONCILE", file=out)
    print("  reconcile audit id : %s" % audit_id, file=out)
    print("  reconciles         : %s" % args.open_audit_id, file=out)
    print("  passed             : %s" % args.passed, file=out)
    if not args.passed:
        print("", file=out)
        print("  A FAILED reconciliation does not clear the region 7 row.",
              file=out)
    return EXIT_OK


def _cmd_unreconciled(conn, args, out):
    rows = actions.unreconciled_break_glass(conn)
    if not rows:
        print("No unreconciled break-glass sessions. (Hard target 0: met.)",
              file=out)
        return EXIT_OK
    print("UNRECONCILED BREAK-GLASS — %d session(s)" % len(rows), file=out)
    for row in rows:
        print("", file=out)
        print("  open audit id : %s" % row["open_audit_id"], file=out)
        print("  state         : %s" % row["state"], file=out)
        print("  opened by     : %s at %s" % (row["actor"], row["opened_at"]),
              file=out)
        print("  age (hours)   : %s" % row["age_hours"], file=out)
        print("  scope         : %s" % row["target_scope"], file=out)
        print("  reason        : %s" % row["open_reason"], file=out)
    return EXIT_OK


def _cmd_audit(conn, args, out):
    rows = actions.recent_mutations(conn, limit=args.limit)
    if not rows:
        print("The mutation ledger is empty.", file=out)
        return EXIT_OK
    print("MUTATION LEDGER — most recent %d" % len(rows), file=out)
    for row in rows:
        marker = "dry-run" if row["dry_run"] else "APPLIED"
        print("", file=out)
        print("  %s  [%s]  %s" % (row["audit_id"], marker,
                                  row["action_class"]), file=out)
        print("    at     : %s by %s" % (row["performed_at"], row["actor"]),
              file=out)
        print("    scope  : %s" % row["target_scope"], file=out)
        print("    reason : %s" % row["reason"], file=out)
        print("    rows   : %s" % row["rows_affected"], file=out)
        if row.get("idempotency_key"):
            print("    key    : %s" % row["idempotency_key"], file=out)
    return EXIT_OK


def _parse_duration_seconds(text):
    """Parse '2h', '3d', '30m', '90s' or a bare integer, into seconds.

    No calendar arithmetic — a day here is always 86400 seconds, never a
    DST-aware civil day — because `--older-than` bounds an age computed in
    the database from timestamps, and the two must agree on what a unit
    means or the CLI's own rehearsal ("N attempts older than 3d") would
    disagree with the query it is about to run.
    """
    text = text.strip()
    suffixes = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if text and text[-1].lower() in suffixes:
        magnitude, unit = text[:-1], text[-1].lower()
    else:
        magnitude, unit = text, "s"
    try:
        value = int(magnitude)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "rapidctl: --older-than %r is not a duration; use an integer "
            "optionally suffixed s/m/h/d (e.g. '2h', '3d', '90')" % text)
    if value < 0:
        raise argparse.ArgumentTypeError(
            "rapidctl: --older-than %r is negative; an age bound cannot be"
            % text)
    return value * suffixes[unit]


def _cmd_attempts(conn, args, out):
    from pipeline.operatorctl import actions
    older_than_seconds = _parse_duration_seconds(args.older_than)
    rows = actions.attempts_by_state(conn, args.state, older_than_seconds)
    if not rows:
        print("No attempts in state %r older than %s." % (
            args.state, args.older_than), file=out)
        return EXIT_OK
    print("ATTEMPTS — state=%s, older than %s — %d row(s)" % (
        args.state, args.older_than, len(rows)), file=out)
    for row in rows:
        anomaly = "  [success+none: no product recorded]" \
            if row["success_with_no_product"] else ""
        print("", file=out)
        print("  attempt %s  run=%s  logical_job=%s" % (
            row["attempt_id"], row["run_id"], row["logical_job_id"]),
            file=out)
        print("    scheduler_job : %s" % row["scheduler_job_id"], file=out)
        print("    outcome       : %s / %s%s" % (
            row["rapid_outcome"], row["product_disposition"], anomaly),
            file=out)
        print("    error_category: %s" % row["error_category"], file=out)
        print("    submitted/started/ended : %s / %s / %s" % (
            row["submitted_at"], row["started_at"], row["ended_at"]),
            file=out)
    return EXIT_OK


def _cmd_show_attempt(conn, args, out):
    from pipeline.operatorctl import actions
    detail = actions.attempt_detail(conn, args.attempt_id)
    if detail is None:
        print("rapidctl: no attempt %s" % args.attempt_id, file=sys.stderr)
        return EXIT_USAGE

    print("ATTEMPT %s" % detail["attempt_id"], file=out)
    print("  run_id           : %s" % detail["run_id"], file=out)
    print("  logical_job_id   : %s" % detail["logical_job_id"], file=out)
    print("  lifecycle_state  : %s" % detail["lifecycle_state"], file=out)
    print("  scheduler_job_id : %s" % detail["scheduler_job_id"], file=out)
    print("  scope            : exposure=%s sca=%s sky_tile=%s" % (
        detail["exposure_id"], detail["sca"], detail["sky_tile"]), file=out)
    print("  submitted/started/ended : %s / %s / %s" % (
        detail["submitted_at"], detail["started_at"], detail["ended_at"]),
        file=out)
    print("", file=out)
    print("  APPLICATION ACCOUNT", file=out)
    print("    rapid_outcome        : %s" % detail["rapid_outcome"], file=out)
    print("    product_disposition  : %s" % detail["product_disposition"],
          file=out)
    if detail["success_with_no_product"]:
        # THE ANOMALY THE CAMPAIGN NAMES "success+none on a product route":
        # the application reported success and recorded no product at all.
        # Printed as its own loud line rather than left to be inferred from
        # the two fields above — an operator scanning many rows should not
        # have to notice this combination themselves.
        print("    ** ANOMALY: success+none — the attempt succeeded but "
              "no product was recorded **", file=out)
    print("    application_intended_exit : %s" % (
        detail["application_intended_exit"]), file=out)
    print("    error_category       : %s" % detail["error_category"],
          file=out)
    print("    scheduler_state      : %s" % detail["scheduler_state"],
          file=out)
    print("", file=out)
    print("  TERMINAL RECORD", file=out)
    print("    key      : %s" % detail["terminal_record_key"], file=out)
    print("    sequence : %s" % detail["terminal_record_sequence"], file=out)
    print("    checksum : %s" % detail["terminal_record_checksum"], file=out)
    print("", file=out)
    print("  REGISTRATION", file=out)
    print("    registered_at              : %s" % detail["registered_at"],
          file=out)
    print("    registered_record_sequence : %s" % (
        detail["registered_record_sequence"]), file=out)
    print("    registration_outcome       : %s" % (
        detail["registration_outcome"]), file=out)
    print("", file=out)
    print("  STAGES (%d)" % len(detail["stages"]), file=out)
    for stage in detail["stages"]:
        print("    %-24s %-8s started=%s duration_ms=%s" % (
            stage["stage_name"], stage["outcome"], stage["started_at"],
            stage["duration_ms"]), file=out)
    print("", file=out)
    print("  WORK UNIT", file=out)
    if detail["work_unit_id"] is None:
        print("    (none — pre-intent-layer attempt or definition-FK guard "
              "held it back at submission time)", file=out)
    else:
        print("    work_unit_id      : %s" % detail["work_unit_id"], file=out)
        print("    job_type          : %s" % detail["work_unit_job_type"],
              file=out)
        print("    input_scope       : %s" % detail["work_unit_input_scope"],
              file=out)
        print("    state             : %s" % detail["work_unit_state"],
              file=out)
        print("    blocked_reason    : %s" % (
            detail["work_unit_blocked_reason"]), file=out)
        print("    operational_class : %s" % (
            detail["work_unit_operational_class"]), file=out)
    print("", file=out)
    print("  SUBMISSION (most recent for this run_id)", file=out)
    if detail["submission_id"] is None:
        print("    (none — DRAFT 044 not applied at submission time, or "
              "this run predates it)", file=out)
    else:
        print("    submission_id  : %s" % detail["submission_id"], file=out)
        print("    job_name       : %s" % detail["submission_job_name"],
              file=out)
        print("    job_queue      : %s" % detail["submission_job_queue"],
              file=out)
        print("    state          : %s" % detail["submission_state"],
              file=out)
        print("    array_size     : %s" % detail["submission_array_size"],
              file=out)
        print("    manifest_uri   : %s" % detail["submission_manifest_uri"],
              file=out)
    print("", file=out)
    print("  PROVENANCE (binding at submission time)", file=out)
    print("    job_definition_arn : %s" % (
        detail["binding_job_definition_arn"]), file=out)
    print("    job_definition_rev : %s" % (
        detail["binding_job_definition_rev"]), file=out)
    print("    image_digest       : %s" % detail["binding_image_digest"],
          file=out)
    print("    release_identity   : %s" % detail["binding_release_identity"],
          file=out)
    print("    manifest_checksum  : %s" % (
        detail["binding_manifest_checksum"]), file=out)
    return EXIT_OK


def _cmd_set_release(conn, args, out):
    from pipeline.operatorctl.contract import call_function
    key = args.idempotency_key or new_idempotency_key("release")
    expected = ({"current_release": args.expect_current}
                if args.expect_current is not None else None)
    import json as _json
    result = call_function(
        conn,
        "SELECT derived.set_admission_release(%s, %s, %s, %s, %s, %s)",
        (key, args.release_identity, args.reason,
         _json.dumps(expected) if expected is not None else None,
         not args.apply, args.policy_citation))
    print(render_plan("admission_release_set",
                      "admission_release_pointer:%s" % args.release_identity,
                      args.reason, key, result, args.apply), file=out)
    return EXIT_OK


#: The declared GC scope for this package: the products bucket alone. The
#: records, diagnostics, backup, logs, meta, build and simulation input
#: buckets are OUT OF SCOPE and no plan may name them (brief H, "Out of
#: scope"). Widening it is a later, separately argued change.
DEFAULT_GC_BUCKETS = ("roman-rapid-products",)


def _read_inventory_file(path):
    """Read a pinned inventory report into the page shape the reader takes.

    JSON lines, one object per line. Deliberately primitive: an S3 Inventory
    report reader, a recorded listing and a test double all present the same
    thing, which is what lets the DOUBLE REFUSE — return a partial page, a
    missing object or a row with no version — and a double that cannot fail
    proves nothing.

    A line carrying `{"truncated": true}` is honoured, so a report that KNOWS
    it is short can say so and be refused rather than read as complete.
    """
    import json as _json
    objects = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = _json.loads(line)
            if row.get("truncated"):
                return [{"objects": objects, "truncated": True}]
            objects.append(row)
    return [{"objects": objects}]


def _cmd_gc_compute(conn, args, out, manifest_reader=None):
    """Compute a plan.

    `manifest_reader` is injectable for the same reason `submission.py` injects
    its Batch and S3 clients: resolving a reference set is a DECISION, and a
    test of that decision should not need S3 credentials to make it. Production
    passes the real S3 reader.
    """
    from pipeline.operatorctl.gc import compute_plan, s3_manifest_reader
    key = args.idempotency_key or new_idempotency_key("gc-compute")
    buckets = tuple(args.bucket) if args.bucket else DEFAULT_GC_BUCKETS
    prefixes = tuple(args.prefix) if args.prefix else ()
    horizons = {"configured": args.horizon_seconds}

    result = compute_plan(
        conn, _executor(conn),
        inventory_source=_read_inventory_file(args.inventory),
        inventory_id=args.inventory_id,
        inventory_taken_at=args.inventory_taken_at,
        declared_buckets=buckets, declared_prefixes=prefixes,
        horizons=horizons, max_deletions=args.max_deletions,
        freshness_seconds=args.freshness, reason=args.reason,
        idempotency_key=key, actor=_session_user(conn),
        allowlist=tuple(args.allow_class) if args.allow_class else (),
        dry_run=not args.apply,
        horizon_provenance=args.horizon_provenance,
        manifest_reader=manifest_reader or s3_manifest_reader())
    if args.apply:
        conn.commit()
    print(render_plan("gc_compute_plan",
                      "gc:%s" % ",".join(buckets), args.reason, key, result,
                      args.apply), file=out)
    # THE REFUSAL IS PRINTED PLAINLY, not left to be inferred from a zero. An
    # operator seeing "0 candidates" deserves to know whether that is because
    # nothing qualified or because no horizon is configured.
    if result.get("refusal"):
        print("", file=out)
        print("  NOTE: %s" % result["refusal"], file=out)
    return EXIT_OK


def _cmd_gc_recompute(conn, args, out):
    from pipeline.gc.inventory import read_inventory
    from pipeline.gc.plans import GCPlanRepository
    key = args.idempotency_key or new_idempotency_key("gc-recompute")
    repo = GCPlanRepository(conn)
    plan = repo.plan(args.plan_id)
    if plan is None:
        print("rapidctl: no GC plan %s" % args.plan_id, file=sys.stderr)
        return EXIT_USAGE

    inventory = read_inventory(
        _read_inventory_file(args.inventory), inventory_id=args.inventory_id,
        taken_at=args.inventory_taken_at, freshness_seconds=args.freshness)
    # ONLY CANDIDATES PRESENT IN THE SECOND INVENTORY AND STILL UNREFERENCED
    # SURVIVE. Absence in either pass excludes.
    present = {(o.bucket, o.key, o.version_id) for o in inventory.objects}

    if not args.apply:
        result = {"action": "gc_recompute_plan", "plan_id": args.plan_id,
                  "state": plan.state, "second_inventory": args.inventory_id,
                  "objects_in_second_inventory": len(inventory.objects),
                  "dry_run": True, "rows_affected": 0}
    else:
        excluded = repo.recompute(args.plan_id, surviving_keys=present,
                                  inventory=inventory,
                                  recomputed_by=_session_user(conn))
        conn.commit()
        result = {"action": "gc_recompute_plan", "plan_id": args.plan_id,
                  "excluded_on_recompute": excluded, "dry_run": False,
                  "rows_affected": excluded}
    print(render_plan("gc_recompute_plan", "gc_plan:%s" % args.plan_id,
                      args.reason, key, result, args.apply), file=out)
    return EXIT_OK


def _cmd_gc_execute(conn, args, out):
    """Drive the executor — the production call site it previously lacked."""
    from pipeline.gc.execute import Executor
    from pipeline.gc.plans import GCPlanRepository
    from pipeline.operatorctl.gc import (record_execution,
                                         s3_manifest_reader,
                                         still_referenced_check)

    key = args.idempotency_key or new_idempotency_key("gc-execute")
    repo = GCPlanRepository(conn)
    plan = repo.plan(args.plan_id)
    if plan is None:
        print("rapidctl: no GC plan %s" % args.plan_id, file=sys.stderr)
        return EXIT_USAGE

    if not args.apply:
        # THE DRY RUN VERIFIES THE CHECKSUM AND COUNTS THE REAL WORK, and
        # deletes nothing. It is the plan the apply will act on, minus the
        # acting.
        repo.verify_checksum(args.plan_id)
        unresolved = repo.unresolved_items(args.plan_id)
        result = {"action": "gc_plan_execute", "plan_id": args.plan_id,
                  "state": plan.state,
                  "candidate_checksum": plan.candidate_checksum,
                  "unresolved_items": len(unresolved),
                  "dry_run": True, "rows_affected": 0}
        print(render_plan("gc_plan_execute", "gc_plan:%s" % args.plan_id,
                          args.reason, key, result, args.apply), file=out)
        return EXIT_OK

    import boto3
    executor = Executor(conn, _S3Versions(boto3.client("s3")),
                        actor=_session_user(conn))
    outcomes = executor.execute(
        args.plan_id, commit=conn.commit,
        still_referenced=still_referenced_check(
            _executor(conn), manifest_reader=s3_manifest_reader()))
    result = record_execution(conn, key, args.plan_id, args.reason, outcomes,
                              dry_run=False,
                              policy_citation=args.policy_citation)
    tally = {}
    for outcome in outcomes:
        tally[outcome.status] = tally.get(outcome.status, 0) + 1
    print(render_plan("gc_plan_execute", "gc_plan:%s" % args.plan_id,
                      args.reason, key, result, args.apply), file=out)
    print("", file=out)
    print("  items by outcome:", file=out)
    for status in sorted(tally):
        print("    %-22s %s" % (status, tally[status]), file=out)
    return EXIT_OK


class _S3Versions(object):
    """The narrow S3 surface `pipeline/gc/execute.py` needs, over boto3.

    Two methods, deliberately: `head_version` and `delete_version`. Keeping
    the surface this small is what lets the contract tier substitute a double
    that can REFUSE — return a missing object, fail a delete, report a version
    that moved after planning — which a wider wrapper around a boto3 client
    could not.
    """

    def __init__(self, client):
        self._client = client

    def head_version(self, bucket, key):
        try:
            head = self._client.head_object(Bucket=bucket, Key=key)
        except Exception as exc:                      # noqa: BLE001
            code = getattr(exc, "response", {}).get(
                "Error", {}).get("Code", "")
            if str(code) in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        return head.get("VersionId")

    def delete_version(self, bucket, key, version_id):
        """Delete the EXACT version, never the key.

        `VersionId` is mandatory here rather than optional: a key-only delete
        on a versioning-enabled bucket installs a delete marker over whatever
        is current, including a version written after the plan was computed.
        """
        self._client.delete_object(Bucket=bucket, Key=key,
                                   VersionId=version_id)
        return True


def _executor(conn):
    """The one-callable `execute(sql, params)` the intent layer takes."""
    def execute(statement, params=None):
        with conn.cursor() as cur:
            cur.execute(statement, params)
            if cur.description is not None:
                return cur.fetchall()
            return cur.rowcount
    return execute


def _session_user(conn):
    """The database's own `session_user` — never a CLI argument.

    An operator-supplied actor string would be an operator attesting to their
    own identity, which is exactly what an audit actor must not be.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT session_user")
        return cur.fetchone()[0]


def _cmd_gc_approve(conn, args, out):
    from pipeline.gc.plans import GCPlanRepository
    key = args.idempotency_key or new_idempotency_key("gc-approve")
    repo = GCPlanRepository(conn)
    plan = repo.plan(args.plan_id)
    if plan is None:
        print("rapidctl: no GC plan %s" % args.plan_id, file=sys.stderr)
        return EXIT_USAGE
    if not args.apply:
        result = {"action": "gc_approve_plan", "plan_id": args.plan_id,
                  "state": plan.state, "candidates": plan.candidate_count,
                  "candidate_checksum": plan.candidate_checksum,
                  "dry_run": True, "rows_affected": 0}
    else:
        # THE ACTOR IS THE DATABASE'S OWN `session_user`, not a CLI argument.
        # An operator-supplied actor string would be an operator attesting to
        # their own identity, which is exactly what an audit actor must not
        # be — every other mutation in this package takes it from
        # `session_user` for the same reason.
        with conn.cursor() as cur:
            cur.execute("SELECT session_user")
            actor = cur.fetchone()[0]
        approved = repo.approve(args.plan_id, approved_by=actor,
                                reason=args.reason)
        conn.commit()
        result = dict(approved, action="gc_approve_plan", dry_run=False,
                      rows_affected=1)
    print(render_plan("gc_approve_plan", "gc_plan:%s" % args.plan_id,
                      args.reason, key, result, args.apply), file=out)
    return EXIT_OK


def _cmd_gc_show(conn, args, out):
    from pipeline.gc.plans import GCPlanRepository
    repo = GCPlanRepository(conn)
    plan = repo.plan(args.plan_id)
    if plan is None:
        print("rapidctl: no GC plan %s" % args.plan_id, file=sys.stderr)
        return EXIT_USAGE
    print("GC PLAN %s" % plan.plan_id, file=out)
    print("  state              : %s" % plan.state, file=out)
    print("  candidates         : %s" % plan.candidate_count, file=out)
    print("  candidate checksum : %s" % plan.candidate_checksum, file=out)
    print("  horizon (seconds)  : %s" % (plan.horizon_seconds
                                         if plan.horizon_seconds is not None
                                         else "<unset — deletes nothing>"),
          file=out)
    print("  approved by        : %s" % (plan.approved_by or "<not approved>"),
          file=out)
    with conn.cursor() as cur:
        cur.execute("SELECT status, count(*) FROM gc_plan_items"
                    " WHERE plan_id = %s GROUP BY status ORDER BY status",
                    (args.plan_id,))
        rows = cur.fetchall()
    if rows:
        print("  items by status:", file=out)
        for status, count in rows:
            print("    %-22s %s" % (status, count), file=out)
    return EXIT_OK


def _preflight(conn):
    """Assert the deployed schema and this application satisfy rule 18.

    **`rapidctl` DID NOT PREFLIGHT AT ALL** before this — verified by grep at
    the branch point: zero matches for `schema_contract` or `preflight` across
    all six files of `pipeline/operatorctl/`. Four of the five entry points
    do (`pipeline/reconciler/main.py:168`, `pipeline/operator/service.py:698`,
    `pipeline/entrypoints/job.py:954`, and `pipeline/publisher/service.py:68`
    with its own narrower probe); this one did not.

    Scoped in narrowly and deliberately, per the brief: H adds an operator
    surface to `rapidctl` (the release pointer and the GC plan), and shipping
    a new operator surface behind the one entry point that does not preflight
    would be adding to the defect. This is NOT extended to the other four.

    Follows `reconciler/main.py:168-183`'s three-line pattern exactly.
    """
    from database.modules.utils.rapid_db_connect import ConnectionExecutor
    from pipeline.intent.schema_contract import verify_schema_contract

    verified = verify_schema_contract(ConnectionExecutor(conn).execute)

    # THE APPLICATION HALF (rule 18's "application/schema contract"). Kept
    # tolerant of a missing image digest here: `rapidctl` is an operator tool
    # run from a shell, not a payload container, so requiring the container's
    # own digest would refuse to start the tool for a fact it has no way to
    # know. The release identity IS required — that is the half that decides
    # what an admission gets stamped with.
    from pipeline.intent.application_contract import (
        ApplicationContractUnmet, verify_application_contract)
    try:
        verify_application_contract(
            execute=ConnectionExecutor(conn).execute,
            require_image_digest=False)
    except ApplicationContractUnmet:
        # RE-RAISED, NOT SWALLOWED — fail-closed is the requirement. Caught
        # only to make the failure legible as a contract failure rather than
        # an arbitrary exception from an import.
        raise
    return verified


def main(argv=None, out=None):
    """Parse, run one subcommand in one operator session, return an exit code.

    The session wraps the whole subcommand rather than each call inside
    it, because break-glass open/close and a mutation's audit row belong
    to one operator act and one assumed role.
    """
    out = out or sys.stdout
    args = build_parser().parse_args(argv)

    try:
        with operator_session() as conn:
            _preflight(conn)
            return args.func(conn, args, out)
    except OperatorSessionError as exc:
        print("rapidctl: %s" % exc, file=sys.stderr)
        return EXIT_NOT_AUTHORIZED
    except OperatorError as exc:
        # The contract's own refusals — expected-state mismatch, key
        # conflict. These are the tool working correctly, so they print
        # the refusal plainly and without a traceback.
        print("rapidctl: REFUSED — %s" % exc, file=sys.stderr)
        return exc.exit_code
    except SchemaContractUnmet as exc:
        # THE LEGIBLE PREFLIGHT FAILURE, not a raw undefined-function error
        # from whichever query happened to run first. It names every missing
        # migration and why this build needs each one.
        print("rapidctl: %s" % exc, file=sys.stderr)
        return EXIT_CONFIG
    except ApplicationContractUnmet as exc:
        # Rule 18's application half, failing closed.
        print("rapidctl: %s" % exc, file=sys.stderr)
        return EXIT_CONFIG
    except Exception as exc:                      # noqa: BLE001
        # Everything else, including DBUnavailable and DBCredentialError.
        # Typed at the source; presented here rather than as a traceback,
        # because an operator reading a stack trace is an operator the
        # tool has failed.
        category = getattr(exc, "error_category", None)
        if category in ("config_invalid", "db_unavailable"):
            print("rapidctl: %s" % exc, file=sys.stderr)
            return EXIT_CONFIG
        raise


if __name__ == "__main__":
    sys.exit(main())
