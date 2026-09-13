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

**A route names LANES, plural, not one queue** (2026-09-13). Batch now
runs two lanes rather than a partitioned four-environment fleet: the
prompt lane is on-demand at the on-demand vCPU quota, the bulk lane is
Spot at the Spot quota, and the two draw on different quotas. Which lane
a job takes is therefore a submit-time CHOICE for most job types, not a
property fixed by the class — the same science job is correct on either,
differing only in cost and reclaim exposure.

So `Route.lanes` is an ordered tuple of queue parameter keys, default
first, and the queue check below accepts ANY of them. The entrypoint's
rejection keeps its exact meaning: a job on a queue its type may not use
is still `config_invalid`. What changed is the size of the allowed set,
from one to the lanes the type may legitimately run on.

Bulk is the default because the Spot lane is the larger, cheaper one and
most work tolerates reclaim; alert production is route-fixed to prompt,
because the whole point of the trigger is that an alert follows its
difference image promptly. Note that the lane is orthogonal to the
WORKLOAD CLASS: science is prompt-class (its job definition's command
says so, and that fixes the attempt timeout and the log group) while
defaulting to the bulk lane. Class is what the image can trust about
itself; lane is where the operator put the work.

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
JOB_TYPE_REGISTRATION = "registration"
JOB_TYPE_REPROCESSING = "reprocessing"
JOB_TYPE_CATALOG_LOAD = "catalog-load"
JOB_TYPE_CROSSMATCH = "crossmatch"

# The post-DB science chain's remaining four (step-3 conversion). The chain is
# six job types, not four: the two sweeps beyond the currently invoked set
# (source currency, merge dedup) are part of the operational chain because they
# maintain integrity properties the schema does not enforce, and an
# unmaintained invariant is a defect under the cross-cutting rules (co-design
# ruling 3).
JOB_TYPE_STATISTICS = "statistics"
JOB_TYPE_MERGE_CURRENCY = "merge-currency-sweep"
JOB_TYPE_SOURCE_CURRENCY = "source-currency-sweep"
JOB_TYPE_MERGE_DEDUP = "merge-dedup"

# The alert-production trigger (step-4 co-design, gate 2): "Alert production
# is a job type on the prompt queue, fed by gathering over registration
# outcomes through the accumulator". The in-process after-commit seam was
# ruled out — it is not durable, it couples registration to the stream, and
# it gives alert work no attempt of its own to be recorded against.
JOB_TYPE_ALERT_PRODUCTION = "alert-production"


# --- Execution lanes -------------------------------------------------------
# The two Batch lanes (2026-09-13). A lane is named by the parameter-tree
# key of its queue, never by the queue's own name: the names are
# operational configuration and live in the parameter tree.
#
# LANE_* below are DATABASE lanes and are a different axis entirely —
# transaction vs session pooling. These are the execution lanes.

QUEUE_PARAM_PROMPT = "batch/queue-prompt"
QUEUE_PARAM_BULK = "batch/queue-bulk"

# What `run start --lane` accepts, mapped to the parameter key it selects.
# The CLI's vocabulary is the short name; the matrix's is the parameter key.
LANE_NAMES: dict[str, str] = {
    "prompt": QUEUE_PARAM_PROMPT,
    "bulk": QUEUE_PARAM_BULK,
}
DEFAULT_LANE = "bulk"

# The three lane sets the matrix uses, default first in each.
LANES_EITHER = (QUEUE_PARAM_BULK, QUEUE_PARAM_PROMPT)
LANES_BULK_ONLY = (QUEUE_PARAM_BULK,)
LANES_PROMPT_ONLY = (QUEUE_PARAM_PROMPT,)


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
    lanes : tuple of str
        The parameter-tree keys of every queue this job type may run on,
        DEFAULT FIRST. A job type with one entry is route-fixed to that
        lane; one with two may be sent to either, and `run start --lane`
        is how a submitter chooses. See the module docstring's "lane"
        section for why this is a tuple rather than a single key.
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
    lanes: tuple[str, ...]
    definition_parameter: str
    db_lane: str
    ppid: int | None = None

    @property
    def queue_parameter(self) -> str:
        """The DEFAULT lane's queue parameter key.

        Kept as the name every pre-lane caller already used, now meaning
        "the lane this job type takes when nobody chooses one". Callers
        that must honour an explicit choice use `lanes` and
        `queue_parameter_for_lane` instead.
        """
        return self.lanes[0]


