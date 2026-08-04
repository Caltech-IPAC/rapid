"""
Attempt records: the authoritative account of every processing attempt.

design/observability.md § Attempt record, realized as three live-state tables
(migration 011) and the code that writes and reads them.

Modules
-------
attempts
    Write side: create attempt rows at submission time, advance them through
    the lifecycle, record stages and milestones, write the reconciler's
    scheduler-observed columns.
intervals
    Read side: decompose SCA-to-alert latency into its five intervals, and
    report application-vs-scheduler timestamp disagreement.
registration
    Read side: decide what to register from the attempt record rather than by
    parsing job output.

The database boundary is one injected `execute(sql, params)` callable, so all
of this is exercisable without a database — which is how it is tested.
"""

from .attempts import (
    SCHEMA_VERSION,
    AttemptIdentity,
    AttemptWriter,
    LifecycleState,
    ProductDisposition,
    Provenance,
    RapidOutcome,
    ReconciliationClass,
    Stage,
    StageOutcome,
)
from .intervals import Decomposition, compare_timestamps, decompose
from .registration import RegistrationDecision, decide, decide_all

__all__ = [
    "SCHEMA_VERSION",
    "AttemptIdentity",
    "AttemptWriter",
    "Decomposition",
    "LifecycleState",
    "ProductDisposition",
    "Provenance",
    "RapidOutcome",
    "ReconciliationClass",
    "RegistrationDecision",
    "Stage",
    "StageOutcome",
    "compare_timestamps",
    "decide",
    "decide_all",
    "decompose",
]
