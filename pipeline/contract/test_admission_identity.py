"""
File:    test_admission_identity.py

Admission identity, both grains: brief H's acceptance criterion 1.

    1. "Admission identity is deterministic and grain-correct: the exposure
        grain is `dateobs` ALONE with no checksum participating, the L2 grain
        is a content key over (expid, sca) plus the source checksum, the two
        grains cannot collide, and no path, filename, bucket, run/attempt
        identifier or ingest wall-clock ever enters either payload."

**THIS FILE IS PURE, AND THAT IS DELIBERATE.** Every property below is a
property of `pipeline/repositories/admission_identity.py` — a digest over a
canonically serialized dict — and needs no database to state or to refute.
The DATABASE half (the UNIQUE constraints, the write-once triggers, the
idempotent admit path, the concurrency convergence) is criteria 2 and 3, and
lives in `test_admission_repository.py` beside this one, exactly the split
`test_alert_outbox_identity.py` documents against `alerts/test/test_identity
.py`: "the pure-digest properties are asserted where they need no database;
what needs one is everything the SCHEMA promises about those digests".

The file nonetheless lives HERE rather than in a stub-tier directory, for one
reason: this is where the admission acceptance suite is read. `conftest.py`
auto-marks everything under this package `contract`, so these are collected in
the same selection as their database siblings — but they import no `psycopg2`,
open no connection and take no `conn` fixture, so they are collectible and
runnable with no database reachable at all. A pure test that a missing
database can silently skip would be a pure test nobody notices going missing.

**WHY THE ASSERTIONS ARE OVER THE PAYLOAD AND NOT ONLY OVER THE DIGEST.**
`exposure_payload` and `l2file_payload` return the object rather than only
hashing it precisely so a test can assert over its CONTENT. "No checksum
participates at the exposure grain" is a statement about the SET OF KEYS
hashed; a test that could see only 64 hex characters could observe that two
digests differ but never that the right things went into them. The module's
own docstring makes that promise ("Returned rather than only hashed so the
acceptance suite can assert over its CONTENT"), and this file is the caller
that cashes it.

**THE DEFECT UNDER GUARD.** Admission identity today is a FILENAME BASENAME
compared in Python and switched off by an environment variable
(`db_register_socsim_files.py:92` reads `DONTCHECKALREADYINGESTED`, applied at
:892 as a linear membership test over basenames parsed out of
`l2files.filename`). Every test here is a statement that the replacement does
not repeat any part of that: not the filename as identity, not the text of a
timestamp as a proxy for the instant, and not a checksum smuggled into a grain
where no file exists.
"""

import datetime
import unittest

import pytest

from pipeline.repositories.admission_identity import (
    ALLOWED_KEYS, FORBIDDEN_KEY_PARTS, GRAIN_EXPOSURE, GRAIN_L2FILE,
    SERIALIZATION_VERSION, AdmissionIdentityError, ForbiddenAdmissionInput,
    _reject_forbidden, admission_identity, canonical_dateobs, canonical_json,
    exposure_identity, exposure_payload, l2file_identity, l2file_payload,
    normalized_checksum)

#: One instant, written six ways. Every spelling below names 2027-03-04
#: 05:06:07.089000 UTC and nothing else — that is the whole content of the
#: `canonical_dateobs` contract, and the list is the test.
INSTANT_UTC = datetime.datetime(2027, 3, 4, 5, 6, 7, 89000,
                                tzinfo=datetime.timezone.utc)

#: A valid SHA-256, and the same bytes' digest under a different tool's
#: capitalization. Two spellings, one content key.
CHECKSUM_LOWER = "9f" + "3c" * 31
CHECKSUM_UPPER = CHECKSUM_LOWER.upper()

#: A second, genuinely different checksum — for the sensitivity assertions,
#: where varying one component alone must move the identity.
OTHER_CHECKSUM = "a1" + "b2" * 31

EXPOSURE = 8_675_309
SCA = 7


