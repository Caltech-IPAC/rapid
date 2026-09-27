"""Supersession by slot without a database (supervisor step 5a, 2026-09-26).

The selector and plan helpers (``rapidpipe.runs.slots``); ``promote``'s
selector handling, refusals and the association-set ancestor rule;
``promote_run``'s frozen plan; ``rollback_promotion``'s recorded
selectors; the quiet fill; ``run promote-plan`` and ``run promote --plan``
through the CLI; the reference check reading the slot. The derivation
itself is SQL (migration 20260926-02) and is proven by the DB-backed tests.
"""

from __future__ import annotations

import contextlib
import json

import psycopg2
import pytest

from rapidpipe.checks import builtin, runner
from rapidpipe.cli import main as cli
from rapidpipe.runs import repository, slots

# ======================================================================
# An in-memory product_instances for promote (substring-matched SQL)
# ======================================================================


class _Db:
    """Enough of product_instances, promotions and promotion_changes for
    :func:`repository.promote` and :func:`repository.promote_run`."""

    def __init__(self, instances):
        self.instances = {i["id"]: {"custody": "candidate", "deletion_state": "retained",
                                    "logical_key": {}, "slot": None, **i}
                          for i in instances}
        self.changes: list[tuple] = []
        self.promotions: list[tuple] = []
        self.executed: list[str] = []
        self.fill_error: Exception | None = None
        self.run = ("production", "open", None)

    def current(self, kind, column, value):
        for row in self.instances.values():
            if row["kind"] == kind and row[column] == value and row["custody"] == "current":
                return row["id"]
        return None

    def execute(self, sql, params=()):  # noqa: C901 - one branch per statement shape
        text = " ".join(sql.split())
        self.executed.append(text)
        if "pg_advisory_xact_lock" in text or "SAVEPOINT" in text:
            return []
        if "FROM product_identity_fill()" in text:
            if self.fill_error is not None:
                raise self.fill_error
            return [("source-set", 1, 0, 0)]
        if text.startswith("SELECT kind, state, check_policy_ref FROM runs"):
            return [self.run]
        if text.startswith("SELECT pi.id, pi.kind, pi.slot FROM product_instances pi"):
            return [(r["id"], r["kind"], r["slot"]) for r in sorted(
                        self.instances.values(), key=lambda r: (r["kind"], r["id"]))
                    if r.get("run") == params[0] and r["custody"] == "candidate"]
        if text.startswith("SELECT id FROM product_instances WHERE kind = %s AND slot = %s"):
            found = self.current(params[0], "slot", json.loads(params[1]))
            return [(found,)] if found else []
        if text.startswith("SELECT id FROM product_instances WHERE kind = %s AND logical_key"):
            found = self.current(params[0], "logical_key", json.loads(params[1]))
            return [(found,)] if found else []
        if text.startswith("SELECT pi.custody, pi.kind, pi.slot, pi.logical_key"):
            r = self.instances.get(params[0])
            return [] if r is None else [(r["custody"], r["kind"], r["slot"], r["logical_key"],
                                          r["deletion_state"], False, None)]
        if text.startswith("SELECT pi.producing_attempt, u.selected_attempt"):
            return [("A", "A")]
        if "FROM dependencies d" in text:
            return []
        if text.startswith("SELECT o.id FROM product_instances o"):
            after, before = params
            a = self.instances[after]
            return [(r["id"],) for r in self.instances.values()
                    if r["kind"] == a["kind"] and r["logical_key"] == a["logical_key"]
                    and r["custody"] == "current" and r["id"] not in (after, before)]
        if text.startswith("SELECT logical_key->>'base' FROM product_instances"):
            r = self.instances.get(params[0])
            return [] if r is None else [(r["logical_key"].get("base"),)]
        if "LEFT JOIN execution_records er" in text:
            return [("A", "sha256:x", None, True)]
        if text.startswith("INSERT INTO promotions"):
            self.promotions.append(params)
            return []
        if text.startswith("SELECT logical_key FROM product_instances WHERE id"):
            return [(self.instances[params[0]]["logical_key"],)]
        if text.startswith("UPDATE product_instances SET custody"):
            custody = "candidate" if "'candidate'" in text else "current"
            self.instances[params[0]]["custody"] = custody
            return []
        if text.startswith("INSERT INTO promotion_changes"):
            self.changes.append(params[2:])
            return []
        if text.startswith("SELECT id, outcome, detail->>'summary' FROM checks"):
            return [("CHK1", "passed", "ok")]
        if text.startswith("SELECT 1 FROM promotions"):
            return [(1,)]
        raise AssertionError(f"unexpected SQL: {text}")


