"""
File:    admission_identity.py

Deterministic admission identity — the two grains rule 20 needs, and the
canonical serialization they are digests over.

**What rule 20 asks for, verbatim:** "The admission source is durable and
replayable; admission is idempotent and a repeated observation returns its
existing admission."

Today admission identity is a FILENAME BASENAME, compared in Python, and
switched off by an environment variable: `db_register_socsim_files.py:92`
reads `DONTCHECKALREADYINGESTED` and `:892` applies the resulting set as a
linear membership test over basenames parsed out of `l2files.filename`. Two
concurrent ingests both pass that test. This module is the replacement, and
after it a filename is a source ADDRESS and never an identity.

**THE TWO GRAINS ARE DEFINED SEPARATELY AND DIFFERENTLY**, because ingestion
is per-L2-detector-file: each file is downloaded, `register_exposure` is
called, then the `(expid, sca)` L2 row is registered. There is no
exposure-level "admitted file" whose checksum could enter an exposure
identity.

  EXPOSURE GRAIN — identity is `dateobs` ALONE. It matches the database's own
  natural key, `exposurespk UNIQUE (dateobs)`
  (`006-core-tables.sql:194`), and NO CHECKSUM PARTICIPATES. An exposure is an
  observational fact, not a file: the same pointing at the same instant is the
  same exposure however many detector files carry it and whatever their bytes
  are. Putting a checksum in would make one exposure into N admissions, one
  per detector file — which is the defect, not the fix.

  L2 GRAIN — identity is a CONTENT KEY over `(expid, sca)` plus the source
  content checksum. This is the grain where a file, and therefore a checksum,
  actually exists, and where `l2filespk UNIQUE (expid, sca, version)` leaves a
  hole: uniqueness INCLUDES the version, so `addl2file`'s
  `coalesce(max(version), 0) + 1` (`008-functions.sql:438-446`) sidesteps the
  constraint by construction and a re-ingest mints a new admission row.

**THE GRAINS ARE NAMESPACE-SEPARATED AND CANNOT COLLIDE.** `admission_grain`
is a hashed component of every payload, so an exposure identity and an L2
identity are different digests even if every other component coincided. That
is asserted directly by the acceptance suite rather than argued here.

**Forbidden inputs, at BOTH grains.** Paths, filenames, basenames, bucket
names, S3 keys, attempt/run identity and the ingest wall-clock are refused by
`_reject_forbidden` walking the payload — the same guard
`pipeline/registration/identity.py` established for rule 10, and for the same
reason: the guard runs on every real call, not only in tests, because a guard
only tests have is a guard production does not have.

`dateobs` is serialized through `canonical_dateobs` rather than by
`str()`. A timestamp's *text* has many spellings for one instant — offset
vs `Z`, microsecond padding, naive vs aware — and identity must be over the
INSTANT. Two ingests of one observation whose readers formatted the header
differently must produce one identity.

**Why a versioned canonical serialization.** The digest is over JSON with
sorted keys and fixed separators, prefixed by a serialization version that is
IN the hashed payload rather than beside it, so a future change to the
canonical form produces different identities deliberately and visibly instead
of silently colliding two spellings of the same content.

**Fail-loud, never a partial identity.** Every component is required. A
missing one raises `AdmissionIdentityError` naming it: an identity computed
over an absent component would be a confident claim about an admission nobody
can reconstruct, and it would be UNIQUE-constrained in the database, where it
would collide with the next such admission.
"""

import datetime
import hashlib
import json

#: The canonical-serialization version, hashed as part of the payload.
#: Bumping it changes every admission identity by design.
SERIALIZATION_VERSION = 1

#: The two grains. These strings are hashed, so they are the namespace
#: separation — not a label on it.
GRAIN_EXPOSURE = "exposure"
GRAIN_L2FILE = "l2file"

#: Substrings that must never appear as a KEY anywhere in a canonical
#: admission payload. A filename is not an identity; that conflation is the
#: defect this module exists to remove, so it is refused structurally rather
#: than by review.
FORBIDDEN_KEY_PARTS = (
    "uri", "url", "path", "filename", "file_name", "basename", "base_name",
    "bucket", "key", "prefix", "object_key",
    "run_id", "attempt_id", "attempt", "batch", "array_index", "index",
    "ingested_at", "ingest_time", "wall_clock", "created", "admitted_at",
    "rid", "expid_surrogate", "version",
)

