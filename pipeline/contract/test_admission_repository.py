"""
File:    test_admission_repository.py

Admission as an INVARIANT rather than a convention: brief H's acceptance
criteria 2 and 3, over real SQL against DRAFT migration 051.

    2. "Admission is idempotent — a repeated observation returns its existing
        admission, unchanged, and a repeat that would REDEFINE it is refused
        with both values named."
    3. "Two concurrent admissions of one observation converge: one inserts,
        the other receives the existing admission, and neither surfaces a
        unique violation to its caller."

**THESE SKIP WHERE DRAFT 051 IS ABSENT AND RUN WHERE IT IS APPLIED.**
`migrations-draft/051-admission-identity-and-release.sql` is a proposed change
request against `rapid_systems`, not part of the authoritative stream, so CI —
which builds its database from that stream alone — skips this file, while the
rapid-admin acceptance run (base + drafts) runs every test in it for real. The
skip is decided by `fixture.has_table` PROBING the catalog rather than by
catching a failure, which is what keeps "this schema is not deployed" apart
from "the query is wrong": conflating those two turns a broken test into a
silent pass.

**THE THREE DEFECTS UNDER GUARD**, each named in the test that guards it and
each read out of this stream's own SQL rather than assumed:

  * `addexposure` IS SELECT-THEN-INSERT (`008-functions.sql:290-293`): it
    reads `select expid into expid__ from Exposures where dateobs = dateobs_`
    and then conditionally inserts. Two concurrent admissions of one
    observation both read NULL and both insert; the loser takes a unique
    violation on `exposurespk` instead of RECEIVING THE EXISTING ADMISSION.
    Guarded by the concurrency test below, which is the most important test
    in this file.

  * `addexposure` OVERWRITES ON A REPEAT (`008-functions.sql:331-345`): its
    `else` branch updates every field INCLUDING `created = now()`, so
    re-admitting an observation destroys the original ingest timestamp
    unrecoverably. Guarded by the `admitted_at` idempotence assertion and,
    structurally, by the write-once trigger test.

  * `addl2file` COMPUTES `coalesce(max(version), 0) + 1`
    (`008-functions.sql:438-446`) against a uniqueness that INCLUDES the
    version (`l2filespk UNIQUE (expid, sca, version)`), so the max+1
    deliberately sidesteps the constraint and a re-run of an ingest MINTS A
    NEW ADMISSION ROW for the same detector file. Guarded by the L2
    idempotence test, which counts rows for the `(expid, sca)` pair rather
    than trusting the returned identity.

**FIXTURE HONESTY.** Every test builds its own rows under `fixture.RUN_TAG`
and deletes only what it created, so a re-run is safe and two runs may overlap
on one database. Nothing truncates. The `exposures` rows are minted with
run-unique `dateobs` values because `exposurespk UNIQUE (dateobs)` is exactly
the constraint these tests provoke — a shared literal would collide with the
PREVIOUS run's leftovers and the failure would look precisely like the defect
under test.
"""

import datetime
import json
import threading
import unittest
import uuid

import psycopg2
import pytest

from pipeline.contract import fixture
from pipeline.repositories.admission import (
    AdmissionConflict, AdmissionError, AdmissionRepository,
    AdmissionSchemaAbsent, ManifestNotSealed, ReleaseScopeConflict)

#: A run-unique base instant for the exposures these tests admit.
#:
#: `exposurespk UNIQUE (dateobs)` makes `dateobs` the exposure table's natural
#: key, and `admission_exposures_expid_uq` makes the sidecar one-per-exposure.
#: Two runs of this suite against one database therefore collide on a
#: hard-coded timestamp — so the base is derived from `RUN_TAG` the same way
#: `test_alert_outbox_identity.FIELD_BASE` derives its sky fields, and each
#: test takes its own offset from it.
INSTANT_BASE = (datetime.datetime(2031, 1, 1, tzinfo=datetime.timezone.utc)
                + datetime.timedelta(
                    seconds=int(fixture.RUN_TAG[:6], 16) % 1_000_000))

#: A run-unique sky field, for the same reason.
FIELD_BASE = 800_000 + (int(fixture.RUN_TAG[6:12], 16) % 100_000)


def _checksum(marker):
    """A well-formed, run-unique SHA-256-shaped digest.

    Must satisfy `normalized_checksum` (64 lower-case hex) AND
    `admission_l2files_checksum_ck` (length between 32 and 128). A literal
    would collide across runs on `admission_l2files_identity_uq`, and the
    failure would arrive at the wrong assertion.
    """
    body = (str(marker) + fixture.RUN_TAG + "0" * 64)
    body = "".join(c if c in "0123456789abcdef" else "c" for c in body.lower())
    return body[:64]


# ---------------------------------------------------------------------------
# The draft gate, and the per-test row bookkeeping.
# ---------------------------------------------------------------------------
@pytest.fixture
def admission_db(conn):
    """A repository over DRAFT 051, a registered release, or a clean skip.

    Yields `(conn, repository, release_identity, cleanup)`. `cleanup` is a
    list the test appends its own row keys to; teardown deletes exactly those
    and nothing else — fixture honesty, so a failure leaves the offending rows
    behind for inspection while a re-run still starts clean.

    The release is registered per test and per run: `admission_releases` is the
    registry a pointer and every admission FK into, and a shared identity would
    make one test's teardown delete another's parent.
    """
    for table in ("admission_exposures", "admission_l2files",
                  "admission_manifests", "admission_release_pointer"):
        if not fixture.has_table(conn, table):
            pytest.skip(
                f"DRAFT 051 is not applied (no {table}); this suite runs on "
                f"rapid-admin where base + drafts are applied, and CI builds "
                f"from the authoritative stream alone")

    repository = AdmissionRepository(conn)
    release = f"admit-{fixture.RUN_TAG}-{uuid.uuid4().hex[:8]}"
    repository.register_release(release, manifest_uri=None,
                                manifest_checksum=None)
    conn.commit()

    cleanup = {"manifests": [], "expids": [], "rids": []}
    yield conn, repository, release, cleanup

    # TEARDOWN DELETES IN FK ORDER, and only this test's rows. The admission
    # sidecars go before their parents; the parents (`l2files`, `exposures`)
    # are left in place because other suites' fixtures reuse them and because
    # deleting them is not this test's business — only the admission records
    # are.
    execute = fixture.executor(conn)
    try:
        if cleanup["rids"]:
            execute("DELETE FROM admission_l2files WHERE rid = ANY(%s)",
                    [cleanup["rids"]])
        if cleanup["expids"]:
            execute("DELETE FROM admission_exposures WHERE expid = ANY(%s)",
                    [cleanup["expids"]])
        if cleanup["manifests"]:
            execute("DELETE FROM admission_manifest_entries"
                    " WHERE manifest_id = ANY(%s)", [cleanup["manifests"]])
            execute("DELETE FROM admission_manifests"
                    " WHERE manifest_id = ANY(%s)", [cleanup["manifests"]])
        execute("DELETE FROM admission_release_pointer"
                " WHERE release_identity = %s", [release])
        execute("DELETE FROM admission_releases WHERE release_identity = %s",
                [release])
        conn.commit()
    except Exception:                                        # noqa: BLE001
        conn.rollback()


def _make_exposure(conn, offset, field=None):
    """One real `exposures` row, returning `(expid, dateobs)`.

    Inserted directly rather than through a fixture helper because the table is
    small and every column the admission path reads is named here: `dateobs` IS
    the exposure grain's whole identity, so a fixture that generated it out of
    sight would hide the one value under test. `field`, `fid`, `exptime`,
    `mjdobs`, `hp6` and `hp9` are the table's other NOT NULLs, and `fid` is
    read from the seeded `filters` catalogue rather than invented — writing a
    filter row would put fixture data in a table the pipeline reads as
    reference data.

    `mjdobs` moves with `dateobs` for the same reason `_diffimage_parents`
    does it: an exposure whose MJD disagrees with its dateobs is a row no
    pipeline would ever write, and a fixture that writes one is testing a
    schema nobody deploys.
    """
    dateobs = INSTANT_BASE + datetime.timedelta(seconds=offset)
    mjdobs = 62501.0 + offset / 86400.0
    with conn.cursor() as cur:
        cur.execute("SELECT fid FROM filters ORDER BY fid LIMIT 1")
        row = cur.fetchone()
        if row is None:
            raise AssertionError(
                "no rows in `filters`; 009-seed-data.sql seeds them, so an "
                "empty table means the stream was not fully applied")
        fid = row[0]
        cur.execute(
            "INSERT INTO exposures (dateobs, field, fid, exptime, mjdobs,"
            "                       hp6, hp9)"
            " VALUES (%s, %s, %s, 100.0, %s, 1, 1) RETURNING expid",
            [dateobs, FIELD_BASE + offset if field is None else field,
             fid, mjdobs])
        expid = cur.fetchone()[0]
    return expid, dateobs


