"""
File:    identity.py

Deterministic ALERT PACKET identity — the digest that becomes an outbox row's
`alert_id` and the Kafka message key consumers deduplicate on.

**WHY A SEPARATE MODULE FROM `pipeline/registration/identity.py`.** That module
computes PRODUCT keys under rule 10 (process specification, canonical subject,
ordered inputs, role). A packet is not a product: it is one detection's alert
under one release, and its components are fixed by brief E as

  1. the identity-basis discriminator,
  2. the source difference image's identity UNDER THAT BASIS,
  3. the detection's CATALOG key — `(id, isdiffpos)`,
  4. the release identity.

The canonical-serialization DISCIPLINE is shared and deliberately reused: a
versioned payload, sorted keys, fixed separators, a forbidden-inputs guard that
walks the whole structure, and a `sha256:`-prefixed digest. What is not shared
is the component set, and pretending otherwise by widening the product-key
functions would make one module answer two questions with one vocabulary.

**WHY NOT `sid`.** `sid` is the obvious candidate and it is wrong. It is
DB-generated at catalog load — `pipeline/stages/post_db.py`'s COPY column list
does not carry it — so it is REALIZATION-LOCAL: reload the same catalogue and
every detection gets a different `sid`, which would re-mint every alert
identity for data that did not change. The catalogue's own conflict identity is
`(pid, id, isdiffpos)` (`pipeline/stages/catalog_db.py`, migration 041), where
`id` is a per-file ordinal and the SIGN is part of the identity because a
product has a positive and a negative file. So the packet's detection component
is `(id, isdiffpos)`, and the image it belongs to is named by its own identity
rather than by `pid`.

**THE TWO BASES, AND WHY BOTH EXIST.**

  * `product-key` — the difference image's DRAFT 048 `product_key`, resolved
    through the `diffimages.product_id -> products.product_key` join. This is
    the identity that survives reprocessing, and it is the basis every alert
    minted after D's rollout uses.

  * `legacy-pid` — for difference images with NO product binding, which is the
    ordinary state of pre-D history: 048 added `diffimages.product_id` as a
    NULLABLE column, so rows registered before that migration have none. The
    image identity is then the legacy tuple exactly `(pid)`.

The second basis mirrors the D worker's ratified P8 degradation, and it is
degradation, not equivalence: `pid` is a database surrogate — precisely what
rule 10 forbids as a product identity — and it appears here only because for
these rows it is the ONLY identity of record. The alternative was to invent a
key from the image's contents, which would be a confident claim about an
identity nobody could reproduce. So the rule is: legacy-only, logged with its
reason, never invented.

**THE BASIS IS INSIDE THE HASHED REPRESENTATION.** Not merely stored beside it
in the outbox row. That is what makes the two bases namespace-separated BY
CONSTRUCTION: a `product-key` packet and a `legacy-pid` packet cannot collide
even if a future product key were somehow numerically equal to a `pid`, because
the discriminator is part of what was hashed.

**IDENTITY IS IMMUTABLE PER OUTBOX ROW.** A difference image that later gains a
product binding does NOT re-mint identities for packets already outboxed under
`legacy-pid`. The identity was computed at outbox-write time from what was true
then, and the row is write-once. Re-minting would change the key consumers
deduplicate on for bytes they have already received — the exact failure the
digest exists to prevent.

**WHY `legacy_processing_id` AND NOT `pid`.** The forbidden-inputs guard below
refuses any key containing `pid`, inherited from rule 10's list for good
reason. The legacy basis needs that value anyway, by ratified exception, so it
is spelled `legacy_processing_id` and admitted through an ALLOWLIST with the
reason attached — visible, greppable, and impossible to introduce accidentally,
because every other spelling of a surrogate id still raises. A guard with a
documented exception beats a guard switched off.
"""

import hashlib
import json
import logging

logger = logging.getLogger("rapid.alerts.identity")

#: The canonical-serialization version, hashed as part of the payload. Bumping
#: it changes every alert id by design — see `identity.py`'s own reasoning in
#: the registration package: the version is IN the payload, not beside it, so a
#: future change to the canonical form produces different ids deliberately and
#: visibly rather than silently colliding two spellings of one packet.
SERIALIZATION_VERSION = 1

#: The two identity bases, fixed by brief E. Stored in the outbox row's
#: `identity_basis` column (whose CHECK constraint names exactly these) AND
#: hashed as the payload's discriminator.
BASIS_PRODUCT_KEY = "product-key"
BASIS_LEGACY_PID = "legacy-pid"

