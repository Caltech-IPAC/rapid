"""Supervisor step 9 rulings R1 and R2 against a real PostgreSQL.

R1 (retry provenance): a done check reuses a complete set of this run only
when its producing attempt is the calling attempt or one whose disposition
is ``succeeded``; a set a failed attempt committed is not reused and the
retry writes its own.

R2 (cross-run result-set isolation): a stage reads a result set only when
it is complete and retained and either belongs to the reading run or is a
production run's output (custody ``candidate``/``current``) made by its
unit's selected attempt; ``register_manifest`` applies the same rule to a
dependency on another run's result set. The loop's real case -- date 2's
crossmatch extending date 1's association set -- is exercised end to end
through the crossmatch stage.

Skips cleanly if PGHOST is unset (see conftest.py).
"""

from __future__ import annotations

import json

import pytest

from rapidpipe.db import alerts as alerts_db
from rapidpipe.db import objects, sources
from rapidpipe.db.ids import new_ulid
from rapidpipe.products.manifest import Manifest
from rapidpipe.runs import repository as repo
from rapidpipe.stages import export
from rapidpipe.stages.contract import ExitCode

from .attempt_helpers import set_disposition, succeed_and_select
from .test_crossmatch import FIELD as XM_FIELD
from .test_crossmatch import _loaded_source_set, _output, _run_crossmatch
from .test_repository import _make_run, _make_unit

FIELD = 999999902   # a field no real per-field table has
STAGES = ["load", "crossmatch", "statistics", "prune", "alerts"]


def _run(conn, kind):
    return _make_run(conn, kind=kind, selected_stages=STAGES, max_attempts=3)


def _unit(conn, run_id, stage):
    unit_id = f"{stage}-{new_ulid()}"
    _make_unit(conn, run_id, stage=stage, unit_id=unit_id)
    return unit_id


def _register_set(conn, run_id, stage, unit_id, *, kind="association-set", key=None,
                  result_sets=(), attempt_id=None):
    """Allocate an attempt on ``unit_id`` (unless given) and register one complete set from it."""
    if attempt_id is None:
        attempt_id = repo.allocate_attempt(conn, run_id, stage, unit_id)
    instance = new_ulid()
    repo.register_manifest(conn, {
        "run": run_id, "stage": stage, "attempt": attempt_id,
        "inputs": {"products": {}, "result_sets": list(result_sets)},
        "outputs": [{"kind": kind, "format_version": "1", "instance": instance,
                     "key": key if key is not None else {"field": FIELD, "base": None,
                                                         "settings_hash": new_ulid()},
                     "primary": None, "members": [], "registration": {}, "row_count": 1}],
    }, registering_attempt_id=attempt_id)
    return attempt_id, instance


def _custody(conn, instance, custody):
    with conn.cursor() as cur:
        cur.execute("UPDATE product_instances SET custody = %s WHERE id = %s",
                    (custody, instance))


# ----------------------------------------------------------------------
# R1: done checks
# ----------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["association-set", "statistics-set", "pruned-set"])
def test_done_check_reuses_only_its_own_or_a_succeeded_attempts_set(conn, kind):
    run_id = _run(conn, "scratch")
    unit_id = _unit(conn, run_id, "crossmatch")
    key = {"field": FIELD, "settings_hash": new_ulid()}
    first, instance = _register_set(conn, run_id, "crossmatch", unit_id, kind=kind, key=key)
    second = repo.allocate_attempt(conn, run_id, "crossmatch", unit_id)
    with conn.cursor() as cur:
        def found(attempt):
            return objects.find_complete_result_set(cur, kind, run_id, key, attempt)

        # Its own attempt, still without a disposition, finds it (a rerun of itself).
        assert found(first) == (instance, 1)
        # Another attempt does not while the producer has no disposition...
        assert found(second) is None
        # ...nor once it failed after committing its rows...
        set_disposition(conn, first, "failed")
        assert found(second) is None
        # ...and does once it succeeded.
        set_disposition(conn, first, "succeeded")
        assert found(second) == (instance, 1)


