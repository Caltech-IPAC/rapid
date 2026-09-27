"""The SQL ``rapidpipe.cli.runctl`` reads (``_run_row``, ``_unit_row``,
``_status_rows``, ``_compare_units``, ``_compare_instances``,
``_registered_instances``) against a real PostgreSQL with the migrations
applied. The unit tests replace these functions; this checks the queries
themselves. Skips cleanly if PGHOST is unset (see conftest.py)."""

from __future__ import annotations

from rapidpipe.cli import runctl
from rapidpipe.db.ids import new_ulid
from rapidpipe.runs import inputs
from rapidpipe.runs import repository as repo
from rapidpipe.runs.slots import canonical_json

from .test_repository import (
    TEST_KIND,
    _make_run,
    _make_unit,
    _register_simple_instance,
    _succeed_and_select,
    stand_in_slot,
)


def test_run_row_and_unit_row(conn):
    run_id = _make_run(conn, kind="scratch", selected_stages=["admit", "register", "difference"],
                       settings_overlay_ref="s3://settings/overlay.toml")
    row = runctl._run_row(conn, run_id)
    assert row == runctl.RunRow("scratch", ["admit", "register", "difference"], "open",
                                "s3://settings/overlay.toml")
    assert runctl._run_row(conn, new_ulid()) is None

    stage, unit_id = _make_unit(conn, run_id)
    assert runctl._unit_row(conn, run_id, stage, unit_id) == runctl.UnitRow(
        "pending", None, None, None, None, None)
    assert runctl._unit_row(conn, run_id, stage, "no-such-unit") is None

    first = repo.allocate_attempt(conn, run_id, stage, unit_id, outputs_root="s3://b/p")
    row = runctl._unit_row(conn, run_id, stage, unit_id)
    assert (row.state, row.last_attempt, row.last_disposition) == ("running", first, None)
    assert row.last_output == f"s3://b/p/runs/{run_id}/{stage}/{unit_id}/{first}"


def test_status_rows_and_compare(conn):
    run_a = _make_run(conn, kind="scratch")
    run_b = _make_run(conn, kind="scratch")
    key = {"unit": "e001/SCA01"}
    instances = {}
    for run_id in (run_a, run_b):
        stage, unit_id = _make_unit(conn, run_id)
        attempt = _succeed_and_select(conn, run_id, stage, unit_id)
        instances[run_id] = _register_simple_instance(
            conn, run_id, stage, attempt, logical_key=key)

    status = runctl._status_rows(conn, run_a)
    assert len(status) == 1
    stage, unit_id, state, selected, last, job, disposition = status[0]
    assert (stage, unit_id, state, disposition, job) == (
        "difference", "e001/SCA01", "complete", "succeeded", "batch-job-1")
    assert selected == last

    assert runctl._compare_units(conn, run_a) == [
        ("difference", "e001/SCA01", "succeeded", "sha256:xyz")]
    rows = runctl._compare_instances(conn, run_a)
    assert [(k, i) for k, _key, i, _slot in rows] == [(TEST_KIND, instances[run_a])]
    assert rows[0][1] == runctl._compare_instances(conn, run_b)[0][1]
    # The slot column (supervisor step 5a, R8), as canonical JSON.
    assert rows[0][3] == canonical_json(stand_in_slot(key))

    unregistered = new_ulid()
    # The registered-instance lookup lives in rapidpipe.runs.inputs since
    # supervisor step 2 (R12); it returns a set.
    assert inputs._registered(conn, [instances[run_a], unregistered]) == {instances[run_a]}
    assert inputs._registered(conn, []) == set()
