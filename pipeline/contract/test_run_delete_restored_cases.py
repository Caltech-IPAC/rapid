"""Contract-tier tests for the FIRST plan's own restored coverage
requirement — cases not carried by `test_run_delete_evidence_envelope.py`'s
battery-run-delete run: rc-nonowner, rc-companion-unbound,
rc-unresolved-provenance, rc-executed-preserves-sibling.

Against the real applied migration stream, real Postgres, `pipeline
.operatorctl.actions.delete_run` — the same call path as
`test_run_delete_evidence_envelope.py`.

**`rc-executed-preserves-sibling` IS THE ONE CASE IN THIS ENTIRE TASK THAT
CALLS `p_dry_run => false` FOR A REAL DELETE.** Authorized ONLY inside CI's
disposable, per-run PostgreSQL service container (`.github/workflows
/contract-tests.yml`'s `postgres:18` service, torn down at job end) — never
against SMDC, consistent with the brief's permitted-effects list. The
isolation is made explicit below via `fixture.RUN_TAG`-scoped run names (the
same discipline every other contract test in this repository already
follows) and an assertion that the target database is NOT named
`rapid_smdc`/`smdc` — belt and suspenders against ever pointing this test at
anything but a throwaway CI database.
"""

import uuid

import pytest

from pipeline.contract import fixture
from pipeline.operatorctl import actions
from pipeline.operatorctl.contract import InvariantViolation

pytestmark = pytest.mark.contract


def require_run_delete_schema(conn):
    if not fixture.has_function(conn, "delete_run"):
        pytest.skip("migration 131/142 (derived.delete_run) is not applied")


def _key(label):
    return "rc-%s-%s-%s" % (label, fixture.RUN_TAG, uuid.uuid4().hex[:8])


def _run_name(label):
    return "rundel-rc-%s-%s" % (fixture.RUN_TAG, label)


def _declare_reference_set(conn, label):
    """A fresh, uniquely-named reference set for one test's run.

    `test_run_model.py`'s established pattern: `refimages_vbest_current_
    per_set_unique` (migration 126) is keyed on `(field, fid, ppid,
    reference_set_id)` at `vbest IN (1, 2)`, not on `version` — so two runs
    realizing the SAME reference identity (same field/fid/ppid, both at
    vbest=1, exactly what `rc-executed-preserves-sibling` needs) collide on
    that index unless each lands in its own set.
    """
    result = actions.create_reference_set(
        conn, _key("refset-%s" % label), _run_name("refset-%s" % label),
        "rc contract fixture", "rc contract fixture", dry_run=False)
    assert result["rows_affected"] == 1
    conn.commit()
    return result["reference_set_id"]


def _declare_scratch_run(conn, label, owner=None):
    name = _run_name(label)
    reference_set_id = _declare_reference_set(conn, label)
    result = actions.create_run(
        conn, _key("declare-%s" % label), name, owner, "scratch",
        reason="rc contract fixture", dry_run=False,
        reference_set_id=reference_set_id)
    assert result["rows_affected"] == 1
    conn.commit()
    return name, result["run_id"]


def _dry_run_delete(conn, run_name, objects=None):
    return actions.delete_run(
        conn, _key("dryrun-" + run_name), run_name, "rc contract test",
        objects or [], dry_run=True)


