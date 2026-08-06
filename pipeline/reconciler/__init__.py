"""The reconciler: the scheduler-observation authority.

The attempt record is the sole machine-readable account of what a job did, and
it has exactly two writers. The application authors what it did; *this* service
authors what the scheduler saw. No field has two writers, so nothing here ever
overwrites an application-authored value — it fills the columns beside them and
flags disagreement rather than resolving it.

What the reconciler owns, and nothing else does:

- **Scheduler observations.** State, the three scheduler timestamps, the
  observed exit code, and the scheduler-observed attempt index.
- **Classification.** Every submitted attempt reaches a terminal state within a
  stated horizon, or becomes a flagged reconciliation case. Two error
  categories are the reconciler's alone (`scheduler_reclaimed`,
  `scheduler_provisioning`) — an attempt the application never classified gets
  a machine-readable category, never a null or a mislabeled `internal_error`.
- **Closure records.** Every classification publishes a closure record, and
  every one is a *complete canonical snapshot*: it folds in the predecessor's
  application facts (validated by checksum) and adds the scheduler's. The
  highest-sequence record alone is always the full account — consumers never
  chain-fold.
- **Retention tags.** The bundle key is classification-neutral; the retention
  class is a tag stamped here at classification time. Because S3's tagging API
  replaces the whole tag set, the retag is a canonical full-set rewrite, and
  correction is monotonic toward longer retention.
- **Materialization.** A crash between the terminal record and the
  application-closed transition leaves a started row beside a valid record.
  The reconciler materializes the transition *from* the record, values
  verbatim, marked reconciler-materialized — the one sanctioned projection of
  application facts by another writer.

Concurrency is arbitrated structurally. A per-attempt lease — a
transaction-scoped advisory lock — spans reread, closure publication, tag
rewrite and the terminal transition, because the S3 tagging API offers no
compare-and-set and the lease is what makes the monotonic-retention rule hold
under concurrent or replayed correction.
"""

from .horizons import (
    GRACE_HORIZON_SECONDS,
    SUBMISSION_HORIZON_SECONDS,
    beyond_grace_horizon,
    beyond_submission_horizon,
)
from .scheduler import (
    DescribeBatch,
    SchedulerObservation,
    derive_attempt_indices,
    describe_in_batches,
    observation_from_job,
)
from .service import ReconcilerService, run_forever
from .closure import (
    ClosureRecord,
    build_closure_record,
    publish_closure_record,
)
from .retention import (
    RETENTION_CLASSES,
    canonical_tag_set,
    retention_class_for,
    stamp_retention,
)

__all__ = [
    "GRACE_HORIZON_SECONDS",
    "SUBMISSION_HORIZON_SECONDS",
    "beyond_grace_horizon",
    "beyond_submission_horizon",
    "DescribeBatch",
    "SchedulerObservation",
    "derive_attempt_indices",
    "describe_in_batches",
    "observation_from_job",
    "ReconcilerService",
    "run_forever",
    "ClosureRecord",
    "build_closure_record",
    "publish_closure_record",
    "RETENTION_CLASSES",
    "canonical_tag_set",
    "retention_class_for",
    "stamp_retention",
]