# The matrix, exactly as the co-design states it. Queue and job-definition
# NAMES are deliberately absent: they live in the parameter tree
# (batch/queue-prompt, batch/job-definition-science, ...) and naming them
# here would be a second home for the same fact.
#
# The lane tuples are DEFAULT FIRST. Science and registration may run on
# either lane and default to bulk; every bulk-class type is bulk-only;
# alert production is prompt-only.
ROUTES: tuple[Route, ...] = (
    Route(JOB_TYPE_SCIENCE, CLASS_PROMPT,
          LANES_EITHER, "batch/job-definition-science",
          LANE_TRANSACTION, ppid=15),
    Route(JOB_TYPE_REFERENCE_IMAGE, CLASS_BULK,
          LANES_BULK_ONLY, "batch/job-definition-bulk",
          LANE_TRANSACTION, ppid=12),
    Route(JOB_TYPE_REGISTRATION, CLASS_PROMPT,
          LANES_EITHER, "batch/job-definition-science",
          LANE_TRANSACTION, ppid=None),
    Route(JOB_TYPE_REPROCESSING, CLASS_BULK,
          LANES_BULK_ONLY, "batch/job-definition-bulk",
          LANE_TRANSACTION, ppid=15),
    Route(JOB_TYPE_CATALOG_LOAD, CLASS_BULK,
          LANES_BULK_ONLY, "batch/job-definition-bulk",
          LANE_SESSION, ppid=None),
    Route(JOB_TYPE_CROSSMATCH, CLASS_BULK,
          LANES_BULK_ONLY, "batch/job-definition-bulk",
          LANE_SESSION, ppid=None),
    # The four remaining post-DB job types. All bulk class, all TRANSACTION
    # lane: the database design assigns the budgeted session lane by
    # transaction shape, and only "catalog bulk load, crossmatch" hold it.
    # Statistics rebuilds one field's table and the three sweeps delete
    # bounded row sets — brief transactions, whatever their scan cost, so
    # putting them on the session lane would spend a budgeted connection on
    # work that does not need one.
    Route(JOB_TYPE_STATISTICS, CLASS_BULK,
          LANES_BULK_ONLY, "batch/job-definition-bulk",
          LANE_TRANSACTION, ppid=None),
    Route(JOB_TYPE_MERGE_CURRENCY, CLASS_BULK,
          LANES_BULK_ONLY, "batch/job-definition-bulk",
          LANE_TRANSACTION, ppid=None),
    Route(JOB_TYPE_SOURCE_CURRENCY, CLASS_BULK,
          LANES_BULK_ONLY, "batch/job-definition-bulk",
          LANE_TRANSACTION, ppid=None),
    Route(JOB_TYPE_MERGE_DEDUP, CLASS_BULK,
          LANES_BULK_ONLY, "batch/job-definition-bulk",
          LANE_TRANSACTION, ppid=None),
    # Alert production: PROMPT class, because it is prompt work — the whole
    # point of the trigger is that an alert follows its difference image
    # promptly, and the bulk queue's scaling is sized for reprocessing.
    # TRANSACTION lane: it reads candidate rows and writes one watermark row
    # per unit — brief transactions, not the long-held session the budgeted
    # lane exists for. ppid None: alert production registers no pipeline
    # products, so it belongs to no pipeline's row lineage; a placeholder
    # would put rows in a pipeline they are not from.
    Route(JOB_TYPE_ALERT_PRODUCTION, CLASS_PROMPT,
          LANES_PROMPT_ONLY, "batch/job-definition-science",
          LANE_TRANSACTION, ppid=None),
)

