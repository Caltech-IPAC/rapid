"""Dependency eligibility against a real PostgreSQL.

The chain proofs build a three-generation chain across three production
runs: a grandparent difference image G (run A) -> a parent source set P
(run B) -> a child association set C (run C). Promoting C walks the whole
chain (runs.md §Rules): every ancestor must be current or superseded, or
an after instance of the same request; the refusal writes nothing.
A dependency chain within one run promotes in one ``promote_run``; a run
depending on another run's candidates waits until those are promoted.
The ``state:`` block of ``run show`` and the registration-time read rule
for file products (products.md §Registration metadata) are proved here
too.

Skips cleanly if PGHOST is unset (see conftest.py).
"""

from __future__ import annotations

import pytest

from rapidpipe.checks.policy import load_policy
from rapidpipe.runs.checking import run_policy_checks
from rapidpipe.db.ids import new_ulid
from rapidpipe.runs import eligibility
from rapidpipe.runs import repository as repo

from .test_checks import TRIAL, _diff_candidate, _savepoint_raises, _selected_attempt
from .test_checks import cli_conn  # noqa: F401  (pytest fixture)
from .test_repository import _make_run, _register_simple_instance, by_slot, pin_test_slot
from .test_slots import (
    _NO_CHECKS_POLICY,
    _register_difference,
    _register_l2,
    _register_reference,
)

#: Statistics ``rebuild-trial@1`` refuses (n_min is 1000).
BAD_STATS = {"nsexcatsources": 10}


# ======================================================================
# Builders
# ======================================================================

def _check(conn, run_id, instance):
    """Run the trial policy's checks over ``instance`` and record them."""
    return run_policy_checks(conn, run_id, load_policy(TRIAL), instance=instance, who="test")


def _result_set(conn, run_id, kind, *, key, products=None, result_sets=()):
    """A complete result set of ``kind`` in ``run_id`` from a selected attempt,
    depending on ``products`` / ``result_sets``; slot pinned."""
    attempt_id = _selected_attempt(conn, run_id, stage="load")
    instance = new_ulid()
    repo.register_manifest(conn, {
        "run": run_id, "stage": "load", "attempt": attempt_id,
        "inputs": {"products": products or {}, "result_sets": list(result_sets)},
        "outputs": [{"kind": kind, "format_version": "1", "instance": instance,
                     "key": key, "primary": None, "members": [], "registration": {},
                     "row_count": 1}],
    }, registering_attempt_id=attempt_id)
    pin_test_slot(conn, instance, key)
    return instance


class Chain:
    """G (difference-image, run A) -> P (source-set, run B) -> C (association-set, run C)."""

    def __init__(self, conn):
        self.run_a = _make_run(conn)
        self.run_b = _make_run(conn)
        self.run_c = _make_run(conn)
        self.g_key = {"k": new_ulid()}
        self.g = _diff_candidate(conn, self.run_a, key=self.g_key)
        self.p_key = {"difference": self.g, "catalog_type": "t"}
        self.p = _result_set(conn, self.run_b, "source-set", key=self.p_key,
                             products={"difference": self.g})
        self.c_key = {"field": new_ulid(), "base": None}
        self.c = _result_set(conn, self.run_c, "association-set", key=self.c_key,
                             result_sets=[self.p])

    def change(self, which):
        kind, key, instance = {
            "g": ("difference-image", self.g_key, self.g),
            "p": ("source-set", self.p_key, self.p),
            "c": ("association-set", self.c_key, self.c),
        }[which]
        return (kind, by_slot(key), None, instance)

    def promote(self, conn, *which, **kw):
        return repo.promote(conn, "t", "chain", [self.change(w) for w in which], **kw)

    def promote_child(self, conn):
        return self.promote(conn, "c")


def _custody(conn, instance):
    with conn.cursor() as cur:
        cur.execute("SELECT custody FROM product_instances WHERE id = %s", (instance,))
        return cur.fetchone()[0]


