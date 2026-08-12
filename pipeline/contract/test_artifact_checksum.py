"""Artifact checksum integrity (brief D, criterion 5).

"An artifact row round-trips all 64 hex chars of a real SHA-256 (fixture
strings >= 64 chars; algorithm recorded)."

**THE DEFECT THIS IS MEASURED AGAINST IS LIVE IN THE SCHEMA.**
`refimages.checksum` and `diffimages.checksum` are `character varying(32)`
(`006-core-tables.sql:393,448`), and the pipeline computes SHA-256 in hex —
64 characters (`pipeline/stages/publishing.py:_digest`). So every product
checksum the pipeline has ever written to those columns was silently
truncated to its first 32 characters. That is not a cosmetic loss: a
truncated digest compares EQUAL for any two files sharing a 128-bit prefix,
and the comparison that would catch corrupted bytes succeeds instead.

Brief D flags that as a latent defect and a candidate change request, and
requires the artifact table to simply do it right. These tests assert the
"right", and one of them asserts the legacy defect still exists — so that
when someone widens those columns, a test tells them this note is stale
rather than leaving the claim to rot.
"""

import hashlib

import pytest

from pipeline.contract import fixture
from pipeline.registration import artifacts as artifact_layer


def _require_schema(conn):
    if not fixture.has_table(conn, "artifacts"):
        pytest.skip("DRAFT migration 048 is not applied on this database")


#: A REAL SHA-256, computed rather than typed, so the fixture cannot be a
#: 64-character string that no hash function would ever produce.
REAL_DIGEST = hashlib.sha256(b"rapid brief D artifact checksum").hexdigest()


def test_the_fixture_is_a_real_sha256_of_the_right_length():
    """The fixture's own precondition, asserted rather than assumed."""
    assert len(REAL_DIGEST) == 64
    assert set(REAL_DIGEST) <= set("0123456789abcdef")


def test_an_artifact_round_trips_all_64_hex_characters(conn):
    """Write a real digest, read it back, compare every character."""
    _require_schema(conn)
    from pipeline.repositories.products import ProductRepository

    repository = ProductRepository(conn)
    attempt = fixture.make_attempt(conn, terminal_record_sequence=1)
    written = repository.upsert_artifact(
        attempt_id=attempt, record_sequence=1,
        published_name="checksum-roundtrip",
        uri="s3://bucket/roundtrip.fits", checksum=REAL_DIGEST,
        size_bytes=1234, content_type="difference_image")

    assert written.checksum == REAL_DIGEST

    with conn.cursor() as cur:
        cur.execute("SELECT checksum, checksum_algorithm, size_bytes,"
                    " content_type FROM artifacts WHERE artifact_id = %s",
                    [written.artifact_id])
        checksum, algorithm, size, content_type = cur.fetchone()

    assert checksum == REAL_DIGEST, (
        f"the stored checksum is {len(checksum)} characters, not 64; the "
        f"artifact column has the legacy varchar(32) truncation it exists "
        f"to avoid")
    assert algorithm == "sha256"
    assert size == 1234
    assert content_type == "difference_image"
    conn.rollback()


def test_the_algorithm_is_recorded_not_implied(conn):
    """`checksum_algorithm` is a real column with a real value.

    A checksum whose algorithm is implied by convention cannot be
    re-verified once the convention changes, and "it is 64 hex characters
    so it must be SHA-256" is a convention, not a record.
    """
    _require_schema(conn)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name = 'artifacts'"
            "   AND column_name = 'checksum_algorithm'")
        assert cur.fetchone() is not None
    assert artifact_layer.CHECKSUM_ALGORITHM == "sha256"


def test_a_truncated_checksum_is_refused_by_the_database(conn):
    """The CHECK constraint refuses a 32-character digest.

    This is the guarantee. If the truncation ever reaches this table —
    through a caller that read a legacy column and forwarded it — the row
    fails to insert rather than being stored as a half-digest that compares
    equal to the wrong bytes.
    """
    _require_schema(conn)
    import psycopg2

    attempt = fixture.make_attempt(conn, terminal_record_sequence=1)
    with pytest.raises(psycopg2.errors.CheckViolation):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO artifacts (attempt_id, record_sequence,"
                " published_name, uri, checksum_algorithm, checksum)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                [attempt, 1, "truncated", "s3://bucket/t.fits", "sha256",
                 REAL_DIGEST[:32]])
    conn.rollback()


def test_a_truncated_checksum_is_refused_before_the_database(conn):
    """The application refuses it too, naming the published file.

    Both layers, deliberately: the constraint is the guarantee, and this is
    the message. A constraint violation names a table and a constraint; the
    operator needs to know WHICH published product carried the bad digest.
    """
    _require_schema(conn)
    with pytest.raises(artifact_layer.ArtifactRecordingError) as raised:
        artifact_layer._entry_checksum({"checksum": REAL_DIGEST[:32]},
                                       "sfft_diffimage")
    assert "sfft_diffimage" in str(raised.value)


def test_a_missing_checksum_is_refused(conn):
    """An artifact asserts bytes are durable AND verifiable."""
    _require_schema(conn)
    with pytest.raises(artifact_layer.ArtifactRecordingError):
        artifact_layer._entry_checksum({}, "sfft_diffimage")


def test_the_legacy_columns_still_truncate_and_this_is_known(conn):
    """The flagged latent defect, asserted so the flag cannot go stale.

    Brief D puts fixing this OUT of scope — the artifact table does it
    right, and widening a live column is its own change request. This test
    records the defect's existence rather than its absence, so whoever
    eventually widens `refimages.checksum` sees this test fail and knows to
    retire the note instead of discovering the claim was untrue years later.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, character_maximum_length"
            " FROM information_schema.columns"
            " WHERE table_name IN ('refimages', 'diffimages')"
            "   AND column_name = 'checksum'"
            " ORDER BY table_name")
        rows = cur.fetchall()

    assert rows, "neither legacy product table was found"
    for table_name, maximum in rows:
        assert maximum == 32, (
            f"{table_name}.checksum is now varchar({maximum}) rather than "
            f"varchar(32) — the truncation brief D flagged has been fixed, "
            f"so this test and the notes citing it are stale and should be "
            f"retired")
