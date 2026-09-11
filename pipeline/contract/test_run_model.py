"""Contract tests: the run model (migrations 108 `runs`, 109 run mutation
functions).

**THIS FILE IS THE PROOF, NOT A RESTATEMENT.** 108's whole point is that
coexistence is a STRUCTURAL FACT -- two partial unique indexes with mutually
exclusive predicates -- rather than a convention some caller could forget.
A structural fact can only be demonstrated by writing the rows and letting
PostgreSQL enforce (or fail to enforce) the index, which is why every
property below is asserted against the real migration stream and none of it
would mean anything against a fake.

  * COEXISTENCE (property 1) is the reason this sitting exists at all: two
    campaign runs holding a current product for the same identity group,
    neither superseding the other. Everything else here either protects that
    property (production currency unchanged, campaigns and production never
    collide) or protects the mechanisms it depends on (run-scoped work-unit
    identity, the prefix-overlap refusal that keeps a run's attempts
    findable, and archiving's demote-don't-delete contract).
  * `derived.create_run` and `derived.archive_run` are called through
    `pipeline.operatorctl.actions` -- the same call path `rapidctl run
    create`/`rapidctl run archive` use -- so these tests exercise the shipped
    entry point rather than a hand-written SELECT that happens to agree
    with it (the same discipline `test_operator_mutations.py` states for
    the other operate-tier actions).

No skip-if-absent guard: unlike DRAFT 047, migrations 108 and 109 are part
of THIS branch's authoritative stream, so a database built from it always
has `runs`, both product `run_id` columns, and both functions. A test here
failing to collect the schema is a real problem to report, not a
skip-worthy absence.
"""

import uuid

import pytest

from pipeline.contract import fixture
from pipeline.operatorctl import actions
from pipeline.operatorctl.contract import InvariantViolation


def _key(name):
    """An idempotency key unique to this test and this run of the suite."""
    return "run-model-%s-%s-%s" % (name, fixture.RUN_TAG, uuid.uuid4().hex[:8])


def _run_name(label):
    """A run name unique to this run of the suite and free of any run this
    suite has already declared prefix-overlapping with another.

    `fixture.RUN_TAG` is a 12-hex-digit tag with no `-` inside it, so
    appending `-<label>` can never itself create a prefix relationship
    between two names this helper mints -- each label is a fixed literal
    chosen per test, not a prefix of another label used in the same file.
    """
    return "runmodel-%s-%s" % (fixture.RUN_TAG, label)


def _declare_run(conn, name, kind, owner="run-model-tests"):
    """Create a run for real (`--apply`) and return its `run_id`.

    Every test that needs a run in the registry goes through here rather
    than a bare INSERT, so the tests exercise `derived.create_run` itself
    (the prefix-overlap check lives there, not in a CHECK constraint) on
    every run they touch, not only the ones naming that function directly.
    """
    result = actions.create_run(
        conn, _key("declare-%s" % name), name, owner, kind,
        reason="contract test fixture", dry_run=False)
    assert result["rows_affected"] == 1
    return result["run_id"]


def _make_run_scoped_diffimage(conn, run_id_name, field, ppid, sca=1):
    """A `diffimages` row at `vbest=1`, attributed to campaign run
    `run_id_name`.

    `fixture.make_diffimage` has no `run_id` parameter of its own -- it
    predates 108 -- so the run attribution is applied as a second statement
    once the row exists. This is a fixture convenience only: production
    writes `run_id` on the same INSERT that writes `attempt_id` (108's
    comment on `diffimages.run_id`), and that write path is not what this
    file is testing. What IS under test is what the two partial unique
    indexes do once the row carries a `run_id`, which is unaffected by
    which statement put it there.
    """
    attempt_id = fixture.make_attempt(conn, lifecycle="terminal_without_start")
    pid = fixture.make_diffimage(conn, attempt_id, field=field, ppid=ppid,
                                 vbest=1, sca=sca)
    with conn.cursor() as cur:
        cur.execute("UPDATE diffimages SET run_id = %s WHERE pid = %s",
                    [run_id_name, pid])
    conn.commit()
    return pid


