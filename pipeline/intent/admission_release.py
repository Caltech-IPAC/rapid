"""Carrying an admission's release into the execution binding (rule 18).

**THE CLAUSE THIS MODULE EXISTS FOR.** Rule 18 says work stays pinned to its
release and "rollback changes only the release used for future admissions".
Half of that already conforms: `ExecutionBinding`
(`observability/attempts.py:203`) is mandatory and complete, authored once at
logical-job scope and copied onto every attempt row, so a retry inherits the
pin rather than floating to whatever release is current when it runs.

The half that did not exist is the ADMISSION half. Submission builds
`ExecutionBinding.release_identity` from the process-wide
`RAPID_RELEASE_IDENTITY` environment variable
(`pipeline/operator/submission.py:274`), and `pipeline/seams.py:375-401`
passes that binding to `create_logical_job` and to every `create_submitted`.
So work derived from an admission took its release from THE SUBMITTING
PROCESS'S ENVIRONMENT, and an admission had no release concept at all
(`addexposure`'s parameters are pure observational facts). "Rollback changes
only the release used for future admissions" therefore had no mechanism: there
was nothing an admission carried for a rollback to change.

DRAFT 051 gives the admission a stamp. This module is what makes the stamp
REACH the binding — without it a worker ships a pointer and a column that
nothing reads, and the rule stays violated while looking closed. The brief is
explicit that an isolated pointer is a non-fix.

**A DISAGREEMENT IS REFUSED LOUDLY, NEVER RESOLVED SILENTLY.** When the
admission's stamp and the environment's `RAPID_RELEASE_IDENTITY` differ, this
raises. It does not prefer one, and it especially does not prefer the
environment — preferring the environment is exactly the current defect, and
preferring the admission silently would hide a genuine deployment
misconfiguration (a submitter running the wrong image for the work it is
picking up). Both are real conditions an operator must see.

**WHY THE CHECK LIVES HERE AND NOT IN `submission.py`.** The submitter reads
the environment; the admission stamp is a database fact reached over a
connection. Putting the reconciliation in a small module both can call keeps
`submission.py`'s environment handling unchanged and gives the contract tier
something to exercise without an AWS environment. It is a pure function of two
values plus a repository read.
"""

from pipeline.repositories.errors import RepositoryQueryFailed

#: Does DRAFT 051's release stamp exist? Probed, never caught — the same
#: discipline `pipeline/repositories/admission.py` documents at length: a
#: failed statement aborts the caller's open transaction, and recovering by
#: rolling back would discard writes the caller had not finished with.
_STAMP_PROBE = (
    "SELECT to_regclass('public.admission_exposures') IS NOT NULL"
    "   AND to_regclass('public.admission_l2files') IS NOT NULL")


class ReleaseBindingError(Exception):
    """Base for release-binding refusals."""

    error_category = "release_binding_error"


class ReleaseDisagreement(ReleaseBindingError):
    """The admission's release and the environment's do not agree.

    REFUSED LOUDLY, which is the rule-18 clause. Silently taking the
    environment's value is the defect this package closes; silently taking the
    admission's would hide a submitter running the wrong image. Both values
    are named, because an operator needs to know which two things disagreed
    and not merely that something did.
    """

    error_category = "release_disagreement"
    exit_code = 65

    def __init__(self, admitted, environment, subject=None):
        where = " for %s" % (subject,) if subject else ""
        super().__init__(
            "release disagreement%s: this work derives from an admission "
            "stamped with release %r, but the submitting process's "
            "RAPID_RELEASE_IDENTITY is %r. Work stays pinned to the release "
            "its admission was made under (rule 18), so this submission is "
            "REFUSED rather than silently resolved in favour of either. "
            "Either submit from a process running the admitted release, or "
            "re-admit the observation under the current one."
            % (where, admitted, environment))
        self.admitted = admitted
        self.environment = environment
        self.subject = subject


class ReleaseStampMissing(ReleaseBindingError):
    """Work derives from an admission carrying no release stamp.

    Fail-closed. An unstamped admission predates DRAFT 051 or was written by a
    path that bypassed the admission repository; either way the release this
    work should be pinned to is unknown, and guessing it from the environment
    would reintroduce exactly the gap rule 18 names.
    """

    error_category = "release_stamp_missing"
    exit_code = 65