def _make_l2file(conn, expid, dateobs, sca, tag):
    """One real `l2files` row for `(expid, sca)`, returning its `rid`.

    `admission_l2files.rid` carries an FK to `l2files(rid)`, so the L2
    admission tests need a real parent — the sidecar records an admission OF a
    row, and one without a parent would be an admission of nothing.

    Built through `fixture._insert_filling_required` for the reason that helper
    documents: `l2files` has around fifty NOT NULL columns (the full SIP
    distortion solution, the ctype/cunit pairs, forty-odd polynomial
    coefficients) and none of them is read by anything here. Naming them by
    hand produces a fixture that is mostly noise and that breaks whenever the
    schema gains a column. Only the columns these ASSERTIONS depend on are
    stated: the grain (`expid`, `sca`), the version dimension `addl2file`
    abuses, and the filename/checksum pair whose conflation into an identity is
    the defect under repair.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT fid, field FROM exposures WHERE expid = %s",
                    [expid])
        fid, field = cur.fetchone()
        cur.execute("SELECT coalesce(max(version), 0) + 1 FROM l2files"
                    " WHERE expid = %s AND sca = %s", [expid, sca])
        version = cur.fetchone()[0]
    return fixture._insert_filling_required(
        conn, "l2files", "rid",
        {"expid": expid, "sca": sca, "version": version, "vbest": 1,
         "field": field, "fid": fid, "dateobs": dateobs,
         # `l2files.checksum` is varchar(32) and TRUNCATES every SHA-256 given
         # to it (CR-8, unlanded). The value written here is deliberately short
         # so this fixture does not depend on that defect either way; admission
         # identity reads its own full-width `source_checksum` and never this
         # column.
         "filename": f"l2/{fixture.RUN_TAG}/{tag}.fits",
         "checksum": tag[:8]})


def _facts(**overrides):
    """The parsed admission facts one ingest would record.

    Deliberately plain observational values: `admitted_facts` exists so a
    replay reconstructs the database row FROM RECORDED FACTS rather than by
    re-parsing source bytes that may no longer exist, and a fact that was a
    path or a run id would be the forbidden-input defect wearing a different
    hat.
    """
    facts = {"field": FIELD_BASE, "exptime": 100.0, "hp6": 1, "hp9": 1}
    facts.update(overrides)
    return facts


def _sealed_manifest(repository, conn, release, cleanup, key_suffix,
                     entries=1):
    """An opened, filled and SEALED manifest — the state an admission may cite.

    Sealing is the LAST write of the enumeration, which is the crash-ordering
    guarantee rather than a convenience: a crash anywhere before it leaves an
    explicitly UNSEALED manifest, which `admit_*` refuses to cite, so a partial
    source can never be mistaken for a whole one.
    """
    manifest = repository.open_manifest(
        manifest_key=f"manifest-{fixture.RUN_TAG}-{key_suffix}",
        source_scope=f"scope-{fixture.RUN_TAG}",
        release_identity=release, byte_custody="external-versioned")
    cleanup["manifests"].append(manifest.manifest_id)
    for index in range(entries):
        repository.add_manifest_entry(
            manifest.manifest_id, source_bucket="rapid-ingest-contract",
            source_key=f"{fixture.RUN_TAG}/{key_suffix}/{index}.fits",
            source_checksum=_checksum(f"{key_suffix}{index}"),
            source_version_id=f"v-{fixture.RUN_TAG}-{index}")
    sealed = repository.seal_manifest(manifest.manifest_id)
    conn.commit()
    return manifest.manifest_id, sealed


# ===========================================================================
# 1. IDEMPOTENCE — a repeated observation returns its existing admission.
# ===========================================================================
def test_admitting_one_exposure_twice_returns_the_same_admission(admission_db):
    """THE REPEAT RETURNS; IT DOES NOT MUTATE. Rule 20, asserted whole.

    Four things are checked together because a defect can break any one of
    them while leaving the others intact:

      * the same `admission_identity` — the digest is deterministic;
      * the same `admission_id` — the SURROGATE, which is what proves no
        second row was minted; two rows would carry two ids while agreeing on
        everything a caller reads;
      * `created` False on the second call — the repository reports that it
        received an existing admission rather than making one;
      * `admitted_at` EXACTLY UNCHANGED — captured after the first admit and
        compared for equality after the second.

    That last one is the direct guard on `addexposure`'s `else` branch
    (`008-functions.sql:331-345`), which updates every field INCLUDING
    `created = now()` on every repeat and destroys the original ingest
    timestamp unrecoverably. Compared with `==` and not "within a second":
    the defect it guards against writes a NEW `now()`, and a tolerant
    comparison is exactly how that goes unnoticed on a fast machine.
    """
    conn, repository, release, cleanup = admission_db
    expid, dateobs = _make_exposure(conn, offset=1)
    cleanup["expids"].append(expid)
    conn.commit()

    first = repository.admit_exposure(
        dateobs=dateobs, expid=expid, facts=_facts(),
        release_identity=release)
    conn.commit()
    assert first.created is True
    assert first.admission_identity.startswith("sha256:")

    second = repository.admit_exposure(
        dateobs=dateobs, expid=expid, facts=_facts(),
        release_identity=release)
    conn.commit()

    assert second.admission_identity == first.admission_identity
    assert second.admission_id == first.admission_id
    assert second.created is False
    assert second.admitted_at == first.admitted_at, (
        "admitted_at moved on a repeat — this is addexposure's "
        "`created = now()` overwrite, which destroys the original ingest "
        "timestamp unrecoverably")

    # AND NO DUPLICATE ROW. The returned values agreeing is necessary but not
    # sufficient: a second row carrying the same identity would be refused by
    # `admission_exposures_identity_uq`, but a second row under a DIFFERENT
    # identity for the same exposure would not be — which is what
    # `admission_exposures_expid_uq` exists for, and what this counts.
    rows = fixture.executor(conn)(
        "SELECT count(*) FROM admission_exposures WHERE expid = %s", [expid])
    assert rows[0][0] == 1


def test_a_differently_spelled_dateobs_is_the_same_admission(admission_db):
    """The canonicalization contract, where it actually matters.

    `test_admission_identity.py` proves the digest is stable across spellings
    with no database in sight. This proves the consequence: an ingest whose
    reader hands over an ISO STRING rather than a `datetime` — the difference
    between two FITS libraries, or between a replay and the original run —
    receives the EXISTING admission rather than minting a second one.

    A test of the pure function alone would leave the repository free to
    stringify `dateobs` itself before hashing, and that defect would only ever
    appear here.
    """
    conn, repository, release, cleanup = admission_db
    expid, dateobs = _make_exposure(conn, offset=2)
    cleanup["expids"].append(expid)
    conn.commit()

    first = repository.admit_exposure(
        dateobs=dateobs, expid=expid, facts=_facts(),
        release_identity=release)
    conn.commit()

    # The same instant, written as an ISO-8601 string with a trailing Z.
    as_text = dateobs.astimezone(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f") + "Z"
    second = repository.admit_exposure(
        dateobs=as_text, expid=expid, facts=_facts(),
        release_identity=release)
    conn.commit()

    assert second.admission_id == first.admission_id
    assert second.created is False
    assert second.admitted_at == first.admitted_at


def test_admitting_one_l2_file_twice_does_not_re_version(admission_db):
    """`addl2file`'s `max(version) + 1`, asserted as ABSENT.

    This is the deeper of the two defects. `l2filespk UNIQUE (expid, sca,
    version)` puts the version INSIDE the uniqueness, and `addl2file` computes
    `coalesce(max(version), 0) + 1` (`008-functions.sql:438-446`) — so the
    max+1 sidesteps the constraint by construction and re-running an ingest for
    the same L2 file MINTS A NEW ADMISSION ROW every time. There is no
    `(expid, sca)`-level natural key and no content uniqueness anywhere in the
    legacy path.

    051's `admission_l2files_grain_uq UNIQUE (expid, sca)` is the natural key
    that grain has never had, and this test asserts it end to end: same
    components twice, one row, one surrogate, one unmoved `admitted_at`.

    THE ROW COUNT IS THE LOAD-BEARING ASSERTION. A re-versioning defect returns
    a perfectly plausible `Admission` — new id, new identity, no error — so
    only counting rows for the `(expid, sca)` pair can see it.
    """
    conn, repository, release, cleanup = admission_db
    expid, dateobs = _make_exposure(conn, offset=3)
    cleanup["expids"].append(expid)
    rid = _make_l2file(conn, expid, dateobs, sca=4, tag=uuid.uuid4().hex[:8])
    cleanup["rids"].append(rid)
    conn.commit()

    digest = _checksum("l2a")
    first = repository.admit_l2file(
        exposure=expid, sca=4, source_checksum=digest, rid=rid,
        facts=_facts(sca=4), release_identity=release)
    conn.commit()
    assert first.created is True

    second = repository.admit_l2file(
        exposure=expid, sca=4, source_checksum=digest, rid=rid,
        facts=_facts(sca=4), release_identity=release)
    conn.commit()

    assert second.admission_identity == first.admission_identity
    assert second.admission_id == first.admission_id
    assert second.created is False
    assert second.admitted_at == first.admitted_at

    rows = fixture.executor(conn)(
        "SELECT count(*) FROM admission_l2files WHERE expid = %s AND sca = %s",
        [expid, 4])
    assert rows[0][0] == 1, (
        "the L2 grain re-versioned: this is addl2file's "
        "coalesce(max(version), 0) + 1 minting a second admission row for one "
        "detector file, which is the defect 051's (expid, sca) UNIQUE removes")


def test_the_checksum_case_does_not_split_one_l2_admission(admission_db):
    """The same content, spelled by two tools, is one admission IN THE TABLE.

    The pure test proves the digest is case-insensitive. This proves the
    repository normalizes BEFORE it reaches the database — an upper-case
    checksum that reached `admission_l2files.source_checksum` unmodified would
    also fail the `row[4] != digest` comparison in `admit_l2file` and surface
    as a spurious `AdmissionConflict` on a re-ingest, which is a far more
    confusing failure than a duplicate.
    """
    conn, repository, release, cleanup = admission_db
    expid, dateobs = _make_exposure(conn, offset=4)
    cleanup["expids"].append(expid)
    rid = _make_l2file(conn, expid, dateobs, sca=5, tag=uuid.uuid4().hex[:8])
    cleanup["rids"].append(rid)
    conn.commit()

    digest = _checksum("l2b")
    first = repository.admit_l2file(
        exposure=expid, sca=5, source_checksum=digest, rid=rid,
        facts=_facts(sca=5), release_identity=release)
    conn.commit()

    second = repository.admit_l2file(
        exposure=expid, sca=5, source_checksum=digest.upper(), rid=rid,
        facts=_facts(sca=5), release_identity=release)
    conn.commit()

    assert second.admission_id == first.admission_id
    assert second.created is False
    stored = fixture.executor(conn)(
        "SELECT source_checksum FROM admission_l2files WHERE rid = %s", [rid])
    assert stored[0][0] == digest


# ===========================================================================
# 2. CONFLICT POLICY — a repeat that would REDEFINE is refused, both values
#    named, and nothing is overwritten.
# ===========================================================================
def test_conflicting_exposure_facts_are_refused_naming_both_values(
        admission_db):
    """REFUSAL, NOT OVERWRITE AND NOT SILENT ACCEPT.

    Rule 20 says a repeat RETURNS its existing admission. It does not say a
    repeat may REDEFINE it — so the same `dateobs` arriving with different
    observational facts is a disagreement neither this code nor the database
    can adjudicate, and the honest answer is to refuse and report both sides.

    The alternatives are both worse and both present in the legacy path:
    `addexposure` OVERWRITES (its `else` branch updates every field), which
    silently adopts whichever ingest ran last; a bare `ON CONFLICT DO NOTHING`
    would silently keep the first, which hides that two ingests believe
    different things about one observation.

    BOTH VALUES ARE ASSERTED ON THE EXCEPTION, not merely in its text: an
    operator's tooling classifies on `exc.existing` / `exc.arriving`, and
    `str(exc)` is what a human reads. Checking only the string would let a
    later reword drop a field nobody notices is gone.
    """
    conn, repository, release, cleanup = admission_db
    expid, dateobs = _make_exposure(conn, offset=5)
    cleanup["expids"].append(expid)
    conn.commit()

    repository.admit_exposure(dateobs=dateobs, expid=expid,
                              facts=_facts(exptime=100.0),
                              release_identity=release)
    conn.commit()

    with pytest.raises(AdmissionConflict) as caught:
        repository.admit_exposure(dateobs=dateobs, expid=expid,
                                  facts=_facts(exptime=300.0),
                                  release_identity=release)

    conflict = caught.value
    assert conflict.grain == "exposure"
    assert conflict.field == "exptime"
    assert float(conflict.existing) == 100.0
    assert float(conflict.arriving) == 300.0
    assert "exptime" in str(conflict)
    assert conflict.error_category == "admission_conflict"

    # NOTHING WAS OVERWRITTEN. Re-read the row rather than trusting that a
    # raise implies no write: the refusal has to happen BEFORE the update, and
    # a version that raised after writing would pass every assertion above.
    conn.rollback()
    rows = fixture.executor(conn)(
        "SELECT admitted_facts FROM admission_exposures WHERE expid = %s",
        [expid])
    facts = rows[0][0]
    if isinstance(facts, str):
        facts = json.loads(facts)
    assert float(facts["exptime"]) == 100.0


def test_a_different_source_checksum_for_one_grain_is_refused(admission_db):
    """Same `(expid, sca)`, different bytes — refused, NEVER re-versioned.

    This is the L2 half of the conflict policy and the exact behaviour
    `addl2file` lacks: presented with the same detector file carrying different
    content, it computes `max(version) + 1` and admits it as a new row, so the
    database ends up holding two admissions and no statement about which is
    current.

    The repository checks the GRAIN BEFORE THE IDENTITY, deliberately — the
    identities differ (the checksum is a hashed component), so attempting the
    insert would take a unique violation on `admission_l2files_grain_uq` that
    says far less than this exception does.
    """
    conn, repository, release, cleanup = admission_db
    expid, dateobs = _make_exposure(conn, offset=6)
    cleanup["expids"].append(expid)
    rid = _make_l2file(conn, expid, dateobs, sca=6, tag=uuid.uuid4().hex[:8])
    cleanup["rids"].append(rid)
    conn.commit()

    original = _checksum("l2c")
    arriving = _checksum("l2d")
    assert original != arriving

    repository.admit_l2file(exposure=expid, sca=6, source_checksum=original,
                            rid=rid, facts=_facts(sca=6),
                            release_identity=release)
    conn.commit()

    with pytest.raises(AdmissionConflict) as caught:
        repository.admit_l2file(exposure=expid, sca=6,
                                source_checksum=arriving, rid=rid,
                                facts=_facts(sca=6), release_identity=release)

    conflict = caught.value
    assert conflict.grain == "l2file"
    assert conflict.existing == original
    assert conflict.arriving == arriving
    assert "source checksum" in str(conflict)
    assert str(expid) in str(conflict) and "sca=6" in str(conflict)

    # NEITHER OVERWROTE, AND NEITHER RE-VERSIONED: the original checksum
    # survives and there is still exactly one row for the pair.
    conn.rollback()
    rows = fixture.executor(conn)(
        "SELECT source_checksum, count(*) OVER () FROM admission_l2files"
        " WHERE expid = %s AND sca = %s", [expid, 6])
    assert rows[0][0] == original
    assert rows[0][1] == 1


def test_facts_that_only_add_are_not_a_conflict(admission_db):
    """MORE INFORMATION IS NOT A DISAGREEMENT — the documented behaviour of
    `_refuse_fact_conflict`, asserted so it stays deliberate.

    An ingest that learned to parse one more FITS header records a fact the
    previous run did not. That is more information about the SAME observation,
    not a contradiction, and refusing it would make every parser improvement a
    breaking change requiring the whole survey to be re-admitted.

    The comparison is therefore over keys present in BOTH records. Asserted
    explicitly because the alternative — comparing whole dicts — is the obvious
    implementation and would pass a naive reading of "the facts must match".
    """
    conn, repository, release, cleanup = admission_db
    expid, dateobs = _make_exposure(conn, offset=7)
    cleanup["expids"].append(expid)
    conn.commit()

    first = repository.admit_exposure(
        dateobs=dateobs, expid=expid, facts={"a": 1},
        release_identity=release)
    conn.commit()

    # The superset: `a` agrees, `b` is new.
    second = repository.admit_exposure(
        dateobs=dateobs, expid=expid, facts={"a": 1, "b": 2},
        release_identity=release)
    conn.commit()

    assert second.admission_id == first.admission_id
    assert second.created is False
    assert second.admitted_at == first.admitted_at

    # AND THE RECORDED FACTS ARE NOT REWRITTEN EITHER. The repeat returns the
    # existing admission; it does not merge into it. `admitted_facts` still
    # holds what was recorded at first admission, which is what a replay
    # reconstructs the row from.
    rows = fixture.executor(conn)(
        "SELECT admitted_facts FROM admission_exposures WHERE expid = %s",
        [expid])
    recorded = rows[0][0]
    if isinstance(recorded, str):
        recorded = json.loads(recorded)
    assert recorded == {"a": 1}


def test_numeric_facts_compare_by_value_not_by_type(admission_db):
    """`1` and `1.0` are one observation, not two.

    A fact written as an integer and read back out of `jsonb` as `1.0` — or
    the reverse, depending on which reader parsed the header — would otherwise
    raise `AdmissionConflict` on every single re-ingest, turning the conflict
    policy from a safety property into a permanent outage. The repository
    compares numerics by value for exactly this reason, and this test is what
    keeps that from being tidied away as a redundant branch.
    """
    conn, repository, release, cleanup = admission_db
    expid, dateobs = _make_exposure(conn, offset=8)
    cleanup["expids"].append(expid)
    conn.commit()

    first = repository.admit_exposure(
        dateobs=dateobs, expid=expid, facts={"x": 1},
        release_identity=release)
    conn.commit()

    second = repository.admit_exposure(
        dateobs=dateobs, expid=expid, facts={"x": 1.0},
        release_identity=release)
    conn.commit()

    assert second.admission_id == first.admission_id
    assert second.created is False

    # And the converse direction, so the equivalence is not one-way.
    third = repository.admit_exposure(
        dateobs=dateobs, expid=expid, facts={"x": 1},
        release_identity=release)
    conn.commit()
    assert third.admission_id == first.admission_id


# ===========================================================================
# 3. CONCURRENCY — the criterion-3 test, and the most important one here.
# ===========================================================================
def test_two_concurrent_admissions_converge_on_one_admission(
        admission_db, second_conn):
    """TWO REAL CONNECTIONS, A REAL BARRIER, ONE ROW.

    THE DEFECT, EXACTLY. `addexposure` is SELECT-THEN-INSERT
    (`008-functions.sql:290-293`): `select expid into expid__ from Exposures
    where dateobs = dateobs_` followed by a conditional INSERT. Two concurrent
    admissions of one observation both read NULL at that SELECT — neither
    transaction can see the other's uncommitted row — and both proceed to
    insert. The loser then takes a unique violation on `exposurespk` and the
    caller sees a database error for what is, operationally, the ordinary case
    of two workers picking up the same file. Rule 20 asks for the OPPOSITE: a
    repeated observation RECEIVES ITS EXISTING ADMISSION.

    THE RACE IS REAL, NOT SCRIPTED. Two independent connections (`conn` and
    `second_conn`, which `conftest` opens separately precisely so two
    connections from one pool cannot turn out to be one session), each in its
    own transaction, each in its own thread, held at a `threading.Barrier(2)`
    so neither can finish before the other has started. Whichever reaches the
    INSERT first wins; the other's `ON CONFLICT (admission_identity) DO UPDATE
    ... RETURNING` — and it must be DO UPDATE, because DO NOTHING returns no
    row at all — blocks on the winner's uncommitted row, then returns it with
    `xmax != 0`, which is how `created=False` is derived.

    BOTH CONNECTIONS COMMIT. The loser's `ON CONFLICT` cannot resolve until the
    winner's transaction ENDS, so a version of this test that left either
    transaction open would deadlock on the barrier's far side and time out
    rather than assert anything.

    FOUR ASSERTIONS, and each rules out a different way of passing wrongly:

      * exactly one `created=True` and one `created=False` — the split that
        proves both calls returned and only one inserted. Two `True`s would
        mean two rows; two `False`s would mean neither inserted and the
        precondition was wrong;
      * NO exception reached either caller — specifically no
        `psycopg2.errors.UniqueViolation`, which is exactly what the
        select-then-insert shape surfaces;
      * both received the SAME `admission_id` — the loser received the
        WINNER'S admission and not a second one under a different surrogate;
      * ONE ROW in the table afterwards, counted on a third read after both
        transactions closed.

    A repository built on SELECT-then-INSERT passes every stub test ever
    written for it and fails HERE. That is the entire reason this tier exists.
    """
    conn, repository, release, cleanup = admission_db
    expid, dateobs = _make_exposure(conn, offset=9)
    cleanup["expids"].append(expid)
    conn.commit()

    # The second connection needs the release row visible, which the commit
    # above has already made — but the repository over it is built here so both
    # threads talk to genuinely separate sessions.
    other = AdmissionRepository(second_conn)

    barrier = threading.Barrier(2)
    results = {}
    errors = {}

    def admit(slot, connection, repo):
        try:
            # EVERY THREAD DOES ITS ARGUMENT-BUILDING BEFORE THE BARRIER, so
            # the barrier releases both into the DATABASE call and not into a
            # digest computation — the window under test is the one where two
            # transactions are inside the admission statement at once.
            facts = _facts()
            barrier.wait(timeout=30)
            admission = repo.admit_exposure(
                dateobs=dateobs, expid=expid, facts=facts,
                release_identity=release)
            connection.commit()
            results[slot] = admission
        except Exception as exc:                             # noqa: BLE001
            connection.rollback()
            errors[slot] = exc

    threads = [threading.Thread(target=admit, args=(slot, connection, repo))
               for slot, connection, repo in (("a", conn, repository),
                                              ("b", second_conn, other))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, (
        f"an admission raised instead of converging: {errors}. A unique "
        f"violation here is addexposure's select-then-insert shape: both "
        f"transactions read NULL and both inserted, so the loser got a "
        f"constraint error instead of receiving the existing admission")
    for slot, exc in errors.items():
        assert not isinstance(exc, psycopg2.errors.UniqueViolation)
    assert len(results) == 2, f"only {sorted(results)} completed"

    created = sorted(admission.created for admission in results.values())
    assert created == [False, True], (
        f"expected exactly one insert and one receive, got created={created}")
    assert results["a"].admission_id == results["b"].admission_id, (
        f"the two admissions produced different surrogates: "
        f"{results['a'].admission_id} and {results['b'].admission_id} — the "
        f"loser minted a second admission instead of receiving the winner's")
    assert (results["a"].admission_identity
            == results["b"].admission_identity)

    # ONE ROW, read after both transactions have closed.
    rows = fixture.executor(conn)(
        "SELECT count(*) FROM admission_exposures WHERE expid = %s", [expid])
    assert rows[0][0] == 1


def test_two_concurrent_l2_admissions_converge_on_one_admission(
        admission_db, second_conn):
    """The same race at the L2 grain, where the legacy path cannot even lose.

    Worth its own test rather than folding into the one above, because the
    failure modes differ. At the exposure grain the legacy defect surfaces as
    a UNIQUE VIOLATION — loud, at least. At the L2 grain `addl2file`'s
    `max(version) + 1` means two concurrent ingests of one detector file both
    SUCCEED, minting versions N+1 and N+2 and leaving two admissions with no
    error anywhere. That is the silent version of the same defect, and only a
    row count can see it.
    """
    conn, repository, release, cleanup = admission_db
    expid, dateobs = _make_exposure(conn, offset=10)
    cleanup["expids"].append(expid)
    rid = _make_l2file(conn, expid, dateobs, sca=8, tag=uuid.uuid4().hex[:8])
    cleanup["rids"].append(rid)
    conn.commit()

    digest = _checksum("l2race")
    other = AdmissionRepository(second_conn)
    barrier = threading.Barrier(2)
    results = {}
    errors = {}

    def admit(slot, connection, repo):
        try:
            facts = _facts(sca=8)
            barrier.wait(timeout=30)
            admission = repo.admit_l2file(
                exposure=expid, sca=8, source_checksum=digest, rid=rid,
                facts=facts, release_identity=release)
            connection.commit()
            results[slot] = admission
        except Exception as exc:                             # noqa: BLE001
            connection.rollback()
            errors[slot] = exc

    threads = [threading.Thread(target=admit, args=(slot, connection, repo))
               for slot, connection, repo in (("a", conn, repository),
                                              ("b", second_conn, other))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, f"an L2 admission raised instead of converging: {errors}"
    assert len(results) == 2
    assert sorted(a.created for a in results.values()) == [False, True]
    assert results["a"].admission_id == results["b"].admission_id

    rows = fixture.executor(conn)(
        "SELECT count(*) FROM admission_l2files WHERE expid = %s AND sca = %s",
        [expid, 8])
    assert rows[0][0] == 1, (
        "two concurrent L2 admissions minted two rows — the silent form of "
        "addl2file's max(version)+1, where neither ingest sees an error")


# ===========================================================================
# 4. WRITE-ONCE IS THE DATABASE'S, NOT THE REPOSITORY'S.
# ===========================================================================
def test_admitted_at_cannot_be_rewritten_by_a_direct_update(admission_db):
    """THE BACKSTOP IS A TRIGGER, AND IT IS TESTED AS A TRIGGER.

    The repository's `ON CONFLICT DO UPDATE SET admission_identity =
    EXCLUDED.admission_identity` is a no-op write chosen so the path can RETURN
    the existing row, and it touches nothing else — but "the repository is
    careful" is a promise, not an invariant. 051's write-once trigger is what
    makes it structural: whatever a future edit, an operator's psql session, or
    a SECURITY DEFINER function tries, the moment of first admission cannot be
    rewritten.

    Provoked with a DIRECT `UPDATE ... SET admitted_at = now()` — deliberately
    going AROUND the repository, because a test that could only reach the
    trigger through the code that is careful never reaches the trigger at all.
    Run as the connected superuser, which is the role every grant in 051 is
    written to still refuse: the table owner and any SECURITY DEFINER function
    bypass column grants, so a property this load-bearing must not depend on
    getting a grant map right.

    Classified by SQLSTATE **RA010**, never by message text — the discipline
    `pipeline/operatorctl/contract.py` established for RA001/RA002. The message
    is asserted too, but as a SECOND assertion: the code is the database's own
    classification and the text is written for an operator and may be reworded.
    """
    conn, repository, release, cleanup = admission_db
    expid, dateobs = _make_exposure(conn, offset=11)
    cleanup["expids"].append(expid)
    conn.commit()

    admission = repository.admit_exposure(
        dateobs=dateobs, expid=expid, facts=_facts(),
        release_identity=release)
    conn.commit()

    with pytest.raises(psycopg2.Error) as caught:
        with conn.cursor() as cur:
            cur.execute("UPDATE admission_exposures SET admitted_at = now()"
                        " WHERE admission_id = %s", [admission.admission_id])
    assert caught.value.pgcode == "RA010"
    assert "write-once" in str(caught.value)
    conn.rollback()

    # THE TIMESTAMP SURVIVED. A trigger that raised after the row had already
    # changed would satisfy the assertion above and still have lost the
    # original, so the value is re-read on a fresh statement.
    rows = fixture.executor(conn)(
        "SELECT admitted_at FROM admission_exposures WHERE admission_id = %s",
        [admission.admission_id])
    assert rows[0][0] == admission.admitted_at


def test_the_admission_identity_cannot_be_rewritten_by_a_direct_update(
        admission_db):
    """The identity is immutable too, and for a different reason.

    `admitted_at` is history; `admission_identity` is the key everything else
    joins and deduplicates on. Re-minting it under a live row would leave every
    downstream reference pointing at a value nothing computes any more, and —
    unlike a lost timestamp — the damage would not even be visible in the row
    itself. Its own trigger branch, so its own test.
    """
    conn, repository, release, cleanup = admission_db
    expid, dateobs = _make_exposure(conn, offset=12)
    cleanup["expids"].append(expid)
    conn.commit()

    admission = repository.admit_exposure(
        dateobs=dateobs, expid=expid, facts=_facts(),
        release_identity=release)
    conn.commit()

    with pytest.raises(psycopg2.Error) as caught:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE admission_exposures SET admission_identity = %s"
                " WHERE admission_id = %s",
                ["sha256:" + "f" * 64, admission.admission_id])
    assert caught.value.pgcode == "RA010"
    assert "immutable" in str(caught.value)
    conn.rollback()

    rows = fixture.executor(conn)(
        "SELECT admission_identity FROM admission_exposures"
        " WHERE admission_id = %s", [admission.admission_id])
    assert rows[0][0] == admission.admission_identity


def test_the_l2_admission_is_write_once_too(admission_db):
    """The same two freezes on the other grain's table.

    Asserted separately rather than assumed from the exposure table: 051
    attaches the trigger to each table with its own `CREATE TRIGGER`, and a
    missing one is exactly the kind of copy-paste omission a review misses and
    a catalog query does not.
    """
    conn, repository, release, cleanup = admission_db
    expid, dateobs = _make_exposure(conn, offset=13)
    cleanup["expids"].append(expid)
    rid = _make_l2file(conn, expid, dateobs, sca=9, tag=uuid.uuid4().hex[:8])
    cleanup["rids"].append(rid)
    conn.commit()

    admission = repository.admit_l2file(
        exposure=expid, sca=9, source_checksum=_checksum("l2wo"), rid=rid,
        facts=_facts(sca=9), release_identity=release)
    conn.commit()

    with pytest.raises(psycopg2.Error) as caught:
        with conn.cursor() as cur:
            cur.execute("UPDATE admission_l2files SET admitted_at = now()"
                        " WHERE admission_id = %s", [admission.admission_id])
    assert caught.value.pgcode == "RA010"
    conn.rollback()

    rows = fixture.executor(conn)(
        "SELECT admitted_at FROM admission_l2files WHERE admission_id = %s",
        [admission.admission_id])
    assert rows[0][0] == admission.admitted_at


# ===========================================================================
# 5. THE SEALED MANIFEST — crash ordering, enforced rather than conventional.
# ===========================================================================
def test_an_admission_citing_an_unsealed_manifest_is_refused(admission_db):
    """SEAL LAST, OR THE REPLAY GUARANTEE IS A GUESS.

    A manifest is created UNSEALED, its entries are written, and it is sealed
    ONLY once every entry is durable. A crash therefore leaves either a
    complete replayable record or an EXPLICITLY UNSEALED one — never a sealed
    manifest whose entries are partial. That ordering is the whole content of
    rule 20's durability clause, and it is worth nothing unless citing an
    unsealed manifest is REFUSED.

    Enforced by a trigger rather than by application discipline, for the reason
    050 established and 051 repeats: the table owner and any SECURITY DEFINER
    function bypass column grants, so a property this load-bearing must not
    depend on getting a grant map right.

    The test walks the real sequence — open, add an entry, admit (refused),
    seal, admit (accepted) — so the SAME manifest proves both halves. Two
    manifests would leave open the possibility that the second admission
    succeeded for some unrelated reason.
    """
    conn, repository, release, cleanup = admission_db
    expid, dateobs = _make_exposure(conn, offset=14)
    cleanup["expids"].append(expid)

    manifest = repository.open_manifest(
        manifest_key=f"manifest-{fixture.RUN_TAG}-unsealed",
        source_scope=f"scope-{fixture.RUN_TAG}",
        release_identity=release, byte_custody="pipeline-retained")
    cleanup["manifests"].append(manifest.manifest_id)
    assert manifest.sealed is False, (
        "open_manifest returned a SEALED manifest: unsealed is the only state "
        "a manifest can be created in, and that is the crash-ordering "
        "guarantee rather than a default")

    repository.add_manifest_entry(
        manifest.manifest_id, source_bucket="rapid-ingest-contract",
        source_key=f"{fixture.RUN_TAG}/unsealed/0.fits",
        source_checksum=_checksum("unsealed"),
        source_version_id=f"v-{fixture.RUN_TAG}")
    conn.commit()

    # STILL UNSEALED: the admission must be refused, by the trigger, with
    # RA010 — the invariant SQLSTATE, which `_is_invariant_violation` lets
    # pass through UNWRAPPED rather than re-typing as a retryable query
    # failure. A violated invariant is neither retryable nor recoverable.
    with pytest.raises(psycopg2.Error) as caught:
        repository.admit_exposure(dateobs=dateobs, expid=expid,
                                  facts=_facts(), release_identity=release,
                                  manifest_id=manifest.manifest_id)
    assert caught.value.pgcode == "RA010"
    assert "not sealed" in str(caught.value)
    conn.rollback()

    # NOW SEAL, and the same admission is accepted. The seal records what it
    # sealed — count and a checksum over the canonical entry list — because
    # sealing without those would be sealing without knowing what was sealed
    # (`admission_manifests_sealed_complete_ck` says so too).
    sealed = repository.seal_manifest(manifest.manifest_id)
    conn.commit()
    assert sealed.sealed is True
    assert sealed.entry_count == 1

    admission = repository.admit_exposure(
        dateobs=dateobs, expid=expid, facts=_facts(),
        release_identity=release, manifest_id=manifest.manifest_id)
    conn.commit()
    assert admission.created is True

    # AND THE ADMISSION IS FINDABLE FROM THE MANIFEST, which is what a replay
    # check reads.
    made = repository.admissions_for_manifest(manifest.manifest_id)
    assert [row[0] for row in made["exposures"]] == [
        admission.admission_identity]


def test_sealing_is_idempotent_and_an_empty_manifest_is_refused(admission_db):
    """A retried ingest converges; an empty enumeration does not seal.

    IDEMPOTENT SEALING is what lets an ingest whose response was lost re-run
    its own tail without failing on its own manifest — the same "re-run after a
    lost response" case the outbox's insert path absorbs. Re-sealing returns
    the RECORDED state rather than recomputing, so the entries checksum stays a
    statement about the list as it stood at the first seal.

    AN EMPTY MANIFEST IS REFUSED because sealing one would record a complete,
    replayable source that describes nothing — the manifest would then satisfy
    every downstream check while guaranteeing no replay at all. The most
    dangerous shape a "durable source" can take is an empty one that claims to
    be whole.
    """
    conn, repository, release, cleanup = admission_db

    empty = repository.open_manifest(
        manifest_key=f"manifest-{fixture.RUN_TAG}-empty",
        source_scope=f"scope-{fixture.RUN_TAG}",
        release_identity=release, byte_custody="none")
    cleanup["manifests"].append(empty.manifest_id)
    conn.commit()

    with pytest.raises(AdmissionError) as caught:
        repository.seal_manifest(empty.manifest_id)
    assert "no entries" in str(caught.value)
    conn.rollback()

    # A manifest with entries seals, and seals again to the same state.
    manifest_id, sealed = _sealed_manifest(
        repository, conn, release, cleanup, "idempotent", entries=2)
    assert sealed.entry_count == 2

    again = repository.seal_manifest(manifest_id)
    conn.commit()
    assert again.sealed is True
    assert again.entry_count == 2

    # THE RECORDED SEAL DID NOT MOVE. Re-sealing returning the recorded state
    # is only meaningful if it also left the row alone — a version that
    # recomputed would produce the same checksum here and a different
    # `sealed_at`, and only the timestamp can tell them apart.
    rows = fixture.executor(conn)(
        "SELECT entry_count, entries_checksum, sealed_at"
        "  FROM admission_manifests WHERE manifest_id = %s", [manifest_id])
    assert rows[0][0] == 2
    assert rows[0][1].startswith("sha256:")
    first_sealed_at = rows[0][2]
    repository.seal_manifest(manifest_id)
    conn.commit()
    rows = fixture.executor(conn)(
        "SELECT sealed_at FROM admission_manifests WHERE manifest_id = %s",
        [manifest_id])
    assert rows[0][0] == first_sealed_at


def test_a_sealed_manifest_refuses_further_entries(admission_db):
    """APPENDING AFTER THE SEAL WOULD MAKE THE SEAL A STATEMENT ABOUT
    SOMETHING ELSE.

    `entries_checksum` is computed over the entry list AS IT STOOD AT SEALING,
    and it exists so a sealed manifest that later disagrees with its entries is
    DETECTABLE. An append that succeeded would silently invalidate that check
    for every replay afterwards — the manifest would still claim to be sealed
    and complete, and the checksum would quietly describe a list that no longer
    exists.
    """
    conn, repository, release, cleanup = admission_db
    manifest_id, _sealed = _sealed_manifest(
        repository, conn, release, cleanup, "closed", entries=1)

    with pytest.raises(ManifestNotSealed) as caught:
        repository.add_manifest_entry(
            manifest_id, source_bucket="rapid-ingest-contract",
            source_key=f"{fixture.RUN_TAG}/closed/late.fits",
            source_checksum=_checksum("late"))
    assert "already SEALED" in str(caught.value)
    conn.rollback()

    # THE ENTRY LIST IS UNCHANGED, re-read rather than inferred from the
    # raise: a refusal issued after the INSERT would pass the assertion above.
    entries = repository.manifest_entries(manifest_id)
    assert len(entries) == 1
    rows = fixture.executor(conn)(
        "SELECT entry_count FROM admission_manifests WHERE manifest_id = %s",
        [manifest_id])
    assert rows[0][0] == 1


def test_opening_a_manifest_twice_returns_the_same_manifest(admission_db):
    """`open_manifest` is idempotent on `manifest_key`, so a retried ingest
    does not fork its own source record.

    `admission_manifests_key_uq` is the constraint; `ON CONFLICT DO UPDATE SET
    manifest_key = EXCLUDED.manifest_key` is the no-op write that lets the
    statement RETURN the existing row (DO NOTHING returns none). The important
    consequence: a re-run picks up the manifest it was already filling,
    including its sealed state, rather than opening a second unsealed one
    beside it and admitting against a source that enumerates half the files.
    """
    conn, repository, release, cleanup = admission_db
    key = f"manifest-{fixture.RUN_TAG}-reopen"

    first = repository.open_manifest(
        manifest_key=key, source_scope=f"scope-{fixture.RUN_TAG}",
        release_identity=release, byte_custody="external-versioned")
    cleanup["manifests"].append(first.manifest_id)
    conn.commit()

    second = repository.open_manifest(
        manifest_key=key, source_scope=f"scope-{fixture.RUN_TAG}",
        release_identity=release, byte_custody="external-versioned")
    conn.commit()
    assert second.manifest_id == first.manifest_id

    # AND AFTER SEALING, RE-OPENING REPORTS IT AS SEALED — the state a retried
    # ingest has to see in order to skip straight to admitting.
    repository.add_manifest_entry(
        first.manifest_id, source_bucket="rapid-ingest-contract",
        source_key=f"{fixture.RUN_TAG}/reopen/0.fits",
        source_checksum=_checksum("reopen"))
    repository.seal_manifest(first.manifest_id)
    conn.commit()

    third = repository.open_manifest(
        manifest_key=key, source_scope=f"scope-{fixture.RUN_TAG}",
        release_identity=release, byte_custody="external-versioned")
    conn.commit()
    assert third.manifest_id == first.manifest_id
    assert third.sealed is True


def test_a_release_cannot_be_reopened_under_a_different_source_scope(
        admission_db):
    """THE DEFECT THIS GUARD CLOSES, against real rows.

    `begin_admission_run` (`database/sims/admission_bridge.py`) reads the
    current release pointer and never asks its caller to declare a release —
    so nothing stops the next ingest from inheriting a one-off's identity.
    This is the live shape: `provenance-backfill-g0001-2026-08-15` was
    recorded under `source_scope='socsim-r00340'`; a production socsim
    ingest passes an `s3://...` scope
    (`database/sims/db_register_socsim_files.py`), and troxel/rimtimsim
    ingests pass their own `s3://` forms. Reproduced here with one release
    opened first under one scope, then a second `open_manifest` call for the
    SAME release under a DIFFERENT scope, which must be refused rather than
    silently recording a second manifest that muddies what the release means.
    """
    conn, repository, release, cleanup = admission_db

    first = repository.open_manifest(
        manifest_key=f"manifest-{fixture.RUN_TAG}-scope-a",
        source_scope=f"scope-{fixture.RUN_TAG}-a",
        release_identity=release, byte_custody="external-versioned")
    cleanup["manifests"].append(first.manifest_id)
    conn.commit()

    with pytest.raises(ReleaseScopeConflict) as caught:
        repository.open_manifest(
            manifest_key=f"manifest-{fixture.RUN_TAG}-scope-b",
            source_scope=f"scope-{fixture.RUN_TAG}-b",
            release_identity=release, byte_custody="external-versioned")

    conflict = caught.value
    assert conflict.release_identity == release
    assert conflict.existing_scope == f"scope-{fixture.RUN_TAG}-a"
    assert conflict.requested_scope == f"scope-{fixture.RUN_TAG}-b"
    assert conflict.error_category == "admission_release_scope_conflict"
    assert release in str(conflict)
    assert f"scope-{fixture.RUN_TAG}-a" in str(conflict)
    assert f"scope-{fixture.RUN_TAG}-b" in str(conflict)
    conn.rollback()

    # AND NOTHING WAS WRITTEN by the refused call: only the first manifest
    # exists for this release, re-read rather than inferred from the raise.
    rows = fixture.executor(conn)(
        "SELECT source_scope FROM admission_manifests"
        " WHERE release_identity = %s", [release])
    assert [row[0] for row in rows] == [f"scope-{fixture.RUN_TAG}-a"]


def test_reopening_the_same_release_and_scope_is_unaffected_by_the_guard(
        admission_db):
    """Idempotent replay keeps working: same release, same scope, twice.

    The guard's own SQL excludes `source_scope = requested_scope`, so a
    manifest re-opened with the release and scope it already has finds
    nothing to conflict with. Asserted end to end here rather than trusted
    from the SQL alone, because `test_opening_a_manifest_twice_returns_the_
    same_manifest` above predates this guard and would not by itself prove
    the guard leaves that path alone.
    """
    conn, repository, release, cleanup = admission_db
    key = f"manifest-{fixture.RUN_TAG}-guard-replay"
    scope = f"scope-{fixture.RUN_TAG}-replay"

    first = repository.open_manifest(
        manifest_key=key, source_scope=scope, release_identity=release,
        byte_custody="external-versioned")
    cleanup["manifests"].append(first.manifest_id)
    conn.commit()

    second = repository.open_manifest(
        manifest_key=key, source_scope=scope, release_identity=release,
        byte_custody="external-versioned")
    conn.commit()
    assert second.manifest_id == first.manifest_id


def test_the_same_source_scope_under_a_different_release_is_fine(
        admission_db):
    """The constraint is PER-RELEASE, not per-scope.

    A deliberate re-release of the same source under a new release identity
    — an original ingest and a later, intentional re-release of
    `socsim-r00340` under a fresh release, say — is not the failure this
    guard exists to catch, and must not be refused. A second, independent
    release is registered here rather than reusing `admission_db`'s, because
    the property under test is specifically that TWO DIFFERENT releases may
    share one scope.
    """
    conn, repository, release, cleanup = admission_db
    other_release = f"admit-{fixture.RUN_TAG}-other-{uuid.uuid4().hex[:8]}"
    repository.register_release(other_release, manifest_uri=None,
                                manifest_checksum=None)
    conn.commit()

    scope = f"scope-{fixture.RUN_TAG}-shared"
    first = repository.open_manifest(
        manifest_key=f"manifest-{fixture.RUN_TAG}-shared-a",
        source_scope=scope, release_identity=release,
        byte_custody="external-versioned")
    cleanup["manifests"].append(first.manifest_id)
    conn.commit()

    second = repository.open_manifest(
        manifest_key=f"manifest-{fixture.RUN_TAG}-shared-b",
        source_scope=scope, release_identity=other_release,
        byte_custody="external-versioned")
    cleanup["manifests"].append(second.manifest_id)
    conn.commit()

    assert second.manifest_id != first.manifest_id

    execute = fixture.executor(conn)
    try:
        execute("DELETE FROM admission_release_pointer"
                " WHERE release_identity = %s", [other_release])
        execute("DELETE FROM admission_releases WHERE release_identity = %s",
                [other_release])
        conn.commit()
    except Exception:                                          # noqa: BLE001
        conn.rollback()


def test_byte_custody_must_be_stated(admission_db):
    """The replay guarantee is only as strong as what `byte_custody` names.

    Either this pipeline retains the source bytes, or the manifest pins an
    immutable external object version and durability rests on that external
    retention, or neither and a replay reconstructs from recorded facts alone.
    A manifest must SAY WHICH — an unstated custody is an assumed one, and the
    assumption is always the optimistic one.
    """
    conn, repository, release, _cleanup = admission_db

    with pytest.raises(AdmissionError) as caught:
        repository.open_manifest(
            manifest_key=f"manifest-{fixture.RUN_TAG}-custody",
            source_scope=f"scope-{fixture.RUN_TAG}",
            release_identity=release, byte_custody="probably-fine")
    assert "byte_custody" in str(caught.value)
    conn.rollback()


# ===========================================================================
# 5b. RE-ENTRANCE UNDER THE REAL RUNTIME ROLE — ledger row F, closing a gap
#     `test_opening_a_manifest_twice_returns_the_same_manifest` above cannot
#     see by construction.
# ===========================================================================
#
# **THE GAP.** Every `AdmissionRepository` write method — `open_manifest`
# included — is `INSERT ... ON CONFLICT (...) DO UPDATE SET <col> =
# EXCLUDED.<col> ... RETURNING`, the same "no-op write so the RETURNING has a
# row" idiom this module's own docstring names (see admission.py:33-40).
# PostgreSQL requires UPDATE privilege on the SET-listed column for the DO
# UPDATE branch to authorize AT ALL, even though the write changes nothing —
# a fact the class's own commit-free `_query` (admission.py:611) has no way
# to route around, since it is PostgreSQL's own authorization check, not
# application logic. Migration 093 (`rapid_systems/cloudformation/
# db-migrations/093-admission-conflict-update-grants.sql`) grants
# `rapid_pipeline_write` column-scoped `UPDATE (manifest_key)` on
# `admission_manifests` to fix exactly this for `open_manifest` — and the
# matching column sets on the other three admission tables, for the same
# reason on `add_manifest_entry`, `admit_exposure` and `admit_l2file`.
#
# **WHY THE TEST ABOVE DID NOT CATCH IT.** That test calls `open_manifest`
# twice using `admission_db`'s `repository`, which runs on `conn` — the
# contract tier's superuser session connection (`conftest.py`'s `_session_
# conn`, `fixture.connect()` defaulting `PGUSER` to `postgres`). A superuser
# is exempt from every GRANT check by construction, so that test's second
# call — the one that takes the `ON CONFLICT DO UPDATE` branch — proves the
# SQL shape is idempotent under a connection to which the question "does
# this role have UPDATE" never gets asked. It differs from the tests below
# by CONNECTION IDENTITY, not by SQL shape, exactly as `test_gc_fence_roles.
# py`'s header draws that same distinction for `gc_fences`/085. NO test
# before this one called `open_manifest` twice AS `rapid_pipeline_write` —
# the tier that AdmissionRepository is actually meant to run under in
# production ingest — so the second call was, before 093, unexecutable
# outside a superuser session and nothing here noticed.
#
# **MECHANISM**, copied from `test_gc_fence_roles.py`: `SET LOCAL ROLE`
# inside the test's own transaction (postgres is exempt from the membership
# check `SET ROLE` normally enforces), drive `repository.open_manifest`
# through it twice, then ROLLBACK to unwind both the role and every write —
# no `cleanup["manifests"]` bookkeeping needed, because nothing here is ever
# committed. See `_open_manifest_twice_as_role`'s own docstring for the one
# trap that shape has to avoid: `conn.commit()` between the two calls would
# end the transaction and silently drop back to the superuser identity for
# the second one, making the test pass for the wrong reason.

def _require_role(conn, role):
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [role])
        if cur.fetchone() is None:
            pytest.skip("%s is not present in this database" % role)


def _require_093(conn):
    """Skip unless 093's column-scoped grant is actually present.

    `has_column_privilege('rapid_pipeline_write', 'admission_manifests',
    'manifest_key', 'UPDATE')` is the one fact only 093 grants — no earlier
    migration gives `rapid_pipeline_write` UPDATE on `manifest_key` — so a
    True reading here is a direct catalog proof the pin includes 093,
    matching `test_gc_fence_roles._require_085`'s reasoning for the same gap
    class on `gc_fences`.

    MUST USE `has_column_privilege`, NOT `has_table_privilege`, HERE. 093's
    grant is column-scoped (`GRANT UPDATE (manifest_key) ON admission_
    manifests TO rapid_pipeline_write`), and `has_table_privilege(...,
    'UPDATE')` reads FALSE for a role that holds only a column-scoped UPDATE
    grant with no whole-table UPDATE alongside it — a prior investigation
    was misled by exactly this before finding `has_column_privilege`, which
    is scoped to ask about the column 093 actually touches.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT has_column_privilege('rapid_pipeline_write',"
            " 'admission_manifests', 'manifest_key', 'UPDATE')")
        has_093 = cur.fetchone()[0]
    if not has_093:
        pytest.skip(
            "the schema pin present predates migration 093 "
            "(rapid_pipeline_write lacks column-scoped UPDATE on "
            "admission_manifests.manifest_key); this role-scoped "
            "re-entrance test needs 093's grant to exercise the post-fix "
            "contract and skips cleanly without it")