def _keys_everywhere(node, trail=()):
    """Every key in a nested payload, with the path that reached it.

    The forbidden-input assertions below walk the RETURNED payload rather than
    checking its top level, for the reason `_reject_forbidden` gives for
    walking: a forbidden input reintroduced by a later edit would arrive nested
    inside a facts record, where a top-level check would not see it. The test
    that guards the guard has to look everywhere the guard looks.
    """
    found = []
    if isinstance(node, dict):
        for key, value in node.items():
            found.append((key, trail))
            found.extend(_keys_everywhere(value, trail + (key,)))
    elif isinstance(node, (list, tuple)):
        for position, value in enumerate(node):
            found.extend(_keys_everywhere(value, trail + (position,)))
    return found


def _assert_no_forbidden_key(payload, grain):
    """No key anywhere in `payload` carries a forbidden substring.

    Shared by both grains because the rule is stated at both: "Paths,
    filenames, basenames, bucket names, S3 keys, attempt/run identity and the
    ingest wall-clock are all forbidden at BOTH grains" (051's header). A
    per-grain copy of this loop would let one grain drift.
    """
    for key, trail in _keys_everywhere(payload):
        lowered = str(key).lower()
        if lowered in ALLOWED_KEYS:
            continue
        for part in FORBIDDEN_KEY_PARTS:
            assert part not in lowered, (
                f"the {grain} payload carries the key {key!r} at "
                f"{' -> '.join(str(p) for p in trail) or '<root>'}, which "
                f"contains the forbidden substring {part!r}. A filename, "
                f"path, bucket, run/attempt identifier or ingest wall-clock "
                f"is a source ADDRESS, never an identity — that conflation is "
                f"the defect this module exists to remove")


# ---------------------------------------------------------------------------
# 1. The exposure grain is `dateobs` ALONE.
# ---------------------------------------------------------------------------
def test_the_exposure_payload_carries_dateobs_and_nothing_identity_bearing():
    """The exposure grain's whole identity, asserted as a key SET.

    `exposurespk UNIQUE (dateobs)` (`006-core-tables.sql:194`) is the
    database's own natural key for an exposure, and this grain matches it
    exactly. The assertion is over the complete set of keys rather than over
    the presence of `dateobs`, because the failure this guards against is an
    ADDITION: a later edit that "helpfully" folds the field, the filter or a
    file's checksum into the exposure identity would still contain `dateobs`
    and would still produce a digest, while silently making one exposure into
    N admissions.
    """
    payload = exposure_payload(INSTANT_UTC)

    assert set(payload) == {"serialization_version", "admission_grain",
                            "dateobs"}
    assert payload["serialization_version"] == SERIALIZATION_VERSION
    assert payload["admission_grain"] == GRAIN_EXPOSURE
    assert payload["dateobs"] == "2027-03-04T05:06:07.089000Z"


def test_no_checksum_participates_in_the_exposure_grain():
    """NO CHECKSUM, stated as its own assertion over the payload's content.

    An exposure is an OBSERVATIONAL FACT, not a file: the same pointing at the
    same instant is the same exposure however many detector files carry it and
    whatever their bytes are. Ingestion is per-L2-detector-file, so a checksum
    admitted here would make ONE exposure into eighteen admissions, one per
    SCA — which is the defect, not the fix (051's header states exactly this).

    Asserted by scanning the keys for the word rather than by comparing the
    key set again above: the two tests fail for different reasons, and this
    one names the specific mistake in its own failure message.
    """
    payload = exposure_payload(INSTANT_UTC)

    for key, _trail in _keys_everywhere(payload):
        lowered = str(key).lower()
        assert "checksum" not in lowered and "digest" not in lowered, (
            f"the exposure payload carries {key!r}: no checksum participates "
            f"at the exposure grain, because an exposure is an observational "
            f"fact and not a file")

    # And the value side too — a 64-hex string anywhere in this payload would
    # be a checksum admitted under an innocent key name.
    for value in payload.values():
        text = str(value).lower()
        assert not (len(text) == 64
                    and all(c in "0123456789abcdef" for c in text))