def _make_run_scoped_refimage(conn, run_id_name, field, fid, ppid, tag):
    """A `refimages` row at `vbest=1` for `(field, fid, ppid)`, attributed to
    campaign run `run_id_name`. Returns its `rfid`.

    Built directly with `_insert_filling_required` (the same helper
    `fixture._diffimage_parents` uses for its own refimages row) rather than
    through a dedicated fixture helper, since none exists yet for a
    standalone, run-attributed reference image -- 108 predates one.
    """
    return fixture._insert_filling_required(
        conn, "refimages", "rfid",
        {"field": field, "fid": fid, "ppid": ppid, "version": 1, "vbest": 1,
         "run_id": run_id_name,
         "filename": f"ref/{fixture.RUN_TAG}/{tag}.fits"})


def _first_filter_id(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT fid FROM filters ORDER BY fid LIMIT 1")
        row = cur.fetchone()
    assert row is not None, (
        "no rows in `filters`; 009-seed-data.sql seeds them, so an empty "
        "table means the stream was not fully applied")
    return row[0]


# ---------------------------------------------------------------------------
# Property 1 -- coexistence: two campaign runs, one identity group, both
# current. THE test this whole migration pair exists to make possible.
# ---------------------------------------------------------------------------

def test_two_campaign_runs_each_hold_a_current_diffimage_for_one_identity(
        conn):
    """Two campaigns process the same (rid, ppid) at once; neither supersedes
    the other.

    Before 108, `diffimages_vbest_current_unique` was global on `(rid,
    ppid)` alone, so the second campaign's insert at `vbest=1` would have
    raised a unique violation on the first's row. Migration 108 replaces
    that index with a pair whose campaign half also carries `run_id`, so two
    campaigns' rows for the SAME `(rid, ppid)` no longer share a key. That
    structural change -- not a convention either campaign's code has to
    honour -- is what is asserted here: both inserts succeed, and both rows
    are still `vbest=1` afterward.
    """
    run_a = _run_name("coexist-diff-a")
    run_b = _run_name("coexist-diff-b")
    _declare_run(conn, run_a, "campaign")
    _declare_run(conn, run_b, "campaign")

    field = 991001
    ppid = 15

    # Both campaigns process the SAME field/ppid -- sharing `rid` is what
    # makes this the identity group the index guards, not two unrelated rows
    # that merely look alike.
    attempt_a = fixture.make_attempt(conn, lifecycle="terminal_without_start")
    pid_a = fixture.make_diffimage(conn, attempt_a, field=field, ppid=ppid,
                                   vbest=1)
    with conn.cursor() as cur:
        cur.execute("SELECT rid FROM diffimages WHERE pid = %s", [pid_a])
        rid = cur.fetchone()[0]
        cur.execute("UPDATE diffimages SET run_id = %s WHERE pid = %s",
                    [run_a, pid_a])
    conn.commit()

    # The second campaign's row for the SAME rid/ppid, at the SAME vbest.
    # `_insert_filling_required`'s version lookup would pick the next
    # version for this (rid, ppid) automatically; a second call to
    # `make_diffimage` does exactly that, so this row is a genuinely
    # different physical file at a later version, current under its own run.
    attempt_b = fixture.make_attempt(conn, lifecycle="terminal_without_start")
    pid_b = fixture._insert_filling_required(
        conn, "diffimages", "pid",
        {"rid": rid, "expid": _diffimage_expid(conn, pid_a),
         "sca": 1, "ppid": ppid,
         "version": _next_diffimage_version(conn, rid, ppid), "vbest": 1,
         "rfid": _diffimage_rfid(conn, pid_a), "field": field,
         "fid": _diffimage_fid(conn, pid_a),
         "filename": f"diffimages/{fixture.RUN_TAG}/coexist-b.fits",
         "attempt_id": attempt_b, "registered_record_sequence": 1,
         "run_id": run_b,
         "ra0": 10.0, "dec0": 10.0, "ra1": 10.0, "dec1": 10.0,
         "ra2": 10.1, "dec2": 10.0, "ra3": 10.1, "dec3": 10.1,
         "ra4": 10.0, "dec4": 10.1})
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT pid, run_id, vbest FROM diffimages"
                    " WHERE pid IN (%s, %s) ORDER BY pid", [pid_a, pid_b])
        rows = cur.fetchall()

    assert [r[2] for r in rows] == [1, 1], (
        "one of the two campaigns' rows was not left at vbest=1; "
        "coexistence failed")
    assert {r[1] for r in rows} == {run_a, run_b}, (
        "both rows must carry their own campaign's run_id")


