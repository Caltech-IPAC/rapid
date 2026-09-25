"""Attempt dispositions and selection for the database tests, as the launcher records them.

The stage tests run a stage under an attempt that is only allocated; the
launcher's reconcile later records its disposition and, on success,
selects it. Supervisor step 9 rulings R1 (a done check reuses only this
attempt's set or a succeeded attempt's) and R2 (another run's result set is
readable only when its producing attempt is selected) make both visible to
the stages, so the tests set them explicitly.
"""

from __future__ import annotations

from rapidpipe.runs import repository as repo

EXEC_RECORD = {"source_revision": "abc123", "schema_version": "1",
               "settings_hash": "sha256:xyz"}


def set_disposition(conn, attempt_id: str, disposition: str | None) -> None:
    """Set an attempt's disposition directly (the reconcile's column, no other effect)."""
    exit_code = None if disposition is None else (0 if disposition == "succeeded" else 1)
    with conn.cursor() as cur:
        cur.execute("UPDATE attempts SET disposition = %s, exit_code = %s, ended = now() "
                    "WHERE id = %s", (disposition, exit_code, attempt_id))
        assert cur.rowcount == 1


def succeed_and_select(conn, attempt_id: str) -> None:
    """Record ``attempt_id`` as succeeded and select it, through the repository."""
    with conn.cursor() as cur:
        cur.execute("SELECT output_location FROM attempts WHERE id = %s", (attempt_id,))
        (output_location,) = cur.fetchone()
    repo.record_attempt_result(
        conn, attempt_id, exit_code=0, disposition="succeeded",
        output_location=output_location,
        execution_record=EXEC_RECORD, scheduler_job_id=None)
    repo.select_attempt(conn, attempt_id)
