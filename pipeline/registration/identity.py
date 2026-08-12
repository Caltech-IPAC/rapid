"""
File:    identity.py

Deterministic product identity — the digest rule 10 requires, and the
canonical serialization it is taken over.

**What rule 10 asks for, verbatim:** "Products and artifacts are distinct
records. Scientific identity is a deterministic digest of process
specification, canonical subject, ordered inputs and role — never a path,
Batch ID or array index."

Today's product identity is `(attempt_id, record_sequence)`-keyed replay
dedup plus an auto-incrementing `version`
(`pipeline/registration/products.py`), with the S3 key
(`pipeline/stages/context.py:130-165`, embedding `run_id` and `attempt_id`)
load-bearing for uniqueness. That is identity by path and by execution
accident: the same science, reprocessed under a new run, produces a
different key, and nothing in the database says the two are the same
product. This module is the replacement. After it, the path is an ADDRESS
for bytes (an artifact's storage location) and never an identity.

**THE FOUR COMPONENTS ARE FIXED, NOT CHOSEN HERE.** Brief D fixes them and
this module implements exactly them, in this order:

  1. the process specification,
  2. the canonical product subject,
  3. the ordered inputs,
  4. the role.

**Component 1 is a specification, not a build.** `ppid` alone is NOT the
process specification: it is a routing fact — "which pipeline a row belongs
to" (`submission/routes.py:39-42`, values 12 and 15, shared by science and
reprocessing) — and it carries no version at all. The canonical process-spec
object is therefore the process family (the `ppid` value, kept as the legacy
family attribute) plus the workflow-definition checksum
(`pipeline/intent/definitions.py`, the sha256 of the reviewed definition
file's bytes) plus the release science-content digest
(`pipeline/runtime/science_config.py:252`, the canonicalized digest of
`cdf/science/pipeline.toml`).

Source and container identity — `build_provenance`
(`pipeline/entrypoints/job.py:174-186`), the image digest, the git revision
— is deliberately NOT in the product key. It is recorded on the ARTIFACT
instead (`pipeline/registration/artifacts.py`). The reason is the
distinction rule 10 draws: the product key tracks the *specified science
process* (its definition and its configuration), while the build that
executed it is provenance about a particular execution of that
specification. Two builds of the same reviewed definition and the same
release configuration are the same product by intent — a rebuilt container
with an unchanged science specification must not fork product identity —
and the artifact still records exactly which build produced each set of
bytes, so nothing is lost. (Recorded verbatim for the merge gate; brief D
requires this rationale in the proposals ledger.)

**Component 2 is the product's subject, which is NOT always the work
unit's subject.** The typed work-unit subject (`submission/subjects.py`)
identifies *work*; the product subject identifies the *scientific target of
a product*. They coincide for difference images (exposure/SCA) and DIFFER
for reference images: the work unit is declared at exposure/SCA grain
(`subjects.py:148-149`) but a reference image's target is a field and a
filter (`add_refimage(ppid, field, fid, ...)`,
`products.py:187`; `get_best_reference_image(ppid, field, fid)`,
`rapid_db.py:1680`). Conflating the two would give every reference image
built from a different triggering exposure a different identity, which is
precisely the reprocessing-reproducibility property rule 10 exists to
provide. So the subject is declared PER PRODUCT CLASS, below, and the
work-unit tuple is never hard-coded as the product subject.

**Component 3 is inputs by their own identities, never by their storage.**
The forbidden list is explicit and enforced by `_reject_forbidden`: URIs,
paths, filenames, database surrogate ids (`rid`/`rfid`/`pid`), the
coadd-list CSV checksum, the manifest checksum, `run_id`, `attempt_id`,
Batch ids, array indices, or anything derived from them. The coadd-list
checksum deserves its own sentence because it is the subtle one: its rows
embed `input_rid` and `filename` (`submission/gathering.py:852,901`), so
hashing that document would hide a path dependency behind a digest and pass
a reviewer's eye. The inputs are named by mission identity instead — the
`(expid, sca)` the survey itself assigns — which those same rows carry.

**Component 4 is the role, not the algorithm.** `"difference_image"` is the
stable consumer-contract role; the release binds it to `"sfft_diffimage"`
(`cdf/science/pipeline.toml:81`). That binding is already inside the release
science-content digest, i.e. inside component 1, so the bound algorithm name
does not appear in the key a second time. Putting it in twice would make a
rebinding change the key through two independent paths and make the key's
meaning ambiguous.

**Why a versioned canonical serialization.** The digest is over JSON with
sorted keys and fixed separators, prefixed by a serialization version. The
version is IN the hashed payload, not beside it: a future change to the
canonical form must produce different keys deliberately and visibly, rather
than silently colliding old and new spellings of the same content. This is
the same canonicalization discipline `science_config.digest` and
`submission.startup.configuration_digest` already use, for the same reason.

**Fail-loud, never a partial key.** Every component is required. A missing
one raises `ProductIdentityError` naming it, because a product key computed
over an absent input would be a confident statement about a product whose
inputs nobody knows — worse than no key at all, since it would be
UNIQUE-constrained in the database and would collide with the next such
product.
"""

