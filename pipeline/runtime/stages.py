"""
File:    stages.py

`run_stage`: stage accounting, span-shaped and written once.

design/observability.md § Attempt record: "Stage records are span-shaped and
append-only: one row per stage execution, written once at completion, carrying
a wall-clock start for correlation and a monotonic-clock duration that is
authoritative for elapsed time."

Three things follow from that sentence and are enforced here:

**Two clocks, each for what it is good for.** `started_at` is
`datetime.now(timezone.utc)` — a wall clock, for correlating this stage against
a scheduler timestamp or another host's log. `duration_ms` is from
`time.monotonic()` — which cannot run backwards when NTP steps the clock, and
is the only defensible source for an elapsed time that ends up in a latency
decomposition. Subtracting two wall clocks would silently produce a negative
duration during a leap-second smear or an NTP correction, and the attempt
writer's own `Stage` rejects a negative duration, so the failure would surface
as a crash in the recording path at the end of a successful stage.

**Written once, at completion, including on failure.** A stage that raises
still gets its record, with `outcome=failure` — a stage whose failure left no
row would be indistinguishable from a stage that never ran, and "silence is a
state, not an answer" is exactly what the design refuses.

**Stage records are best-effort relative to the stage's own outcome.** If the
stage succeeded but recording its span failed, the failure is logged and the
stage's result is still returned. That asymmetry is deliberate and bounded:
the stage record is a span for latency analysis, not the outcome authority —
the terminal record and the application-closed transition are, and those are
NOT best-effort (a failure there raises `RecordsError` and takes the process
to a nonzero exit). Losing one span row degrades a latency query; failing the
job because a span row would not insert would trade a real product for a
diagnostic.
"""

import contextlib
import dataclasses
import datetime
import time
from typing import Any, Callable

from pipeline.runtime.errors import serialize_error
from pipeline.runtime.logging_setup import StageFileHandler, get_logger
from pipeline.runtime.process import redact

_logger = get_logger("stages")


@dataclasses.dataclass
class StageRecord:
    """One completed stage span, as the runtime accounts for it.

    Converted to `observability.attempts.Stage` by `to_attempt_stage` at the
    moment of writing. Kept as a separate type rather than using the writer's
    dataclass directly so the runtime can carry the fields the writer has no
    column for — the error category and message of a failed stage, which go
    into the terminal record's stage list — without pretending the database
    stores them.
    """

    stage_name: str
    started_at: datetime.datetime
    duration_ms: float
    outcome: str
    error_category: str | None = None
    error_message: str | None = None

    def to_attempt_stage(self) -> Any:
        """Convert to the attempt writer's `Stage`."""
        from observability.attempts import Stage, StageOutcome

        return Stage(
            stage_name=self.stage_name,
            started_at=self.started_at,
            duration_ms=self.duration_ms,
            outcome=StageOutcome(self.outcome),
        )

    def as_dict(self) -> dict:
        out = {
            "stage_name": self.stage_name,
            "started_at": self.started_at.isoformat(),
            "duration_ms": round(self.duration_ms, 3),
            "outcome": self.outcome,
        }
        if self.error_category is not None:
            out["error_category"] = self.error_category
        if self.error_message is not None:
            out["error_message"] = self.error_message
        return out


class StageRecorder:
    """Collects stage spans and writes each one once, at completion.

    The writer boundary is one callable, `write(stage_record)`, so a test
    substitutes a list and a live run substitutes the attempt writer. The
    recorder also keeps every span in memory, because the terminal record
    carries the full stage list and building it from the database would mean
    reading back what this process just wrote.
    """

    def __init__(self, write: Callable[[StageRecord], None] | None = None):
        self._write = write
        self.records: list[StageRecord] = []

    def record(self, record: StageRecord) -> None:
        self.records.append(record)
        if self._write is None:
            return
        try:
            self._write(record)
        except Exception as exc:  # noqa: BLE001 - see the module docstring
            # Best-effort by design, and loud about it: the span is lost, the
            # stage's own outcome is not affected, and the log says so.
            _logger.error(
                "could not record the span for stage %r (%s: %s); the stage's "
                "own outcome is unaffected and the terminal record still "
                "carries this stage",
                record.stage_name, type(exc).__name__, redact(str(exc)))

    def as_list(self) -> list:
        return [record.as_dict() for record in self.records]

    @property
    def failed(self) -> list:
        return [r for r in self.records if r.outcome == "failure"]


