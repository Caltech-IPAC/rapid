"""
File:    load_definitions_cli.py

The deployment step that loads this release's workflow definitions.

`python -m pipeline.intent.load_definitions_cli --reason "<why>" --apply`

**THE MISSING DEPLOY HOOK** (rule 12). Migration 039 shipped
`derived.load_workflow_definition` with no caller; this is the caller, run at
deployment, before the services that depend on loaded definitions start. Its
companion is the startup completeness check in
`pipeline.intent.definitions`, which refuses to start a service whose enabled
streams are not loaded — so a deploy that forgets this step fails loudly at
service start rather than producing a pipeline that silently creates no work.

**DRY RUN IS THE DEFAULT**, matching the mutation contract migration 031
established and this repo's own `supersede_lost_evidence` CLI: `--apply` is
the explicit flag, and without it the run reports what it WOULD load. The
loader audits both paths (a rehearsal that leaves no trace would be an
invisible probe of what the API would do), so a dry run is itself recorded.

**IDEMPOTENT BY CONSTRUCTION.** Re-running this is a no-op success for every
definition whose bytes have not changed; an edited file under an already-loaded
version RAISES, because versions are immutable — the fix for a changed
definition is a new version, not a re-load. Both properties live in the SQL
function, not here.

**RUNS AS `rapid_operator`.** Migration 039 grants EXECUTE on the loader to
that role only, deliberately not to either service role: loading a reviewed
definitions file is a human review-and-load action. The connection below is
opened with the operator credential for that reason, and a permission error
here means the step is being run as the wrong role, not that the grant is
missing.
"""

import argparse
import json
import logging
import sys

logger = logging.getLogger(__name__)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="load-workflow-definitions",
        description="Load this release's cdf/workflow/*-v1.toml definitions "
                    "through derived.load_workflow_definition (migration "
                    "039). Dry run unless --apply.")
    parser.add_argument("--reason", required=True,
                        help="why this load is happening; mandatory and "
                             "unvalidated per the mutation contract")
    parser.add_argument("--apply", action="store_true",
                        help="write; without it the run only reports")
    parser.add_argument("--dispatcher", default=None,
                        help="optional attribution recorded in the audit row")
    parser.add_argument("--policy-citation", default=None,
                        help="optional policy citation for the audit row")
    parser.add_argument("--software-root", default=None,
                        help="override the definitions root (tests/rehearsal)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    from database.modules.utils.rapid_db_connect import (ConnectionExecutor,
                                                         connection)
    from pipeline.intent.definitions import load_definitions

    # ONE TRANSACTION FOR THE WHOLE SET. Definitions are release content
    # loaded as a set, and a partially-loaded release is the state the startup
    # completeness check exists to reject — so a failure on the seventh file
    # rolls back the first six rather than leaving a deployment half-specified
    # for an operator to reconcile by hand. The loader's own audit rows are
    # inside this transaction too (migration 031's pattern), so an aborted
    # load leaves no audit claiming it happened.
    with connection("rapid-load-definitions", lane="transaction") as conn:
        results = load_definitions(
            ConnectionExecutor(conn).execute,
            reason=args.reason,
            dry_run=not args.apply,
            dispatcher=args.dispatcher,
            policy_citation=args.policy_citation,
            software_root=args.software_root)
        if args.apply:
            conn.commit()
        else:
            # A dry run's audit rows are deliberately NOT kept: migration
            # 030's CHECK forbids a dry-run audit claiming rows_affected > 0,
            # and this CLI's dry run exists to report to a human, not to
            # accumulate rehearsal rows. Rolling back also guarantees a dry
            # run cannot load anything by accident.
            conn.rollback()

    print(json.dumps({"apply": args.apply, "definitions": len(results),
                      "results": results}, indent=2, default=str))

    # A load that changed nothing is a success, not a warning: idempotence is
    # the contract. The exit code says only whether the step ran.
    return 0


if __name__ == "__main__":
    sys.exit(main())
