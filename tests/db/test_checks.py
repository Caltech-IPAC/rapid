"""Candidate checks and the promotion gate against a live PostgreSQL
(supervisor step 6, 2026-09-24, R1-R5 and plan-review amendments A1-A3).

Covers: both shipped checks recorded as ``checks`` rows (measurements,
bounds, policy and params in detail; required flag from the policy; a
raising check recorded as failed); the gate refusing a missing or failed
required result and admitting a passing one, choosing the latest row
under the policy's own params; the promotions row carrying the policy
version and the row ids relied on; rollback recording no policy;
``create_run`` refusing automatic promotion; ``maybe_auto_promote`` off,
and on with a fixture policy that permits it.

Skips cleanly if PGHOST is unset (see conftest.py). Everything runs
inside the conftest's never-committed transaction.
"""

from __future__ import annotations

import json
import random
from dataclasses import replace

import pytest

from rapidpipe.checks import policy as policy_mod
from rapidpipe.checks import registry
from rapidpipe.checks.policy import Policy, PolicyCheck, load_policy
from rapidpipe.checks.registry import CheckResult
from rapidpipe.checks.runner import (
    Candidate,
    maybe_auto_promote,
    recorded_checks,
    run_candidates,
    run_check,
    run_policy_checks,
)
from rapidpipe.db.ids import new_ulid
from rapidpipe.runs import repository as repo

from .test_repository import _make_run, _make_unit, _register_simple_instance

TRIAL = "rebuild-trial@1"
STRICT = "rebuild-strict@1"

#: diffimmeta values inside rebuild-trial@1's bounds and outside
#: rebuild-strict@1's (the control run's shape).
GOOD_STATS = {"scalefacref": 1.07, "dxrmsfin": 0.21, "dyrmsfin": 0.19,
              "dxmedianfin": 0.02, "dymedianfin": -0.03, "nsexcatsources": 123456,
              "source_counts": {"sextractor": {"positive": 1000, "negative": 900}}}


# ======================================================================
# Helpers
# ======================================================================

def _release(conn):
    tag = f"rebuild-v0.{random.randrange(10**6, 10**9)}"
    digest = "sha256:" + "".join(random.choice("0123456789abcdef") for _ in range(64))
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO releases (tag, source_revision, schema_version, image_digest, "
            "state, cut_by) VALUES (%s, %s, 'x', %s, 'complete', 'test')",
            (tag, "a" * 40, digest))
    return tag, digest


def _selected_attempt(conn, run_id, stage="difference"):
    """A unit with one succeeded, selected attempt that ran a released image."""
    tag, digest = _release(conn)
    unit_id = new_ulid()
    _make_unit(conn, run_id, stage=stage, unit_id=unit_id)
    attempt_id = repo.allocate_attempt(conn, run_id, stage, unit_id)
    repo.record_attempt_result(
        conn, attempt_id, exit_code=0, disposition="succeeded",
        output_location="runs/x", scheduler_job_id="j-" + attempt_id,
        execution_record={"source_revision": "a" * 40, "schema_version": "1",
                          "settings_hash": "h", "image_digest": digest, "release": tag})
    repo.select_attempt(conn, attempt_id)
    return attempt_id


