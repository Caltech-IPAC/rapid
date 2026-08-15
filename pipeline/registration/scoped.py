"""Scoped registration: register an EXPLICIT, BOUNDED set of attempts.

**WHY THIS EXISTS.** `pipeline.registration.consumer.candidates()`'s own
query has no `LIMIT`, no run_id filter, no campaign scoping — it selects
EVERY reconciled attempt the database holds. The only two callers of
`register_batch` before this module were `pipeline.operator.operator.
Operator._register` (reached only when a class disposition is `'run'`,
which also stands up live submitters that immediately gather-and-submit —
not something to flip on to complete one unit) and `pipeline.entrypoints.
job.dispatch_registration` (the `JOB_TYPE_REGISTRATION` Batch route, which
this codebase's own docstrings call dormant, with nothing producing it a
manifest). So the only way to register even ONE attempt through either path
was an unscoped sweep over the entire candidate set — thousands-wide on a
live database, and growing every pass.

This module is the third way: a standalone script that runs registration
over a caller-named scope and NOTHING else, without flipping any class
disposition and without touching any candidate outside that scope. It needs
no `Operator`, no class disposition, and no Batch job — it opens its own
database connection, resolves the scope through `pipeline.registration.
consumer.candidates()`'s new `run_id_prefix`/`attempt_ids` parameters, and
calls `register_batch` over exactly those rows.

**DRY RUN IS THE DEFAULT.** `main()`'s `--execute` flag is required to write
anything; every invocation without it resolves the scope and reports exactly
which attempt_ids WOULD be registered, without a `register` callback and
without touching the database beyond the read-only candidate query. This
mirrors the read-mostly, opt-in-to-mutate posture the codebase already uses
for the registration job route's own rehearsal mode (`dispatch_registration`:
"a rehearsal's candidates count into `would_register`", never `registered`).

**AN UNBOUNDED SCOPE IS REFUSED**, here, even though `candidates()` itself
will happily run unscoped for its two existing callers. Neither
`--run-id-prefix` nor `--attempt-id` given is refused with a clear message
rather than silently falling back to the same thousands-wide sweep this
module exists to avoid — that unscoped sweep remains reachable only through
the existing `Operator`/`dispatch_registration` paths, unchanged by anything
here.

Usage (inside the pinned image, on rapid-admin — this needs the pipeline
parameter tree and a live database, both read from the environment exactly
as `pipeline.entrypoints.job.dispatch_registration` reads them):

    python3.11 -m pipeline.registration.scoped --run-id-prefix w9-ramp-science-18-...
    python3.11 -m pipeline.registration.scoped --attempt-id 12345 --attempt-id 12346
    python3.11 -m pipeline.registration.scoped --run-id-prefix ... --execute

Prints one JSON line, `SCOPED-REGISTRATION-SUMMARY {...}`, as its last line
of stdout — the same one-line-JSON convention `pipeline.test.live_w9_ramp`
uses for `W9-RAMP-SUMMARY`. Exits `consumer.EXIT_FAILURES` (65) if any
attempt in scope failed to register, `consumer.EXIT_OK` (0) otherwise
(including a dry run, and including an empty resolved scope).
"""

import argparse
import json
import logging
import sys

from observability.registration import RegistrationDecision, decide_all
from pipeline.registration.consumer import (
    EXIT_FAILURES,
    EXIT_OK,
    RegistrationRun,
    _Row,
    candidates,
    register_batch,
)

logger = logging.getLogger("rapid.registration.scoped")

#: The tree key the records bucket lives under — the same constant
#: `pipeline.entrypoints.job.PARAM_RECORDS_BUCKET` names, duplicated here
#: (as a plain string, not an import) because importing `pipeline.
#: entrypoints.job` for one constant would pull in that module's own
#: `argparse`-based CLI and its full stage-sequence import graph for a
#: script that runs none of it. `test_scoped.py` asserts the two strings
#: match, so a rename of the tree key on one side without the other fails
#: loudly in the stub tier rather than silently in production.
PARAM_RECORDS_BUCKET = "s3/records-bucket"


class UnboundedScopeError(ValueError):
    """Raised when neither `run_id_prefix` nor `attempt_ids` is given.

    The whole reason this module exists is to avoid the unscoped,
    thousands-wide sweep `candidates()` runs for its two existing callers —
    so, unlike `candidates()` itself, this entrypoint refuses to run with no
    scope at all rather than silently falling back to that sweep.
    """


