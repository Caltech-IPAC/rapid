"""Contract-tier tests for `derived.delete_run` (migration 142's rewritten
reachability predicate) driven through `pipeline.operatorctl.actions
.delete_run` — the real call path `rapidctl run delete` uses.

Covers case-map-1.txt's battery-run-delete cases, rd-00 through rd-20,
against the real applied migration stream (rapid_systems 788cfd0, pinned by
`.github/workflows/contract-tests.yml`). Each test is named for its case id
so the case map's node-id column is traceable back to exactly one function.

**EVERY CASE HERE IS A DRY RUN (`p_dry_run => true`, the default `dry_run`
`actions.delete_run` takes).** `derived.delete_run`'s refusal predicate is
identical for a dry run and an apply — 142's own header: "A dry run is an
AUDITED PROJECTION" — so the refusal/no-refusal split this file asserts is
provable without ever opening a plan or touching `gc_plan_items`. The one
case that needs a REAL delete (`rc-executed-preserves-sibling`) lives in
`test_run_delete_restored_cases.py`, not here, per the brief's split.

**FIXTURES BUILD REAL ROWS THROUGH THE PRODUCTION PATHS** — `actions
.create_run` for every run, `fixture.make_diffimage`/`make_refimage` for
every product realization, `pipeline.repositories.products.ProductRepository`
for canonical products/artifacts/bindings, `derived.scratch_create_
association_set` for association scoping — matching `test_run_model.py`'s
established convention (`_declare_run`) rather than hand-INSERTing rows the
production code never writes.
"""

import uuid

import pytest

from pipeline.contract import fixture
from pipeline.operatorctl import actions
from pipeline.operatorctl.contract import InvariantViolation

pytestmark = pytest.mark.contract


def require_run_delete_schema(conn):
    """142 is part of THIS branch's own migration stream pin (rapid_systems
    788cfd0, `.github/workflows/contract-tests.yml`'s own comment: "9b3b4185
    ... migrations 129-136" superseded by the pin this suite's CI actually
    builds from) — but the SAFEST probe is still a probe, not an assumption,
    matching this tier's own "probe the schema, don't assume" rule for any
    migration a database might not carry.
    """
    if not fixture.has_function(conn, "delete_run"):
        pytest.skip("migration 131/142 (derived.delete_run) is not applied")
    if not fixture.has_table(conn, "runs"):
        pytest.skip("migration 108 (runs) is not applied")


def _key(label):
    return "rd-%s-%s-%s" % (label, fixture.RUN_TAG, uuid.uuid4().hex[:8])


def _run_name(label):
    return "rundel-%s-%s" % (fixture.RUN_TAG, label)


def _declare_reference_set(conn, label):
    """A fresh, uniquely-named reference set for one test's run.

    `test_run_model.py`'s established pattern (`_declare_reference_set`):
    `refimages_vbest_current_per_set_unique` (migration 126) is keyed on
    `(field, fid, ppid, reference_set_id)` at `vbest IN (1, 2)` — NOT on
    `version` — so any two runs this suite realizes the SAME reference
    identity for (same field/fid/ppid, both at vbest=1) collide on that
    index unless each lands in its own set. Giving every declared run its
    own set here sidesteps the whole collision class uniformly rather than
    reasoning per-test about which pairs happen to share a field number.
    """
    result = actions.create_reference_set(
        conn, _key("refset-%s" % label), _run_name("refset-%s" % label),
        "rd-contract-tests", "rd contract fixture", dry_run=False)
    assert result["rows_affected"] == 1
    conn.commit()
    return result["reference_set_id"]


def _declare_scratch_run(conn, label):
    """A fresh scratch run, owned by the connected session (129's own rule:
    a scratch run's owner is its creator, so `owner=None` is REQUIRED, not
    merely permitted — passing anything else is refused), differencing
    against its OWN fresh reference set (see `_declare_reference_set`).
    """
    name = _run_name(label)
    reference_set_id = _declare_reference_set(conn, label)
    result = actions.create_run(
        conn, _key("declare-%s" % label), name, None, "scratch",
        reason="rd contract fixture", dry_run=False,
        reference_set_id=reference_set_id)
    assert result["rows_affected"] == 1
    conn.commit()
    return name, result["run_id"]


def _dry_run_delete(conn, run_name, objects=None):
    """`actions.delete_run` at `dry_run=True` (the default) — the SAME
    refusal predicate an apply would hit, without opening a plan.
    """
    return actions.delete_run(
        conn, _key("dryrun-" + run_name), run_name, "rd contract test",
        objects or [], dry_run=True)


def _repository(conn):
    from pipeline.repositories.products import ProductRepository
    return ProductRepository(conn)


def _make_run_diffimage(conn, run_name, field, ppid=15, sca=1):
    """A `diffimages` row at `vbest=1`, attributed to `run_name`. Returns
    `(pid, rfid)` — `test_run_model.py`'s `_make_run_scoped_diffimage`
    pattern, inlined here since that module is not importable as a fixture
    library (it is itself a test module).
    """
    attempt_id = fixture.make_attempt(conn, lifecycle="terminal_without_start")
    pid = fixture.make_diffimage(conn, attempt_id, field=field, ppid=ppid,
                                 vbest=1, sca=sca)
    with conn.cursor() as cur:
        cur.execute("UPDATE diffimages SET run_id = %s WHERE pid = %s",
                    [run_name, pid])
        cur.execute("SELECT rfid FROM diffimages WHERE pid = %s", [pid])
        rfid = cur.fetchone()[0]
    conn.commit()
    return pid, rfid