#: Substrings that must never appear as a KEY in an alert-identity payload.
#: The same guard rule 10 requires of product keys, for the same reason: these
#: are the inputs that make an identity depend on where bytes were stored or on
#: which execution produced them, and a later edit that reintroduces one would
#: otherwise pass review.
#:
#: `sid` is here and is E-specific: it is the single most likely wrong answer
#: for "what identifies a detection", and it is realization-local (see the
#: module docstring). The acceptance suite asserts it is absent.
FORBIDDEN_KEY_PARTS = (
    "uri", "url", "path", "filename", "file_name", "prefix", "key_prefix",
    "sid", "rid", "rfid", "psfid", "rtid",
    "run_id", "attempt_id", "batch", "array_index", "index",
    "manifest_checksum", "topic", "broker",
)

#: Keys that contain a forbidden substring but are legitimate, checked BEFORE
#: `FORBIDDEN_KEY_PARTS`. Each is here for a stated reason:
#:
#:   * `legacy_processing_id` — the `legacy-pid` basis's image identity. A
#:     database surrogate, admitted by brief E's ratified degradation because
#:     for pre-D rows it is the only identity of record. Never used under the
#:     `product-key` basis, which is enforced by construction below (the two
#:     builders emit different image records).
#:   * `product_key` — the `product-key` basis's image identity. Contains no
#:     forbidden substring; listed so this allowlist reads as the complete set
#:     of image-identity spellings.
#:   * `serialization_version` — the canonical form's own version.
ALLOWED_KEYS = frozenset({
    "legacy_processing_id", "product_key", "serialization_version",
})


class AlertIdentityError(ValueError):
    """An alert id could not be computed, and why.

    Raised rather than returning a sentinel because the caller is about to
    write a UNIQUE-constrained outbox row: a fallback id would be a real row
    claiming a real identity it does not have, and the next packet computing
    the same fallback would collide with it.
    """


class ForbiddenIdentityInput(AlertIdentityError):
    """An alert-identity payload carried a forbidden input.

    Its own type because it is a DESIGN defect rather than missing data: it
    means code put a path, a surrogate id, an execution identifier or `sid`
    into the identity payload. It names the offending key so the fix is one
    grep away.
    """

    def __init__(self, key, trail=()):
        location = " -> ".join(str(part) for part in trail) or "<root>"
        super().__init__(
            f"the key {key!r} at {location} is a forbidden alert-identity "
            f"input: a packet's identity is never derived from paths, URIs, "
            f"filenames, `sid` (which is DB-generated at catalog load and so "
            f"is realization-local), database surrogate ids, run/attempt/"
            f"Batch identifiers, array indices, or the delivery target")
        self.key = key


def _require(value, name):
    """One required identity component, or a named failure."""
    if value is None or (isinstance(value, str) and not value.strip()):
        raise AlertIdentityError(
            f"the alert-identity component {name!r} is absent; an alert id "
            f"computed over an absent component would be a confident claim "
            f"about a packet whose identity nobody knows")
    return value