import hashlib
import json

#: The canonical-serialization version, hashed as part of the payload.
#: Bumping it changes every product key by design; see the module docstring.
SERIALIZATION_VERSION = 1

#: The stable consumer-contract roles. `cdf/science/pipeline.toml` binds
#: these to concrete algorithm names; the binding lives in the release
#: digest, not here.
ROLE_DIFFERENCE_IMAGE = "difference_image"
ROLE_REFERENCE_IMAGE = "reference_image"

#: The product classes, which decide the subject shape and the input shape.
CLASS_DIFFERENCE_IMAGE = "difference_image"
CLASS_REFERENCE_IMAGE = "reference_image"

#: Substrings that must never appear as a KEY anywhere in a canonical
#: serialization. This is a guard against the identity inputs rule 10
#: forbids being reintroduced by a later edit — the acceptance suite asserts
#: over it, and so does every call, because a guard that only tests run is a
#: guard production does not have.
FORBIDDEN_KEY_PARTS = (
    "uri", "url", "path", "filename", "file_name", "prefix", "key_prefix",
    "rid", "rfid", "pid", "psfid", "rtid",
    "run_id", "attempt_id", "batch", "array_index", "index",
    "manifest_checksum", "coadd_inputs_checksum", "coadd_inputs_uri",
    "version",
)

#: Key names that contain a forbidden substring but are legitimate, checked
#: before `FORBIDDEN_KEY_PARTS`. Each is here for a stated reason:
#:   * `definition_checksum` / `release_digest` — component 1's two content
#:     digests. They contain no forbidden substring but are listed so the
#:     allowlist reads as the complete set of digest-bearing keys.
#:   * `serialization_version` — the canonical form's own version, which is
#:     the one "version" that legitimately appears (it is metadata about the
#:     serialization, not a database row version).
ALLOWED_KEYS = frozenset({
    "definition_checksum", "release_digest", "serialization_version",
})


class ProductIdentityError(ValueError):
    """A product key could not be computed, and why.

    Raised rather than returning a sentinel because every caller is writing
    a UNIQUE-constrained database row: a fallback key would be a real row
    claiming a real identity it does not have.
    """


class ForbiddenIdentityInput(ProductIdentityError):
    """A canonical serialization carried a forbidden identity input.

    Its own type because it is a DESIGN defect rather than missing data: it
    means code somewhere put a path, a surrogate id or an execution
    identifier into the identity payload, which is the exact failure rule 10
    names. It names the offending key so the fix is one grep away.
    """

    def __init__(self, key, trail=()):
        location = " -> ".join(str(part) for part in trail) or "<root>"
        super().__init__(
            f"the key {key!r} at {location} is a forbidden identity input: "
            f"product identity is never derived from paths, URIs, filenames, "
            f"database surrogate ids, run/attempt/Batch identifiers, array "
            f"indices, or the coadd-list or manifest checksums (rule 10)")
        self.key = key


def _require(value, name):
    """One required identity component, or a named failure."""
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ProductIdentityError(
            f"the product key component {name!r} is absent; a product key "
            f"computed over an absent component would be a confident claim "
            f"about a product whose identity nobody knows")
    return value