# ---------------------------------------------------------------------------
# 2. One instant, one identity: the `canonical_dateobs` contract.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("spelling", [
    # An aware datetime — what a FITS reader that honours the header's
    # timezone hands over.
    INSTANT_UTC,
    # A NAIVE datetime, the common case: every timestamp in this pipeline is
    # UTC by construction (`dateobs timestamptz`, UTC FITS headers), so naive
    # is treated as UTC rather than refused. The module states that assumption
    # out loud rather than hiding it, and this is the case that pins it.
    datetime.datetime(2027, 3, 4, 5, 6, 7, 89000),
    # ISO-8601 with a trailing Z — the spelling `fromisoformat` refuses before
    # Python 3.11, which is why the module normalizes it before parsing rather
    # than letting the identity depend on the interpreter version.
    "2027-03-04T05:06:07.089000Z",
    # The same instant with an explicit zero offset.
    "2027-03-04T05:06:07.089000+00:00",
    # DIFFERENT MICROSECOND PADDING. `.089` and `.089000` are the same
    # fraction of a second written two ways; a str()-based identity would make
    # them two different exposures.
    "2027-03-04T05:06:07.089+00:00",
    # A NON-UTC OFFSET NAMING THE SAME INSTANT. This is the sharpest case: the
    # text shares not one character with the others, and the observation is
    # identical. Identity is over the INSTANT, never over its text.
    "2027-03-04T00:06:07.089000-05:00",
    # Surrounding whitespace, which a header parse can leave behind.
    "  2027-03-04T05:06:07.089000Z  ",
])
def test_every_spelling_of_one_instant_gives_one_identity(spelling):
    """SIX SPELLINGS, ONE ADMISSION.

    Two ingests of one observation whose readers formatted the header
    differently must produce ONE identity, or the UNIQUE constraint underneath
    admits both and the pipeline has two admissions for one exposure — the
    duplicate-admission failure rule 20 names, reintroduced through the back
    door of a formatting difference.

    Parameterized rather than looped so a failure names WHICH spelling
    diverged; a loop reports only that something did.
    """
    canonical = canonical_dateobs(INSTANT_UTC)
    assert canonical_dateobs(spelling) == canonical

    baseline, _payload = exposure_identity(INSTANT_UTC)
    identity_, payload = exposure_identity(spelling)
    assert identity_ == baseline
    assert payload["dateobs"] == canonical


def test_a_different_instant_is_a_different_exposure():
    """The other half of the same contract: one microsecond IS a difference.

    Without this, `canonical_dateobs` could satisfy every test above by
    returning a constant. Stated as its own assertion for that reason.
    """
    later = INSTANT_UTC + datetime.timedelta(microseconds=1)
    assert exposure_identity(later)[0] != exposure_identity(INSTANT_UTC)[0]


def test_a_dateobs_that_is_not_a_timestamp_is_refused_by_name():
    """Fail loud: an unparseable dateobs never yields a partial identity.

    An identity computed over a value nobody can turn back into an instant
    would be a confident claim about an admission nobody can reconstruct, and
    it would then be UNIQUE-constrained in the database, where it collides
    with the next such admission.
    """
    with pytest.raises(AdmissionIdentityError) as caught:
        canonical_dateobs("not-a-timestamp")
    assert "ISO-8601" in str(caught.value)

    with pytest.raises(AdmissionIdentityError) as caught:
        canonical_dateobs(1_772_000_000)
    assert "datetime" in str(caught.value)


# ---------------------------------------------------------------------------
# 3. The L2 grain: (expid, sca, source checksum), each component load-bearing.
# ---------------------------------------------------------------------------
def test_the_same_l2_components_give_the_same_identity():
    """Determinism at the grain where a file — and therefore a checksum —
    actually exists.

    Computed twice from separately-constructed argument sets so the test
    cannot pass by comparing an object with itself.
    """
    first, first_payload = l2file_identity(
        exposure=EXPOSURE, sca=SCA, source_checksum=CHECKSUM_LOWER)
    second, second_payload = l2file_identity(
        exposure=EXPOSURE, sca=SCA, source_checksum=CHECKSUM_LOWER)

    assert first == second
    assert first_payload == second_payload
    assert first.startswith("sha256:")
    assert len(first) == len("sha256:") + 64


def test_a_different_exposure_moves_the_l2_identity():
    """`expid` varied ALONE. One of three separate assertions, deliberately.

    Each component gets its own test rather than one test varying all three,
    because the failure modes are different and a combined test reports only
    that "something" is not participating. `exposure` is the MISSION exposure
    identifier — the survey's own name for the observation — so dropping it
    would merge every detector file with the same SCA number across the survey
    into one admission.
    """
    baseline, _ = l2file_identity(exposure=EXPOSURE, sca=SCA,
                                  source_checksum=CHECKSUM_LOWER)
    varied, _ = l2file_identity(exposure=EXPOSURE + 1, sca=SCA,
                                source_checksum=CHECKSUM_LOWER)
    assert varied != baseline


