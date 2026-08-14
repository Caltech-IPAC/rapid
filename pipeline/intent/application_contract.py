"""The APPLICATION half of rule 18's preflight contract.

Rule 18's last clause, verbatim: "Services and payloads preflight the
**application/schema** contract at startup."

`pipeline/intent/schema_contract.py` implements the SCHEMA half — a
one-directional migration floor — and it is complete and correct for what it
does. What has never existed is the application half: **nothing anywhere in
this repository compares the running application's own version against
anything.** Verified by grep at this branch's head, not assumed.

Brief H scopes this in because H is the FINAL conformance package: leaving it
out would leave rule 18 knowingly non-conforming with no later package to
close it. It is deliberately kept PROPORTIONATE — a preflight comparison, not
a new release system.

**WHAT IS COMPARED, AND WHY IT IS NOT AN EQUALITY.** The schema check models
expand/contract by being a floor: a database carrying migrations this build
has never heard of is the expand half of a deployment, and refusing to start
there would refuse exactly the step rule 18 requires to work. The application
check must model the same asymmetry, from the other side:

  * **OLD APPLICATION AGAINST A NEWER SCHEMA IS LEGAL AND MUST START.** That
    is the expand window: the schema moves first, old workers keep running,
    and their results stay acceptable. A check that refused this would be a
    disguised equality test, and the acceptance criterion asserts it
    explicitly for that reason.

  * A NEWER application against an OLDER schema is what the schema floor
    already catches, and it catches it better than a version comparison could
    — by naming the missing migrations rather than comparing two opaque
    strings.

So this check does not compare application version to schema version at all.
It asserts two much narrower things, which are the ones that are genuinely
missing:

  1. **The application's own identity is DETERMINABLE**, and the service fails
     closed when it is not. A process that cannot say what it is cannot have
     its results attributed, cannot be reconciled against an
     `ExecutionBinding`, and cannot be rolled back from — and today it starts
     anyway.

  2. **The release this process claims is visible against the registered
     releases** — a loud warning (never a refusal) when it is unregistered,
     because registration is an operator act and the real gate is the
     admission pointer. Release COMPATIBILITY is per-work, not process-level:
     `pipeline/intent/admission_release.py` pins each admission's release at
     submission time, and the expand/contract direction is enforced by the
     schema half being a floor. (A process-level comparison function lived
     here until 2026-08-14; see the note at the bottom of this module for
     why it was removed.)
"""

import logging
import os

logger = logging.getLogger("rapid.intent.application_contract")

#: The environment variable the submission path already requires
#: (`pipeline/operator/submission.py:226`). Reused rather than invented: a
#: second spelling of "which release is this" would be a second answer.
RELEASE_ENV = "RAPID_RELEASE_IDENTITY"

#: The image digest, likewise already required at submission.
IMAGE_DIGEST_ENV = "RAPID_IMAGE_DIGEST"


class ApplicationContractUnmet(RuntimeError):
    """The running application cannot state what it is.

    A start failure, deliberately, and raised before the service builds
    anything — the same shape as `SchemaContractUnmet`, so an operator reads
    one kind of message for both halves of the contract.
    """

    def __init__(self, missing, detail=""):
        self.missing = tuple(missing)
        listed = "\n".join("  - %s" % name for name in self.missing)
        super().__init__(
            "the running application cannot determine its own identity, so "
            "rule 18's application/schema contract cannot be preflighted. "
            "Missing:\n%s\n%s"
            "A process that cannot say which release it is cannot have its "
            "results attributed to a release, cannot be reconciled against "
            "an ExecutionBinding, and cannot be rolled back from — so it "
            "fails closed here rather than starting and discovering this "
            "later, one unattributable result at a time."
            % (listed, (detail + "\n") if detail else ""))


def application_identity(environ=None):
    """This process's release identity and image digest, or what is missing.

    Returns `(identity, missing)`. Read from the environment because that is
    where the deployment puts it and where the submission path already reads
    it; a second source would be a second answer to one question.
    """
    environ = environ if environ is not None else os.environ
    identity = {
        "release_identity": (environ.get(RELEASE_ENV) or "").strip(),
        "image_digest": (environ.get(IMAGE_DIGEST_ENV) or "").strip(),
    }
    missing = [name for name, value in
               ((RELEASE_ENV, identity["release_identity"]),
                (IMAGE_DIGEST_ENV, identity["image_digest"]))
               if not value]
    return identity, missing


def verify_application_contract(execute=None, environ=None,
                                require_image_digest=True):
    """Fail closed unless this application can state what it is.

    `execute` is the same one-callable executor the schema half takes. It is
    optional: when supplied, the check additionally asserts that the release
    this process claims is REGISTERED (DRAFT 051's `admission_releases`),
    which is what makes "a pointer naming an unknown release is refused at
    mutation time" hold at startup too.

    Returns the identity dict, so a caller can log it — and so a service that
    preflighted is distinguishable in the journal from one that did not.
    """
    identity, missing = application_identity(environ)
    if not require_image_digest:
        missing = [name for name in missing if name != IMAGE_DIGEST_ENV]
    if missing:
        raise ApplicationContractUnmet(missing)

    if execute is not None:
        rows = execute(
            "SELECT to_regclass('public.admission_releases') IS NOT NULL", [])
        present = bool(rows and _first(rows[0]))
        if present:
            rows = execute(
                "SELECT 1 FROM admission_releases WHERE release_identity = %s",
                [identity["release_identity"]])
            if not rows:
                # NOT FATAL AT STARTUP, AND THIS IS THE EXPAND WINDOW. A
                # release may legitimately start before it is registered —
                # registration is an operator act. It is logged loudly rather
                # than refused, because refusing here would make the
                # registration order load-bearing for whether a service can
                # boot, and the actual gate (the admission pointer refusing an
                # unregistered release) is already in the right place.
                logger.warning(
                    "application preflight: release %r is not registered in "
                    "admission_releases. Admissions under this release will "
                    "be refused until it is registered; work already admitted "
                    "is unaffected.", identity["release_identity"])

    logger.info("application preflight passed: release %s image %s",
                identity["release_identity"], identity["image_digest"][:19])
    return identity


# A direction-aware release comparison, `assert_compatible(application_release,
# admitted_release, schema_is_newer=)`, lived here until 2026-08-14 with a
# companion `ApplicationSchemaIncompatible`. REMOVED AS DEAD BY CONSTRUCTION,
# not merely unused: no call site could ever exist, because no process-level
# "admitted release" exists to pass it — a service touches work admitted under
# many releases, and release pinning is per-work, done at admission time by
# `admission_release.reconcile` (strict, correctly so: the submitting process
# defines the release, so no expand window applies there) and carried on the
# `ExecutionBinding` thereafter. The expand-window asymmetry the function
# restated ("old application against a newer schema is legal") is enforced
# structurally by the schema half being a FLOOR — surplus migrations are
# tolerated, missing ones refuse — and asserted by
# `test_the_schema_half_passes_on_a_surplus`. If a per-work execution-time
# release gate is ever wanted (an old container picking up newer-release
# work), it belongs beside the binding read, with the admitted release in
# hand — not here at startup with no operand.


def _first(row):
    if isinstance(row, dict):
        return next(iter(row.values()))
    if isinstance(row, (list, tuple)):
        return row[0]
    return row
