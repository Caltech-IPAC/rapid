"""`registerRefImMeta` exercised against the function the database actually has.

**THE GAP THIS CLOSES.** `database/modules/utils/rapid_db.py`'s
`register_refimmeta` sends twenty-four values positionally into a stored
function declared by rapid_systems migration 128. The stub tier asserts the
argument order against the METHOD'S OWN SIGNATURE, which proves the call is
self-consistent — not that it matches the function the database has. Nothing
in this repository contains that function, so nothing outside this tier can.

That distinction is not hypothetical here. `pipeline/registration/products.py`
records the same class of defect landing in production: `refimcatalogs.cattype`
is a `smallint`, the registrar sent the STRINGS `"sextractor"` and
`"photutils"`, PostgreSQL refused every reference-image registration at its
first catalogue, and the unit suite was green throughout because its fake
database accepts whatever it is handed. A twenty-four argument positional call
is the same shape with more places to be wrong: a shifted argument, a cast that
does not match the parameter, a column renamed on one side only.

**WHY THE VALUES ARE ALL DISTINCT.** Every column below gets a different
number, and the row is read back column by column. Two columns sharing a value
would make a swap between them invisible — which is precisely the failure an
argument-order test exists to catch — so the fixture's distinctness is load-
bearing rather than cosmetic. The `real` columns take values exactly
representable in binary32 (`.25`, `.5`, `.75`) so a round-trip comparison is
exact and this suite never fails on float formatting.

**WHY exit_code IS ASSERTED, NOT JUST THE ROW.** `register_refimmeta` catches
`psycopg2.DatabaseError`, prints it, sets `exit_code = 67` and RETURNS — it
does not raise. So a signature mismatch does not blow up the test; it quietly
writes nothing. A test that only looked for the row would fail with "no row",
which reads like a missing fixture. Asserting `exit_code == 0` first is what
makes the failure say "the database refused the call".

**A REFUSED CALL LEAVES THE TRANSACTION ABORTED**, and the tests below take a
SAVEPOINT around every call they expect to fail. That is not a test
convenience — it is a property of the production path, found by writing these
tests. `register_refimmeta` catches the `DatabaseError` and returns, but
PostgreSQL has already put the transaction into the aborted state, so the next
statement on that connection raises `InFailedSqlTransaction` no matter what it
is. The registration path is safe from this only because
`products._check` raises `RegistrationFailed` immediately after each call and
the consumer's error path rolls back; a future caller that "handled"
`exit_code` 67 by carrying on would meet a wall of unrelated-looking errors.
`test_a_refused_call_leaves_the_transaction_aborted` asserts it so the
constraint is stated rather than rediscovered.

What this file does NOT cover: the registrar's own assembly of those twenty-
four arguments (`products._refimmeta_measurements` and its
`REFIMMETA_MEASUREMENTS` table) is asserted in the stub tier, against the
mapping it declares. The two together cover the chain from provenance key to
database column; neither covers it alone.
"""

import pytest

from database.modules.utils.rapid_db import RAPIDDB
from pipeline.contract import fixture

pytestmark = pytest.mark.usefixtures("conn")


#: The measurements, in the order `register_refimmeta` takes them after its
#: five identity arguments — the order `products.REFIMMETA_MEASUREMENTS`
#: produces. Column name -> value, every value distinct.
MEASUREMENTS = (
    ("nframes", 25),
    ("mjdobsmin", 60000.5),
    ("mjdobsmax", 60010.25),
    ("npixnan", 17),
    ("clmean", 1.25),
    ("clstddev", 2.5),
    ("clnoutliers", 42),
    ("gmedian", 3.75),
    ("datascale", 4.25),
    ("gmin", -5.5),
    ("gmax", 6.75),
    ("cov5percent", 87.25),
    ("medncov", 7.5),
    ("medpixunc", 8.75),
    ("fwhmmedpix", 9.25),
    ("fwhmminpix", 10.5),
    ("fwhmmaxpix", 11.75),
    ("nsxcatsources", 1234),
    ("npucatsources", 1100),
)