def test_a_different_sca_moves_the_l2_identity():
    """`sca` varied ALONE.

    Roman's focal plane carries eighteen SCAs and ingestion is
    per-detector-file, so an identity insensitive to `sca` would collapse all
    eighteen files of one exposure into a single admission — and the L2 grain
    would then be the exposure grain wearing a checksum.
    """
    baseline, _ = l2file_identity(exposure=EXPOSURE, sca=SCA,
                                  source_checksum=CHECKSUM_LOWER)
    varied, _ = l2file_identity(exposure=EXPOSURE, sca=SCA + 1,
                                source_checksum=CHECKSUM_LOWER)
    assert varied != baseline


def test_a_different_source_checksum_moves_the_l2_identity():
    """The CONTENT half varied alone — the reason this grain has a checksum.

    Same detector file, different bytes: a reprocessed or corrected L2 product
    is genuinely a different thing to admit, and the identity must say so. The
    repository turns this into an explicit `AdmissionConflict` rather than a
    silent re-version (`addl2file`'s `coalesce(max(version), 0) + 1`), and it
    can only do that because the two identities differ HERE first.
    """
    baseline, _ = l2file_identity(exposure=EXPOSURE, sca=SCA,
                                  source_checksum=CHECKSUM_LOWER)
    varied, _ = l2file_identity(exposure=EXPOSURE, sca=SCA,
                                source_checksum=OTHER_CHECKSUM)
    assert varied != baseline


def test_the_l2_payload_names_its_components_and_no_others():
    """The L2 grain's key set, stated whole for the same reason the exposure
    grain's is: the failure is an addition, not a removal."""
    _identity, payload = l2file_identity(
        exposure=EXPOSURE, sca=SCA, source_checksum=CHECKSUM_LOWER)

    assert set(payload) == {"serialization_version", "admission_grain",
                            "exposure", "sca", "source_checksum",
                            "checksum_algorithm"}
    assert payload["admission_grain"] == GRAIN_L2FILE
    assert payload["exposure"] == EXPOSURE
    assert payload["sca"] == SCA
    assert payload["source_checksum"] == CHECKSUM_LOWER
    assert payload["checksum_algorithm"] == "sha256"


# ---------------------------------------------------------------------------
# 4. Checksum normalization: one content key per content, and refusals.
# ---------------------------------------------------------------------------
def test_checksum_case_does_not_change_the_identity():
    """The same bytes hashed by two tools differ only in CASE.

    `sha256sum` writes lower-case hex; several S3 and FITS toolchains write
    upper. Both name the same content, so both must name the same admission —
    otherwise a change of ingest tooling silently re-admits the entire survey.
    """
    lower, lower_payload = l2file_identity(
        exposure=EXPOSURE, sca=SCA, source_checksum=CHECKSUM_LOWER)
    upper, upper_payload = l2file_identity(
        exposure=EXPOSURE, sca=SCA, source_checksum=CHECKSUM_UPPER)

    assert lower == upper
    # And the STORED spelling is the lower-case one, so the payload — which is
    # what the digest is over and what a replay would re-serialize — carries
    # one canonical form rather than whichever the caller happened to pass.
    assert lower_payload["source_checksum"] == CHECKSUM_LOWER
    assert upper_payload["source_checksum"] == CHECKSUM_LOWER


def test_a_wrong_length_checksum_is_refused():
    """A TRUNCATED CHECKSUM WOULD MAKE TWO FILES SHARE AN ADMISSION.

    This is not hypothetical in this schema: `l2files.checksum` is
    `character varying(32)` (`006-core-tables.sql:259`) and therefore truncates
    every SHA-256 it is given — the CR-8 defect, still unlanded. Admission
    identity reads the full-width value from the SOURCE and never that column,
    and this refusal is what makes reading the truncated one a loud failure
    instead of a quiet collision between every pair of files whose digests
    share a prefix.
    """
    with pytest.raises(AdmissionIdentityError) as caught:
        normalized_checksum(CHECKSUM_LOWER[:32])
    assert "64 hex characters" in str(caught.value)

    # And through the identity entry point, which is where a caller meets it.
    with pytest.raises(AdmissionIdentityError):
        l2file_identity(exposure=EXPOSURE, sca=SCA,
                        source_checksum=CHECKSUM_LOWER[:32])

    # An MD5-length value under the sha256 algorithm is the same mistake, and
    # is caught by length rather than by silently reinterpreting the algorithm.
    with pytest.raises(AdmissionIdentityError):
        normalized_checksum("d4" * 16, "sha256")
    # Declared as md5, the same 32 characters are legitimate.
    assert normalized_checksum("d4" * 16, "md5") == ("d4" * 16, "md5")