def resolve_scope(conn, run_id_prefix=None, attempt_ids=None):
    """The candidate rows for this scope. Raises `UnboundedScopeError` if
    neither `run_id_prefix` nor `attempt_ids` narrows the query.

    A thin wrapper over `consumer.candidates()` — the refusal is this
    function's only addition; the query itself, and everything about how it
    is reconciled-state-gated, is exactly what `candidates()` already does
    for its two production callers.
    """
    if run_id_prefix is None and attempt_ids is None:
        raise UnboundedScopeError(
            "scoped registration refuses to run with no scope: pass "
            "--run-id-prefix and/or --attempt-id. An unscoped call would "
            "candidate-select every reconciled attempt in the database — "
            "that sweep is intentionally only reachable through the "
            "existing Operator/registration-job-route paths, not this "
            "entrypoint.")
    return candidates(conn, run_id_prefix=run_id_prefix,
                      attempt_ids=attempt_ids)


def registrar_for_scope(records_bucket, conn, s3_client=None):
    """The same product-registration callback `pipeline.entrypoints.job.
    registrar_for` builds, over a plain `records_bucket` string rather than
    a `StageContext` — this script has no stage context, only the pipeline
    parameter tree's own `s3/records-bucket` value (`main()` reads it via
    `fetch_parameters()`, the same tree `registrar_for` reads through
    `context.parameter(...)`).

    See `pipeline.entrypoints.job.registrar_for` for the full reasoning this
    mirrors verbatim: the store is the records store because the registrar
    fetches and validates each attempt's record itself, `conn` is required
    (not a legacy connectionless branch) because the product rows and the
    watermark have to be one transaction, and the RAPIDDB handle is built
    lazily inside the returned closure so a scope with no candidates costs
    no cursor and no round trips.

    Returns `(register, store)`. THE STORE IS RETURNED, NOT DISCARDED, because
    `register_batch` needs the same records store a second time — to bind the
    GC fence around each attempt before the registrar touches it. This function
    already builds exactly that store; handing back the one object keeps the
    registrar and the fence pointed at the same bucket by construction, rather
    than making the caller build a second one that could disagree.
    """
    import boto3

    from database.modules.utils import rapid_db
    from pipeline.registration.products import registrar
    from pipeline.repositories.products import ProductRepository
    from pipeline.runtime.boundaries import S3ObjectStore

    store = S3ObjectStore(records_bucket,
                          client=s3_client or boto3.client("s3"))
    register = registrar(lambda: rapid_db.RAPIDDB.borrowing(conn), store,
                         identity_repository=ProductRepository(conn))
    return register, store


def _dry_run_verdicts(rows):
    """Classify `rows` with `decide_all` alone — NO call into `register_batch`,
    even with its own `dry_run=True`.

    **WHY NOT JUST `register_batch(conn, rows, dry_run=True)`.** That would
    be the obvious mirror of the production job route's own rehearsal mode
    (`dispatch_registration`'s `dry_run=register is None`), and for the
    REGISTER-decision branch it would indeed write nothing — but `register_
    batch`'s SKIP-decision branch is unconditional: it opens `_transaction
    (conn)`, acquires the per-attempt lease, and calls `mark_consumed` (and,
    for several dispositions, `_apply_skip_disposition`, which can transition
    a work unit) regardless of `dry_run`. That is existing, deliberate
    behavior in `register_batch` — SKIP's own watermark write is not gated
    on a `register` callback the way REGISTER's is, because a SKIP verdict
    was never going to call one — and this module's brief is explicit that
    `register_batch`'s per-attempt semantics are not to be altered to make a
    scoped dry run possible.

    So the only way to make "a dry run must resolve the scope and report
    exactly which attempt_ids WOULD be registered, without writing anything"
    true for EVERY row a scope can contain — not just the ones that happen
    to land on REGISTER — is to never call `register_batch` at all for a
    dry run. `decide_all` (`observability.registration`) is the pure,
    read-only classification `register_batch` itself calls first, before
    any transaction opens; running it directly here is a read over data
    already in hand, with no database statement issued at all.
    """
    return decide_all(_Row(row) for row in rows)


