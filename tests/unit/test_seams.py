"""Tests for rapidpipe.seams: the ``module:factory`` loader and variable names."""

from __future__ import annotations

import sys
import types

import pytest

from rapidpipe.seams import database_env, load_factory, toolkit_env

ENV = "RAPIDPIPE_TEST_SEAM"


class _Refused(Exception):
    pass


def test_variable_names():
    assert database_env("load") == "RAPIDPIPE_LOAD_DATABASE"
    assert toolkit_env("difference") == "RAPIDPIPE_DIFFERENCE_TOOLKIT"


@pytest.mark.parametrize("value", [None, ""])
def test_unset_or_empty_is_none(monkeypatch, value):
    if value is None:
        monkeypatch.delenv(ENV, raising=False)
    else:
        monkeypatch.setenv(ENV, value)
    assert load_factory(ENV, _Refused) is None


def test_resolves_the_named_callable(monkeypatch):
    monkeypatch.setenv(ENV, "os.path:join")
    import os.path
    assert load_factory(ENV, _Refused) is os.path.join


def test_a_falsy_attribute_is_returned_not_treated_as_unset(monkeypatch):
    module = types.ModuleType("_seam_falsy")
    module.factory = 0
    monkeypatch.setitem(sys.modules, "_seam_falsy", module)
    monkeypatch.setenv(ENV, "_seam_falsy:factory")
    assert load_factory(ENV, _Refused) == 0


@pytest.mark.parametrize("value", [":factory", "no_such_module_xyz:f", "os.path:no_such_attr"])
def test_an_unresolvable_value_raises_the_callers_error(monkeypatch, value):
    monkeypatch.setenv(ENV, value)
    with pytest.raises(_Refused, match="does not name a factory"):
        load_factory(ENV, _Refused)


@pytest.mark.parametrize("failure", [
    ValueError("broken at import"),
    ModuleNotFoundError("No module named 'missing_dependency'", name="missing_dependency"),
    ImportError("cannot import name 'x' from 'somewhere'"),
    AttributeError("module 'somewhere' has no attribute 'x'"),
])
def test_a_failure_inside_the_module_propagates(monkeypatch, failure):
    class _Finder:
        def find_spec(self, name, path=None, target=None):
            if name != "_seam_broken":
                return None
            import importlib.util

            class _Loader:
                def create_module(self, spec):
                    return None

                def exec_module(self, module):
                    raise failure

            return importlib.util.spec_from_loader(name, _Loader())

    monkeypatch.setattr(sys, "meta_path", [_Finder(), *sys.meta_path])
    monkeypatch.setenv(ENV, "_seam_broken:factory")
    with pytest.raises(type(failure)) as exc:
        load_factory(ENV, _Refused)
    assert exc.value is failure


class _FalsyFactory:
    """A factory whose truth value is False, so only an ``is not None``
    test tells it from an unset variable."""

    def __init__(self):
        self.calls = 0

    def __bool__(self):
        return False

    def __call__(self):
        self.calls += 1
        return "from-factory"


@pytest.mark.parametrize("stage, loader, env_of", [
    (stage, "open_database", database_env)
    for stage in ("alerts", "crossmatch", "export", "load", "maintain", "prune", "statistics")
] + [
    ("difference", "toolkit", toolkit_env),
    ("reference", "toolkit", toolkit_env),
])
def test_every_stage_seam_calls_a_falsy_factory(monkeypatch, stage, loader, env_of):
    import importlib

    factory = _FalsyFactory()
    module = types.ModuleType("_seam_falsy_callable")
    module.factory = factory
    monkeypatch.setitem(sys.modules, "_seam_falsy_callable", module)
    monkeypatch.setenv(env_of(stage), "_seam_falsy_callable:factory")
    stage_module = importlib.import_module(f"rapidpipe.stages.{stage}")
    assert getattr(stage_module, loader)() == "from-factory"
    assert factory.calls == 1
