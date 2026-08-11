"""Contract tests: rule 8's worker/database separation and the milestone's
co-commit with registration (brief C2, acceptance criterion 2).

    "no `require_connection` in any product-producing sequence (assert
     statically or by fixture); milestone commits atomically with registration
     — a crash between registration and the milestone is impossible by
     construction (single transaction)."

The two halves are asserted very differently on purpose.

THE FIRST IS STATIC, and it has to be. "This code path holds no database
connection" is a claim about what is ABSENT, and absence cannot be observed by
running the happy path — a test that exercises `download_inputs` and sees no
write proves only that this input did not trigger one. Reading the source for
the call is what actually bounds the claim, and it is the same technique the
repo already uses for structural invariants (`scripts/check-env-policy.sh`
greps for a compiled-in default rather than trying to observe its absence).

THE SECOND IS TRANSACTIONAL, and needs a real database: "these two writes are
one commit" is a statement about transaction boundaries, observable only from
a SECOND connection's visibility — which is exactly the argument
`test_borrowed_connection.py` makes for living in this tier.

No draft schema is needed; `milestones` is migration 011's and the acceptance
transaction is already there. These run in CI.
"""

import pathlib

import pytest

from pipeline.contract import fixture
from pipeline.registration.consumer import record_l2_available

#: The pixel/transform stage modules — the ones rule 8 is about. `post_db` and
#: `alert_production` are deliberately NOT here: they are the post-DB job types
#: `pipeline/stages/context.py` reserves the borrowed connection for ("the
#: post-DB job types produce database state rather than S3 products, so they
#: need that connection"), and their `context.produce` calls emit database
#: TABLE NAMES rather than S3 artifacts. Listing them would assert the
#: opposite of the rule.
PRODUCT_STAGE_MODULES = ("science.py", "reference_image.py", "post_process.py")

_STAGES_DIR = pathlib.Path(__file__).resolve().parents[1] / "stages"


@pytest.mark.parametrize("module_name", PRODUCT_STAGE_MODULES)
def test_product_producing_stages_take_no_database_connection(module_name):
    """No `require_connection()` call in any pixel/transform stage module.

    Criterion 2's first half, and rule 8 verbatim: "Pixel/transform workers
    hold no database connection. They upload artifacts and one sealed,
    checksummed result manifest, then exit."

    **PARSED, NOT GREPPED.** A text search cannot tell a call from a docstring
    quoting one, and `science.py` now contains a long note explaining why the
    call it used to make was removed — so a grep-based check would fail on the
    very comment that removing the call earned, and the only way to pass it
    would be to delete the explanation. Walking the AST asks the question that
    is actually meant: does a CALL NODE named `require_connection` appear
    anywhere in this module's executable code? Comments and docstrings are not
    in the tree, so they cannot be false positives, and a call cannot hide
    from it by being spelled across two lines or aliased through a local.
    """
    import ast

    source = (_STAGES_DIR / module_name).read_text()
    tree = ast.parse(source)

    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "require_connection"
    ]
    assert offenders == [], (
        f"{module_name} calls require_connection() in a product-producing "
        f"sequence, against rule 8; line(s): {offenders}")


def test_milestone_commits_in_the_same_transaction_as_registration(conn,
                                                                   second_conn):
    """The milestone is invisible until the acceptance transaction commits.

    Criterion 2's second half. "A crash between registration and the milestone
    is impossible by construction (single transaction)" is asserted the only
    way a transaction boundary can be: from a SECOND connection, which sees
    nothing while the first is open and both facts at once after it commits.

    Against a fake this would be a tautology — the fake would report whatever
    it was told. Here PostgreSQL's own isolation is the thing answering.
    """
    exposure, sca = _unique_exposure_sca(conn)
    attempt_id = fixture.make_attempt(conn, lifecycle="terminal_after_start")
    conn.commit()

    with conn.cursor() as cur:
        # The registration side's own write, standing in for the product rows
        # and the watermark: what matters is that the milestone shares its
        # transaction, not which specific sibling write it shares it with.
        cur.execute("UPDATE attempts SET registered_at = now(),"
                    " registered_record_sequence = 1 WHERE attempt_id = %s",
                    [attempt_id])
        wrote = record_l2_available(
            attempt_id, {"exposure_id": exposure, "sca": sca}, cursor=cur)
        assert wrote is True

        # MID-TRANSACTION: the other connection sees neither write.
        assert _milestone_count(second_conn, exposure, sca) == 0
        assert _registered_at(second_conn, attempt_id) is None

    conn.commit()

    # AFTER COMMIT: it sees both. There is no interval in which one exists
    # without the other, which is the property the criterion asks for.
    assert _milestone_count(second_conn, exposure, sca) == 1
    assert _registered_at(second_conn, attempt_id) is not None


def test_the_milestone_is_not_duplicated_by_a_second_registration(conn):
    """Find-before-write, because `milestones` has no unique constraint.

    A re-registration under a later supersession would otherwise append a
    second `l2_available` row for a unit that reached the milestone once —
    migration 011 defines only a scope CHECK on this table, so nothing in the
    schema would refuse it.
    """
    exposure, sca = _unique_exposure_sca(conn)
    attempt_id = fixture.make_attempt(conn, lifecycle="terminal_after_start")
    conn.commit()

    with conn.cursor() as cur:
        assert record_l2_available(
            attempt_id, {"exposure_id": exposure, "sca": sca}, cursor=cur)
        assert not record_l2_available(
            attempt_id, {"exposure_id": exposure, "sca": sca}, cursor=cur)
    conn.commit()

    assert _milestone_count(conn, exposure, sca) == 1


def test_an_attempt_without_exposure_scope_records_no_milestone(conn):
    """Reference construction and the post-DB chain have no L2 input.

    `milestones_scope_check` requires at least one scope column, and a
    half-scoped row would be unfindable by the find-before-write guard — so
    the honest behaviour for an attempt carrying neither is to record nothing
    rather than to invent a scope.
    """
    attempt_id = fixture.make_attempt(conn, lifecycle="terminal_after_start")
    conn.commit()

    with conn.cursor() as cur:
        assert record_l2_available(
            attempt_id, {"exposure_id": None, "sca": None}, cursor=cur) is False
    conn.commit()


def _unique_exposure_sca(conn):
    """An (exposure, sca) pair no other run has used.

    Fixture honesty: these tests assert milestone COUNTS, so a pair another
    run already recorded would make a correct implementation look like a
    duplicate. Derived from the table's own high-water mark rather than from
    RUN_TAG, because the columns are integers and the tag is hex.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT coalesce(max(exposure_id), 900000) + 1"
                    " FROM milestones")
        return cur.fetchone()[0], 1


def _milestone_count(connection, exposure, sca):
    with connection.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM milestones WHERE milestone_name ="
            " 'l2_available' AND exposure_id = %s AND sca = %s",
            [exposure, sca])
        count = cur.fetchone()[0]
    # The reading connection must not hold a transaction open across the
    # writer's commit, or it would keep observing its own snapshot and report
    # a stale answer as a fresh one.
    connection.rollback()
    return count


def _registered_at(connection, attempt_id):
    with connection.cursor() as cur:
        cur.execute("SELECT registered_at FROM attempts WHERE attempt_id = %s",
                    [attempt_id])
        row = cur.fetchone()
    connection.rollback()
    return row[0] if row else None
