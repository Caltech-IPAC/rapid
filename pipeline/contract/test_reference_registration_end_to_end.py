"""A whole reference-image registration, against the real stored functions.

**THE GAP THIS CLOSES.** `test_refimmeta_registration.py` proves the
`register_refimmeta` WRAPPER matches the function migration 128 declares. The
stub tier proves `register_reference_image` assembles its arguments from the
provenance keys `REFIMMETA_MEASUREMENTS` names. Neither covers the join: that
a value a reference-image STAGE recorded arrives in the database column it was
meant for, through the registrar, through four stored functions, on one
transaction. This file is that join.

It is the test that would have caught the `cattype` defect
(`pipeline/registration/products.py` records it): `refimcatalogs.cattype` is a
`smallint`, the registrar sent the STRINGS "sextractor" and "photutils",
PostgreSQL refused every reference-image registration at its first catalogue —
and the unit suite stayed green the whole time, because its fake database
accepts whatever it is handed. Four stored functions run here
(`addRefImage`, `updateRefImage`, `registerRefImCatalog`, `registerRefImMeta`),
so that entire family is exercised rather than just the newest one.

**THE RECORD IS IMPORTED, NOT REBUILT.** `reference_record` comes from the stub
suite, where it is constructed by `termination.build_terminal_record` from a
real `StageContext` — the same path the application authors a record by — and
where `RecordsProductionCanAuthorTests` asserts it carries every fact the body
needs. Writing a second record here would be exactly the failure that suite's
own docstring warns about: "a hand-written dict tests the registrar against a
record shape someone imagined", which is how it once passed green against a
registrar that could not have registered a single real attempt. One definition,
asserted for shape in one tier and for database agreement in this one.

**FLOAT COMPARISON IS APPROXIMATE FOR THE `real` COLUMNS AND EXACT OTHERWISE.**
Sixteen `refimmeta` columns are `real` (binary32), and the fixture's values are
the readable ones a stage would produce (0.33, 3.1, 8.4) rather than
binary32-exact ones. Demanding exact equality would make this suite fail on
float width rather than on anything about the registration — so `real` columns
compare with a relative tolerance well inside binary32's ~7 digits, while the
integer and `double precision` columns compare exactly, where a tolerance
would hide a real defect.
"""

import pytest

from database.modules.utils.rapid_db import RAPIDDB
from pipeline.contract import fixture
from pipeline.registration import products
from pipeline.registration.test.test_products import (REFERENCE_MEASUREMENTS,
                                                      reference_record)

pytestmark = pytest.mark.usefixtures("conn")

#: Column -> the provenance key whose value belongs in it, STATED HERE rather
#: than read from `products.REFIMMETA_MEASUREMENTS`.
#:
#: The duplication is deliberate and was earned: the first version of this file
#: read the mapping from the registrar and compared the row against it, which
#: made the test agree with ITSELF — swapping `gmin`/`gmax` in the registrar's
#: table passed green, because the expectation moved with the defect. A mapping
#: can only be tested against an independent statement of what it should be,
#: and this is that statement. `test_the_registrars_mapping_is_the_one_this_
#: file_asserts` keeps the two honest in the other direction, so a deliberate
#: change fails loudly here and names itself instead of silently weakening the
#: test below.
EXPECTED_MEASUREMENTS = (
    ("nframes", "reference_nframes"),
    ("mjdobsmin", "reference_mjdobsmin"),
    ("mjdobsmax", "reference_mjdobsmax"),
    ("npixnan", "reference_npixnan"),
    ("clmean", "reference_avg"),
    ("clstddev", "reference_std"),
    ("clnoutliers", "reference_noutliers"),
    ("gmedian", "reference_gmed"),
    ("datascale", "reference_datascale"),
    ("gmin", "reference_gmin"),
    ("gmax", "reference_gmax"),
    ("cov5percent", "reference_cov5percent"),
    ("medncov", "reference_medncov"),
    ("medpixunc", "reference_medpixunc"),
    ("fwhmmedpix", "fwhm_ref_medpix"),
    ("fwhmminpix", "fwhm_ref_minpix"),
    ("fwhmmaxpix", "fwhm_ref_maxpix"),
    ("nsxcatsources", "reference_sexcat_sources"),
    ("npucatsources", "reference_psfcat_sources"),
)

#: `refimmeta` columns typed `real`. Compared with a tolerance; everything
#: else is compared exactly. Derived from the table rather than hand-listed
#: would be better, but the list IS the schema's own and a drift here is
#: caught by the exact comparison failing loudly on a column that moved.
REAL_COLUMNS = frozenset({
    "clmean", "clstddev", "gmedian", "datascale", "gmin", "gmax",
    "cov5percent", "medncov", "medpixunc", "fwhmmedpix", "fwhmminpix",
    "fwhmmaxpix",
})


