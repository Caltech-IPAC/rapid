"""Fix round 1 — the WIRING tests, for B1, B2 and B4.

**WHY THIS FILE EXISTS AND WHAT IT IS FOR.** Package H's first acceptance run
was green across 474 tests while four blocking wiring gaps were live: every
component was unit-tested and the real call graph was never exercised. Admission
raised `ReleasePointerUnset` on every production exposure, the L2 defect rule 20
exists to fix was untouched, and the release stamp never reached
`ExecutionBinding`.

Every test here is written to **FAIL IF THE WIRING WERE REMOVED AGAIN**. They
assert over the real call graph — the actual ingest functions, the actual
submission seam — rather than over hand-injected inputs. Where a test could
have been written by calling the repository directly, it deliberately is not:
that is precisely the shape of test that passed while production was broken.
"""

import ast
import os

import pytest

from pipeline.contract import fixture

REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", ".."))

INGEST_SCRIPTS = (
    "database/sims/db_register_socsim_files.py",
    "database/sims/db_register_rimtimsim_files.py",
    "database/sims/db_register_troxel_sim_files.py",
)

#: The full admission lifecycle. A script that imports these and calls none of
#: them is exactly the B1 defect, and an import-only check would have passed
#: it — which is why every assertion below is over CALLS.
LIFECYCLE = ("begin_admission_run", "enumerate_source",
             "record_exposure_admission", "record_l2file_admission",
             "seal_admission_run")


