"""Criterion 5's other half — the stamp must REACH the execution binding.

**WHY THIS FILE EXISTS SEPARATELY.** `test_admission_release.py` proves the
POINTER behaves: it switches, it supersedes, it audits, it refuses. That is
necessary and not sufficient. The brief is explicit that an isolated pointer
is a NON-FIX — "a worker can ship a pointer and a column that nothing reads
and the rule remains violated" — so what must also be proven is that work
derived from an admission carries THAT admission's release into its
`ExecutionBinding`, and that a disagreement with the submitting process's
environment is refused loudly rather than resolved.

Today, without this, `ExecutionBinding.release_identity` comes from the
process-wide `RAPID_RELEASE_IDENTITY`
(`pipeline/operator/submission.py:274`), and `pipeline/seams.py:375-401`
copies that binding onto the logical job and every attempt. So the release a
piece of work is pinned to is a property of WHICHEVER PROCESS HAPPENED TO
SUBMIT IT. These tests assert the replacement.

Mostly pure: `reconcile` and `binding_release_for_units` are functions of two
values plus a repository read, which is deliberate — it lets the rule be
exercised without an AWS environment.
"""

import pytest

from pipeline.contract import fixture
from pipeline.intent.admission_release import (ReleaseDisagreement,
                                               ReleaseStampMissing,
                                               binding_release_for_units,
                                               reconcile)