def _diff_candidate(conn, run_id, *, key=None, stats=None, with_row=True):
    """A registered difference-image instance with its diffimages/diffimmeta
    rows. The science rows' foreign keys to reference tables (l2files,
    refimages, exposures ...) are not what these tests are about, so they
    are dropped inside the never-committed transaction (as
    test_run_lifecycle.py does for a CHECK); the run-model ones stay."""
    stats = {**GOOD_STATS, **(stats or {})}
    attempt_id = _selected_attempt(conn, run_id)
    instance = _register_simple_instance(
        conn, run_id, "difference", attempt_id, kind="difference-image",
        logical_key=key or {"k": new_ulid()})
    if not with_row:
        return instance
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT conrelid::regclass::text, conname FROM pg_constraint
            WHERE contype = 'f' AND conrelid IN ('diffimages'::regclass, 'diffimmeta'::regclass)
              AND confrelid NOT IN ('runs'::regclass, 'attempts'::regclass,
                                    'product_instances'::regclass, 'diffimages'::regclass)
            """)
        for table, constraint in cur.fetchall():
            cur.execute(f"ALTER TABLE {table} DROP CONSTRAINT {constraint}")
        cur.execute("SELECT coalesce(max(pid), 0) + 1 FROM diffimages")
        (pid,) = cur.fetchone()
        cur.execute(
            """
            INSERT INTO diffimages (pid, rid, expid, sca, ppid, version, vbest, rfid, field,
                hp6, hp9, fid, ra0, dec0, ra1, dec1, ra2, dec2, ra3, dec3, ra4, dec4,
                infobitssci, infobitsref, filename, svid, run, attempt, instance)
            VALUES (%s, %s, 1, 1, 1, 1, 0, 1, 1, 1, 1, 1, 10, 0, 10, 0, 10, 0, 10, 0, 10, 0,
                    0, 0, 'diff.fits', 1, %s, %s, %s)
            """,
            (pid, pid, run_id, attempt_id, instance))
        cur.execute(
            """
            INSERT INTO diffimmeta (pid, nsexcatsources, scalefacref, dxrmsfin, dyrmsfin,
                dxmedianfin, dymedianfin, field, hp6, hp9, fid, sca, source_counts,
                run, attempt, instance)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 1, 1, 1, 1, 1, %s, %s, %s, %s)
            """,
            (pid, stats["nsexcatsources"], stats["scalefacref"], stats["dxrmsfin"],
             stats["dyrmsfin"], stats["dxmedianfin"], stats["dymedianfin"],
             None if stats["source_counts"] is None else json.dumps(stats["source_counts"]),
             run_id, attempt_id, instance))
    return instance


def _source_set(conn, run_id, *, key, rows, complete=True):
    attempt_id = _selected_attempt(conn, run_id, stage="load")
    instance = new_ulid()
    repo.register_manifest(conn, {
        "run": run_id, "stage": "load", "attempt": attempt_id,
        "inputs": {"products": {}, "result_sets": []},
        "outputs": [{"kind": "source-set", "format_version": "1", "instance": instance,
                     "key": key, "primary": None, "members": [], "registration": {},
                     "row_count": rows}],
    }, registering_attempt_id=attempt_id)
    if not complete:
        with conn.cursor() as cur:
            cur.execute("UPDATE result_sets SET complete = false WHERE instance = %s",
                        (instance,))
    return instance


def _candidate(conn, instance):
    with conn.cursor() as cur:
        cur.execute("SELECT id, kind, logical_key FROM product_instances WHERE id = %s",
                    (instance,))
        return Candidate(*cur.fetchone())


def _check_rows(conn, instance):
    with conn.cursor() as cur:
        cur.execute("SELECT check_name, version, required, outcome, detail FROM checks "
                    "WHERE instance = %s ORDER BY happened_at, id", (instance,))
        return cur.fetchall()


def _promotion(conn, promotion_id):
    with conn.cursor() as cur:
        cur.execute("SELECT check_policy_version, check_result_ids::text[] FROM promotions "
                    "WHERE id = %s", (promotion_id,))
        return cur.fetchone()


def _custody(conn, instance):
    with conn.cursor() as cur:
        cur.execute("SELECT custody FROM product_instances WHERE id = %s", (instance,))
        return cur.fetchone()[0]


def _savepoint_raises(conn, exc_type, fn, match=None):
    with conn.cursor() as cur:
        cur.execute("SAVEPOINT t")
        with pytest.raises(exc_type, match=match) as info:
            fn()
        cur.execute("ROLLBACK TO SAVEPOINT t")
    return info


# ======================================================================
# difference-image-statistics@1
# ======================================================================

def test_difference_statistics_pass_trial_fail_strict_and_record_everything(conn):
    run_id = _make_run(conn)
    instance = _diff_candidate(conn, run_id)

    (trial,) = run_policy_checks(conn, run_id, load_policy(TRIAL), who="tester")
    (strict,) = run_policy_checks(conn, run_id, load_policy(STRICT))
    assert (trial.outcome, strict.outcome) == ("passed", "failed")
    assert trial.check_ref == "difference-image-statistics@1" and trial.required

    rows = _check_rows(conn, instance)
    assert [(r[0], r[1], r[2], r[3]) for r in rows] == [
        ("difference-image-statistics", "1", True, "passed"),
        ("difference-image-statistics", "1", True, "failed")]
    detail = rows[1][4]
    assert detail["policy"] == STRICT
    assert detail["params"] == load_policy(STRICT).checks[0].params
    assert detail["measurements"]["nsexcatsources"] == 123456
    assert detail["bounds"]["nsexcatsources"] == [0, 1000]
    assert set(detail["failing"]) == {"nsexcatsources", "scalefacref", "dxrmsfin", "dyrmsfin"}
    assert "nsexcatsources=123456 outside [0, 1000]" in detail["summary"]
    assert rows[0][4]["who"] == "tester"
    assert rows[0][4]["summary"] == "7 measurements within bounds"


def test_difference_statistics_ratio_only_when_source_counts_present(conn):
    run_id = _make_run(conn)
    lopsided = _diff_candidate(
        conn, run_id, stats={"source_counts": {"sextractor": {"positive": 900, "negative": 100}}})
    absent = _diff_candidate(conn, run_id, stats={"source_counts": None})
    results = {r.instance: r for r in run_policy_checks(conn, run_id, load_policy(TRIAL))}
    assert results[lopsided].outcome == "failed"
    assert results[lopsided].detail["failing"] == ["sextractor_pos_neg_ratio"]
    assert results[absent].outcome == "passed"
    assert "ratio bound was not applied" in results[absent].detail["notes"][0]


def test_difference_statistics_without_a_diffimmeta_row_records_failed(conn):
    run_id = _make_run(conn)
    instance = _diff_candidate(conn, run_id, with_row=False)
    (result,) = run_policy_checks(conn, run_id, load_policy(TRIAL))
    assert result.outcome == "failed"
    assert _check_rows(conn, instance)[0][4]["reason"] == "no diffimmeta row for this instance"


def test_a_raising_check_is_recorded_failed_with_the_error(conn, monkeypatch):
    def boom(conn, instance_id, params):
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM no_such_table")   # aborts the savepoint only
        return CheckResult("passed")

    registry._load_builtins()
    monkeypatch.setitem(registry._REGISTRY, "boom@1", registry.RegisteredCheck(
        "boom", "1", "difference-image", (), boom))
    run_id = _make_run(conn)
    instance = _diff_candidate(conn, run_id)
    result = run_check(conn, _candidate(conn, instance),
                       PolicyCheck("boom", "1", "difference-image", True, {}),
                       policy_ref="fixture@1")
    assert result.outcome == "failed"
    ((name, _v, _req, outcome, detail),) = _check_rows(conn, instance)
    assert (name, outcome) == ("boom", "failed")
    assert "UndefinedTable" in detail["error"]


# ======================================================================
# catalog-counts-vs-reference@1
# ======================================================================

def _catalog(conn, run_id, instance, policy=TRIAL, **params):
    pc = load_policy(policy).checks[1]
    return run_check(conn, _candidate(conn, instance), pc, policy_ref=policy,
                     params={**pc.params, **params} if params else None)


def test_catalog_counts_missing_reference_passes_trial_fails_strict(conn):
    run_id = _make_run(conn)
    instance = _source_set(conn, run_id, key={"k": new_ulid()}, rows=100)
    trial = _catalog(conn, run_id, instance)
    strict = _catalog(conn, run_id, instance, STRICT)
    assert (trial.outcome, strict.outcome) == ("passed", "failed")
    assert trial.required is False
    assert trial.detail["reference"] == {"instance": None, "chosen_as": "current",
                                         "row_count": None}


def test_catalog_counts_against_the_current_instance(conn):
    key = {"k": new_ulid()}
    old_run = _make_run(conn)
    old = _source_set(conn, old_run, key=key, rows=1000)
    repo.promote_run(conn, old_run, "t", "first")
    run_id = _make_run(conn)
    near = _source_set(conn, run_id, key=key, rows=1080)
    result = _catalog(conn, run_id, near)
    assert result.outcome == "passed"
    assert result.detail["reference"]["instance"] == old
    assert result.detail["measurements"]["relative_difference"] == pytest.approx(0.08)
    far = _catalog(conn, run_id, near, tolerance=0.05)
    assert far.outcome == "failed" and far.detail["failing"] == ["relative_difference"]


def test_catalog_counts_when_the_candidate_is_current_uses_the_previous_current(conn):
    key = {"k": new_ulid()}
    old_run = _make_run(conn)
    old = _source_set(conn, old_run, key=key, rows=1000)
    repo.promote_run(conn, old_run, "t", "first")
    run_id = _make_run(conn)
    new = _source_set(conn, run_id, key=key, rows=2000)
    repo.promote_run(conn, run_id, "t", "second")
    result = _catalog(conn, run_id, new)
    assert result.detail["reference"]["instance"] == old
    assert result.detail["reference"]["chosen_as"].startswith("previous current")
    assert result.outcome == "failed"


def test_catalog_counts_against_a_named_reference_run(conn):
    key = {"k": new_ulid()}
    control = _make_run(conn)
    _source_set(conn, control, key=key, rows=500)
    run_id = _make_run(conn)
    instance = _source_set(conn, run_id, key=key, rows=500)
    result = _catalog(conn, run_id, instance, reference_run=control)
    assert result.outcome == "passed"
    assert result.detail["reference"]["chosen_as"] == f"run {control}"


def test_catalog_counts_refuse_an_incomplete_candidate(conn):
    run_id = _make_run(conn)
    instance = _source_set(conn, run_id, key={"k": new_ulid()}, rows=5, complete=False)
    assert _catalog(conn, run_id, instance).outcome == "failed"


# ======================================================================
# The promotion gate (R4, A1, A2)
# ======================================================================

def test_promotion_refuses_a_required_check_with_no_result(conn):
    run_id = _make_run(conn)
    instance = _diff_candidate(conn, run_id)
    _savepoint_raises(
        conn, repo.PromotionRefused, lambda: repo.promote_run(conn, run_id, "t", "go"),
        match=(f"check policy {TRIAL}: required check difference-image-statistics@1 on "
               f"instance {instance} \\(kind difference-image\\) has no result; refusing"))


def test_promotion_admits_passing_checks_and_records_version_and_ids(conn):
    run_id = _make_run(conn)
    diff = _diff_candidate(conn, run_id)
    catalog = _source_set(conn, run_id, key={"k": new_ulid()}, rows=10)
    results = run_policy_checks(conn, run_id, load_policy(TRIAL))
    promotion_id = repo.promote_run(conn, run_id, "t", "go")
    version, ids = _promotion(conn, promotion_id)
    assert version == TRIAL
    assert sorted(ids) == sorted(r.id for r in results)
    assert (_custody(conn, diff), _custody(conn, catalog)) == ("current", "current")


def test_promotion_under_strict_refuses_naming_the_failed_check(conn):
    run_id = _make_run(conn)
    instance = _diff_candidate(conn, run_id)
    run_policy_checks(conn, run_id, load_policy(STRICT))
    info = _savepoint_raises(
        conn, repo.PromotionRefused,
        lambda: repo.promote_run(conn, run_id, "t", "go", check_policy=STRICT))
    message = str(info.value)
    assert message.startswith(
        f"check policy {STRICT}: required check difference-image-statistics@1 on "
        f"instance {instance} (kind difference-image) is failed (")
    assert message.endswith("); refusing")


def test_the_run_check_policy_ref_is_used_when_none_is_given(conn):
    run_id = _make_run(conn, check_policy_ref=STRICT)
    _diff_candidate(conn, run_id)
    run_policy_checks(conn, run_id, load_policy(TRIAL))   # passes, but under trial params
    _savepoint_raises(conn, repo.PromotionRefused,
                      lambda: repo.promote_run(conn, run_id, "t", "go"), match="has no result")
    promotion_id = repo.promote_run(conn, run_id, "t", "go", check_policy=TRIAL)
    assert _promotion(conn, promotion_id)[0] == TRIAL


def test_the_latest_row_under_the_policy_params_decides(conn):
    run_id = _make_run(conn)
    instance = _diff_candidate(conn, run_id)
    trial = load_policy(TRIAL)
    # A later pass under overridden params does not count (A2) ...
    run_policy_checks(conn, run_id, load_policy(STRICT))
    with conn.cursor() as cur:
        cur.execute("UPDATE checks SET detail = jsonb_set(detail, '{params}', %s::jsonb) "
                    "WHERE instance = %s", (trial.checks[0].params_json(), instance))
    run_policy_checks(conn, run_id, trial, check="difference-image-statistics@1",
                      param_overrides={"n_max": 10**9})
    _savepoint_raises(conn, repo.PromotionRefused,
                      lambda: repo.promote_run(conn, run_id, "t", "go"), match="is failed")
    # ... a later pass under the policy's own params does.
    (passed,) = run_policy_checks(conn, run_id, trial)
    promotion_id = repo.promote_run(conn, run_id, "t", "go")
    assert _promotion(conn, promotion_id)[1] == [passed.id]


def test_a_failed_advisory_check_does_not_refuse(conn):
    run_id = _make_run(conn)
    catalog = _source_set(conn, run_id, key={"k": new_ulid()}, rows=10)
    (failed,) = [r for r in run_policy_checks(conn, run_id, load_policy(STRICT))]
    assert failed.outcome == "failed" and not failed.required
    promotion_id = repo.promote_run(conn, run_id, "t", "go", check_policy=STRICT)
    assert _promotion(conn, promotion_id) == (STRICT, [failed.id])
    assert _custody(conn, catalog) == "current"


def test_an_unapproved_policy_admits_no_promotion(conn, monkeypatch):
    unapproved = replace(load_policy(TRIAL), name="unapproved", approval="none",
                         approved_by=None)
    monkeypatch.setitem(policy_mod._FIXTURE_POLICIES, "unapproved@1", unapproved)
    run_id = _make_run(conn)
    _source_set(conn, run_id, key={"k": new_ulid()}, rows=10)
    _savepoint_raises(conn, repo.PromotionRefused,
                      lambda: repo.promote_run(conn, run_id, "t", "go",
                                               check_policy="unapproved@1"),
                      match="is not approved")
    _savepoint_raises(conn, repo.PromotionRefused,
                      lambda: repo.promote_run(conn, run_id, "t", "go",
                                               check_policy="nosuch@1"),
                      match="does not exist")


def test_rollback_records_no_policy(conn):
    run_id = _make_run(conn)
    _source_set(conn, run_id, key={"k": new_ulid()}, rows=10)
    promotion_id = repo.promote_run(conn, run_id, "t", "go")
    rollback = repo.rollback_promotion(conn, promotion_id, "t", "undo")
    assert _promotion(conn, promotion_id)[0] == TRIAL
    assert _promotion(conn, rollback) == (None, [])


def test_promotion_refuses_a_dependency_on_an_incomplete_result_set(conn):
    run_id = _make_run(conn)
    producer = _source_set(conn, run_id, key={"k": new_ulid()}, rows=3)
    attempt_id = _selected_attempt(conn, run_id)
    _register_simple_instance(conn, run_id, "difference", attempt_id,
                              logical_key={"k": new_ulid()},
                              input_products={"source-set": producer})
    with conn.cursor() as cur:
        cur.execute("UPDATE result_sets SET complete = false WHERE instance = %s", (producer,))
    _savepoint_raises(conn, repo.PromotionRefused,
                      lambda: repo.promote_run(conn, run_id, "t", "go", kinds=["test-product"]),
                      match="incomplete result set")


# ======================================================================
# check run / show helpers
# ======================================================================

def test_candidates_are_selected_attempt_instances_of_any_custody(conn):
    run_id = _make_run(conn, kind="scratch")
    instance = _diff_candidate(conn, run_id)
    assert [c.id for c in run_candidates(conn, run_id)] == [instance]
    run_policy_checks(conn, run_id, load_policy(TRIAL))
    run_policy_checks(conn, run_id, load_policy(STRICT))
    shown = recorded_checks(conn, run_id)
    assert [r.outcome for r in shown] == ["failed", "passed"]   # newest first
    assert recorded_checks(conn, run_id, instance="0" * 26) == []


# ======================================================================
# Automatic promotion (R5)
# ======================================================================

def test_create_run_refuses_auto_promote_and_unknown_policies(conn):
    for ref in (None, TRIAL, STRICT):
        _savepoint_raises(conn, repo.CheckPolicyRefused,
                          lambda: _make_run(conn, auto_promote=True, check_policy_ref=ref),
                          match="does not permit automatic promotion; lead approval pending")
    _savepoint_raises(conn, repo.CheckPolicyRefused,
                      lambda: _make_run(conn, check_policy_ref="nosuch@1"),
                      match="does not exist")
    run_id = _make_run(conn, check_policy_ref=STRICT)
    with conn.cursor() as cur:
        cur.execute("SELECT check_policy_ref, auto_promote FROM runs WHERE id = %s", (run_id,))
        assert cur.fetchone() == (STRICT, False)


def test_maybe_auto_promote_is_off_for_an_ordinary_run(conn):
    run_id = _make_run(conn)
    _diff_candidate(conn, run_id)
    outcome = maybe_auto_promote(conn, run_id)
    assert (outcome.status, outcome.message) == ("off", f"auto-promote off (policy {TRIAL})")
    assert recorded_checks(conn, run_id) == []


def _auto_policy(base: str, name: str) -> Policy:
    return replace(load_policy(base), name=name, approval="lead",
                   approved_by="lead-login", auto_promote=True)


def test_maybe_auto_promote_with_a_permitting_policy_checks_and_promotes(conn, monkeypatch):
    monkeypatch.setitem(policy_mod._FIXTURE_POLICIES, "auto@1", _auto_policy(TRIAL, "auto"))
    run_id = _make_run(conn, auto_promote=True, check_policy_ref="auto@1")
    diff = _diff_candidate(conn, run_id)
    outcome = maybe_auto_promote(conn, run_id)
    assert outcome.status == "promoted", outcome.message
    assert _custody(conn, diff) == "current"
    version, ids = _promotion(conn, outcome.promotion_id)
    assert version == "auto@1" and ids == [outcome.checks[0].id]


def test_maybe_auto_promote_refused_keeps_the_check_rows(conn, monkeypatch):
    monkeypatch.setitem(policy_mod._FIXTURE_POLICIES, "auto-strict@1",
                        _auto_policy(STRICT, "auto-strict"))
    run_id = _make_run(conn, auto_promote=True, check_policy_ref="auto-strict@1")
    diff = _diff_candidate(conn, run_id)
    outcome = maybe_auto_promote(conn, run_id)
    assert outcome.status == "refused"
    assert "is failed" in outcome.message
    assert _custody(conn, diff) == "candidate"
    assert [r[3] for r in _check_rows(conn, diff)] == ["failed"]


def test_maybe_auto_promote_skips_while_units_are_incomplete(conn, monkeypatch):
    monkeypatch.setitem(policy_mod._FIXTURE_POLICIES, "auto@1", _auto_policy(TRIAL, "auto"))
    run_id = _make_run(conn, auto_promote=True, check_policy_ref="auto@1")
    _diff_candidate(conn, run_id)
    _make_unit(conn, run_id, unit_id=new_ulid())   # pending
    outcome = maybe_auto_promote(conn, run_id)
    assert outcome.status == "skipped"
    assert "1 of 2 units not complete" in outcome.message


# ======================================================================
# rapidpipe check run / show / run create through main(argv)
# ======================================================================

class _Tx:
    """The conftest connection with commit and rollback made no-ops, so
    the CLI's rows stay inside the test's never-committed transaction."""

    def __init__(self, conn):
        self._conn = conn

    def commit(self):
        pass

    def rollback(self):
        pass

    def __getattr__(self, name):
        return getattr(self._conn, name)


@pytest.fixture()
def cli_conn(conn, monkeypatch):
    import contextlib

    from rapidpipe.cli import main as cli

    @contextlib.contextmanager
    def _connect(**_kwargs):
        yield _Tx(conn)

    monkeypatch.setattr(cli, "connect", _connect)
    return cli


def test_check_run_and_show_print_one_line_per_result(conn, cli_conn, capsys):
    run_id = _make_run(conn, kind="scratch")
    diff = _diff_candidate(conn, run_id, key={"exposure": 7, "sca": 3})

    assert cli_conn.main(["check", "run", run_id, "--who", "ops"]) == 0
    (line,) = capsys.readouterr().out.splitlines()
    assert line == (f'instance={diff} kind=difference-image key={{"exposure":7,"sca":3}} '
                    "check=difference-image-statistics@1 required=true outcome=passed "
                    "7 measurements within bounds")

    assert cli_conn.main(["check", "run", run_id, "--policy", STRICT]) == 1
    out = capsys.readouterr().out
    assert "required=true outcome=failed scalefacref=1.07 outside [0.99, 1.01]; " in out
    assert "dxrmsfin=0.21 > 0.01; " in out
    assert out.rstrip().endswith("nsexcatsources=123456 outside [0, 1000]")

    assert cli_conn.main(["check", "run", run_id, "--check", "difference-image-statistics@1",
                          "--param", "n_max=1e9", "--param", "scalefacref_hi=2",
                          "--instance", diff]) == 0
    capsys.readouterr()
    (row,) = [r for r in recorded_checks(conn, run_id) if r.detail["params"]["n_max"] == 1e9]
    assert row.detail["params"]["scalefacref_hi"] == 2

    assert cli_conn.main(["check", "show", run_id, "--instance", diff]) == 0
    shown = capsys.readouterr().out.splitlines()
    assert len(shown) == 3
    assert shown[0].startswith(f"id={row.id} at=")
    assert f" instance={diff} kind=difference-image " in shown[0]


@pytest.mark.parametrize("argv, message", [
    (["--instance", "0" * 26], "is not a product of run"),
    (["--check", "nosuch@1"], "names no check nosuch@1"),
    (["--check", "difference-image-statistics@1", "--param", "bogus=1"], "takes no parameter bogus"),
    (["--policy", "nosuch@1"], "does not exist"),
])
def test_check_run_usage_errors_exit_64(conn, cli_conn, capsys, argv, message):
    run_id = _make_run(conn)
    _diff_candidate(conn, run_id)
    assert cli_conn.main(["check", "run", run_id, *argv]) == 64
    assert message in capsys.readouterr().err
    assert recorded_checks(conn, run_id) == []


def test_run_promote_check_policy_flag_reaches_the_gate(conn, cli_conn, capsys):
    run_id = _make_run(conn)
    _diff_candidate(conn, run_id)
    run_policy_checks(conn, run_id, load_policy(STRICT))
    assert cli_conn.main(["run", "promote", run_id, "--reason", "r",
                          "--check-policy", STRICT]) == 64
    err = capsys.readouterr().err
    assert err.startswith(f"rapidpipe run promote: check policy {STRICT}: required check "
                          "difference-image-statistics@1 on instance ")
    assert err.rstrip().endswith("; refusing")


def test_run_create_check_policy_and_auto_promote(conn, cli_conn, capsys):
    base = ["run", "create", "--kind", "production", "--purpose", "p", "--stages", "admit"]
    assert cli_conn.main(base + ["--auto-promote"]) == 64
    assert capsys.readouterr().err == (
        f"rapidpipe run create: policy {TRIAL} does not permit automatic promotion; "
        "lead approval pending\n")
    assert cli_conn.main(base + ["--check-policy", "nosuch@1"]) == 64
    capsys.readouterr()
    assert cli_conn.main(base + ["--check-policy", STRICT]) == 0
    run_id = capsys.readouterr().out.strip()
    with conn.cursor() as cur:
        cur.execute("SELECT check_policy_ref, auto_promote FROM runs WHERE id = %s", (run_id,))
        assert cur.fetchone() == (STRICT, False)
