"""Contract tests: the APPLICATION half of rule 18's preflight (brief H,
acceptance criterion 6).

Rule 18's last clause, verbatim: "Services and payloads preflight the
**application/schema** contract at startup." The schema half — a
one-directional migration floor — has existed since
`pipeline/intent/schema_contract.py` and is exercised by
`test_schema_preflight.py`. The application half is what brief H adds, and
what this file asserts.

**MOST OF THIS NEEDS NO DATABASE, AND THAT IS THE POINT.** The application
half is environment-driven: it reads `RAPID_RELEASE_IDENTITY` and
`RAPID_IMAGE_DIGEST` from a dict the caller passes in, and it compares two
strings under a direction flag. Asserting that against a real PostgreSQL
would add a dependency the behaviour does not have, and would make the most
important assertion in this file — the expand window — silently skippable on
any database that had not applied a draft. So the pure tests take no `conn`
fixture and run wherever the suite is collected. Only the one test that
genuinely reads `admission_releases` needs a database, and it skips cleanly
when DRAFT 051 is absent.

(Everything collected under this package is marked `contract` by
`conftest.py`'s `pytest_collection_modifyitems`, deliberately — a tier whose
membership depends on remembering to say so is a tier that leaks. The pure
tests here are therefore selected with the contract tier but do not require
its database, which costs nothing and keeps the marker rule intact.)

**WHY THE EXPAND WINDOW IS THE LOAD-BEARING ASSERTION.** Rule 18 requires
that during a deployment the schema may move first and OLD WORKERS' RESULTS
REMAIN ACCEPTABLE. A compatibility check written as an equality would satisfy
every obvious test — matching releases pass, mismatched ones fail — and would
refuse to start exactly the deployment step the rule mandates. The only test
that can tell a real direction-aware check from a disguised equality is one
that asserts the ASYMMETRIC case passes: old application, newer schema, no
exception. `test_an_old_application_against_a_newer_schema_is_allowed` below
is that test, and it is why this file exists.
"""

import pytest

from pipeline.contract import fixture
from pipeline.intent import application_contract
from pipeline.intent.application_contract import (IMAGE_DIGEST_ENV,
                                                  RELEASE_ENV,
                                                  ApplicationContractUnmet,
                                                  application_identity,
                                                  assert_compatible,
                                                  verify_application_contract)

#: Referenced through the module rather than imported by name purely to keep
#: the import block inside the line limit; it is the same class.
ApplicationSchemaIncompatible = (
    application_contract.ApplicationSchemaIncompatible)

#: A complete, plausible environment. Tests remove from it rather than
#: building up to it, so what is MISSING in each case is stated at the call
#: site instead of being implied by what was never added.
COMPLETE_ENV = {
    RELEASE_ENV: "h6-release-%s" % fixture.RUN_TAG,
    IMAGE_DIGEST_ENV: "sha256:" + "a" * 64,
}


def _env_without(*names):
    """`COMPLETE_ENV` minus these variables.

    A DICT IS PASSED IN, never `os.environ` mutated. The function takes an
    `environ` argument precisely so a test does not have to mutate process
    state — and a test that did would leak into every other test in the
    session, including the ones that pass an environment deliberately.
    """
    return {k: v for k, v in COMPLETE_ENV.items() if k not in names}


# ---------------------------------------------------------------------------
# 1 — an undeterminable application identity FAILS CLOSED
# ---------------------------------------------------------------------------
def test_a_missing_release_identity_fails_closed():
    """A process that cannot say what it is must not start.

    THE FAILURE THIS CATCHES is the quiet one: a service starting with no
    release identity looks perfectly healthy — the process is up, the journal
    is quiet — and every result it produces is unattributable. It cannot be
    reconciled against an `ExecutionBinding`, it cannot be rolled back from,
    and nobody finds out until someone asks which release produced a
    particular alert and the answer is that nothing recorded one. Failing at
    startup converts that into a deployment error an operator fixes in the
    minute they see it.

    The exception must NAME the missing variable: a fail-closed check whose
    message does not say what to set is a check an operator escalates rather
    than fixes.
    """
    with pytest.raises(ApplicationContractUnmet) as caught:
        verify_application_contract(environ=_env_without(RELEASE_ENV))

    assert RELEASE_ENV in str(caught.value), (
        "the refusal does not name the variable that is missing")
    assert RELEASE_ENV in caught.value.missing, (
        "the missing variable is not carried on the exception, so a caller "
        "cannot report it without parsing the message")