@pytest.mark.parametrize("disposition", [None, "failed", "transient", "lost", "killed"])
def test_done_check_never_reuses_another_attempts_set_unless_it_succeeded(conn, disposition):
    run_id = _run(conn, "scratch")
    unit_id = _unit(conn, run_id, "crossmatch")
    key = {"field": FIELD, "settings_hash": new_ulid()}
    first, _ = _register_set(conn, run_id, "crossmatch", unit_id, key=key)
    if disposition is not None:
        set_disposition(conn, first, disposition)
    retry = repo.allocate_attempt(conn, run_id, "crossmatch", unit_id)
    with conn.cursor() as cur:
        assert objects.find_complete_result_set(
            cur, "association-set", run_id, key, retry) is None


def test_a_succeeded_but_unselected_set_is_reused_in_run_and_refused_across_runs(conn):
    producer = _run(conn, "production")
    unit_id = _unit(conn, producer, "crossmatch")
    key = {"field": FIELD, "base": None, "settings_hash": new_ulid()}
    first, instance = _register_set(conn, producer, "crossmatch", unit_id, key=key)
    set_disposition(conn, first, "succeeded")          # succeeded, never selected
    retry = repo.allocate_attempt(conn, producer, "crossmatch", unit_id)
    reader = _run(conn, "production")
    with conn.cursor() as cur:
        # R1: the retry in the same run reuses it...
        assert objects.find_complete_result_set(
            cur, "association-set", producer, key, retry) == (instance, 1)
        # ...and the reuse leaves its producing provenance alone.
        cur.execute("SELECT producing_attempt, custody FROM product_instances WHERE id = %s",
                    (instance,))
        assert cur.fetchone() == (first, "candidate")
        # R2: another run may not read it, its producer not being selected.
        with pytest.raises(ValueError, match="not its unit's selected attempt"):
            objects.assert_readable_result_set(cur, instance, reader)
        with pytest.raises(ValueError, match="not its unit's selected attempt"):
            objects.association_chain(cur, instance, reader)


def test_a_reused_set_keeps_its_producing_attempt(conn, tmp_path, monkeypatch):
    run_id, load_outputs = _loaded_source_set(conn, tmp_path, monkeypatch)
    rc, first_attempt, first = _run_crossmatch(conn, monkeypatch, tmp_path, run_id,
                                               load_outputs, name="first")
    assert rc == 0
    set_disposition(conn, first_attempt, "succeeded")
    rc, _, second = _run_crossmatch(conn, monkeypatch, tmp_path, run_id, load_outputs,
                                    name="second")
    assert rc == 0
    instance = _output(first)[1].instance
    assert _output(second)[1].instance == instance
    with conn.cursor() as cur:
        cur.execute("SELECT producing_attempt, registering_attempt FROM product_instances "
                    "WHERE id = %s", (instance,))
        assert cur.fetchone() == (first_attempt, first_attempt)


def test_every_link_and_every_named_source_set_is_checked(conn):
    """A readable head does not carry an unreadable base or source set with it."""
    reader = _run(conn, "production")
    scratch = _run(conn, "scratch")
    # A selected scratch source set, named by a production set's key.
    load_unit = _unit(conn, scratch, "load")
    load_attempt, private_sources = _register_set(
        conn, scratch, "load", load_unit, kind="source-set",
        key={"difference": new_ulid(), "catalog_type": "photutils"})
    succeed_and_select(conn, load_attempt)
    _, _, base = _production_set(conn, select=False)   # unselected base
    head_run = _run(conn, "production")
    head_unit = _unit(conn, head_run, "crossmatch")
    head_attempt, head = _register_set(
        conn, head_run, "crossmatch", head_unit,
        key={"field": FIELD, "base": base, "source_sets": [private_sources],
             "settings_hash": "h"})
    succeed_and_select(conn, head_attempt)
    with conn.cursor() as cur:
        objects.assert_readable_result_set(cur, head, reader)      # the head alone is fine
        with pytest.raises(ValueError, match=f"'{base}'.*selected"):
            objects.association_chain(cur, head, reader)
        with pytest.raises(ValueError, match="scratch"):
            objects.source_set_table(cur, private_sources, reader)


