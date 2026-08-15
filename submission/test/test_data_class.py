"""The inheritance rule, and the direction it must never invert.

`submission.data_class` decides what class a derived product takes when its
inputs disagree. Getting it backwards does not fail loudly — it files
validation data under a science prefix, where the promotion and publication
roles can reach it. That is the leak the design calls non-waivable, so the
direction is asserted here in both the agreeing and the disagreeing case.
"""

import unittest

from submission import data_class


class RegistryTests(unittest.TestCase):

    def test_the_registry_is_exactly_the_four_ratified_tokens(self):
        # Closed set, per naming.md's token registry: a fifth value amends
        # the two-axis identity model and needs its own ratification, so it
        # must not be possible to acquire one by accident here.
        self.assertEqual(
            ("real-pristine", "real-injected",
             "sim-pristine", "sim-injected"),
            data_class.DATA_CLASSES)

    def test_the_registry_matches_the_builders_own_closed_set(self):
        """Two modules naming the same closed set is two places to drift.

        `pipeline.stages.context.DATA_CLASSES` is what `product_prefix()`
        validates against; this module is what gathering resolves with. A
        token accepted by one and refused by the other would either refuse a
        legitimate unit at build time or admit one this module can never
        produce — so their agreement is asserted rather than assumed.
        """
        from pipeline.stages.context import DATA_CLASSES as BUILDER_CLASSES

        self.assertEqual(set(BUILDER_CLASSES), set(data_class.DATA_CLASSES))

    def test_an_unregistered_token_is_refused_not_split(self):
        # 'simulated-injected' uses the MODEL's vocabulary rather than the
        # KEY's ('sim'), which is exactly the plausible near-miss: storage.md
        # gives the mapping real|simulated -> real|sim, so a caller reading
        # the model and not the mapping writes this.
        with self.assertRaises(data_class.DataClassError):
            data_class.parse("simulated-injected")

    def test_the_refusal_names_the_registry(self):
        # An operator seeing this must not have to find the module to learn
        # what was allowed.
        with self.assertRaises(data_class.DataClassError) as caught:
            data_class.parse("sim")
        message = str(caught.exception)
        for token in data_class.DATA_CLASSES:
            self.assertIn(token, message)


class MostRestrictiveTests(unittest.TestCase):

    def test_one_input_keeps_its_own_class(self):
        for token in data_class.DATA_CLASSES:
            with self.subTest(token=token):
                self.assertEqual(token, data_class.most_restrictive([token]))

    def test_agreeing_inputs_keep_the_agreed_class(self):
        self.assertEqual(
            "sim-injected",
            data_class.most_restrictive(["sim-injected", "sim-injected"]))

    def test_science_mixed_with_anything_stops_being_science(self):
        """THE DIRECTION THAT MATTERS, and the one that inverts plausibly.

        Reading "most restrictive" as "hardest to leak FROM" would make
        `real-pristine` dominate and return science here — which would file a
        product built partly from simulated, injected pixels under the
        science prefix and make it promotable. The rule is the opposite: a
        derivation is science only if EVERY input is.
        """
        for other in ("real-injected", "sim-pristine", "sim-injected"):
            with self.subTest(other=other):
                result = data_class.most_restrictive(["real-pristine", other])

                self.assertNotEqual("real-pristine", result)
                self.assertFalse(data_class.is_science(result))

    def test_each_axis_resolves_independently(self):
        """The per-axis rule, shown where an opaque ranking would fail.

        `sim-pristine` and `real-injected` are each non-science on ONE axis,
        and on different ones. Ordering the four tokens as opaque strings —
        by registry position, alphabetically, however — must pick one of the
        two inputs; the correct answer is neither, because the result is
        non-science on BOTH axes. This is the case that proves the
        combination splits the compound token rather than ranking it.
        """
        self.assertEqual(
            "sim-injected",
            data_class.most_restrictive(["sim-pristine", "real-injected"]))

    def test_all_four_together_give_the_least_eligible(self):
        self.assertEqual(
            "sim-injected",
            data_class.most_restrictive(list(data_class.DATA_CLASSES)))

    def test_no_inputs_yields_no_class_rather_than_a_default(self):
        # A unit nothing knows the class of must NOT acquire a plausible
        # one here: the builder's documented fallback serves it, and a guess
        # made in this function would be indistinguishable from knowledge.
        self.assertIsNone(data_class.most_restrictive([]))
        self.assertIsNone(data_class.most_restrictive([None, None]))

    def test_a_known_class_beside_an_unknown_one_still_counts(self):
        # A NULL contributes no evidence; it must not veto the classes that
        # ARE known, or one legacy manifest in an input set would silently
        # declassify the whole unit.
        self.assertEqual(
            "sim-injected",
            data_class.most_restrictive([None, "sim-injected"]))

    def test_an_unregistered_token_in_the_mix_is_refused(self):
        # 090's CHECK makes this unreachable from the database, so reaching
        # it means the constraint was dropped or bypassed — which must fail
        # loudly rather than build an object key from an unknown token.
        with self.assertRaises(data_class.DataClassError):
            data_class.most_restrictive(["real-pristine", "nonsense"])


class ScienceGateTests(unittest.TestCase):

    def test_exactly_one_cell_is_science(self):
        # "Science is exactly one cell: real ^ pristine; the other three are
        # validation data" (operations.md).
        self.assertTrue(data_class.is_science("real-pristine"))
        for token in ("real-injected", "sim-pristine", "sim-injected"):
            with self.subTest(token=token):
                self.assertFalse(data_class.is_science(token))


if __name__ == "__main__":
    unittest.main()
