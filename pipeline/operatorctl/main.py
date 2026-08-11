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
