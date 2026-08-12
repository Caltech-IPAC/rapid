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


def _stub_reader(uri):
    """A manifest reader that needs no S3 credentials.

    The scratch database carries `submissions` rows left by other suites,
    whose `manifest_uri` values point at buckets this host cannot read — so
    the production reader raises `AccessDenied` and, correctly, REFUSES THE
    PLAN. That refusal is right and is asserted in
    `test_gc_manifest_expansion.py`; here the subject is the operator surface,
    so the reader is injected at the same seam `submission.py` injects its
    Batch and S3 clients, for the same stated reason.
    """
    return {"units": []}


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
                                "dry-%s" % fixture.RUN_TAG]), out,
            manifest_reader=_stub_reader)
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
                                "--apply"]), out,
            manifest_reader=_stub_reader)
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
    """Refused at computation, never truncated at execution.

    **THE BOUND IS TESTED AT `record_plan`, NOT THROUGH THE ANTI-JOIN**, and
    the reason is worth stating: on this scratch database no inventory key can
    be canonically attributed (no attempt reconstructs to a
    `science/r/u/attempt-...` prefix), so every object is retained as
    unattributable and the candidate list is EMPTY — a plan of zero never
    exceeds a bound of one. A first revision of this test drove the CLI and
    asserted a refusal that could not fire, which would have read as "the
    bound is enforced" while proving nothing.

    So the candidates are constructed directly for this one assertion. That is
    the honest shape: the bound is a property of `record_plan`, and this
    exercises exactly it, with the CLI-level path covered by the test above.
    """
    from pipeline.gc.inventory import InventoryObject
    from pipeline.gc.plans import GCPlanRepository, PlanBoundExceeded
    from pipeline.gc.references import Candidate
    import datetime

    conn = fixture.connect()
    try:
        require_gc_schema(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM gc_plans")
            before = cur.fetchone()[0]

        moment = datetime.datetime(2026, 8, 12, tzinfo=datetime.timezone.utc)
        objects = [InventoryObject(bucket=BUCKET,
                                   key="science/bound/%d.fits" % i,
                                   version_id="v1", size=1,
                                   last_modified=moment) for i in range(3)]
        candidates = [Candidate(obj=o, object_class="difference_image",
                                attempt_id=None,
                                canonical_prefix="science/bound")
                      for o in objects]

        class _Inventory(object):
            inventory_id = "inv-bound-%s" % fixture.RUN_TAG
            taken_at = moment
            complete = True

        _Inventory.objects = tuple(objects)

        repo = GCPlanRepository(conn)
        with pytest.raises(PlanBoundExceeded) as caught:
            repo.record_plan(
                candidates=candidates, retained_counts={},
                inventory=_Inventory(), declared_buckets=(BUCKET,),
                declared_prefixes=("science/",), horizon_seconds=60,
                horizon_provenance="test", max_deletions=1, allowlist=(),
                reason="bound test",
                idempotency_key="bound-%s" % fixture.RUN_TAG,
                computed_by="contract-test")
        assert "REFUSED AT COMPUTATION" in str(caught.value)
        conn.rollback()

        # AND NO PARTIAL PLAN WAS WRITTEN — the bound is checked before any
        # row, so a refused plan leaves nothing behind.
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM gc_plans")
            assert cur.fetchone()[0] == before
    finally:
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
            operatorctl_main._cmd_gc_compute(conn, args, _Out(),
                                             manifest_reader=_stub_reader)
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
        # The dry run VERIFIED the checksum — `verify_checksum` re-derives it
        # from the recorded items and raises on a mismatch, so reaching here
        # is the assertion. (`render_plan` prints the contract's fields, not
        # the plan's internals, so the digest itself is not in the output.)
        assert "DRY RUN" in out.text
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT status FROM gc_plan_items"
                        " WHERE plan_id = %s", (plan.plan_id,))
            assert [r[0] for r in cur.fetchall()] == ["pending"]
    finally:
        conn.close()


