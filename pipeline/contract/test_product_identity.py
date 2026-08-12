"""Product-key determinism and identity hygiene (brief D, criteria 1 and 2).

**WHY THESE LIVE IN THE CONTRACT TIER even though the digest is pure
Python.** The determinism half genuinely is pure computation and would run
anywhere. The hygiene half is not: criterion 2 requires asserting that the
serialization contains no forbidden input, AND that the product row the
database actually stores carries the same payload — a claim about what is
in `products.identity_payload` after a real INSERT through a real jsonb
column. Splitting the two across tiers would put the property and its
enforcement in different suites, and the interesting failure is exactly the
one where they disagree.

The pure-computation tests carry no `conn` fixture and so cost nothing when
the database half skips; the tier's `has_table` probe gates only the tests
that need draft schema.
"""

import json

import pytest

from pipeline.contract import fixture
from pipeline.registration import identity


# A fixed set of components, so every test below varies exactly one thing.
# Values are shaped like the real ones (a 64-hex definition checksum, a
# 64-hex release digest) because a test whose fixtures are shaped unlike
# production can pass on a code path production never takes.
SPEC = {
    "process_family": 15,
    "definition_checksum": "a" * 64,
    "release_digest": "b" * 64,
}

REFERENCE_INPUTS = [(101, 3, 0), (102, 3, 0), (100, 3, 0)]


def _reference_key(**overrides):
    kwargs = dict(SPEC, field=4242, fid=2, coadd_inputs=REFERENCE_INPUTS)
    kwargs.update(overrides)
    return identity.reference_image_key(**kwargs)


def _difference_key(**overrides):
    kwargs = dict(SPEC, exposure=9001, sca=7,
                  reference_product_key="sha256:" + "c" * 64)
    kwargs.update(overrides)
    return identity.difference_image_key(**kwargs)


# ---------------------------------------------------------------------------
# Criterion 1 — determinism
# ---------------------------------------------------------------------------


def test_determinism_same_components_same_key_across_runs_and_attempts():
    """Identical components under two run/attempt pairs give the same key.

    THE WHOLE POINT OF RULE 10, stated as a test. `run_id` and `attempt_id`
    are not parameters of any key function — that is the structural reason
    this holds — so the test demonstrates it the only way it can be
    demonstrated here: by computing the key twice and asserting equality, and
    by asserting (below, in the hygiene tests) that no run or attempt value
    could have reached the payload even if a caller passed one.

    **THE TWO-ATTEMPT FORM IS DEMONSTRATED IN
    `test_product_cardinality.py`**, by
    `test_retry_under_a_new_attempt_keeps_the_product_and_moves_the_binding`:
    two distinct `attempt_id`s register through the real repository against a
    real database and resolve to ONE product row with one product key. That
    is criterion 1's literal reading — "identical components under two
    different run/attempt pairs → same product key" — end to end, and this
    test is its by-construction counterpart.
    """
    first, first_payload = _difference_key()
    second, second_payload = _difference_key()
    assert first == second
    assert first_payload == second_payload
    assert first.startswith("sha256:")
    assert len(first) == len("sha256:") + 64


def test_determinism_reference_key_is_stable():
    assert _reference_key()[0] == _reference_key()[0]


def test_determinism_input_order_does_not_change_a_reference_key():
    """Coadd inputs in a different order produce the SAME key.

    The canonical total order is imposed by `ordered_science_inputs`, which
    exists because the overlap query orders by `dist` with no tie-breaker —
    so two runs of the same query over the same data may legitimately return
    equidistant rows in either order. If that order reached the digest, an
    identical reference image would have two identities depending on which
    way the database happened to sort.
    """
    forward, _ = _reference_key(coadd_inputs=[(100, 3, 0), (101, 3, 0),
                                              (102, 3, 0)])
    backward, _ = _reference_key(coadd_inputs=[(102, 3, 0), (101, 3, 0),
                                               (100, 3, 0)])
    assert forward == backward