#: The same columns, with a second distinct value each, for the replay case.
#: Different from the first set in EVERY position: a replay that updated only
#: some columns would otherwise pass on the ones it happened not to touch.
AMENDED = (
    ("nframes", 30),
    ("mjdobsmin", 60001.5),
    ("mjdobsmax", 60011.75),
    ("npixnan", 19),
    ("clmean", 12.25),
    ("clstddev", 13.5),
    ("clnoutliers", 44),
    ("gmedian", 14.75),
    ("datascale", 15.25),
    ("gmin", -16.5),
    ("gmax", 17.75),
    ("cov5percent", 91.25),
    ("medncov", 18.5),
    ("medpixunc", 19.75),
    ("fwhmmedpix", 20.25),
    ("fwhmminpix", 21.5),
    ("fwhmmaxpix", 22.75),
    ("nsxcatsources", 1300),
    ("npucatsources", 1150),
)


def _require_schema(conn):
    if not fixture.has_table(conn, "refimmeta"):
        pytest.skip(
            "rapid_systems migration 128 is not applied on this database; "
            "`refimmeta` and registerRefImMeta() arrive together in it")


def _field():
    """A run-unique field, so two runs of this suite never share a row.

    `refimmeta`'s primary key is the `rfid`, and `refimagespk` is UNIQUE on
    `(field, fid, ppid, version)` — the same fixture-honesty rule the rest of
    this tier follows: no truncation, unique tags, so a second run cannot be
    confused by the first run's rows.
    """
    return int(fixture.RUN_TAG[:6], 16)


def _register(handle, rfid, fid, field, measurements):
    """One call, exactly as `register_reference_image` makes it."""
    handle.register_refimmeta(rfid, fid, field, 11, 111,
                              *[value for _column, value in measurements])


def _savepoint(conn, name):
    with conn.cursor() as cur:
        cur.execute(f"SAVEPOINT {name}")


def _rollback_to(conn, name):
    """Clear the aborted state without discarding the fixture rows.

    A plain `rollback()` would take the `refimages` row with it and the
    follow-up assertions would then be about an empty database rather than
    about the refusal.
    """
    with conn.cursor() as cur:
        cur.execute(f"ROLLBACK TO SAVEPOINT {name}")


def _row(conn, rfid, columns):
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(columns)} FROM refimmeta WHERE rfid = %s",
            [rfid])
        return cur.fetchone()


def test_every_argument_lands_in_the_column_it_was_meant_for(conn):
    """The twenty-four-argument call, against the deployed function.

    The assertion is column by column rather than "a row exists": the point is
    WHICH column each value reached, and a row-count check would pass on a
    call whose arguments were rotated by one.
    """
    _require_schema(conn)
    field = _field()
    rfid, fid = fixture.make_refimage(conn, field)
    handle = RAPIDDB.borrowing(conn)

    _register(handle, rfid, fid, field, MEASUREMENTS)

    assert handle.exit_code == 0, (
        "register_refimmeta reported exit_code %s — the database refused the "
        "call. This is the signature check: the wrapper sends 24 values with "
        "explicit casts into registerRefImMeta(), and a parameter list that "
        "does not match migration 128's is exactly what this looks like."
        % handle.exit_code)

    columns = ["rfid", "fid", "field", "hp6", "hp9"]
    columns += [column for column, _value in MEASUREMENTS]
    row = _row(conn, rfid, columns)
    assert row is not None, (
        "no refimmeta row for rfid %s, though the call reported success" % rfid)

    expected = [rfid, fid, field, 11, 111]
    expected += [value for _column, value in MEASUREMENTS]
    mismatched = [(c, e, a) for c, e, a in zip(columns, expected, row) if e != a]
    assert not mismatched, (
        "value(s) reached the wrong column: "
        + "; ".join("%s expected %r got %r" % m for m in mismatched))


def test_a_second_registration_updates_rather_than_inserting(conn):
    """Replay corrects the row it already wrote; it does not add a second.

    The registration consumer re-runs a candidate whenever its watermark write
    did not commit, which is a normal outcome of a crash between the rows and
    the watermark. `registerRefImMeta` is find-then-branch rather than a bare
    INSERT for that reason, and `refimmeta_pkey` would refuse the second
    insert — so this asserts the branch is taken, not that the constraint
    exists.
    """
    _require_schema(conn)
    field = _field()
    rfid, fid = fixture.make_refimage(conn, field)
    handle = RAPIDDB.borrowing(conn)

    _register(handle, rfid, fid, field, MEASUREMENTS)
    assert handle.exit_code == 0
    _register(handle, rfid, fid, field, AMENDED)
    assert handle.exit_code == 0, (
        "the replayed registration reported exit_code %s; a second call for "
        "one rfid must update, and a failure here means it attempted an "
        "insert against the primary key" % handle.exit_code)

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM refimmeta WHERE rfid = %s", [rfid])
        assert cur.fetchone()[0] == 1, (
            "two refimmeta rows for one reference image")

    columns = [column for column, _value in AMENDED]
    row = _row(conn, rfid, columns)
    expected = [value for _column, value in AMENDED]
    stale = [(c, e, a) for c, e, a in zip(columns, expected, row) if e != a]
    assert not stale, (
        "the replay left the first call's value(s) in place: "
        + "; ".join("%s expected %r got %r" % m for m in stale))