def test_the_executor_deletes_by_exact_version_and_audits_the_run():
    """The EXECUTOR's own behaviour against a stub — nothing about the CLI.

    **THIS TEST DOES NOT ENTER `_cmd_gc_execute` AND DOES NOT PROVE THE CLI
    PATH.** An earlier revision was headed "the executor is GENUINELY REACHED
    from the operator surface" and claimed that "everything else on the path
    is the production code". Both were false as written: it constructs
    `Executor` directly, so it substitutes the executor BY NEVER ENTERING THE
    CODE THAT CONSTRUCTS IT, and a typo in the apply branch would not have
    failed it. Fix round 2 corrected the claim rather than deleting the test,
    because what it actually checks is worth checking.

    What it proves: given an approved plan, the executor deletes by exact
    `VersionId`, marks the item `deleted`, and `record_execution` writes one
    run-level audit row under the enumerated `gc_plan_execute` class.

    The CLI path — `build_parser()` → `args.func` → the apply branch at
    `main.py:612-618` — is proven by
    `test_gc_execute_plan_apply_drives_the_executor_through_the_cli` below,
    which is the test the review asked for and the one that fails if that
    branch breaks.
    """
    conn = fixture.connect()
    try:
        require_gc_schema(conn)
        from pipeline.contract.test_gc_execution import (StubS3, approved_plan,
                                                          item_statuses)
        key = "science/r/u/attempt-0000000001/exec.fits"
        repo, plan = approved_plan(conn, (key,), "opexec-" + fixture.RUN_TAG)

        stub = StubS3(versions={(BUCKET, key): "v1"})

        from pipeline.gc.execute import Executor
        from pipeline.operatorctl.gc import record_execution
        executor = Executor(conn, stub, actor="contract-test")
        outcomes = executor.execute(plan.plan_id, commit=conn.commit,
                                    still_referenced=lambda item: False)
        result = record_execution(conn, "exec-%s" % fixture.RUN_TAG,
                                  plan.plan_id, "contract test", outcomes,
                                  dry_run=False)

        assert stub.delete_calls == [(BUCKET, key, "v1")], (
            "the executor deleted by key rather than by exact version")
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