class _Cursor:
    def __init__(self, db):
        self.db = db
        self.rows: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self.rows = list(self.db.execute(sql, params))

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, db):
        self.db = db

    def cursor(self):
        return _Cursor(self.db)


S1 = {"exposure": "e1", "detector": "SCA01", "differencer": "zogy", "catalog_type": "sex"}


def _promote(db, changes, **kw):
    return repository.promote(_Conn(db), "ops", "test", changes, **kw)


# ======================================================================
# rapidpipe.runs.slots
# ======================================================================

def test_canonical_json_sorts_keys_and_drops_spaces():
    assert slots.canonical_json({"b": 1, "a": "x"}) == '{"a":"x","b":1}'


@pytest.mark.parametrize("selector", [
    None, {}, {"slot": {}}, {"slot": [1]}, {"slot": {"a": 1}, "logical_key": {"a": 1}},
    {"key": {"a": 1}}, "slot",
])
def test_selector_parts_refuses_what_is_not_a_selector(selector):
    with pytest.raises(ValueError):
        slots.selector_parts(selector)


def test_selector_parts_returns_the_kind_and_value():
    assert slots.selector_parts({"slot": {"a": 1}}) == ("slot", {"a": 1})
    assert slots.selector_parts({"logical_key": {}}) == ("logical_key", {})


def test_plan_entries_sort_by_kind_then_canonical_slot_and_round_trip():
    entries = slots.plan_entries([
        ("source-set", {"exposure": "e2"}, None, "I3"),
        ("l2-image", {"exposure": "e1"}, "I0", "I1"),
        ("source-set", {"exposure": "e1"}, None, "I2"),
    ])
    assert [(e["kind"], e["after"]) for e in entries] == [
        ("l2-image", "I1"), ("source-set", "I2"), ("source-set", "I3")]
    assert json.loads(slots.plan_json(entries)) == entries
    assert slots.plan_by_slot(entries)[("l2-image", '{"exposure":"e1"}')] == ("I0", "I1")


@pytest.mark.parametrize("plan", [
    {"kind": "x"},
    [{"kind": "x", "slot": {"a": 1}, "before": None}],
    [{"kind": "x", "slot": {}, "before": None, "after": "I"}],
    [{"kind": "x", "slot": {"a": 1}, "before": 3, "after": "I"}],
    [{"kind": "x", "slot": {"a": 1}, "before": None, "after": None}],
    [{"kind": "x", "slot": {"a": 1}, "before": None, "after": "I"},
     {"kind": "x", "slot": {"a": 1}, "before": None, "after": "J"}],
])
def test_plan_by_slot_refuses_a_malformed_plan(plan):
    with pytest.raises(ValueError):
        slots.plan_by_slot(plan)


# ======================================================================
# promote: selectors and refusals (R4, R15)
# ======================================================================

def test_promote_by_slot_replaces_the_current_instance_and_records_the_slot():
    db = _Db([
        {"id": "OLD", "kind": "source-set", "slot": S1, "custody": "current",
         "logical_key": {"difference": "D1"}},
        {"id": "NEW", "kind": "source-set", "slot": S1, "logical_key": {"difference": "D2"}},
    ])
    _promote(db, [("source-set", {"slot": S1}, "OLD", "NEW")])
    assert db.instances["OLD"]["custody"] == "candidate"
    assert db.instances["NEW"]["custody"] == "current"
    kind, key, slot, before, after = db.changes[0]
    assert (kind, json.loads(key), json.loads(slot), before, after) == (
        "source-set", {"difference": "D2"}, S1, "OLD", "NEW")