def _reject_forbidden(node, trail=()):
    """Walk a canonical payload, refusing any forbidden key.

    Walks rather than checks the top level: the forbidden inputs are exactly
    the ones that get reintroduced nested inside an input record, where a
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


def process_specification(process_family, definition_checksum,
                          release_digest):
    """Component 1: the specified science process.

    Parameters
    ----------
    process_family : int
        The `ppid` value, kept as the legacy family attribute. A routing
        fact, carried because the operations tables are partitioned by it,
        but never the whole specification — see the module docstring.
    definition_checksum : str
        The sha256 of the reviewed workflow-definition file's BYTES
        (`pipeline.intent.definitions.read_definition`).
    release_digest : str
        The release science-content digest
        (`pipeline.runtime.science_config.digest`).
    """
    return {
        "process_family": int(_require(process_family, "process_family")),
        "definition_checksum": str(
            _require(definition_checksum, "definition_checksum")),
        "release_digest": str(_require(release_digest, "release_digest")),
    }


def difference_image_subject(exposure, sca):
    """Component 2 for a difference image: the typed exposure/SCA subject.

    Named, typed components rather than a bare tuple, so the serialization
    says what each number is. `exposure` here is the MISSION exposure
    identifier (`expid`, `L2Files.expid`), not a date ordinal and not a
    database row id.
    """
    return {
        "grain": "exposure_sca",
        "exposure": int(_require(exposure, "subject.exposure")),
        "sca": int(_require(sca, "subject.sca")),
    }


def reference_image_subject(field, fid):
    """Component 2 for a reference image: the field + filter target.

    NOT the triggering exposure. A reference image is built for a patch of
    sky in a filter and is selected back by exactly that
    (`get_best_reference_image(ppid, field, fid)`); two builds of the same
    field and filter from the same inputs are the same product, whichever
    science exposure happened to notice one was needed.
    """
    return {
        "grain": "field_filter",
        "field": int(_require(field, "subject.field")),
        "fid": int(_require(fid, "subject.fid")),
    }


def science_image_input(exposure, sca, infobits=None):
    """One science exposure input, by its mission identity.

    `(expid, sca)` is the survey's own name for this L2 file — the identity
    that survives reprocessing, unlike `rid` (a database surrogate) or the
    filename.

    `infobits` is the L2 quality mask (`L2Files.infobits`) and is included
    when the manifest carries it: it is a property OF the input file's
    content, so two L2 files with the same `(expid, sca)` but different
    quality masks are genuinely different inputs. It is optional because a
    manifest may not carry it, and refusing to compute a key for that case
    would block reprocessing of older submissions.

    **NO CALIBRATION VERSION IS AVAILABLE.** Brief D asks for "whatever
    calibration-version identity the pipeline actually holds for the L2
    file" and to record what is available. Verified at head 7dd00dd: the
    pipeline holds NONE — there is no calibration version, no CRDS context
    and no reduction-version fact in `UnitFacts`
    (`submission/manifest.py:264` ff.), in the coadd-input rows
    (`submission/gathering.py:852`), or anywhere in the manifest schema. So
    the input identity is `(expid, sca[, infobits])` and the absence is a
    recorded gap, not an invented value: if the mission later supplies a
    calibration version, adding it here is a `SERIALIZATION_VERSION` bump.
    """
    record = {
        "kind": "science_image",
        "exposure": int(_require(exposure, "input.exposure")),
        "sca": int(_require(sca, "input.sca")),
    }
    if infobits is not None:
        record["infobits"] = int(infobits)
    return record


def reference_image_input(product_key):
    """The reference image a difference image was made against, BY ITS KEY.

    Brief D fixes this: the reference input enters a difference image's
    identity as the reference image's own PRODUCT KEY, not as `rfid` and not
    as its URI. That makes product identity compositional — a difference
    image's key changes exactly when its reference's identity changes — and
    it keeps the surrogate id out of the payload.
    """
    return {
        "kind": "reference_image",
        "product_key": str(_require(product_key, "input.product_key")),
    }


def ordered_science_inputs(inputs):
    """A canonical total order over science-image inputs.

    Sorted by the identity tuple itself, NEVER by query return order. The
    overlap query that produces coadd inputs orders by `dist`
    (`rapid_db.py:1498`) with no tie-breaker, so two runs of the same query
    over the same data may return equidistant rows in either order. Letting
    that reach the digest would make identity depend on the database's row
    ordering — a nondeterminism indistinguishable from a real identity
    change.

    Takes already-built input records (`science_image_input`) and returns
    them sorted; deduplication is NOT done here, because a repeated input is
    a caller-side defect this function would hide.
    """
    return sorted(
        inputs,
        key=lambda record: (record["exposure"], record["sca"],
                            record.get("infobits", -1)))


def canonical_payload(product_class, specification, subject, inputs, role):
    """The exact object the product key is a digest of.

    Returned rather than only hashed so the acceptance suite can assert over
    its CONTENT — "the serialization contains no URI, no `rid`, no
    `run_id`" is a property of this object, and a test that could only see
    the hex digest could not check it.
    """
    payload = {
        "serialization_version": SERIALIZATION_VERSION,
        "product_class": str(_require(product_class, "product_class")),
        "process_specification": _require(specification,
                                          "process_specification"),
        "subject": _require(subject, "subject"),
        "inputs": list(_require(inputs, "inputs")),
        "role": str(_require(role, "role")),
    }
    if not payload["inputs"]:
        raise ProductIdentityError(
            "a product key needs at least one ordered input; an empty input "
            "list would give every product of this class and subject the "
            "same identity")
    return _reject_forbidden(payload)


def canonical_json(payload):
    """The canonical serialization: sorted keys, fixed separators, UTF-8.

    `sort_keys=True` so two dicts differing only in construction order
    serialize identically; explicit `separators` so no Python version's
    whitespace default can change a digest; `ensure_ascii=True` (the
    default, stated) so the bytes are stable regardless of the reader's
    encoding.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)


