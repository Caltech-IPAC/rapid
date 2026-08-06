"""
File:    stubs.py

Test doubles shared by the runtime suites.

The database boundary is a recorder — every statement and its parameters are
captured, and the lifecycle state of each simulated row is tracked — rather
than a mock asserting on call counts. That distinction is the point: a crash-
recovery test needs to ask "what state is this attempt in now", which a
call-count mock cannot answer, and a real database cannot answer without a
live connection the unit suite is forbidden.

`RecordingExecutor` implements W1's executor contract exactly as
`ConnectionExecutor` does after the charge-4 fix: rows for a statement with a
result set, an int rowcount for one without. Tests that would pass against a
stub returning None for everything are precisely the tests that missed the
looseness in the first place.
"""

import re
from typing import Any


class RecordingExecutor:
    """An `Executor` that records statements and simulates the attempts table.

    It is not a SQL engine. It recognizes the handful of statement shapes the
    attempt writer issues — the resolver call, the lifecycle UPDATEs, the
    stage INSERT — and maintains just enough state for a test to assert on the
    resulting lifecycle. Anything it does not recognize is recorded and
    answered with a rowcount of 1, so an unrelated statement does not fail a
    test for the wrong reason.
    """

    def __init__(self, next_attempt_id: int = 1000):
        self.calls: list = []
        self.rows: dict = {}
        self.stages: list = []
        self.logical_jobs: dict = {}
        self._next_attempt_id = next_attempt_id
        #: Statement substrings that should raise when executed — how a test
        #: simulates the database being unreachable at one specific step.
        self.fail_on: dict = {}
        #: Attempt ids that do not exist, so an UPDATE naming one matches zero
        #: rows (the charge-4 case).
        self.missing_attempt_ids: set = set()

    # -- the Executor contract ----------------------------------------------

    def __call__(self, statement: Any, params: Any) -> Any:
        return self.execute(statement, params)

    def execute(self, statement: Any, params: Any) -> Any:
        text = str(statement)
        self.calls.append((text, list(params) if params else []))

        for fragment, exc in self.fail_on.items():
            if fragment in text:
                raise exc

        if "resolve_attempt(" in text:
            return self._resolve(params)
        if text.strip().upper().startswith("SELECT"):
            return self._select(text, params)
        if "INSERT INTO logical_jobs" in text:
            # `ON CONFLICT DO NOTHING RETURNING logical_job_id` (FixA, #3):
            # one row back when the insert landed, none when it conflicted.
            # The writer verifies a conflict against the recorded binding
            # rather than ignoring it, so the two cases must be
            # distinguishable here.
            if params[0] in self.logical_jobs:
                return []
            self.logical_jobs[params[0]] = list(params)
            return [(params[0],)]
        if "INSERT INTO attempt_stages" in text:
            self.stages.append(list(params))
            return 1
        if "INSERT INTO attempts" in text:
            return self._insert_attempt(params)
        if "INSERT INTO milestones" in text:
            return 1
        if text.strip().upper().startswith("UPDATE ATTEMPTS"):
            return self._update_attempt(text, params)
        return 1

    # -- simulated behaviour -------------------------------------------------

    def _resolve(self, params: Any) -> Any:
        """Model migration 017's resolver, claim/index split included.

        The claim lands in `application_claim_index`, NOT
        `application_attempt_index` (review finding #9): the latter is the
        DDL's evidence the application RAN, and writing it at claim time was
        what made a container killed between claim and start unclosable as
        terminal-without-start. The started compare-and-set copies the claim
        forward, so a row claimed here is still `submitted` with a NULL
        attempt index — which is exactly the state the started CAS requires.
        """
        run_id, logical_job_id, scheduler_job_id = params[0], params[1], params[2]
        app_index = params[3]

        for attempt_id, row in self.rows.items():
            if (row.get("logical_job_id") == logical_job_id
                    and row.get("application_claim_index") == app_index):
                return [(attempt_id,)]

        attempt_id = self._next_attempt_id
        self._next_attempt_id += 1
        self.rows[attempt_id] = {
            "attempt_id": attempt_id,
            "run_id": run_id,
            "logical_job_id": logical_job_id,
            "scheduler_job_id": scheduler_job_id,
            "application_claim_index": app_index,
            "application_attempt_index": None,
            "scheduler_attempt_index": params[4],
            "lifecycle_state": ("submitted" if logical_job_id in self.logical_jobs
                                or not self.logical_jobs
                                else "missing_or_contradictory"),
        }
        return [(attempt_id,)]

    def _insert_attempt(self, params: Any) -> Any:
        attempt_id = self._next_attempt_id
        self._next_attempt_id += 1
        self.rows[attempt_id] = {
            "attempt_id": attempt_id,
            "lifecycle_state": "submitted",
            "scheduler_job_id": params[3],
        }
        return [(attempt_id,)]

    def _update_attempt(self, text: str, params: Any) -> int:
        # A compare-and-set transition ends `WHERE attempt_id = %s AND
        # lifecycle_state = %s`, so the attempt id is the second-to-last
        # parameter and the required state is the last. An unconditional
        # transition ends `WHERE attempt_id = %s` and the id is last.
        #
        # Modelling the guard is the point (FixA, review finding #10): the
        # started and application-closed transitions became real
        # compare-and-sets, and a stub that returned 1 whatever the WHERE
        # clause said could not tell a CAS from the unconditional UPDATE it
        # replaced — so the tests asserting "a second writer does not
        # overwrite the first" would pass against either.
        guarded = text.rstrip().endswith("AND lifecycle_state = %s")
        if guarded:
            attempt_id = params[-2]
            required_state = params[-1]
        else:
            attempt_id = params[-1]
            required_state = None

        if attempt_id in self.missing_attempt_ids:
            return 0
        row = self.rows.get(attempt_id)
        if row is None:
            return 0
        if required_state is not None \
                and row.get("lifecycle_state") != required_state:
            # The row has left the state this transition may leave. Zero rows
            # matched, which is what the database would report.
            return 0

        # The lifecycle state is the first parameter of every transition
        # UPDATE that sets one; the scheduler-observation UPDATE sets none.
        if "SET lifecycle_state = %s" in text or "SET lifecycle_state = %s," in text:
            row["lifecycle_state"] = params[0]

        if "scheduler_job_id = %s" in text and "COALESCE" not in text:
            row["scheduler_job_id"] = params[0]
        if "scheduler_job_id = COALESCE" in text:
            for value in params:
                if isinstance(value, str) and value.startswith("job-"):
                    row.setdefault("scheduler_job_id", value)
        if "terminal_record_key = %s" in text:
            row["terminal_record_key"] = _param_after(text, params,
                                                      "terminal_record_key")
        return 1

    def _select(self, text: str, params: Any) -> Any:
        if "lifecycle_state FROM attempts" in text:
            row = self.rows.get(params[0])
            if row is None:
                return []
            return [(row["lifecycle_state"],)]
        return []

    # -- assertions helpers --------------------------------------------------

    def state_of(self, attempt_id: int) -> Any:
        row = self.rows.get(attempt_id)
        return None if row is None else row.get("lifecycle_state")

    def statements_matching(self, fragment: str) -> list:
        return [call for call in self.calls if fragment in call[0]]