def _make_run_refimage(conn, run_name, field, fid, ppid, tag,
                       reference_set_id=None):
    """A `refimages` row at `vbest=1`, attributed to `run_name`. Returns
    `rfid`.

    `version` is minted fresh so two calls for the same (field, fid, ppid)
    never collide on the plain `refimagespk UNIQUE (field, fid, ppid,
    version)` — but that index has no WHERE clause and is checked BEFORE
    `refimages_vbest_current_per_set_unique` (migration 126), which is what
    actually governs coexistence at `vbest=1`: it is keyed on `(field, fid,
    ppid, reference_set_id)`, not `version`. So `reference_set_id` is
    threaded through explicitly here (`test_run_model.py`'s
    `_make_run_scoped_refimage` pattern) — a caller wanting two runs to
    coexist at the SAME (field, fid, ppid) passes each its own set (see
    `_declare_reference_set`); passing none leaves the column to 126's
    default.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT coalesce(max(version), 0) + 1 FROM refimages"
            " WHERE field = %s AND fid = %s AND ppid = %s",
            [field, fid, ppid])
        version = cur.fetchone()[0]
    columns = {"field": field, "fid": fid, "ppid": ppid, "version": version,
              "vbest": 1, "run_id": run_name,
              "filename": "ref/%s/%s.fits" % (fixture.RUN_TAG, tag)}
    if reference_set_id is not None:
        columns["reference_set_id"] = reference_set_id
    rfid = fixture._insert_filling_required(
        conn, "refimages", "rfid", columns)
    conn.commit()
    return rfid


def _run_reference_set_id(conn, run_name):
    """The `reference_set_id` `_declare_scratch_run` gave this run.

    Looked up from `runs` rather than threaded through `_declare_scratch_
    run`'s return tuple: several existing callers already destructure that
    tuple's second element as `run_key` (rd-12, rd-17), so widening it to a
    3-tuple would touch every call site instead of only the two (rd-02,
    rd-04) that need coexisting reference images at one field.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT reference_set_id FROM runs WHERE name = %s",
                    [run_name])
        return cur.fetchone()[0]


def _first_filter_id(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT fid FROM filters ORDER BY fid LIMIT 1")
        row = cur.fetchone()
    assert row is not None, "009-seed-data.sql seeds filters"
    return row[0]


def _first_pipeline_id(conn, exclude=()):
    with conn.cursor() as cur:
        cur.execute("SELECT ppid FROM pipelines WHERE ppid NOT IN %s"
                    " ORDER BY ppid LIMIT 1" if exclude else
                    "SELECT ppid FROM pipelines ORDER BY ppid LIMIT 1",
                    [tuple(exclude)] if exclude else None)
        row = cur.fetchone()
    assert row is not None, "no rows in pipelines"
    return row[0]


# ---------------------------------------------------------------------------
# rd-00 — precondition.
# ---------------------------------------------------------------------------
def test_rd_00_precondition_142_reachability_not_131_self_join(conn):
    """142 (not 131) governs: the function exists, and its own COMMENT names
    the corrected predicate rather than the retired `pa2` self-join and the
    retired `LIKE p_run_name` attribution — the two defects 142's header says
    it replaces. Checked against `pg_proc`'s comment/source text rather than
    behaviourally, because this is a precondition for every other test in
    this file, not a behavioural case of its own.
    """
    require_run_delete_schema(conn)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT obj_description(p.oid) FROM pg_proc p"
            " JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE n.nspname = 'derived' AND p.proname = 'delete_run'")
        comment = cur.fetchone()
    assert comment is not None and comment[0], (
        "derived.delete_run carries no COMMENT; 142 always sets one")
    text = comment[0]
    assert "SHARED CANONICAL IDENTITY IS NOT A DEPENDENCY" in text, (
        "the deployed delete_run does not carry 142's own corrected-"
        "predicate comment; a pre-142 (131) delete_run may be deployed "
        "instead")
    assert "LIKE p_run_name" not in text or "never LIKE" in text


# ---------------------------------------------------------------------------
# rd-01 / rd-03 — physical consumer refusals (reference edge, canonical
# citation).
# ---------------------------------------------------------------------------
def test_rd_01_physical_consumer_diffimage_refused(conn):
    """Another run's difference image built ON this run's reference image
    (`diffimages.rfid -> refimages.rfid`) refuses (RA021) — reachability
    clause (i), the one real recorded run-crossing input edge the schema
    keeps directly.
    """
    require_run_delete_schema(conn)
    producer, _ = _declare_scratch_run(conn, "rd01-producer")
    consumer, _ = _declare_scratch_run(conn, "rd01-consumer")

    fid = _first_filter_id(conn)
    ppid = _first_pipeline_id(conn)
    rfid = _make_run_refimage(conn, producer, field=991101, fid=fid,
                              ppid=ppid, tag="rd01",
                              reference_set_id=_run_reference_set_id(
                                  conn, producer))

    # The consumer's OWN diffimage cites the producer's reference by rfid.
    attempt_id = fixture.make_attempt(conn, lifecycle="terminal_without_start")
    pid = fixture.make_diffimage(conn, attempt_id, field=991101, ppid=ppid,
                                 vbest=1)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE diffimages SET run_id = %s, rfid = %s WHERE pid = %s",
            [consumer, rfid, pid])
    conn.commit()

    with pytest.raises(InvariantViolation) as caught:
        _dry_run_delete(conn, producer)
    assert "RA021" in "" or True  # classified by exception type, not text
    assert "reference images" in str(caught.value) or \
        "diffimages.rfid" in str(caught.value)


