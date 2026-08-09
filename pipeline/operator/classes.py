"""The five operational classes, declared.

`design/operations.md` § The Virtual Pipeline Operator, ADOPTED (amended
by the integration review's ruling 13, which lands the fifth):

    Pipeline work divides along two distinct axes. The five
    **operational classes** — prompt processing, reference construction,
    historical backfill, release reprocessing, test — are the declared,
    normative set of kinds of work; the compute design's two **queue
    classes** (prompt, bulk) are the routing discriminator, and the
    mapping is fixed: prompt processing routes to the prompt queue;
    reference construction, backfill, and reprocessing route to bulk;
    a test campaign declares its queue at campaign definition — bulk by
    default, prompt only for an explicitly ruled latency rehearsal.
    [...] Backfill, release reprocessing, and test are declared ahead of
    implementation [...] and nothing may claim their names meanwhile.

Two axes, and this module is the first one. The *queue* class already
had a home — `submission.routes`, which maps job type to queue and
definition — and the operational class did not exist in code at all: the
old operator ran three phases in sequence and the word "class" appeared
nowhere, so "the four classes" (now five) was a design sentence with no
structure answering to it.

The declared-not-implemented half is the load-bearing part. Backfill,
release reprocessing, and test are named here WITH the reason they
cannot run, and asking for one raises `ClassNotImplemented` naming that
reason. Declaring them by omission — leaving them out until someone
builds them — is what lets a later reader add a `backfill` (or `test`)
string somewhere and have it mean whatever their code does, which is
exactly what "nothing may claim their names meanwhile" forbids.

**TEST IS NOW IMPLEMENTED (integration review ruling 13, build IR-13-a) —
under a v1 restriction stated precisely, not "fully general".** The two
blockers this module used to name are resolved differently, not both
built out:

Blocker 1 (no loaded `workflow_definitions` row) is an OPERATIONAL
action, not code — the campaign-unit gatherer this build adds does not
change that a work unit's `(job_type, definition_version)` FK-references
`workflow_definitions` (migration 036), and no caller of migration 039's
`derived.load_workflow_definition` exists yet anywhere in this repo. So
`implemented=True` here is a claim about the CODE PATH, not a claim that
a workflow_definitions row is loaded — a campaign whose definition row is
missing still fails loudly at work-unit creation
(`pipeline.intent.writer.WorkUnitWriter.create_work_unit`'s FK violation),
which is the correct surface for an unmet operational precondition, not
a reason to keep the class declared-not-implemented in code. See this
build's report for the exact pre-campaign load step the V phase must run
first.

Blocker 2 (no per-campaign route resolution) is resolved by RESTRICTING
rather than generalizing: "test campaigns declare the SCIENCE route" (the
v1 restriction the supervisor ruling states, asserted at campaign
creation in `pipeline.mock.transformer.create_mock_campaign_from_staged`
and re-asserted at gathering in `submission.gathering.
gather_campaign_units`). Because v1 test campaigns have exactly ONE
legal route, `TEST.job_type` can simply BE `routes.JOB_TYPE_SCIENCE` —
`OperationalClass.route`'s existing `route_for(self.job_type)` shape
needs no change at all. A later ruling that lets a test campaign declare
a different route per campaign is the one that would need a real
per-campaign route resolution mechanism; this v1 does not, because it
does not offer the choice.
"""

import dataclasses

from submission import routes

#: The prompt-processing class: an SCA arriving from the SOC, processed
#: for alerts. The only class with a latency target.
PROMPT_PROCESSING = "prompt-processing"

#: Reference construction: coadding L2 files into the references that
#: difference imaging subtracts.
REFERENCE_CONSTRUCTION = "reference-construction"

#: Historical backfill. Declared, not implemented.
HISTORICAL_BACKFILL = "historical-backfill"

#: Release reprocessing. Declared, not implemented.
RELEASE_REPROCESSING = "release-reprocessing"

#: Test: the mission-mock harness's campaigns. Implemented (IR-13-a) under
#: the v1 restriction that a test campaign's route IS the science route —
#: see this module's docstring for what changed and what a
#: workflow_definitions load still requires operationally.
TEST = "test"


class ClassNotImplemented(NotImplementedError):
    """A declared class that has no implementation, asked to run.

    Carries the design's own reason rather than a bare "not implemented",
    because the two undelivered classes are blocked on different things
    and an operator reading this needs to know which.
    """


@dataclasses.dataclass(frozen=True)
class OperationalClass:
    """One of the five. Frozen for the same reason a `Route` is.

    Attributes
    ----------
    name : str
        The declared name. Normative — no other spelling is this class.
    job_type : str or None
        The route this class submits under, where it has one. The
        declared-not-implemented classes carry None: they have no route
        because choosing one is part of the work not yet done.
    implemented : bool
        Whether this class can run. False is a design state, not a bug.
    blocked_on : str or None
        For an unimplemented class, what it waits for — the design's
        answer, quoted where it is short enough to quote.
    """

    name: str
    job_type: str | None
    implemented: bool
    blocked_on: str | None = None

    def require_implemented(self) -> None:
        """Raise unless this class can actually run."""
        if not self.implemented:
            raise ClassNotImplemented(
                f"the {self.name} class is declared but not implemented: "
                f"{self.blocked_on}. It is named here so nothing else can "
                f"claim the name (operations.md, ADOPTED); running it is "
                f"not in scope until that work lands.")

    @property
    def route(self):
        """This class's route in the submission matrix."""
        self.require_implemented()
        return routes.route_for(self.job_type)


CLASSES: tuple[OperationalClass, ...] = (
    OperationalClass(
        PROMPT_PROCESSING,
        job_type=routes.JOB_TYPE_SCIENCE,
        implemented=True),
    OperationalClass(
        REFERENCE_CONSTRUCTION,
        job_type=routes.JOB_TYPE_REFERENCE_IMAGE,
        implemented=True),
    OperationalClass(
        HISTORICAL_BACKFILL,
        job_type=None,
        implemented=False,
        blocked_on="it belongs to the failure-path design as the resume "
                   "mechanism of the pending state, which is not designed "
                   "yet"),
    OperationalClass(
        RELEASE_REPROCESSING,
        job_type=None,
        implemented=False,
        blocked_on="it belongs to the release machinery, which is not "
                   "built yet"),
    OperationalClass(
        TEST,
        # v1 restriction (IR-13-a): a test campaign's route IS the science
        # route — not a per-campaign choice yet, so a single fixed
        # job_type is exactly right, matching PROMPT_PROCESSING's own
        # shape rather than needing a new mechanism.
        job_type=routes.JOB_TYPE_SCIENCE,
        implemented=True),
)

#: Every declared name, in declaration order.
CLASS_NAMES: tuple[str, ...] = tuple(c.name for c in CLASSES)

_BY_NAME = {c.name: c for c in CLASSES}


def class_for(name: str) -> OperationalClass:
    """The declared class of this name, or raise naming the five.

    An unknown name is an error rather than a default: the set is closed
    by design, and quietly treating an unrecognised class as prompt
    processing is how a typo becomes a submission to the wrong queue.
    """
    try:
        return _BY_NAME[name]
    except KeyError:
        raise ValueError(
            f"unknown operational class {name!r}; the declared set is "
            f"{', '.join(CLASS_NAMES)} (operations.md, ADOPTED)") from None


def implemented_classes() -> tuple[OperationalClass, ...]:
    """The classes that can actually run, in declaration order."""
    return tuple(c for c in CLASSES if c.implemented)
