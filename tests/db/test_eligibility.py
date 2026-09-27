"""Dependency eligibility against a real PostgreSQL.

The chain proofs build a three-generation chain across three production
runs: a grandparent difference image G (run A) whose required check
``difference-image-statistics@1`` failed under ``rebuild-trial@1`` -> a
parent source set P (run B; the policy names no required check for its
kind) -> a child association set C (run C). Promoting C walks the whole
chain (loop.md §Promotion); ``check accept`` on G through ``rapidpipe.cli.main``
lets it through; the refusal writes nothing. The acceptance lines of
``check show`` and ``run show`` and the registration-time read rule
for file products (products.md §Registration metadata) are proved here too.

Skips cleanly if PGHOST is unset (see conftest.py).
"""

from __future__ import annotations

import json

import pytest

from rapidpipe.checks.policy import load_policy
from rapidpipe.checks.runner import run_policy_checks
from rapidpipe.db.ids import new_ulid
from rapidpipe.runs import eligibility
from rapidpipe.runs import repository as repo

from .test_checks import TRIAL, _diff_candidate, _savepoint_raises, _selected_attempt
from .test_checks import cli_conn  # noqa: F401  (pytest fixture)
from .test_repository import _make_run, _register_simple_instance, by_slot, pin_test_slot

#: Statistics ``rebuild-trial@1`` refuses (n_min is 1000).
BAD_STATS = {"nsexcatsources": 10}


# ======================================================================
# Builders
# ======================================================================

def _check(conn, run_id, instance):
    """Run the run's policy checks over ``instance`` and record them."""
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

    def __init__(self, conn, *, grandparent_checked=True, grandparent_passes=False):
        self.run_a = _make_run(conn)
        self.run_b = _make_run(conn)
        self.run_c = _make_run(conn)
        self.g_key = {"k": new_ulid()}
        self.g = _diff_candidate(conn, self.run_a, key=self.g_key,
                                 stats=None if grandparent_passes else BAD_STATS)
        if grandparent_checked:
            _check(conn, self.run_a, self.g)
        self.p = _result_set(conn, self.run_b, "source-set",
                             key={"difference": self.g, "catalog_type": "t"},
                             products={"difference": self.g})
        self.c_key = {"field": new_ulid(), "base": None}
        self.c = _result_set(conn, self.run_c, "association-set", key=self.c_key,
                             result_sets=[self.p])

    def promote_child(self, conn):
        return repo.promote(conn, "t", "chain", [
            ("association-set", by_slot(self.c_key), None, self.c)])


def _custody(conn, instance):
    with conn.cursor() as cur:
        cur.execute("SELECT custody FROM product_instances WHERE id = %s", (instance,))
        return cur.fetchone()[0]


def _state(conn, instance):
    with conn.cursor() as cur:
        return eligibility.acceptance_state(cur, instance)


def _counts(conn, instances):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM promotions")
        promotions = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM promotion_changes")
        changes = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM acceptances")
        acceptances = cur.fetchone()[0]
        cur.execute("SELECT id, custody FROM product_instances WHERE id = ANY(%s) ORDER BY id",
                    (list(instances),))
        custody = cur.fetchall()
    return promotions, changes, acceptances, custody


def _acceptance_rows(conn, instance):
    with conn.cursor() as cur:
        cur.execute("SELECT who, reason, policy_ref, check_ids::text[], detail "
                    "FROM acceptances WHERE instance = %s", (instance,))
        return cur.fetchall()


# ======================================================================
# The states
# ======================================================================

def test_states_of_the_chain(conn):
    chain = Chain(conn)
    g, p, c = (_state(conn, i) for i in (chain.g, chain.p, chain.c))
    assert g.state == "rejected"
    assert g.required_checks == [("difference-image-statistics@1", "failed")]
    assert g.policy_ref == TRIAL and len(g.check_ids) == 1
    # source-set: only an advisory check under the policy, so none is required.
    assert p.state == "accepted" and p.required_checks == []
    assert c.state == "accepted"
    assert "rejected: difference-image-statistics@1 failed" in g.why()


def test_state_precedence(conn):
    chain = Chain(conn, grandparent_checked=False)
    assert _state(conn, chain.g).state == "pending"
    with conn.cursor() as cur:
        cur.execute("UPDATE product_instances SET custody = 'current' WHERE id = %s", (chain.g,))
    assert _state(conn, chain.g).state == "current"
    with conn.cursor() as cur:
        cur.execute("UPDATE units SET selected_attempt = NULL WHERE id = "
                    "(SELECT a.unit FROM attempts a JOIN product_instances pi "
                    " ON pi.producing_attempt = a.id WHERE pi.id = %s)", (chain.g,))
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
            eligibility.acceptance_state(cur, "0" * 26)