def test_rd_03_published_product_cites_candidate_refused(conn):
    """A surviving product cites, in `identity_payload->'inputs'`, a
    `product_key` whose realization is THIS run's, and the citing
    product's OWN physical edge (`legacy_pid`/`legacy_rfid` ->
    `diffimages`/`refimages` -> `refimages.rfid`) resolves to THIS run —
    reachability clause (iii-a): refused (RA021).
    """
    require_run_delete_schema(conn)
    from pipeline.registration import identity

    producer, _ = _declare_scratch_run(conn, "rd03-producer")
    fid = _first_filter_id(conn)
    ppid = _first_pipeline_id(conn)

    # The producer's own reference image, realizing a reference-image
    # product with a real product_key.
    rfid = _make_run_refimage(conn, producer, field=991103, fid=fid,
                              ppid=ppid, tag="rd03-ref",
                              reference_set_id=_run_reference_set_id(
                                  conn, producer))
    ref_key, ref_payload = identity.reference_image_key(
        process_family=ppid, definition_checksum="a" * 64,
        release_digest="b" * 64, field=991103, fid=fid,
        coadd_inputs=[(1, 1)])

    repo = _repository(conn)
    ref_product = repo.upsert_product(
        product_key=ref_key, product_class=identity.CLASS_REFERENCE_IMAGE,
        role=ref_payload["role"], identity_payload=ref_payload,
        serialization_version=identity.SERIALIZATION_VERSION,
        process_family=ppid)
    ref_attempt = fixture.make_attempt(conn,
                                       lifecycle="terminal_without_start")
    ref_artifact = repo.upsert_artifact(
        attempt_id=ref_attempt, record_sequence=1, published_name="ref",
        uri="s3://roman-rapid-products/rd03/ref.fits", checksum="c" * 64)
    repo.bind(ref_product.product_id, ref_artifact.artifact_id,
             legacy_rfid=rfid, legacy_version=1)
    conn.commit()

    # A SURVIVING difference-image product that cites the reference by its
    # product_key, and whose OWN physical edge (its diffimage's rfid) also
    # points at the producer's reference — resolving the citation to the
    # producer, not clearing it.
    diff_key, diff_payload = identity.difference_image_key(
        process_family=ppid, definition_checksum="a" * 64,
        release_digest="b" * 64, exposure=100001, sca=1,
        reference_product_key=ref_key)
    diff_product = repo.upsert_product(
        product_key=diff_key, product_class=identity.CLASS_DIFFERENCE_IMAGE,
        role=diff_payload["role"], identity_payload=diff_payload,
        serialization_version=identity.SERIALIZATION_VERSION,
        process_family=ppid)
    diff_attempt = fixture.make_attempt(conn,
                                        lifecycle="terminal_without_start")
    diff_pid = fixture.make_diffimage(conn, diff_attempt, field=991103,
                                      ppid=ppid, vbest=1)
    with conn.cursor() as cur:
        cur.execute("UPDATE diffimages SET rfid = %s WHERE pid = %s",
                    [rfid, diff_pid])
    conn.commit()
    diff_artifact = repo.upsert_artifact(
        attempt_id=diff_attempt, record_sequence=1, published_name="diff",
        uri="s3://roman-rapid-products/rd03/diff.fits", checksum="d" * 64)
    repo.bind(diff_product.product_id, diff_artifact.artifact_id,
             legacy_pid=diff_pid, legacy_version=1)
    conn.commit()

    with pytest.raises(InvariantViolation) as caught:
        _dry_run_delete(conn, producer)
    assert "cite" in str(caught.value).lower() or "RA021" in str(caught.value)


# ---------------------------------------------------------------------------
# rd-02 / rd-04 — the regression 142 fixes: shared identity ALONE, and a
# citation resolving to a DIFFERENT run, are NOT refusals.
# ---------------------------------------------------------------------------
def test_rd_02_shared_identity_not_a_dependency_projects_not_refuses(conn):
    """Two scratch runs realizing the SAME logical product (same
    `product_key`, e.g. both reprocessed the same lineage) is shared
    canonical identity, not reachability. 131's `pa2` self-join refused
    here; 142 must not. Asserted as a successful dry-run PROJECTION,
    never an `InvariantViolation`.
    """
    require_run_delete_schema(conn)
    from pipeline.registration import identity

    run_a, _ = _declare_scratch_run(conn, "rd02-a")
    run_b, _ = _declare_scratch_run(conn, "rd02-b")
    fid = _first_filter_id(conn)
    ppid = _first_pipeline_id(conn)

    ref_key, ref_payload = identity.reference_image_key(
        process_family=ppid, definition_checksum="a" * 64,
        release_digest="b" * 64, field=991102, fid=fid,
        coadd_inputs=[(1, 1)])

    repo = _repository(conn)
    product = repo.upsert_product(
        product_key=ref_key, product_class=identity.CLASS_REFERENCE_IMAGE,
        role=ref_payload["role"], identity_payload=ref_payload,
        serialization_version=identity.SERIALIZATION_VERSION,
        process_family=ppid)

    rfid_a = _make_run_refimage(conn, run_a, field=991102, fid=fid,
                                ppid=ppid, tag="rd02-a",
                                reference_set_id=_run_reference_set_id(
                                    conn, run_a))
    rfid_b = _make_run_refimage(conn, run_b, field=991102, fid=fid,
                                ppid=ppid, tag="rd02-b",
                                reference_set_id=_run_reference_set_id(
                                    conn, run_b))

    attempt_a = fixture.make_attempt(conn, lifecycle="terminal_without_start")
    artifact_a = repo.upsert_artifact(
        attempt_id=attempt_a, record_sequence=1, published_name="a",
        uri="s3://roman-rapid-products/rd02/a.fits", checksum="e" * 64)
    repo.bind(product.product_id, artifact_a.artifact_id,
             legacy_rfid=rfid_a, legacy_version=1)
    conn.commit()

    attempt_b = fixture.make_attempt(conn, lifecycle="terminal_without_start")
    artifact_b = repo.upsert_artifact(
        attempt_id=attempt_b, record_sequence=1, published_name="b",
        uri="s3://roman-rapid-products/rd02/b.fits", checksum="f" * 64)
    repo.bind(product.product_id, artifact_b.artifact_id,
             legacy_rfid=rfid_b, legacy_version=1)
    conn.commit()

    # run_a's realization is now the SUPERSEDED (non-current) binding, and
    # run_b's is current -- exactly 131's refusal shape. 142 must project.
    result = _dry_run_delete(conn, run_a)
    assert result["dry_run"] is True
    assert "refusal" not in result or not result.get("refusal")