def run_scoped_registration(conn, run_id_prefix=None, attempt_ids=None,
                            dry_run=True, records_bucket=None,
                            s3_client=None):
    """Resolve the scope and run (or rehearse) registration over it.

    Returns `(run, rows)` — a `RegistrationRun` (real, from `register_batch`,
    when `dry_run=False`; a synthetic one built from `decide_all`'s pure
    classification when `dry_run=True` — see `_dry_run_verdicts` for why a
    dry run never reaches `register_batch` at all) and the resolved
    candidate rows, so the caller can report exactly which attempt_ids were
    in scope regardless of what became of them.

    `dry_run=True` is the default (the module's own contract). A dry run
    touches the database only through `resolve_scope`'s own read-only
    `candidates()` query (which itself ends in `conn.rollback()`) — nothing
    past that point issues a single further statement.
    """
    rows = resolve_scope(conn, run_id_prefix=run_id_prefix,
                         attempt_ids=attempt_ids)

    if dry_run:
        verdicts = _dry_run_verdicts(rows)
        run = RegistrationRun()
        for verdict in verdicts:
            if verdict.decision is RegistrationDecision.REGISTER:
                run.would_register += 1
            elif verdict.decision is RegistrationDecision.DEFER:
                run.deferred += 1
            else:
                run.skipped += 1
        return run, rows

    if records_bucket is None:
        raise ValueError(
            "records_bucket is required to actually register (dry_run="
            "False): the registrar reads each attempt's terminal record "
            "from the records store, and there is no default bucket to "
            "guess.")
    # THE STORE IS PASSED, AND THAT IS THE FENCE. `register_batch` binds the
    # GC fence per attempt only when it has a records store (consumer.py's
    # per-attempt loop falls back to `nullcontext()` when `store is None`), so
    # the `store=None` this used to pass left every scoped `--execute` run
    # racing GC deletes — the exact race `_bind_fence` exists to prevent. The
    # operator and job-dispatch paths were fenced on 2026-08-14; this third
    # entrypoint was missed, while holding the bucket it needed all along.
    register, store = registrar_for_scope(records_bucket, conn,
                                          s3_client=s3_client)
    run = register_batch(conn, rows, register=register, store=store)
    return run, rows


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="python3 -m pipeline.registration.scoped",
        description="Register an explicit, bounded set of reconciled "
                    "attempts — never the full unscoped candidate set.")
    parser.add_argument(
        "--run-id-prefix", default=None,
        help="Match run_id LIKE '<prefix>%%' (handles submit_gathered's "
            "-0/-1/... split-batch suffixes).")
    parser.add_argument(
        "--attempt-id", action="append", type=int, default=[], dest="attempt_ids",
        help="An explicit attempt_id to include. Repeatable.")
    parser.add_argument(
        "--execute", action="store_true",
        help="Actually register (writes to the database). Without this "
            "flag, the scope is resolved and reported but nothing is "
            "written — the default, safe mode.")
    return parser.parse_args(argv)


def main(argv=None):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    args = _parse_args(sys.argv[1:] if argv is None else argv)
    run_id_prefix = args.run_id_prefix
    attempt_ids = args.attempt_ids or None
    dry_run = not args.execute

    from database.modules.utils.rapid_db_connect import connection
    from submission.startup import fetch_parameters

    try:
        parameters = fetch_parameters()
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        print(f"!! could not read the pipeline parameter tree: {exc}",
              file=sys.stderr)
        return EXIT_FAILURES
    records_bucket = parameters.get(PARAM_RECORDS_BUCKET)

    lane = "transaction"
    with connection("rapid-scoped-registration", lane=lane) as conn:
        try:
            run, rows = run_scoped_registration(
                conn, run_id_prefix=run_id_prefix, attempt_ids=attempt_ids,
                dry_run=dry_run, records_bucket=records_bucket)
        except UnboundedScopeError as exc:
            print(f"!! {exc}", file=sys.stderr)
            return EXIT_FAILURES

    summary = {
        "mode": "dry-run" if dry_run else "execute",
        "run_id_prefix": run_id_prefix,
        "attempt_ids_requested": attempt_ids,
        "scope_size": len(rows),
        "attempt_ids_in_scope": [row["attempt_id"] for row in rows],
        "counts": run.as_dict(),
    }
    print("SCOPED-REGISTRATION-SUMMARY " + json.dumps(summary))
    if dry_run:
        return EXIT_OK
    return EXIT_FAILURES if run.failed else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