def test_an_empty_release_identity_fails_closed():
    """An empty string is not an identity.

    Checked separately from absence because the two arrive by different
    routes: an unset variable is a deployment that forgot the field, while
    `RAPID_RELEASE_IDENTITY=` is a deployment that set it from an empty
    template substitution — much easier to ship, and much harder to spot,
    because the variable IS present in the container's environment listing.
    """
    environ = dict(COMPLETE_ENV, **{RELEASE_ENV: ""})
    with pytest.raises(ApplicationContractUnmet) as caught:
        verify_application_contract(environ=environ)
    assert RELEASE_ENV in caught.value.missing


def test_a_whitespace_only_release_identity_fails_closed():
    """Whitespace is not an identity either.

    The check is `(value or "").strip()`, not `value is not None` — and this
    is the assertion that keeps it that way. A release identity of `" "`
    would otherwise pass the preflight, be stamped onto admissions, and
    compare unequal to every real release forever after.
    """
    environ = dict(COMPLETE_ENV, **{RELEASE_ENV: "   \t "})
    with pytest.raises(ApplicationContractUnmet) as caught:
        verify_application_contract(environ=environ)
    assert RELEASE_ENV in caught.value.missing

    # And the identity helper reports it as missing rather than handing back
    # a whitespace string a caller might log as though it meant something.
    identity, missing = application_identity(environ)
    assert identity["release_identity"] == ""
    assert RELEASE_ENV in missing


def test_a_complete_environment_passes_and_returns_the_identity():
    """The positive case, without which every refusal above is vacuous.

    A check that refused everything would pass all three tests above. It
    would also refuse to start every service in the system, which is why the
    return value is asserted too: the identity dict is what a caller logs, and
    a service that preflighted is distinguishable in the journal from one that
    did not.
    """
    identity = verify_application_contract(environ=dict(COMPLETE_ENV))
    assert identity["release_identity"] == COMPLETE_ENV[RELEASE_ENV]
    assert identity["image_digest"] == COMPLETE_ENV[IMAGE_DIGEST_ENV]


# ---------------------------------------------------------------------------
# 2 — require_image_digest=False relaxes the digest, never the release
# ---------------------------------------------------------------------------
def test_the_image_digest_may_be_waived_but_the_release_may_not():
    """`rapidctl`'s posture, asserted as two separate facts.

    `pipeline/operatorctl/main.py:_preflight` calls this with
    `require_image_digest=False` for a stated reason: `rapidctl` is an
    operator tool run from a shell, not a payload container, so requiring the
    container's own digest would refuse to start the tool for a fact it has no
    way to know.

    THE RISK IN THAT RELAXATION is that it becomes a way to switch the whole
    preflight off. So both halves are asserted together: the digest is
    genuinely optional under the flag, AND the release identity is still
    mandatory under the same flag. The second is the one that matters —
    the release is what decides what an admission gets stamped with, and a
    `rapidctl` that could move the release pointer without knowing its own
    release would be the exact hole this preflight was added to close.
    """
    # The digest is waived.
    identity = verify_application_contract(
        environ=_env_without(IMAGE_DIGEST_ENV), require_image_digest=False)
    assert identity["release_identity"] == COMPLETE_ENV[RELEASE_ENV]
    assert identity["image_digest"] == ""

    # The release is NOT waived by the same flag.
    with pytest.raises(ApplicationContractUnmet) as caught:
        verify_application_contract(
            environ=_env_without(RELEASE_ENV, IMAGE_DIGEST_ENV),
            require_image_digest=False)
    assert RELEASE_ENV in caught.value.missing
    assert IMAGE_DIGEST_ENV not in caught.value.missing, (
        "the waived digest was reported as missing anyway, which would make "
        "the flag do nothing")


def test_the_image_digest_is_required_by_default():
    """The default is the strict reading, for a payload container.

    Stated as its own test because "optional under a flag" and "optional"
    are one careless default apart, and every service other than `rapidctl`
    runs as a container that knows its own digest.
    """
    with pytest.raises(ApplicationContractUnmet) as caught:
        verify_application_contract(environ=_env_without(IMAGE_DIGEST_ENV))
    assert IMAGE_DIGEST_ENV in caught.value.missing


