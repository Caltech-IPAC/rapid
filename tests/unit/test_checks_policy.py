"""Check registry and policy loading, no database (supervisor step 6,
2026-09-24, R1, R3, R5, amendment A1)."""

from __future__ import annotations

import tomllib
from dataclasses import replace
from importlib import resources

import pytest

from rapidpipe.checks import policy as policy_mod
from rapidpipe.checks import registry
from rapidpipe.checks.policy import (
    DEFAULT_POLICY,
    PolicyError,
    load_policy,
    parse_policy,
    policy_permits_auto_promote,
    policy_permits_promotion,
    shipped_policies,
)


def test_two_checks_are_registered_with_their_kinds():
    refs = {c.ref: c.kind for c in registry.registered_checks()}
    assert refs == {"difference-image-statistics@1": "difference-image",
                    "catalog-counts-vs-reference@1": "source-set"}


def test_registering_a_reference_twice_is_refused(monkeypatch):
    monkeypatch.setattr(registry, "_REGISTRY", {})

    @registry.check("x", "1", kind="k")
    def first(conn, instance_id, params):
        """First line."""

    assert registry._REGISTRY["x@1"].description == "First line."
    with pytest.raises(registry.CheckError, match="already registered"):
        @registry.check("x", "1", kind="k")
        def second(conn, instance_id, params):
            pass


def test_unknown_check_and_bad_refs():
    with pytest.raises(registry.UnknownCheck):
        registry.get_check("nosuch@1")
    for bad in ("nosuch", "@1", "x@"):
        with pytest.raises(ValueError):
            registry.parse_ref(bad)


def test_check_result_refuses_an_unknown_outcome():
    with pytest.raises(ValueError):
        registry.CheckResult("skipped")


def test_shipped_policies_load_and_ship_as_package_data():
    assert shipped_policies() == ["rebuild-strict@1", "rebuild-trial@1"]
    files = {p.name for p in resources.files("rapidpipe.checks").joinpath("policies").iterdir()}
    assert {"rebuild-trial@1.toml", "rebuild-strict@1.toml"} <= files
    assert DEFAULT_POLICY == "rebuild-trial@1"


def test_rebuild_trial_bounds_and_flags():
    trial = load_policy("rebuild-trial@1")
    assert (trial.approval, trial.auto_promote) == ("trial", False)
    diff = trial.find_check("difference-image-statistics@1")
    assert diff.required and diff.kind == "difference-image"
    assert diff.params == {"scalefacref_lo": 0.2, "scalefacref_hi": 5.0, "rms_max": 1.0,
                           "median_max": 0.5, "n_min": 50000, "n_max": 500000,
                           "ratio_lo": 0.5, "ratio_hi": 2.0}
    catalog = trial.find_check("catalog-counts-vs-reference@1")
    assert not catalog.required
    assert catalog.params == {"tolerance": 0.10, "missing_reference": "pass",
                              "reference_run": ""}
    assert trial.checks_for_kind("psf") == []


def test_rebuild_strict_is_tighter():
    strict = load_policy("rebuild-strict@1")
    diff = strict.find_check("difference-image-statistics@1").params
    assert (diff["n_max"], diff["scalefacref_lo"], diff["scalefacref_hi"], diff["rms_max"]) == (
        1000, 0.99, 1.01, 0.01)
    catalog = strict.find_check("catalog-counts-vs-reference@1").params
    assert (catalog["tolerance"], catalog["missing_reference"]) == (0.0001, "fail")


def test_no_shipped_policy_permits_automatic_promotion():
    for ref in shipped_policies():
        policy = load_policy(ref)
        assert policy_permits_promotion(policy)
        assert not policy_permits_auto_promote(policy)


def test_permission_rules():
    trial = load_policy("rebuild-trial@1")
    assert not policy_permits_promotion(replace(trial, approval="none", approved_by=None))
    assert not policy_permits_auto_promote(replace(trial, auto_promote=True))   # trial only
    assert not policy_permits_auto_promote(replace(trial, approval="lead"))     # flag off
    assert policy_permits_auto_promote(replace(trial, approval="lead", auto_promote=True))
    assert not policy_permits_auto_promote(
        replace(trial, approval="lead", approved_by=None, auto_promote=True))


def _trial_doc():
    with resources.files("rapidpipe.checks").joinpath(
            "policies", "rebuild-trial@1.toml").open("rb") as handle:
        return tomllib.load(handle)


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d.update(extra=1), "unknown fields"),
    (lambda d: d.update(version="2"), "but its file is named"),
    (lambda d: d.update(approval="maybe"), "approval must be one of"),
    (lambda d: d.update(approved_by=""), "needs approved_by"),
    (lambda d: d.update(auto_promote="yes"), "auto_promote must be"),
    (lambda d: d["checks"][0].update(version="9"), "no check is registered"),
    (lambda d: d["checks"][0].update(kind="psf"), "applies to kind"),
    (lambda d: d["checks"][0]["params"].pop("rms_max"), "params"),
    (lambda d: d["checks"].append(dict(d["checks"][0])), "appears twice"),
    (lambda d: d["checks"][0].pop("kind"), "missing 'kind'"),
])
def test_malformed_policies_are_refused(mutate, message):
    doc = _trial_doc()
    mutate(doc)
    with pytest.raises(PolicyError, match=message):
        parse_policy(doc, source="t", expected_ref="rebuild-trial@1")


def test_unknown_policy_and_the_fixture_seam(monkeypatch):
    with pytest.raises(PolicyError, match="does not exist"):
        load_policy("nosuch@1")
    with pytest.raises(PolicyError, match="NAME@VERSION"):
        load_policy("nosuch")
    fixture = replace(load_policy("rebuild-trial@1"), name="fixture")
    monkeypatch.setitem(policy_mod._FIXTURE_POLICIES, "fixture@1", fixture)
    assert load_policy("fixture@1") is fixture


def test_load_policy_file(tmp_path):
    source = resources.files("rapidpipe.checks").joinpath("policies", "rebuild-strict@1.toml")
    path = tmp_path / "p.toml"
    path.write_bytes(source.read_bytes())
    assert policy_mod.load_policy_file(path).ref == "rebuild-strict@1"
