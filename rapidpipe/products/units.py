"""`register`'s producer-keyed unit id (runs page, "Rules").

`register` is the one stage whose unit id is not its own: "`register`'s own
unit id is `<producing stage>/<producing unit id>`... derived from the
manifest `register` reads: one `register` unit follows every producer"
(runs page, "Rules"). Every other stage's unit id is the bare unit; a
caller walking a run's selected stages back for the nearest producing
stage skips a `register` entry, since it never produces for the stage
after it. This module is the one place that knowledge lives, so a caller
names :func:`takes_producer_unit` instead of comparing a stage name to
the literal ``"register"``, and :func:`register_unit_id`/
:func:`nominal_unit_id` instead of hand-building or splitting the
``<producer>/<unit>`` string.
"""

from __future__ import annotations

#: The one stage whose unit id is keyed by its producer.
REGISTER = "register"


def takes_producer_unit(stage: str) -> bool:
    """Whether `stage`'s unit id is `<producer>/<unit>`, not the bare unit.

    True only for `register` (runs page, "Rules").
    """
    return stage == REGISTER


def register_unit_id(producer: str, unit_id: str) -> str:
    """The `register` unit id for `unit_id`, produced by `producer`."""
    return f"{producer}/{unit_id}"


def nominal_unit_id(unit_id: str) -> str:
    """`unit_id` without its `<producer>/` prefix, if it has one.

    For a `register` unit id (``<producer>/<unit>``) this is the bare
    unit; any other unit id has no ``/`` and is returned unchanged.
    """
    return unit_id.split("/", 1)[1] if "/" in unit_id else unit_id