# ---------------------------------------------------------------------------
# 3 — THE EXPAND WINDOW. The most important assertion in this file.
# ---------------------------------------------------------------------------
def test_an_old_application_against_a_newer_schema_is_allowed():
    """**THE LEGAL EXPAND CASE — AND THE ASSERTION THAT PROVES THIS CHECK IS
    NOT A DISGUISED EQUALITY TEST.**

    Rule 18's requirement, verbatim, is the reason this case must pass:

        "Expand/contract migrations with a rollback window; OLD WORKERS'
         RESULTS REMAIN ACCEPTABLE DURING A DEPLOYMENT; work stays pinned to
         its release, and rollback changes only the release used for future
         admissions."

    An expand deployment moves the schema FIRST and leaves the old workers
    running. During that window an old application is talking to a newer
    schema BY DESIGN, its results are acceptable BY DESIGN, and a preflight
    that refused to start there would refuse exactly the deployment step the
    rule mandates. `schema_contract.py` models this by being a floor rather
    than an equality; this is the same asymmetry from the application side.

    WHY THIS TEST CANNOT BE OMITTED. Every other assertion in this file is
    also satisfied by a check written as `application_release ==
    admitted_release` — matching releases pass, mismatched ones raise, a
    missing identity fails closed. An equality would look correct, review
    correct, and quietly break every deployment. This is the ONLY assertion
    that distinguishes the two implementations, because it is the only case
    where a direction-aware check and an equality disagree: the releases
    differ, and the answer must still be yes.

    So: differing releases, `schema_is_newer=True`, and the required outcome
    is a plain `True` return with NO exception of any kind.
    """
    result = assert_compatible(application_release="r1",
                               admitted_release="r2",
                               schema_is_newer=True)
    assert result is True, (
        "the expand window was refused. An old application against a newer "
        "schema is the deployment step rule 18 requires to work — refusing "
        "it makes this check a disguised equality test, which is exactly "
        "what the flag exists to prevent")


def test_the_expand_window_holds_however_far_apart_the_releases_are():
    """The flag decides, not the strings' similarity.

    A check that allowed the expand window only for releases that looked
    related (a shared prefix, an adjacent version number) would be an
    equality test with a fuzzy comparator — still the wrong shape, and still
    liable to refuse a legitimate deployment on the day the naming scheme
    changes. The window is a property of the DEPLOYMENT STATE, which is what
    `schema_is_newer` carries.
    """
    assert assert_compatible("2024.11.0-rc4", "totally-unrelated-name",
                             schema_is_newer=True) is True
    assert assert_compatible("", "r2", schema_is_newer=True) is True


# ---------------------------------------------------------------------------
# 4 — the illegal direction IS refused
# ---------------------------------------------------------------------------
def test_a_release_mismatch_outside_an_expand_window_is_refused():
    """The other side of the asymmetry, without which allowing is meaningless.

    Outside a deployment window, an application running one release against
    work admitted under another is not expand/contract — it is a worker
    picking up work it is not pinned to, which is the rule-18 violation
    `pipeline/intent/admission_release.py` refuses per work unit. Here it is
    the process-level assertion of the same thing.

    A check that returned True unconditionally would pass every expand test
    above; this is what stops it.
    """
    with pytest.raises(ApplicationSchemaIncompatible) as caught:
        assert_compatible(application_release="r1", admitted_release="r2",
                          schema_is_newer=False)

    # BOTH releases are named. An operator reading this needs to know which
    # two things disagreed, not merely that something did — the same
    # discipline `ReleaseDisagreement` follows.
    message = str(caught.value)
    assert "r1" in message and "r2" in message


def test_the_default_direction_is_the_strict_one():
    """`schema_is_newer` defaults to False, so the permissive path is opt-in.

    A caller that forgets the flag gets the refusal, not the allowance. The
    expand window is a deliberate statement about deployment state, and a
    check that assumed it by default would permit the illegal direction every
    time a call site was written carelessly.
    """
    with pytest.raises(ApplicationSchemaIncompatible):
        assert_compatible("r1", "r2")


# ---------------------------------------------------------------------------
# 5 — matching releases pass in both directions
# ---------------------------------------------------------------------------
def test_matching_releases_pass_in_both_directions():
    """The ordinary steady state: one release, everywhere, in or out of a
    window.

    Asserted in both directions because the expand flag must not change the
    answer when there is nothing to disagree about. If it did, a deployment
    would be able to flip a passing check to a failing one without any
    release actually differing — a false alarm arriving in the middle of the
    one operation an operator most needs to be quiet.
    """
    assert assert_compatible("r1", "r1", schema_is_newer=False) is True
    assert assert_compatible("r1", "r1", schema_is_newer=True) is True

    # Nothing to compare is not a disagreement either: work with no admitted
    # release recorded is the pre-051 case, and the gate for THAT is
    # `admission_release.reconcile`'s `require_stamp`, not this check.
    assert assert_compatible("r1", None, schema_is_newer=False) is True
    assert assert_compatible(None, "r2", schema_is_newer=False) is True


