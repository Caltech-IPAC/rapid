"""The Virtual Pipeline Operator, as a supervised service.

`design/operations.md`, ADOPTED: "The VPO is a supervised long-running
service under the same service-supervision requirement as the reconciler
— clean start/stop/restart, bounded local diagnostics — not a scheduled
script."

Module map:

* `classes` — the four declared operational classes, two of them
  declared-not-implemented.
* `inputs` — what an invocation is asked for: a window and a
  disposition for every declared class.
* `submitters` — the rehearsal seam. Rehearsal holds an object with no
  submitting capability rather than a flag guarding one.
* `operator` — one pass: gather, accumulate, cut on the tree's cadence,
  submit, register.
* `registration` — pass-level verdict over the consumer's per-item
  results; partial failure distinct from total, neither fatal to a pass.
* `service` — the entry point the systemd unit runs.

The module this replaces, `pipeline/virtualPipelineOperator.py`, stays
in the tree for the phase logic still being converted; see the operator
documentation page for what has moved and what has not.
"""

from pipeline.operator.classes import (CLASS_NAMES, CLASSES,
                                       ClassNotImplemented,
                                       HISTORICAL_BACKFILL,
                                       PROMPT_PROCESSING,
                                       REFERENCE_CONSTRUCTION,
                                       RELEASE_REPROCESSING, class_for,
                                       implemented_classes)
from pipeline.operator.operator import Operator, PassResult
from pipeline.operator.submitters import (LiveSubmitter, RehearsalSubmission,
                                          RehearsalSubmitter)

__all__ = [
    "CLASSES", "CLASS_NAMES", "ClassNotImplemented",
    "HISTORICAL_BACKFILL", "PROMPT_PROCESSING", "REFERENCE_CONSTRUCTION",
    "RELEASE_REPROCESSING", "class_for", "implemented_classes",
    "Operator", "PassResult",
    "LiveSubmitter", "RehearsalSubmitter", "RehearsalSubmission",
]
