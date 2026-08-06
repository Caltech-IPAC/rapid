"""
File:    routes.py

The route matrix: what a job type is allowed to run as.

The batch-payload co-design's entrypoint contract turns three
independently selectable facts into one validated tuple:

    "The submission manifest names the job type (science,
    reference-image, post-process, registration, ...); the entrypoint
    dispatches on it and rejects at startup any manifest whose job type
    is incompatible with the definition's class — which binds job type,
    queue, job definition, and database lane into one validated route
    instead of three independently selectable facts."

Before this, a submission could name any queue with any job definition
and the mismatch would only show as a job that ran on the wrong hardware
or held the wrong kind of database connection. Here a job type resolves
to exactly one workload class, and the class fixes the queue, the job
definition, and the database lane. The entrypoint (W5) calls the
validators below at startup and refuses to run a route that does not
appear in the matrix.

**Why the class, not the queue, is the discriminator.** The container
command in each job definition names the workload class; the queue is a
submit-time parameter Batch does not bind to the definition. So the class
is what the image can trust, and the queue is what has to be checked
against it — which is why `AWS_BATCH_JQ_NAME` is part of the environment
contract and `validate_route` takes the queue as a separate argument
rather than deriving it.

**The session lane is defined by transaction shape, not by queue.** Most
bulk-queue work transacts briefly and belongs on the transaction lane;
only the genuinely long-transaction job types (catalog bulk load,
crossmatch) get the session-pooled, budgeted lane. The co-design is
explicit about this and the matrix below encodes it: bulk-queue
reprocessing is on the transaction lane.

**The ppid map lives here.** The pipeline identifiers (12, 15, 17) were
defined in three places — a hardcoded map in virtualPipelineOperator, three
`ppid` keys in the master .ini, and bare integer literals in SQL. They are
routing facts: they say which pipeline a row belongs to, exactly as the
job type does. One home, and it is the same home as the rest of the
routing vocabulary.
"""

import dataclasses

# --- Workload classes ------------------------------------------------------
# Fixed by the job definitions' container commands (the prompt definition's
# command names the prompt class, the bulk definition's the bulk class).

CLASS_PROMPT = "prompt"
CLASS_BULK = "bulk"
WORKLOAD_CLASSES = (CLASS_PROMPT, CLASS_BULK)

# --- Database lanes --------------------------------------------------------
# The two lanes at the one pooler door. Names match
# database.modules.utils.rapid_db_connect's LANE_* constants, which is
# what the connection helper is actually given.

LANE_TRANSACTION = "transaction"
LANE_SESSION = "session"
DB_LANES = (LANE_TRANSACTION, LANE_SESSION)

# --- Job types -------------------------------------------------------------

JOB_TYPE_SCIENCE = "science"
JOB_TYPE_REFERENCE_IMAGE = "reference-image"
JOB_TYPE_POST_PROCESS = "post-process"
JOB_TYPE_REGISTRATION = "registration"
JOB_TYPE_REPROCESSING = "reprocessing"
JOB_TYPE_CATALOG_LOAD = "catalog-load"
JOB_TYPE_CROSSMATCH = "crossmatch"


class RouteError(ValueError):
    """A submission's route is not one the matrix allows.

    Raised at submission time by the manifest's own validation and again
    at startup by the entrypoint. Both ends check because they fail
    differently: a submitter's mistake should never reach a container,
    and a container that somehow receives one must refuse rather than run
    the wrong work on the wrong hardware.
    """


@dataclasses.dataclass(frozen=True)
class Route:
    """One row of the route matrix.

    Frozen: a route is a contract, and code that could edit one in place
    would be able to make an invalid submission valid by mutating the
    thing that was supposed to reject it.

    Attributes
    ----------
    job_type : str
        What the manifest names.
    workload_class : str
        Which job definition's command runs it.
    queue_parameter : str
        The parameter-tree key naming this class's queue — the queue name
        itself is operational configuration and is NOT duplicated here.
    definition_parameter : str
        The parameter-tree key naming this class's job definition, same
        reasoning.
    db_lane : str
        Which pooled lane this job type's transactions belong on.
    ppid : int or None
        Pipeline identifier for the rows this job type writes, where it
        has one. Registration and crossmatch operate across pipelines
        rather than as one, so they carry None rather than a placeholder.
    """

    job_type: str
    workload_class: str
    queue_parameter: str
    definition_parameter: str
    db_lane: str
    ppid: int | None = None


