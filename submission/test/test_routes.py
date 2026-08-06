"""Tests for the route matrix.

The matrix is what turns three independently selectable facts — job type,
queue, job definition — into one validated tuple. These tests hold it to
the co-design's table, and hold the validator to rejecting each way a
submission can be off it.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from submission import routes  # noqa: E402
from submission.routes import (  # noqa: E402
    CLASS_BULK, CLASS_PROMPT, LANE_SESSION, LANE_TRANSACTION, RouteError,
)


# --- the matrix itself ------------------------------------------------

def test_every_route_names_a_known_class_and_lane():
    for route in routes.ROUTES:
        assert route.workload_class in routes.WORKLOAD_CLASSES
        assert route.db_lane in routes.DB_LANES


def test_job_types_are_unique():
    assert len(routes.JOB_TYPES) == len(set(routes.JOB_TYPES))


@pytest.mark.parametrize("job_type,workload_class,lane", [
    ("science", CLASS_PROMPT, LANE_TRANSACTION),
    ("reference-image", CLASS_BULK, LANE_TRANSACTION),
    ("post-process", CLASS_PROMPT, LANE_TRANSACTION),
    ("registration", CLASS_PROMPT, LANE_TRANSACTION),
    ("reprocessing", CLASS_BULK, LANE_TRANSACTION),
    ("catalog-load", CLASS_BULK, LANE_SESSION),
    ("crossmatch", CLASS_BULK, LANE_SESSION),
])
def test_matrix_matches_the_codesign_table(job_type, workload_class, lane):
    route = routes.route_for(job_type)
    assert route.workload_class == workload_class
    assert route.db_lane == lane


def test_only_the_long_transaction_types_take_the_session_lane():
    # The co-design is explicit that the lane follows transaction shape,
    # not queue: bulk-queue reprocessing stays on the transaction lane.
    session = {r.job_type for r in routes.ROUTES if r.db_lane == LANE_SESSION}
    assert session == {"catalog-load", "crossmatch"}
    assert routes.route_for("reprocessing").db_lane == LANE_TRANSACTION


def test_routes_carry_parameter_keys_not_queue_names():
    # The queue and definition NAMES live in the parameter tree; a copy
    # here would be a second home for the same fact.
    for route in routes.ROUTES:
        assert route.queue_parameter.startswith("batch/queue-")
        assert route.definition_parameter.startswith("batch/job-definition-")
        assert not route.queue_parameter.startswith("rapid-")


def test_a_route_is_frozen():
    route = routes.route_for("science")
    with pytest.raises(Exception):
        route.db_lane = LANE_SESSION


# --- lookups ----------------------------------------------------------

def test_unknown_job_type_is_rejected_naming_the_vocabulary():
    with pytest.raises(RouteError, match="not a known job type"):
        routes.route_for("telescope-repair")


def test_ppid_map_is_single_homed_here():
    assert routes.ppid_for("science") == 15
    assert routes.ppid_for("post-process") == 17
    assert routes.ppid_for("reference-image") == 12


def test_cross_pipeline_types_have_no_ppid_rather_than_a_placeholder():
    # A placeholder identifier would put rows in a pipeline they do not
    # belong to.
    with pytest.raises(RouteError, match="across pipelines"):
        routes.ppid_for("registration")
    with pytest.raises(RouteError, match="across pipelines"):
        routes.ppid_for("crossmatch")


def test_reverse_ppid_lookup():
    assert routes.job_type_for_ppid(15) == "science"
    assert routes.job_type_for_ppid(12) == "reference-image"
    assert routes.job_type_for_ppid(17) == "post-process"


def test_reverse_lookup_of_an_unknown_ppid_is_rejected():
    with pytest.raises(RouteError, match="no job type has pipeline identifier"):
        routes.job_type_for_ppid(99)


def test_types_for_class_partitions_the_matrix():
    prompt = set(routes.types_for_class(CLASS_PROMPT))
    bulk = set(routes.types_for_class(CLASS_BULK))
    assert not prompt & bulk
    assert prompt | bulk == set(routes.JOB_TYPES)


def test_types_for_an_unknown_class_is_rejected():
    with pytest.raises(RouteError, match="not a workload class"):
        routes.types_for_class("medium")


# --- validation, the entrypoint's startup check -----------------------

QUEUE_NAMES = {
    "batch/queue-prompt": "rapid-queue-prompt",
    "batch/queue-bulk": "rapid-queue-bulk",
}


def test_a_matching_route_validates():
    route = routes.validate_route("science", CLASS_PROMPT,
                                  queue_name="rapid-queue-prompt",
                                  queue_names=QUEUE_NAMES)
    assert route.job_type == "science"


def test_job_type_incompatible_with_the_definition_class_is_rejected():
    # The W8 case: a science manifest handed to the bulk definition.
    with pytest.raises(RouteError, match="runs on the prompt class"):
        routes.validate_route("science", CLASS_BULK)


def test_the_rejection_names_what_the_class_can_run():
    with pytest.raises(RouteError, match="catalog-load"):
        routes.validate_route("science", CLASS_BULK)


def test_right_definition_wrong_queue_is_rejected():
    # The other W8 case: the queue is a submit-time parameter Batch does
    # not bind to the definition, so it has to be checked separately.
    with pytest.raises(RouteError, match="submitted to rapid-queue-bulk"):
        routes.validate_route("science", CLASS_PROMPT,
                              queue_name="rapid-queue-bulk",
                              queue_names=QUEUE_NAMES)


def test_queue_is_not_checked_when_the_tree_was_not_supplied():
    # Callers that have no parameter tree to hand still get the
    # class check; they just do not get the queue check.
    route = routes.validate_route("science", CLASS_PROMPT,
                                  queue_name="anything")
    assert route.job_type == "science"


def test_a_tree_missing_the_queue_parameter_is_a_route_error():
    with pytest.raises(RouteError, match="does not carry batch/queue-prompt"):
        routes.validate_route("science", CLASS_PROMPT,
                              queue_name="rapid-queue-prompt",
                              queue_names={})


def test_unknown_class_is_rejected_before_the_compatibility_check():
    with pytest.raises(RouteError, match="not a workload class"):
        routes.validate_route("science", "prompt-ish")


# ---------------------------------------------------------------------------
# Implemented vocabulary (implementation review #12)
# ---------------------------------------------------------------------------

def test_a_routable_but_unimplemented_job_type_is_rejected_at_the_boundary():
    # REVIEW FINDING #12. The matrix accepts reprocessing, catalog-load and
    # crossmatch because the design names them — but no payload implements
    # them. A manifest naming one used to pass validation, CLAIM AND START an
    # attempt, and only then raise a route error from inside `_execute`, where
    # it became an application failure: a row, a bundle, a terminal record and
    # a failed attempt, all describing a submission that should never have
    # been accepted.
    for job_type in ("reprocessing", "catalog-load", "crossmatch"):
        with pytest.raises(RouteError, match="no implementation"):
            routes.validate_route(job_type, routes.CLASS_BULK)


def test_the_implemented_types_still_validate():
    for job_type, workload_class in (("science", routes.CLASS_PROMPT),
                                     ("reference-image", routes.CLASS_BULK),
                                     ("post-process", routes.CLASS_PROMPT),
                                     ("registration", routes.CLASS_PROMPT)):
        assert routes.validate_route(job_type, workload_class).job_type \
            == job_type


def test_the_implemented_set_matches_what_the_payload_actually_has():
    # The two lists are deliberately separate — the matrix carries design
    # facts (class, queue, lane) for types whose payload has not landed — so
    # something has to hold them together. This is that something: the
    # implemented set is exactly the stage sequences plus registration, which
    # dispatches to the records-consumer path rather than to a sequence.
    from pipeline.stages.sequences import SEQUENCES

    assert routes.IMPLEMENTED_JOB_TYPES == frozenset(SEQUENCES) | {
        routes.JOB_TYPE_REGISTRATION}


def test_every_implemented_type_is_in_the_matrix():
    assert routes.IMPLEMENTED_JOB_TYPES <= set(routes.JOB_TYPES)
