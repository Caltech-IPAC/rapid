"""The child tables of ``sources`` and ``xsources``: their names, and the children a task reads.

``database/schema.sql`` keeps the rows of ``sources`` and ``xsources`` in
child tables that inherit them, one per UT observation date and SCA,
``<parent>_<yyyymmdd>_<sca>``, made by ``create_catalog_child``. A reader
queries the children its task needs by name, not the parent: a query on
the parent visits every child, and their number grows by up to 18 a
night. A source set's child is found without touching it: the job that
wrote the set also wrote its difference image, whose L2 row's ``mjdobs``
and whose own ``sca`` name the child (:func:`set_children`), for an
imported set as for one the difference stage made.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Iterable

#: The tables whose rows live in child tables (``rapidpipe.foundation.publish.CHILDREN``).
PARENTS = ("sources", "xsources")

#: MJD 0, the day ``create_catalog_child`` counts from; MJD days begin at 0h UT.
MJD_EPOCH = date(1858, 11, 17)


def child_name(parent: str, mjdobs: float, sca: int) -> str:
    """The name ``create_catalog_child`` gives the child of ``parent`` holding rows at ``mjdobs`` on ``sca``.

    Parameters
    ----------
    parent : str
        ``sources`` or ``xsources``.
    mjdobs : float
        The exposure's MJD-OBS.
    sca : int
        The SCA, 1 to 18.

    Returns
    -------
    str
        ``<parent>_<yyyymmdd>_<sca>``, the date the UT day of ``mjdobs``.
    """
    if parent not in PARENTS:
        raise ValueError(f"{parent!r} has no child tables; {PARENTS} do")
    day = MJD_EPOCH + timedelta(days=math.floor(mjdobs))
    return f"{parent}_{day:%Y%m%d}_{int(sca)}"


def set_children(cur, parent: str, jobs: Iterable[int]) -> dict[int, str]:
    """The child of ``parent`` holding each source set's rows, found from its difference image.

    Parameters
    ----------
    cur : psycopg2 cursor
        A cursor of the reading transaction; any cursor factory.
    parent : str
        ``sources`` or ``xsources``.
    jobs : iterable of int
        Source-set jobs (the jobs that wrote the sets).

    Returns
    -------
    dict of int to str
        Each job whose child exists to that child's name. A job with no
        difference image, or whose child was never made (a set with no rows
        of ``parent``), is absent: it has no rows to read.
    """
    jobs = sorted(set(jobs))
    if not jobs:
        return {}
    with cur.connection.cursor() as plain:
        plain.execute("SELECT d.job, l.mjdobs, d.sca FROM diffimages d"
                      " JOIN l2files l ON l.rid = d.rid WHERE d.job = ANY(%s)", (jobs,))
        names = {job: child_name(parent, mjdobs, sca) for job, mjdobs, sca in plain.fetchall()}
        plain.execute("SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                      " WHERE n.nspname = current_schema() AND c.relname = ANY(%s)",
                      (sorted(set(names.values())),))
        made = {r[0] for r in plain.fetchall()}
    return {job: name for job, name in names.items() if name in made}


__all__ = ("MJD_EPOCH", "PARENTS", "child_name", "set_children")