# ======================================================================
# The promotion walk
# ======================================================================

def test_promoting_the_child_is_refused_naming_the_grandparent(conn):
    chain = Chain(conn)
    info = _savepoint_raises(conn, repo.PromotionRefused, lambda: chain.promote_child(conn))
    message = str(info.value)
    assert f"after instance {chain.c!r} depends on {chain.g!r}" in message
    assert "difference-image, rejected: difference-image-statistics@1 failed" in message
    assert "accept it with `check accept` or replace it" in message


def test_the_refusal_changes_nothing(conn):
    chain = Chain(conn)
    instances = (chain.g, chain.p, chain.c)
    before = _counts(conn, instances)
    _savepoint_raises(conn, repo.PromotionRefused, lambda: chain.promote_child(conn))
    assert _counts(conn, instances) == before
    assert [c for _, c in before[3]] == ["candidate"] * 3


def test_check_accept_on_the_grandparent_permits_the_promotion(conn, cli_conn, capsys):  # noqa: F811
    chain = Chain(conn)
    assert cli_conn.main(["check", "accept", chain.run_a, "--instance", chain.g,
                          "--reason", "scalefacref known high on this date",
                          "--who", "lead"]) == 0
    out = capsys.readouterr().out.strip()
    (row,) = _acceptance_rows(conn, chain.g)
    who, reason, policy_ref, check_ids, detail = row
    assert (who, reason, policy_ref) == ("lead", "scalefacref known high on this date", TRIAL)
    assert check_ids == _state(conn, chain.g).check_ids and len(check_ids) == 1
    assert detail["failed"][0]["check"] == "difference-image-statistics@1"
    assert detail["failed"][0]["outcome"] == "failed" and detail["failed"][0]["summary"]
    assert out.startswith(f"accepted instance={chain.g} kind=difference-image acceptance=")
    assert out.endswith(f"policy={TRIAL} checks=1")
    accepted = _state(conn, chain.g)
    assert accepted.state == "accepted" and accepted.reason == "scalefacref known high on this date"

    chain.promote_child(conn)
    assert _custody(conn, chain.c) == "current"
    # Acceptance is separate from selection: the ancestors stay candidates.
    assert (_custody(conn, chain.g), _custody(conn, chain.p)) == ("candidate", "candidate")


def test_a_superseded_grandparent_permits(conn):
    chain = Chain(conn)
    # G current (promoted without a policy), then replaced in its slot.
    repo.promote(conn, "t", "g", [("difference-image", by_slot(chain.g_key), None, chain.g)])
    replacement = _diff_candidate(conn, _make_run(conn), key=chain.g_key)
    repo.promote(conn, "t", "g2",
                 [("difference-image", by_slot(chain.g_key), chain.g, replacement)])
    assert _state(conn, chain.g).state == "superseded"
    chain.promote_child(conn)
    assert _custody(conn, chain.c) == "current"


def test_a_rejected_grandparent_behind_a_current_parent_still_refuses(conn):
    chain = Chain(conn)
    with conn.cursor() as cur:
        cur.execute("UPDATE product_instances SET custody = 'current' WHERE id = %s", (chain.p,))
    _savepoint_raises(conn, repo.PromotionRefused, lambda: chain.promote_child(conn),
                      match=f"depends on {chain.g!r}.*rejected")


def test_an_unselected_ancestor_refuses(conn):
    chain = Chain(conn, grandparent_passes=True)
    with conn.cursor() as cur:
        cur.execute("UPDATE units SET selected_attempt = NULL WHERE id = "
                    "(SELECT a.unit FROM attempts a JOIN product_instances pi "
                    " ON pi.producing_attempt = a.id WHERE pi.id = %s)", (chain.g,))
    _savepoint_raises(conn, repo.PromotionRefused, lambda: chain.promote_child(conn),
                      match=f"depends on {chain.g!r} .*unselected")


def test_a_scratch_ancestor_refuses(conn):
    chain = Chain(conn, grandparent_passes=True)
    with conn.cursor() as cur:
        cur.execute("UPDATE product_instances SET custody = 'scratch' WHERE id = %s", (chain.g,))
    _savepoint_raises(conn, repo.PromotionRefused, lambda: chain.promote_child(conn),
                      match=f"depends on {chain.g!r} .*scratch: not project custody")


def test_a_not_yet_checked_ancestor_refuses_as_pending(conn):
    chain = Chain(conn, grandparent_checked=False)
    _savepoint_raises(conn, repo.PromotionRefused, lambda: chain.promote_child(conn),
                      match=f"depends on {chain.g!r} .*pending: "
                            "difference-image-statistics@1 has not run")


