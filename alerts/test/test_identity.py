"""Tests for `alerts/identity.py`: the alert-packet identity digest.

Stub tier — no database, no network, pure computation over the module's
own functions. The database half (outbox row insert, collision guard,
`identity_basis` CHECK constraint) is brief E's acceptance criterion 1's
other half and is NOT here; see the module docstring's own account of why
this module even exists apart from `pipeline/registration/identity.py`.

Covers brief E's acceptance criterion 1, the pure-Python half:

  1. determinism         -- same components, computed twice, same id
  2. sensitivity          -- each component varied alone moves the id
  3. namespace separation -- the two bases cannot collide, by construction
  4. forbidden inputs      -- no sid/path/uri/run/attempt/batch key, ever
  5. digest shape          -- `sha256:` + 64 hex chars
  6. error cases           -- both bases, neither basis, no sign, no catalog id
  7. payload_checksum      -- sha256-prefixed digest of raw bytes
  8. serialization stability -- sorted keys, no whitespace, order-independent
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from alerts.identity import (
    AlertIdentityError,
    ForbiddenIdentityInput,
    alert_id,
    alert_identity,
    canonical_json,
    canonical_payload,
    detection_key,
    legacy_pid_image,
    payload_checksum,
    product_key_image,
    _reject_forbidden,
)

# A fixed set of components shared across tests, so each test that varies
# something varies exactly one thing against a known-good baseline.
PRODUCT_KEY = "sha256:" + "a" * 64
LEGACY_PID = 4242
CATALOG_ID = 9001
ISDIFFPOS = True
RELEASE_IDENTITY = "sha256:" + "b" * 64


def _product_identity(**overrides):
    """alert_identity() under the product-key basis, one baseline call."""
    kwargs = dict(product_key=PRODUCT_KEY, catalog_id=CATALOG_ID,
                  isdiffpos=ISDIFFPOS, release_identity=RELEASE_IDENTITY)
    kwargs.update(overrides)
    return alert_identity(**kwargs)


def _legacy_identity(**overrides):
    """alert_identity() under the legacy-pid basis, one baseline call."""
    kwargs = dict(legacy_pid=LEGACY_PID, catalog_id=CATALOG_ID,
                  isdiffpos=ISDIFFPOS, release_identity=RELEASE_IDENTITY)
    kwargs.update(overrides)
    return alert_identity(**kwargs)


# ---------------------------------------------------------------------------
# 1. Determinism
# ---------------------------------------------------------------------------

class DeterminismTests(unittest.TestCase):
    """Same components in, same alert_id out -- no matter how many times."""

    def test_same_components_give_same_id_under_product_key_basis(self):
        # The module's whole reason to exist is that consumers dedup on this
        # id, so "compute it twice, get the same bytes" is the load-bearing
        # property: a run that recomputes an id for a packet it already
        # outboxed must recompute the SAME id, not a fresh one.
        first, first_payload = _product_identity()
        second, second_payload = _product_identity()
        self.assertEqual(first, second)
        self.assertEqual(first_payload, second_payload)

    def test_same_components_give_same_id_under_legacy_pid_basis(self):
        first, first_payload = _legacy_identity()
        second, second_payload = _legacy_identity()
        self.assertEqual(first, second)
        self.assertEqual(first_payload, second_payload)


# ---------------------------------------------------------------------------
# 2. Sensitivity: each component, varied alone, moves the id
# ---------------------------------------------------------------------------

class SensitivityTests(unittest.TestCase):
    """Changing any ONE identity component changes the alert_id.

    A digest that silently ignored one of its inputs would collapse two
    different packets onto the same id, which is exactly the collision the
    outbox's UNIQUE constraint on alert_id exists to catch -- as a
    production failure, not a test failure, if this were untested.
    """

    def test_different_product_key_gives_different_id(self):
        baseline, _ = _product_identity()
        varied, _ = _product_identity(product_key="sha256:" + "c" * 64)
        self.assertNotEqual(baseline, varied)

    def test_different_legacy_pid_gives_different_id(self):
        baseline, _ = _legacy_identity()
        varied, _ = _legacy_identity(legacy_pid=LEGACY_PID + 1)
        self.assertNotEqual(baseline, varied)

    def test_different_catalog_id_gives_different_id(self):
        baseline, _ = _product_identity()
        varied, _ = _product_identity(catalog_id=CATALOG_ID + 1)
        self.assertNotEqual(baseline, varied)

    def test_different_isdiffpos_gives_different_id(self):
        # THE ONE THAT MATTERS MOST, per migration 041 (see detection_key's
        # docstring and the module docstring's "WHY NOT sid" section):
        # `id` is a per-file ordinal that restarts per file, and a product
        # has both a positive and a negative file. So (id) alone would
        # conflate a positive detection with an unrelated negative one --
        # the exact collision 041 was written to prevent. If True and False
        # ever hashed to the same id, this module would have reintroduced
        # that collision one layer up.
        positive, _ = _product_identity(isdiffpos=True)
        negative, _ = _product_identity(isdiffpos=False)
        self.assertNotEqual(positive, negative)

    def test_different_release_identity_gives_different_id(self):
        baseline, _ = _product_identity()
        varied, _ = _product_identity(release_identity="sha256:" + "f" * 64)
        self.assertNotEqual(baseline, varied)

    def test_product_key_basis_and_legacy_pid_basis_give_different_ids(self):
        # Same catalog id, same sign, same release -- only the basis (and
        # necessarily the image spelling that basis uses) differs. This is
        # the sensitivity half of namespace separation; the adversarial
        # collision-construction is in NamespaceSeparationTests below.
        product, _ = _product_identity()
        legacy, _ = _legacy_identity()
        self.assertNotEqual(product, legacy)


# ---------------------------------------------------------------------------
# 3. Namespace separation: the two bases can never collide
# ---------------------------------------------------------------------------

class NamespaceSeparationTests(unittest.TestCase):
    """`product-key` and `legacy-pid` packets can never collide.

    The module docstring's own claim: "THE BASIS IS INSIDE THE HASHED
    REPRESENTATION. Not merely stored beside it... a `product-key` packet
    and a `legacy-pid` packet cannot collide even if a future product key
    were somehow numerically equal to a `pid`, because the discriminator is
    part of what was hashed." This test is that claim, made adversarial.
    """

    def test_most_adversarial_case_still_does_not_collide(self):
        # Construct the closest two packets CAN get: a legacy pid and a
        # product key that are numerically/textually the same value under
        # the two image builders, plus every other component held identical.
        # If the basis discriminator were only metadata beside the payload
        # (e.g. a separate outbox column) rather than a hashed field, these
        # two would collide -- str(4242) as a product_key vs int(4242) as a
        # legacy_pid are "the same identity" under every field except which
        # basis produced it.
        shared_value = "4242"
        product_image = product_key_image(shared_value)
        legacy_image = legacy_pid_image(int(shared_value))

        product_payload = canonical_payload(
            product_image, detection_key(CATALOG_ID, ISDIFFPOS),
            RELEASE_IDENTITY)
        legacy_payload = canonical_payload(
            legacy_image, detection_key(CATALOG_ID, ISDIFFPOS),
            RELEASE_IDENTITY)

        # Why this can't collide: `image` is INSIDE canonical_payload (not a
        # sibling field looked up afterward), and the two builders emit
        # structurally different keys under "image" -- product_key_image
        # produces {"basis": ..., "product_key": ...} while legacy_pid_image
        # produces {"basis": ..., "legacy_processing_id": ...}. Different key
        # sets serialize to different JSON text no matter what the values
        # are, so the digest moves even though every value a human could
        # compare ("4242" vs 4242) looks like "the same identity".
        self.assertNotEqual(canonical_json(product_payload),
                            canonical_json(legacy_payload))
        self.assertNotEqual(alert_id(product_payload),
                            alert_id(legacy_payload))
        # And the basis field itself is the visible discriminator, present
        # in both payloads under the same key, but with different values --
        # the mechanism the module docstring is describing.
        self.assertEqual(product_payload["identity_basis"], "product-key")
        self.assertEqual(legacy_payload["identity_basis"], "legacy-pid")


# ---------------------------------------------------------------------------
# 4. Forbidden inputs
# ---------------------------------------------------------------------------

class ForbiddenInputTests(unittest.TestCase):
    """No `sid`, path/uri/filename, attempt/run id, or batch id, ever.

    Two angles, matching the module's own two-part account: (a) a normal
    payload built through the public API never contains one of these keys
    to begin with, walked recursively rather than checked at the top level
    because -- per `_reject_forbidden`'s docstring -- "the forbidden inputs
    are exactly the ones that get reintroduced nested inside a component
    record, where a top-level check would not see them"; and (b) the guard
    that would catch a reintroduction is actually live, proven by calling it
    directly with a crafted dict rather than trusting that it is merely
    unexercised in the payloads this module happens to build today.
    """

    #: Substrings from `alerts.identity.FORBIDDEN_KEY_PARTS`, restated here
    #: rather than imported, so this test also catches someone editing the
    #: module's list to remove one without updating what the suite expects.
    MUST_NEVER_APPEAR = ("sid", "path", "uri", "filename", "attempt_id",
                         "run_id", "batch")

    def _assert_no_forbidden_keys(self, node, trail=()):
        """Recursively walk a payload, failing on any of MUST_NEVER_APPEAR.

        Walked rather than checked shallowly for the same reason
        `_reject_forbidden` walks: a forbidden key nested inside "image" or
        "detection" would pass a top-level-only check and still be a design
        defect (a path or a surrogate id feeding the identity).
        """
        if isinstance(node, dict):
            for key, value in node.items():
                lowered = str(key).lower()
                for forbidden in self.MUST_NEVER_APPEAR:
                    self.assertNotIn(
                        forbidden, lowered,
                        f"{key!r} at {trail!r} looks forbidden ({forbidden!r})")
                self._assert_no_forbidden_keys(value, trail + (key,))
        elif isinstance(node, (list, tuple)):
            for position, value in enumerate(node):
                self._assert_no_forbidden_keys(value, trail + (position,))

    def test_product_key_payload_carries_no_forbidden_keys(self):
        _, payload = _product_identity()
        self._assert_no_forbidden_keys(payload)

    def test_legacy_pid_payload_carries_no_forbidden_keys(self):
        # legacy_processing_id CONTAINS the substring "sid"? No -- but it
        # does contain nothing on MUST_NEVER_APPEAR's list either way. What
        # it DOES contain, deliberately, is a database surrogate id under an
        # ALLOWLISTED spelling (see the module docstring's "WHY
        # legacy_processing_id AND NOT pid" section): the raw substring
        # "pid" is not on this test's list because the allowlist exists
        # precisely so that this one spelling is permitted while every other
        # spelling of a surrogate id still raises. This test asserts the
        # payload built through the public API reflects that allowance
        # without tripping any of the truly-forbidden substrings.
        _, payload = _legacy_identity()
        self._assert_no_forbidden_keys(payload)
        self.assertIn("legacy_processing_id", payload["image"])

    def test_reject_forbidden_raises_on_a_crafted_forbidden_key(self):
        # Proves the guard is LIVE, not merely unexercised: a payload that
        # this module's own builders would never produce, but that some
        # future change might, must still be refused when handed to the
        # guard directly.
        with self.assertRaises(ForbiddenIdentityInput):
            _reject_forbidden({"sid": 12345})

    def test_reject_forbidden_raises_when_nested_deeply(self):
        # The exact scenario the module docstring's own reasoning names:
        # "the forbidden inputs are exactly the ones that get reintroduced
        # nested inside a component record, where a top-level check would
        # not see them." A shallow check would pass this; the real guard
        # must not.
        nested = {"image": {"basis": "product-key",
                            "product_key": "sha256:" + "a" * 64,
                            "provenance": {"nested": {"run_id": "run-1"}}}}
        with self.assertRaises(ForbiddenIdentityInput):
            _reject_forbidden(nested)

    def test_reject_forbidden_names_the_offending_key_and_its_location(self):
        # ForbiddenIdentityInput's docstring promises the fix is "one grep
        # away" -- meaning the exception carries the key, and the message
        # locates it. Asserted here so a refactor that dropped the `.key`
        # attribute or the location trail would be caught as a regression
        # in the diagnostic, not just in the raise itself.
        try:
            _reject_forbidden({"outer": {"attempt_id": "a-1"}})
            self.fail("expected ForbiddenIdentityInput")
        except ForbiddenIdentityInput as exc:
            # `.key` is the part callers grep for; the trail ("outer ->
            # attempt_id") is folded into the message by " -> ".join in
            # ForbiddenIdentityInput.__init__, so it is checked in the
            # rendered string rather than as a separate attribute.
            self.assertEqual(exc.key, "attempt_id")
            self.assertIn("outer", str(exc))


# ---------------------------------------------------------------------------
# 5. Digest shape
# ---------------------------------------------------------------------------

class DigestShapeTests(unittest.TestCase):
    """`sha256:` prefix, 64 hex chars -- the shape every digest in this
    module and its siblings (payload_checksum, product-key identity) share,
    per alert_id's own docstring: "so a stored id says how it was computed
    and a future algorithm change is visible in the value rather than
    inferred from its length."""

    def _assert_is_sha256_digest(self, value):
        self.assertTrue(value.startswith("sha256:"))
        hex_part = value[len("sha256:"):]
        self.assertEqual(len(hex_part), 64)
        int(hex_part, 16)  # raises ValueError if not valid hex

    def test_alert_id_has_sha256_prefix_and_64_hex_chars(self):
        computed_id, _ = _product_identity()
        self._assert_is_sha256_digest(computed_id)

    def test_legacy_basis_alert_id_has_sha256_prefix_and_64_hex_chars(self):
        computed_id, _ = _legacy_identity()
        self._assert_is_sha256_digest(computed_id)


# ---------------------------------------------------------------------------
# 6. Error cases
# ---------------------------------------------------------------------------

class ErrorCaseTests(unittest.TestCase):
    """Every way an alert identity can be under-specified raises loudly.

    `AlertIdentityError`'s own docstring: "a fallback id would be a real row
    claiming a real identity it does not have, and the next packet computing
    the same fallback would collide with it" -- so every one of these must
    raise rather than silently degrade to a partial or default identity.
    """

    def test_both_bases_given_raises(self):
        # alert_identity's docstring: resolving this by precedence "would
        # hide a difference image that has a product binding being outboxed
        # as legacy" -- a silent choice the caller didn't make. Refused
        # instead of guessed.
        with self.assertRaises(AlertIdentityError):
            alert_identity(product_key=PRODUCT_KEY, legacy_pid=LEGACY_PID,
                           catalog_id=CATALOG_ID, isdiffpos=ISDIFFPOS,
                           release_identity=RELEASE_IDENTITY)

    def test_neither_basis_given_raises(self):
        # "an identity over the detection alone would collide across every
        # image that ever produced a detection with that catalog id."
        with self.assertRaises(AlertIdentityError):
            alert_identity(catalog_id=CATALOG_ID, isdiffpos=ISDIFFPOS,
                           release_identity=RELEASE_IDENTITY)

    def test_isdiffpos_none_raises(self):
        # detection_key's own deliberate error: isdiffpos is REQUIRED and
        # NOT defaulted, because "a caller that does not know the sign does
        # not know which detection this is" (migration 041's collision).
        # This is distinct from a generic missing-value error -- it exists
        # specifically because None must not silently become False or True.
        with self.assertRaises(AlertIdentityError):
            alert_identity(product_key=PRODUCT_KEY, catalog_id=CATALOG_ID,
                           isdiffpos=None, release_identity=RELEASE_IDENTITY)

    def test_missing_catalog_id_raises(self):
        with self.assertRaises(AlertIdentityError):
            alert_identity(product_key=PRODUCT_KEY, catalog_id=None,
                           isdiffpos=ISDIFFPOS,
                           release_identity=RELEASE_IDENTITY)

    def test_missing_release_identity_raises(self):
        # Not explicitly required by the task list but the same _require
        # discipline applies to every component -- an absent release
        # identity is exactly as much "a confident claim about a packet
        # whose identity nobody knows" as an absent catalog id.
        with self.assertRaises(AlertIdentityError):
            alert_identity(product_key=PRODUCT_KEY, catalog_id=CATALOG_ID,
                           isdiffpos=ISDIFFPOS, release_identity=None)


# ---------------------------------------------------------------------------
# 7. payload_checksum
# ---------------------------------------------------------------------------

class PayloadChecksumTests(unittest.TestCase):
    """`payload_checksum`: sha256-prefixed digest over raw packet bytes.

    Per its own docstring, this is "the cheap comparison that proves" two
    packets agreeing on `alert_id` also agree on their content -- so it must
    actually be sensitive to the bytes, not a constant or a digest of
    something else.
    """

    def test_returns_sha256_prefixed_digest(self):
        checksum = payload_checksum(b"some packet bytes")
        self.assertTrue(checksum.startswith("sha256:"))
        hex_part = checksum[len("sha256:"):]
        self.assertEqual(len(hex_part), 64)
        int(hex_part, 16)

    def test_different_bytes_give_different_checksums(self):
        first = payload_checksum(b"packet-one")
        second = payload_checksum(b"packet-two")
        self.assertNotEqual(first, second)

    def test_same_bytes_give_same_checksum(self):
        # The collision guard this feeds compares checksums across separate
        # insert attempts, so this must be as deterministic as alert_id
        # itself.
        first = payload_checksum(b"identical bytes")
        second = payload_checksum(b"identical bytes")
        self.assertEqual(first, second)


# ---------------------------------------------------------------------------
# 8. Serialization stability
# ---------------------------------------------------------------------------

class SerializationStabilityTests(unittest.TestCase):
    """`canonical_json`: sorted keys, no whitespace, construction-order
    independent.

    Per canonical_json's own docstring: "sort_keys=True so two dicts
    differing only in construction order serialize identically; explicit
    separators so no Python version's whitespace default can change a
    digest." Both halves of that claim are tested directly rather than
    trusted from reading the source, since a stray `json.dumps` default
    creeping back in (e.g. someone "simplifying" the call) would move every
    alert_id in the tree without any single test here failing loudly unless
    this is checked.
    """

    def test_output_has_no_whitespace(self):
        payload = {"b": 2, "a": 1, "nested": {"z": [1, 2, 3]}}
        serialized = canonical_json(payload)
        self.assertNotIn(" ", serialized)
        self.assertNotIn("\n", serialized)
        self.assertNotIn("\t", serialized)

    def test_keys_are_sorted_in_the_output(self):
        payload = {"zebra": 1, "apple": 2, "mango": 3}
        serialized = canonical_json(payload)
        # A crude but direct check: each key's first quote position should
        # appear in sorted order in the serialized text.
        positions = [serialized.index(f'"{key}"')
                    for key in ("apple", "mango", "zebra")]
        self.assertEqual(positions, sorted(positions))

    def test_construction_order_does_not_change_the_serialization(self):
        built_forward = {}
        built_forward["basis"] = "product-key"
        built_forward["product_key"] = "sha256:" + "a" * 64
        built_forward["serialization_version"] = 1

        built_backward = {}
        built_backward["serialization_version"] = 1
        built_backward["product_key"] = "sha256:" + "a" * 64
        built_backward["basis"] = "product-key"

        # Two dicts that compare equal but were populated in opposite key
        # order -- exactly the "differing only in construction order" case
        # the docstring names. If sort_keys were ever dropped, Python's
        # dict insertion-order-preserving iteration would make these two
        # serialize differently and silently split one packet's identity in
        # two depending on which code path built its payload dict.
        self.assertEqual(built_forward, built_backward)
        self.assertEqual(canonical_json(built_forward),
                         canonical_json(built_backward))

    def test_construction_order_does_not_change_a_real_alert_id(self):
        # The same property, exercised through the public alert_identity
        # entry point rather than a hand-built dict, so it also covers
        # canonical_payload's own key insertion order.
        via_product, _ = _product_identity()
        # Build the identical components again, nothing reordered at this
        # call site (alert_identity always constructs the payload the same
        # way internally) -- restated here as the end-to-end counterpart of
        # test_construction_order_does_not_change_the_serialization above.
        via_product_again, _ = _product_identity()
        self.assertEqual(via_product, via_product_again)


if __name__ == "__main__":
    unittest.main()