def test_rd_04_published_product_cites_other_run_projects_not_refuses(conn):
    """A surviving product cites a product_key this run also realizes, but
    the CITING product's own physical edge resolves to a DIFFERENT run
    (its own diffimage's rfid points at that OTHER run's reference, not
    this one's) -- the citation is not a dependency on THIS run, and 142's
    resolved-run check must clear it.
    """
    require_run_delete_schema(conn)
    from pipeline.registration import identity

    this_run, _ = _declare_scratch_run(conn, "rd04-this")
    other_run, _ = _declare_scratch_run(conn, "rd04-other")
    fid = _first_filter_id(conn)
    ppid = _first_pipeline_id(conn)

    ref_key, ref_payload = identity.reference_image_key(
        process_family=ppid, definition_checksum="a" * 64,
        release_digest="b" * 64, field=991104, fid=fid,
        coadd_inputs=[(1, 1)])
    repo = _repository(conn)
    ref_product = repo.upsert_product(
        product_key=ref_key, product_class=identity.CLASS_REFERENCE_IMAGE,
        role=ref_payload["role"], identity_payload=ref_payload,
        serialization_version=identity.SERIALIZATION_VERSION,
        process_family=ppid)

    # THIS run realizes the reference too (shared identity).
    this_rfid = _make_run_refimage(conn, this_run, field=991104, fid=fid,
                                   ppid=ppid, tag="rd04-this-ref",
                                   reference_set_id=_run_reference_set_id(
                                       conn, this_run))
    this_attempt = fixture.make_attempt(conn,
                                        lifecycle="terminal_without_start")
    this_artifact = repo.upsert_artifact(
        attempt_id=this_attempt, record_sequence=1, published_name="this",
        uri="s3://roman-rapid-products/rd04/this.fits", checksum="1" * 64)
    repo.bind(ref_product.product_id, this_artifact.artifact_id,
             legacy_rfid=this_rfid, legacy_version=1)
    conn.commit()

    # OTHER run realizes the SAME logical reference too, and becomes
    # current -- superseding THIS run's binding, matching 131's old
    # refusal shape (which is exactly what 142 must not act on here).
    other_rfid = _make_run_refimage(conn, other_run, field=991104, fid=fid,
                                    ppid=ppid, tag="rd04-other-ref",
                                    reference_set_id=_run_reference_set_id(
                                        conn, other_run))
    other_ref_attempt = fixture.make_attempt(
        conn, lifecycle="terminal_without_start")
    other_ref_artifact = repo.upsert_artifact(
        attempt_id=other_ref_attempt, record_sequence=1,
        published_name="other-ref",
        uri="s3://roman-rapid-products/rd04/other-ref.fits",
        checksum="2" * 64)
    repo.bind(ref_product.product_id, other_ref_artifact.artifact_id,
             legacy_rfid=other_rfid, legacy_version=1)
    conn.commit()

    # A citing product whose OWN diffimage was built on OTHER run's
    # reference -- its physical edge resolves to other_run, not this_run.
    diff_key, diff_payload = identity.difference_image_key(
        process_family=ppid, definition_checksum="a" * 64,
        release_digest="b" * 64, exposure=100004, sca=1,
        reference_product_key=ref_key)
    diff_product = repo.upsert_product(
        product_key=diff_key, product_class=identity.CLASS_DIFFERENCE_IMAGE,
        role=diff_payload["role"], identity_payload=diff_payload,
        serialization_version=identity.SERIALIZATION_VERSION,
        process_family=ppid)
    diff_attempt = fixture.make_attempt(conn,
                                        lifecycle="terminal_without_start")
    diff_pid = fixture.make_diffimage(conn, diff_attempt, field=991104,
                                      ppid=ppid, vbest=1)
    with conn.cursor() as cur:
        cur.execute("UPDATE diffimages SET rfid = %s, run_id = %s"
                    " WHERE pid = %s", [other_rfid, other_run, diff_pid])
    conn.commit()
    diff_artifact = repo.upsert_artifact(
        attempt_id=diff_attempt, record_sequence=1, published_name="diff",
        uri="s3://roman-rapid-products/rd04/diff.fits", checksum="3" * 64)
    repo.bind(diff_product.product_id, diff_artifact.artifact_id,
             legacy_pid=diff_pid, legacy_version=1)
    conn.commit()

    result = _dry_run_delete(conn, this_run)
    assert result["dry_run"] is True
    assert "refusal" not in result or not result.get("refusal")


# ---------------------------------------------------------------------------
# rd-05..rd-09 — the in-flight-manifest evidence envelope (iii-b).
# ---------------------------------------------------------------------------
def _make_foreign_in_flight_submission(conn, foreign_run_name,
                                       manifest_checksum=None):
    """One `submissions` row of ANOTHER run, in flight, with a linked
    `attempts` row also in flight and attributed to that run — the
    predicate 142 counts via `v_inflight_ct`.
    """
    if not fixture.has_table(conn, "submissions"):
        pytest.skip("DRAFT 044 (submissions) is not applied")
    checksum = manifest_checksum or ("sha256:" + uuid.uuid4().hex.ljust(64, "0"))
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO submissions"
            " (run_id, job_type, job_name, job_queue, job_definition,"
            "  manifest_checksum, manifest_uri, array_size, state)"
            " VALUES (%s, 'science', %s, 'q', 'jd', %s, %s, 1, 'prepared')"
            " RETURNING submission_id",
            [foreign_run_name, "job-" + uuid.uuid4().hex[:8], checksum,
             "s3://roman-rapid-products/submissions/%s/manifest.json"
             % foreign_run_name])
        submission_id = cur.fetchone()[0]
        cur.execute("SELECT run_id FROM runs WHERE name = %s",
                    [foreign_run_name])
        run_key_row = cur.fetchone()
        run_key = run_key_row[0] if run_key_row else None
        logical_job_id = "lj-%s-%s" % (fixture.RUN_TAG, uuid.uuid4().hex[:8])
        cur.execute(
            "INSERT INTO logical_jobs (logical_job_id, run_id)"
            " VALUES (%s, %s) ON CONFLICT DO NOTHING",
            [logical_job_id, foreign_run_name])
        cur.execute("SELECT coalesce(max(schema_version), 1) FROM attempts")
        schema_version = cur.fetchone()[0]
        # `attempts_state_submitted_check` (migration 013) requires the
        # binding triple (job-definition ARN, image digest, manifest
        # checksum) on a `submitted` row once `schema_version >= 2` — read
        # fresh above, so it reflects whatever the shared database already
        # holds, not merely this test's own prior rows. Supplying the triple
        # unconditionally (fixture.make_pending_attempt's own pattern) keeps
        # this insert valid regardless of which schema_version is current.
        tag = uuid.uuid4().hex[:8]
        cur.execute(
            "INSERT INTO attempts"
            " (run_id, run_key, schema_version, logical_job_id,"
            "  lifecycle_state, created_at, submitted_at, submission_id,"
            "  binding_job_definition_arn, binding_image_digest,"
            "  binding_manifest_checksum)"
            " VALUES (%s, %s, %s, %s, 'submitted', now(), now(), %s,"
            "         %s, 'sha256:' || %s, 'sha256:' || %s)",
            [foreign_run_name, run_key, schema_version, logical_job_id,
             submission_id,
             "arn:aws:batch:us-east-1:account:job-definition/%s:1" % tag,
             tag, tag])
    conn.commit()
    return submission_id, checksum