def test_gc_execute_plan_apply_drives_the_executor_through_the_cli(
        monkeypatch):
    """B3's core, properly: `gc-execute-plan --apply` THROUGH THE REAL CLI.

    **THIS IS THE TEST THE APPLY BRANCH NEVER HAD**, and its absence is the
    one item fix round 1 left open. Every other execution test either drives
    the dry-run branch or builds `Executor` itself, so a typo inside
    `main.py:612-618` — or a dropped `set_defaults(func=...)` mapping — would
    have passed a fully green suite. That is fix round 1's own stated failure
    mode reproduced one layer inside the fix, which is why it is closed here.

    THE PATH EXERCISED IS THE REAL ONE, end to end:

      * `build_parser().parse_args([...])` — so the subcommand must exist,
        accept `--apply`, and carry the mutation-contract arguments;
      * `args.func` — so the `set_defaults(func=_cmd_gc_execute)` mapping must
        be present and point at the right body;
      * the apply branch itself — so `_S3Versions(...)`, `_session_user(conn)`,
        `still_referenced_check(...)` and `record_execution(...)` must all be
        constructed and called as written.

    **BOTO3 IS SUBSTITUTED AT THE BOUNDARY, NOT BYPASSED.** `boto3.client` is
    patched, so `main.py:613` still executes `_S3Versions(boto3.client("s3"))`
    — the wrapper is really constructed, by the production line, around a fake
    client. That is the difference between substituting a dependency and
    skipping the code under test. The manifest reader is patched for the same
    reason as elsewhere in this file: leftover `submissions` rows point at
    buckets this host cannot read, and the resulting refusal is correct
    behaviour asserted in `test_gc_manifest_expansion.py`.

    `main()` itself is not called: it opens its own `operator_session()`, and
    a contract test must run against the fixture's connection. The dispatch it
    performs — `args.func(conn, args, out)` — is performed here identically,
    which is the whole of what `main()` adds over this.
    """
    conn = fixture.connect()
    try:
        require_gc_schema(conn)
        from pipeline.contract.test_gc_execution import (StubS3, approved_plan,
                                                          item_statuses)

        key = "science/r/u/attempt-0000000001/cliexec.fits"
        repo, plan = approved_plan(conn, (key,), "cliexec-" + fixture.RUN_TAG)

        stub = StubS3(versions={(BUCKET, key): "v1"})
        built = []

        class _FakeBoto3Client(object):
            """Stands in for the boto3 S3 client `main.py:613` constructs.

            It implements the two calls `_S3Versions` makes of a real client —
            `head_object` and `delete_object` — and forwards them to the stub,
            so the assertions below are about what the PRODUCTION wrapper did
            with a client, not about what a hand-built surface chose to do.
            """

            def head_object(self, Bucket, Key, **kwargs):
                version = stub.head_version(Bucket, Key)
                if version is None:
                    error = Exception("NoSuchKey")
                    error.response = {"Error": {"Code": "404"}}
                    raise error
                return {"VersionId": version}

            def delete_object(self, Bucket, Key, VersionId=None, **kwargs):
                assert VersionId is not None, (
                    "the CLI path deleted by key alone; on a versioning-"
                    "enabled bucket that installs a delete marker over "
                    "whatever is current")
                stub.delete_version(Bucket, Key, VersionId)
                return {}

        def _fake_client(service, *args, **kwargs):
            built.append(service)
            return _FakeBoto3Client()

        # Patched on the boto3 module itself, because `main.py:612` does a
        # local `import boto3` inside the branch — so the attribute is
        # resolved at call time, which is exactly what makes this substitution
        # reach the production line rather than replace it.
        import boto3
        monkeypatch.setattr(boto3, "client", _fake_client)
        monkeypatch.setattr(
            "pipeline.operatorctl.gc.s3_manifest_reader",
            lambda client=None: _stub_reader)

        # THE REAL PARSER, THE REAL DISPATCH.
        args = parse(["gc-execute-plan", "--plan-id", str(plan.plan_id),
                      "--reason", "fix round 2: the CLI apply path",
                      "--idempotency-key", "cliexec-%s" % fixture.RUN_TAG,
                      "--apply"])
        assert args.apply is True
        assert args.func is operatorctl_main._cmd_gc_execute, (
            "the gc-execute-plan subcommand does not dispatch to "
            "_cmd_gc_execute; a dropped func mapping is exactly what this "
            "test exists to catch")

        out = _Out()
        rc = args.func(conn, args, out)
        assert rc == operatorctl_main.EXIT_OK

        # THE APPLY BRANCH RAN: it built an S3 client...
        assert built == ["s3"], (
            "the apply branch did not construct its S3 client; either it was "
            "not reached or main.py:612-613 changed")
        # ...and drove the executor through it, by EXACT VersionId.
        assert stub.delete_calls == [(BUCKET, key, "v1")], (
            "the executor was not reached through the CLI, or the deletion "
            "did not go by exact VersionId")
        assert item_statuses(conn, plan.plan_id)[0][1] == "deleted"

        # The run-level operator act is recorded under the enumerated class,
        # with the key the operator supplied — proving `record_execution` was
        # called from the branch and not merely importable.
        with conn.cursor() as cur:
            cur.execute("SELECT actor, rows_affected FROM derived.mutation_audit"
                        " WHERE action_class = 'gc_plan_execute'"
                        "   AND idempotency_key = %s",
                        ("cliexec-%s" % fixture.RUN_TAG,))
            row = cur.fetchone()
        assert row is not None, (
            "the CLI apply path wrote no audit row under gc_plan_execute")
        actor, rows_affected = row
        # The actor came from `_session_user(conn)` — the database's own
        # session_user — not from a CLI argument.
        with conn.cursor() as cur:
            cur.execute("SELECT session_user")
            assert actor == cur.fetchone()[0]
        assert rows_affected == 1, "the deleted count reached the ledger"

        # And the operator saw the per-outcome tally the branch prints.
        assert "items by outcome:" in out.text
        assert "deleted" in out.text
    finally:
        conn.close()


def test_gc_execute_plan_apply_refuses_an_unapproved_plan_through_the_cli():
    """The same CLI path, and it must REFUSE outside APPROVED/EXECUTING.

    The safety property the review accepted by inspection, now exercised
    through the dispatch that reaches it: compute, recompute, approve and
    execute are distinct recorded steps, so `--apply` on a merely-COMPUTED
    plan raises rather than deleting.
    """
    from pipeline.gc.references import PlanRefused
    conn = fixture.connect()
    try:
        require_gc_schema(conn)
        from pipeline.contract.test_gc_execution import record_a_plan
        repo, plan = record_a_plan(conn, tag="cliunapp-" + fixture.RUN_TAG)

        args = parse(["gc-execute-plan", "--plan-id", str(plan.plan_id),
                      "--reason", "unapproved", "--apply"])
        with pytest.raises(PlanRefused):
            args.func(conn, args, _Out())
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
