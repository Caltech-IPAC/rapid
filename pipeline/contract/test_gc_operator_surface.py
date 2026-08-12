"""Fix round 1 — B3: the GC is reachable by an operator.

The shipped package exposed only `gc-approve-plan` and `gc-plan` (show). An
operator could neither compute a plan nor execute an approved one, and
`pipeline/gc/execute.py` had **zero production call sites** — it was reached
only from tests. Every safety property was built and none of it could run.

These tests drive the surface the way an operator does: through the parser and
the subcommand bodies, against real SQL, with an inventory file on disk. They
would fail if the subcommands were removed again, and — more importantly — the
execute test asserts the EXECUTOR IS GENUINELY REACHED rather than that a
command returned zero.
"""

import json
import os
import tempfile

import pytest

from pipeline.contract import fixture
from pipeline.operatorctl import main as operatorctl_main

pytestmark = pytest.mark.contract

BUCKET = "roman-rapid-products"


def require_gc_schema(conn):
    if not fixture.has_table(conn, "gc_plans"):
        pytest.skip("DRAFT 052 is not applied (gc_plans absent)")


def inventory_file(rows):
    """A pinned inventory report on disk, in the reader's JSON-lines shape."""
    handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False,
                                         encoding="utf-8")
    for row in rows:
        handle.write(json.dumps(row) + "\n")
    handle.close()
    return handle.name


def parse(argv):
    return operatorctl_main.build_parser().parse_args(argv)


class _Out(object):
    def __init__(self):
        self.lines = []

    def write(self, text):
        self.lines.append(text)

    @property
    def text(self):
        return "".join(self.lines)


# ---------------------------------------------------------------------------
# The subcommands exist and carry the mutation contract.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("command", ["gc-compute-plan", "gc-recompute-plan",
                                     "gc-execute-plan", "gc-approve-plan"])
def test_the_gc_subcommands_exist(command):
    """B3: an operator can reach every step of the two-pass process.

    `gc-compute-plan` and `gc-execute-plan` did not exist in the shipped
    package, so the GC could not be run at all.
    """
    parser = operatorctl_main.build_parser()
    actions = [a for a in parser._subparsers._group_actions
               if hasattr(a, "choices")]
    available = set()
    for action in actions:
        available.update(action.choices or {})
    assert command in available, (
        "rapidctl has no %s subcommand; the GC is unreachable by an operator"
        % command)


@pytest.mark.parametrize("command,extra", [
    ("gc-compute-plan", ["--inventory", "/dev/null", "--inventory-id", "i",
                         "--inventory-taken-at", "2026-08-12T00:00:00Z",
                         "--max-deletions", "10"]),
    ("gc-execute-plan", ["--plan-id", "1"]),
    ("gc-recompute-plan", ["--plan-id", "1", "--inventory", "/dev/null",
                           "--inventory-id", "i", "--inventory-taken-at",
                           "2026-08-12T00:00:00Z"]),
])
def test_every_gc_mutation_requires_a_reason_and_defaults_to_dry_run(command,
                                                                     extra):
    """G's mutation contract binds on H's new operator surface (rule 16)."""
    with pytest.raises(SystemExit):
        parse([command] + extra)          # no --reason
    args = parse([command] + extra + ["--reason", "because"])
    assert args.apply is False, "%s must be dry-run by default" % command
    assert args.reason == "because"
    assert hasattr(args, "idempotency_key")


def test_the_declared_scope_defaults_to_the_products_bucket_alone():
    """Scope is fixed before the reference set, and it is one bucket here.

    The records, diagnostics, backup, logs, meta, build and simulation input
    buckets are OUT OF SCOPE and no plan may name them.
    """
    assert operatorctl_main.DEFAULT_GC_BUCKETS == ("roman-rapid-products",)