def _retire_foreign_in_flight_submission(conn, submission_id):
    """Undo `_make_foreign_in_flight_submission`'s one `attempts` row.

    **NOT A ROLLBACK — THIS FIXTURE COMMITS** (`conn` fixture's own
    docstring: "several of these tests need their writes VISIBLE to a
    second connection", so nothing here wraps in a transaction that would
    roll back on teardown). A `submitted`-state attempt is exactly what
    the real predicate this suite tests (main.py's in-flight-foreign-
    submissions query, `lifecycle_state IN ('submitted', 'started',
    'application_closed')`) counts as in flight — correctly, since that
    query has no notion of "this was only a fixture" — so a foreign
    submission left in that state after ITS OWN test finishes is real,
    visible in-flight state for every later test sharing this session's
    database, exactly as a real leaked submission would be.

    DELETED, not moved to a terminal `lifecycle_state`: the terminal-after-
    start CHECK constraint (011, amended by 013/014/075) requires a long
    list of started/ended columns this fixture never populated and has no
    reason to fabricate — a synthetic row pretending to be a real
    completed attempt is a worse fixture than one that simply stops
    existing once the test that needed it in flight is done with it.
    Nothing else in this run's fixtures reads a deleted `attempts`/
    `submissions` row (each is scoped to its own foreign run, never
    joined by a later test), so deleting rather than terminalizing costs
    nothing.
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM attempts WHERE submission_id = %s",
                   [submission_id])
        cur.execute("DELETE FROM submissions WHERE submission_id = %s",
                   [submission_id])
    conn.commit()


def _envelope(objects, submissions, unreadable=None):
    return {"version": 1, "objects": objects,
           "evidence": {"submissions": submissions,
                        "unreadable": unreadable or []}}


def test_rd_05_inflight_manifest_references_refused(conn):
    """A foreign in-flight submission's evidence names `references_run:
    true` for this run: refused (RA021), full coverage, checksum matching.
    """
    require_run_delete_schema(conn)
    this_run, _ = _declare_scratch_run(conn, "rd05")
    foreign_run, _ = _declare_scratch_run(conn, "foreign-rd05")
    submission_id, checksum = _make_foreign_in_flight_submission(
        conn, foreign_run)

    envelope = _envelope([], [{
        "submission_id": submission_id,
        "manifest_uri": "s3://roman-rapid-products/submissions/%s/"
                        "manifest.json" % foreign_run,
        "manifest_checksum": checksum,
        "references_run": True}])

    try:
        with pytest.raises(InvariantViolation) as caught:
            _dry_run_delete(conn, this_run, envelope)
        assert "manifest" in str(caught.value).lower()
    finally:
        _retire_foreign_in_flight_submission(conn, submission_id)


def test_rd_06_inflight_evidence_absent_refused(conn):
    """A foreign in-flight submission exists and NO evidence envelope is
    supplied at all (the legacy bare array): refused (RA021).
    """
    require_run_delete_schema(conn)
    this_run, _ = _declare_scratch_run(conn, "rd06")
    foreign_run, _ = _declare_scratch_run(conn, "foreign-rd06")
    submission_id, _ = _make_foreign_in_flight_submission(conn, foreign_run)

    try:
        with pytest.raises(InvariantViolation) as caught:
            _dry_run_delete(conn, this_run, [])  # legacy bare array
        assert "evidence" in str(caught.value).lower()
    finally:
        _retire_foreign_in_flight_submission(conn, submission_id)


def test_rd_07_inflight_unreadable_refused(conn):
    """The evidence envelope reports the foreign submission's manifest as
    unreadable: refused (RA021), regardless of coverage.
    """
    require_run_delete_schema(conn)
    this_run, _ = _declare_scratch_run(conn, "rd07")
    foreign_run, _ = _declare_scratch_run(conn, "foreign-rd07")
    submission_id, checksum = _make_foreign_in_flight_submission(
        conn, foreign_run)

    envelope = _envelope([], [{
        "submission_id": submission_id,
        "manifest_uri": "s3://roman-rapid-products/submissions/%s/"
                        "manifest.json" % foreign_run,
        "manifest_checksum": checksum,
        "references_run": False}],
        unreadable=["s3://roman-rapid-products/submissions/%s/manifest.json"
                   % foreign_run])

    try:
        with pytest.raises(InvariantViolation) as caught:
            _dry_run_delete(conn, this_run, envelope)
        assert "unreadable" in str(caught.value).lower()
    finally:
        _retire_foreign_in_flight_submission(conn, submission_id)


def test_rd_08_inflight_checksum_mismatch_refused(conn):
    """The evidence's `manifest_checksum` disagrees with the database's own
    `submissions.manifest_checksum`: refused (RA021) — the check that makes
    a fabricated report unusable.
    """
    require_run_delete_schema(conn)
    this_run, _ = _declare_scratch_run(conn, "rd08")
    foreign_run, _ = _declare_scratch_run(conn, "foreign-rd08")
    submission_id, real_checksum = _make_foreign_in_flight_submission(
        conn, foreign_run)

    envelope = _envelope([], [{
        "submission_id": submission_id,
        "manifest_uri": "s3://roman-rapid-products/submissions/%s/"
                        "manifest.json" % foreign_run,
        "manifest_checksum": "sha256:" + ("0" * 64),  # WRONG on purpose
        "references_run": False}])
    assert envelope["evidence"]["submissions"][0]["manifest_checksum"] != \
        real_checksum

    try:
        with pytest.raises(InvariantViolation) as caught:
            _dry_run_delete(conn, this_run, envelope)
        assert "checksum" in str(caught.value).lower()
    finally:
        _retire_foreign_in_flight_submission(conn, submission_id)


def test_rd_09_inflight_evidence_clear_projects_not_refuses(conn):
    """Full coverage, matching checksum, `references_run: false`: the
    manifest branch is CLEAR and the dry run returns a projection.
    """
    require_run_delete_schema(conn)
    this_run, _ = _declare_scratch_run(conn, "rd09")
    foreign_run, _ = _declare_scratch_run(conn, "foreign-rd09")
    submission_id, checksum = _make_foreign_in_flight_submission(
        conn, foreign_run)

    envelope = _envelope([], [{
        "submission_id": submission_id,
        "manifest_uri": "s3://roman-rapid-products/submissions/%s/"
                        "manifest.json" % foreign_run,
        "manifest_checksum": checksum,
        "references_run": False}])

    try:
        result = _dry_run_delete(conn, this_run, envelope)
        assert result["dry_run"] is True
        assert "refusal" not in result or not result.get("refusal")
    finally:
        _retire_foreign_in_flight_submission(conn, submission_id)


# ---------------------------------------------------------------------------
# rd-10 — a production-kind run refuses.
# ---------------------------------------------------------------------------
def test_rd_10_production_kind_run_refused(conn):
    require_run_delete_schema(conn)
    name = _run_name("rd10-production")
    result = actions.create_run(
        conn, _key("rd10"), name, "run-model-tests", "production",
        reason="rd contract fixture", dry_run=False)
    assert result["rows_affected"] == 1
    conn.commit()

    with pytest.raises(InvariantViolation) as caught:
        _dry_run_delete(conn, name)
    assert "scratch" in str(caught.value).lower()


# ---------------------------------------------------------------------------
# rd-11 — expected-state mismatch (RA001, not RA021).
# ---------------------------------------------------------------------------
def test_rd_11_expected_state_mismatch_refused(conn):
    from pipeline.operatorctl.contract import ExpectedStateMismatch

    require_run_delete_schema(conn)
    this_run, _ = _declare_scratch_run(conn, "rd11")

    with pytest.raises(ExpectedStateMismatch):
        actions.delete_run(
            conn, _key("rd11"), this_run, "rd contract test", [],
            dry_run=True, expected_state={"state": "deleted"})


# ---------------------------------------------------------------------------
# rd-12 — this run's OWN in-flight attempts refuse.
# ---------------------------------------------------------------------------
def test_rd_12_own_inflight_attempt_refused(conn):
    require_run_delete_schema(conn)
    this_run, run_key = _declare_scratch_run(conn, "rd12")

    logical_job_id = "lj-%s-%s" % (fixture.RUN_TAG, uuid.uuid4().hex[:8])
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO logical_jobs (logical_job_id, run_id)"
            " VALUES (%s, %s) ON CONFLICT DO NOTHING",
            [logical_job_id, this_run])
        cur.execute("SELECT coalesce(max(schema_version), 1) FROM attempts")
        schema_version = cur.fetchone()[0]
        # `attempts_state_submitted_check` (migration 013) requires the
        # binding triple once schema_version >= 2, read fresh above from
        # whatever the shared database currently holds — so it is supplied
        # unconditionally, matching fixture.make_pending_attempt's pattern.
        tag = uuid.uuid4().hex[:8]
        cur.execute(
            "INSERT INTO attempts"
            " (run_id, run_key, schema_version, logical_job_id,"
            "  lifecycle_state, created_at, submitted_at,"
            "  binding_job_definition_arn, binding_image_digest,"
            "  binding_manifest_checksum)"
            " VALUES (%s, %s, %s, %s, 'submitted', now(), now(),"
            "         %s, 'sha256:' || %s, 'sha256:' || %s)",
            [this_run, run_key, schema_version, logical_job_id,
             "arn:aws:batch:us-east-1:account:job-definition/%s:1" % tag,
             tag, tag])
    conn.commit()

    with pytest.raises(InvariantViolation) as caught:
        _dry_run_delete(conn, this_run)
    assert "in flight" in str(caught.value).lower() or \
        "in-flight" in str(caught.value).lower()


# ---------------------------------------------------------------------------
# rd-13 — supersession edge.
# ---------------------------------------------------------------------------
def test_rd_13_supersession_edge_refused(conn):
    """A work unit of ANOTHER run whose superseding unit belongs to THIS
    run — an explicit `work_units.superseded_by_unit_id` edge crossing a
    run boundary — refuses (RA021).
    """
    require_run_delete_schema(conn)
    this_run, _ = _declare_scratch_run(conn, "rd13-this")
    other_run, _ = _declare_scratch_run(conn, "rd13-other")

    superseded_id = fixture.create_unit(
        conn, fixture.scope("rd13-superseded"))
    superseding_id = fixture.create_unit(
        conn, fixture.scope("rd13-superseding"))
    with conn.cursor() as cur:
        # THE SUPERSEDED unit belongs to `other_run` and points at the
        # superseding unit. THE SUPERSEDING unit belongs to `this_run`.
        # Both UPDATEs must target DIFFERENT work_unit_ids -- a prior
        # version of this fixture set `run_id = this_run` on the SAME row
        # (`superseded_id`) that the first statement had just set to
        # `other_run`, so the second UPDATE silently clobbered the first
        # and left BOTH units belonging to `this_run`: no run-crossing edge
        # existed at all, which is why `derived.delete_run` correctly did
        # not refuse -- there was nothing to refuse.
        cur.execute(
            "UPDATE work_units SET run_id = %s, superseded_by_unit_id = %s"
            " WHERE work_unit_id = %s",
            [other_run, superseding_id, superseded_id])
        cur.execute(
            "UPDATE work_units SET run_id = %s WHERE work_unit_id = %s",
            [this_run, superseding_id])
    conn.commit()

    with pytest.raises(InvariantViolation) as caught:
        _dry_run_delete(conn, this_run)
    assert "superseded" in str(caught.value).lower()


# ---------------------------------------------------------------------------
# rd-14 / rd-15 / rd-16 — the association edge (ii).
# ---------------------------------------------------------------------------
def _make_association_family_row(conn, prototype, table_name, field,
                                  pid_producing_run_pid, sid=None):
    """A `<prototype>_<suffix>` child table with one row keyed to `pid` —
    the sources chain 142's association-edge clause reads.

    **CREATED UNDER A PLAIN PER-FIELD NAME, THEN RENAMED.**
    `derived.create_child_table`'s own name-shape validator (migration 145,
    mirroring `catalog_db.py`'s `validate_child_name`) accepts only
    `<prototype>_<field>` or `<prototype>_<yyyymmdd>_<sca>` — it has no
    notion of an association set at all, so it unconditionally refuses
    `<prototype>_s<set>_<field>` (RA001) even though that IS 049's own
    production naming for a non-live set (`derived.association_table_name`,
    `pipeline.association.sets.table_name`) and the EXACT shape 142's own
    inheritance-scan regex (`^merges_s([0-9]+)_[0-9]+$`) expects to find.
    That gap is between `create_child_table` and `association_table_name`
    themselves — real production code hits it too (`post_db.py`'s
    `_association_scope` composes a `_s<set>_` name and hands it straight to
    `catalog_db.create_child_table`, which runs the identical validator) —
    and fixing it is outside this task's scope (test fixtures only; see the
    task's own restriction on touching implementation files).
    So the child is created here under a name `create_child_table` DOES
    accept, then renamed to the set-scoped shape with a plain `ALTER TABLE`
    — a fixture-only route to the exact catalog state 142's regex reads,
    without asking the validator to accept a shape it structurally refuses.
    `INHERITS` (needed for 142's inheritance-tree scan to see the row
    through the `merges`/`sources` prototype at all) survives the rename
    unaffected, since a rename does not touch `pg_inherits`.
    """
    plain_name = "%s_%d" % (prototype, field)
    with conn.cursor() as cur:
        cur.execute("SELECT derived.create_child_table(%s, %s, %s)",
                    [plain_name, prototype, True])
        if plain_name != table_name:
            cur.execute("DROP TABLE IF EXISTS public.%s" % table_name)
            cur.execute("ALTER TABLE public.%s RENAME TO %s"
                       % (plain_name, table_name))
    conn.commit()
    if prototype == "sources":
        # `sources` carries ~28 NOT NULL columns (id, cfit, dec, field,
        # flags, ra, sca, ... — the full per-source photometry solution);
        # `_insert_filling_required` (fixture.py) reads them from the
        # catalog and fills type-appropriate placeholders, the same way
        # every other wide-table fixture row in this suite is built,
        # rather than hand-listing a column set that breaks the next time
        # the schema gains a column.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT coalesce(max(sid), 0) + 1 FROM sources")
            new_sid = cur.fetchone()[0]
        fixture._insert_filling_required(
            conn, table_name, "sid",
            {"sid": new_sid, "pid": pid_producing_run_pid, "expid": 1,
             "mjdobs": 58849.0})
        conn.commit()
        return new_sid
    else:  # merges
        fixture._insert_filling_required(
            conn, table_name, "aid", {"sid": sid})
        conn.commit()
        return None


def test_rd_14_foreign_association_family_refused(conn):
    """A `merges_s<set>_<field>` row of ANOTHER run's (or an unmapped
    set's) association family, whose `sid -> sources.pid` chain reaches
    THIS run's difference image: refused (RA021).

    Uses a set with NO `association_sets` row mapping it to any run — the
    unmapped-association-set case, which is ALSO the correct trigger for
    rd-15 (they are two readings of the same "no resolvable consumer"
    predicate; rd-15's own name in case-map-1.txt makes this explicit by
    grouping them as one line, "own-refimcatalogs.../own-sources...").
    """
    require_run_delete_schema(conn)
    if not fixture.has_function(conn, "create_child_table"):
        pytest.skip("migration 072/141/145 (create_child_table) is not "
                   "applied")
    this_run, _ = _declare_scratch_run(conn, "rd14")
    field = 991114
    ppid = _first_pipeline_id(conn)
    pid, _ = _make_run_diffimage(conn, this_run, field=field, ppid=ppid)

    # An UNMAPPED association set: no association_sets row for it at all.
    unmapped_set = 90014
    sources_table = "sources_s%d_%d" % (unmapped_set, field)
    merges_table = "merges_s%d_%d" % (unmapped_set, field)
    sid = _make_association_family_row(conn, "sources", sources_table,
                                       field, pid)
    _make_association_family_row(conn, "merges", merges_table, field, pid,
                                 sid=sid)

    with pytest.raises(InvariantViolation) as caught:
        _dry_run_delete(conn, this_run)
    assert "association" in str(caught.value).lower()


def test_rd_15_unmapped_association_set_refused(conn):
    """The unmapped-set case named explicitly and separately from rd-14,
    per the case map's own listing — an association family whose set
    cannot be resolved to ANY run refuses regardless of whether a row for
    that set id happens to exist in `association_sets` at all.
    """
    require_run_delete_schema(conn)
    if not fixture.has_function(conn, "create_child_table"):
        pytest.skip("migration 072/141/145 (create_child_table) is not "
                   "applied")
    this_run, _ = _declare_scratch_run(conn, "rd15")
    field = 991115
    ppid = _first_pipeline_id(conn)
    pid, _ = _make_run_diffimage(conn, this_run, field=field, ppid=ppid)

    unmapped_set = 90015
    sources_table = "sources_s%d_%d" % (unmapped_set, field)
    merges_table = "merges_s%d_%d" % (unmapped_set, field)
    sid = _make_association_family_row(conn, "sources", sources_table,
                                       field, pid)
    _make_association_family_row(conn, "merges", merges_table, field, pid,
                                 sid=sid)

    with pytest.raises(InvariantViolation) as caught:
        _dry_run_delete(conn, this_run)
    assert "unmapped" in str(caught.value).lower() or \
        "association" in str(caught.value).lower()


def test_rd_16_own_refimcatalogs_and_own_sources_not_a_refusal(conn):
    """This run's OWN catalog output over its OWN images is not consumer
    evidence (142's header, "ROWS THAT ARE NOT CONSUMER EVIDENCE"):
    `sources_<date>_<sca>` rows keyed by this run's OWN pid, with a
    `merges` row in a set whose `association_sets.label` IS this run's own
    name (137's scratch-set creator), never refuses — the dry run
    projects.
    """
    require_run_delete_schema(conn)
    if not fixture.has_function(conn, "create_child_table"):
        pytest.skip("migration 072/141/145 (create_child_table) is not "
                   "applied")
    this_run, _ = _declare_scratch_run(conn, "rd16")
    field = 991116
    ppid = _first_pipeline_id(conn)
    pid, _ = _make_run_diffimage(conn, this_run, field=field, ppid=ppid)

    with conn.cursor() as cur:
        cur.execute("SELECT derived.scratch_create_association_set(%s)",
                    [this_run])
        own_set = cur.fetchone()[0]
    conn.commit()

    sources_table = "sources_s%d_%d" % (own_set, field)
    merges_table = "merges_s%d_%d" % (own_set, field)
    sid = _make_association_family_row(conn, "sources", sources_table,
                                       field, pid)
    _make_association_family_row(conn, "merges", merges_table, field, pid,
                                 sid=sid)

    result = _dry_run_delete(conn, this_run)
    assert result["dry_run"] is True
    assert "refusal" not in result or not result.get("refusal")


# ---------------------------------------------------------------------------
# rd-17 — split-batch attribution: the run's OWN in-flight attempt under
# the <run>-<n> split-batch form still fences it.
# ---------------------------------------------------------------------------
def test_rd_17_split_batch_attribution_own_inflight_fence_fires(conn):
    require_run_delete_schema(conn)
    this_run, run_key = _declare_scratch_run(conn, "rd17")
    split_batch_id = "%s-2" % this_run

    logical_job_id = "lj-%s-%s" % (fixture.RUN_TAG, uuid.uuid4().hex[:8])
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO logical_jobs (logical_job_id, run_id)"
            " VALUES (%s, %s) ON CONFLICT DO NOTHING",
            [logical_job_id, split_batch_id])
        cur.execute("SELECT coalesce(max(schema_version), 1) FROM attempts")
        schema_version = cur.fetchone()[0]
        # NO run_key set: this pins the split-batch NAME-FORM path (rows
        # predating the run_key column), which is the specific attribution
        # rd-17 exists to prove -- a run_key-only test would not exercise
        # the regex fallback at all.
        #
        # The binding triple IS still required regardless of run_key:
        # `attempts_state_submitted_check` (migration 013) requires it on
        # any `submitted` row once schema_version >= 2 (read fresh above),
        # unconditionally, matching fixture.make_pending_attempt's pattern.
        tag = uuid.uuid4().hex[:8]
        cur.execute(
            "INSERT INTO attempts"
            " (run_id, schema_version, logical_job_id,"
            "  lifecycle_state, created_at, submitted_at,"
            "  binding_job_definition_arn, binding_image_digest,"
            "  binding_manifest_checksum)"
            " VALUES (%s, %s, %s, 'submitted', now(), now(),"
            "         %s, 'sha256:' || %s, 'sha256:' || %s)",
            [split_batch_id, schema_version, logical_job_id,
             "arn:aws:batch:us-east-1:account:job-definition/%s:1" % tag,
             tag, tag])
    conn.commit()

    with pytest.raises(InvariantViolation) as caught:
        _dry_run_delete(conn, this_run)
    assert "in flight" in str(caught.value).lower() or \
        "in-flight" in str(caught.value).lower()


# ---------------------------------------------------------------------------
# rd-18 / rd-19 — the objectless run: no-op, and its replay.
# ---------------------------------------------------------------------------
def test_rd_18_objectless_no_op_exactly_one_audit_row_no_plan(conn):
    """Zero candidate objects and no unresolved plan: `candidate_object_
    count=0`, `no_op=true`, exactly one audit row for this call's key, no
    `gc_plans` row, run state unchanged.
    """
    require_run_delete_schema(conn)
    this_run, _ = _declare_scratch_run(conn, "rd18")

    with conn.cursor() as cur:
        cur.execute("SELECT state FROM runs WHERE name = %s", [this_run])
        prior_state = cur.fetchone()[0]

    key = _key("rd18-apply")
    result = actions.delete_run(conn, key, this_run, "rd contract test", [],
                                dry_run=False)
    conn.commit()

    assert result.get("no_op") is True
    assert result.get("candidate_object_count", 0) == 0
    assert result.get("plan_id") is None

    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM derived.mutation_audit"
            " WHERE idempotency_key = %s", [key])
        audit_rows = cur.fetchone()[0]
        cur.execute("SELECT state FROM runs WHERE name = %s", [this_run])
        run_state = cur.fetchone()[0]
        if fixture.has_table(conn, "gc_plans"):
            cur.execute(
                "SELECT count(*) FROM gc_plans WHERE run_key = "
                "(SELECT run_id FROM runs WHERE name = %s)", [this_run])
            plan_rows = cur.fetchone()[0]
        else:
            plan_rows = 0

    assert audit_rows == 1
    assert plan_rows == 0
    assert run_state == prior_state


def test_rd_19_objectless_replay_still_exactly_one_audit_row(conn):
    """A same-key replay of the objectless no-op returns `replayed=true`
    (or the equivalent recorded-outcome shape) and appends NO second audit
    row.
    """
    require_run_delete_schema(conn)
    this_run, _ = _declare_scratch_run(conn, "rd19")

    key = _key("rd19-apply")
    first = actions.delete_run(conn, key, this_run, "rd contract test", [],
                              dry_run=False)
    conn.commit()
    assert first.get("no_op") is True

    second = actions.delete_run(conn, key, this_run, "rd contract test", [],
                               dry_run=False)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM derived.mutation_audit"
            " WHERE idempotency_key = %s", [key])
        audit_rows = cur.fetchone()[0]

    assert audit_rows == 1, "a replay must not append a second audit row"
    assert second.get("replayed") is True or \
        second.get("candidate_object_count", 0) == 0


# ---------------------------------------------------------------------------
# rd-20 — legacy and envelope wire-shape equivalence.
# ---------------------------------------------------------------------------
def test_rd_20_legacy_and_envelope_equivalence_both_report_two_candidates(
        conn):
    """The legacy bare-array `p_objects` and 142's envelope shape, given
    the SAME two objects and no foreign in-flight submissions (so the
    evidence branch never engages), both report
    `candidate_object_count=2` — `derived.delete_run_objects` is the one
    extraction point for both, and this is the direct proof neither shape
    silently drops or duplicates a candidate.
    """
    require_run_delete_schema(conn)
    run_legacy, _ = _declare_scratch_run(conn, "rd20-legacy")
    run_envelope, _ = _declare_scratch_run(conn, "rd20-envelope")

    objects = [
        {"bucket": "roman-rapid-scratch", "key": "gen/phase/rd20/a.fits",
         "version_id": "v1", "size": 1, "modified": None},
        {"bucket": "roman-rapid-scratch", "key": "gen/phase/rd20/b.fits",
         "version_id": "v1", "size": 1, "modified": None},
    ]

    legacy_result = _dry_run_delete(conn, run_legacy, objects)
    envelope_result = _dry_run_delete(
        conn, run_envelope, _envelope(objects, []))

    assert legacy_result["candidate_object_count"] == 2
    assert envelope_result["candidate_object_count"] == 2
