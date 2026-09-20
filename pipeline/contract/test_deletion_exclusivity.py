"""Acceptance criteria 11 and 12 — one deletion route, and the frozen class.

Both are assertions over the SOURCE TREE rather than over a database, so
neither needs the `contract` marker. They are the two regressions this arc has
most reason to fear: a second deletion route appearing quietly, and a new
method landing on `RAPIDDB` (which the D, F and E workers each did, and each
needed a fix round for).
"""

import os
import subprocess

REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", ".."))


def test_object_deletion_is_reachable_only_from_the_gc_executor():
    """Criterion 11 — deletion has exactly one route in production code.

    **THE SCOPE OF THIS ASSERTION IS STATED HONESTLY IN ITS OWN FAILURE
    MESSAGE AND HERE.** It proves exclusivity WITHIN THIS REPOSITORY'S
    PRODUCTION CODE. It does NOT prove it for the deployed system: S3
    lifecycle expiry rules delete objects outside this mechanism and live in
    `rapid_systems` bucket configuration, which this package may not edit.

    Verified read-only against `rapid_systems` on this branch, those rules are
    on `roman-rapid-logs`, `roman-rapid-diagnostics`, `roman-rapid-meta` and
    `roman-rapid-build` — all OUTSIDE the declared GC scope, which is the
    products bucket alone. `rapid-product-buckets.yaml` carries no
    `LifecycleConfiguration` at all. So the residual gap is narrower than the
    brief anticipated, but it is still a gap, and rule 21 scores PARTIAL —
    pending CR on that basis.

    A passing result here must never be reported as system-wide exclusivity.
    """
    from pipeline.gc.exclusivity import assert_single_deletion_route
    assert assert_single_deletion_route(REPO_ROOT) is True


def test_the_approved_exclusions_are_enumerated_not_pattern_matched():
    """The exclusions are a list, so a new one cannot appear silently.

    `scripts/` and `*/test/` harness cleanup removes each run's OWN S3 staging
    prefix under the build-artifacts bucket — never a product, never a bucket
    in the declared scope. Enumerating the directories rather than matching a
    substring means a deletion added to production code cannot hide behind a
    filename that merely contains "test".
    """
    from pipeline.gc import exclusivity
    assert "test" in exclusivity.EXCLUDED_DIRECTORIES
    assert "scripts" in exclusivity.EXCLUDED_DIRECTORIES
    # The scanner must actually FIND the harness deletions it is excluding —
    # an exclusion list that excluded nothing would mean the pattern had
    # stopped matching and the assertion had quietly become vacuous.
    hits = subprocess.run(
        ["grep", "-rln", "aws s3 rm", "--include=*.sh", "scripts", "pipeline",
         "submission", "database"],
        cwd=REPO_ROOT, capture_output=True, text=True)
    assert hits.stdout.strip(), (
        "the harness cleanup deletions have vanished from the tree; either "
        "they moved (update the exclusions) or this assertion has gone "
        "vacuous")


def test_the_gc_executor_is_the_module_named_by_the_assertion():
    """The one permitted module exists and is the one that deletes."""
    from pipeline.gc import exclusivity
    target = os.path.join(REPO_ROOT, exclusivity.GC_EXECUTION_MODULE)
    assert os.path.isfile(target), exclusivity.GC_EXECUTION_MODULE
    with open(target, "r", encoding="utf-8") as handle:
        body = handle.read()
    assert "delete_version" in body


# ---------------------------------------------------------------------------
# Criterion 12 — the frozen class.
# ---------------------------------------------------------------------------
RAPID_DB = os.path.join("database", "modules", "utils", "rapid_db.py")


def _public_methods(text):
    methods = set()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("def ") and not stripped.startswith("def _"):
            name = stripped[4:].split("(", 1)[0].strip()
            methods.add(name)
    return methods