def stamp_schema_present(execute):
    """Is DRAFT 051's admission schema applied?

    Takes the same one-callable `execute(sql, params)` executor the rest of
    the intent layer takes, so a service on a borrowed connection and a
    contract test on a scratch database call it identically.

    **ANY UNRECOGNISED ANSWER MEANS "NOT PRESENT", AND THAT IS DELIBERATE.**
    The executor contract is "rows for a statement with a result set,
    `rowcount` otherwise", and a stub that answers this probe with an integer
    — which the seams stub tier does, having no schema at all — is telling us
    it has no admission tables. Reading `rows[0]` there raised `TypeError` and
    took down the whole submission path on a database this check exists to
    tolerate. Fail SOFT here rather than closed: an absent stamp schema means
    the caller falls back to the environment's release, which is the
    documented pre-051 behaviour, whereas an exception would refuse to submit
    at all.
    """
    try:
        rows = execute(_STAMP_PROBE, [])
    except Exception:                                 # noqa: BLE001
        return False
    if not isinstance(rows, (list, tuple)) or not rows:
        return False
    return bool(_first(rows[0]))


def release_for_exposure(execute, expid):
    """The release an exposure's admission was stamped with, or None."""
    rows = execute(
        "SELECT release_identity FROM admission_exposures WHERE expid = %s",
        [int(expid)])
    return _first(rows[0]) if rows else None


def release_for_l2file(execute, exposure, sca):
    """The release an L2 file's admission was stamped with, or None."""
    rows = execute(
        "SELECT release_identity FROM admission_l2files"
        " WHERE expid = %s AND sca = %s", [int(exposure), int(sca)])
    return _first(rows[0]) if rows else None


def reconcile(admitted_release, environment_release, subject=None,
              require_stamp=True):
    """The one release this work is pinned to, or a loud refusal.

    Returns the release identity the `ExecutionBinding` must carry.

    `require_stamp` exists for the expand half of a deployment and defaults to
    the strict reading: an admission with no stamp is refused. A deployment
    that must submit work admitted before 051 landed can pass False, which
    falls back to the environment AND IS RECORDED AS A FALLBACK by the caller
    — it is not a quiet default, because a quiet default here is the whole
    defect.
    """
    if not environment_release:
        raise ReleaseStampMissing(
            "the submitting process has no RAPID_RELEASE_IDENTITY, so no "
            "release can be bound to this work (rule 18). This is a "
            "deployment fault, not a data fault.")
    if not admitted_release:
        if require_stamp:
            raise ReleaseStampMissing(
                "work%s derives from an admission carrying no release stamp, "
                "so the release it should be pinned to is unknown. Taking "
                "the submitting process's environment instead is exactly the "
                "gap rule 18 names, so this is refused. Re-admit through the "
                "admission repository, or submit with require_stamp=False "
                "and record the fallback."
                % (" for %s" % subject if subject else "",))
        return environment_release
    if admitted_release != environment_release:
        raise ReleaseDisagreement(admitted_release, environment_release,
                                  subject)
    return admitted_release


def binding_release_for_units(execute, units, environment_release,
                              require_stamp=True):
    """The single release a manifest's units are all pinned to.

    **LINEARIZATION, AS THE BRIEF FIXES IT.** One manifest carries one
    release. The admission side guarantees this by reading the pointer ONCE at
    the start of a sealed manifest's admission; this is the submission-side
    counterpart, and it REFUSES a manifest whose units carry two different
    admitted releases rather than picking one. A torn manifest is a state
    neither side may produce, so both sides check.

    `units` is an iterable of `(kind, *identity)` tuples — `("exposure", expid)`
    or `("l2file", expid, sca)` — kept deliberately primitive so the contract
    tier can call this without building manifest objects.
    """
    admitted = set()
    for unit in units:
        kind = unit[0]
        if kind == "exposure":
            value = release_for_exposure(execute, unit[1])
        elif kind == "l2file":
            value = release_for_l2file(execute, unit[1], unit[2])
        else:
            raise ReleaseBindingError(
                "unknown admission grain %r; expected 'exposure' or 'l2file'"
                % (kind,))
        if value is not None:
            admitted.add(value)

    if len(admitted) > 1:
        raise ReleaseDisagreement(
            sorted(admitted), environment_release,
            "a manifest whose units were admitted under %d different "
            "releases — one manifest carries one release, and an admission "
            "run reads the pointer once precisely so a pointer switch "
            "mid-run cannot split it" % (len(admitted),))

    return reconcile(admitted.pop() if admitted else None,
                     environment_release, require_stamp=require_stamp)


def _first(row):
    """The first column of a row, whatever cursor factory produced it.

    psycopg2 hands back tuples; some call sites use dict cursors. Both are
    accepted for the same reason `schema_contract.applied_migrations` accepts
    both — this module does not get to dictate the caller's cursor factory.
    """
    if isinstance(row, dict):
        return next(iter(row.values()))
    if isinstance(row, (list, tuple)):
        return row[0]
    return row