def _state(conn, instance):
    with conn.cursor() as cur:
        return eligibility.instance_state(cur, instance)


def _counts(conn, instances):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM promotions")
        promotions = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM promotion_changes")
        changes = cur.fetchone()[0]
        cur.execute("SELECT id, custody FROM product_instances WHERE id = ANY(%s) ORDER BY id",
                    (list(instances),))
        custody = cur.fetchall()
    return promotions, changes, custody


def _unselect(conn, instance):
    with conn.cursor() as cur:
        cur.execute("UPDATE units SET selected_attempt = NULL WHERE id = "
                    "(SELECT a.unit FROM attempts a JOIN product_instances pi "
                    " ON pi.producing_attempt = a.id WHERE pi.id = %s)", (instance,))


# ======================================================================
# The states
# ======================================================================

def test_states_of_the_chain(conn):
    chain = Chain(conn)
    for instance in (chain.g, chain.p, chain.c):
        state = _state(conn, instance)
        assert (state.state, state.detail()) == ("candidate", "custody=candidate")
    # A failed check changes no state: states are custody, completeness and
    # selection facts only.
    _check(conn, chain.run_a, chain.g)
    assert _state(conn, chain.g).state == "candidate"
    assert _state(conn, chain.g).why() == "candidate: not current or superseded"


def test_state_precedence(conn):
    chain = Chain(conn)
    assert _state(conn, chain.g).state == "candidate"
    with conn.cursor() as cur:
        cur.execute("UPDATE product_instances SET custody = 'current' WHERE id = %s", (chain.g,))
    assert _state(conn, chain.g).state == "current"
    _unselect(conn, chain.g)
    unselected = _state(conn, chain.g)
    assert unselected.state == "unselected"          # before current
    assert "selected_attempt=none" in unselected.detail()
    with conn.cursor() as cur:
        cur.execute("UPDATE product_instances SET custody = 'scratch' WHERE id = %s", (chain.g,))
    assert _state(conn, chain.g).state == "scratch"
    with conn.cursor() as cur:
        cur.execute("UPDATE result_sets SET complete = false WHERE instance = %s", (chain.p,))
        cur.execute("UPDATE product_instances SET deletion_state = 'deleted' WHERE id = %s",
                    (chain.p,))
    assert _state(conn, chain.p).state == "deleted"
    with conn.cursor() as cur:
        cur.execute("UPDATE product_instances SET deletion_state = 'retained' WHERE id = %s",
                    (chain.p,))
    assert _state(conn, chain.p).state == "incomplete"
    with conn.cursor() as cur:
        with pytest.raises(LookupError):
            eligibility.instance_state(cur, "0" * 26)