#: The sha256 of `database/modules/utils/rapid_db.py` AS RATIFIED.
#: **RECORDED RATHER THAN COMPUTED FROM GIT**, and that is the point: the
#: acceptance host stages a TARBALL, not a clone, so every git-based form of
#: this assertion SKIPS there — which is precisely the venue where it most
#: needs to run, and a skipped criterion proves nothing (the PASS2 zero-skip
#: gate exists because of exactly this failure mode).
#:
#: A recorded digest needs no history, no remote ref and no working tree, so
#: it runs identically in CI, on rapid-admin and on a laptop. Updating it is
#: a deliberate act that shows up in a diff — which is the correct amount of
#: friction for editing a frozen class.
#:
#: **THE COMPARISON IS UNCONDITIONAL (2026-08-15).** It used to be a FALLBACK
#: taken only where git history was unavailable, and that made the guard
#: vacuous in the venue that matters most: on a push to `smdc`,
#: `merge-base HEAD origin/smdc` is HEAD itself, so the method-set diff is
#: always empty and the assertion always passes. A method added ON `smdc` was
#: therefore invisible to this guard forever after, and the digest that would
#: still have caught it was never reached. Four methods accumulated exactly
#: that way (ratified below). The digest check now runs in EVERY venue and the
#: git diff is an ADDITIONAL check layered on top, not the only one — so an
#: addition fails on `smdc` too, which is what "frozen" was supposed to mean.
#:
#: **RATIFICATION, 2026-08-15.** This digest is no longer the branch point's.
#: It records `rapid_db.py` as it stands at `smdc` @ 95614085, which includes
#: four public methods added after the branch point (`smdc` @ 066c353) and
#: deployed for some time before anyone noticed the guard had stopped
#: guarding:
#:
#:   * `classify_claim_outcome`, `classify_confirm_outcome`  — `bcc989cb`
#:   * `get_info_for_l2files`,
#:     `get_scas_with_completed_catalog_load_for_processing_date` — `ada293c8`
#:
#: They are ratified as-deployed rather than carved out: both commits predate
#: the correctness campaign, the methods are in live use, and carving them out
#: now would be a behavioural change made to satisfy a guard that was not
#: enforcing at the time. The names are listed HERE, and in
#: `rapid_plan/decisions.md`, so the ratification is evidence rather than an
#: erasure — updating the digest without naming what it absorbs is exactly the
#: move this comment exists to prevent. The previous value, for the record,
#: was `665a8a2e0c6fa45ef7575a4a6e7270db59e41dffc9ab5d102f9eb09785164dba`.
#:
#: FROM NOW ON any further addition fails in every venue. The next person to
#: change this constant should have a ratified decision to cite, and should
#: list what it absorbs the same way.
#:
#: **RE-RATIFICATION, 2026-09-12 — the convergence merge.** The ratified
#: decision cited is the project's convergence ruling of 2026-09-12: `smdc`
#: becomes the team branch, carrying `run-overhaul` and `origin/smdc`
#: together. Both parents had ALREADY changed this frozen file independently
#: before the merge, so the previous digest matched neither parent and could
#: not be restored by any merge resolution — it described a state that no
#: longer existed on either side. Re-freezing at the merged file is therefore
#: not a waiver of the friction: it records the state the team actually
#: receives, and the friction resumes from here.
#:
#: WHAT THE NEW DIGEST ABSORBS, named rather than elided — two public methods,
#: measured as the set difference against the 2026-08-15 ratified state
#: (`smdc` @ 95614085), no method removed:
#:
#:   * `add_psf`, `update_psf` — `63cb2322` (registration through addPSF and
#:     updatePSF), reworked by `a75e30b2` and `ceefb25b`. They predate this
#:     merge on `run-overhaul` and are in live use on the PSF registration
#:     path; carving them out now would be a behavioural change made to
#:     satisfy a guard that was not enforcing across the branch split.
#:
#: The remaining difference is not a method-set change at all: `overlapfields`
#: is threaded through `add_l2file_fourth_order` and `add_l2file_fifth_order`,
#: which is a signature change on two methods that already existed, required
#: by rapid_systems migration 102.
#:
#: The previous value, for the record, was
#: `474cc92e27265dec8b27e3c195acd159c8af44767923bf79abdfb8bcf4069487`.
#:
#: **RE-RATIFICATION, 2026-09-12 evening — the run-aware reference lookup.**
#: The ratified decision cited is the owner's approval of 2026-09-12 20:07 PDT
#: (the pre-Prove review's recommendation, campaign `handoff-2026-09-13`,
#: `LEDGER-fix-reference-lookup.md`): `get_best_reference_image` gains a
#: `run_id` parameter and an `ORDER BY`. Since rapid_systems migration 115 a
#: production current reference and a scratch run's own current reference
#: for the same (ppid, field, fid) coexist legally, and the unscoped,
#: unordered `fetchone()` handed a run whichever the planner emitted first.
#: The fix ranks the caller's own run first, the production lane second, and
#: never another run's row (`df88939d`).
#:
#: WHAT THE NEW DIGEST ABSORBS: no public method added, none removed. One
#: signature change on an existing method — `get_best_reference_image(ppid,
#: field, fid, run_id=None)` — and the query text behind it. The previous
#: value, for the record, was
#: `d7517bb3b4972a24a4965ab94671144875426233edc2bcaf0e719a0872523c89`.
#:
#: **RE-RATIFICATION, 2026-09-14 — the reference-set model.** The ratified
#: decision cited is the owner's approval of the baseline audit's landing
#: path, 2026-09-14 16:58 PDT ("proceed as you think best"), under which the
#: `reference-sets` branch lands on `smdc`. A reference set is now a named,
#: first-class row: a run declares the set it reads, and every reference
#: lookup is scoped to that set instead of to an implicit production lane.
#: This is the `rapid_db.py` half of rapid_systems migrations 126 and 127.
#:
#: WHAT THE NEW DIGEST ABSORBS: two public methods added —
#: `get_default_reference_set()` and `get_reference_set_by_name(name)`, both
#: plain named-record reads against the tables 126 creates. None removed.
#: Three signature changes on existing methods, each taking the set to read:
#: `get_best_psf(sca, fid, reference_set_id)`,
#: `get_best_reference_image(ppid, field, fid, reference_set_id)` — which
#: REPLACES the `run_id=None` parameter the 2026-09-12 re-ratification above
#: added, the reference set now carrying the scoping that `run_id` stood in
#: for — and `update_refimage(..., *, run_id, reference_set_id=None)`. The
#: query text behind all three changed with them. The previous value, for
#: the record, was
#: `6d6beab99839d3e4a0f45ed8677626b02198ffc585629c9c39eb05e9d650044d`.
#:
#: **RE-RATIFICATION, 2026-09-15 — the reference-image metadata row.** The
#: ratified decision cited is the owner's instruction of 2026-09-15: add the
#: `refimmeta` table and populate a record in it each time a new reference
#: image is generated. This is the `rapid_db.py` half of rapid_systems
#: migration 128, which reinstates the table 038 dropped as unwritten — the
#: paired change is what makes it written.
#:
#: WHAT THE NEW DIGEST ABSORBS: **no public method added and none removed.**
#: `register_refimmeta` already existed on this class, carried forward from
#: the pre-SMDC lineage and dead since 038 dropped the table it wrote. One
#: signature change on it, and the query text behind it, to 128's column set:
#: `npixsat` is gone (the reference-image stage still measures it and still
#: records `reference_npixsat` in the attempt's provenance — it is simply not
#: a column of this table), `nsexcatsources` is spelled `nsxcatsources`, and
#: `npucatsources` is new. The body also moves off the `TEMPLATE_` regex
#: substitution the W3 sweep removed everywhere else and onto bound
#: parameters, which is the shape every other writer on this class already
#: has.
#:
#: WHY THIS IS AN EDIT HERE AND NOT A CARVED REPOSITORY (rule 17's own
#: instruction): the row must be written inside the SAME transaction as the
#: `refimages` row and its catalogues — that is the registrar's contract —
#: and the registrar holds one `RAPIDDB` on a borrowed connection for all of
#: them. A repository owns its own connection, so writing this one row
#: through one would put it outside the transaction the rest of the
#: registration commits as a unit, which is a correctness change made to
#: satisfy a packaging rule. The rule's target is NEW database access; this
#: is an existing writer being pointed at the table it was always for.
#:
#: The previous value, for the record, was
#: `ebcda29520b85fcc3ebe35175330d82c93b8568d2ffeefd6cf7cc44376eaa330`.
#:
#: **RE-RATIFICATION, 2026-09-15 — three fixes to existing methods, no new
#: capability.** Ratified by Emily Everetts for the smdc-port-followups
#: branch on the strength of the 2026-09-09 diff-level audit of the
#: reconcile merge (memory: ben-reconcile-audit-2026-09-09). The digest
#: absorbs exactly:
#:
#:   * `_register_numpy_adapters` — the three numpy float types are
#:     registered through psycopg2's `Float` adapter instead of
#:     `AsIs(repr(float(value)))`. The latter (d4b7baa7) rendered NaN and
#:     Infinity as the bare SQL tokens `nan`/`inf`; `numpy.float64` had
#:     adapted correctly through the subclass fallback before that
#:     registration overrode it. Reproduced and re-tested against the real
#:     driver (`test_rapid_db_numpy_adapters_contract.py`).
#:   * `execute_sql_queries` — the empty-list guard (6a2cce9a) returns
#:     `None` like the query-failure path, not `[]`, which no caller could
#:     tell from "found nothing"; the duplicated inner `records = []` is
#:     removed (dev 0cad34b9 MOVED the line; 6a2cce9a added a second copy).
#:   * `get_overlapping_l2files` — the docstring states the ordering the
#:     query has had since 87156b56 (mjdobs, then distance) instead of
#:     "ordered by distance from tile center".
#:
#: No public method is added, removed or re-signed. The branch-diff form of
#: the second test below is expected to report "modified on this branch"
#: until this lands on `smdc`; that is the deliberate friction working, not
#: a defect to route around. The previous value, for the record, was
#: `1cc5f3949f9edf00d3e2bee8dcd728e633b9656fca5a160b3eed12b31369912b`.
#:
#: **RE-RATIFICATION, 2026-09-20 — the scratch tier's constrained writes, no
#: new capability.** Ratified by Ben through the launching session across
#: 21:48, 22:03, 23:13 and 01:03 PDT, each widening recorded in the run
#: ledger with its scope. The digest absorbs exactly ONE change, applied to
#: nine EXISTING methods:
#:
#:   * `add_refimage`, `update_refimage`, `add_diffimage`,
#:     `update_diffimage`, `add_psf`, `update_psf`,
#:     `register_refimcatalog`, `register_refimmeta`,
#:     `register_diffimmeta` — each selects
#:     `derived.scratch_<name>` instead of the bare function when the write
#:     belongs to a scratch run, and each gained the trailing keyword
#:     arguments that dispatch needs. The bare functions are
#:     INVOKER-RIGHTS, so the scratch service identity — which holds no
#:     table privilege at all (rapid_systems migration 132) — cannot use
#:     them; the SECURITY DEFINER wrappers are the only route that works
#:     for it, and each refuses a run whose kind is not `scratch`.
#:
#: The rule this freeze protects is that NEW database access becomes a
#: carved repository rather than another RAPIDDB method. That rule is
#: intact: verified mechanically against the branch point e566af94, this
#: branch adds NO public method and removes none. The production path is
#: byte-for-byte what it was — same statement text, same casts, same
#: parameter order — because every dispatch defaults to it.
#:
#: The previous value, for the record, was
#: `8d7dc0fc5038783f8cc2c74bdaf2225ab9b0af7497e82d47bee4dde1b0d3d376`.
RAPID_DB_BRANCH_POINT_SHA256 = (
    "5627340ca5679f8c3071b06d265f1e191f843f8b4d005c74bd3f228182af305d")