def test_an_unselect_records_the_before_instances_logical_key():
    db = _Db([{"id": "OLD", "kind": "source-set", "slot": S1, "custody": "current",
               "logical_key": {"difference": "D1"}}])
    _promote(db, [("source-set", {"slot": S1}, "OLD", None)])
    assert json.loads(db.changes[0][1]) == {"difference": "D1"}
    assert db.changes[0][4] is None


@pytest.mark.parametrize("selector", [{"difference": "D1"}, {"slot": {}}, {"slot": S1, "x": {}}])
def test_promote_refuses_what_is_not_a_selector(selector):
    db = _Db([{"id": "NEW", "kind": "source-set", "slot": S1}])
    with pytest.raises(repository.PromotionRefused, match="refusing the whole promotion"):
        _promote(db, [("source-set", selector, None, "NEW")])
    assert db.changes == []


def test_promote_refuses_a_logical_key_selector_from_a_direct_caller():
    db = _Db([{"id": "NEW", "kind": "source-set", "slot": None, "logical_key": {"d": 1}}])
    with pytest.raises(repository.PromotionRefused, match="only when rolling back"):
        _promote(db, [("source-set", {"logical_key": {"d": 1}}, None, "NEW")])


def test_promote_accepts_a_logical_key_selector_as_the_recorded_inverse():
    db = _Db([
        {"id": "OLD", "kind": "source-set", "custody": "current", "logical_key": {"d": 1}},
        {"id": "NEW", "kind": "source-set", "logical_key": {"d": 1}},
    ])
    _promote(db, [("source-set", {"logical_key": {"d": 1}}, "OLD", "NEW")],
             _recorded_inverse=True)
    assert db.changes[0][2] is None     # no slot recorded
    assert json.loads(db.changes[0][1]) == {"d": 1}


def test_promote_refuses_the_same_slot_twice():
    db = _Db([{"id": "A", "kind": "source-set", "slot": S1},
              {"id": "B", "kind": "source-set", "slot": S1}])
    with pytest.raises(repository.PromotionRefused, match="more than once"):
        _promote(db, [("source-set", {"slot": S1}, None, "A"),
                      ("source-set", {"slot": dict(reversed(S1.items()))}, None, "B")])


def test_promote_refuses_a_stale_expected_before_in_the_slot():
    db = _Db([{"id": "OTHER", "kind": "source-set", "slot": S1, "custody": "current"},
              {"id": "NEW", "kind": "source-set", "slot": S1}])
    with pytest.raises(repository.PromotionRefused, match="'OTHER'; refusing"):
        _promote(db, [("source-set", {"slot": S1}, None, "NEW")])


def test_promote_refuses_an_after_instance_in_another_slot_or_with_none():
    db = _Db([{"id": "ELSE", "kind": "source-set", "slot": {**S1, "exposure": "e9"}},
              {"id": "NULL", "kind": "source-set", "slot": None}])
    with pytest.raises(repository.PromotionRefused, match='"exposure":"e9"'):
        _promote(db, [("source-set", {"slot": S1}, None, "ELSE")])
    with pytest.raises(repository.PromotionRefused, match=r"slot=none \(unresolved\)"):
        _promote(db, [("source-set", {"slot": S1}, None, "NULL")])


def test_promote_refuses_when_the_logical_key_is_current_outside_the_slot():
    db = _Db([
        {"id": "LEGACY", "kind": "l2-image", "slot": None, "custody": "current",
         "logical_key": {"exposure": "e1"}},
        {"id": "NEW", "kind": "l2-image", "slot": {"exposure": "e1"},
         "logical_key": {"exposure": "e1"}},
    ])
    with pytest.raises(repository.PromotionRefused, match="'LEGACY' is current with the same"):
        _promote(db, [("l2-image", {"slot": {"exposure": "e1"}}, None, "NEW")])


# ======================================================================
# the association-set ancestor rule (R6, R13)
# ======================================================================

def _chain_db(after_base):
    field = {"field": 100}
    return _Db([
        {"id": "X1", "kind": "association-set", "slot": field, "custody": "current",
         "logical_key": {"field": 100, "base": None}},
        {"id": "X2", "kind": "association-set", "slot": field,
         "logical_key": {"field": 100, "base": "X1"}},
        {"id": "X3", "kind": "association-set", "slot": field,
         "logical_key": {"field": 100, "base": after_base}},
        {"id": "Y1", "kind": "association-set", "slot": field,
         "logical_key": {"field": 100, "base": "Y1"}},
    ])