def test_load_done_check_reuses_only_its_own_or_a_succeeded_attempts_set(conn):
    run_id = _run(conn, "scratch")
    unit_id = _unit(conn, run_id, "load")
    key = {"difference": new_ulid(), "catalog_type": "photutils"}
    first, instance = _register_set(conn, run_id, "load", unit_id, kind="source-set", key=key)
    second = repo.allocate_attempt(conn, run_id, "load", unit_id)
    with conn.cursor() as cur:
        assert sources.find_complete_source_set(cur, run_id, key, first) == (instance, 1)
        assert sources.find_complete_source_set(cur, run_id, key, second) is None
        set_disposition(conn, first, "failed")
        assert sources.find_complete_source_set(cur, run_id, key, second) is None
        set_disposition(conn, first, "succeeded")
        assert sources.find_complete_source_set(cur, run_id, key, second) == (instance, 1)


def test_a_crossmatch_retry_after_a_failed_attempt_writes_its_own_set(
        conn, tmp_path, monkeypatch):
    run_id, load_outputs = _loaded_source_set(conn, tmp_path, monkeypatch)
    rc, first_attempt, first = _run_crossmatch(conn, monkeypatch, tmp_path, run_id,
                                               load_outputs, name="first")
    assert rc == 0
    # The attempt committed its rows, then failed (say, publishing its outputs).
    set_disposition(conn, first_attempt, "failed")
    rc, second_attempt, second = _run_crossmatch(conn, monkeypatch, tmp_path, run_id,
                                                 load_outputs, name="second")
    assert rc == 0
    a, b = _output(first)[1], _output(second)[1]
    assert a.instance != b.instance
    with conn.cursor() as cur:
        cur.execute("SELECT id, producing_attempt FROM product_instances "
                    "WHERE kind = 'association-set' AND run = %s ORDER BY id", (run_id,))
        assert sorted(cur.fetchall()) == sorted([(a.instance, first_attempt),
                                                 (b.instance, second_attempt)])
        # The orphaned set's rows stay the run's own rows.
        assert objects.count_result_set_rows(cur, f"merges_{XM_FIELD}", a.instance) == 4


# ----------------------------------------------------------------------
# R2: the read rule, one helper
# ----------------------------------------------------------------------


def _production_set(conn, *, select=True, kind="association-set", key=None):
    """A production run's set (custody candidate), its attempt selected unless told otherwise."""
    run_id = _run(conn, "production")
    unit_id = _unit(conn, run_id, "crossmatch")
    attempt_id, instance = _register_set(conn, run_id, "crossmatch", unit_id, kind=kind,
                                         key=key)
    if select:
        succeed_and_select(conn, attempt_id)
    return run_id, attempt_id, instance


def test_the_loop_case_date_2_reads_date_1s_selected_production_set(conn):
    date1, _, first = _production_set(conn)
    date2 = _run(conn, "production")
    with conn.cursor() as cur:
        state = objects.assert_readable_result_set(cur, first, date2, kind="association-set")
        assert (state["run"], state["custody"]) == (date1, "candidate")
        assert objects.association_chain(cur, first, date2) == [first]
    # Date 2's crossmatch extends it: its own set names date 1's as base.
    unit_id = _unit(conn, date2, "crossmatch")
    _, second = _register_set(conn, date2, "crossmatch", unit_id,
                              key={"field": FIELD, "base": first, "settings_hash": "h"},
                              result_sets=[first])
    with conn.cursor() as cur:
        assert objects.association_chain(cur, second, date2) == [second, first]
        cur.execute("SELECT producer_instance FROM dependencies WHERE consumer_instance = %s",
                    (second,))
        assert [r[0] for r in cur.fetchall()] == [first]
    # Promoted (current), it is still readable.
    _custody(conn, first, "current")
    with conn.cursor() as cur:
        assert objects.association_chain(cur, second, date2) == [second, first]