def _file_digest(path):
    import hashlib
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def test_rapiddb_gains_no_new_method_on_this_branch():
    """Criterion 12 — asserted MECHANICALLY, not by review.

    `RAPIDDB` is frozen (rule 17; brief G's ratified merge decision), and this
    is the arc's most repeated regression: the D, F and E workers each added a
    method to this class and each needed a fix round to carve it back out. An
    assertion is what stops a fourth occurrence being found at a merge gate.

    **THE DIGEST IS CHECKED IN EVERY VENUE, AND IT IS CHECKED FIRST.** This
    used to run the git method-set diff and RETURN on success, reaching the
    digest only where history was unavailable. That made the guard vacuous
    exactly where it mattered: on a push to `smdc`, `merge-base HEAD
    origin/smdc` is HEAD itself, the diff is empty by construction, and the
    assertion passed no matter what the file contained. A method added ON
    `smdc` was invisible to it forever after — four accumulated that way (see
    RAPID_DB_BRANCH_POINT_SHA256's ratification note). The digest now runs
    unconditionally; the method-set diff is an ADDITIONAL check that names
    WHICH methods were added when history can say so.
    """
    current = os.path.join(REPO_ROOT, RAPID_DB)
    assert os.path.isfile(current), RAPID_DB

    # (1) THE UNCONDITIONAL CHECK. No history, no remote ref and no working
    #     tree required, so it is identical in CI, on rapid-admin and on a
    #     laptop — and, critically, identical on `smdc` and on a branch.
    assert _file_digest(current) == RAPID_DB_BRANCH_POINT_SHA256, (
        "%s differs from its RATIFIED digest; RAPIDDB is frozen (rule 17). "
        "New database access is a carved repository under "
        "pipeline/repositories/ — connection owned by the caller, "
        "named-record returns, typed errors. If this file was legitimately "
        "changed by a ratified decision, update "
        "RAPID_DB_BRANCH_POINT_SHA256 in the same commit AND list what the "
        "new digest absorbs — the friction is deliberate." % RAPID_DB)

    # (2) THE ADDITIONAL CHECK, where history can name names. Strictly
    #     weaker than (1) — anything it catches, (1) has already caught —
    #     but its failure message says WHICH methods were added, which is
    #     the difference between "the digest moved" and a fix you can act
    #     on. Deliberately no `return`: (1) has already run.
    branch_point = subprocess.run(
        ["git", "merge-base", "HEAD", "origin/smdc"],
        cwd=REPO_ROOT, capture_output=True, text=True)
    if branch_point.returncode == 0:
        base = branch_point.stdout.strip()
        before = subprocess.run(["git", "show", "%s:%s" % (base, RAPID_DB)],
                                cwd=REPO_ROOT, capture_output=True, text=True)
        if before.returncode == 0:
            with open(current, "r", encoding="utf-8") as handle:
                after_text = handle.read()
            added = _public_methods(after_text) - _public_methods(
                before.stdout)
            assert not added, (
                "RAPIDDB is FROZEN (rule 17) and this branch adds %d public "
                "method(s) to it: %s. New database access is a carved "
                "repository under pipeline/repositories/ — connection owned "
                "by the caller, named-record returns, typed errors. The D, F "
                "and E workers each broke this and each needed a fix round."
                % (len(added), sorted(added)))