def test_open_manifest_is_re_entrant_under_its_own_tier(admission_db):
    """`open_manifest` called TWICE, back-to-back, AS `rapid_pipeline_write`
    — the role that runs it in production, not the superuser this file's
    other tests connect as. See this section's header for the full defect
    this closes (ledger row F, migration 093).

    The first call takes the plain INSERT arm; the second, on the SAME
    `manifest_key` inside the SAME transaction, is the one that has to take
    `ON CONFLICT (manifest_key) DO UPDATE SET manifest_key = EXCLUDED.
    manifest_key ... RETURNING` — the branch 093 exists to unblock. Both
    calls happen before any commit, so the second sees the first's row via
    ordinary uncommitted read-your-own-writes within one transaction; opening
    as the superuser FIRST would prove nothing about the tier's own grant and
    is deliberately not done here.
    """
    conn, repository, release, _cleanup = admission_db
    _require_role(conn, "rapid_pipeline_write")
    _require_093(conn)
    key = f"manifest-{fixture.RUN_TAG}-role-reentrant"
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL ROLE rapid_pipeline_write")

        # THE TIER REALLY IS IN EFFECT — asserted directly, not assumed from
        # the calls below succeeding. A `SET LOCAL ROLE` that silently did
        # nothing (a typo'd role name inside a superuser session simply
        # stays superuser) would let both `open_manifest` calls below
        # succeed AS THE SUPERUSER and this test would pass for the wrong
        # reason — indistinguishable from the grant actually working. That
        # is the same stub-blind failure mode `test_gc_fence_roles.py`'s
        # negative control exists to rule out, applied here as a positive
        # assertion instead.
        with conn.cursor() as cur:
            cur.execute("SELECT current_user")
            assert cur.fetchone()[0] == "rapid_pipeline_write", (
                "SET LOCAL ROLE did not take effect; this test would "
                "otherwise silently run as the superuser and prove nothing "
                "about 093's grant")

        first = repository.open_manifest(
            manifest_key=key, source_scope=f"scope-{fixture.RUN_TAG}",
            release_identity=release, byte_custody="external-versioned")

        # THE SECOND CALL — same key, same transaction, no commit in
        # between. This is the `ON CONFLICT DO UPDATE` branch 093's grant
        # makes reachable under this role; before 093 this raised
        # `psycopg2.errors.InsufficientPrivilege` on the SET-listed column.
        second = repository.open_manifest(
            manifest_key=key, source_scope=f"scope-{fixture.RUN_TAG}",
            release_identity=release, byte_custody="external-versioned")

        assert second.manifest_id == first.manifest_id, (
            "the second open_manifest call under rapid_pipeline_write did "
            "not return the same manifest as the first — the DO UPDATE arm "
            "093 exists to unblock did not converge")
        assert second.manifest_key == key
    finally:
        # ROLLBACK, not commit — unwinds `SET LOCAL ROLE` (transaction-
        # scoped) AND both open_manifest writes together, matching
        # `test_gc_fence_roles._role_executor`'s documented reasoning for
        # why this tier's role-scoped tests must never commit mid-test. No
        # `cleanup["manifests"]` entry is needed: nothing here is ever
        # durable.
        conn.rollback()


