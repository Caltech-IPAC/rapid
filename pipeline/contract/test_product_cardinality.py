"""Product/artifact cardinality and its constraints (brief D, criterion 3).

Three distinct cases, each asserted against a real schema:

  (a) replay of the same `(attempt_id, record_sequence)` writes no new
      product, artifact or binding row;
  (b) a retry under a NEW attempt keeps the same product row, writes new
      artifact rows, and moves the binding — with `vbest` semantics intact;
  (c) changing one identity component produces a NEW product row.

And the UNIQUE/FK constraints are asserted DIRECTLY against the scratch
schema, not inferred from the behaviour above: a test that only observes
"the second insert did not duplicate" cannot tell a database constraint
from a Python `if` that happened to run first, and the whole reason the
cardinality is in the schema is that the application's promise is not
enough under concurrency.
"""

import pytest

from pipeline.contract import fixture
from pipeline.registration import identity

pytestmark = pytest.mark.usefixtures("conn")


SPEC = {
    "process_family": 15,
    "definition_checksum": "a" * 64,
    "release_digest": "b" * 64,
}


def _repository(conn):
    from pipeline.repositories.products import ProductRepository

    return ProductRepository(conn)


def _require_schema(conn):
    if not fixture.has_table(conn, "products"):
        pytest.skip("DRAFT migration 048 is not applied on this database")


def _unique_exposure(offset=0):
    """A run-unique exposure, so two runs never collide on product identity.

    The same fixture-honesty discipline the rest of this tier follows: the
    product key is UNIQUE-constrained, so a fixed exposure would make a
    second run of this suite collide with the first run's rows and the
    failure would look exactly like the defect under test.
    """
    return int(fixture.RUN_TAG[:6], 16) + offset


def _key_for(exposure, **overrides):
    kwargs = dict(SPEC, exposure=exposure, sca=7,
                  reference_product_key="sha256:" + "c" * 64)
    kwargs.update(overrides)
    return identity.difference_image_key(**kwargs)