@pytest.mark.parametrize("after_base", ["X1", "X2"])
def test_association_replacement_passes_when_before_is_an_ancestor(after_base):
    db = _chain_db(after_base)
    _promote(db, [("association-set", {"slot": {"field": 100}}, "X1", "X3")])
    assert db.instances["X3"]["custody"] == "current"


@pytest.mark.parametrize("after, after_base", [("X3", None), ("X3", "NOWHERE"), ("Y1", "X1")])
def test_association_replacement_refused_unless_before_is_an_ancestor(after, after_base):
    db = _chain_db(after_base)
    with pytest.raises(repository.PromotionRefused,
                       match="chain switch is not implemented; ordinary slot replacement "
                             "does not stand in for it"):
        _promote(db, [("association-set", {"slot": {"field": 100}}, "X1", after)])
    assert db.instances["X1"]["custody"] == "current"


def test_association_initial_selection_and_recorded_inverse_skip_the_ancestor_rule():
    db = _chain_db(None)
    db.instances["X1"]["custody"] = "candidate"
    _promote(db, [("association-set", {"slot": {"field": 100}}, None, "X3")])
    # the recorded inverse of a replacement X1 -> X2: X2 is not X1's ancestor
    db = _chain_db(None)
    db.instances["X1"]["custody"] = "candidate"
    db.instances["X2"]["custody"] = "current"
    _promote(db, [("association-set", {"slot": {"field": 100}}, "X2", "X1")],
             _recorded_inverse=True)
    assert db.instances["X1"]["custody"] == "current"


# ======================================================================
# promote_run: grouping by slot, frozen plans (R4, R5)
# ======================================================================

def _run_db():
    return _Db([
        {"id": "CUR", "kind": "source-set", "slot": S1, "custody": "current",
         "logical_key": {"difference": "D0"}},
        {"id": "C1", "kind": "source-set", "slot": S1, "run": "R",
         "logical_key": {"difference": "D1"}},
        {"id": "C2", "kind": "source-set", "slot": {**S1, "exposure": "e2"}, "run": "R",
         "logical_key": {"difference": "D2"}},
    ])


def test_promotion_plan_lists_each_slot_with_its_before_and_after():
    plan = repository.promotion_plan(_Conn(_run_db()), "R")
    assert plan == [
        {"kind": "source-set", "slot": S1, "before": "CUR", "after": "C1"},
        {"kind": "source-set", "slot": {**S1, "exposure": "e2"}, "before": None, "after": "C2"},
    ]


def test_promote_run_applies_a_matching_plan():
    db = _run_db()
    plan = repository.promotion_plan(_Conn(db), "R")
    repository.promote_run(_Conn(db), "R", "ops", "r", plan=plan, allow_unreleased=True)
    assert {c[3]: c[4] for c in db.changes} == {"CUR": "C1", None: "C2"}
    assert any("FROM product_identity_fill()" in s for s in db.executed)


def test_promote_run_refuses_a_stale_plan_naming_the_slot_and_writes_nothing():
    db = _run_db()
    plan = repository.promotion_plan(_Conn(db), "R")
    db.instances["CUR"]["custody"] = "candidate"      # an intervening promotion moved it
    with pytest.raises(repository.StalePlan, match=r"kind='source-set' slot=\{.*\"e1\".*"
                                                   r"before='CUR'.*now has before=None"):
        repository.promote_run(_Conn(db), "R", "ops", "r", plan=plan)
    assert db.changes == [] and db.promotions == []


def test_promote_run_refuses_a_plan_missing_a_candidate():
    db = _run_db()
    plan = repository.promotion_plan(_Conn(db), "R")[:1]
    with pytest.raises(repository.StalePlan, match='"e2"'):
        repository.promote_run(_Conn(db), "R", "ops", "r", plan=plan)


def test_promote_run_refuses_a_malformed_plan():
    with pytest.raises(repository.PromotionRefused, match="plan is malformed"):
        repository.promote_run(_Conn(_run_db()), "R", "ops", "r", plan=[{"kind": "x"}])


