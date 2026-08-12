"""Acceptance criterion 4 — replayability, source durability, and the
production call sites.

Rule 20's first clause: "The admission source is durable and replayable".

**A MANIFEST OF NAMES AND CHECKSUMS IS NOT SUFFICIENT, WHICH IS WHY THESE
TESTS LOOK THE WAY THEY DO.** The current writers derive many database fields
FROM THE SOURCE BYTES — `db_register_socsim_files.py` parses FITS headers and
WCS, and computes the checksum from the downloaded file — so a manifest
listing only object names and checksums could not reconstruct those rows
without the bytes. The replay therefore has to work from RECORDED FACTS, and
`test_replay_needs_no_source_bytes` is what proves it does: it replays with
the source made unavailable.
"""

import json

import pytest

from pipeline.contract import fixture
from pipeline.repositories.admission import (AdmissionRepository,
                                             ManifestNotSealed)

pytestmark = pytest.mark.contract


def require_admission_schema(conn):
    if not fixture.has_table(conn, "admission_exposures"):
        pytest.skip("DRAFT 051 is not applied (admission_exposures absent)")


def _dateobs(day, tag):
    """A run-unique `dateobs`, ZERO-PADDED and deterministically derived.

    Two bugs are avoided here and both were live in the first draft of this
    file. `"%s" % (n % 24)` produces `T4:00:00Z` for single-digit hours, which
    is not ISO-8601 and which `canonical_dateobs` correctly refuses — the
    admission layer was right and the fixture was wrong. And `hash()` is
    salted per process (PYTHONHASHSEED), so a value derived from it differs
    between the suite run and any re-run, which would make a row written by
    one invisible to the next. A stable digest of the tag gives a unique hour
    that is the same every time.
    """
    import hashlib
    digest = hashlib.sha256(tag.encode("utf-8")).hexdigest()
    return "%sT%02d:%02d:00Z" % (day, int(digest[:2], 16) % 24,
                                 int(digest[2:4], 16) % 60)


def a_release(conn, tag):
    identity = "rel-replay-%s" % tag
    with conn.cursor() as cur:
        cur.execute("INSERT INTO admission_releases (release_identity)"
                    " VALUES (%s) ON CONFLICT DO NOTHING", (identity,))
    return identity