def test_a_non_hexadecimal_checksum_is_refused():
    """Right length, wrong alphabet — a base64 digest, or a truncated path.

    Length alone is not enough: a 64-character object key or a base64-encoded
    digest passes a length check and would then be hashed into an identity as
    though it were content.
    """
    with pytest.raises(AdmissionIdentityError) as caught:
        normalized_checksum("z" * 64)
    assert "hexadecimal" in str(caught.value)


def test_an_unsupported_algorithm_is_refused_by_name():
    """The algorithm travels WITH the value, and the set of them is closed.

    A bare hex string does not say how it was computed, and two algorithms'
    digests of the same bytes are different values that would otherwise look
    like a content change — so the algorithm is a hashed component, and an
    unknown one is refused rather than recorded. The database agrees:
    `admission_l2files_algorithm_ck` admits only 'sha256' and 'md5'.
    """
    with pytest.raises(AdmissionIdentityError) as caught:
        normalized_checksum(CHECKSUM_LOWER, "crc32")
    assert "algorithm" in str(caught.value)


def test_the_algorithm_is_part_of_the_identity():
    """Two algorithms over one file are two content keys, not a change.

    Constructed with two values of the right length for their own algorithms,
    so the difference under test is the ALGORITHM and not the digest width.
    """
    as_sha, _ = l2file_identity(exposure=EXPOSURE, sca=SCA,
                                source_checksum="ab" * 32,
                                checksum_algorithm="sha256")
    as_md5, _ = l2file_identity(exposure=EXPOSURE, sca=SCA,
                                source_checksum="ab" * 16,
                                checksum_algorithm="md5")
    assert as_sha != as_md5


# ---------------------------------------------------------------------------
# 5. Forbidden inputs — absent from both payloads, AND the guard proven live.
# ---------------------------------------------------------------------------
def test_no_forbidden_input_appears_in_the_exposure_payload():
    """The full forbidden list, walked over the real returned payload.

    Rule 20's defect is a FILENAME BASENAME used as an admission identity. The
    replacement must not reintroduce it under any of its aliases — uri, url,
    path, filename, basename, bucket, key, prefix — nor any execution
    identifier (run_id, attempt, batch, index) nor the ingest wall-clock
    (ingested_at, created, admitted_at), all of which would make a REPLAY of
    the same observation produce a different identity and therefore a
    duplicate admission.
    """
    _assert_no_forbidden_key(exposure_payload(INSTANT_UTC), "exposure")


def test_no_forbidden_input_appears_in_the_l2_payload():
    """The same walk at the grain where a file genuinely exists.

    This grain is the one at risk: it HAS a source object, with a bucket, a
    key and a filename sitting right beside the checksum in the caller's hand.
    The allowlist is exactly `serialization_version`, `source_checksum`,
    `checksum_algorithm` and `admission_grain`, and nothing else is admitted
    however convenient it would be.
    """
    _identity, payload = l2file_identity(
        exposure=EXPOSURE, sca=SCA, source_checksum=CHECKSUM_LOWER)
    _assert_no_forbidden_key(payload, "l2file")