def test_promote_run_refuses_a_candidate_without_a_slot():
    db = _run_db()
    db.instances["C2"]["slot"] = None
    with pytest.raises(repository.PromotionRefused, match="'C2' of kind='source-set' has no slot"):
        repository.promote_run(_Conn(db), "R", "ops", "r")


def test_promote_run_refuses_two_candidates_in_one_slot():
    db = _run_db()
    db.instances["C2"]["slot"] = S1
    with pytest.raises(repository.PromotionRefused, match="exactly one instance per slot"):
        repository.promote_run(_Conn(db), "R", "ops", "r")


def test_promotion_plan_refuses_a_run_with_nothing_to_promote():
    db = _run_db()
    for row in db.instances.values():
        row["run"] = "OTHER"
    with pytest.raises(repository.PromotionRefused, match="nothing to promote"):
        repository.promotion_plan(_Conn(db), "R")


# ======================================================================
# rollback: the recorded selector (R9, R13)
# ======================================================================

def test_rollback_selects_by_recorded_slot_or_logical_key(monkeypatch):
    class _RollbackCursor(_Cursor):
        def execute(self, sql, params=()):
            if "FROM promotion_changes" in sql:
                self.rows = [("source-set", {"d": 1}, S1, "B1", "A1"),
                             ("l2-image", {"e": 1}, None, None, "A2")]
            else:
                self.rows = [(1,)]

    class _RollbackConn:
        def cursor(self):
            return _RollbackCursor(None)

    seen = {}

    def _promote_stub(conn, who, reason, changes, **kw):
        seen.update(changes=changes, **kw)
        return "UNDO"

    monkeypatch.setattr(repository, "promote", _promote_stub)
    assert repository.rollback_promotion(_RollbackConn(), "P1", "ops", "r") == "UNDO"
    assert seen["changes"] == [("source-set", {"slot": S1}, "A1", "B1"),
                               ("l2-image", {"logical_key": {"e": 1}}, "A2", None)]
    assert seen["_recorded_inverse"] is True and seen["_check_release"] is False


# ======================================================================
# the fill wrapper and its callers (R2, R16)
# ======================================================================

def test_fill_identity_returns_the_report():
    db = _Db([])
    with _Conn(db).cursor() as cur:
        assert repository.fill_identity(cur) == [("source-set", 1, 0, 0)]


def test_fill_identity_safely_rolls_back_to_its_savepoint_and_goes_on(caplog):
    db = _Db([])
    db.fill_error = psycopg2.Error("unique violation")
    with _Conn(db).cursor() as cur:
        assert repository.fill_identity_safely(cur, "testing") == []
    assert "ROLLBACK TO SAVEPOINT rapidpipe_fill_identity" in db.executed
    assert db.executed[-1] == "RELEASE SAVEPOINT rapidpipe_fill_identity"
    assert "fill failed while testing" in caplog.text


def test_fill_identity_safely_takes_no_savepoint_on_an_autocommit_connection(caplog):
    db = _Db([])
    db.fill_error = psycopg2.Error("boom")
    cur = _Conn(db).cursor()
    cur.connection = type("_AutocommitConn", (), {"autocommit": True})()
    assert repository.fill_identity_safely(cur, "testing") == []
    assert not any("SAVEPOINT" in s for s in db.executed)
    assert "fill failed while testing" in caplog.text


def test_run_policy_checks_fills_before_loading_candidates(monkeypatch):
    order = []
    monkeypatch.setattr(repository, "fill_identity_safely",
                        lambda cur, context: order.append(("fill", context)))
    monkeypatch.setattr(runner, "run_candidates",
                        lambda conn, run_id: order.append(("candidates", run_id)) or [])
    policy = runner.load_policy("rebuild-trial@1")
    assert runner.run_policy_checks(_Conn(_Db([])), "R", policy) == []
    assert order == [("fill", "checking run R"), ("candidates", "R")]


# ======================================================================
# the reference check reads the slot (R8)
# ======================================================================