# ---------------------------------------------------------------------------
# 6 — with a database: an unregistered release WARNS, and does not refuse
# ---------------------------------------------------------------------------
def test_an_unregistered_release_warns_but_does_not_refuse_startup(conn,
                                                                   caplog):
    """A release not yet in `admission_releases` is logged, never fatal.

    **THIS IS DELIBERATE, AND THE CODE SAYS SO.** Registration is an operator
    act. A release may legitimately start before it is registered, and
    refusing here would make the registration ORDER load-bearing for whether
    a service can boot — a deployment could then be blocked by a step that has
    nothing to do with whether the service works.

    The actual gate is in the right place already: `derived.
    set_admission_release` refuses to POINT admissions at an unregistered
    release (RA001, asserted in `test_admission_release.py`), so no admission
    can ever be stamped with one. Two gates for one fact would mean the
    weaker one eventually gets relaxed and the stronger one is assumed to
    have been the one that mattered.

    So this test exists to pin the NON-refusal: someone reading the warning
    in a journal will reasonably want to "fix" it by raising, and this states
    why that is the wrong repair.
    """
    import logging

    if not fixture.has_table(conn, "admission_releases"):
        pytest.skip(
            "DRAFT migration 051 (admission_releases) is not applied; the "
            "registration lookup cannot be exercised and skips cleanly")

    unregistered = "h6-unregistered-%s" % fixture.RUN_TAG
    environ = dict(COMPLETE_ENV, **{RELEASE_ENV: unregistered})

    # Confirm the premise rather than assuming it: a leftover row from an
    # earlier run under this tag would make the test pass vacuously.
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM admission_releases"
                    " WHERE release_identity = %s", [unregistered])
        assert cur.fetchone() is None, (
            "the supposedly-unregistered release is registered; this test "
            "would assert nothing")

    with caplog.at_level(logging.WARNING,
                         logger="rapid.intent.application_contract"):
        identity = verify_application_contract(
            execute=fixture.executor(conn), environ=environ)

    # IT RETURNED, which is the assertion. No exception, and the identity
    # comes back for the caller to log.
    assert identity["release_identity"] == unregistered

    # And it was LOUD about it — a silent pass here would leave an operator
    # with no signal at all that admissions under this release will be
    # refused until they register it.
    assert any(unregistered in record.getMessage()
               for record in caplog.records
               if record.levelno >= logging.WARNING), (
        "an unregistered release passed the preflight without a warning; the "
        "operator gets no signal that admissions under it will be refused")


def test_a_registered_release_passes_the_database_check_quietly(conn, caplog):
    """The registered case warns about nothing.

    The paired positive: a check that warned unconditionally would pass the
    test above and would train an operator to ignore the line, which is the
    same failure as not logging it at all.
    """
    import logging

    if not fixture.has_table(conn, "admission_releases"):
        pytest.skip("DRAFT migration 051 (admission_releases) is not applied")

    registered = "h6-registered-%s" % fixture.RUN_TAG
    environ = dict(COMPLETE_ENV, **{RELEASE_ENV: registered})
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO admission_releases (release_identity)"
                " VALUES (%s) ON CONFLICT (release_identity) DO NOTHING",
                [registered])
        conn.commit()

        with caplog.at_level(logging.WARNING,
                            logger="rapid.intent.application_contract"):
            identity = verify_application_contract(
                execute=fixture.executor(conn), environ=environ)

        assert identity["release_identity"] == registered
        assert not [record for record in caplog.records
                    if record.levelno >= logging.WARNING
                    and registered in record.getMessage()], (
            "a registered release produced a not-registered warning")
    finally:
        # Only this test's own row. Nothing truncates.
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM admission_releases"
                            " WHERE release_identity = %s", [registered])
            conn.commit()
        except Exception:                                # noqa: BLE001
            conn.rollback()