@pytest.mark.parametrize("injected", [
    {"filename": "l2/roman/f158_0001_wfi07.fits"},
    {"source_path": "/mnt/ingest/f158_0001_wfi07.fits"},
    {"s3_uri": "s3://rapid-ingest/f158_0001_wfi07.fits"},
    {"source_bucket": "rapid-ingest"},
    {"object_key": "f158_0001_wfi07.fits"},
    {"run_id": "run-2027-03-04"},
    {"attempt_id": 4242},
    {"array_index": 3},
    {"ingested_at": "2027-03-04T05:06:07Z"},
    {"admitted_at": "2027-03-04T05:06:07Z"},
    # NESTED, which is the case that matters. A forbidden input reintroduced
    # by a later edit arrives inside a facts record, not at the top level —
    # which is why `_reject_forbidden` WALKS rather than checking one level,
    # and why this test injects at depth.
    {"facts": {"header": {"basename": "f158_0001_wfi07.fits"}}},
    # And inside a list, the third shape the walk has to cover.
    {"facts": [{"prefix": "roman/l2/"}]},
])
def test_the_forbidden_guard_actually_raises_when_one_is_injected(injected):
    """THE GUARD IS PROVEN LIVE, not merely proven absent-by-construction.

    The two tests above assert that today's payloads contain no forbidden key.
    That is a true statement about today's payloads and NOT a test of the
    guard: both would still pass if `_reject_forbidden` were a no-op, because
    nothing currently puts a forbidden key in. So `_reject_forbidden` is called
    DIRECTLY here with a crafted dict — the only way to observe that the guard
    refuses rather than that it was never asked.

    `stub-blind testing`'s standing rule, applied to a guard rather than to a
    double: a guard that only ever sees clean input has not been tested, and
    the guard runs on every real call precisely so that it is not a
    test-only construct.
    """
    with pytest.raises(ForbiddenAdmissionInput) as caught:
        _reject_forbidden(injected)

    # The offending key is NAMED, so the fix is one grep away rather than a
    # hunt through a nested payload.
    assert caught.value.key
    assert "forbidden admission-identity input" in str(caught.value)


def test_the_allowlisted_keys_survive_the_guard():
    """The allowlist is real, and it is the complete set of content keys.

    Without this, a guard that refused everything would pass every test above.
    `serialization_version` is the one "version" that legitimately appears —
    metadata about the canonical form, not a database row version — and
    `source_checksum` / `checksum_algorithm` / `admission_grain` are the L2
    grain's own content.
    """
    allowlisted = {key: 1 for key in ALLOWED_KEYS}
    assert _reject_forbidden(allowlisted) == allowlisted

    # And a nested allowlisted key is admitted too — the walk applies the
    # allowlist at every depth, not only at the root.
    nested = {"outer": {"serialization_version": 1}}
    assert _reject_forbidden(nested) == nested


# ---------------------------------------------------------------------------
# 6. The grains never collide.
# ---------------------------------------------------------------------------
def test_an_exposure_identity_and_an_l2_identity_never_collide():
    """NAMESPACE SEPARATION, asserted against real values of both grains.

    The two grains share a table-free namespace: both are `sha256:` digests
    over a canonical JSON payload, and both are written into columns carrying
    the same `LIKE 'sha256:%'` CHECK. If a collision were possible, one
    exposure's admission and one L2 file's admission could claim one identity
    — and since `admission_exposures_identity_uq` and
    `admission_l2files_identity_uq` are separate constraints on separate
    tables, nothing in the database would notice.

    `admission_grain` being a HASHED component is what makes the collision
    impossible rather than merely unlikely, which is why the next assertion
    checks it is inside the payload and not only alongside it.
    """
    exposure_id, exposure_side = exposure_identity(INSTANT_UTC)
    l2_id, l2_side = l2file_identity(exposure=EXPOSURE, sca=SCA,
                                     source_checksum=CHECKSUM_LOWER)

    assert exposure_id != l2_id
    assert exposure_side != l2_side


def test_the_grain_is_inside_the_hashed_payload_at_both_grains():
    """The separator is HASHED, not a label beside the hash.

    Demonstrated the only way it can be: recompute each identity from the same
    payload with the grain swapped, and observe that the digest moves. A grain
    recorded only in a column would leave both payloads identical whenever
    their other components coincided, and the two digests would then be equal
    — the collision the previous test asserts is impossible.
    """
    _exposure_id, exposure_side = exposure_identity(INSTANT_UTC)
    _l2_id, l2_side = l2file_identity(exposure=EXPOSURE, sca=SCA,
                                      source_checksum=CHECKSUM_LOWER)

    assert exposure_side["admission_grain"] == GRAIN_EXPOSURE
    assert l2_side["admission_grain"] == GRAIN_L2FILE

    for payload, grain in ((exposure_side, GRAIN_EXPOSURE),
                           (l2_side, GRAIN_L2FILE)):
        swapped = dict(payload, admission_grain="some-other-grain")
        assert admission_identity(swapped) != admission_identity(payload), (
            f"the {grain} identity did not move when admission_grain changed, "
            f"so the grain is not participating in the digest and the two "
            f"grains are separated by convention rather than by construction")


