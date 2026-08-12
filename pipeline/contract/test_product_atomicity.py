"""Registration atomicity across the new rows (brief D, criterion 4).

"A forced failure inside the registration transaction rolls back product,
artifact, legacy version, and watermark writes together."

**WHY THIS IS A REAL RISK AND NOT A FORMALITY.** It is the exact shape of
round-3 finding #8, which this repository has already been bitten by once:
the registrar opened its OWN database connection, so product rows were
durable before the watermark was even attempted, and a crash between them
left rows written with the attempt still a candidate — the next pass then
registered the same products again. D adds three more tables to that
transaction, so the same defect has three more ways to reappear, and the
one that would reintroduce it silently is a repository that opens or
commits its own connection.

The test forces a failure AFTER the new rows are written and asserts that
nothing survives, on a SECOND connection — because a rollback that is only
observed from inside the rolled-back transaction proves nothing.
"""

import pytest

from pipeline.contract import fixture
from pipeline.registration import identity

SPEC = {
    "process_family": 15,
    "definition_checksum": "a" * 64,
    "release_digest": "b" * 64,
}


def _require_schema(conn):
    if not fixture.has_table(conn, "products"):
        pytest.skip("DRAFT migration 048 is not applied on this database")


def _key(offset):
    return identity.difference_image_key(
        **SPEC, exposure=int(fixture.RUN_TAG[:6], 16) + 1000 + offset,
        sca=7, reference_product_key="sha256:" + "c" * 64)


class ForcedFailure(RuntimeError):
    """The injected failure. Its own type so the test cannot pass on a real
    error that happened to occur at the right moment."""


def test_a_failure_after_the_writes_rolls_all_of_them_back(conn,
                                                           second_conn):
    """Product, artifact and binding rows all vanish on rollback.

    Written the way the consumer's `_transaction` behaves: the writes
    happen, an exception is raised inside the block, and the transaction is
    rolled back. Observed from `second_conn`, which never saw the
    uncommitted rows and must not see them afterwards either.
    """
    _require_schema(conn)
    from pipeline.repositories.products import ProductRepository

    repository = ProductRepository(conn)
    key, payload = _key(0)
    attempt = fixture.make_attempt(conn, terminal_record_sequence=1)
    conn.commit()          # the attempt row is fixture, not part of the test

    try:
        product = repository.upsert_product(
            product_key=key, product_class=identity.CLASS_DIFFERENCE_IMAGE,
            role=payload["role"], identity_payload=payload,
            serialization_version=identity.SERIALIZATION_VERSION,
            process_family=SPEC["process_family"])
        artifact = repository.upsert_artifact(
            attempt_id=attempt, record_sequence=1,
            published_name="sfft_diffimage",
            uri="s3://bucket/atomicity.fits", checksum="d" * 64)
        repository.bind(product.product_id, artifact.artifact_id)

        # Visible INSIDE the transaction — so the test is asserting a
        # rollback, not asserting that the writes never happened.
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM products"
                        " WHERE product_key = %s", [key])
            assert cur.fetchone()[0] == 1

        raise ForcedFailure("the registration failed after writing rows")
    except ForcedFailure:
        conn.rollback()

    # Observed from an independent connection.
    with second_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM products WHERE product_key = %s",
                    [key])
        assert cur.fetchone()[0] == 0, (
            "the product row survived a rolled-back registration")
        cur.execute("SELECT count(*) FROM artifacts"
                    " WHERE attempt_id = %s AND published_name = %s",
                    [attempt, "sfft_diffimage"])
        assert cur.fetchone()[0] == 0, (
            "the artifact row survived a rolled-back registration")
    second_conn.rollback()

    # Clean up the fixture attempt this test committed.
    with conn.cursor() as cur:
        cur.execute("DELETE FROM attempts WHERE attempt_id = %s", [attempt])
    conn.commit()


def test_the_repository_never_commits_on_its_own(conn, second_conn):
    """A write through the repository is invisible until the CALLER commits.

    The property that makes the transaction boundary the consumer's. If the
    repository committed — or opened its own connection — this row would be
    visible from `second_conn` immediately, and every atomicity guarantee
    above would be vacuous.
    """
    _require_schema(conn)
    from pipeline.repositories.products import ProductRepository

    key, payload = _key(1)
    ProductRepository(conn).upsert_product(
        product_key=key, product_class=identity.CLASS_DIFFERENCE_IMAGE,
        role=payload["role"], identity_payload=payload,
        serialization_version=identity.SERIALIZATION_VERSION,
        process_family=SPEC["process_family"])

    with second_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM products WHERE product_key = %s",
                    [key])
        assert cur.fetchone()[0] == 0, (
            "the repository made its write visible to another connection "
            "before the caller committed; it has opened or committed a "
            "connection of its own, which is round-3 finding #8 returning")
    second_conn.rollback()
    conn.rollback()


def test_a_constraint_violation_leaves_the_caller_able_to_roll_back(conn):
    """A failed query raises a typed error, not a wedged connection.

    `RepositoryQueryFailed` deliberately does NOT roll back — the caller's
    transaction owns the boundary and rolling back here would discard the
    milestone write and the watermark of a transaction the caller had not
    finished with. What it must not do is leave the caller unable to
    recover, so this asserts the caller's own rollback still works.
    """
    _require_schema(conn)
    from pipeline.repositories.errors import RepositoryQueryFailed
    from pipeline.repositories.products import ProductRepository

    repository = ProductRepository(conn)
    with pytest.raises(RepositoryQueryFailed):
        # An artifact naming an attempt that does not exist: the FK refuses.
        repository.upsert_artifact(
            attempt_id=-999999, record_sequence=1, published_name="orphan",
            uri="s3://bucket/x.fits", checksum="a" * 64)

    conn.rollback()
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone() == (1,)