def _require_schema(conn):
    if not fixture.has_table(conn, "refimmeta"):
        pytest.skip(
            "rapid_systems migration 128 is not applied on this database; "
            "`refimmeta` and registerRefImMeta() arrive together in it")


def _attempt(conn):
    """A REAL `attempts` row, because `attempt_id` is a bigint.

    Found by this file's first run, and worth recording rather than quietly
    fixing: the first version passed a readable string (`att-<tag>`) and
    PostgreSQL refused it — `addRefImage` casts that argument to `bigint`,
    because `refimages.attempt_id` points at the attempts table's own key.
    The stub suite never notices, since its fake takes whatever it is given;
    a string would have been an `InvalidTextRepresentation` on the first live
    registration. Using the fixture's real row is both the fix and the more
    faithful thing: production's attempt_id is a row that exists.
    """
    return fixture.make_attempt(conn, lifecycle="terminal_without_start",
                                terminal_record_sequence=1)


def _register(conn, record, attempt_id=None, record_sequence=1):
    """Drive the production body on a borrowed handle.

    `RAPIDDB.borrowing` is what the registration consumer uses: the several
    writes and the watermark land in ONE transaction, so the handle's own
    `commit()` calls are suppressed and this test's rows roll back with the
    fixture connection.
    """
    handle = RAPIDDB.borrowing(conn)
    science = record["science_provenance"]
    registered = products.register_reference_image(
        handle, record, science,
        _attempt(conn) if attempt_id is None else attempt_id,
        record_sequence, run_id=None)
    return handle, registered


def _refimmeta_row(conn, rfid, columns):
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(columns)} FROM refimmeta WHERE rfid = %s",
            [rfid])
        return cur.fetchone()


def test_a_whole_registration_lands_every_row_it_promises(conn):
    """refimages, refimcatalogs and refimmeta, from one production call.

    The reference body's contract is that all three are written and keyed by
    the rfid the insert returned. Asserting the rows rather than the call
    sequence is what makes this different from the stub suite: there, a fake
    records the calls; here, PostgreSQL records the consequences.
    """
    _require_schema(conn)
    record = reference_record()
    handle, registered = _register(conn, record)

    assert handle.exit_code == 0, (
        "a database call reported exit_code %s during the registration"
        % handle.exit_code)
    rfid = registered["rfid"]

    with conn.cursor() as cur:
        cur.execute("SELECT vbest, filename FROM refimages WHERE rfid = %s",
                    [rfid])
        refimage = cur.fetchone()
        cur.execute("SELECT count(*) FROM refimcatalogs WHERE rfid = %s",
                    [rfid])
        catalogs = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM refimmeta WHERE rfid = %s", [rfid])
        meta = cur.fetchone()[0]

    assert refimage is not None, "no refimages row for the rfid just returned"
    assert refimage[0] == 1, (
        "vbest is %s, not 1 — update_refimage did not finalize this version "
        "as the current one" % refimage[0])
    assert refimage[1] == "s3://p/ref.fits"
    assert catalogs == 1, (
        "expected the SExtractor catalogue only (the record publishes no "
        "PhotUtils one), got %s refimcatalogs row(s)" % catalogs)
    assert meta == 1, "expected exactly one refimmeta row, got %s" % meta
    assert registered["refimmeta"] is True


def test_the_registrars_mapping_is_the_one_this_file_asserts():
    """The two statements of the mapping agree.

    Not ceremony: without this, a column added to the registrar's table would
    simply go untested here, and the suite would keep reporting green over a
    value nothing checks. With it, any change to `REFIMMETA_MEASUREMENTS` fails
    in a way that names exactly which pairs moved — and updating
    `EXPECTED_MEASUREMENTS` in the same commit is the deliberate act that
    records the author meant it.
    """
    expected, actual = set(EXPECTED_MEASUREMENTS), set(products.REFIMMETA_MEASUREMENTS)
    assert not expected - actual, (
        "this file expects pair(s) the registrar does not have: %r"
        % sorted(expected - actual))
    assert not actual - expected, (
        "the registrar has pair(s) this file does not assert: %r"
        % sorted(actual - expected))
    assert tuple(EXPECTED_MEASUREMENTS) == tuple(products.REFIMMETA_MEASUREMENTS), (
        "same pairs, different ORDER — the call is positional, so the order "
        "is the contract")