def test_another_runs_scratch_set_is_refused(conn):
    scratch = _run(conn, "scratch")
    unit_id = _unit(conn, scratch, "crossmatch")
    attempt_id, private = _register_set(conn, scratch, "crossmatch", unit_id)
    succeed_and_select(conn, attempt_id)   # selected, yet still scratch
    reader = _run(conn, "production")
    with conn.cursor() as cur:
        # Its own run reads it.
        assert objects.association_chain(cur, private, scratch) == [private]
        with pytest.raises(ValueError, match="scratch result set is not readable"):
            objects.assert_readable_result_set(cur, private, reader)
        with pytest.raises(ValueError, match="association chain of .*scratch"):
            objects.association_chain(cur, private, reader)
        with pytest.raises(ValueError, match="scratch"):
            alerts_db.result_set_kinds(cur, [private], reader)
        with pytest.raises(ValueError, match="scratch"):
            export.PostgresExportDatabase(conn).result_set_states([private], reader)
    # Nor can another run record it as a dependency.
    other_unit = _unit(conn, reader, "crossmatch")
    with pytest.raises(repo.DependencyRefused, match="scratch"):
        _register_set(conn, reader, "crossmatch", other_unit, result_sets=[private])
    # A scratch set as the base of another run's set breaks that set's chain too.
    base_unit = _unit(conn, scratch, "crossmatch")
    extension_attempt, extension = _register_set(
        conn, scratch, "crossmatch", base_unit,
        key={"field": FIELD, "base": private, "settings_hash": "h"})
    succeed_and_select(conn, extension_attempt)
    _custody(conn, extension, "candidate")
    with conn.cursor() as cur:
        with pytest.raises(ValueError, match=f"chain of '{extension}'.*'{private}'.*scratch"):
            objects.association_chain(cur, extension, reader)


def test_another_runs_scratch_source_set_is_refused(conn):
    scratch = _run(conn, "scratch")
    unit_id = _unit(conn, scratch, "load")
    attempt_id, source_set = _register_set(
        conn, scratch, "load", unit_id, kind="source-set",
        key={"difference": new_ulid(), "catalog_type": "photutils"})
    succeed_and_select(conn, attempt_id)
    reader = _run(conn, "scratch")
    with conn.cursor() as cur:
        # Its own run passes the rule (and stops at the missing diffimages row).
        with pytest.raises(ValueError, match="no diffimages"):
            objects.source_set_table(cur, source_set, scratch)
        with pytest.raises(ValueError, match="scratch result set is not readable"):
            objects.source_set_table(cur, source_set, reader)


@pytest.mark.parametrize("disposition", [None, "failed", "succeeded"])
def test_an_unselected_attempts_set_is_refused(conn, disposition):
    producer, attempt_id, instance = _production_set(conn, select=False)
    if disposition is not None:
        set_disposition(conn, attempt_id, disposition)
    reader = _run(conn, "production")
    with conn.cursor() as cur:
        assert objects.association_chain(cur, instance, producer) == [instance]
        with pytest.raises(ValueError, match="not its unit's selected attempt"):
            objects.association_chain(cur, instance, reader)
    unit_id = _unit(conn, reader, "crossmatch")
    with pytest.raises(repo.DependencyRefused, match="selected attempt"):
        _register_set(conn, reader, "crossmatch", unit_id, result_sets=[instance])


def test_of_two_attempts_only_the_selected_ones_set_is_readable(conn):
    producer = _run(conn, "production")
    unit_id = _unit(conn, producer, "crossmatch")
    failed, orphan = _register_set(conn, producer, "crossmatch", unit_id)
    set_disposition(conn, failed, "failed")
    winner, selected = _register_set(conn, producer, "crossmatch", unit_id)
    succeed_and_select(conn, winner)
    reader = _run(conn, "production")
    with conn.cursor() as cur:
        assert objects.association_chain(cur, selected, reader) == [selected]
        with pytest.raises(ValueError, match="selected"):
            objects.association_chain(cur, orphan, reader)


def test_a_deleted_or_incomplete_production_set_is_refused(conn):
    _, _, instance = _production_set(conn)
    reader = _run(conn, "production")
    with conn.cursor() as cur:
        cur.execute("UPDATE result_sets SET complete = false WHERE instance = %s", (instance,))
        with pytest.raises(ValueError, match="not complete and retained"):
            objects.assert_readable_result_set(cur, instance, reader)
    unit_id = _unit(conn, reader, "crossmatch")
    with pytest.raises(repo.DependencyRefused, match="not a complete result set"):
        _register_set(conn, reader, "crossmatch", unit_id, result_sets=[instance])


