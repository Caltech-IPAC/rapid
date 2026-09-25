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
from pathlib import Path

import pytest

from rapidpipe.checks import policy as policy_mod
from rapidpipe.checks import registry
from rapidpipe.checks.policy import PolicyCheck, load_policy, load_policy_file
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

#: The control run's diffimmeta values in rapid_rebuild (supervisor step 6,
#: live-values correction): inside rebuild-trial@1's bounds, outside
#: rebuild-strict@1's.
GOOD_STATS = {"scalefacref": 17572.896, "dxrmsfin": 0.25, "dyrmsfin": 0.56,
              "dxmedianfin": 0.004, "dymedianfin": -0.48, "nsexcatsources": 21749,
              "source_counts": {"sextractor": {"positive": 21749, "negative": 55451},
                                "photutils": {"positive": 85079, "negative": 58599}}}

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "checks"


def _fixture_policy(monkeypatch, ref):
    """Install a tests/fixtures/checks policy (never shipped) by its ref."""
    policy = load_policy_file(FIXTURES / f"{ref}.toml")
    monkeypatch.setitem(policy_mod._FIXTURE_POLICIES, ref, policy)
    return policy


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
    assert detail["measurements"]["nsexcatsources"] == 21749
    assert detail["bounds"]["nsexcatsources"] == [0, 1000]
    assert detail["failing"] == ["scalefacref", "dxrmsfin", "dyrmsfin", "abs_dymedianfin",
                                 "nsexcatsources", "sextractor_pos_neg_ratio"]
    assert "nsexcatsources=21749 outside [0, 1000]" in detail["summary"]
    assert rows[0][4]["who"] == "tester"
    assert rows[0][4]["summary"] == "7 measurements within bounds"


def test_difference_statistics_ratio_only_when_source_counts_present(conn):
    run_id = _make_run(conn)
    lopsided = _diff_candidate(
        conn, run_id, stats={"source_counts": {"sextractor": {"positive": 90000, "negative": 100}}})
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


def test_non_finite_medians_record_the_failed_check(conn):
    """Codex diff review of step 6 (P1): NaN/Infinity signed medians are
    recorded as text, so the failed check's row is written, not lost to a
    jsonb error."""
    run_id = _make_run(conn)
    instance = _diff_candidate(conn, run_id, stats={"dxmedianfin": float("nan"),
                                                    "dymedianfin": float("inf")})
    (result,) = run_policy_checks(conn, run_id, load_policy(TRIAL))
    assert result.outcome == "failed"
    ((_n, _v, _r, outcome, detail),) = _check_rows(conn, instance)
    assert outcome == "failed"
    assert detail["measurements"]["dxmedianfin"] == "nan"
    assert detail["measurements"]["dymedianfin"] == "inf"
    assert detail["failing"] == ["abs_dxmedianfin", "abs_dymedianfin"]


def test_a_check_detail_with_a_raw_nan_is_recorded_failed_with_the_error(conn, monkeypatch):
    """The recording path serialises with allow_nan=False: a stray NaN in a
    check's detail records a failed row carrying the error and the evidence
    (non-finite values as text), never a lost row."""
    def stray(conn, instance_id, params):
        return CheckResult("passed", {"measurements": {"x": float("nan"), "y": [1.0, float("inf")]}},
                           "all fine")

    registry._load_builtins()
    monkeypatch.setitem(registry._REGISTRY, "stray@1", registry.RegisteredCheck(
        "stray", "1", "difference-image", (), stray))
    run_id = _make_run(conn)
    instance = _diff_candidate(conn, run_id)
    result = run_check(conn, _candidate(conn, instance),
                       PolicyCheck("stray", "1", "difference-image", True, {}),
                       policy_ref="fixture@1")
    assert result.outcome == "failed"
    ((name, _v, _req, outcome, detail),) = _check_rows(conn, instance)
    assert (name, outcome) == ("stray", "failed")
    assert "not recordable" in detail["error"] and "Out of range float" in detail["error"]
    assert detail["measurements"] == {"x": "nan", "y": [1.0, "inf"]}
    assert "check reported passed: all fine" in detail["summary"]


# ======================================================================
# catalog-counts-vs-reference@1: reference by science identity
# ======================================================================