def test_the_reference_identity_comes_from_the_slot():
    assert builtin._identity({"exposure": 12, "detector": "SCA01", "differencer": "z",
                              "catalog_type": "sex"}) == ("12", "SCA01", "sex")
    assert builtin._identity(None) is None
    assert builtin._identity({"exposure": "e", "detector": "d"}) is None
    assert builtin._identity({"exposure": {}, "detector": "d", "catalog_type": "c"}) is None


def test_the_reference_query_selects_on_slot_fields_only():
    assert "slot->>'exposure'" in builtin._SAME_IDENTITY
    assert "slot->>'detector'" in builtin._SAME_IDENTITY
    assert "slot->>'catalog_type'" in builtin._SAME_IDENTITY
    assert "l2files" not in builtin._SAME_IDENTITY


def test_a_candidate_without_a_slot_fails_as_an_unresolved_identity():
    class _CheckCursor(_Cursor):
        def execute(self, sql, params=()):
            self.rows = [("source-set", None, "R", True, True, 10)]

    class _CheckConn:
        def cursor(self):
            return _CheckCursor(None)

    result = builtin.catalog_counts_vs_reference(
        _CheckConn(), "I", {"tolerance": 0.1, "missing_reference": "pass", "reference_run": ""})
    assert result.outcome == "failed"
    assert result.detail["failing"] == ["identity"]


# ======================================================================
# the CLI: run promote-plan, run promote --plan (R5)
# ======================================================================

class _CliConn:
    def __init__(self):
        self.committed = 0
        self.rolled_back = 0

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1


@pytest.fixture()
def cli_conn(monkeypatch):
    conn = _CliConn()

    @contextlib.contextmanager
    def _connect(**_kwargs):
        yield conn

    monkeypatch.setattr(cli, "connect", _connect)
    return conn


def test_promote_plan_prints_the_plan_as_json_and_rolls_back(monkeypatch, cli_conn, capsys):
    plan = [{"kind": "source-set", "slot": S1, "before": None, "after": "C1"}]
    seen = {}

    def _plan(conn, run_id, *, kinds=None):
        seen.update(run_id=run_id, kinds=kinds)
        return plan

    monkeypatch.setattr(repository, "promotion_plan", _plan)
    assert cli.main(["run", "promote-plan", "R1", "--kinds", "source-set"]) == 0
    assert json.loads(capsys.readouterr().out) == plan
    assert seen == {"run_id": "R1", "kinds": ["source-set"]}
    assert cli_conn.rolled_back == 1


def test_promote_plan_exits_64_when_there_is_nothing_to_promote(monkeypatch, cli_conn, capsys):
    def _refuse(conn, run_id, *, kinds=None):
        raise repository.PromotionRefused(f"run {run_id!r} has nothing to promote")

    monkeypatch.setattr(repository, "promotion_plan", _refuse)
    assert cli.main(["run", "promote-plan", "R1"]) == 64
    captured = capsys.readouterr()
    assert captured.out == "" and "nothing to promote" in captured.err


def test_promote_with_a_plan_passes_it_and_a_stale_plan_exits_64(
        monkeypatch, cli_conn, capsys, tmp_path):
    plan = [{"kind": "source-set", "slot": S1, "before": None, "after": "C1"}]
    path = tmp_path / "plan.json"
    path.write_text(slots.plan_json(plan))
    seen = {}

    def _promote_run(conn, run_id, who, reason, **kw):
        seen.update(kw)
        raise repository.StalePlan("stale plan for run 'R1': kind='source-set'")

    monkeypatch.setattr(repository, "promote_run", _promote_run)
    assert cli.main(["run", "promote", "R1", "--reason", "r", "--plan", str(path)]) == 64
    assert seen["plan"] == plan
    assert "stale plan" in capsys.readouterr().err
    assert cli_conn.committed == 0


def test_promote_with_an_unreadable_plan_exits_64(cli_conn, capsys, tmp_path):
    path = tmp_path / "plan.json"
    path.write_text("not json")
    assert cli.main(["run", "promote", "R1", "--reason", "r", "--plan", str(path)]) == 64
    assert "cannot read plan" in capsys.readouterr().err
    assert cli.main(["run", "promote", "R1", "--reason", "r",
                     "--plan", str(tmp_path / "missing.json")]) == 64