def test_two_campaign_runs_each_hold_a_current_refimage_for_one_identity(
        conn):
    """The same coexistence property, for `refimages` on `(field, fid, ppid)`.

    `refimages_vbest_current_per_run_unique` is the campaign half of 108's
    second index pair; asserted separately from diffimages because it is a
    DIFFERENT index guarding a DIFFERENT identity group, and a bug specific
    to one table's index definition would not show up in the other's test.
    """
    run_a = _run_name("coexist-ref-a")
    run_b = _run_name("coexist-ref-b")
    _declare_run(conn, run_a, "campaign")
    _declare_run(conn, run_b, "campaign")

    field = 991002
    fid = _first_filter_id(conn)
    ppid = 12

    rfid_a = _make_run_scoped_refimage(conn, run_a, field, fid, ppid,
                                       "coexist-ref-a")
    conn.commit()
    rfid_b = _make_run_scoped_refimage(conn, run_b, field, fid, ppid,
                                       "coexist-ref-b")
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT rfid, run_id, vbest FROM refimages"
                    " WHERE rfid IN (%s, %s) ORDER BY rfid",
                    [rfid_a, rfid_b])
        rows = cur.fetchall()

    assert [r[2] for r in rows] == [1, 1], (
        "one of the two campaigns' reference images was not left at "
        "vbest=1; coexistence failed for refimages")
    assert {r[1] for r in rows} == {run_a, run_b}


# Small helpers used only by the diffimages coexistence test above, to read
# back the parent ids `make_diffimage` resolved on its first call so the
# second row can share them (same rid/expid/fid/rfid, different attempt and
# run) without duplicating `_diffimage_parents`' FK resolution.
def _diffimage_expid(conn, pid):
    with conn.cursor() as cur:
        cur.execute("SELECT expid FROM diffimages WHERE pid = %s", [pid])
        return cur.fetchone()[0]


def _diffimage_fid(conn, pid):
    with conn.cursor() as cur:
        cur.execute("SELECT fid FROM diffimages WHERE pid = %s", [pid])
        return cur.fetchone()[0]


def _diffimage_rfid(conn, pid):
    with conn.cursor() as cur:
        cur.execute("SELECT rfid FROM diffimages WHERE pid = %s", [pid])
        return cur.fetchone()[0]


def _next_diffimage_version(conn, rid, ppid):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT coalesce(max(version), 0) + 1 FROM diffimages"
            " WHERE rid = %s AND ppid = %s", [rid, ppid])
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Property 2 -- production currency is unchanged: 019's global rule, exactly
# as strict as before, for run_id IS NULL rows.
# ---------------------------------------------------------------------------

def test_two_production_diffimages_for_one_identity_still_collide(conn):
    """The production lane keeps 019's original guarantee, untouched by 108.

    Both rows here have `run_id IS NULL` (the production lane), so they are
    subject to `diffimages_vbest_current_unique` -- 108's narrowing of 019's
    index, predicate-restricted to `run_id IS NULL` but otherwise the same
    rule. A second `vbest=1` row for the same `(rid, ppid)` must still raise
    a unique violation; if it did not, 108 would have silently loosened the
    rule the whole operator-publish path depends on.
    """
    field = 991003
    ppid = 15

    attempt_a = fixture.make_attempt(conn, lifecycle="terminal_without_start")
    pid_a = fixture.make_diffimage(conn, attempt_a, field=field, ppid=ppid,
                                   vbest=1)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT rid, expid, fid, rfid FROM diffimages"
                    " WHERE pid = %s", [pid_a])
        rid, expid, fid, rfid = cur.fetchone()

    attempt_b = fixture.make_attempt(conn, lifecycle="terminal_without_start")
    with pytest.raises(Exception) as caught:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO diffimages"
                "  (rid, expid, sca, ppid, version, vbest, rfid, field, fid,"
                "   filename, attempt_id, registered_record_sequence,"
                "   ra0, dec0, ra1, dec1, ra2, dec2, ra3, dec3, ra4, dec4)"
                " VALUES (%s, %s, 1, %s, %s, 1, %s, %s, %s, %s, %s, 1,"
                "         10.0, 10.0, 10.0, 10.0, 10.1, 10.0, 10.1, 10.1,"
                "         10.0, 10.1)",
                [rid, expid, ppid,
                 _next_diffimage_version(conn, rid, ppid), rfid, field, fid,
                 f"diffimages/{fixture.RUN_TAG}/prod-collide.fits",
                 attempt_b])
    conn.rollback()

    assert getattr(caught.value, "pgcode", None) == "23505", (
        "expected a unique violation on diffimages_vbest_current_unique; "
        "got %r -- the production lane's currency rule is not as strict as "
        "019 made it" % caught.value)