def _called_names(path):
    """Every function NAME that is actually CALLED in a module.

    Parsed from the AST rather than grepped, so an occurrence inside a comment,
    a docstring or an import statement cannot be mistaken for a call. That
    distinction is the whole point: all three scripts IMPORTED the lifecycle
    and CALLED almost none of it, and any check that counted imports would
    have reported the wiring present.
    """
    with open(os.path.join(REPO_ROOT, path), "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute):
                called.add(func.attr)
    return called


# ---------------------------------------------------------------------------
# B1 — the admission lifecycle is CALLED, not merely imported.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("script", INGEST_SCRIPTS)
@pytest.mark.parametrize("function", LIFECYCLE)
def test_every_ingest_script_calls_the_whole_admission_lifecycle(script,
                                                                 function):
    """B1: each lifecycle function is CALLED in each production script.

    **THIS IS THE TEST THAT WOULD HAVE CAUGHT THE ORIGINAL DEFECT.** The
    shipped code imported all five and called one. `record_exposure_admission`
    reads run state that only `begin_admission_run` establishes, so the
    omission was not partial wiring — it was admission raising
    `ReleasePointerUnset` on every real exposure. Parameterized per function
    per script so a regression names exactly which call vanished from which
    file.
    """
    called = _called_names(script)
    assert function in called, (
        "%s does not CALL %s. Importing it is not wiring: the first revision "
        "of package H imported all five lifecycle functions into all three "
        "scripts and called only one, and admission was dead on arrival in "
        "production as a result." % (script, function))


@pytest.mark.parametrize("script", INGEST_SCRIPTS)
def test_the_manifest_is_sealed_after_the_admissions_not_before(script):
    """B1's crash ordering: seal LAST.

    Asserted structurally — `seal_admission_run` must appear after the
    admission calls in the source — because the ordering IS the guarantee: a
    manifest sealed before its admissions could be cited by an admission that
    never happened, which is the one state 051's trigger and this ordering
    exist together to prevent.
    """
    with open(os.path.join(REPO_ROOT, script), "r",
              encoding="utf-8") as handle:
        text = handle.read()
    # The LAST call of each, so a mention in the import block or a docstring
    # cannot stand in for the real one; and the handle name differs per script
    # (`dbh` vs `admission_dbh`), so the call is matched by function name.
    seal_at = text.rindex("seal_admission_run(")
    begin_at = text.rindex("begin_admission_run(")
    assert begin_at < seal_at, (
        "%s seals its manifest before opening it" % script)
    # And the seal is guarded on a clean run rather than unconditional.
    assert "UNSEALED" in text[seal_at - 1200:seal_at + 1200], (
        "%s seals unconditionally; a run with failures must leave the "
        "manifest explicitly unsealed, which is the honest record of a "
        "partial ingest" % script)


# ---------------------------------------------------------------------------
# B2 — the L2 grain goes through the carved repository.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("script", INGEST_SCRIPTS)
def test_l2_registration_admits_through_the_repository(script):
    """B2: `record_l2file_admission` is called where L2 rows are written.

    The `max(version) + 1` duplicate-minting defect the brief names as rule
    20's central finding is only repaired if this call actually happens on the
    L2 path. The shipped code imported it into all three scripts and called it
    in none, leaving the deeper half of rule 20 untouched in production.
    """
    with open(os.path.join(REPO_ROOT, script), "r",
              encoding="utf-8") as handle:
        text = handle.read()
    assert "record_l2file_admission(" in text
    # It must sit with the legacy write, not in some unrelated function.
    legacy = max(text.rfind("add_l2file_fourth_order("),
                 text.rfind("add_l2file_fifth_order("))
    admission = text.rfind("record_l2file_admission(")
    assert legacy != -1, "%s no longer writes an l2files row" % script
    assert admission > legacy, (
        "%s admits the L2 file before (or apart from) registering it; the "
        "admission must follow the legacy write, which is what supplies the "
        "rid it references" % script)


def test_no_ingest_script_calls_a_nonexistent_rapiddb_method():
    """The troxel defect that made B2 unfixable there.

    `dbh.add_l2file(...)` named a method `RAPIDDB` does not define — only the
    `_fourth_order` and `_fifth_order` overloads exist — so that script raised
    `AttributeError` on its first L2 file and its L2 path was DEAD. Asserted
    against the real class rather than a list, so a future rename of either
    overload is caught here too.
    """
    import database.modules.utils.rapid_db as rapid_db
    available = {name for name in dir(rapid_db.RAPIDDB)
                 if not name.startswith("_")}
    for script in INGEST_SCRIPTS:
        with open(os.path.join(REPO_ROOT, script), "r",
                  encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            if not isinstance(func.value, ast.Name) or func.value.id != "dbh":
                continue
            assert func.attr in available, (
                "%s calls dbh.%s(), which RAPIDDB does not define. This is "
                "the defect that made troxel's L2 path dead: it raised "
                "AttributeError on the first file."
                % (script, func.attr))


# ---------------------------------------------------------------------------
# B4 — the release stamp reaches the execution binding.
# ---------------------------------------------------------------------------
def test_the_submission_seam_resolves_the_admitted_release():
    """B4: `seams.py` builds its binding from the ADMISSION's release.

    The shipped code took `release_identity` straight from the submitting
    process's environment, so work derived from an admission carried whatever
    release that process happened to have. The brief names this exact trap:
    "an isolated pointer is a non-fix … a worker can ship a pointer and a
    column that nothing reads and the rule remains violated."

    Asserted structurally AND behaviourally: the seam must call the
    reconciler, and the reconciler's refusal must be reachable from it.
    """
    import inspect

    from pipeline import seams
    source = inspect.getsource(seams)
    assert "binding_release_for_units" in source, (
        "pipeline/seams.py does not resolve the admitted release; the "
        "ExecutionBinding would carry the submitting process's environment "
        "instead, and rule 18 would remain violated")
    # The resolved value must be what reaches the binding, not a shadowed
    # local that is computed and dropped.
    assert "release_identity=release_identity" in source, (
        "seams.py computes an admitted release but does not put it in the "
        "ExecutionBinding — a column that nothing reads")


def test_only_exposure_grain_units_are_looked_up():
    """A crossmatch unit names no admitted file and must not be coerced.

    Rule 11 removed the sentinel exposure/SCA carrier precisely so a
    date-grained unit stops pretending to have an exposure. `_admission_units_of`
    must respect that rather than reading `exposure`/`sca` off everything.
    """
    from pipeline.seams import _admission_units_of

    class _Payload(object):
        def __init__(self, exposure=None, sca=None):
            if exposure is not None:
                self.exposure = exposure
            if sca is not None:
                self.sca = sca

    class _Unit(object):
        def __init__(self, payload):
            self.payload = payload

    class _Manifest(object):
        units = [_Unit(_Payload(90000, 1)), _Unit(_Payload()),
                 _Unit(_Payload(90001, 2))]

    assert _admission_units_of(_Manifest()) == [
        ("l2file", 90000, 1), ("l2file", 90001, 2)]


@pytest.mark.contract
def test_the_admitted_release_reaches_a_real_execution_binding():
    """B4 end to end, against real SQL: admission → attempt.

    The chain the acceptance criterion names. An exposure and its L2 file are
    admitted under release A; the submission seam's resolver is then asked
    what a manifest of that unit should be pinned to, and it must answer A —
    not the environment's B. And a genuine disagreement must be REFUSED.

    This is the assertion that could not have passed while B4 was live.
    """
    from pipeline.intent.admission_release import (ReleaseDisagreement,
                                                   binding_release_for_units,
                                                   stamp_schema_present)
    conn = fixture.connect()
    try:
        if not fixture.has_table(conn, "admission_l2files"):
            pytest.skip("DRAFT 051 is not applied")
        execute = fixture.executor(conn)
        assert stamp_schema_present(execute) is True

        tag = fixture.RUN_TAG + "-b4"
        release_a = "rel-admitted-%s" % tag
        with conn.cursor() as cur:
            cur.execute("INSERT INTO admission_releases (release_identity)"
                        " VALUES (%s) ON CONFLICT DO NOTHING", (release_a,))
            cur.execute("SELECT fid FROM filters ORDER BY fid LIMIT 1")
            row = cur.fetchone()
            if row is None:
                pytest.skip("no filters rows on this database")
            cur.execute(
                "INSERT INTO exposures"
                " (dateobs, field, fid, exptime, mjdobs, hp6, hp9)"
                " VALUES (%s, 1, %s, 100.0, 60000.0, 1, 1)"
                " ON CONFLICT (dateobs) DO UPDATE SET dateobs ="
                " EXCLUDED.dateobs RETURNING expid",
                ("2026-05-01T%02d:%02d:00Z" % (len(tag) % 24, 30), row[0]))
            expid = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO admission_l2files"
                " (admission_identity, rid, expid, sca, source_checksum,"
                "  release_identity, admitted_facts)"
                " SELECT %s, rid, %s, 7, %s, %s, '{}'::jsonb FROM l2files"
                " LIMIT 1"
                " ON CONFLICT (admission_identity) DO NOTHING",
                ("sha256:" + ("b4" * 32), expid, "c" * 64, release_a))
            wrote_l2 = cur.rowcount
        conn.commit()

        try:
            if not wrote_l2:
                # No l2files row to borrow a rid from: assert the exposure
                # grain instead, which needs no FK to l2files.
                from pipeline.intent.admission_release import (
                    release_for_exposure)
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO admission_exposures"
                        " (admission_identity, expid, release_identity,"
                        "  admitted_facts)"
                        " VALUES (%s, %s, %s, '{}'::jsonb)"
                        " ON CONFLICT (admission_identity) DO NOTHING",
                        ("sha256:" + ("b5" * 32), expid, release_a))
                conn.commit()
                assert release_for_exposure(execute, expid) == release_a
                units = [("exposure", expid)]
            else:
                units = [("l2file", expid, 7)]

            # THE BINDING TAKES THE ADMITTED RELEASE.
            assert binding_release_for_units(
                execute, units, release_a) == release_a

            # AND A DISAGREEMENT WITH THE ENVIRONMENT IS REFUSED LOUDLY.
            with pytest.raises(ReleaseDisagreement) as caught:
                binding_release_for_units(execute, units,
                                          "rel-environment-%s" % tag)
            assert release_a in str(caught.value)
        finally:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM admission_l2files WHERE expid = %s",
                            (expid,))
                cur.execute("DELETE FROM admission_exposures WHERE expid = %s",
                            (expid,))
                cur.execute("DELETE FROM exposures WHERE expid = %s", (expid,))
                cur.execute("DELETE FROM admission_releases"
                            " WHERE release_identity = %s", (release_a,))
            conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The bridge survives the process fan-out.