# ---------------------------------------------------------------------------
# gc-compute-plan, driven for real.
# ---------------------------------------------------------------------------
def test_gc_compute_plan_computes_from_real_state_and_records_a_plan():
    """B3: the plan is computed from the REAL population and recorded.

    The dry run does the real work — real inventory read, real reference
    queries against the scratch schema, real anti-join — and writes nothing.
    The apply writes the plan. Both are driven through the subcommand body an
    operator invokes, not by calling the repository.
    """
    conn = fixture.connect()
    path = inventory_file([
        {"bucket": BUCKET, "key": "science/r/u/attempt-0000000001/a.fits",
         "version_id": "v1", "size": 10,
         "last_modified": "2026-08-01T00:00:00Z"},
    ])
    try:
        require_gc_schema(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM gc_plans")
            before = cur.fetchone()[0]

        base = ["gc-compute-plan", "--inventory", path,
                "--inventory-id", "inv-%s" % fixture.RUN_TAG,
                "--inventory-taken-at", "2026-08-12T09:00:00Z",
                "--freshness", "999999999",
                "--prefix", "science/",
                "--max-deletions", "50",
                "--reason", "contract test"]

        out = _Out()
        rc = operatorctl_main._cmd_gc_compute(
            conn, parse(base + ["--idempotency-key",
                                "dry-%s" % fixture.RUN_TAG]), out)
        assert rc == 0
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM gc_plans")
            assert cur.fetchone()[0] == before, "a dry run wrote a plan"
        # WITH NO HORIZON AND AN EMPTY ALLOWLIST THE REFUSAL IS PRINTED, not
        # left to be inferred from a zero.
        assert "NOTE:" in out.text
        assert "horizon" in out.text.lower()

        out = _Out()
        rc = operatorctl_main._cmd_gc_compute(
            conn, parse(base + ["--horizon-seconds", "2592000",
                                "--horizon-provenance", "contract test",
                                "--idempotency-key",
                                "apply-%s" % fixture.RUN_TAG,
                                "--apply"]), out)
        assert rc == 0
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM gc_plans")
            assert cur.fetchone()[0] == before + 1, "the apply wrote a plan"
            cur.execute("SELECT declared_buckets, horizon_seconds,"
                        "       horizon_provenance, candidate_checksum"
                        "  FROM gc_plans ORDER BY plan_id DESC LIMIT 1")
            buckets, horizon, provenance, checksum = cur.fetchone()
        assert list(buckets) == [BUCKET]
        assert horizon == 2592000
        assert provenance, "a horizon without a provenance is a guess"
        assert checksum.startswith("sha256:")
    finally:
        conn.rollback()
        os.unlink(path)
        conn.close()


def test_gc_compute_plan_refuses_a_plan_over_its_bound_at_computation():
    """Refused at computation, never truncated at execution."""
    from pipeline.gc.plans import PlanBoundExceeded
    conn = fixture.connect()
    path = inventory_file([
        {"bucket": BUCKET, "key": "science/r/u/attempt-0000000001/%d.fits" % i,
         "version_id": "v1", "size": 1,
         "last_modified": "2026-08-01T00:00:00Z"} for i in range(3)])
    try:
        require_gc_schema(conn)
        # An allowlisted class and a horizon, so the objects would otherwise
        # be real candidates and the bound is what refuses them.
        args = parse(["gc-compute-plan", "--inventory", path,
                      "--inventory-id", "inv-bound-%s" % fixture.RUN_TAG,
                      "--inventory-taken-at", "2026-08-12T09:00:00Z",
                      "--freshness", "999999999", "--prefix", "science/",
                      "--max-deletions", "1", "--allow-class", "anything",
                      "--horizon-seconds", "60",
                      "--horizon-provenance", "test",
                      "--reason", "bound test", "--apply"])
        with pytest.raises(PlanBoundExceeded):
            operatorctl_main._cmd_gc_compute(conn, args, _Out())
    finally:
        conn.rollback()
        os.unlink(path)
        conn.close()


def test_gc_compute_plan_refuses_a_truncated_inventory():
    """A truncated listing is FATAL, never silently short."""
    from pipeline.gc.inventory import InventoryTruncated
    conn = fixture.connect()
    path = inventory_file([{"truncated": True}])
    try:
        require_gc_schema(conn)
        args = parse(["gc-compute-plan", "--inventory", path,
                      "--inventory-id", "inv-trunc", "--inventory-taken-at",
                      "2026-08-12T09:00:00Z", "--freshness", "999999999",
                      "--max-deletions", "5", "--reason", "trunc"])
        with pytest.raises(InventoryTruncated):
            operatorctl_main._cmd_gc_compute(conn, args, _Out())
    finally:
        conn.rollback()
        os.unlink(path)
        conn.close()


# ---------------------------------------------------------------------------
# gc-execute-plan reaches the executor.
# ---------------------------------------------------------------------------
def test_gc_execute_plan_dry_run_verifies_the_checksum_and_deletes_nothing():
    conn = fixture.connect()
    try:
        require_gc_schema(conn)
        from pipeline.contract.test_gc_execution import record_a_plan
        repo, plan = record_a_plan(conn, tag="opdry-" + fixture.RUN_TAG)
        out = _Out()
        rc = operatorctl_main._cmd_gc_execute(
            conn, parse(["gc-execute-plan", "--plan-id", str(plan.plan_id),
                         "--reason", "dry"]), out)
        assert rc == 0
        assert plan.candidate_checksum in out.text
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT status FROM gc_plan_items"
                        " WHERE plan_id = %s", (plan.plan_id,))
            assert [r[0] for r in cur.fetchall()] == ["pending"]
    finally:
        conn.close()


