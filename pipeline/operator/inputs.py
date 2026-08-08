"""What an operator is asked for: a window, and a disposition per class.

THE INPUT CHANGE. The old operator took a processing DATE — `sys.argv[1]`,
read at import time, defaulting to "today in Pacific time" — and derived
its window from it. That input carried two wrong assumptions.

First, a date is not a window. The gathering pass needs a start and an
end, and deriving them from a date meant the operator could only ever ask
for a calendar day in one fixed timezone. The smoke run's own harness had
to work around it: the staged inputs occupy 2027-10-01 to 2027-10-07, and
a 2026 window gathers zero units while still reporting 109 (field, filter)
pairs — "a confident 109 pairs followed by nothing to submit, which reads
like an empty pipeline rather than a wrong window" (`smoke_run.rst`).

Second, one date said nothing about WHICH work. The operator ran all its
phases on whatever the date selected, so there was no way to ask for
reference construction without also asking for prompt processing, and no
way to say "this class, this pass, but do not act on the rest".

So the input is now a window plus a disposition per declared class. The
remaining classes are named explicitly in every invocation — that is the
"remaining census dispositions per policy class" the scope asks for —
which means an operator invocation is a complete statement about all four
classes rather than a statement about one and silence about three.
"""

import dataclasses
from datetime import datetime, timezone

from pipeline.operator import classes as opclasses

#: Run this class this pass.
RUN = "run"
#: Do not run it, and say so deliberately. The default for every class
#: the invocation does not name: silence about a class must mean "not
#: this pass", never "whatever the code does by default".
HOLD = "hold"
#: Named so an operator can record that a class is declared-not-
#: implemented without that reading as a decision they made.
DECLARED_NOT_IMPLEMENTED = "declared-not-implemented"

DISPOSITIONS = (RUN, HOLD, DECLARED_NOT_IMPLEMENTED)


class InputError(ValueError):
    """The operator was asked for something it cannot act on."""


def _parse(value, field):
    """An ISO-8601 datetime, timezone-aware, UTC where none is given."""
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            raise InputError(
                f"{field}={value!r} is not an ISO-8601 datetime; the "
                f"operator takes a window (start and end), not a "
                f"processing date") from None
    if parsed.tzinfo is None:
        # UTC rather than local: the old operator's Pacific-local date was
        # a second timezone for the same instant, and the gathering
        # queries are all UTC/MJD.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclasses.dataclass(frozen=True)
class OperatorInput:
    """One invocation's complete statement of what to do.

    Attributes
    ----------
    start, end : datetime
        The observation window, timezone-aware.
    dispositions : dict
        Every declared class name -> one of `DISPOSITIONS`. Complete by
        construction: `build` fills the classes the caller did not name.
    """

    start: datetime
    end: datetime
    dispositions: dict

    @property
    def to_run(self):
        """The classes this invocation asks to run, in declaration order."""
        return tuple(c for c in opclasses.CLASSES
                     if self.dispositions.get(c.name) == RUN)

    def disposition_of(self, name):
        return self.dispositions[opclasses.class_for(name).name]

    def as_dict(self):
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "dispositions": dict(self.dispositions),
        }


def build(start, end, dispositions=None):
    """An `OperatorInput`, with the census completed and checked.

    Every declared class ends up with a disposition. A class the caller
    did not name is HOLD — except the two declared-not-implemented ones,
    which take `DECLARED_NOT_IMPLEMENTED` so the record distinguishes
    "the operator chose not to run this" from "this cannot run".

    Asking to RUN an unimplemented class is refused here, at the input,
    rather than deep in a pass: the answer does not depend on anything
    the pass would discover.
    """
    start_dt = _parse(start, "start")
    end_dt = _parse(end, "end")
    if end_dt <= start_dt:
        raise InputError(
            f"the window ends at or before it starts: start={start_dt.isoformat()} "
            f"end={end_dt.isoformat()}")

    given = dict(dispositions or {})
    for name in given:
        # Raises naming the four if this is not one of them.
        opclasses.class_for(name)
    for name, value in given.items():
        if value not in DISPOSITIONS:
            raise InputError(
                f"disposition {value!r} for {name} is not one of "
                f"{', '.join(DISPOSITIONS)}")

    census = {}
    for declared in opclasses.CLASSES:
        if declared.name in given:
            value = given[declared.name]
            if value == RUN and not declared.implemented:
                raise InputError(
                    f"cannot run {declared.name}: {declared.blocked_on}. It "
                    f"is declared so nothing else claims the name; its "
                    f"disposition can only be "
                    f"{DECLARED_NOT_IMPLEMENTED} or {HOLD}.")
            census[declared.name] = value
        elif not declared.implemented:
            census[declared.name] = DECLARED_NOT_IMPLEMENTED
        else:
            census[declared.name] = HOLD

    return OperatorInput(start=start_dt, end=end_dt, dispositions=census)


def add_arguments(parser):
    """The operator's command-line surface.

    `--start`/`--end` are REQUIRED and there is no default window. The
    old operator defaulted to today, which is how a run against staged
    2027 inputs silently gathered nothing.
    """
    parser.add_argument(
        "--start", required=True,
        help="window start, ISO-8601 (UTC assumed if no offset given)")
    parser.add_argument(
        "--end", required=True,
        help="window end, ISO-8601")
    for declared in opclasses.CLASSES:
        allowed = (list(DISPOSITIONS) if declared.implemented
                   else [HOLD, DECLARED_NOT_IMPLEMENTED])
        parser.add_argument(
            f"--{declared.name}", choices=allowed,
            default=None,
            help=(f"disposition for the {declared.name} class"
                  + ("" if declared.implemented
                     else f" (declared, not implemented: {declared.blocked_on})")))
    return parser


def from_namespace(args):
    """Build the input from parsed arguments."""
    dispositions = {}
    for declared in opclasses.CLASSES:
        value = getattr(args, declared.name.replace("-", "_"), None)
        if value is not None:
            dispositions[declared.name] = value
    return build(args.start, args.end, dispositions)
