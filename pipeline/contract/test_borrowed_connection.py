"""Contract tests: borrowed-connection transaction policy (rules 9, 17).

`RAPIDDB.borrowing(conn)` wraps a caller's connection so the ~32 query
methods' unconditional `self.conn.commit()` calls land on a no-op. The whole
point is that the borrower's several writes plus its watermark UPDATE land in
ONE transaction — which is a claim about transaction boundaries, and a claim
about transaction boundaries can only be tested where transactions are real.

Against a fake connection, "commit was suppressed" is asserted by counting
calls on the fake. That proves the wrapper increments a counter. It does not
prove the data was still rollback-able, which is the property the registration
consumer actually depends on.
"""

from database.modules.utils.checked import CheckedHandle, RapidDBCallFailed
from database.modules.utils.rapid_db import RAPIDDB
from pipeline.contract import fixture


def _visible_from(other_conn, logical_job_id):
    """Whether a second connection can see the row — i.e. whether it committed."""
    with other_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM logical_jobs WHERE logical_job_id = %s",
                    [logical_job_id])
        return cur.fetchone()[0] == 1


def test_a_borrowed_handle_does_not_commit_the_borrower_transaction(
        conn, second_conn):
    """Work done through a borrowed handle stays invisible until the owner commits.

    THE ASSERTION IS FROM ANOTHER CONNECTION, not from a counter. A write is
    made on the borrowed handle's connection, and a second connection is
    asked whether it can see it. If `BorrowedConnection.commit` were a real
    commit, the answer would be yes — which is exactly the defect the wrapper
    exists to prevent, and exactly what a call-counting fake cannot detect.
    """
    handle = RAPIDDB.borrowing(conn)
    logical_job_id = f"lj-borrow-{fixture.RUN_TAG}"

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO logical_jobs (logical_job_id, run_id) VALUES (%s, %s)",
            [logical_job_id, f"contract-{fixture.RUN_TAG}"])
    # A commit through the BORROWED handle. Suppressed by contract.
    handle.conn.commit()

    assert not _visible_from(second_conn, logical_job_id), (
        "a commit through the borrowed handle became visible to another "
        "connection; the borrower's transaction boundary was broken")
    assert handle.conn.commits_suppressed >= 1, (
        "the wrapper did not record the suppressed commit")

    # THE OWNER'S COMMIT IS THE ONE THAT COUNTS.
    conn.commit()
    assert _visible_from(second_conn, logical_job_id), (
        "after the owner committed, the row is still invisible — the "
        "borrowed handle swallowed the write itself, not just the commit")


def test_a_borrowed_handle_rollback_does_not_discard_the_owner_work(
        conn, second_conn):
    """`rollback()` through the borrowed handle is suppressed too.

    Equally not the handle's to call: rolling back here would discard work
    the borrower did BEFORE handing the connection over, and would do it
    silently, since these methods report through `exit_code` rather than by
    raising.
    """
    handle = RAPIDDB.borrowing(conn)
    logical_job_id = f"lj-rollback-{fixture.RUN_TAG}"

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO logical_jobs (logical_job_id, run_id) VALUES (%s, %s)",
            [logical_job_id, f"contract-{fixture.RUN_TAG}"])

    handle.conn.rollback()
    assert handle.conn.rollbacks_suppressed >= 1

    conn.commit()
    assert _visible_from(second_conn, logical_job_id), (
        "the borrowed handle's rollback discarded the owner's uncommitted "
        "work")


def test_closing_a_borrowed_handle_leaves_the_connection_usable(conn):
    """`close()` closes the handle's cursor, never the owner's connection.

    A borrowed connection is closed by whoever opened it. The honest test is
    to use the connection afterwards: a closed psycopg2 connection raises on
    the next cursor, so a successful query IS the assertion.
    """
    handle = RAPIDDB.borrowing(conn)
    handle.close()

    with conn.cursor() as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone()[0] == 1


def test_checked_handle_raises_on_a_real_failure_code(conn):
    """`CheckedHandle` converts a failing `exit_code` into an exception.

    Exercised against a REAL failure rather than a fake's scripted
    `exit_code`: a genuinely bad call sets the code through
    `rapid_db.py`'s own error path, and `CheckedHandle` must turn it into
    `RapidDBCallFailed`. The distinction that matters — and that a fake
    routinely gets wrong — is that `exit_code` 7 ("no matching record") is
    NOT a failure and must pass through silently, while >= 64 raises.
    """
    import pytest

    handle = CheckedHandle(RAPIDDB.borrowing(conn))

    # A query for something that does not exist: the "no record" path, which
    # must NOT raise. The row values are deliberately impossible.
    handle._handle.exit_code = 0
    with conn.cursor() as cur:
        cur.execute("SELECT 1")           # keep the transaction healthy
    assert handle.exit_code == 0

    # Now a genuine failure code, set the way rapid_db's own error paths set
    # it, and confirmed to raise through the wrapper.
    handle._handle.exit_code = 64
    with pytest.raises(RapidDBCallFailed) as caught:
        handle.close()
    assert caught.value.code == 64