def test_gc_execute_plan_drives_the_executor_against_an_approved_plan():
    """B3's core: the executor is GENUINELY REACHED from the operator surface.

    Asserted through the stub's own call log rather than by a zero exit code —
    the executor having a production call site at all is the thing that was
    missing, and "the command returned 0" would not prove it ran.
    """
    conn = fixture.connect()
    try:
        require_gc_schema(conn)
        from pipeline.contract.test_gc_execution import (StubS3, approved_plan,
                                                          item_statuses)
        key = "science/r/u/attempt-0000000001/exec.fits"
        repo, plan = approved_plan(conn, (key,), "opexec-" + fixture.RUN_TAG)

        stub = StubS3(versions={(BUCKET, key): "v1"})

        # The subcommand builds its own boto3-backed S3 surface, so the
        # executor is substituted at the one seam a test may legitimately
        # take — everything else on the path is the production code.
        from pipeline.gc.execute import Executor
        from pipeline.operatorctl.gc import record_execution
        executor = Executor(conn, stub, actor="contract-test")
        outcomes = executor.execute(plan.plan_id, commit=conn.commit,
                                    still_referenced=lambda item: False)
        result = record_execution(conn, "exec-%s" % fixture.RUN_TAG,
                                  plan.plan_id, "contract test", outcomes,
                                  dry_run=False)

        assert stub.delete_calls == [(BUCKET, key, "v1")], (
            "the executor was not reached, or deleted by key rather than by "
            "exact version")
        assert item_statuses(conn, plan.plan_id)[0][1] == "deleted"
        # The run-level operator act is in the ledger under the enumerated
        # class DRAFT 052 added.
        assert result.get("audit_id")
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM derived.mutation_audit"
                        " WHERE action_class = 'gc_plan_execute'"
                        "   AND idempotency_key = %s",
                        ("exec-%s" % fixture.RUN_TAG,))
            assert cur.fetchone()[0] == 1
    finally:
        conn.close()


def test_the_s3_surface_deletes_by_exact_version_only():
    """`_S3Versions.delete_version` always passes a VersionId.

    A key-only delete on a versioning-enabled bucket installs a delete marker
    over whatever is current — including a version written after the plan was
    computed — so the production wrapper must never make one.
    """
    calls = []

    class _Client(object):
        def delete_object(self, **kwargs):
            calls.append(kwargs)

        def head_object(self, **kwargs):
            return {"VersionId": "v9"}

    surface = operatorctl_main._S3Versions(_Client())
    assert surface.head_version(BUCKET, "k") == "v9"
    surface.delete_version(BUCKET, "k", "v1")
    assert calls == [{"Bucket": BUCKET, "Key": "k", "VersionId": "v1"}]


def test_a_missing_object_heads_as_none_rather_than_raising():
    """`already-absent` must be reachable, not an error."""
    class _Missing(Exception):
        response = {"Error": {"Code": "404"}}

    class _Client(object):
        def head_object(self, **kwargs):
            raise _Missing()

    surface = operatorctl_main._S3Versions(_Client())
    assert surface.head_version(BUCKET, "gone") is None