@contextlib.contextmanager
def stage_context(name: str, recorder: StageRecorder | None = None,
                  workdir: Any = None, logger: Any = None):
    """Run a block as one accounted stage.

    The context-manager form. `run_stage` is the callable form and is
    implemented on top of this; both exist because a stage body is sometimes a
    function to call and sometimes a block of inline code, and forcing the
    second into a lambda is how a stage ends up with its real work outside its
    own accounting.

    Yields a dict the block may add to: `{"outcome": ...}` lets a stage
    declare `skipped` rather than `success` for work it decided not to do —
    a real third state in the writer's vocabulary, and the difference between
    "this stage did nothing because there was nothing to do" and "this stage
    did nothing because it silently failed".
    """
    log = logger if logger is not None else _logger
    started_at = datetime.datetime.now(datetime.timezone.utc)
    started_monotonic = time.monotonic()
    state: dict = {"outcome": "success"}

    log.info("stage start: %s", name)

    # The stage's log lines are captured to their own bundle member for the
    # duration of the stage — and only for that duration, so a later stage's
    # lines never land in an earlier stage's file.
    capture: Any = contextlib.nullcontext()
    if workdir is not None:
        capture = StageFileHandler(workdir.stage_log_path(name))

    try:
        with capture:
            try:
                yield state
            except BaseException as exc:
                duration_ms = (time.monotonic() - started_monotonic) * 1000.0
                serialized = serialize_error(exc, include_traceback=False,
                                             redactor=redact)
                log.error("stage failed: %s after %.1fms (%s: %s)",
                          name, duration_ms, serialized.error_type,
                          serialized.message)
                if recorder is not None:
                    recorder.record(StageRecord(
                        stage_name=name,
                        started_at=started_at,
                        duration_ms=duration_ms,
                        outcome="failure",
                        error_category=serialized.error_category,
                        error_message=serialized.message,
                    ))
                raise
    finally:
        pass

    duration_ms = (time.monotonic() - started_monotonic) * 1000.0
    outcome = state.get("outcome", "success")
    if outcome not in ("success", "skipped"):
        raise ValueError(
            f"stage {name!r} declared outcome {outcome!r}; a stage that "
            f"returns normally is 'success' or 'skipped' — a failure is "
            f"raised, not declared, so it carries a category")
    log.info("stage %s: %s (%.1fms)", outcome, name, duration_ms)
    if recorder is not None:
        recorder.record(StageRecord(
            stage_name=name,
            started_at=started_at,
            duration_ms=duration_ms,
            outcome=outcome,
        ))


def run_stage(name: str, fn: Callable[[], Any],
              recorder: StageRecorder | None = None,
              workdir: Any = None, logger: Any = None) -> Any:
    """Run `fn` as one accounted stage and return its result.

    The proposal's `run_stage(name, fn)`. `fn` takes no arguments — a stage
    needing inputs is closed over them at the call site, which keeps this
    signature from growing a `*args` that would make every stage's call
    different from every other's.
    """
    result: dict = {}
    with stage_context(name, recorder=recorder, workdir=workdir,
                       logger=logger) as state:
        result["value"] = fn()
        # A stage function may declare `skipped` by returning the sentinel
        # rather than by reaching into the context state, which keeps the
        # callable form usable without a closure over `state`.
        if result["value"] is SKIPPED:
            state["outcome"] = "skipped"
            result["value"] = None
    return result["value"]


class _Skipped:
    """Sentinel: returned by a stage function that decided not to run."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "SKIPPED"

    def __bool__(self) -> bool:
        return False


SKIPPED = _Skipped()