@pytest.mark.parametrize("override", [
    {"definition_checksum": "d" * 64},
    {"release_digest": "e" * 64},
    {"process_family": 12},
    {"field": 4243},
    {"fid": 3},
    {"coadd_inputs": REFERENCE_INPUTS + [(103, 3, 0)]},
    {"coadd_inputs": [(101, 3, 0), (102, 3, 0), (100, 4, 0)]},
    {"role": "some_other_role"},
])
def test_varies_when_any_reference_component_changes(override):
    """Changing any ONE component changes the reference-image key.

    Parameterized over every component the brief names, rather than
    spot-checking two: a digest that ignored one of its inputs would pass a
    two-case test and silently collapse two different products onto one
    identity — and the database's UNIQUE constraint would then make the
    second product unregisterable, which is a production outage rather than
    a test failure.
    """
    assert _reference_key(**override)[0] != _reference_key()[0]


@pytest.mark.parametrize("override", [
    {"definition_checksum": "d" * 64},
    {"release_digest": "e" * 64},
    {"process_family": 12},
    {"exposure": 9002},
    {"sca": 8},
    {"reference_product_key": "sha256:" + "f" * 64},
    {"science_infobits": 4},
    {"role": "some_other_role"},
])
def test_varies_when_any_difference_component_changes(override):
    """Changing any ONE component changes the difference-image key."""
    assert _difference_key(**override)[0] != _difference_key()[0]


def test_varies_input_order_where_order_is_meaningful():
    """A difference image's two inputs are ordered, and the order matters.

    Science exposure then reference image — they are different roles in a
    subtraction, not an unordered set, so unlike the coadd list they are NOT
    sorted. Asserted by building the payload by hand in the opposite order
    and checking the digest moves, which is what "order is meaningful" means
    operationally.
    """
    _, payload = _difference_key()
    reversed_payload = dict(payload, inputs=list(reversed(payload["inputs"])))
    assert (identity.product_key(payload)
            != identity.product_key(reversed_payload))


def test_serialization_version_is_inside_the_hashed_payload():
    """A canonical-form change must move keys visibly, not silently."""
    _, payload = _difference_key()
    assert payload["serialization_version"] == identity.SERIALIZATION_VERSION
    bumped = dict(payload, serialization_version=99)
    assert identity.product_key(payload) != identity.product_key(bumped)


# ---------------------------------------------------------------------------
# Criterion 2 — identity hygiene
# ---------------------------------------------------------------------------

#: Every forbidden identity source brief D names, as the substrings that
#: would appear in a canonical serialization if one leaked in. Checked
#: against the SERIALIZED JSON, not the dict, because that is the artifact
#: the digest is actually taken over.
FORBIDDEN_VALUE_MARKERS = (
    "s3://", "/", ".fits", ".csv",
    "run-", "attempt", "batch", "arn:aws",
)


@pytest.mark.parametrize("build", [_reference_key, _difference_key])
def test_hygiene_serialization_carries_no_forbidden_input(build):
    """The canonical serialization contains no path, id or execution handle.

    Asserted over the JSON TEXT the digest is computed from, so the check
    covers values as well as keys — a `rid` smuggled in as an unnamed
    integer would pass a key-name check and fail here if it were ever
    rendered as a path or an ARN, and the key-name check below covers the
    named case.
    """
    _, payload = build()
    serialized = identity.canonical_json(payload)
    for marker in FORBIDDEN_VALUE_MARKERS:
        assert marker not in serialized, (
            f"the canonical serialization contains {marker!r}: {serialized}")


@pytest.mark.parametrize("build", [_reference_key, _difference_key])
def test_hygiene_no_forbidden_key_names(build):
    """No key in the payload names a forbidden identity source."""
    _, payload = build()

    def walk(node, trail=()):
        if isinstance(node, dict):
            for key, value in node.items():
                lowered = str(key).lower()
                if lowered not in identity.ALLOWED_KEYS:
                    for part in identity.FORBIDDEN_KEY_PARTS:
                        assert part not in lowered, (
                            f"{key!r} at {trail} contains {part!r}")
                walk(value, trail + (key,))
        elif isinstance(node, list):
            for position, value in enumerate(node):
                walk(value, trail + (position,))

    walk(payload)


def test_hygiene_forbidden_input_is_refused_in_production_not_only_in_tests():
    """A payload carrying a forbidden key RAISES when built.

    The guard runs in `canonical_payload`, on every production call — not
    only here. A guard that only tests run is a guard production does not
    have, and the failure it prevents (a path reaching product identity) is
    exactly the one a reviewer's eye is worst at catching.
    """
    with pytest.raises(identity.ForbiddenIdentityInput):
        identity.canonical_payload(
            identity.CLASS_DIFFERENCE_IMAGE,
            identity.process_specification(**SPEC),
            identity.difference_image_subject(1, 2),
            [{"kind": "science_image", "uri": "s3://bucket/key.fits"}],
            identity.ROLE_DIFFERENCE_IMAGE)


