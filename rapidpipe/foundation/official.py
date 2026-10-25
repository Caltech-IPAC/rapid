"""Official bindings: one change operation, and undo (Part 1 statement 5).

A change group is a list of items, each a dict with either or both of:

``close``
    the id of an open binding to close;
``open``
    a product binding ``{"product", "key", "job"}`` or a live binding
    ``{"survey", "dataset", "run"}`` to open.

The group gets one new id from the ``change_groups`` sequence. Every close
names its binding and refuses the whole group if that binding is no longer
open; every open refuses it if its key, or its survey and dataset, already
has an open binding (the partial unique indexes). Closes run first, in
binding-id order, so two groups closing the same bindings queue on the
first row rather than deadlock; the second wakes, finds the row closed and
refuses. Nothing else serialises a group: no table lock, no lock table.
"""

from __future__ import annotations

from typing import Iterable

import psycopg2
from psycopg2.extras import Json


class NotFound(LookupError):
    """A run, job, group or row the foundation looked up and did not find: exit 64 at the CLI.

    The one lookup class the CLI maps to a usage error; a ``KeyError`` or
    ``IndexError`` from stage code is a bug and reaches the 70 path.
    """


class Refused(Exception):
    """A change group the official table refused; ``binding`` names the binding, when one is."""

    def __init__(self, message: str, binding: int | None = None):
        super().__init__(message)
        self.binding = binding


def apply(cur, group: Iterable[dict], who: str, why: str) -> int:
    """Apply ``group`` on ``cur``'s open transaction and return its group id.

    Does not commit: :func:`change` and the publisher's automatic binding
    are the two callers that own the transaction. Raises :class:`Refused`.
    """
    group = list(group)
    cur.execute("SELECT nextval('change_groups')")
    group_id = cur.fetchone()[0]
    for binding in sorted(item["close"] for item in group if item.get("close") is not None):
        cur.execute(
            "UPDATE official SET closed_group = %s, closed_by = %s, closed_at = now(),"
            " closed_why = %s WHERE id = %s AND closed_at IS NULL",
            (group_id, who, why, binding))
        if cur.rowcount == 0:
            raise Refused(f"binding {binding} is no longer open", binding)
    for item in group:
        new = item.get("open")
        if new is not None:
            _open(cur, group_id, new, who, why)
    return group_id


def _open(cur, group_id: int, new: dict, who: str, why: str) -> None:
    if "run" in new:
        where, params = "survey = %s AND dataset = %s AND run IS NOT NULL", (new["survey"], new["dataset"])
        what = f"survey {new['survey']!r} dataset {new['dataset']!r}"
        values = (None, None, None, new["run"], new["survey"], new["dataset"])
    else:
        where, params = "product = %s AND key = %s AND job IS NOT NULL", (new["product"], Json(new["key"]))
        what = f"{new['product']} {new['key']}"
        values = (new["product"], Json(new["key"]), new["job"], None, None, None)
    cur.execute(f"SELECT id FROM official WHERE {where} AND closed_at IS NULL", params)
    held = cur.fetchone()
    if held is not None:
        raise Refused(f"{what} already has open binding {held[0]}", held[0])
    try:
        cur.execute(
            "INSERT INTO official (product, key, job, run, survey, dataset,"
            " opened_group, opened_by, opened_why) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (*values, group_id, who, why))
    except psycopg2.errors.UniqueViolation as exc:
        # Another transaction opened a binding on the same key and committed first.
        raise Refused(f"{what} already has an open binding") from exc


def change(conn, group: Iterable[dict], who: str, why: str) -> int:
    """Apply one change group in one transaction; return its group id or raise :class:`Refused`."""
    try:
        with conn.cursor() as cur:
            group_id = apply(cur, group, who, why)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return group_id


def undo(conn, group_id: int, who: str, why: str) -> int:
    """Apply the inverse of change group ``group_id`` through :func:`change`; return the new group id.

    The inverse closes every binding the group opened and opens, as new
    rows, every binding it closed; it never reopens a closed row, and it
    refuses on the same rule as any group, naming the first binding that
    has since moved.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM official WHERE opened_group = %s ORDER BY id", (group_id,))
        opened = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT product, key, job, survey, dataset, run FROM official"
                    " WHERE closed_group = %s ORDER BY id", (group_id,))
        closed = cur.fetchall()
    if not opened and not closed:
        conn.rollback()
        raise NotFound(f"change group {group_id} opened and closed nothing")
    inverse = [{"close": binding} for binding in opened]
    for product, key, job, survey, dataset, run in closed:
        inverse.append({"open": {"product": product, "key": key, "job": job} if job is not None
                        else {"survey": survey, "dataset": dataset, "run": run}})
    return change(conn, inverse, who, why)