# The six post-DB science chain job types, in chain order. Named as a group
# because the operator submits them as a chain and the gathering layer
# enumerates them together; the order is the dependency order (crossmatch
# gathers after catalog load has written the source tables it reads).
POST_DB_CHAIN: tuple[str, ...] = (
    JOB_TYPE_CATALOG_LOAD,
    JOB_TYPE_CROSSMATCH,
    JOB_TYPE_STATISTICS,
    JOB_TYPE_MERGE_CURRENCY,
    JOB_TYPE_SOURCE_CURRENCY,
    JOB_TYPE_MERGE_DEDUP,
)

JOB_TYPES: tuple[str, ...] = tuple(route.job_type for route in ROUTES)

# The job types this image can actually RUN, as against the ones the matrix
# describes (review finding #12).
#
# The matrix is the design's vocabulary and deliberately names job types that
# are planned — reprocessing, catalog-load, crossmatch. This is the subset
# with a payload behind it: the science and reference-image stage sequences
# plus registration, which dispatches to the records-consumer path rather
# than to a sequence.
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
    JOB_TYPE_REGISTRATION,
    # The post-DB science chain, implemented by the step-3 conversion:
    # `pipeline.stages.post_db` carries the six sequences, so these are
    # runnable rather than merely described. Before the conversion they were
    # matrix rows with no payload, and `validate_route` rejected them at the
    # route boundary — which is exactly what this line changes.
    *POST_DB_CHAIN,
    # The alert-production trigger: `pipeline.stages.alert_production` wires
    # the complete-but-unwired alerts path to the real producer.
    JOB_TYPE_ALERT_PRODUCTION,
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


def queue_parameter_for_lane(job_type: str, lane: str | None = None) -> str:
    """The queue parameter key a submission should use.

    Parameters
    ----------
    job_type : str
        From the manifest.
    lane : str, optional
        A short lane name (``prompt``/``bulk``) as `run start --lane`
        takes it. None means "take this job type's default", which is
        the first entry in its `lanes`.

    Raises
    ------
    RouteError
        If the lane name is not one of the two, or if the job type may
        not run on the lane asked for. The second is the submit-time
        twin of the entrypoint's rejection: alert production asked for
        bulk is refused here rather than being submitted and then
        refused by the container.
    """
    route = route_for(job_type)
    if lane is None:
        return route.queue_parameter
    if lane not in LANE_NAMES:
        raise RouteError(
            f"{lane!r} is not a lane; expected one of "
            + ", ".join(sorted(LANE_NAMES)))
    key = LANE_NAMES[lane]
    if key not in route.lanes:
        allowed = ", ".join(
            name for name, param in sorted(LANE_NAMES.items())
            if param in route.lanes)
        raise RouteError(
            f"job type {job_type!r} may not run on the {lane} lane; it "
            f"runs on: {allowed}")
    return key


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

    # The queue check accepts ANY lane the route names, not just the
    # default: with two Batch lanes the same science job is correct on
    # either, and pinning the check to the default would reject every
    # `--lane prompt` run at startup. The rejection keeps its meaning —
    # a job type on a lane it may not use (alert production on bulk) is
    # still refused, as config_invalid, before any work is claimed.
    if queue_name is not None and queue_names is not None:
        allowed = {}
        for key in route.lanes:
            value = queue_names.get(key)
            if value is not None:
                allowed[value] = key
        if not allowed:
            raise RouteError(
                "the parameter tree carries none of "
                + ", ".join(route.lanes)
                + f", so the queue for job type {job_type!r} cannot be checked")
        if queue_name not in allowed:
            raise RouteError(
                f"job type {job_type!r} runs on "
                + ", ".join(f"{name} ({allowed[name]})" for name in sorted(allowed))
                + f", but this job was submitted to {queue_name}")

    return route