def _digest(tag):
    """A stable per-tag digest. `hash()` is salted per process."""
    import hashlib
    return hashlib.sha256(tag.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# The pure half — the reconciliation rule itself.
# ---------------------------------------------------------------------------
def test_the_admissions_release_is_what_the_binding_carries():
    """Agreement resolves to the admitted release, not to the environment.

    The values coincide here, so this asserts the direction of the answer
    rather than distinguishing the two sources — the disagreement tests below
    do that. It is worth stating anyway: the admitted release is the one
    returned, and a later refactor that returned the environment's value would
    still pass every other test in this file.
    """
    assert reconcile("rel-42", "rel-42") == "rel-42"


def test_a_disagreement_is_refused_loudly_and_names_both_values():
    """THE CLAUSE. Not resolved in favour of the environment — that is the
    current defect — and not silently in favour of the admission either, which
    would hide a submitter running the wrong image. Both are real conditions
    an operator must see, so both values appear in the refusal.
    """
    with pytest.raises(ReleaseDisagreement) as caught:
        reconcile("rel-admitted", "rel-environment")
    message = str(caught.value)
    assert "rel-admitted" in message
    assert "rel-environment" in message
    assert caught.value.admitted == "rel-admitted"
    assert caught.value.environment == "rel-environment"


def test_the_environment_never_silently_wins():
    """Stated as its own test because it is the failure mode being removed.

    A version of `reconcile` that preferred the environment would return
    "rel-environment" here instead of raising, and every OTHER test in this
    file would still pass. This is the one that catches it.
    """
    try:
        result = reconcile("rel-admitted", "rel-environment")
    except ReleaseDisagreement:
        return
    pytest.fail(
        "reconcile() returned %r instead of refusing: work derived from an "
        "admission must not silently take the submitting process's release "
        "(rule 18)" % (result,))


def test_an_unstamped_admission_fails_closed_by_default():
    """`require_stamp` defaults to the strict reading.

    An admission with no stamp predates DRAFT 051 or was written by a path
    that bypassed the repository; either way the release this work should be
    pinned to is UNKNOWN, and taking the environment's is exactly the gap
    rule 18 names.
    """
    with pytest.raises(ReleaseStampMissing):
        reconcile(None, "rel-environment")


def test_the_expand_fallback_is_explicit_and_never_a_default():
    """A deployment may submit pre-051 work — but must ASK to.

    `require_stamp=False` is the escape hatch, and it is a parameter rather
    than a default precisely because a quiet default here is the whole defect.
    """
    assert reconcile(None, "rel-environment",
                     require_stamp=False) == "rel-environment"


def test_a_missing_environment_release_is_a_deployment_fault():
    """No `RAPID_RELEASE_IDENTITY` at all: refused before anything else."""
    with pytest.raises(ReleaseStampMissing):
        reconcile("rel-admitted", None)
    with pytest.raises(ReleaseStampMissing):
        reconcile("rel-admitted", "")


# ---------------------------------------------------------------------------
# Linearization — one manifest carries one release.
# ---------------------------------------------------------------------------
def test_a_manifest_admitted_under_two_releases_is_refused():
    """A TORN MANIFEST IS A STATE NEITHER SIDE MAY PRODUCE.

    The admission side prevents it by reading the pointer ONCE per sealed
    manifest; this is the submission-side counterpart, and it refuses rather
    than picking one. Both sides check, because a manifest split across two
    releases is exactly what a pointer switch mid-run would cause and neither
    side alone can see the whole run.
    """
    def execute(sql, params=None):
        # Two units, two different admitted releases.
        if "admission_exposures" in sql:
            return [("rel-A",)] if params[0] == 1 else [("rel-B",)]
        return []

    with pytest.raises(ReleaseDisagreement) as caught:
        binding_release_for_units(
            execute, [("exposure", 1), ("exposure", 2)], "rel-A")
    assert "different releases" in str(caught.value)


def test_a_manifest_admitted_under_one_release_resolves_to_it():
    def execute(sql, params=None):
        if "admission_exposures" in sql:
            return [("rel-A",)]
        return []

    assert binding_release_for_units(
        execute, [("exposure", 1), ("exposure", 2)], "rel-A") == "rel-A"


def test_an_unknown_grain_is_refused_rather_than_ignored():
    """A grain this code does not understand is not silently skipped.

    Skipping it would mean its admitted release never entered the comparison,
    so a torn manifest could pass by virtue of one unit being unreadable.
    """
    from pipeline.intent.admission_release import ReleaseBindingError
    with pytest.raises(ReleaseBindingError):
        binding_release_for_units(lambda sql, params=None: [],
                                  [("sausage", 1)], "rel-A")


# ---------------------------------------------------------------------------
# The database half — the stamp is readable along the real chain.
# ---------------------------------------------------------------------------
@pytest.mark.contract
def test_the_stamp_is_readable_from_the_admission_row():
    """The stamp exists on the durable row, read by real SQL.

    `release_for_exposure` is what the submission path calls to find the
    release a piece of work must be pinned to, so it executes here against the
    real schema rather than against a fake — a column typo would pass a fake
    and fail here.
    """
    from pipeline.intent.admission_release import (release_for_exposure,
                                                   stamp_schema_present)
    conn = fixture.connect()
    try:
        if not fixture.has_table(conn, "admission_exposures"):
            pytest.skip("DRAFT 051 is not applied")
        execute = fixture.executor(conn)
        assert stamp_schema_present(execute) is True

        tag = fixture.RUN_TAG + "-chain"
        release = "rel-chain-%s" % tag
        with conn.cursor() as cur:
            cur.execute("INSERT INTO admission_releases (release_identity)"
                        " VALUES (%s) ON CONFLICT DO NOTHING", (release,))
            cur.execute("SELECT fid FROM filters ORDER BY fid LIMIT 1")
            row = cur.fetchone()
            if row is None:
                pytest.skip("no filters rows on this database")
            cur.execute(
                "INSERT INTO exposures"
                " (dateobs, field, fid, exptime, mjdobs, hp6, hp9)"
                " VALUES (%s, 1, %s, 100.0, 60000.0, 1, 1)"
                " ON CONFLICT (dateobs) DO UPDATE SET dateobs = "
                " EXCLUDED.dateobs RETURNING expid",
                # Zero-padded and derived from a STABLE digest, not `hash()`,
                # which is salted per process and so would differ between a
                # run and its re-run.
                ("2026-04-01T%02d:%02d:00Z"
                 % (int(_digest(tag)[:2], 16) % 24,
                    int(_digest(tag)[2:4], 16) % 60), row[0]))
            expid = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO admission_exposures"
                " (admission_identity, expid, release_identity,"
                "  admitted_facts)"
                " VALUES (%s, %s, %s, '{}'::jsonb)"
                " ON CONFLICT (admission_identity) DO NOTHING",
                ("sha256:" + ("e" * 64), expid, release))
        conn.commit()

        try:
            assert release_for_exposure(execute, expid) == release
            # And the full reconciliation, from the row: agreement passes,
            # disagreement with the environment refuses.
            assert reconcile(release_for_exposure(execute, expid),
                             release) == release
            with pytest.raises(ReleaseDisagreement):
                reconcile(release_for_exposure(execute, expid),
                          "rel-something-else")
        finally:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM admission_exposures WHERE expid = %s",
                            (expid,))
                cur.execute("DELETE FROM exposures WHERE expid = %s", (expid,))
                cur.execute("DELETE FROM admission_releases"
                            " WHERE release_identity = %s", (release,))
            conn.commit()
    finally:
        conn.close()


@pytest.mark.contract
def test_an_unstamped_exposure_reads_as_none_not_as_a_guess():
    """A missing stamp is None, which `reconcile` then fails closed on.

    The two halves are separate on purpose: the reader reports what is there,
    and the policy about what to do with an absence lives in one place.
    """
    from pipeline.intent.admission_release import release_for_exposure
    conn = fixture.connect()
    try:
        if not fixture.has_table(conn, "admission_exposures"):
            pytest.skip("DRAFT 051 is not applied")
        execute = fixture.executor(conn)
        assert release_for_exposure(execute, -999999) is None
        with pytest.raises(ReleaseStampMissing):
            reconcile(release_for_exposure(execute, -999999), "rel-env")
    finally:
        conn.close()