def test_the_database_check_is_skipped_where_the_draft_is_absent(conn):
    """No `admission_releases` table means no lookup, not a startup failure.

    The probe is `to_regclass(...) IS NOT NULL` inside
    `verify_application_contract`, and it exists so a service preflighting
    against a database WITHOUT the draft still starts. Asserted here against
    a real database because that probe is a PostgreSQL fact: a temp table
    shadowing the name would not change what `to_regclass('public....')`
    resolves to, so the honest test is simply that the call returns whatever
    the deployed database's state is, without raising either way.

    (This runs regardless of whether 051 is applied, which is the point: both
    branches of the probe must be non-fatal.)
    """
    identity = verify_application_contract(
        execute=fixture.executor(conn), environ=dict(COMPLETE_ENV))
    assert identity["release_identity"] == COMPLETE_ENV[RELEASE_ENV]


# ---------------------------------------------------------------------------
# 7 — the SCHEMA half still fails closed, legibly, with no broken database
# ---------------------------------------------------------------------------
def test_the_schema_half_fails_closed_naming_every_missing_migration():
    """Criterion 6's schema half, asserted without a database at all.

    `verify_schema_contract` takes a one-callable `execute(sql, params)`
    executor, so a deliberately short applied-migrations list is a two-line
    fake here rather than a database that has to be broken to be tested.
    `test_schema_preflight.py` asserts the same property against real
    PostgreSQL by shadowing `schema_migrations` with a temp table; this
    covers the SHAPE OF THE REFUSAL — that it names every gap and says why
    each one matters — and it covers it wherever the suite is collected,
    including where no draft is applied and the tier's database tests skip.

    **THIS FAKE IS NOT A CONTRACT-TIER STUB.** The tier's rule is that a
    double must be able to refuse what the live system refuses (`test_double_
    agreement.py`), and this executor refuses nothing — it is not standing in
    for PostgreSQL. It is a fixed INPUT to a pure function: the check's
    behaviour given a list of applied migrations is Python, and the list's
    provenance is asserted separately, live, in `test_schema_preflight.py`.
    Asserting the message shape against a live database would require
    breaking one, which is the thing the tier's fixture honesty forbids.

    THE PROPERTY UNDER TEST is that the failure is LEGIBLE. An operator
    reading a start failure at 3am needs every missing migration (not the
    first, which turns one restart into ten) and the reason each one is
    required (which tells them whether the migration step was skipped or this
    image is older than this database).
    """
    from pipeline.intent.schema_contract import (REQUIRED_MIGRATIONS,
                                                 SchemaContractUnmet,
                                                 verify_schema_contract)

    # The first two required migrations are recorded as applied; every later
    # one is not. A partial list rather than an empty one, so the assertion
    # is that the check reports EXACTLY the gap rather than the whole list —
    # the floor's whole value is telling an operator which step was skipped.
    applied = [name for name, _why in REQUIRED_MIGRATIONS[:2]]
    expected_missing = [(name, why) for name, why in REQUIRED_MIGRATIONS[2:]]

    def execute(_statement, _params=None):
        # Tuples, as psycopg2 hands them back — the row shape the real
        # executor produces, so the fake does not exercise a code path the
        # driver never takes.
        return [(name,) for name in applied]

    with pytest.raises(SchemaContractUnmet) as caught:
        verify_schema_contract(execute)

    assert list(caught.value.missing) == expected_missing, (
        "the refusal did not name exactly the missing migrations")
    assert caught.value.present_count == len(applied)

    message = str(caught.value)
    for name, why in expected_missing:
        assert name in message, "%s is not named in the failure" % name
        # THE REASON TRAVELS WITH THE NAME. A list of filenames tells an
        # operator what to run; the reason tells them what broke, which is
        # what decides whether this is a skipped step or a stale image.
        assert why in message, (
            "%s is named without the reason this build requires it" % name)

    # The remedy is named, because a start failure that does not say what to
    # run is a start failure an operator escalates instead of fixing.
    assert "apply-db-migrations.sh" in message


def test_the_schema_half_passes_on_a_surplus():
    """The floor allows a database AHEAD of this build — the expand window
    again, from the schema side.

    Kept in this file alongside the application-side expand test on purpose:
    criterion 6 is one contract with two halves, and the two halves must
    agree about the direction that is legal. If either one ever became an
    equality, a deployment would stall on the half that changed while the
    other passed — and the file that only tested one half would still be
    green.
    """
    from pipeline.intent.schema_contract import (REQUIRED_MIGRATIONS,
                                                 verify_schema_contract)

    applied = [name for name, _why in REQUIRED_MIGRATIONS]
    applied.append("099-a-migration-from-the-future.sql")

    def execute(_statement, _params=None):
        return [(name,) for name in applied]

    assert verify_schema_contract(execute) == len(REQUIRED_MIGRATIONS)