def test_the_serialization_version_is_inside_the_hashed_payload():
    """A future change to the canonical form must be VISIBLE, not silent.

    Bumping `SERIALIZATION_VERSION` changes every admission identity by
    design. That is only true if the version is hashed — recorded beside the
    digest instead, a canonical-form change would silently collide two
    spellings of the same content under one identity while the stored rows
    claimed a version they were not computed under.
    """
    _identity, payload = exposure_identity(INSTANT_UTC)
    bumped = dict(payload, serialization_version=SERIALIZATION_VERSION + 1)
    assert admission_identity(bumped) != admission_identity(payload)


def test_the_canonical_serialization_is_construction_order_independent():
    """Two dicts differing only in insertion order serialize identically.

    `sort_keys=True` and explicit `separators` are what make the digest a
    function of the CONTENT rather than of the order a caller happened to
    build the dict in — and Python preserves insertion order, so without
    sorting these two would be different bytes and therefore two admissions
    for one observation.
    """
    _identity, payload = l2file_identity(
        exposure=EXPOSURE, sca=SCA, source_checksum=CHECKSUM_LOWER)
    reversed_order = {key: payload[key] for key in reversed(list(payload))}

    assert canonical_json(reversed_order) == canonical_json(payload)
    assert admission_identity(reversed_order) == admission_identity(payload)
    # No whitespace anywhere: a Python version whose json defaults changed
    # would otherwise silently rewrite every identity in the database.
    assert " " not in canonical_json(payload)


# ---------------------------------------------------------------------------
# 7. Missing components fail loud, and name themselves.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kwargs, component", [
    ({"exposure": None, "sca": SCA, "source_checksum": CHECKSUM_LOWER},
     "exposure"),
    ({"exposure": EXPOSURE, "sca": None, "source_checksum": CHECKSUM_LOWER},
     "sca"),
    ({"exposure": EXPOSURE, "sca": SCA, "source_checksum": None},
     "source_checksum"),
    ({"exposure": EXPOSURE, "sca": SCA, "source_checksum": "   "},
     "source_checksum"),
])
def test_a_missing_l2_component_raises_naming_the_component(kwargs, component):
    """EVERY COMPONENT IS REQUIRED, and the failure says which one is absent.

    A partial identity is worse than no identity: it computes, it looks like a
    digest, and it goes into a UNIQUE-constrained column where it collides
    with the next admission missing the same component. Naming the component
    is what turns that into a one-line fix instead of a hunt.

    An empty-or-whitespace string counts as absent, because an ingest that
    read a missing FITS card usually produces `''` rather than `None`.
    """
    with pytest.raises(AdmissionIdentityError) as caught:
        l2file_identity(**kwargs)
    assert component in str(caught.value)


def test_a_missing_dateobs_raises_naming_dateobs():
    """The exposure grain's single component, absent.

    Its own test rather than a parameter above, because this grain has exactly
    one component and its absence is therefore the absence of the whole
    identity — the case where a fallback would be most tempting and most
    damaging.
    """
    for absent in (None, "", "   "):
        with pytest.raises(AdmissionIdentityError) as caught:
            exposure_identity(absent)
        assert "dateobs" in str(caught.value)


def test_the_error_type_is_a_valueerror_so_callers_can_catch_broadly():
    """`AdmissionIdentityError` is a `ValueError`, and the forbidden-input
    error is one of it.

    The hierarchy is load-bearing at the call site: an ingest catching
    `AdmissionIdentityError` catches the forbidden-input case too — a design
    defect it should fail on rather than skip past — while a caller catching
    only `ForbiddenAdmissionInput` has deliberately narrowed to the design
    defect alone.
    """
    assert issubclass(AdmissionIdentityError, ValueError)
    assert issubclass(ForbiddenAdmissionInput, AdmissionIdentityError)


if __name__ == "__main__":
    unittest.main()