def test_a_passed_grandparent_permits(conn):
    chain = Chain(conn, grandparent_passes=True)
    chain.promote_child(conn)
    assert _custody(conn, chain.c) == "current"


def _sibling_pair(conn, *, passes):
    run_id = _make_run(conn)
    d_key = {"k": new_ulid()}
    diff = _diff_candidate(conn, run_id, key=d_key, stats=None if passes else BAD_STATS)
    _check(conn, run_id, diff)
    s_key = {"difference": diff, "catalog_type": "t"}
    source_set = _result_set(conn, run_id, "source-set", key=s_key,
                             products={"difference": diff})
    return run_id, (d_key, diff), (s_key, source_set)


def test_a_same_promotion_sibling_chain_promotes(conn):
    _run, (d_key, diff), (s_key, source_set) = _sibling_pair(conn, passes=True)
    repo.promote(conn, "t", "pair", [
        ("difference-image", by_slot(d_key), None, diff),
        ("source-set", by_slot(s_key), None, source_set)], check_policy=load_policy(TRIAL))
    assert (_custody(conn, diff), _custody(conn, source_set)) == ("current", "current")


def test_a_rejected_sibling_in_the_same_promotion_is_not_laundered(conn):
    """No sibling skip; the ancestor is judged under its own run's policy."""
    _run, (d_key, diff), (s_key, source_set) = _sibling_pair(conn, passes=False)
    _savepoint_raises(conn, repo.PromotionRefused, lambda: repo.promote(conn, "t", "pair", [
        ("difference-image", by_slot(d_key), None, diff),
        ("source-set", by_slot(s_key), None, source_set)]),
        match=f"depends on {diff!r} .*rejected")


def test_a_candidate_reference_of_another_production_run_promotes(conn):
    """The live loop: a difference image of this date's run depends on a
    candidate reference image of another production run that was never
    promoted, a kind with no policy check."""
    ref_run = _make_run(conn)
    attempt_id = _selected_attempt(conn, ref_run, stage="reference")
    reference = _register_simple_instance(
        conn, ref_run, "reference", attempt_id, kind="reference-image",
        logical_key={"field": "1", "filter": "F158", "recipe": "r", "version": new_ulid()})
    assert _state(conn, reference).state == "accepted"
    run_id = _make_run(conn)
    d_attempt = _selected_attempt(conn, run_id)
    d_key = {"k": new_ulid()}
    diff = _register_simple_instance(conn, run_id, "difference", d_attempt,
                                     kind="difference-image", logical_key=d_key,
                                     input_products={"reference": reference})
    s_key = {"difference": diff, "catalog_type": "t"}
    source_set = _result_set(conn, run_id, "source-set", key=s_key,
                             products={"difference": diff})
    # The difference image itself has no diffimages row here, so promote its
    # consumer: the walk reaches diff (pending until checked) and reference.
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO checks (id, instance, check_name, version, required, outcome, "
            "detail) VALUES (%s, %s, 'difference-image-statistics', '1', true, 'passed', "
            "%s)", (new_ulid(), diff, json.dumps({
                "params": load_policy(TRIAL).find_check(
                    "difference-image-statistics@1").params, "summary": "ok"})))
    repo.promote(conn, "t", "loop", [("source-set", by_slot(s_key), None, source_set)])
    assert _custody(conn, source_set) == "current"
    assert _custody(conn, reference) == "candidate"


def test_rollback_skips_the_walk(conn):
    """A rollback restores what an earlier promotion admitted."""
    chain = Chain(conn, grandparent_passes=True)
    p_key = {"difference": chain.g, "catalog_type": "t"}
    repo.promote(conn, "t", "first", [("source-set", by_slot(p_key), None, chain.p)])
    replacement = _result_set(conn, _make_run(conn), "source-set", key=p_key)
    second = repo.promote(conn, "t", "replace", [
        ("source-set", by_slot(p_key), chain.p, replacement)])
    # G's check now fails on a later row: G is rejected.
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO checks (id, instance, check_name, version, required, outcome, "
            "detail, happened_at) VALUES (%s, %s, 'difference-image-statistics', '1', true, "
            "'failed', %s, now() + interval '1 second')", (new_ulid(), chain.g, json.dumps({
                "params": load_policy(TRIAL).find_check(
                    "difference-image-statistics@1").params, "summary": "late"})))
    assert _state(conn, chain.g).state == "rejected"
    # A fresh promotion of the old source set would now be refused...
    _savepoint_raises(conn, repo.PromotionRefused, lambda: repo.promote(
        conn, "t", "again", [("source-set", by_slot(p_key), replacement, chain.p)]),
        match=f"depends on {chain.g!r} .*rejected")
    # ...but the rollback restores it.
    repo.rollback_promotion(conn, second, "t", "undo")
    assert _custody(conn, chain.p) == "current"