#: Key names containing a forbidden substring that are nonetheless legitimate,
#: checked before `FORBIDDEN_KEY_PARTS`. Each is here for a stated reason:
#:   * `serialization_version` — the canonical form's own version, the one
#:     "version" that legitimately appears (metadata about the serialization,
#:     not a database row version);
#:   * `source_checksum` / `checksum_algorithm` — the L2 grain's content key.
#:     Neither contains a forbidden substring, but both are listed so the
#:     allowlist reads as the complete set of content-bearing keys;
#:   * `admission_grain` — contains "index"? no; listed for the same
#:     completeness reason as the two above.
ALLOWED_KEYS = frozenset({
    "serialization_version", "source_checksum", "checksum_algorithm",
    "admission_grain",
})


class AdmissionIdentityError(ValueError):
    """An admission identity could not be computed, and why.

    Raised rather than returning a sentinel because every caller is writing a
    UNIQUE-constrained database row: a fallback identity would be a real row
    claiming an identity it does not have.
    """


class ForbiddenAdmissionInput(AdmissionIdentityError):
    """A canonical payload carried a forbidden identity input.

    Its own type because it is a DESIGN defect rather than missing data: it
    means code somewhere put a path, a filename, a bucket or an execution
    identifier into an admission identity, which is the exact failure rule 20
    names. It names the offending key so the fix is one grep away.
    """

    def __init__(self, key, trail=()):
        location = " -> ".join(str(part) for part in trail) or "<root>"
        super().__init__(
            f"the key {key!r} at {location} is a forbidden admission-identity "
            f"input: admission identity is never derived from paths, URIs, "
            f"filenames, basenames, bucket names, S3 keys, run/attempt/Batch "
            f"identifiers, or the ingest wall-clock (rule 20). A filename is "
            f"a source address, not an identity — that conflation is the "
            f"defect this guard exists to prevent.")
        self.key = key


def _require(value, name):
    """One required identity component, or a named failure."""
    if value is None or (isinstance(value, str) and not value.strip()):
        raise AdmissionIdentityError(
            f"the admission identity component {name!r} is absent; an "
            f"identity computed over an absent component would be a confident "
            f"claim about an admission nobody can reconstruct")
    return value