def test_the_manifest_key_grant_is_column_scoped_not_table_wide(
        admission_db):
    """THE CONTROL ON THE TEST ABOVE, and it is a CATALOG assertion rather
    than a second refused call — because in this schema no role can serve as
    a behavioural negative control, and a test that can only ever skip is
    worse than no test at all.

    WHY THERE IS NO BEHAVIOURAL CONTROL TO WRITE. The obvious shape — run the
    same second call under a role holding INSERT but not `UPDATE
    (manifest_key)`, and watch it be refused — has no role to run under.
    Probed live against the deployed database (2026-08-15): every role that
    holds INSERT on `admission_manifests` (`rapid_pipeline_write`,
    `rapid_orchestrator`, `rapid_pipeline`, `rapid_admin`, `rapid_break_glass`)
    also holds the `manifest_key` UPDATE, and every role that lacks the UPDATE
    (`rapid_read`, `rapid_operator`, `rapid_agent_operator`, `rapid_dashboard`,
    `rapid_publisher`) also lacks INSERT — so it is refused on the plain
    INSERT arm with "permission denied for table", never reaching the DO
    UPDATE branch under test. A catalog query for a role in between returns
    the empty set. Creating one is not available either: `rapid_orchestrator`
    has `rolcreaterole = false`, and a contract test that shapes the schema to
    fit itself is not asking what the schema says.

    SO WHAT THIS ASSERTS INSTEAD, and why it is a real control. The risk the
    test above carries is that 093's grant gets "repaired" later by someone
    widening it to table-wide UPDATE — which would make that test keep passing
    while silently handing `rapid_pipeline_write` the ability to rewrite
    `data_class`, `release_identity` and the seal columns on any manifest.
    That is precisely what 093's header forbids ("NOT GRANTED, DELIBERATELY:
    `admission_manifests.data_class`") and precisely what no behavioural test
    can notice, because widening a grant breaks nothing. This test fails on
    that widening.

    IT CAN FAIL, which is the property that makes it worth having: grant
    table-wide UPDATE and the first assertion trips; grant UPDATE on
    `data_class` and the second trips.
    """
    conn, _repository, _release, _cleanup = admission_db
    _require_role(conn, "rapid_pipeline_write")
    _require_093(conn)

    # TABLE-WIDE UPDATE MUST BE ABSENT. `has_table_privilege(..., 'UPDATE')`
    # answers a genuinely different question from `has_column_privilege` and
    # reads FALSE while column-scoped grants exist — the distinction that
    # misled the investigation behind row F, and the reason both spellings
    # appear in this file. Here FALSE is the REQUIRED reading.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT has_table_privilege('rapid_pipeline_write',"
            " 'admission_manifests', 'UPDATE')")
        assert cur.fetchone()[0] is False, (
            "rapid_pipeline_write holds TABLE-WIDE UPDATE on "
            "admission_manifests; 093 grants UPDATE (manifest_key) only, and "
            "a table-wide grant would let the ingest tier rewrite data_class, "
            "release_identity and the seal columns on any manifest")

    # `data_class` STAYS UNGRANTED TO EVERY ROUTINE ROLE. Migration 090 added
    # the column and granted UPDATE on it to nobody, deliberately: a manifest
    # states its class at birth, and a re-classable manifest would change the
    # data class of already-published objects underneath them. 093 kept it
    # that way; this is the assertion that keeps the next migration from
    # quietly not keeping it.
    #
    # `rapid_break_glass` IS EXCLUDED, AND THAT IS NOT A HOLE IN THIS TEST.
    # It holds table-wide `arw` on every admission table by construction
    # (verified in the deployed ACL, 2026-08-15) — being able to do what no
    # routine tier may do is the entire definition of that role, and its use
    # is a headline event that announces itself through
    # `derived.mutation_audit` rather than being prevented by a grant. A test
    # asserting break-glass CANNOT reclass a manifest would be asserting the
    # break-glass role is not a break-glass role. What matters, and what is
    # asserted, is that no tier reachable by ordinary pipeline or operator
    # work can.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT r.rolname FROM pg_roles r"
            " WHERE r.rolname LIKE 'rapid\\_%' ESCAPE '\\'"
            "   AND NOT r.rolsuper"
            "   AND r.rolname <> 'rapid_break_glass'"
            "   AND has_column_privilege(r.rolname, 'admission_manifests',"
            "                            'data_class', 'UPDATE')"
            " ORDER BY r.rolname")
        classable = [row[0] for row in cur.fetchall()]
    assert classable == [], (
        "these routine roles can UPDATE admission_manifests.data_class: %s — "
        "090 and 093 both leave it ungranted on purpose, because the class a "
        "manifest was sealed under is what every published object's prefix "
        "was reconstructed from" % (classable,))

    # And the four columns 093 DOES grant are exactly the four the repository's
    # no-op conflict clauses name — no more. Read from the catalog rather than
    # restated from the migration, so the two cannot drift apart silently.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.column_privileges"
            " WHERE grantee = 'rapid_pipeline_write'"
            "   AND table_name = 'admission_manifests'"
            "   AND privilege_type = 'UPDATE'"
            " ORDER BY column_name")
        granted = [row[0] for row in cur.fetchall()]
    assert granted == ["entries_checksum", "entry_count", "manifest_key",
                       "sealed_at"], (
        "the UPDATE grant surface on admission_manifests has changed: %s. "
        "051 granted the three seal columns; 093 added manifest_key for "
        "open_manifest's conflict clause. Anything else is a widening that "
        "needs its own migration and its own reasoning" % (granted,))