def _reject_forbidden(node, trail=()):
    """Walk a canonical payload, refusing any forbidden key.

    Walks rather than checking the top level: the forbidden inputs are exactly
    the ones that get reintroduced nested inside a component record, where a
    top-level check would not see them.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            lowered = str(key).lower()
            if lowered not in ALLOWED_KEYS:
                for part in FORBIDDEN_KEY_PARTS:
                    if part in lowered:
                        raise ForbiddenIdentityInput(key, trail)
            _reject_forbidden(value, trail + (key,))
    elif isinstance(node, (list, tuple)):
        for position, value in enumerate(node):
            _reject_forbidden(value, trail + (position,))
    return node


def product_key_image(product_key):
    """The source image, named by its DRAFT 048 product key.

    The basis every alert minted after D's rollout uses: a deterministic digest
    over the image's own science identity, so the same difference image
    reprocessed under a new run names the same image here.
    """
    return {
        "basis": BASIS_PRODUCT_KEY,
        "product_key": str(_require(product_key, "image.product_key")),
    }


def legacy_pid_image(pid):
    """The source image, named by its legacy `pid` — the ratified degradation.

    For difference images with no product binding (pre-D history: 048 added
    `diffimages.product_id` as a nullable column). `pid` is a database
    surrogate and would be forbidden as a PRODUCT identity; it is admitted here
    because for these rows there is no other identity of record, and inventing
    one would be worse. Logged by the caller with the reason, per brief E.
    """
    return {
        "basis": BASIS_LEGACY_PID,
        "legacy_processing_id": int(_require(pid, "image.pid")),
    }


def detection_key(catalog_id, isdiffpos):
    """The detection, by the CATALOG's own key — never by `sid`.

    `catalog_id` is `sources.id`, a per-file ordinal, and `isdiffpos` is the
    subtraction sign. Together with the image they are migration 041's
    conflict identity `(pid, id, isdiffpos)`, which is the catalogue's own
    statement of what makes two detection rows the same detection.

    The sign is not decoration: a product has a positive and a negative file
    and `id` restarts per file, so `(id)` alone would conflate a positive
    detection with an unrelated negative one — the exact defect 041's comment
    records having found.
    """
    # `isdiffpos` is REQUIRED and is not defaulted. It is half of the
    # detection's identity, and a missing sign silently defaulted to positive
    # would give a negative detection the positive one's identity — the
    # collision migration 041 exists to prevent. A caller that does not know
    # the sign does not know which detection this is.
    if isdiffpos is None:
        raise AlertIdentityError(
            "the alert-identity component 'detection.isdiffpos' is absent; "
            "the subtraction sign is part of the catalogue's own identity "
            "(migration 041: `id` is a per-file ordinal and a product has a "
            "positive and a negative file), so defaulting it would give a "
            "negative detection a positive detection's identity")
    return {
        "catalog_id": int(_require(catalog_id, "detection.catalog_id")),
        # Normalized to a bool so a driver returning 't'/1/True cannot make two
        # otherwise identical packets hash differently.
        "isdiffpos": bool(isdiffpos),
    }


def canonical_payload(image, detection, release_identity):
    """The exact object an alert id is a digest of.

    Returned rather than only hashed so the acceptance suite can assert over
    its CONTENT — "the serialization contains no sid, no path, no attempt id"
    is a property of this object, and a test that could only see the hex digest
    could not check it.
    """
    payload = {
        "serialization_version": SERIALIZATION_VERSION,
        "identity_basis": str(_require(image, "image")["basis"]),
        "image": _require(image, "image"),
        "detection": _require(detection, "detection"),
        "release_identity": str(_require(release_identity,
                                         "release_identity")),
    }
    return _reject_forbidden(payload)


def canonical_json(payload):
    """The canonical serialization: sorted keys, fixed separators, UTF-8.

    `sort_keys=True` so two dicts differing only in construction order
    serialize identically; explicit `separators` so no Python version's
    whitespace default can change a digest; `ensure_ascii=True` (the default,
    stated) so the bytes are stable regardless of the reader's encoding.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)


def alert_id(payload):
    """The alert id: `sha256:<64 hex>` over the canonical serialization.

    Prefixed with its algorithm, like every other content digest in this tree,
    so a stored id says how it was computed and a future algorithm change is
    visible in the value rather than inferred from its length.
    """
    encoded = canonical_json(payload).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def alert_identity(*, product_key=None, legacy_pid=None, catalog_id,
                   isdiffpos, release_identity):
    """`(alert_id, payload)` for one packet. The one entry point callers use.

    Exactly one of `product_key` or `legacy_pid` is given, and which one is
    given IS the basis. Passing both would leave the basis ambiguous at the
    call site — the caller would have decided already and this function would
    be re-deciding — so it is refused rather than resolved by precedence.

    Passing neither is refused for the reason `_require` gives everywhere else:
    a packet whose source image has no identity at all cannot have one.
    """
    if product_key is not None and legacy_pid is not None:
        raise AlertIdentityError(
            "an alert identity takes ONE image basis: a product_key or a "
            "legacy pid, never both. Which basis applies is the caller's "
            "decision — resolving it here by precedence would hide a "
            "difference image that has a product binding being outboxed as "
            "legacy")
    if product_key is not None:
        image = product_key_image(product_key)
    elif legacy_pid is not None:
        image = legacy_pid_image(legacy_pid)
    else:
        raise AlertIdentityError(
            "an alert identity needs its source image: either the difference "
            "image's product_key (DRAFT 048) or, for pre-D rows with no "
            "product binding, its legacy pid. Neither was given, and an "
            "identity over the detection alone would collide across every "
            "image that ever produced a detection with that catalog id")
    payload = canonical_payload(image, detection_key(catalog_id, isdiffpos),
                                release_identity)
    return alert_id(payload), payload


def payload_checksum(payload_bytes):
    """`sha256:<64 hex>` over the serialized packet bytes.

    Stored beside the bytes in the outbox row and compared by the insert path's
    collision guard: two packets agreeing on `alert_id` must agree on their
    content, and this is the cheap comparison that proves it.
    """
    return "sha256:" + hashlib.sha256(payload_bytes).hexdigest()