# ======================================================================
# Check accept refusals
# ======================================================================

@pytest.mark.parametrize("case, message", [
    ("pending", "is pending: .*run the checks first"),
    ("accepted", "is accepted: it is already accepted"),
    ("current", "is current, so already accepted"),
    ("wrong-run", "is not a product of run"),
    ("unselected", "is unselected: .*not acceptable"),
])
def test_check_accept_refusals_exit_64_and_write_nothing(conn, cli_conn, capsys,  # noqa: F811
                                                         case, message):
    chain = Chain(conn, grandparent_checked=case != "pending",
                  grandparent_passes=case == "accepted")
    run_id = chain.run_b if case == "wrong-run" else chain.run_a
    with conn.cursor() as cur:
        if case == "current":
            cur.execute("UPDATE product_instances SET custody = 'current' WHERE id = %s",
                        (chain.g,))
        if case == "unselected":
            cur.execute("UPDATE units SET selected_attempt = NULL WHERE id = "
                        "(SELECT a.unit FROM attempts a JOIN product_instances pi "
                        " ON pi.producing_attempt = a.id WHERE pi.id = %s)", (chain.g,))
    capsys.readouterr()
    assert cli_conn.main(["check", "accept", run_id, "--instance", chain.g,
                          "--reason", "why"]) == 64
    err = capsys.readouterr().err
    assert err.startswith("rapidpipe check: accept: ")
    import re
    assert re.search(message, err), err
    assert _acceptance_rows(conn, chain.g) == []


@pytest.mark.parametrize("reason", ["", "   "])
def test_check_accept_refuses_an_empty_reason(conn, cli_conn, capsys, reason):  # noqa: F811
    chain = Chain(conn)
    assert cli_conn.main(["check", "accept", chain.run_a, "--instance", chain.g,
                          "--reason", reason]) == 64
    assert "--reason must not be empty" in capsys.readouterr().err
    assert _acceptance_rows(conn, chain.g) == []


def test_check_accept_twice_says_already_accepted(conn, cli_conn, capsys):  # noqa: F811
    chain = Chain(conn)
    argv = ["check", "accept", chain.run_a, "--instance", chain.g, "--reason", "ok"]
    assert cli_conn.main(argv) == 0
    assert cli_conn.main(argv) == 64
    assert "already accepted" in capsys.readouterr().err
    assert len(_acceptance_rows(conn, chain.g)) == 1


def test_the_acceptances_table_refuses_an_empty_reason(conn):
    chain = Chain(conn)
    import psycopg2

    with conn.cursor() as cur:
        cur.execute("SAVEPOINT t")
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute("INSERT INTO acceptances (id, instance, who, reason, policy_ref, "
                        "check_ids) VALUES (%s, %s, 'x', '', %s, '{}')",
                        (new_ulid(), chain.g, TRIAL))
        cur.execute("ROLLBACK TO SAVEPOINT t")


# ======================================================================
# Check show and run show
# ======================================================================

def test_check_show_and_run_show_print_acceptance_lines(conn, cli_conn, capsys):  # noqa: F811
    chain = Chain(conn)
    capsys.readouterr()
    assert cli_conn.main(["check", "show", chain.run_a]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("id=") and f"instance={chain.g}" in lines[0]
    assert lines[-1] == (f"acceptance instance={chain.g} kind=difference-image "
                         f"state=rejected check=difference-image-statistics@1 "
                         f"outcome=failed policy={TRIAL}")

    assert cli_conn.main(["check", "accept", chain.run_a, "--instance", chain.g,
                          "--reason", "ok"]) == 0
    acceptance_id = capsys.readouterr().out.split("acceptance=")[1].split()[0]
    assert cli_conn.main(["check", "show", chain.run_a, "--instance", chain.g]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == (
        f"acceptance instance={chain.g} kind=difference-image state=accepted "
        f"acceptance={acceptance_id} policy={TRIAL}")

    assert cli_conn.main(["check", "show", chain.run_b]) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"acceptance instance={chain.p} kind=source-set state=accepted "
        f"policy={TRIAL} required_checks=none"]

    assert cli_conn.main(["run", "show", chain.run_a]) == 0
    out = capsys.readouterr().out.splitlines()
    at = out.index("acceptance:")
    assert out[at + 1] == (f"  acceptance instance={chain.g} kind=difference-image "
                           f"state=accepted acceptance={acceptance_id} policy={TRIAL}")


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
