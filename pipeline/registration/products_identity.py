"""
File:    products_identity.py

Where an attempt's terminal record becomes a product key, an artifact set,
and a binding — the D1/D2 write path, run inside the registration
transaction.

**WHY A SEPARATE MODULE FROM `products.py`.** `products.py` carries the
legacy registration bodies, ported verbatim from the four `__main__`-only
scripts, and its value is that it is recognisably those bodies. The
identity model is new behaviour with its own vocabulary, its own failure
modes and its own reasons; interleaving it into those bodies would make the
port unreadable and the new work unreviewable at the same time. The bodies
call in here at one point each.

**WHAT THIS DOES NOT CHANGE.** Not the registration transaction's boundary,
not its per-attempt lease, not the watermark, not C3's unified lock order.
Every write here goes through the connection that transaction already
holds, so product rows, artifact rows, legacy version rows and the
watermark commit together or roll back together. D changes WHAT the
transaction records, never the transaction.

**THE READERS ARE NOT MIGRATED.** `refimages` and `diffimages` keep every
column populated exactly as today, because the production reader set is
broader than the registration writers — reference selection, post-DB
gathering over `diffimages.filename`, forced photometry's URI+checksum
join, alert production's companion-file anchoring, the `pid`/`vbest`
currency sweeps and catalog-load's sibling-catalogue derivation. This
module ADDS rows; it rewrites none.
"""

import logging

from pipeline.registration.artifacts import record_artifacts
from pipeline.registration.identity import (CLASS_DIFFERENCE_IMAGE,
                                            CLASS_REFERENCE_IMAGE,
                                            ProductIdentityError,
                                            ROLE_DIFFERENCE_IMAGE,
                                            ROLE_REFERENCE_IMAGE,
                                            SERIALIZATION_VERSION,
                                            difference_image_key,
                                            reference_image_key)

logger = logging.getLogger("rapid.registration.identity")


class MissingIdentityComponent(ProductIdentityError):
    """The record does not carry a component the product key needs.

    Its own type, and deliberately NOT `MissingRecordFact`: that exception
    is caught by the registration consumer as a DURABLE REJECTION — the
    record is malformed, the attempt is rejected, and the watermark advances
    past it. A record that is complete by today's standard but predates
    deterministic product identity is a different thing entirely, and
    classifying it as malformed would durably reject every attempt written
    before this package landed.

    See `identity_or_none`, which is what the bodies actually call: the
    absence is logged and registration proceeds legacy-only, so the failure
    mode of a partial rollout is "no product row yet", not "the attempt is
    rejected forever".
    """


def _need(record, science, name, attempt_id):
    """One product-key component from the record, or a named failure."""
    value = record.get(name)
    if value is None:
        value = science.get(name)
    if value is None:
        raise MissingIdentityComponent(
            f"attempt {attempt_id}'s record does not carry {name!r}, which "
            f"is a component of the deterministic product key (rule 10); a "
            f"key computed without it would claim an identity this product "
            f"does not have")
    return value


def reference_identity(record, science, attempt_id):
    """`(product_key, payload)` for the reference image this attempt built.

    Subject: field + filter — the reference image's SCIENTIFIC TARGET, not
    the exposure/SCA work unit that triggered the build. Two builds of the
    same field and filter from the same inputs under the same specification
    are the same product, whichever exposure noticed one was needed; keying
    on the triggering exposure would make that false and would defeat the
    reprocessing reproducibility rule 10 exists to give.

    Inputs: the coadded science images by mission identity, put into a
    canonical total order by `identity.ordered_science_inputs`.
    """
    identities = _need(record, science, "coadd_input_identities", attempt_id)
    return reference_image_key(
        process_family=int(_need(record, science, "ppid", attempt_id)),
        definition_checksum=_need(record, science, "definition_checksum",
                                  attempt_id),
        release_digest=_need(record, science, "release_content_digest",
                             attempt_id),
        field=int(_need(record, science, "field", attempt_id)),
        fid=int(_need(record, science, "fid", attempt_id)),
        coadd_inputs=identities,
        role=ROLE_REFERENCE_IMAGE)


def difference_identity(record, science, attempt_id, reference_product_key):
    """`(product_key, payload)` for the difference image this attempt made.

    Subject: the typed exposure/SCA subject, which for this product class
    IS the work unit's subject — they coincide here and differ for
    reference images.

    Inputs, in the fixed order: the science exposure by its mission
    `(expid, sca)` identity, then the reference image BY ITS PRODUCT KEY.
    The order is meaningful (they are different roles in a subtraction, not
    an unordered set) and so is not sorted.

    `reference_product_key` is resolved by the caller, which is what makes
    identity compositional: a difference image's key changes exactly when
    its reference's identity changes.
    """
    return difference_image_key(
        process_family=int(_need(record, science, "ppid", attempt_id)),
        definition_checksum=_need(record, science, "definition_checksum",
                                  attempt_id),
        release_digest=_need(record, science, "release_content_digest",
                             attempt_id),
        exposure=int(_need(record, science, "expid", attempt_id)),
        sca=int(_need(record, science, "sca", attempt_id)),
        reference_product_key=reference_product_key,
        science_infobits=science.get("infobits"),
        role=ROLE_DIFFERENCE_IMAGE)


