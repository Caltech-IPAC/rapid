"""Stub-tier tests for `pipeline.operatorctl.gc.compute_plan`'s allowlist
handling — the `--allow-class` operator surface (sims-followup round,
2026-08-15).

**THE PROPERTY UNDER TEST.** `references.validated_allowlist()` refuses any
`real-*` data-class token mechanically, per the closeout memo
(`rapid_plan/research/deletable-class-allowlist-memo.md`). `compute_plan`
is the OTHER path that reads `DELETABLE_CLASS_ALLOWLIST` (or an
operator-supplied override) besides `references.classify()` itself —
`rapidctl gc ... --allow-class real-pristine` resolves to this function's
`allowlist` keyword. This file pins that an operator handing that token to
the CLI is refused too, not just a caller of `classify()` directly.

`compute_plan` validates its allowlist as the FIRST thing it does, before
any inventory read or SQL — `conn` and `execute` are never touched on the
refusal path, which is exactly what lets this run stub-tier: `object()` in
place of a real connection is enough, because a real one would never be
reached. Passing something that would explode on first use (rather than a
lenient fake) is what makes this test able to tell "refused before any I/O"
from "coincidentally never got that far" (`stub-blind-testing`'s house
rule: a double that could not fail proves nothing — here the fakes are
deliberately unusable rather than merely absent).
"""

import unittest

from pipeline.gc.inventory import InventoryStale
from pipeline.gc.references import ScienceClassNotDeletable
from pipeline.operatorctl.gc import compute_plan


class _ExplodesOnUse(object):
    """Stands in for `conn`/`execute`: any attribute access or call fails.

    If `compute_plan` ever reached the database on the refusal path, this
    fake would make the test fail loudly at that point rather than silently
    passing because a lenient stub happened to return something usable.
    """

    def __getattr__(self, name):
        raise AssertionError(
            "compute_plan touched %r before validating its allowlist — "
            "the real-* refusal must fire before any inventory or "
            "reference-set I/O" % name)

    def __call__(self, *args, **kwargs):
        raise AssertionError(
            "compute_plan invoked its execute() before validating its "
            "allowlist")


class ComputePlanAllowlistRefusalTests(unittest.TestCase):
    def test_allow_class_real_pristine_is_refused_before_any_io(self):
        with self.assertRaises(ScienceClassNotDeletable):
            compute_plan(
                conn=_ExplodesOnUse(), execute=_ExplodesOnUse(),
                inventory_source=_ExplodesOnUse(), inventory_id="inv-1",
                inventory_taken_at="2026-08-15T00:00:00Z",
                declared_buckets=("roman-rapid-products",),
                declared_prefixes=("science/",), horizons={},
                max_deletions=1, freshness_seconds=3600,
                reason="operator test", idempotency_key="key-1",
                actor="operator", allowlist=("real-pristine",))

    def test_allow_class_accepts_a_sim_token(self):
        """The converse: a `sim-*` `--allow-class` value is NOT refused by
        the validator itself. `compute_plan` still needs real inventory and
        reference-set I/O beyond this point (which the contract tier covers,
        e.g. `pipeline/contract/test_gc_operator_surface.py`), so this only
        asserts the allowlist gate specifically lets execution PAST itself:
        the next thing `compute_plan` does is call `read_inventory`, which
        rejects this test's arbitrary `inventory_taken_at` as stale before
        ever touching `inventory_source` — proving the failure came from
        past the allowlist gate, not from `ScienceClassNotDeletable`.
        """
        with self.assertRaises(InventoryStale):
            compute_plan(
                conn=_ExplodesOnUse(), execute=_ExplodesOnUse(),
                inventory_source=_ExplodesOnUse(), inventory_id="inv-1",
                inventory_taken_at="2026-08-15T00:00:00Z",
                declared_buckets=("roman-rapid-products",),
                declared_prefixes=("science/",), horizons={},
                max_deletions=1, freshness_seconds=3600,
                reason="operator test", idempotency_key="key-1",
                actor="operator", allowlist=("sim-injected",))

    def test_the_default_allowlist_still_validates_fine(self):
        """No `--allow-class` at all — `allowlist=None` falls back to
        `references.DELETABLE_CLASS_ALLOWLIST`, which is `()`, and `()`
        still passes `validated_allowlist()` unchanged (proven directly in
        `pipeline/gc/test/test_plans.py`). Confirms the fallback wiring
        itself does not regress that: execution proceeds past the allowlist
        gate to the same stale-inventory refusal as the `sim-*` case above,
        rather than tripping `ScienceClassNotDeletable` on an empty tuple.
        """
        with self.assertRaises(InventoryStale):
            compute_plan(
                conn=_ExplodesOnUse(), execute=_ExplodesOnUse(),
                inventory_source=_ExplodesOnUse(), inventory_id="inv-1",
                inventory_taken_at="2026-08-15T00:00:00Z",
                declared_buckets=("roman-rapid-products",),
                declared_prefixes=("science/",), horizons={},
                max_deletions=1, freshness_seconds=3600,
                reason="operator test", idempotency_key="key-1",
                actor="operator", allowlist=None)


if __name__ == "__main__":
    unittest.main()