def test_metadata_for_an_unregistered_reference_image_is_refused(conn):
    """The `rfid` FK is what makes this one-row-per-reference-image.

    Asserted through the wrapper rather than with a raw INSERT, because the
    property that matters to the registrar is that the refusal arrives as
    `exit_code` 67 — which `products._check` turns into `RegistrationFailed`,
    leaving the attempt a candidate. A wrapper that swallowed the error and
    reported success would advance the watermark over a row that was never
    written.
    """
    _require_schema(conn)
    field = _field()
    _rfid, fid = fixture.make_refimage(conn, field)
    with conn.cursor() as cur:
        cur.execute("SELECT coalesce(max(rfid), 0) + 1000000 FROM refimages")
        orphan = cur.fetchone()[0]

    handle = RAPIDDB.borrowing(conn)
    _savepoint(conn, "before_orphan")
    _register(handle, orphan, fid, field, MEASUREMENTS)
    exit_code = handle.exit_code
    _rollback_to(conn, "before_orphan")

    assert exit_code == 67, (
        "metadata for rfid %s — which has no refimages row — was accepted "
        "(exit_code %s); refimmeta_rfid_fk is not enforcing, or the wrapper "
        "is not reporting the refusal" % (orphan, exit_code))


def test_a_half_known_row_is_refused_by_the_column(conn):
    """A NULL measurement cannot be written.

    128 declares all 23 columns NOT NULL, including the three 006 left
    nullable (`mjdobsmin`, `mjdobsmax`, `cov5percent`). The registrar refuses
    to build a half-known row — `_refimmeta_measurements` raises
    `MissingRecordFact` — and this is the database's half of that same
    refusal, which is what makes it hold for any writer, not just that one.
    """
    _require_schema(conn)
    field = _field()
    rfid, fid = fixture.make_refimage(conn, field)
    holed = tuple((column, None if column == "cov5percent" else value)
                  for column, value in MEASUREMENTS)

    handle = RAPIDDB.borrowing(conn)
    _savepoint(conn, "before_holed")
    _register(handle, rfid, fid, field, holed)
    exit_code = handle.exit_code
    _rollback_to(conn, "before_holed")

    assert exit_code == 67, (
        "a row with a NULL cov5percent was accepted (exit_code %s); the NOT "
        "NULL migration 128 declares is not enforcing" % exit_code)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM refimmeta WHERE rfid = %s", [rfid])
        assert cur.fetchone()[0] == 0, (
            "a refimmeta row exists for rfid %s after a refused call" % rfid)


def test_a_refused_call_leaves_the_transaction_aborted(conn):
    """The failure mode a caller must not try to continue through.

    `register_refimmeta` reports through `exit_code` rather than by raising,
    which reads like "carry on and check the code" — but PostgreSQL has
    already aborted the transaction by then, so the NEXT statement fails too,
    whatever it is, with an error naming nothing to do with reference images.

    This is why `products._check` raises immediately after every rapid_db call
    rather than accumulating failures: the connection is unusable from the
    refusal onward, and the registrar's remaining writes would each produce a
    misleading error instead of the real one. Stated here as an assertion so
    the next person to add a call site reads it as a rule rather than
    discovering it from a confusing failure.
    """
    import psycopg2

    _require_schema(conn)
    field = _field()
    rfid, fid = fixture.make_refimage(conn, field)
    holed = tuple((column, None if column == "cov5percent" else value)
                  for column, value in MEASUREMENTS)

    handle = RAPIDDB.borrowing(conn)
    _savepoint(conn, "before_abort_probe")
    _register(handle, rfid, fid, field, holed)
    assert handle.exit_code == 67

    with pytest.raises(psycopg2.errors.InFailedSqlTransaction):
        with conn.cursor() as cur:
            cur.execute("SELECT 1")

    _rollback_to(conn, "before_abort_probe")
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone()[0] == 1, (
            "the connection did not recover after ROLLBACK TO SAVEPOINT")