def test_two_production_runs_for_one_work_unit_identity_still_collide(conn):
    """The other half of 'production is unchanged': `work_units` identity.

    Both units below have `run_id IS NULL`. `NULLS NOT DISTINCT` on 108's
    replacement index is what makes two NULLs collide instead of coexisting
    (the Postgres 15+ default is NULLS DISTINCT, under which two NULLs never
    collide) -- so this is the test that would fail first, and silently, if
    that clause had been dropped from the migration.
    """
    scope = fixture.scope("prod-identity-still-collides")
    unit_id = fixture.create_unit(conn, scope)
    assert unit_id is not None

    with pytest.raises(Exception) as caught:
        fixture.create_unit(conn, scope)
    conn.rollback()

    assert getattr(caught.value, "pgcode", None) == "23505", (
        "two production (job_type, input_scope, NULL run_id) units did not "
        "collide; NULLS NOT DISTINCT may be missing from "
        "work_units_current_identity_uq")


# ---------------------------------------------------------------------------
# Property 3 -- campaign and production never collide with each other.
# ---------------------------------------------------------------------------

def test_a_campaign_and_a_production_diffimage_coexist_for_one_identity(
        conn):
    """A campaign row and a production row for the same (rid, ppid) can both
    be current -- because the two indexes' predicates (`run_id IS NULL` vs
    `run_id IS NOT NULL`) are mutually exclusive, so no row is ever subject
    to both at once.
    """
    run_name = _run_name("mixed-diff")
    _declare_run(conn, run_name, "campaign")

    field = 991004
    ppid = 15

    attempt_prod = fixture.make_attempt(conn, lifecycle="terminal_without_start")
    pid_prod = fixture.make_diffimage(conn, attempt_prod, field=field,
                                      ppid=ppid, vbest=1)
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT rid, expid, fid, rfid FROM diffimages"
                    " WHERE pid = %s", [pid_prod])
        rid, expid, fid, rfid = cur.fetchone()

    attempt_camp = fixture.make_attempt(conn, lifecycle="terminal_without_start")
    pid_camp = fixture._insert_filling_required(
        conn, "diffimages", "pid",
        {"rid": rid, "expid": expid, "sca": 1, "ppid": ppid,
         "version": _next_diffimage_version(conn, rid, ppid), "vbest": 1,
         "rfid": rfid, "field": field, "fid": fid,
         "filename": f"diffimages/{fixture.RUN_TAG}/mixed-camp.fits",
         "attempt_id": attempt_camp, "registered_record_sequence": 1,
         "run_id": run_name,
         "ra0": 10.0, "dec0": 10.0, "ra1": 10.0, "dec1": 10.0,
         "ra2": 10.1, "dec2": 10.0, "ra3": 10.1, "dec3": 10.1,
         "ra4": 10.0, "dec4": 10.1})
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT pid, run_id, vbest FROM diffimages"
                    " WHERE pid IN (%s, %s) ORDER BY pid",
                    [pid_prod, pid_camp])
        rows = cur.fetchall()

    assert [r[2] for r in rows] == [1, 1], (
        "the production and campaign rows for the same identity group did "
        "not both stay current")
    run_ids = {r[1] for r in rows}
    assert None in run_ids and run_name in run_ids


# ---------------------------------------------------------------------------
# Property 4 -- work-unit identity is run-scoped.
# ---------------------------------------------------------------------------