def _l2(conn, run_id, *, expid, sca, fid):
    """A registered l2-image instance with an ``l2files`` row carrying
    (expid, sca, fid). Its reference-table foreign keys are dropped and its
    other NOT NULL columns filled with placeholders, inside the
    never-committed transaction."""
    attempt_id = _selected_attempt(conn, run_id, stage="admit")
    instance = _register_simple_instance(conn, run_id, "admit", attempt_id, kind="l2-image",
                                         logical_key={"k": new_ulid()})
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT conname FROM pg_constraint
            WHERE contype = 'f' AND conrelid = 'l2files'::regclass
              AND confrelid NOT IN ('runs'::regclass, 'attempts'::regclass,
                                    'product_instances'::regclass)
            """)
        for (constraint,) in cur.fetchall():
            cur.execute(f"ALTER TABLE l2files DROP CONSTRAINT {constraint}")
        cur.execute("SELECT coalesce(max(rid), 0) + 1 FROM l2files")
        (rid,) = cur.fetchone()
        values = {"rid": rid, "expid": expid, "sca": sca, "fid": fid, "version": 1, "vbest": 0,
                  "run": run_id, "attempt": attempt_id, "instance": instance}
        cur.execute(
            """
            SELECT column_name, data_type FROM information_schema.columns
            WHERE table_name = 'l2files' AND is_nullable = 'NO' AND column_default IS NULL
            """)
        for column, data_type in cur.fetchall():
            if column in values:
                continue
            if "timestamp" in data_type:
                values[column] = "2026-09-24T00:00:00"
            elif "char" in data_type or data_type == "text":
                values[column] = "x"
            else:
                values[column] = 0
        columns = list(values)
        cur.execute(
            f"INSERT INTO l2files ({', '.join(columns)}) "
            f"VALUES ({', '.join(['%s'] * len(columns))})",
            [values[c] for c in columns])
    return instance


def _chain(conn, run_id, *, identity=(7001, 3, 1), rows, catalog_type="sextractor",
           complete=True):
    """l2 -> difference-image -> source-set, keyed as the live stages key
    them; returns the source-set instance."""
    expid, sca, fid = identity
    l2 = _l2(conn, run_id, expid=expid, sca=sca, fid=fid)
    attempt_id = _selected_attempt(conn, run_id)
    diff = _register_simple_instance(
        conn, run_id, "difference", attempt_id, kind="difference-image",
        logical_key={"l2": l2, "reference": "REF", "differencer": "sfft",
                     "settings_hash": new_ulid()})
    return _source_set(conn, run_id, key={"difference": diff, "catalog_type": catalog_type},
                       rows=rows, complete=complete)


def _catalog(conn, instance, policy=TRIAL, **params):
    pc = load_policy(policy).find_check("catalog-counts-vs-reference@1")
    return run_check(conn, _candidate(conn, instance), pc, policy_ref=policy,
                     params={**pc.params, **params} if params else None)


def _promote_source_sets(conn, run_id):
    return repo.promote_run(conn, run_id, "t", "reference", kinds=["source-set"])


def _identity_triple():
    return (random.randrange(10**6, 10**9), 3, 1)


def test_catalog_counts_missing_reference_passes_trial_fails_strict(conn):
    run_id = _make_run(conn)
    instance = _chain(conn, run_id, identity=_identity_triple(), rows=100)
    trial = _catalog(conn, instance)
    strict = _catalog(conn, instance, STRICT)
    assert (trial.outcome, strict.outcome) == ("passed", "failed")
    assert trial.required is False
    assert trial.detail["reference"]["instance"] is None


def test_catalog_counts_against_the_current_instance_of_the_same_identity(conn):
    identity = _identity_triple()
    control = _make_run(conn)
    reference = _chain(conn, control, identity=identity, rows=1000)
    _promote_source_sets(conn, control)
    # Not references: another sca, and the other catalog type.
    decoy = _make_run(conn)
    _chain(conn, decoy, identity=(identity[0], 4, 1), rows=5)
    _chain(conn, decoy, identity=identity, rows=5, catalog_type="photutils")
    _promote_source_sets(conn, decoy)

    run_id = _make_run(conn)
    candidate = _chain(conn, run_id, identity=identity, rows=1080)
    result = _catalog(conn, candidate)
    assert result.outcome == "passed", result.summary
    assert result.detail["reference"] == {"instance": reference, "run": control,
                                          "chosen_as": "current", "row_count": 1000}
    assert result.detail["identity"] == {"expid": identity[0], "sca": 3, "fid": 1,
                                         "catalog_type": "sextractor"}
    assert result.detail["measurements"]["relative_difference"] == pytest.approx(0.08)
    tight = _catalog(conn, candidate, tolerance=0.05)
    assert tight.outcome == "failed" and tight.detail["failing"] == ["relative_difference"]


def test_catalog_counts_never_use_the_candidates_own_run(conn):
    identity = _identity_triple()
    run_id = _make_run(conn)
    candidate = _chain(conn, run_id, identity=identity, rows=10)
    _promote_source_sets(conn, run_id)
    result = _catalog(conn, candidate, STRICT)
    assert result.detail["reference"]["instance"] is None
    assert result.outcome == "failed"      # missing_reference = fail


def test_catalog_counts_against_a_named_reference_run(conn):
    identity = _identity_triple()
    control = _make_run(conn)          # never promoted: named explicitly
    reference = _chain(conn, control, identity=identity, rows=500)
    run_id = _make_run(conn)
    candidate = _chain(conn, run_id, identity=identity, rows=500)
    result = _catalog(conn, candidate, reference_run=control)
    assert result.outcome == "passed"
    assert result.detail["reference"]["instance"] == reference
    assert result.detail["reference"]["chosen_as"] == f"run {control}"
    other = _make_run(conn)
    absent = _catalog(conn, candidate, reference_run=other)
    assert absent.outcome == "failed" and absent.detail["failing"] == ["reference"]


def test_catalog_counts_fail_an_incomplete_or_unresolvable_candidate(conn):
    run_id = _make_run(conn)
    incomplete = _chain(conn, run_id, identity=_identity_triple(), rows=5, complete=False)
    assert _catalog(conn, incomplete).outcome == "failed"
    loose = _source_set(conn, run_id, key={"k": new_ulid()}, rows=5)
    result = _catalog(conn, loose)
    assert result.outcome == "failed"
    assert result.detail["failing"] == ["identity"]


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
    _fixture_policy(monkeypatch, "unapproved@1")
    run_id = _make_run(conn)
    _source_set(conn, run_id, key={"k": new_ulid()}, rows=10)
    _savepoint_raises(conn, repo.PromotionRefused,
                      lambda: repo.promote_run(conn, run_id, "t", "go",
                                               check_policy="unapproved@1"),
                      match="check policy unapproved@1 is not approved; refusing")
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


def test_maybe_auto_promote_with_a_permitting_policy_checks_and_promotes(conn, monkeypatch):
    _fixture_policy(monkeypatch, "auto-trial@1")
    run_id = _make_run(conn, auto_promote=True, check_policy_ref="auto-trial@1")
    diff = _diff_candidate(conn, run_id)
    outcome = maybe_auto_promote(conn, run_id)
    assert outcome.status == "promoted", outcome.message
    assert _custody(conn, diff) == "current"
    version, ids = _promotion(conn, outcome.promotion_id)
    assert version == "auto-trial@1" and ids == [outcome.checks[0].id]


def test_maybe_auto_promote_refused_keeps_the_check_rows(conn, monkeypatch):
    _fixture_policy(monkeypatch, "auto-strict@1")
    run_id = _make_run(conn, auto_promote=True, check_policy_ref="auto-strict@1")
    diff = _diff_candidate(conn, run_id)
    outcome = maybe_auto_promote(conn, run_id)
    assert outcome.status == "refused"
    assert "is failed" in outcome.message
    assert _custody(conn, diff) == "candidate"
    assert [r[3] for r in _check_rows(conn, diff)] == ["failed"]


def test_maybe_auto_promote_skips_while_units_are_incomplete(conn, monkeypatch):
    _fixture_policy(monkeypatch, "auto-trial@1")
    run_id = _make_run(conn, auto_promote=True, check_policy_ref="auto-trial@1")
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
    assert "required=true outcome=failed scalefacref=17572.9 outside [0.99, 1.01]; " in out
    assert "dxrmsfin=0.25 > 0.01; " in out
    assert out.rstrip().endswith("sextractor_pos_neg_ratio=0.39222 outside [0.9, 1.1]")

    assert cli_conn.main(["check", "run", run_id, "--check", "difference-image-statistics@1",
                          "--param", "n_max=1e9", "--param", "scalefacref_hi=1e6",
                          "--instance", diff]) == 0
    capsys.readouterr()
    (row,) = [r for r in recorded_checks(conn, run_id) if r.detail["params"]["n_max"] == 1e9]
    assert row.detail["params"]["scalefacref_hi"] == 1e6

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