def test_rapid_db_is_byte_identical_with_the_branch_point():
    """The stronger form: this branch does not touch the file at all.

    Stronger than the method-set assertion and kept alongside it deliberately:
    the method-set check would pass an edit that changed a method's BODY,
    which is still an edit to a frozen class.

    **SAME VENUE FIX AS ABOVE (2026-08-15).** The digest comparison was the
    fallback arm and is now unconditional. The `git diff origin/smdc...HEAD`
    form it used to short-circuit on is empty by construction for a commit
    that IS `origin/smdc`, so on every push to `smdc` this test asserted
    nothing at all.
    """
    current = os.path.join(REPO_ROOT, RAPID_DB)

    # (1) THE UNCONDITIONAL CHECK — byte identity against the ratified digest,
    #     in every venue including a push to `smdc`.
    assert _file_digest(current) == RAPID_DB_BRANCH_POINT_SHA256, (
        "%s differs from its RATIFIED digest; RAPIDDB is frozen (rule 17). "
        "Update RAPID_DB_BRANCH_POINT_SHA256 only with a ratified decision, "
        "and list what the new digest absorbs." % RAPID_DB)

    # (2) THE ADDITIONAL CHECK. Says "modified on this branch" rather than
    #     "digest differs", which is the more useful message when a branch is
    #     what did it. No `return`: (1) has already run.
    changed = subprocess.run(
        ["git", "diff", "--name-only", "origin/smdc...HEAD", "--", RAPID_DB],
        cwd=REPO_ROOT, capture_output=True, text=True)
    if changed.returncode == 0:
        assert not changed.stdout.strip(), (
            "%s is modified on this branch; RAPIDDB is frozen" % RAPID_DB)


def test_the_admission_carve_exists_and_is_where_new_access_lives():
    """The positive half: admission access lives in a carved repository."""
    carved = os.path.join(REPO_ROOT, "pipeline", "repositories",
                          "admission.py")
    assert os.path.isfile(carved)
    with open(carved, "r", encoding="utf-8") as handle:
        body = handle.read()
    # The repository never commits and never opens a connection — the two
    # properties that make it composable with a caller's transaction.
    assert "def __init__(self, conn)" in body
    assert "self._conn.cursor()" in body
    assert ".commit()" not in body, (
        "the admission repository must never commit: the caller owns the "
        "transaction boundary")