# ---------------------------------------------------------------------------
# rc-nonowner — a non-owner, non-admin caller is refused.
# ---------------------------------------------------------------------------
def test_rc_nonowner_caller_refused(conn):
    """A caller who is neither the run's owner nor a `rapid_admin` member
    is refused (RA021).

    `derived.delete_run` reads `session_user` directly — the connected
    role — so this asserts against the REAL identity check by giving the
    run an owner that is NOT the connected session and confirming this
    session also does not carry `rapid_admin` membership, rather than
    trying to reconnect as a second role (which the contract tier's own
    fixture connection does not set up).
    """
    require_run_delete_schema(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT session_user")
        connected_as = cur.fetchone()[0]
        cur.execute(
            "SELECT pg_has_role(session_user, 'rapid_admin', 'MEMBER')")
        is_admin = cur.fetchone()[0]
    if is_admin:
        pytest.skip(
            "the connected role (%r) is a rapid_admin member, so the "
            "ownership fence cannot be exercised from this session — "
            "rc-nonowner needs a non-admin login, which the contract "
            "tier's single fixture connection does not provide" %
            connected_as)

    name = _run_name("nonowner")
    result = actions.create_run(
        conn, _key("declare-nonowner"), name, "someone-else-entirely",
        "scratch", reason="rc contract fixture", dry_run=False)
    assert result["rows_affected"] == 1
    conn.commit()

    with pytest.raises(InvariantViolation) as caught:
        _dry_run_delete(conn, name)
    assert "owned by" in str(caught.value).lower() or \
        "owner" in str(caught.value).lower()


# ---------------------------------------------------------------------------
# rc-companion-unbound — an unbound companion artifact is NOT a refusal.
# ---------------------------------------------------------------------------
def test_rc_companion_unbound_is_not_a_refusal(conn):
    """A companion artifact with NO product binding at all (the
    unselected-variant case 142's header names explicitly under "WHAT DOES
    NOT REFUSE") does not block deletion — the dry run projects.

    An `artifacts` row exists (attempt-scoped, real FK to `attempts`) but
    is never bound into `product_artifacts` — exactly the shape a
    not-chosen candidate realization takes when a stage keeps more than
    one output and only publishes one.
    """
    require_run_delete_schema(conn)
    if not fixture.has_table(conn, "artifacts"):
        pytest.skip("DRAFT 048 (artifacts) is not applied")

    this_run, run_key = _declare_scratch_run(conn, "companion")

    attempt_id = fixture.make_attempt(conn, lifecycle="terminal_without_start")
    with conn.cursor() as cur:
        cur.execute("UPDATE attempts SET run_key = %s WHERE attempt_id = %s",
                    [run_key, attempt_id])
        cur.execute(
            "INSERT INTO artifacts"
            " (attempt_id, record_sequence, published_name, uri,"
            "  checksum_algorithm, checksum)"
            " VALUES (%s, 1, %s, %s, 'sha256', %s)",
            [attempt_id, "companion-unbound",
             "s3://roman-rapid-products/rc-companion/unbound.fits",
             "a" * 64])
    conn.commit()

    result = _dry_run_delete(conn, this_run)
    assert result["dry_run"] is True
    assert "refusal" not in result or not result.get("refusal")


# ---------------------------------------------------------------------------
# rc-unresolved-provenance — fail closed.
# ---------------------------------------------------------------------------
def test_rc_unresolved_provenance_fails_closed(conn):
    """An attempt whose `run_key` matches this run but whose `run_id`
    string is neither this run's exact name nor a `<run>-<n>` split-batch
    form — genuinely unresolvable provenance — refuses (RA021) rather than
    being silently treated as belonging elsewhere.
    """
    require_run_delete_schema(conn)
    if not fixture.has_table(conn, "artifacts"):
        pytest.skip("DRAFT 048 (artifacts) is not applied")

    this_run, run_key = _declare_scratch_run(conn, "unresolved")

    attempt_id = fixture.make_attempt(conn, lifecycle="terminal_without_start")
    # run_key correctly attributes this attempt to `this_run`'s registry
    # row, but its OWN run_id string is a completely unrelated name — not
    # `this_run` and not `<this_run>-<n>`. 142's own SELECT for `v_unresolved`
    # reads exactly this combination.
    unrelated_name = "totally-unrelated-run-%s" % fixture.RUN_TAG
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE attempts SET run_key = %s, run_id = %s"
            " WHERE attempt_id = %s", [run_key, unrelated_name, attempt_id])
        cur.execute(
            "INSERT INTO artifacts"
            " (attempt_id, record_sequence, published_name, uri,"
            "  checksum_algorithm, checksum)"
            " VALUES (%s, 1, %s, %s, 'sha256', %s)",
            [attempt_id, "unresolved-provenance",
             "s3://roman-rapid-products/rc-unresolved/a.fits", "b" * 64])
    conn.commit()

    with pytest.raises(InvariantViolation) as caught:
        _dry_run_delete(conn, this_run)
    assert "unresolved" in str(caught.value).lower() or \
        "resolved" in str(caught.value).lower()