def test_two_campaign_work_units_with_the_same_scope_coexist(conn):
    """Two DIFFERENT non-null run_ids sharing (job_type, input_scope) both
    live, non-superseded -- the mechanism that lets two campaigns claim the
    same input independently.
    """
    scope = fixture.scope("campaign-identity-coexists")
    run_a = _run_name("wu-a")
    run_b = _run_name("wu-b")

    unit_a = fixture._insert_filling_required(
        conn, "work_units", "work_unit_id",
        {"job_type": fixture.JOB_TYPE, "input_scope": scope,
         "run_id": run_a})
    conn.commit()
    unit_b = fixture._insert_filling_required(
        conn, "work_units", "work_unit_id",
        {"job_type": fixture.JOB_TYPE, "input_scope": scope,
         "run_id": run_b})
    conn.commit()

    assert unit_a != unit_b
    with conn.cursor() as cur:
        cur.execute("SELECT superseded_by_unit_id FROM work_units"
                    " WHERE work_unit_id IN (%s, %s)", [unit_a, unit_b])
        assert all(row[0] is None for row in cur.fetchall())


def test_two_production_work_units_with_the_same_scope_still_collide(conn):
    """Restated at the (job_type, input_scope) grain directly (property 2
    covers it through `fixture.create_unit`; this pins the same fact against
    a raw INSERT so the property is visible without going through the
    helper's own defaults).
    """
    scope = fixture.scope("production-identity-still-collides-2")

    fixture._insert_filling_required(
        conn, "work_units", "work_unit_id",
        {"job_type": fixture.JOB_TYPE, "input_scope": scope})
    conn.commit()

    with pytest.raises(Exception) as caught:
        fixture._insert_filling_required(
            conn, "work_units", "work_unit_id",
            {"job_type": fixture.JOB_TYPE, "input_scope": scope})
    conn.rollback()

    assert getattr(caught.value, "pgcode", None) == "23505"


# ---------------------------------------------------------------------------
# Property 5 -- `derived.create_run` refuses a prefix-overlapping name.
# ---------------------------------------------------------------------------

def test_create_run_refuses_a_name_that_is_a_prefix_of_an_existing_run(conn):
    """`alpha` exists; `alpha-two` overlaps it as a prefix and must be
    refused with RA011 (`InvariantViolation`), because every reader finds a
    run's attempts by `run_id LIKE name || '%'` and two overlapping names
    would be indistinguishable to that match.
    """
    base = _run_name("alpha")
    _declare_run(conn, base, "campaign")

    overlapping = base + "-two"
    with pytest.raises(InvariantViolation):
        actions.create_run(
            conn, _key("overlap-fwd"), overlapping, "run-model-tests",
            "campaign", reason="should be refused", dry_run=False)


def test_create_run_refuses_the_overlap_in_the_other_order_too(conn):
    """The reverse order: the SHORTER name arrives second, and is itself a
    prefix of the existing (longer) one. The overlap query's `OR` matches
    both directions, so this must refuse exactly like the forward case.
    """
    longer = _run_name("beta-two")
    _declare_run(conn, longer, "campaign")

    shorter = _run_name("beta")
    with pytest.raises(InvariantViolation):
        actions.create_run(
            conn, _key("overlap-rev"), shorter, "run-model-tests", "campaign",
            reason="should be refused", dry_run=False)


# ---------------------------------------------------------------------------
# Property 6 -- `derived.archive_run` deletes nothing and demotes only
# campaign products.
# ---------------------------------------------------------------------------

def test_archiving_a_campaign_run_demotes_its_current_products_without_deleting(
        conn):
    """vbest 1 -> 0, row count unchanged, run state becomes archived."""
    run_name = _run_name("archive-campaign")
    _declare_run(conn, run_name, "campaign")

    field = 991005
    ppid = 15
    pid = _make_run_scoped_diffimage(conn, run_name, field, ppid)

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM diffimages")
        count_before = cur.fetchone()[0]

    result = actions.archive_run(
        conn, _key("archive-campaign"), run_name, reason="test archival",
        dry_run=False)

    assert result["dry_run"] is False
    assert result["kind"] == "campaign"
    assert result["diffimages_demoted"] == 1
    assert result["nothing_deleted"] is True

    with conn.cursor() as cur:
        cur.execute("SELECT vbest FROM diffimages WHERE pid = %s", [pid])
        assert cur.fetchone()[0] == 0, "the campaign product was not demoted"
        cur.execute("SELECT count(*) FROM diffimages")
        count_after = cur.fetchone()[0]

    assert count_after == count_before, (
        "archiving changed the row count; archive_run must delete nothing")

    run = actions.run_row(conn, run_name)
    assert run["state"] == "archived"
    assert run["archived_at"] is not None


