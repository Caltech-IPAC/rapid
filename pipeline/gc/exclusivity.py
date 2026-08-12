"""Criterion 11 — deletion has exactly one route in production code.

**THE SCOPE OF THIS ASSERTION IS STATED HONESTLY, HERE AND IN ITS OWN FAILURE
MESSAGE.** It proves exclusivity WITHIN THIS REPOSITORY'S PRODUCTION CODE. It
does NOT prove it for the deployed system, and no passing result here should
be read as "rule 21 conforms".

What the deployed system additionally has, verified read-only against
`rapid_systems` on this branch:

  * S3 lifecycle EXPIRY rules that delete objects outside this mechanism —
    `cloudformation/rapid-storage-buckets.yaml:202` (`roman-rapid-logs`),
    `:489-491` (`roman-rapid-diagnostics`, filtering on the
    `retention-class=success` tag `pipeline/reconciler/retention.py` stamps),
    `:568-569` (`roman-rapid-meta`) and `:760` (`roman-rapid-build`).

  * **None of those is inside this package's declared GC scope.**
    `cloudformation/rapid-product-buckets.yaml` is 74 lines, defines
    `roman-rapid-products` and `roman-rapid-alerts`, and carries NO
    `LifecycleConfiguration` at all. So inside the declared scope there is
    today no competing deletion route — which narrows the residual gap the
    brief anticipated, and is recorded in `notes-brief-h-evidence.md` rather
    than being quietly relied on.

Rule 21 is an EXCLUSIVITY constraint, and it is scored **PARTIAL — pending
CR** on that basis: the repository-side mechanism is delivered, fail-closed,
and exclusive within this repository's production code; the diagnostics-bucket
tag-expiry route still deletes outside it, in a bucket this package does not
govern and may not edit. CR-H5 in the worker's ledger is the change request.
"""

import os
import re

#: Deletion calls this assertion hunts for.
DELETE_CALL = re.compile(
    r"\b(delete_object|delete_objects|delete_bucket)\b|"
    r"aws\s+s3\s+rm\b|"
    r"\bs3\s*\.\s*Object\([^)]*\)\s*\.\s*delete\b")

#: The ONE production module permitted to delete objects.
GC_EXECUTION_MODULE = "pipeline/gc/execute.py"

#: APPROVED EXCLUSIONS, ENUMERATED EXPLICITLY rather than expressed as a
#: pattern. Each is a real, legitimate deletion that is not product GC:
#:
#:   * `scripts/` and `*/test/` acceptance-harness cleanup removes the run's
#:     OWN S3 staging prefix under the build-artifacts bucket — never a
#:     product, never a bucket in the declared scope;
#:   * test fixtures construct and tear down their own scratch objects.
#:
#: Listing the directories rather than matching a substring means a new
#: deletion added to production code cannot hide behind a filename that
#: happens to contain "test".
EXCLUDED_DIRECTORIES = ("scripts", "test", "tests", "sims", "RuBR", "docs")

#: Production trees this assertion walks.
PRODUCTION_TREES = ("pipeline", "submission", "observability", "database",
                    "alerts", "modules", "aws")


def _is_excluded(relative_path):
    parts = relative_path.split(os.sep)
    if any(part in EXCLUDED_DIRECTORIES for part in parts[:-1]):
        return True
    # A top-level module in an excluded tree, e.g. `scripts/foo.py`.
    return parts[0] in EXCLUDED_DIRECTORIES


def find_deletion_routes(root):
    """Every deletion call site in production code.

    Returns a list of `(relative_path, line_number, line)`.
    """
    found = []
    for tree in PRODUCTION_TREES:
        base = os.path.join(root, tree)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames
                           if d not in EXCLUDED_DIRECTORIES
                           and not d.startswith(".")]
            for filename in filenames:
                if not filename.endswith((".py", ".sh")):
                    continue
                # AppleDouble sidecars ship with a .py suffix and binary
                # content; brief E's scanner tripped over exactly these.
                if filename.startswith("._"):
                    continue
                full = os.path.join(dirpath, filename)
                relative = os.path.relpath(full, root)
                if _is_excluded(relative):
                    continue
                try:
                    with open(full, "r", encoding="utf-8",
                              errors="replace") as handle:
                        for number, line in enumerate(handle, 1):
                            if line.lstrip().startswith("#"):
                                continue
                            if DELETE_CALL.search(line):
                                found.append((relative, number, line.strip()))
                except OSError:
                    continue
    return found


def assert_single_deletion_route(root):
    """Raise unless every production deletion call is in the GC executor.

    The failure message STATES ITS OWN SCOPE, so a passing assertion can never
    be quoted as system-wide exclusivity.
    """
    offenders = [entry for entry in find_deletion_routes(root)
                 if entry[0].replace(os.sep, "/") != GC_EXECUTION_MODULE]
    if offenders:
        listed = "\n".join("  - %s:%d  %s" % entry for entry in offenders)
        raise AssertionError(
            "object deletion must be reachable only from %s (rule 21), but "
            "%d other production call site(s) delete objects:\n%s\n\n"
            "SCOPE OF THIS ASSERTION: it proves exclusivity within this "
            "repository's production code ONLY. It does NOT prove it for the "
            "deployed system: S3 lifecycle expiry rules delete objects "
            "outside this mechanism and live in rapid_systems bucket "
            "configuration, which this package may not edit. Rule 21 "
            "therefore scores PARTIAL — pending CR — and a passing result "
            "here must not be reported as system-wide exclusivity."
            % (GC_EXECUTION_MODULE, len(offenders), listed))
    return True