# ---------------------------------------------------------------------------
# rc-executed-preserves-sibling — the one REAL delete in this whole task.
# ---------------------------------------------------------------------------
def test_rc_executed_preserves_sibling(conn):
    """Executed deletion (`p_dry_run => false`) of ONE run's realization
    leaves the OTHER run's realization rows, current pointer, bindings and
    provenance unchanged; the shared products row survives untombstoned
    while the deleted realization is tombstoned.

    **THIS IS A REAL DELETE.** `conn` here is the contract tier's own
    `fixture.connect()` connection, pointed at the target
    `PGHOST`/`PGPORT`/`PGDATABASE` the CI job's disposable `postgres:18`
    service container sets (`.github/workflows/contract-tests.yml`'s own
    `env:` block: `PGHOST=127.0.0.1`, `PGDATABASE=rapid`), which is torn
    down with the job — never a persistent or SMDC target. The guard below
    makes that unmistakable at the top of the one test in this whole
    delegated task that performs a real deletion, rather than trusting the
    surrounding environment silently.
    """
    require_run_delete_schema(conn)
    target = fixture.connection_target()
    assert "smdc" not in (target.get("dbname") or "").lower(), (
        "refusing to run the one REAL-DELETE test in this suite against a "
        "database named %r — rc-executed-preserves-sibling is authorized "
        "ONLY inside CI's disposable postgres:18 service container"
        % target.get("dbname"))
    assert fixture.RUN_TAG, (
        "fixture.RUN_TAG is empty; every row this test writes must be "
        "scoped to this run of the suite")

    from pipeline.registration import identity
    repo_module = __import__("pipeline.repositories.products",
                             fromlist=["ProductRepository"])
    repo = repo_module.ProductRepository(conn)

    run_a, run_key_a = _declare_scratch_run(conn, "sibling-a")
    run_b, run_key_b = _declare_scratch_run(conn, "sibling-b")

    with conn.cursor() as cur:
        cur.execute("SELECT fid FROM filters ORDER BY fid LIMIT 1")
        fid = cur.fetchone()[0]
        cur.execute("SELECT ppid FROM pipelines ORDER BY ppid LIMIT 1")
        ppid = cur.fetchone()[0]

    field = 991200

    def _refimage(run_name, tag):
        # `refimages_vbest_current_per_set_unique` (migration 126) is keyed
        # on `(field, fid, ppid, reference_set_id)` at vbest IN (1, 2) — not
        # on `version` — and BOTH run_a and run_b deliberately realize the
        # SAME (field, fid, ppid) at vbest=1 here (that shared realization
        # is the whole point of the sibling test). Each run's own
        # `reference_set_id` (from `_declare_scratch_run`) is threaded onto
        # its refimage row so the two inserts land in different sets and
        # never collide on that index.
        with conn.cursor() as cur:
            cur.execute("SELECT reference_set_id FROM runs WHERE name = %s",
                       [run_name])
            reference_set_id = cur.fetchone()[0]
            cur.execute(
                "SELECT coalesce(max(version), 0) + 1 FROM refimages"
                " WHERE field = %s AND fid = %s AND ppid = %s",
                [field, fid, ppid])
            version = cur.fetchone()[0]
        rfid = fixture._insert_filling_required(
            conn, "refimages", "rfid",
            {"field": field, "fid": fid, "ppid": ppid, "version": version,
             "vbest": 1, "run_id": run_name,
             "reference_set_id": reference_set_id,
             "filename": "ref/%s/%s.fits" % (fixture.RUN_TAG, tag)})
        conn.commit()
        return rfid

    rfid_a = _refimage(run_a, "sib-a")
    rfid_b = _refimage(run_b, "sib-b")

    ref_key, ref_payload = identity.reference_image_key(
        process_family=ppid, definition_checksum="a" * 64,
        release_digest="b" * 64, field=field, fid=fid,
        coadd_inputs=[(1, 1)])
    product = repo.upsert_product(
        product_key=ref_key, product_class=identity.CLASS_REFERENCE_IMAGE,
        role=ref_payload["role"], identity_payload=ref_payload,
        serialization_version=identity.SERIALIZATION_VERSION,
        process_family=ppid)

    attempt_a = fixture.make_attempt(conn, lifecycle="terminal_without_start")
    # ATTRIBUTED to run A by `run_key`/`run_id` -- the tombstone UPDATE
    # joins `artifacts` to `attempts` and matches on exactly this pair
    # (142's own predicate), so an unattributed attempt would leave this
    # artifact untouchable by either run's delete_run call and the test
    # would prove nothing about the tombstone step.
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE attempts SET run_key = %s, run_id = %s"
            " WHERE attempt_id = %s", [run_key_a, run_a, attempt_a])
    artifact_a = repo.upsert_artifact(
        attempt_id=attempt_a, record_sequence=1, published_name="sib-a",
        uri="s3://roman-rapid-products/rc-sibling/a.fits", checksum="c" * 64)
    repo.bind(product.product_id, artifact_a.artifact_id,
             legacy_rfid=rfid_a, legacy_version=1)
    conn.commit()

    attempt_b = fixture.make_attempt(conn, lifecycle="terminal_without_start")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE attempts SET run_key = %s, run_id = %s"
            " WHERE attempt_id = %s", [run_key_b, run_b, attempt_b])
    artifact_b = repo.upsert_artifact(
        attempt_id=attempt_b, record_sequence=1, published_name="sib-b",
        uri="s3://roman-rapid-products/rc-sibling/b.fits", checksum="d" * 64)
    repo.bind(product.product_id, artifact_b.artifact_id,
             legacy_rfid=rfid_b, legacy_version=1)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT product_artifact_id, is_current, deleted_at"
            " FROM product_artifacts pa"
            " JOIN artifacts a ON a.artifact_id = pa.artifact_id"
            " WHERE pa.product_id = %s ORDER BY pa.product_artifact_id",
            [product.product_id])
        before = cur.fetchall()
    assert len(before) == 2

    # ONE REAL CANDIDATE OBJECT, run A's own artifact URI — so the FIRST
    # (apply) call opens a real gc_plans/gc_plan_items row rather than
    # taking the early-return objectless-no-op path, which never reaches
    # the tombstone UPDATE at all.
    candidate = {"bucket": "roman-rapid-products",
                "key": "rc-sibling/a.fits", "version_id": "v1",
                "size": 1, "modified": None}
    key = _key("apply-sibling-a")
    result = actions.delete_run(
        conn, key, run_a, "rc contract test: real delete", [candidate],
        dry_run=False)
    conn.commit()
    assert result["dry_run"] is False
    plan_id = result["plan_id"]
    assert plan_id is not None, (
        "a real candidate object must open a gc_plans row, not take the "
        "objectless no-op path")

    # SIMULATE THE EXECUTOR'S OWN WRITE — `pipeline/gc/execute.py`'s
    # Executor is what would normally mark this item `deleted` after a
    # real S3 delete, and there is no real S3 in the contract tier to
    # drive it through. What is under test here is `derived.delete_run`'s
    # OWN tombstone SQL (the UPDATE ... WHERE gi.status IN ('deleted',
    # 'already-absent')), which reads the item's status and nothing about
    # how it got there — so writing that one column directly is a faithful
    # substitute for the Executor's own write, not a bypass of the
    # function under test.
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE gc_plan_items SET status = 'deleted', outcome_at = now()"
            " WHERE plan_id = %s", [plan_id])
    conn.commit()

    # THE SECOND (RESUME) CALL, exactly as `_cmd_run_delete`'s own step 3b
    # does — a fresh key, the SAME candidate list, `dry_run=False` — is
    # what runs the tombstone UPDATE and resolves the run to `deleted`.
    result = actions.delete_run(
        conn, _key("apply-sibling-a-resume"), run_a,
        "rc contract test: real delete (resume)", [candidate],
        dry_run=False)
    conn.commit()
    assert result["run_state"] == "deleted"

    with conn.cursor() as cur:
        cur.execute(
            "SELECT product_artifact_id, is_current, deleted_at,"
            "       legacy_rfid"
            " FROM product_artifacts pa"
            " JOIN artifacts a ON a.artifact_id = pa.artifact_id"
            " WHERE pa.product_id = %s ORDER BY pa.product_artifact_id",
            [product.product_id])
        after = cur.fetchall()
        cur.execute("SELECT vbest, run_id FROM refimages WHERE rfid = %s",
                    [rfid_b])
        sibling_refimage = cur.fetchone()
        cur.execute("SELECT deleted_at FROM products WHERE product_id = %s",
                    [product.product_id])
        product_deleted_at = cur.fetchone()[0]

    assert len(after) == 2, (
        "no product_artifacts binding was removed by run A's delete_run "
        "call — bindings are kept as provenance either way")
    # RUN A'S OWN REALIZATION IS TOMBSTONED (the real deletion happened).
    a_binding = [row for row in after if row[3] == rfid_a]
    assert len(a_binding) == 1
    assert a_binding[0][2] is not None, (
        "run A's own artifact binding was not tombstoned by its own "
        "executed delete_run call — the tombstone UPDATE did not fire")
    # SIBLING B'S OWN REALIZATION IS UNTOUCHED.
    assert sibling_refimage == (1, run_b), (
        "run B's refimage row (vbest, run_id) changed after run A's "
        "delete_run call")
    b_binding = [row for row in after if row[3] == rfid_b]
    assert len(b_binding) == 1
    assert b_binding[0][2] is None, (
        "run B's artifact binding must not be tombstoned by run A's "
        "deletion")
    # THE SHARED PRODUCT SURVIVES UNTOMBSTONED, because run B's
    # realization (via rfid_b's artifact) is still alive.
    assert product_deleted_at is None, (
        "the shared products row was tombstoned even though run B's "
        "realization survives — 142's own 'no other realization survives' "
        "guard should have kept it alive")