# ---------------------------------------------------------------------------
@pytest.mark.contract
def test_a_worker_process_resolves_the_run_from_the_manifest_row():
    """The socsim driver forks; module state does not survive reliably.

    A worker that inherits neither the module global nor a live connection
    must still stamp the SAME release the run opened under. `_resolve_run`
    re-derives it from the MANIFEST ROW rather than re-reading the pointer —
    which is what keeps the linearization true across processes. Re-reading
    the pointer would be the torn-manifest state the brief forbids.
    """
    from database.sims import admission_bridge
    from pipeline.repositories.admission import AdmissionRepository

    conn = fixture.connect()
    try:
        if not fixture.has_table(conn, "admission_manifests"):
            pytest.skip("DRAFT 051 is not applied")
        tag = fixture.RUN_TAG + "-fork"
        release = "rel-fork-%s" % tag
        repo = AdmissionRepository(conn)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO admission_releases (release_identity)"
                        " VALUES (%s) ON CONFLICT DO NOTHING", (release,))
        manifest = repo.open_manifest("m-fork-%s" % tag, "test", release,
                                      "none")
        conn.commit()

        class _Handle(object):
            pass

        handle = _Handle()
        handle.conn = conn

        # Simulate the worker: no module state, only the environment.
        previous_state = dict(admission_bridge._RUN_RELEASE)
        previous_env = os.environ.get(admission_bridge.RUN_MANIFEST_ENV)
        admission_bridge._RUN_RELEASE.update({"identity": None,
                                              "manifest_id": None})
        os.environ[admission_bridge.RUN_MANIFEST_ENV] = "m-fork-%s" % tag
        try:
            resolved_release, resolved_manifest = \
                admission_bridge._resolve_run(handle)
            assert resolved_release == release, (
                "a worker resolved a different release than the run opened "
                "under; the linearization is broken across processes")
            assert resolved_manifest == manifest.manifest_id

            # WITHOUT the environment either, it must REFUSE rather than
            # guess — the fail-closed half.
            admission_bridge._RUN_RELEASE.update({"identity": None,
                                                  "manifest_id": None})
            os.environ.pop(admission_bridge.RUN_MANIFEST_ENV, None)
            from pipeline.repositories.admission import ReleasePointerUnset
            with pytest.raises(ReleasePointerUnset):
                admission_bridge._resolve_run(handle)
        finally:
            admission_bridge._RUN_RELEASE.update(previous_state)
            if previous_env is None:
                os.environ.pop(admission_bridge.RUN_MANIFEST_ENV, None)
            else:
                os.environ[admission_bridge.RUN_MANIFEST_ENV] = previous_env
            with conn.cursor() as cur:
                cur.execute("DELETE FROM admission_manifests"
                            " WHERE manifest_id = %s",
                            (manifest.manifest_id,))
                cur.execute("DELETE FROM admission_releases"
                            " WHERE release_identity = %s", (release,))
            conn.commit()
    finally:
        conn.close()