# The matrix, exactly as the co-design states it. Queue and job-definition
# NAMES are deliberately absent: they live in the parameter tree
# (batch/queue-prompt, batch/job-definition-science, ...) and naming them
# here would be a second home for the same fact.
ROUTES: tuple[Route, ...] = (
    Route(JOB_TYPE_SCIENCE, CLASS_PROMPT,
          "batch/queue-prompt", "batch/job-definition-science",
          LANE_TRANSACTION, ppid=15),
    Route(JOB_TYPE_REFERENCE_IMAGE, CLASS_BULK,
          "batch/queue-bulk", "batch/job-definition-bulk",
          LANE_TRANSACTION, ppid=12),
    Route(JOB_TYPE_POST_PROCESS, CLASS_PROMPT,
          "batch/queue-prompt", "batch/job-definition-science",
          LANE_TRANSACTION, ppid=17),
    Route(JOB_TYPE_REGISTRATION, CLASS_PROMPT,
          "batch/queue-prompt", "batch/job-definition-science",
          LANE_TRANSACTION, ppid=None),
    Route(JOB_TYPE_REPROCESSING, CLASS_BULK,
          "batch/queue-bulk", "batch/job-definition-bulk",
          LANE_TRANSACTION, ppid=15),
    Route(JOB_TYPE_CATALOG_LOAD, CLASS_BULK,
          "batch/queue-bulk", "batch/job-definition-bulk",
          LANE_SESSION, ppid=None),
    Route(JOB_TYPE_CROSSMATCH, CLASS_BULK,
          "batch/queue-bulk", "batch/job-definition-bulk",
          LANE_SESSION, ppid=None),
)

JOB_TYPES: tuple[str, ...] = tuple(route.job_type for route in ROUTES)

# The job types this image can actually RUN, as against the ones the matrix
# describes (review finding #12).
#
# The matrix is the design's vocabulary and deliberately names job types that
# are planned — reprocessing, catalog-load, crossmatch. This is the subset
# with a payload behind it: the three stage sequences plus registration, which
# dispatches to the records-consumer path rather than to a sequence.
#
# The two lists are deliberately separate rather than the matrix being
# trimmed. The matrix carries each type's class, queue and DB lane, which are
# design facts that stay true while the implementation catches up; deleting
# the rows would lose them and make adding the payload a bigger change than
# it is. Adding a job type here is what turns a described route into a
# runnable one.
#
# This must agree with `pipeline.stages.sequences.SEQUENCES` plus the
# registration dispatch. It is asserted against that registry by a test rather
# than derived from it, because `submission/` must not import the payload's
# stage packages — the submission layer runs on hosts that have no science
# stack at all.
IMPLEMENTED_JOB_TYPES: frozenset = frozenset({
    JOB_TYPE_SCIENCE,
    JOB_TYPE_REFERENCE_IMAGE,
    JOB_TYPE_POST_PROCESS,
    JOB_TYPE_REGISTRATION,
})

_BY_TYPE = {route.job_type: route for route in ROUTES}


def route_for(job_type: str) -> Route:
    """The matrix row for one job type.

    Raises
    ------
    RouteError
        If the job type is not in the vocabulary. Adding one is a
        manifest schema change, not a submit-time argument — which is the
        property that makes the entrypoint's rejection meaningful.
    """
    if job_type not in _BY_TYPE:
        raise RouteError(
            f"{job_type!r} is not a known job type; the vocabulary is "
            + ", ".join(JOB_TYPES))
    return _BY_TYPE[job_type]


def ppid_for(job_type: str) -> int:
    """The pipeline identifier a job type's rows carry.

    Raises
    ------
    RouteError
        If the job type has no ppid. Registration and the catalog jobs
        act across pipelines rather than as one; giving them a
        placeholder identifier would put rows in a pipeline they do not
        belong to.
    """
    route = route_for(job_type)
    if route.ppid is None:
        raise RouteError(
            f"job type {job_type!r} has no pipeline identifier: it acts "
            "across pipelines rather than as one")
    return route.ppid