def _counts(conn, product_key):
    """`(products, artifacts, bindings)` for one product key."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM products WHERE product_key = %s",
                    [product_key])
        products = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM product_artifacts pa"
            " JOIN products p ON p.product_id = pa.product_id"
            " WHERE p.product_key = %s", [product_key])
        bindings = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM artifacts a"
            " JOIN product_artifacts pa ON pa.artifact_id = a.artifact_id"
            " JOIN products p ON p.product_id = pa.product_id"
            " WHERE p.product_key = %s", [product_key])
        artifacts = cur.fetchone()[0]
    return products, artifacts, bindings


def _register(conn, repository, product_key, payload, attempt_id,
              record_sequence, name="sfft_diffimage", checksum=None):
    """One registration of one product, as the production path does it."""
    product = repository.upsert_product(
        product_key=product_key,
        product_class=identity.CLASS_DIFFERENCE_IMAGE,
        role=payload["role"], identity_payload=payload,
        serialization_version=identity.SERIALIZATION_VERSION,
        process_family=SPEC["process_family"])
    artifact = repository.upsert_artifact(
        attempt_id=attempt_id, record_sequence=record_sequence,
        published_name=name,
        uri=f"s3://bucket/{fixture.RUN_TAG}/{attempt_id}/{name}.fits",
        checksum=checksum or ("d" * 64))
    repository.bind(product.product_id, artifact.artifact_id,
                    legacy_pid=None, legacy_version=None)
    return product, artifact


# ---------------------------------------------------------------------------
# (a) replay
# ---------------------------------------------------------------------------


def test_replay_of_the_same_attempt_and_sequence_writes_nothing_new(conn):
    """Case (a): a replayed registration adds no rows anywhere.

    The registration consumer re-runs a candidate whenever its watermark
    write did not commit, which is a normal outcome of a crash between the
    rows and the watermark — so this path runs in production, not only in a
    test, and a second set of rows for one registration would double-count
    every product the pipeline has ever made.
    """
    _require_schema(conn)
    repository = _repository(conn)
    attempt_id = fixture.make_attempt(
        conn, lifecycle="terminal_without_start",
        terminal_record_sequence=1)
    key, payload = _key_for(_unique_exposure())

    _register(conn, repository, key, payload, attempt_id, 1)
    before = _counts(conn, key)

    _register(conn, repository, key, payload, attempt_id, 1)
    after = _counts(conn, key)

    assert before == after == (1, 1, 1)
    conn.rollback()


# ---------------------------------------------------------------------------
# (b) retry under a new attempt
# ---------------------------------------------------------------------------


def test_retry_under_a_new_attempt_keeps_the_product_and_moves_the_binding(
        conn):
    """Case (b): same product, new artifact, binding repointed.

    This is the case the whole separation exists for. Before rule 10 a retry
    had to mint a new PRODUCT row — because the bytes lived on the product —
    so the operations tables counted executions rather than products. Here
    the product row is untouched, the new bytes get their own artifact, and
    the binding moves.
    """
    _require_schema(conn)
    repository = _repository(conn)
    key, payload = _key_for(_unique_exposure(1))

    first_attempt = fixture.make_attempt(
        conn, lifecycle="terminal_without_start",
        terminal_record_sequence=1)
    product_a, artifact_a = _register(conn, repository, key, payload,
                                      first_attempt, 1, checksum="d" * 64)

    second_attempt = fixture.make_attempt(
        conn, lifecycle="terminal_without_start",
        terminal_record_sequence=1)
    product_b, artifact_b = _register(conn, repository, key, payload,
                                      second_attempt, 1, checksum="e" * 64)

    # CRITERION 1, IN ITS LITERAL FORM. The determinism tests in
    # `test_product_identity.py` demonstrate "same components → same key" by
    # computing the key twice, which is by-construction: `run_id` and
    # `attempt_id` are not parameters of any key function, so there is no
    # argument through which an execution context could enter. The brief's
    # LETTER asks for two different attempts, and this is where that is
    # demonstrated end to end — two distinct `attempt_id`s, each registering
    # through the real repository, resolving to ONE product row because their
    # identity components agree.
    assert first_attempt != second_attempt, (
        "the two registrations must be under distinct attempts for this to "
        "demonstrate anything about identity across attempts")
    assert product_a.product_key == product_b.product_key == key

    # ONE product, TWO artifacts.
    assert product_a.product_id == product_b.product_id
    assert artifact_a.artifact_id != artifact_b.artifact_id
    products, artifacts, bindings = _counts(conn, key)
    assert products == 1
    assert artifacts == 2
    assert bindings == 2

    # Exactly one CURRENT binding, and it is the new one — which is what
    # preserves `vbest` semantics for the legacy consumers: the row the
    # binding names is the row `vbest = 1` points at.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT artifact_id FROM product_artifacts"
            " WHERE product_id = %s AND is_current", [product_a.product_id])
        current = cur.fetchall()
    assert current == [(artifact_b.artifact_id,)]
    conn.rollback()


def test_byte_identical_retry_still_writes_a_new_artifact(conn):
    """Attempt-scoped means attempt-scoped, even for identical bytes.

    Stated as its own test because it is the part that looks wrong at first
    glance. An artifact records a PUBLICATION EVENT — this attempt put these
    bytes at this address — and two attempts are two events even when the
    checksums agree. Deduplicating them would lose the record that the
    second attempt ran at all.
    """
    _require_schema(conn)
    repository = _repository(conn)
    key, payload = _key_for(_unique_exposure(2))
    checksum = "f" * 64

    first = fixture.make_attempt(
        conn, lifecycle="terminal_without_start",
        terminal_record_sequence=1)
    second = fixture.make_attempt(
        conn, lifecycle="terminal_without_start",
        terminal_record_sequence=1)
    _, artifact_a = _register(conn, repository, key, payload, first, 1,
                              checksum=checksum)
    _, artifact_b = _register(conn, repository, key, payload, second, 1,
                              checksum=checksum)

    assert artifact_a.artifact_id != artifact_b.artifact_id
    assert artifact_a.checksum == artifact_b.checksum == checksum
    conn.rollback()


# ---------------------------------------------------------------------------
# (c) a changed identity component
# ---------------------------------------------------------------------------


def test_changing_one_identity_component_makes_a_new_product(conn):
    """Case (c): a different release digest is a different product."""
    _require_schema(conn)
    repository = _repository(conn)
    exposure = _unique_exposure(3)
    key_a, payload_a = _key_for(exposure)
    key_b, payload_b = _key_for(exposure, release_digest="9" * 64)
    assert key_a != key_b

    attempt = fixture.make_attempt(
        conn, lifecycle="terminal_without_start",
        terminal_record_sequence=1)
    product_a, _ = _register(conn, repository, key_a, payload_a, attempt, 1)
    other = fixture.make_attempt(
        conn, lifecycle="terminal_without_start",
        terminal_record_sequence=1)
    product_b, _ = _register(conn, repository, key_b, payload_b, other, 1)

    assert product_a.product_id != product_b.product_id
    conn.rollback()


# ---------------------------------------------------------------------------
# The constraints themselves, asserted against the schema
# ---------------------------------------------------------------------------


def _constraint_def(conn, name):
    with conn.cursor() as cur:
        cur.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint"
                    " WHERE conname = %s", [name])
        row = cur.fetchone()
    return row[0] if row else None


def test_product_key_is_unique_in_the_database(conn):
    """The UNIQUE constraint exists, and it refuses a duplicate.

    Both halves: the catalog says the constraint is there, and a real
    concurrent-shaped duplicate insert raises. The catalog half alone could
    pass against a constraint on the wrong column; the behavioural half
    alone could pass against a Python guard.
    """
    _require_schema(conn)
    definition = _constraint_def(conn, "products_product_key_uq")
    assert definition is not None
    assert "product_key" in definition
    assert definition.upper().startswith("UNIQUE")

    key, payload = _key_for(_unique_exposure(4))
    repository = _repository(conn)
    repository.upsert_product(
        product_key=key, product_class=identity.CLASS_DIFFERENCE_IMAGE,
        role=payload["role"], identity_payload=payload,
        serialization_version=identity.SERIALIZATION_VERSION,
        process_family=SPEC["process_family"])

    # A RAW insert, bypassing the repository's ON CONFLICT, so the database
    # is what refuses rather than the upsert absorbing it.
    import psycopg2

    with pytest.raises(psycopg2.errors.UniqueViolation):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO products (product_key, product_class, role,"
                " identity_payload, serialization_version, process_family)"
                " VALUES (%s, %s, %s, %s::jsonb, %s, %s)",
                [key, identity.CLASS_DIFFERENCE_IMAGE, payload["role"],
                 identity.canonical_json(payload),
                 identity.SERIALIZATION_VERSION, SPEC["process_family"]])
    conn.rollback()


def test_artifact_replay_uniqueness_is_a_database_constraint(conn):
    """`artifacts_replay_uq` exists and covers the three replay columns."""
    _require_schema(conn)
    definition = _constraint_def(conn, "artifacts_replay_uq")
    assert definition is not None
    for column in ("attempt_id", "record_sequence", "published_name"):
        assert column in definition

    repository = _repository(conn)
    attempt = fixture.make_attempt(
        conn, lifecycle="terminal_without_start",
        terminal_record_sequence=1)
    repository.upsert_artifact(
        attempt_id=attempt, record_sequence=1, published_name="dup",
        uri="s3://bucket/a.fits", checksum="a" * 64)

    import psycopg2

    with pytest.raises(psycopg2.errors.UniqueViolation):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO artifacts (attempt_id, record_sequence,"
                " published_name, uri, checksum_algorithm, checksum)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                [attempt, 1, "dup", "s3://bucket/b.fits", "sha256", "b" * 64])
    conn.rollback()


def test_exactly_one_current_binding_per_product_is_enforced(conn):
    """The partial unique index refuses a second current binding."""
    _require_schema(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT indexdef FROM pg_indexes"
                    " WHERE indexname = 'product_artifacts_one_current_uq'")
        row = cur.fetchone()
    assert row is not None
    assert "UNIQUE" in row[0].upper()
    assert "is_current" in row[0]

    repository = _repository(conn)
    key, payload = _key_for(_unique_exposure(5))
    attempt = fixture.make_attempt(
        conn, lifecycle="terminal_without_start",
        terminal_record_sequence=1)
    product, artifact = _register(conn, repository, key, payload, attempt, 1)
    second = repository.upsert_artifact(
        attempt_id=attempt, record_sequence=1, published_name="second",
        uri="s3://bucket/second.fits", checksum="c" * 64)

    import psycopg2

    with pytest.raises(psycopg2.errors.UniqueViolation):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO product_artifacts (product_id, artifact_id,"
                " is_current) VALUES (%s, %s, true)",
                [product.product_id, second.artifact_id])
    conn.rollback()


def test_legacy_tables_carry_a_foreign_key_to_products(conn):
    """`refimages`/`diffimages` bind to the product without being it."""
    _require_schema(conn)
    for name in ("refimages_product_fk", "diffimages_product_fk"):
        definition = _constraint_def(conn, name)
        assert definition is not None, f"{name} is absent"
        assert "products" in definition
        assert definition.upper().startswith("FOREIGN KEY")


def test_artifact_foreign_key_to_attempts_is_enforced(conn):
    """An artifact cannot name an attempt that does not exist."""
    _require_schema(conn)
    import psycopg2

    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO artifacts (attempt_id, record_sequence,"
                " published_name, uri, checksum_algorithm, checksum)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                [-999999, 1, "orphan", "s3://bucket/x.fits", "sha256",
                 "a" * 64])
    conn.rollback()