# ===========================================================================
# 6. THE SCHEMA-ABSENT PATH REFUSES — asserted, not assumed.
# ===========================================================================
def test_the_schema_probe_answers_true_where_051_is_applied(admission_db):
    """The probe is a REAL question to the catalog, and here it answers yes.

    Paired with the refusal test below, this is what distinguishes "the
    fail-closed path is unreachable because the schema is present" from "the
    probe always returns True". Without it, monkeypatching `schema_present` to
    False would prove only that a patched method returns what it was patched to
    return.

    The probe spelling is `to_regclass`, matching the three probes already in
    this package, and it is asked BEFORE any admission statement runs —
    catching `UndefinedTable` instead would put the CALLER'S OPEN TRANSACTION
    into an aborted state to be discovered later, and recovering by
    `conn.rollback()` would discard writes the caller had already made and had
    not finished with.
    """
    conn, repository, _release, _cleanup = admission_db

    assert repository.schema_present() is True
    # The connection is still usable immediately afterwards — the probe is a
    # plain SELECT and leaves no failed transaction behind, which is the whole
    # reason it is a probe rather than a caught exception.
    assert fixture.executor(conn)("SELECT 1")[0][0] == 1


def test_admission_refuses_outright_when_the_schema_is_absent(admission_db,
                                                              monkeypatch):
    """THE DEGRADED PATH FAILS CLOSED — a REFUSAL, never a fallback.

    This is the assertion the brief asks for as a real one rather than a
    comment, and it is genuinely awkward to test on a host where 051 IS
    applied: the condition under test is the schema's ABSENCE, and dropping
    four tables to observe it would destroy every other test's fixture rows.

    So the probe is patched to False and the refusal is provoked for real. That
    is legitimate here and would not be legitimate on its own, because the test
    above separately proves the probe is a real catalog question that answers
    True against this database — the two together say "the probe works, and
    when it says no, every admission entry point refuses". Patching the probe
    alone would be a stub asserting a stub.

    WHY REFUSING MATTERS. The tempting alternative is to fall back to the
    legacy stored procedures when the draft is missing, and it would be a
    disaster: `addl2file`'s `coalesce(max(version), 0) + 1` against a
    uniqueness that INCLUDES the version mints a duplicate admission row for
    every re-ingest, so the fallback would reintroduce the exact defect this
    package exists to remove — at precisely the moment nobody was watching,
    because the fallback is silent by design.

    EVERY entry point is checked, not just one: a refusal on `admit_exposure`
    with a fallback still live on `admit_l2file` would leave the worse of the
    two defects reachable.
    """
    conn, repository, release, _cleanup = admission_db
    monkeypatch.setattr(repository, "schema_present", lambda: False)

    entry_points = {
        "current_release": lambda: repository.current_release(),
        "register_release": lambda: repository.register_release("nope"),
        "open_manifest": lambda: repository.open_manifest(
            manifest_key="nope", source_scope="nope",
            release_identity=release, byte_custody="none"),
        "add_manifest_entry": lambda: repository.add_manifest_entry(
            1, "bucket", "key", _checksum("absent")),
        "seal_manifest": lambda: repository.seal_manifest(1),
        "manifest_entries": lambda: repository.manifest_entries(1),
        "admit_exposure": lambda: repository.admit_exposure(
            dateobs=INSTANT_BASE, expid=1, facts=_facts(),
            release_identity=release),
        "admit_l2file": lambda: repository.admit_l2file(
            exposure=1, sca=1, source_checksum=_checksum("absent"), rid=1,
            facts=_facts(), release_identity=release),
        "admissions_for_manifest": lambda:
            repository.admissions_for_manifest(1),
    }
    for name, call in entry_points.items():
        with pytest.raises(AdmissionSchemaAbsent) as caught:
            call()
        assert "REFUSING TO ADMIT" in str(caught.value), (
            f"{name} did not refuse when DRAFT 051 was absent")
        assert caught.value.error_category == "admission_schema_absent"

    # NOTHING WAS WRITTEN by any of those calls: the refusal happens before the
    # first statement, so the caller's transaction is untouched and usable.
    conn.rollback()
    assert fixture.executor(conn)("SELECT 1")[0][0] == 1