def reference_key_for_difference(repository, science, attempt_id):
    """The product key of the reference image a difference image used.

    Resolved from the legacy `rfid` the record already carries, through the
    binding table. The `rfid` is used ONLY as a lookup handle here and never
    enters the digest — what enters is the reference's own product key, read
    back from its product row.

    Returns None where the reference image has no product row yet, which is
    the ordinary state during rollout: reference images registered before
    this package landed have no key, so the difference images made against
    them cannot have one either. Registration then proceeds legacy-only for
    that attempt rather than inventing a key over an unidentified input.
    """
    rfid = science.get("reference_image_id")
    version = science.get("reference_image_version")
    if rfid is None:
        return None
    row = repository.product_key_for_reference(int(rfid), version)
    if row is None:
        logger.info(
            "attempt %s used reference rfid=%s, which has no product row; "
            "no difference-image product key can be computed until the "
            "reference is registered under the identity model",
            attempt_id, rfid)
    return row


def register_identity(repository, record, science, attempt_id,
                      record_sequence, product_class, published_name,
                      product_key, payload, legacy_rfid=None,
                      legacy_pid=None, legacy_version=None):
    """Write the product row, the artifact rows and the binding.

    The order is deliberate and is the order the constraints want:

      1. the product row (upsert on `product_key` — a retry at the same
         identity finds the row that is already there);
      2. every published file as an artifact (upsert on the replay key — a
         replay of this attempt at this sequence writes none);
      3. the binding, superseding any previous current binding, carrying
         the legacy `(rfid|pid, version)` this registration wrote;
      4. the legacy row's FK back to the product, so today's consumers and
         the identity model name the same object.

    Returns a dict describing what was written, which the caller folds into
    the registration result so the pass's log and the ledger can say what
    identity a product got.
    """
    product = repository.upsert_product(
        product_key=product_key,
        product_class=product_class,
        role=payload["role"],
        identity_payload=payload,
        serialization_version=SERIALIZATION_VERSION,
        process_family=payload["process_specification"]["process_family"])

    # EVERY published file, not only the one that filled the role. The
    # unselected difference-image variants (ZOGY, naive) and the companion
    # files are published, checksummed bytes with no identity-table row —
    # which is precisely what an artifact is, and what the release doc
    # already recorded as the design (`cdf/science/pipeline.toml:73-75`).
    artifacts = record_artifacts(repository, record, attempt_id,
                                 record_sequence)

    realizing = artifacts.get(published_name)
    if realizing is None:
        raise MissingIdentityComponent(
            f"attempt {attempt_id} registered {published_name!r} as its "
            f"{product_class} but published no artifact under that name; the "
            f"product would have no bytes realizing it")

    repository.bind(product.product_id, realizing.artifact_id,
                    legacy_rfid=legacy_rfid, legacy_pid=legacy_pid,
                    legacy_version=legacy_version)

    if legacy_rfid is not None:
        repository.link_reference_image(product.product_id, legacy_rfid,
                                        legacy_version)
    elif legacy_pid is not None:
        repository.link_difference_image(product.product_id, legacy_pid,
                                         legacy_version)

    logger.info(
        "attempt %s: %s product %s bound to artifact %s (%d artifact(s) "
        "recorded, legacy rfid=%s pid=%s version=%s)",
        attempt_id, product_class, product.product_key,
        realizing.artifact_id, len(artifacts), legacy_rfid, legacy_pid,
        legacy_version)

    return {"product_key": product.product_key,
            "product_id": product.product_id,
            "artifact_id": realizing.artifact_id,
            "artifacts": len(artifacts)}


def register_reference_identity(repository, record, science, attempt_id,
                                record_sequence, rfid, version,
                                published_name="reference_image"):
    """The reference-image identity write. Returns None if not computable."""
    try:
        product_key, payload = reference_identity(record, science, attempt_id)
    except ProductIdentityError as exc:
        logger.info("attempt %s: no reference-image product key (%s); "
                    "registered legacy-only", attempt_id, exc)
        return None
    return register_identity(
        repository, record, science, attempt_id, record_sequence,
        CLASS_REFERENCE_IMAGE, published_name, product_key, payload,
        legacy_rfid=rfid, legacy_version=version)


def register_difference_identity(repository, record, science, attempt_id,
                                 record_sequence, pid, version,
                                 published_name):
    """The difference-image identity write. Returns None if not computable."""
    reference_key = reference_key_for_difference(repository, science,
                                                 attempt_id)
    if reference_key is None:
        return None
    try:
        product_key, payload = difference_identity(record, science,
                                                   attempt_id, reference_key)
    except ProductIdentityError as exc:
        logger.info("attempt %s: no difference-image product key (%s); "
                    "registered legacy-only", attempt_id, exc)
        return None
    return register_identity(
        repository, record, science, attempt_id, record_sequence,
        CLASS_DIFFERENCE_IMAGE, published_name, product_key, payload,
        legacy_pid=pid, legacy_version=version)