def job_type_for_ppid(ppid: int) -> str:
    """Reverse lookup, for reading legacy rows.

    Raises
    ------
    RouteError
        If no job type claims that identifier.
    """
    for route in ROUTES:
        if route.ppid == ppid:
            return route.job_type
    known = ", ".join(str(r.ppid) for r in ROUTES if r.ppid is not None)
    raise RouteError(
        f"no job type has pipeline identifier {ppid}; known identifiers "
        f"are {known}")


def types_for_class(workload_class: str) -> tuple[str, ...]:
    """Every job type a workload class may run."""
    if workload_class not in WORKLOAD_CLASSES:
        raise RouteError(
            f"{workload_class!r} is not a workload class; expected one of "
            + ", ".join(WORKLOAD_CLASSES))
    return tuple(r.job_type for r in ROUTES if r.workload_class == workload_class)


def validate_route(job_type: str,
                   workload_class: str,
                   queue_name: str | None = None,
                   queue_names: dict[str, str] | None = None) -> Route:
    """Check one submission's route against the matrix.

    This is what the entrypoint calls at startup, with the class its own
    job definition's command fixed and the queue Batch actually put it on.

    Parameters
    ----------
    job_type : str
        From the manifest.
    workload_class : str
        From the entrypoint's own fixed discriminator — what the image
        knows about itself.
    queue_name : str, optional
        From ``AWS_BATCH_JQ_NAME``. Checked only when `queue_names` is
        also given, since the queue's NAME lives in the parameter tree
        and this module deliberately does not hold a copy.
    queue_names : dict, optional
        Parameter-tree values, relative-keyed (``batch/queue-prompt`` ->
        ``rapid-queue-prompt``), as ``submission.startup.fetch_parameters``
        returns them.

    Returns
    -------
    Route
        The validated row.

    Raises
    ------
    RouteError
        Job type unknown; job type not implemented; job type incompatible
        with the class; or the queue is not the one this route runs on.
    """
    route = route_for(job_type)

    # THE VOCABULARY IS RESTRICTED TO WHAT IS IMPLEMENTED (review finding
    # #12). The matrix accepts reprocessing, catalog-load and crossmatch
    # because the design names them as job types — but no payload implements
    # them. A manifest naming one used to pass validation, CLAIM AND START an
    # attempt, and only then raise a route error from inside `_execute`,
    # where it became an application failure: a row, a bundle, a terminal
    # record and a failed attempt, all describing a submission that should
    # never have been accepted.
    #
    # Rejecting here, at the route boundary and before ownership, is the
    # design's own rule — "the entrypoint rejects at startup any manifest
    # whose job type is incompatible with the definition's class", and a job
    # type with no payload is the same kind of unroutable.
    if job_type not in IMPLEMENTED_JOB_TYPES:
        raise RouteError(
            f"job type {job_type!r} is in the route matrix but has no "
            f"implementation in this image; implemented job types are: "
            + ", ".join(sorted(IMPLEMENTED_JOB_TYPES))
            + ". Rejected at the route boundary rather than inside the "
            "payload, so no attempt is claimed for a submission that cannot "
            "run.")

    if workload_class not in WORKLOAD_CLASSES:
        raise RouteError(
            f"{workload_class!r} is not a workload class; expected one of "
            + ", ".join(WORKLOAD_CLASSES))

    if route.workload_class != workload_class:
        raise RouteError(
            f"job type {job_type!r} runs on the {route.workload_class} class, "
            f"but this job definition's command names the {workload_class} "
            f"class; the {workload_class} class runs: "
            + ", ".join(types_for_class(workload_class)))

    if queue_name is not None and queue_names is not None:
        expected = queue_names.get(route.queue_parameter)
        if expected is None:
            raise RouteError(
                f"the parameter tree does not carry {route.queue_parameter}, "
                f"so the queue for job type {job_type!r} cannot be checked")
        if queue_name != expected:
            raise RouteError(
                f"job type {job_type!r} runs on {expected} "
                f"({route.queue_parameter}), but this job was submitted to "
                f"{queue_name}")

    return route