def an_exposure(conn, dateobs):
    """One `exposures` row, built from its actual NOT NULL columns.

    `fid` must reference a real `filters` row, so it is read rather than
    invented — a hard-coded 1 would pass on a seeded database and fail on a
    bare one, which is the kind of fixture that makes a suite unportable.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT fid FROM filters ORDER BY fid LIMIT 1")
        row = cur.fetchone()
        if row is None:
            pytest.skip("no filters rows on this database")
        fid = row[0]
        cur.execute(
            "INSERT INTO exposures"
            " (dateobs, field, fid, exptime, mjdobs, hp6, hp9)"
            " VALUES (%s, 1, %s, 100.0, 60000.0, 1, 1)"
            " ON CONFLICT (dateobs) DO UPDATE SET dateobs = EXCLUDED.dateobs"
            " RETURNING expid", (dateobs, fid))
        return cur.fetchone()[0]


FACTS = {"mjdobs": 60000.0, "field": 1, "hp6": 1, "hp9": 1,
         "exptime": 100.0, "infobits": 0, "status": 1}


def sealed_manifest(conn, repo, release, tag):
    manifest = repo.open_manifest("m-replay-%s" % tag, "socsim", release,
                                  "external-versioned")
    repo.add_manifest_entry(
        manifest.manifest_id, "roman-rapid-inputs-gbtds-sim",
        "sim/%s/file.fits" % tag, "a" * 64,
        source_version_id="objver-%s" % tag, source_bytes=1024)
    repo.seal_manifest(manifest.manifest_id)
    conn.commit()
    return manifest.manifest_id


def test_a_replay_reproduces_the_same_admissions_with_zero_new_rows():
    """The core replayability claim, asserted by counting.

    A replay from the sealed source manifest against a database ALREADY
    carrying the admissions must produce zero new rows and zero mutations —
    "a repeated observation returns its existing admission".
    """
    conn = fixture.connect()
    try:
        require_admission_schema(conn)
        tag = fixture.RUN_TAG + "-replay"
        release = a_release(conn, tag)
        repo = AdmissionRepository(conn)
        manifest_id = sealed_manifest(conn, repo, release, tag)

        dateobs = _dateobs("2026-03-01", tag)
        expid = an_exposure(conn, dateobs)
        first = repo.admit_exposure(dateobs=dateobs, expid=expid, facts=FACTS,
                                    release_identity=release,
                                    manifest_id=manifest_id)
        conn.commit()
        assert first.created is True

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM admission_exposures"
                        " WHERE manifest_id = %s", (manifest_id,))
            before = cur.fetchone()[0]

        # THE REPLAY: the same manifest, admitted again.
        second = repo.admit_exposure(dateobs=dateobs, expid=expid,
                                     facts=FACTS, release_identity=release,
                                     manifest_id=manifest_id)
        conn.commit()

        assert second.created is False, "a replay must not insert"
        assert second.admission_id == first.admission_id
        assert second.admission_identity == first.admission_identity
        # `admitted_at` UNCHANGED — the direct repair of addexposure's
        # `created = now()` overwrite, which destroys the original ingest
        # timestamp unrecoverably on every repeat.
        assert second.admitted_at == first.admitted_at

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM admission_exposures"
                        " WHERE manifest_id = %s", (manifest_id,))
            assert cur.fetchone()[0] == before, "zero new rows"
    finally:
        conn.close()


def test_replay_needs_no_source_bytes():
    """The recorded facts are sufficient — asserted with the source gone.

    A manifest of names and checksums could not reconstruct these rows,
    because the ingest derives them from FITS headers and WCS. So every parsed
    fact is stored on the admission, and this test replays with the source
    made unavailable: the manifest's entries are deleted outright, standing in
    for bytes that no longer exist, and the admission still resolves from what
    was recorded.
    """
    conn = fixture.connect()
    try:
        require_admission_schema(conn)
        tag = fixture.RUN_TAG + "-nobytes"
        release = a_release(conn, tag)
        repo = AdmissionRepository(conn)
        manifest_id = sealed_manifest(conn, repo, release, tag)

        dateobs = _dateobs("2026-03-02", tag)
        expid = an_exposure(conn, dateobs)
        first = repo.admit_exposure(dateobs=dateobs, expid=expid, facts=FACTS,
                                    release_identity=release,
                                    manifest_id=manifest_id)
        conn.commit()

        # THE SOURCE BECOMES UNAVAILABLE.
        with conn.cursor() as cur:
            cur.execute("DELETE FROM admission_manifest_entries"
                        " WHERE manifest_id = %s", (manifest_id,))
        conn.commit()

        # The recorded facts are still there, and still complete.
        with conn.cursor() as cur:
            cur.execute("SELECT admitted_facts FROM admission_exposures"
                        " WHERE admission_id = %s", (first.admission_id,))
            recorded = cur.fetchone()[0]
        if isinstance(recorded, str):
            recorded = json.loads(recorded)
        for key, value in FACTS.items():
            assert float(recorded[key]) == float(value), key

        # And the replay still returns the existing admission.
        again = repo.admit_exposure(dateobs=dateobs, expid=expid, facts=FACTS,
                                    release_identity=release,
                                    manifest_id=manifest_id)
        conn.commit()
        assert again.created is False
        assert again.admitted_at == first.admitted_at
    finally:
        conn.close()


def test_an_unsealed_manifest_is_refused():
    """A partial enumeration is never mistaken for a whole one.

    The crash-ordering guarantee: entries are written, then the manifest is
    sealed. An admission citing an unsealed manifest would be an admission
    against a source that may still be partial, so the trigger refuses it.
    """
    conn = fixture.connect()
    try:
        require_admission_schema(conn)
        tag = fixture.RUN_TAG + "-unsealed"
        release = a_release(conn, tag)
        repo = AdmissionRepository(conn)
        manifest = repo.open_manifest("m-unsealed-%s" % tag, "socsim",
                                      release, "none")
        repo.add_manifest_entry(manifest.manifest_id, "b", "k-%s" % tag,
                                "b" * 64)
        conn.commit()

        dateobs = _dateobs("2026-03-03", tag)
        expid = an_exposure(conn, dateobs)
        # COMMITTED BEFORE THE REFUSAL IS PROVOKED. The refusal aborts the
        # transaction and the `conn.rollback()` below discards everything
        # uncommitted in it — including the `exposures` row this admission's
        # FK needs. A first version left it uncommitted and the retry then
        # failed with `admission_exposures_expid_fk`, which reads as a schema
        # problem and is really a fixture-lifetime one.
        conn.commit()

        with pytest.raises(Exception) as caught:
            repo.admit_exposure(dateobs=dateobs, expid=expid, facts=FACTS,
                                release_identity=release,
                                manifest_id=manifest.manifest_id)
        assert "not sealed" in str(caught.value).lower()
        conn.rollback()

        # Sealed, the same admission succeeds — proving the refusal was about
        # the seal and not about anything else in the fixture.
        repo.seal_manifest(manifest.manifest_id)
        conn.commit()
        admitted = repo.admit_exposure(dateobs=dateobs, expid=expid,
                                       facts=FACTS,
                                       release_identity=release,
                                       manifest_id=manifest.manifest_id)
        conn.commit()
        assert admitted.created is True
    finally:
        conn.close()


def test_a_crash_leaves_a_complete_record_or_an_explicitly_unsealed_one():
    """The ordering, stated as an invariant over the two possible states.

    Sealing is the LAST write. So at any instant a manifest is either unsealed
    (and cites no admissions, because the trigger forbids it) or sealed (and
    every entry it describes is durable, because they were written first).
    There is no third state, and this asserts exactly that.
    """
    conn = fixture.connect()
    try:
        require_admission_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM admission_manifests m"
                " WHERE m.sealed_at IS NULL"
                "   AND (EXISTS (SELECT 1 FROM admission_exposures a"
                "                 WHERE a.manifest_id = m.manifest_id)"
                "     OR EXISTS (SELECT 1 FROM admission_l2files l"
                "                 WHERE l.manifest_id = m.manifest_id))")
            assert cur.fetchone()[0] == 0, (
                "an unsealed manifest has admissions citing it; the seal-last "
                "ordering has been broken")

            cur.execute(
                "SELECT count(*) FROM admission_manifests"
                " WHERE sealed_at IS NOT NULL"
                "   AND (entry_count IS NULL OR entries_checksum IS NULL)")
            assert cur.fetchone()[0] == 0, (
                "a sealed manifest is missing its entry count or checksum")
    finally:
        conn.close()


def test_the_manifest_pins_immutable_version_references():
    """Byte custody is STATED, not assumed (rule 20's durability clause)."""
    conn = fixture.connect()
    try:
        require_admission_schema(conn)
        tag = fixture.RUN_TAG + "-custody"
        release = a_release(conn, tag)
        repo = AdmissionRepository(conn)
        manifest_id = sealed_manifest(conn, repo, release, tag)
        entries = repo.manifest_entries(manifest_id)
        assert entries, "the manifest enumerated its sources"
        assert entries[0]["source_version_id"] == "objver-%s" % tag, (
            "the immutable version reference is what lets a replay name the "
            "exact bytes rather than whatever now sits at that key")

        with conn.cursor() as cur:
            cur.execute("SELECT byte_custody FROM admission_manifests"
                        " WHERE manifest_id = %s", (manifest_id,))
            custody = cur.fetchone()[0]
        assert custody in ("pipeline-retained", "external-versioned", "none")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The production call sites — asserted, not assumed.
# ---------------------------------------------------------------------------
INGEST_SCRIPTS = (
    "database/sims/db_register_socsim_files.py",
    "database/sims/db_register_rimtimsim_files.py",
    "database/sims/db_register_troxel_sim_files.py",
)


@pytest.mark.parametrize("script", INGEST_SCRIPTS)
def test_every_production_ingest_script_uses_the_carved_repository(script):
    """ALL THREE, asserted separately.

    The criterion cannot pass against an isolated repository while production
    still calls `RAPIDDB` alone, so this reads the actual scripts.
    """
    import os
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    with open(os.path.join(root, script), "r", encoding="utf-8") as handle:
        body = handle.read()
    assert "admission_bridge" in body, script
    assert "record_exposure_admission" in body, script


@pytest.mark.parametrize("script", INGEST_SCRIPTS)
def test_the_env_var_opt_out_is_gone_from_every_script(script):
    """`DONTCHECKALREADYINGESTED` no longer disables idempotency anywhere.

    Asserted as ABSENCE OF A LIVE READ rather than absence of the string: the
    socsim script documents the removal in a comment naming the variable, and
    a test that banned the name outright would forbid explaining why it went.
    What must not exist is an `os.getenv` of it.
    """
    import os
    import re
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    with open(os.path.join(root, script), "r", encoding="utf-8") as handle:
        body = handle.read()
    live_read = re.search(
        r"(getenv|environ\s*(\.get)?\s*[\(\[])\s*['\"]DONTCHECKALREADYINGESTED",
        body)
    assert live_read is None, (
        "%s still reads DONTCHECKALREADYINGESTED; admission idempotency must "
        "not be defeatable by an environment variable (rule 20)" % script)


def test_idempotency_survives_the_env_var_being_set():
    """Set it, re-run, still idempotent.

    The criterion's own wording. Setting the variable must now change nothing,
    because nothing reads it — asserted behaviourally rather than by trusting
    the grep above.
    """
    import os
    conn = fixture.connect()
    previous = os.environ.get("DONTCHECKALREADYINGESTED")
    os.environ["DONTCHECKALREADYINGESTED"] = "1"
    try:
        require_admission_schema(conn)
        tag = fixture.RUN_TAG + "-envvar"
        release = a_release(conn, tag)
        repo = AdmissionRepository(conn)
        dateobs = _dateobs("2026-03-04", tag)
        expid = an_exposure(conn, dateobs)
        first = repo.admit_exposure(dateobs=dateobs, expid=expid, facts=FACTS,
                                    release_identity=release)
        conn.commit()
        second = repo.admit_exposure(dateobs=dateobs, expid=expid,
                                     facts=FACTS, release_identity=release)
        conn.commit()
        assert second.created is False
        assert second.admission_id == first.admission_id
        assert second.admitted_at == first.admitted_at
    finally:
        if previous is None:
            os.environ.pop("DONTCHECKALREADYINGESTED", None)
        else:
            os.environ["DONTCHECKALREADYINGESTED"] = previous
        conn.close()