def test_hygiene_forbidden_input_refused_when_nested_deeply():
    """The guard WALKS; it does not check only the top level.

    The forbidden inputs are the ones reintroduced nested inside an input
    record, where a top-level check sees nothing.
    """
    with pytest.raises(identity.ForbiddenIdentityInput):
        identity.canonical_payload(
            identity.CLASS_REFERENCE_IMAGE,
            identity.process_specification(**SPEC),
            identity.reference_image_subject(1, 2),
            [{"kind": "science_image", "exposure": 1, "sca": 2,
              "provenance": {"nested": {"input_rid": 77}}}],
            identity.ROLE_REFERENCE_IMAGE)


def test_hygiene_coadd_list_checksum_is_refused_as_an_input():
    """The subtle one, named explicitly.

    The coadd-list CSV's checksum looks like a legitimate content digest and
    is specifically forbidden: its rows embed `input_rid` and `filename`, so
    hashing that document hides a surrogate-id and path dependency behind a
    digest — the one form of the violation a reviewer is most likely to wave
    through.
    """
    with pytest.raises(identity.ForbiddenIdentityInput):
        identity.canonical_payload(
            identity.CLASS_REFERENCE_IMAGE,
            identity.process_specification(**SPEC),
            identity.reference_image_subject(1, 2),
            [{"kind": "coadd_list", "coadd_inputs_checksum": "a" * 64}],
            identity.ROLE_REFERENCE_IMAGE)


def test_hygiene_changing_uri_run_and_attempt_leaves_the_key_unchanged():
    """The positive form of criterion 2, and the one that matters.

    "Changing URI/path/run/attempt with identities held fixed leaves the key
    unchanged" is structurally guaranteed here — none of the three is a
    parameter of any key function — so the test states it as the API states
    it: the same identity components produce the same key no matter what
    execution context the caller is in.
    """
    baseline, _ = _difference_key()
    # Simulating "a different run, a different attempt, a different S3
    # prefix" is exactly this: the same components again. There is no
    # argument through which the execution context could enter.
    assert _difference_key()[0] == baseline


def test_missing_component_fails_loud_rather_than_keying_on_a_partial():
    """An absent component raises rather than producing a partial key.

    A key over an absent input would be a confident claim about a product
    whose identity nobody knows — and it is UNIQUE-constrained, so it would
    collide with the next such product rather than merely being wrong.
    """
    with pytest.raises(identity.ProductIdentityError):
        _difference_key(definition_checksum=None)
    with pytest.raises(identity.ProductIdentityError):
        _reference_key(coadd_inputs=[])


# ---------------------------------------------------------------------------
# The stored payload is the computed payload (the database half)
# ---------------------------------------------------------------------------


def test_stored_identity_payload_round_trips_through_jsonb(conn):
    """What the database stores is what the digest was taken over.

    The claim criterion 2 makes is about the SERIALIZATION, so it is only
    worth as much as the guarantee that the row carries that serialization.
    jsonb does not preserve key order or insignificant whitespace — which is
    fine, and is why the digest is recomputed from a re-canonicalized form
    here rather than compared as text.
    """
    if not fixture.has_table(conn, "products"):
        pytest.skip("DRAFT migration 048 is not applied on this database")

    from pipeline.repositories.products import ProductRepository

    key, payload = _difference_key(exposure=int(fixture.RUN_TAG[:6], 16))
    repository = ProductRepository(conn)
    product = repository.upsert_product(
        product_key=key, product_class=identity.CLASS_DIFFERENCE_IMAGE,
        role=payload["role"], identity_payload=payload,
        serialization_version=identity.SERIALIZATION_VERSION,
        process_family=SPEC["process_family"])

    with conn.cursor() as cur:
        cur.execute("SELECT identity_payload FROM products"
                    " WHERE product_id = %s", [product.product_id])
        stored = cur.fetchone()[0]
    conn.rollback()

    if isinstance(stored, str):
        stored = json.loads(stored)
    assert identity.product_key(stored) == key
