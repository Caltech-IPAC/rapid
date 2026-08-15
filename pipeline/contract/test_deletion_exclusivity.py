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
RAPID_DB_BRANCH_POINT_SHA256 = (
    "474cc92e27265dec8b27e3c195acd159c8af44767923bf79abdfb8bcf4069487")


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