def test_the_acceptances_table_is_gone(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('acceptances')")
        assert cur.fetchone()[0] is None


# ======================================================================
# The promotion walk
# ======================================================================

def test_promoting_the_child_is_refused_naming_the_grandparent(conn):
    chain = Chain(conn)
    info = _savepoint_raises(conn, repo.PromotionRefused, lambda: chain.promote_child(conn))
    message = str(info.value)
    assert f"after instance {chain.c!r} depends on {chain.g!r}" in message
    assert ("(difference-image, candidate: not current or superseded; promote it first "
            "or in the same request); refusing") in message


def test_the_refusal_changes_nothing(conn):
    chain = Chain(conn)
    instances = (chain.g, chain.p, chain.c)
    before = _counts(conn, instances)
    _savepoint_raises(conn, repo.PromotionRefused, lambda: chain.promote_child(conn))
    assert _counts(conn, instances) == before
    assert [c for _, c in before[2]] == ["candidate"] * 3


def test_the_whole_chain_promotes_in_one_request(conn):
    chain = Chain(conn)
    chain.promote(conn, "g", "p", "c")
    assert [_custody(conn, i) for i in (chain.g, chain.p, chain.c)] == ["current"] * 3


def test_the_chain_promotes_ancestor_first(conn):
    chain = Chain(conn)
    _savepoint_raises(conn, repo.PromotionRefused, lambda: chain.promote(conn, "p", "c"),
                      match=f"depends on {chain.g!r} .*promote it first")
    chain.promote(conn, "g")
    chain.promote(conn, "p")
    chain.promote_child(conn)
    assert [_custody(conn, i) for i in (chain.g, chain.p, chain.c)] == ["current"] * 3


def test_a_superseded_grandparent_permits(conn):
    chain = Chain(conn)
    # G current, then replaced in its slot.
    chain.promote(conn, "g")
    replacement = _diff_candidate(conn, _make_run(conn), key=chain.g_key)
    repo.promote(conn, "t", "g2",
                 [("difference-image", by_slot(chain.g_key), chain.g, replacement)])
    assert _state(conn, chain.g).state == "superseded"
    chain.promote(conn, "p", "c")
    assert (_custody(conn, chain.p), _custody(conn, chain.c)) == ("current", "current")


def test_a_candidate_grandparent_behind_a_current_parent_still_refuses(conn):
    chain = Chain(conn)
    with conn.cursor() as cur:
        cur.execute("UPDATE product_instances SET custody = 'current' WHERE id = %s", (chain.p,))
    _savepoint_raises(conn, repo.PromotionRefused, lambda: chain.promote_child(conn),
                      match=f"depends on {chain.g!r} .*candidate")


def test_an_unselected_ancestor_refuses_even_in_the_same_request(conn):
    chain = Chain(conn)
    _unselect(conn, chain.g)
    _savepoint_raises(conn, repo.PromotionRefused, lambda: chain.promote_child(conn),
                      match=f"depends on {chain.g!r} .*unselected")
    # A same-request ancestor is still validated as an after instance itself.
    _savepoint_raises(conn, repo.PromotionRefused,
                      lambda: chain.promote(conn, "g", "p", "c"),
                      match=f"after instance {chain.g!r} was not produced by its unit's "
                            "selected attempt")


def test_a_scratch_ancestor_refuses(conn):
    chain = Chain(conn)
    with conn.cursor() as cur:
        cur.execute("UPDATE product_instances SET custody = 'scratch' WHERE id = %s", (chain.g,))
    _savepoint_raises(conn, repo.PromotionRefused, lambda: chain.promote(conn, "p", "c"),
                      match=f"depends on {chain.g!r} .*scratch: not project custody\\); ")


def _sibling_pair(conn, *, passes):
    run_id = _make_run(conn)
    d_key = {"k": new_ulid()}
    diff = _diff_candidate(conn, run_id, key=d_key, stats=None if passes else BAD_STATS)
    _check(conn, run_id, diff)
    s_key = {"difference": diff, "catalog_type": "t"}
    source_set = _result_set(conn, run_id, "source-set", key=s_key,
                             products={"difference": diff})
    return run_id, (d_key, diff), (s_key, source_set)


def test_a_same_request_sibling_chain_promotes(conn):
    _run, (d_key, diff), (s_key, source_set) = _sibling_pair(conn, passes=True)
    # Alone, the source set's ancestor is a candidate outside the request.
    _savepoint_raises(conn, repo.PromotionRefused, lambda: repo.promote(conn, "t", "one", [
        ("source-set", by_slot(s_key), None, source_set)], check_policy=load_policy(TRIAL)),
        match=f"depends on {diff!r} .*promote it first or in the same request")
    repo.promote(conn, "t", "pair", [
        ("difference-image", by_slot(d_key), None, diff),
        ("source-set", by_slot(s_key), None, source_set)], check_policy=load_policy(TRIAL))
    assert (_custody(conn, diff), _custody(conn, source_set)) == ("current", "current")


def test_a_failed_sibling_in_the_same_request_is_not_laundered(conn):
    """The same-request ancestor passes the walk but is gated as an after
    instance by the request's own policy."""
    _run, (d_key, diff), (s_key, source_set) = _sibling_pair(conn, passes=False)
    _savepoint_raises(conn, repo.PromotionRefused, lambda: repo.promote(conn, "t", "pair", [
        ("difference-image", by_slot(d_key), None, diff),
        ("source-set", by_slot(s_key), None, source_set)], check_policy=load_policy(TRIAL)),
        match=f"required check difference-image-statistics@1 on instance {diff} "
              "\\(kind difference-image\\) is failed")
    assert (_custody(conn, diff), _custody(conn, source_set)) == ("candidate", "candidate")


def test_a_same_run_dependency_chain_promotes_in_one_promote_run(conn):
    """A difference image depending on the same run's reference candidate:
    one promote_run promotes both (runs.md §Rules)."""
    run_id, l2 = _register_l2(conn, 71001, 4, version=1)
    _run, reference = _register_reference(conn, 4700, "F184", run_id=run_id)
    _run, diff = _register_difference(conn, l2, reference, settings_hash="sha256:chain",
                                      run_id=run_id,
                                      input_products={"reference": reference, "l2": l2})
    _savepoint_raises(conn, repo.PromotionRefused, lambda: repo.promote_run(
        conn, run_id, "t", "diff only", kinds=["difference-image"], allow_unreleased=True,
        check_policy=_NO_CHECKS_POLICY),
        match="promote it first or in the same request")
    repo.promote_run(conn, run_id, "t", "whole run", allow_unreleased=True,
                     check_policy=_NO_CHECKS_POLICY)
    assert [_custody(conn, i) for i in (l2, reference, diff)] == ["current"] * 3


def test_a_later_date_waits_for_the_earlier_dates_promotion(conn):
    """Date 2's difference image binds date 1's reference candidate (loop.md
    §Promotion): date 2 is refused while date 1 is a candidate, then
    promotes once date 1 has (runs.md §Rules)."""
    date1, l2_1 = _register_l2(conn, 71101, 5, version=1)
    _run, reference = _register_reference(conn, 4800, "F184", run_id=date1)
    date2, l2_2 = _register_l2(conn, 71102, 5, version=1)
    _run, diff = _register_difference(conn, l2_2, reference, settings_hash="sha256:date2",
                                      run_id=date2,
                                      input_products={"reference": reference, "l2": l2_2})

    def promote(run_id):
        return repo.promote_run(conn, run_id, "t", "date", allow_unreleased=True,
                                check_policy=_NO_CHECKS_POLICY)

    info = _savepoint_raises(conn, repo.PromotionRefused, lambda: promote(date2))
    assert (f"after instance {diff!r} depends on {reference!r} (reference-image, candidate: "
            "not current or superseded; promote it first or in the same request)"
            in str(info.value))
    assert _custody(conn, diff) == "candidate"
    promote(date1)
    promote(date2)
    assert [_custody(conn, i) for i in (l2_1, reference, l2_2, diff)] == ["current"] * 4


def test_rollback_skips_the_walk_but_not_the_direct_dependencies(conn):
    """A rollback restores what an earlier promotion admitted."""
    chain = Chain(conn)
    chain.promote(conn, "g", "p")
    replacement = _result_set(conn, _make_run(conn), "source-set", key=chain.p_key)
    second = repo.promote(conn, "t", "replace", [
        ("source-set", by_slot(chain.p_key), chain.p, replacement)])
    # G's unit loses its selection: G is unselected.
    _unselect(conn, chain.g)
    assert _state(conn, chain.g).state == "unselected"
    # A fresh promotion of the old source set would now be refused...
    _savepoint_raises(conn, repo.PromotionRefused, lambda: repo.promote(
        conn, "t", "again", [("source-set", by_slot(chain.p_key), replacement, chain.p)]),
        match=f"depends on {chain.g!r} .*unselected")
    # ...a deleted direct dependency refuses the rollback too...
    with conn.cursor() as cur:
        cur.execute("UPDATE product_instances SET deletion_state = 'deleted' WHERE id = %s",
                    (chain.g,))
    _savepoint_raises(conn, repo.PromotionRefused,
                      lambda: repo.rollback_promotion(conn, second, "t", "undo"),
                      match=f"depends on {chain.g!r}, which is 'deleted'")
    with conn.cursor() as cur:
        cur.execute("UPDATE product_instances SET deletion_state = 'retained' WHERE id = %s",
                    (chain.g,))
    # ...but otherwise the rollback restores it.
    undo = repo.rollback_promotion(conn, second, "t", "undo")
    assert _custody(conn, chain.p) == "current"
    with conn.cursor() as cur:
        cur.execute("SELECT request_context FROM promotions WHERE id = %s", (undo,))
        assert cur.fetchone()[0] == {"rollback_of": second}


# ======================================================================
# Check show and run show
# ======================================================================

def test_check_show_prints_results_only_and_run_show_prints_states(
        conn, cli_conn, capsys):  # noqa: F811
    chain = Chain(conn)
    _check(conn, chain.run_a, chain.g)
    capsys.readouterr()
    assert cli_conn.main(["check", "show", chain.run_a]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines and all(line.startswith("id=") for line in lines)
    assert f"instance={chain.g}" in lines[0]

    assert cli_conn.main(["run", "show", chain.run_a]) == 0
    out = capsys.readouterr().out.splitlines()
    assert "acceptance:" not in out
    at = out.index("state:")
    assert out[at + 1] == (f"  instance={chain.g} kind=difference-image state=candidate "
                           "custody=candidate")

    chain.promote(conn, "g")
    assert cli_conn.main(["run", "show", chain.run_a]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[out.index("state:") + 1] == (
        f"  instance={chain.g} kind=difference-image state=current custody=current")


# ======================================================================
# Registration applies the read rule to file products
# ======================================================================

def test_registration_refuses_another_runs_scratch_file_product(conn):
    scratch = _make_run(conn, kind="scratch")
    attempt_id = _selected_attempt(conn, scratch, stage="admit")
    l2 = _register_simple_instance(conn, scratch, "admit", attempt_id, kind="l2-image",
                                   logical_key={"k": new_ulid()})
    reader = _make_run(conn)
    reader_attempt = _selected_attempt(conn, reader)
    _savepoint_raises(
        conn, repo.DependencyRefused,
        lambda: _register_simple_instance(conn, reader, "difference", reader_attempt,
                                          kind="difference-image",
                                          logical_key={"k": new_ulid()},
                                          input_products={"l2": l2}),
        match="custody 'scratch': another run's scratch output")
    # The same file product in its own run is a dependency like any other.
    own_attempt = _selected_attempt(conn, scratch)
    _register_simple_instance(conn, scratch, "difference", own_attempt,
                              kind="difference-image", logical_key={"k": new_ulid()},
                              input_products={"l2": l2})


def test_registration_admits_another_runs_selected_candidate_file_product(conn):
    producer = _make_run(conn)
    attempt_id = _selected_attempt(conn, producer, stage="admit")
    l2 = _register_simple_instance(conn, producer, "admit", attempt_id, kind="l2-image",
                                   logical_key={"k": new_ulid()})
    reader = _make_run(conn)
    _register_simple_instance(conn, reader, "difference", _selected_attempt(conn, reader),
                              kind="difference-image", logical_key={"k": new_ulid()},
                              input_products={"l2": l2})
    with conn.cursor() as cur:
        cur.execute("UPDATE units SET selected_attempt = NULL WHERE id = "
                    "(SELECT unit FROM attempts WHERE id = %s)", (attempt_id,))
    _savepoint_raises(
        conn, repo.DependencyRefused,
        lambda: _register_simple_instance(conn, reader, "difference",
                                          _selected_attempt(conn, reader),
                                          kind="difference-image",
                                          logical_key={"k": new_ulid()},
                                          input_products={"l2": l2}),
        match="not its unit's selected attempt")