# ----------------------------------------------------------------------
# R2 through the crossmatch stage: the loop's two dates
# ----------------------------------------------------------------------


def _producing_attempt(conn, instance):
    with conn.cursor() as cur:
        cur.execute("SELECT producing_attempt FROM product_instances WHERE id = %s",
                    (instance,))
        return cur.fetchone()[0]


def _with_base(tmp_path, load_outputs, base_outputs, name):
    composed = json.loads((load_outputs / "manifest.json").read_text())
    composed["outputs"].append(json.loads((base_outputs / "manifest.json").read_text())
                               ["outputs"][0])
    inputs = tmp_path / name
    inputs.mkdir()
    (inputs / "manifest.json").write_text(json.dumps(composed))
    return inputs


def test_crossmatch_of_date_2_extends_date_1s_selected_set(conn, tmp_path, monkeypatch):
    date1, load_outputs = _loaded_source_set(conn, tmp_path, monkeypatch)
    (source_set,) = Manifest.read(load_outputs / "manifest.json").outputs
    rc, date1_attempt, date1_outputs = _run_crossmatch(conn, monkeypatch, tmp_path, date1,
                                                       load_outputs, name="date1")
    assert rc == 0
    base = _output(date1_outputs)[1].instance
    inputs = _with_base(tmp_path, load_outputs, date1_outputs, "date2-inputs")
    date2 = _make_run(conn, kind="production", selected_stages=["crossmatch"])

    # Date 1 complete: load and crossmatch selected, as the loop requires.
    succeed_and_select(conn, _producing_attempt(conn, source_set.instance))
    succeed_and_select(conn, date1_attempt)
    rc, _, date2_outputs = _run_crossmatch(conn, monkeypatch, tmp_path, date2, inputs,
                                           name="date2")
    assert rc == int(ExitCode.SUCCESS)
    manifest, entry = _output(date2_outputs)
    assert entry.key["base"] == base
    assert set(manifest.inputs.result_sets) == {source_set.instance, base}
    with conn.cursor() as cur:
        assert objects.association_chain(cur, entry.instance, date2) == [entry.instance, base]
        cur.execute("SELECT producer_instance FROM dependencies WHERE consumer_instance = %s",
                    (entry.instance,))
        assert {r[0] for r in cur.fetchall()} == {source_set.instance, base}


def test_crossmatch_refuses_another_runs_scratch_base(conn, tmp_path, monkeypatch):
    date1, load_outputs = _loaded_source_set(conn, tmp_path, monkeypatch)
    rc, date1_attempt, date1_outputs = _run_crossmatch(conn, monkeypatch, tmp_path, date1,
                                                       load_outputs, name="date1")
    assert rc == 0
    (source_set,) = Manifest.read(load_outputs / "manifest.json").outputs
    succeed_and_select(conn, _producing_attempt(conn, source_set.instance))
    succeed_and_select(conn, date1_attempt)
    # The same sets, but date 1's association set is private scratch output.
    _custody(conn, _output(date1_outputs)[1].instance, "scratch")
    inputs = _with_base(tmp_path, load_outputs, date1_outputs, "scratch-base-inputs")
    reader = _make_run(conn, kind="scratch", selected_stages=["crossmatch"])
    rc, _, _ = _run_crossmatch(conn, monkeypatch, tmp_path, reader, inputs, name="reader")
    assert rc == int(ExitCode.INPUT_REJECTED)


def test_crossmatch_refuses_date_1s_sets_before_their_attempts_are_selected(
        conn, tmp_path, monkeypatch):
    # A refusal rolls back the test's transaction, so it is the last step.
    date1, load_outputs = _loaded_source_set(conn, tmp_path, monkeypatch)
    rc, _, date1_outputs = _run_crossmatch(conn, monkeypatch, tmp_path, date1, load_outputs,
                                           name="date1")
    assert rc == 0
    inputs = _with_base(tmp_path, load_outputs, date1_outputs, "unselected-inputs")
    date2 = _make_run(conn, kind="production", selected_stages=["crossmatch"])
    rc, _, _ = _run_crossmatch(conn, monkeypatch, tmp_path, date2, inputs, name="refused")
    assert rc == int(ExitCode.INPUT_REJECTED)
