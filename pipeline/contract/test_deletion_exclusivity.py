"""Acceptance criteria 11 and 12 — one deletion route, and the frozen class.

Both are assertions over the SOURCE TREE rather than over a database, so
neither needs the `contract` marker. They are the two regressions this arc has
most reason to fear: a second deletion route appearing quietly, and a new
method landing on `RAPIDDB` (which the D, F and E workers each did, and each
needed a fix round for).
"""

import os
import subprocess

import pytest

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


def test_rapiddb_gains_no_new_method_on_this_branch():
    """Criterion 12 — asserted MECHANICALLY, not by review.

    `RAPIDDB` is frozen (rule 17; brief G's ratified merge decision), and this
    is the arc's most repeated regression: the D, F and E workers each added a
    method to this class and each needed a fix round to carve it back out.
    An assertion is what stops the fourth occurrence being discovered at a
    merge gate.

    Compares the file's public method set against the branch point rather than
    against a hard-coded list: a hard-coded list would have to be updated by
    the very edit it is meant to catch.
    """
    branch_point = subprocess.run(
        ["git", "merge-base", "HEAD", "origin/smdc"],
        cwd=REPO_ROOT, capture_output=True, text=True)
    if branch_point.returncode != 0:
        # Fall back to the recorded branch point when the remote ref is not
        # fetched (the acceptance host stages a tarball, not a clone).
        base = "066c353"
    else:
        base = branch_point.stdout.strip()

    before = subprocess.run(["git", "show", "%s:%s" % (base, RAPID_DB)],
                            cwd=REPO_ROOT, capture_output=True, text=True)
    if before.returncode != 0:
        pytest.skip("no git history available here (%s)"
                    % before.stderr.strip()[:80])

    with open(os.path.join(REPO_ROOT, RAPID_DB), "r",
              encoding="utf-8") as handle:
        after_text = handle.read()

    added = _public_methods(after_text) - _public_methods(before.stdout)
    assert not added, (
        "RAPIDDB is FROZEN (rule 17) and this branch adds %d public "
        "method(s) to it: %s. New database access is a carved repository "
        "under pipeline/repositories/ — connection owned by the caller, "
        "named-record returns, typed errors. The D, F and E workers each "
        "broke this and each needed a fix round."
        % (len(added), sorted(added)))


def test_rapid_db_is_byte_identical_with_the_branch_point():
    """The stronger form: this branch does not touch the file at all.

    Stronger than the method-set assertion and deliberately kept alongside it:
    the method-set check would pass an edit that changed a method's BODY,
    which is still an edit to a frozen class.
    """
    changed = subprocess.run(
        ["git", "diff", "--name-only", "origin/smdc...HEAD", "--", RAPID_DB],
        cwd=REPO_ROOT, capture_output=True, text=True)
    if changed.returncode != 0:
        pytest.skip("no git history available here")
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