def test_archiving_a_production_run_leaves_its_products_published(conn):
    """A production run's products are never demoted by archiving: nothing
    would succeed them, so taking them out of currency would unpublish
    whatever the operator services are serving with no replacement.
    """
    run_name = _run_name("archive-production")
    _declare_run(conn, run_name, "production")

    field = 991006
    ppid = 15
    pid = _make_run_scoped_diffimage(conn, run_name, field, ppid)

    result = actions.archive_run(
        conn, _key("archive-production"), run_name, reason="test archival",
        dry_run=False)

    assert result["kind"] == "production"
    assert result["diffimages_demoted"] == 0, (
        "archiving a production run must report zero demotions")

    with conn.cursor() as cur:
        cur.execute("SELECT vbest FROM diffimages WHERE pid = %s", [pid])
        assert cur.fetchone()[0] == 1, (
            "archiving a production run demoted a published product")

    run = actions.run_row(conn, run_name)
    assert run["state"] == "archived"


def test_archiving_leaves_a_locked_best_row_alone_in_both_kinds(conn):
    """`vbest = 2` (locked best) survives archiving, campaign or production --
    an operator's explicit lock is not something an archive may overrule.
    """
    campaign = _run_name("archive-locked-campaign")
    production = _run_name("archive-locked-production")
    _declare_run(conn, campaign, "campaign")
    _declare_run(conn, production, "production")

    field_camp = 991007
    field_prod = 991008
    ppid = 15

    attempt_camp = fixture.make_attempt(conn, lifecycle="terminal_without_start")
    pid_camp = fixture.make_diffimage(conn, attempt_camp, field=field_camp,
                                      ppid=ppid, vbest=2)
    with conn.cursor() as cur:
        cur.execute("UPDATE diffimages SET run_id = %s WHERE pid = %s",
                    [campaign, pid_camp])
    conn.commit()

    attempt_prod = fixture.make_attempt(conn, lifecycle="terminal_without_start")
    pid_prod = fixture.make_diffimage(conn, attempt_prod, field=field_prod,
                                      ppid=ppid, vbest=2)
    conn.commit()

    result_camp = actions.archive_run(
        conn, _key("archive-locked-campaign"), campaign,
        reason="test archival", dry_run=False)
    result_prod = actions.archive_run(
        conn, _key("archive-locked-production"), production,
        reason="test archival", dry_run=False)

    assert result_camp["diffimages_demoted"] == 0, (
        "a locked-best (vbest=2) campaign row was counted as demoted")
    assert result_prod["diffimages_demoted"] == 0

    with conn.cursor() as cur:
        cur.execute("SELECT vbest FROM diffimages WHERE pid = %s", [pid_camp])
        assert cur.fetchone()[0] == 2, "archiving touched a locked campaign row"
        cur.execute("SELECT vbest FROM diffimages WHERE pid = %s", [pid_prod])
        assert cur.fetchone()[0] == 2, "archiving touched a locked production row"


# ---------------------------------------------------------------------------
# Property 7 -- dry run writes nothing.
# ---------------------------------------------------------------------------

def test_archive_run_dry_run_leaves_every_row_and_the_run_state_untouched(
        conn):
    """`p_dry_run => true` must report what it would do while changing
    nothing at all -- not the products, not the run's own state.
    """
    run_name = _run_name("archive-dryrun")
    _declare_run(conn, run_name, "campaign")

    field = 991009
    ppid = 15
    pid = _make_run_scoped_diffimage(conn, run_name, field, ppid)

    result = actions.archive_run(
        conn, _key("archive-dryrun"), run_name, reason="rehearsal only",
        dry_run=True)

    assert result["dry_run"] is True
    assert result["diffimages_demoted"] == 1, (
        "a dry run must still report what it WOULD demote")

    with conn.cursor() as cur:
        cur.execute("SELECT vbest FROM diffimages WHERE pid = %s", [pid])
        assert cur.fetchone()[0] == 1, "the dry run demoted a product"

    run = actions.run_row(conn, run_name)
    assert run["state"] == "created", "the dry run changed the run's state"
    assert run["archived_at"] is None