def product_key(payload):
    """The product key: `sha256:<64 hex>` over the canonical serialization.

    Prefixed with its algorithm, the same way the tree's other content
    digests are (`image_digest`, `manifest_checksum`), so a stored key says
    how it was computed and a future algorithm change is visible in the
    value rather than inferred from its length.
    """
    encoded = canonical_json(payload).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def difference_image_key(*, process_family, definition_checksum,
                         release_digest, exposure, sca,
                         reference_product_key, science_infobits=None,
                         role=ROLE_DIFFERENCE_IMAGE):
    """The product key for one difference image.

    Inputs, in the fixed order brief D sets: the science exposure by its
    mission identity, then the reference image by its product key. The order
    is meaningful — they are different roles in the subtraction, not an
    unordered set — so it is NOT sorted.
    """
    payload = canonical_payload(
        CLASS_DIFFERENCE_IMAGE,
        process_specification(process_family, definition_checksum,
                              release_digest),
        difference_image_subject(exposure, sca),
        [science_image_input(exposure, sca, science_infobits),
         reference_image_input(reference_product_key)],
        role)
    return product_key(payload), payload


def reference_image_key(*, process_family, definition_checksum,
                        release_digest, field, fid, coadd_inputs,
                        role=ROLE_REFERENCE_IMAGE):
    """The product key for one reference image.

    `coadd_inputs` is an iterable of `(expid, sca)` or `(expid, sca,
    infobits)` tuples — the coadded science images by mission identity. They
    are put into a canonical total order here (`ordered_science_inputs`), so
    the caller's iteration order, and the overlap query's `order by dist`,
    cannot reach the digest.
    """
    records = []
    for entry in coadd_inputs:
        parts = tuple(entry)
        if len(parts) == 2:
            records.append(science_image_input(parts[0], parts[1]))
        elif len(parts) == 3:
            records.append(science_image_input(parts[0], parts[1], parts[2]))
        else:
            raise ProductIdentityError(
                f"a coadd input must be (expid, sca) or (expid, sca, "
                f"infobits); got {len(parts)} components: {parts!r}")
    if not records:
        raise ProductIdentityError(
            "a reference image's product key needs its coadd inputs; an "
            "empty list would give every reference image of this field and "
            "filter the same identity")
    payload = canonical_payload(
        CLASS_REFERENCE_IMAGE,
        process_specification(process_family, definition_checksum,
                              release_digest),
        reference_image_subject(field, fid),
        ordered_science_inputs(records),
        role)
    return product_key(payload), payload