def _param_after(text: str, params: Any, column: str) -> Any:
    """Find the parameter positionally matching a `column = %s` assignment.

    Crude but adequate: count the `%s` placeholders before the column's
    assignment. Used only to let a test read back what a transition wrote.
    """
    index = text.find(f"{column} = %s")
    if index < 0:
        return None
    position = len(re.findall(r"%s", text[:index]))
    if position < len(params):
        return params[position]
    return None


class FakeLogger:
    """A logger double that records formatted lines."""

    def __init__(self):
        self.lines: list = []

    def _record(self, level: str, msg: Any, *args: Any) -> None:
        try:
            text = str(msg) % args if args else str(msg)
        except Exception:  # noqa: BLE001 - a test double never fails the test
            text = f"{msg!r} {args!r}"
        self.lines.append((level, text))

    def debug(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        self._record("DEBUG", msg, *args)

    def info(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        self._record("INFO", msg, *args)

    def warning(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        self._record("WARNING", msg, *args)

    def error(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        self._record("ERROR", msg, *args)

    def exception(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        self._record("ERROR", msg, *args)

    def text(self) -> str:
        return "\n".join(line for _, line in self.lines)


def make_job_environment(**overrides: Any) -> Any:
    """A valid `JobEnvironment` for tests, overridable field by field."""
    from pipeline.runtime.environment import JobEnvironment

    fields = {
        "manifest_uri": "s3://rapid-manifests/batch-1/manifest.json",
        "batch_id": "batch-1",
        "manifest_checksum": "a" * 64,
        "scheduler_job_id": "job-abc123",
        "attempt_index": 1,
        "queue_name": "rapid-prompt",
        "array_index": 0,
    }
    fields.update(overrides)
    return JobEnvironment(**fields)


def make_ownership(**overrides: Any) -> Any:
    from pipeline.runtime.ownership import AttemptOwnership

    fields = {
        "attempt_id": 1000,
        "run_id": "run-1",
        "logical_job_id": "batch-1:0",
        "scheduler_job_id": "job-abc123",
        "attempt_index": 1,
        "claimed_precreated": True,
    }
    fields.update(overrides)
    return AttemptOwnership(**fields)


def make_provenance(config_digest: str = "d" * 64) -> Any:
    from observability.attempts import Provenance

    return Provenance(
        source_sha="s" * 40,
        container_digest="sha256:" + "c" * 64,
        job_definition_rev="7",
        config_digest=config_digest,
    )