def test_every_measurement_reaches_the_column_it_was_recorded_for(conn):
    """Provenance key -> database column, through the whole chain.

    THE ASSERTION THIS FILE EXISTS FOR. The values come from the RECORD and the
    mapping from `EXPECTED_MEASUREMENTS` — this file's own statement of it, not
    the registrar's. Reading both from the registrar is what the first version
    did, and it passed with `gmin`/`gmax` swapped: the expectation moved with
    the defect. The fixture's values are all distinct precisely so a
    cross-wiring changes the observed value rather than shuffling equal ones.
    """
    _require_schema(conn)
    record = reference_record()
    science = record["science_provenance"]
    _handle, registered = _register(conn, record)

    columns = [column for column, _key in EXPECTED_MEASUREMENTS]
    row = _refimmeta_row(conn, registered["rfid"], columns)
    assert row is not None

    wrong = []
    for (column, key), actual in zip(EXPECTED_MEASUREMENTS, row):
        expected = science[key]
        ok = (actual == pytest.approx(expected, rel=1e-6)
              if column in REAL_COLUMNS else actual == expected)
        if not ok:
            wrong.append((column, key, expected, actual))
    assert not wrong, (
        "provenance did not reach the column it was recorded for: "
        + "; ".join("refimmeta.%s <- science[%r] expected %r got %r" % w
                    for w in wrong))


def test_both_catalogues_register_with_distinct_cattypes(conn):
    """The `cattype` family, against the smallint column it once broke on.

    `CATTYPE_SEXTRACTOR`/`CATTYPE_PHOTUTILS` are proposed values awaiting
    project ratification (`products.py` says so). What is asserted here is not
    that 1 and 2 are the right numbers — this tier cannot know that — but that
    they are values the column ACCEPTS and that the two catalogues land under
    DIFFERENT ones. `refimcatalogspk (rfid, ppid, cattype)` means a shared
    value would collide rather than silently duplicate, so this also proves
    the two are distinguishable at all.
    """
    _require_schema(conn)
    record = reference_record(with_psfcat=True)
    _handle, registered = _register(conn, record)

    with conn.cursor() as cur:
        cur.execute("SELECT cattype FROM refimcatalogs WHERE rfid = %s"
                    " ORDER BY cattype", [registered["rfid"]])
        cattypes = [r[0] for r in cur.fetchall()]

    assert cattypes == [products.CATTYPE_SEXTRACTOR, products.CATTYPE_PHOTUTILS], (
        "expected one row per catalogue under distinct cattypes, got %r"
        % (cattypes,))


def test_a_record_predating_the_measurements_still_registers(conn):
    """The allowance, against a real database rather than a fake.

    An attempt that completed under the previously pinned image carries an
    immutable record no re-run can add a fact to. The registrar writes its
    refimages and refimcatalogs rows and skips `refimmeta` — and the
    distinction that matters is that this is a SUCCESSFUL registration, not a
    partial one: the consumer advances its watermark on a return, so a
    refusal here would leave the attempt a candidate forever.
    """
    _require_schema(conn)
    record = reference_record(with_refimmeta=False)
    handle, registered = _register(conn, record)

    assert handle.exit_code == 0
    rfid = registered["rfid"]
    assert "refimmeta" not in registered

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM refimages WHERE rfid = %s", [rfid])
        assert cur.fetchone()[0] == 1, (
            "the reference image itself was not registered")
        cur.execute("SELECT count(*) FROM refimmeta WHERE rfid = %s", [rfid])
        assert cur.fetchone()[0] == 0, (
            "a refimmeta row was written from a record carrying none of the "
            "measurements")


def test_a_replayed_registration_writes_no_second_row_anywhere(conn):
    """Same attempt, same sequence, twice — one set of rows.

    Migration 018's idempotence reaches `refimmeta` for free, because the row
    is keyed by the rfid `addRefImage` finds rather than mints on replay. That
    is worth asserting rather than assuming: if the second pass minted a new
    version, the reference image would have two rfids and the metadata would
    silently describe only the newer one.
    """
    _require_schema(conn)
    record = reference_record()
    attempt_id = _attempt(conn)

    _h1, first = _register(conn, record, attempt_id=attempt_id,
                           record_sequence=1)
    _h2, second = _register(conn, record, attempt_id=attempt_id,
                            record_sequence=1)

    assert first["rfid"] == second["rfid"], (
        "the replay minted a new rfid (%s then %s); migration 018's "
        "find-or-insert on (attempt_id, record_sequence) did not hold"
        % (first["rfid"], second["rfid"]))
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM refimmeta WHERE rfid = %s",
                    [first["rfid"]])
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT count(*) FROM refimages WHERE attempt_id = %s",
                    [attempt_id])
        assert cur.fetchone()[0] == 1, (
            "the replayed pass wrote a second refimages row")