def _reject_forbidden(node, trail=()):
    """Walk a canonical payload, refusing any forbidden key.

    Walks rather than checking the top level: the forbidden inputs are exactly
    the ones reintroduced nested inside a facts record, where a top-level
    check would not see them.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            lowered = str(key).lower()
            if lowered not in ALLOWED_KEYS:
                for part in FORBIDDEN_KEY_PARTS:
                    if part in lowered:
                        raise ForbiddenAdmissionInput(key, trail)
            _reject_forbidden(value, trail + (key,))
    elif isinstance(node, (list, tuple)):
        for position, value in enumerate(node):
            _reject_forbidden(value, trail + (position,))
    return node


def canonical_dateobs(dateobs):
    """One instant, one spelling.

    THE IDENTITY IS OVER THE INSTANT, NOT OVER ITS TEXT. A FITS `DATE-OBS`
    reaches this code as a `datetime` from one reader and as a string from
    another, with or without an offset, with any microsecond padding. All of
    those name the same observation and must produce the same identity, so
    this normalizes to UTC and formats one way: `YYYY-MM-DDTHH:MM:SS.ffffffZ`.

    A NAIVE DATETIME IS TREATED AS UTC AND NOT REFUSED. Every timestamp in
    this pipeline is UTC by construction (`dateobs timestamptz`, and the
    ingest scripts read UTC FITS headers), and refusing naive input would
    refuse the common case for a distinction the data does not carry. The
    assumption is stated here rather than hidden.
    """
    value = _require(dateobs, "dateobs")
    if isinstance(value, str):
        text = value.strip()
        # `fromisoformat` accepts a trailing Z only from 3.11; normalize it
        # first so the parse is the same on every supported interpreter.
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            value = datetime.datetime.fromisoformat(text)
        except ValueError as exc:
            raise AdmissionIdentityError(
                f"dateobs {dateobs!r} is not an ISO-8601 timestamp and cannot "
                f"be canonicalized to an instant") from exc
    if not isinstance(value, datetime.datetime):
        raise AdmissionIdentityError(
            f"dateobs must be a datetime or an ISO-8601 string, not "
            f"{type(dateobs).__name__}")
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    value = value.astimezone(datetime.timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def normalized_checksum(checksum, algorithm="sha256"):
    """A content checksum in one spelling, with its algorithm.

    Lower-cased hex, because the same bytes hashed by two tools differ only in
    case and must not differ in identity. The algorithm travels WITH the value
    — a bare hex string does not say how it was computed, and two algorithms'
    digests of the same bytes are different values that would otherwise look
    like a content change.

    NOT VALIDATED AGAINST `l2files.checksum`'s WIDTH ON PURPOSE. That column
    is `character varying(32)` (`006-core-tables.sql:259`) and therefore
    truncates every SHA-256 it is given — the CR-8 defect, still unlanded.
    Admission identity reads the full-width value from the source, never that
    column.
    """
    value = str(_require(checksum, "source_checksum")).strip().lower()
    algo = str(_require(algorithm, "checksum_algorithm")).strip().lower()
    if algo not in ("sha256", "md5"):
        raise AdmissionIdentityError(
            f"unsupported checksum algorithm {algorithm!r}; admission "
            f"identity records the algorithm alongside the digest so two "
            f"algorithms' values cannot be mistaken for a content change")
    expected = {"sha256": 64, "md5": 32}[algo]
    if len(value) != expected:
        raise AdmissionIdentityError(
            f"a {algo} checksum is {expected} hex characters; got "
            f"{len(value)} ({value!r}). A truncated checksum would make two "
            f"different files share an admission identity")
    if any(character not in "0123456789abcdef" for character in value):
        raise AdmissionIdentityError(
            f"checksum {value!r} is not hexadecimal")
    return value, algo


def exposure_payload(dateobs):
    """The exact object an exposure admission identity is a digest of.

    ONE COMPONENT, DELIBERATELY. `dateobs` alone is the database's own natural
    key and the whole of this grain's identity. Returned rather than only
    hashed so the acceptance suite can assert over its CONTENT — "no checksum
    participates at the exposure grain" is a property of this object, and a
    test that could only see the hex digest could not check it.
    """
    payload = {
        "serialization_version": SERIALIZATION_VERSION,
        "admission_grain": GRAIN_EXPOSURE,
        "dateobs": canonical_dateobs(dateobs),
    }
    return _reject_forbidden(payload)


def l2file_payload(exposure, sca, source_checksum, checksum_algorithm="sha256"):
    """The exact object an L2 admission identity is a digest of.

    `exposure` is the MISSION exposure identifier (`expid`), the survey's own
    name for the observation — not a database row id and not a date ordinal.
    Together with `sca` it names the detector file; the checksum names its
    content. Same `(expid, sca, checksum)` is the same admission; a different
    checksum for the same `(expid, sca)` is a CONFLICT, refused by the
    repository rather than silently re-versioned.
    """
    digest, algorithm = normalized_checksum(source_checksum,
                                            checksum_algorithm)
    payload = {
        "serialization_version": SERIALIZATION_VERSION,
        "admission_grain": GRAIN_L2FILE,
        "exposure": int(_require(exposure, "exposure")),
        "sca": int(_require(sca, "sca")),
        "source_checksum": digest,
        "checksum_algorithm": algorithm,
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


def admission_identity(payload):
    """The identity: `sha256:<64 hex>` over the canonical serialization.

    Prefixed with its algorithm, the same way the tree's other content digests
    are, so a stored identity says how it was computed and a future algorithm
    change is visible in the value rather than inferred from its length. The
    database's CHECK constraints assert that prefix.
    """
    encoded = canonical_json(payload).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def exposure_identity(dateobs):
    """The admission identity for one exposure, with its payload."""
    payload = exposure_payload(dateobs)
    return admission_identity(payload), payload


def l2file_identity(*, exposure, sca, source_checksum,
                    checksum_algorithm="sha256"):
    """The admission identity for one L2 detector file, with its payload."""
    payload = l2file_payload(exposure, sca, source_checksum,
                             checksum_algorithm)
    return admission_identity(payload), payload