def test_the_release_pointer_is_read_not_assumed(admission_db):
    """An admission is stamped with the release the POINTER names, and an
    unset pointer is a refusal rather than a NULL stamp.

    Rule 18's rollback clause — "rollback changes only the release used for
    future admissions" — needs the pointer to be a real read at admission time,
    and needs an admission that cannot be stamped to FAIL rather than to record
    an unstamped row. An unstamped admission would silently reintroduce the gap
    this whole package closes: nothing downstream could say which release
    produced it.

    The pointer is set and cleared inside this test's own rows (a
    `release_identity` unique to the run), so no other test's pointer state is
    disturbed — and `admission_release_pointer_current_uq` means there is only
    ever one current row to disturb, which is precisely why this test must put
    it back.
    """
    conn, repository, release, cleanup = admission_db
    execute = fixture.executor(conn)

    # Whatever the database's current pointer is, remember it and restore it.
    previous = execute("SELECT pointer_id, release_identity"
                       "  FROM admission_release_pointer WHERE is_current")
    try:
        execute("UPDATE admission_release_pointer SET is_current = false"
                " WHERE is_current")
        execute("INSERT INTO admission_release_pointer"
                " (release_identity, is_current, set_by, reason)"
                " VALUES (%s, true, %s, %s)",
                [release, f"contract-{fixture.RUN_TAG}",
                 "contract tier: asserting the pointer is read, not assumed"])
        conn.commit()

        assert repository.current_release() == release

        # An admission takes the pointer's release, and it is what lands in the
        # row — the stamp a later pointer switch must never rewrite.
        expid, dateobs = _make_exposure(conn, offset=15)
        cleanup["expids"].append(expid)
        admission = repository.admit_exposure(
            dateobs=dateobs, expid=expid, facts=_facts(),
            release_identity=repository.current_release())
        conn.commit()
        assert admission.release_identity == release
        rows = execute("SELECT release_identity FROM admission_exposures"
                       " WHERE expid = %s", [expid])
        assert rows[0][0] == release
    finally:
        execute("UPDATE admission_release_pointer SET is_current = false"
                " WHERE is_current")
        if previous:
            execute("UPDATE admission_release_pointer SET is_current = true"
                    " WHERE pointer_id = %s", [previous[0][0]])
        conn.commit()


if __name__ == "__main__":
    unittest.main()
