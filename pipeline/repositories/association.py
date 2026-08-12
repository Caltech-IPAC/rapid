"""The association-ordering repository (rule 19, brief F2).

The two reads the claim path makes: the live lane's watermark, and the
earliest processing date still owing crossmatch work.

**WHY A REPOSITORY AND NOT A `RAPIDDB` METHOD.** `RAPIDDB` is frozen — brief
G's ratified merge decision, restated in D's `products.py`: "no new method
lands in it". It is the legacy handle whose methods set an `exit_code`
attribute instead of raising, which is the failure signal
`pipeline/repositories/errors.py` exists to replace. An earlier revision of
this work added these two queries to `RAPIDDB` and was correctly refused;
they belong here, over a connection the caller owns, which is G's established
pattern and the one `DiffImageRepository` and `ProductRepository` already
follow.

**WHY THE WORK INVENTORY IS DEFINED ONCE.** The cross-date gate has to answer
"which (date, field) pairs are science work" — exactly the question
`RAPIDDB.get_fields_with_science_jobs_for_processing_date` already answers for
one date. An earlier revision answered it a SECOND time, independently, over
`Attempts ⋈ logical_jobs` filtered on `job_type = 'science'`, and asserted in
a comment that the two "read off the same fact" and "cannot disagree".

They can. The sibling reads `DiffImages` joined to `Attempts`, filtered on
`ppid`, `vbest = 1`, `rapid_outcome = 'success'` and a created-date window;
the `logical_jobs` formulation reads a different table family through a
different join and applies none of those four predicates. A science attempt
that succeeded but whose difference image was superseded (`vbest = 0`) is
science work to one and not the other. So is a row outside the created
window. Two independently written queries agreeing is a claim, and a claim in
a comment is not a mechanism.

`_SCIENCE_WORK_PREDICATE` below is that fact, written ONCE and shared: the
per-date gate's own predicate, generalised over dates by dropping only the
date bound. `science_work_inventory` and the sibling therefore cannot
disagree by construction, and
`pipeline/contract/test_association_work_inventory.py` pins that against real
fixture rows across the edge states rather than trusting this paragraph.

**THIS REPOSITORY NEVER COMMITS AND NEVER OPENS A CONNECTION.** It takes the
connection its caller owns. The claim-path reads are deliberately NOT inside
the acceptance transaction — they happen in a gathering pass, minutes earlier
— which is exactly why F3's acceptance path re-reads the watermark under the
lane lease before it CASes.
"""

import typing

from pipeline.repositories.errors import RepositoryQueryFailed

#: The probe for DRAFT 049. Asked of the catalog rather than inferred from a
#: failing query: "this schema is not deployed" and "this query is wrong" are
#: two facts a caller must never conflate, because conflating them turns a
#: broken query into a silent degradation of the ordering guarantee.
_SCHEMA_PROBE = "SELECT to_regclass('public.association_watermarks')"

_WATERMARK_SQL = (
    "SELECT w.watermark_proc_date, w.watermark_field"
    "  FROM association_watermarks w"
    " WHERE w.association_set = COALESCE(%s, derived.live_association_set())"
    "   AND w.lane = COALESCE(%s, 0)"
)

#: THE SCIENCE-WORK PREDICATE, DEFINED ONCE.
#:
#: This is `RAPIDDB.get_fields_with_science_jobs_for_processing_date`'s own
#: predicate with the date bound removed and the date SELECTed instead, so the
#: per-date gate and the cross-date gate are the same question asked over
#: different ranges rather than two questions hoped to match.
#:
#: Every filter is carried over deliberately and none is "tidied":
#:
#:   ppid              the pipeline the row belongs to, science's.
#:   vbest = 1         the CURRENT difference image. A superseded row is not
#:                     work owed — its successor is.
#:   rapid_outcome     'success'. A failed science attempt produced no
#:                     difference image to crossmatch.
#:   d.field NOT NULL  the field is the unit's own identity component.
#:
#: The created-date window is what makes `d.created` a processing DATE, and it
#: is expressed here as a truncation to the day rather than the sibling's
#: half-open `>= proc_date AND < proc_date + 1 day` bounds. THE TWO ARE THE
#: SAME SET — `date_trunc('day', d.created)` is in the day iff `d.created` is
#: within that half-open interval — and the contract test asserts exactly that
#: equivalence on a row placed at each edge, because "obviously the same" is
#: how off-by-one day boundaries survive review.
_SCIENCE_WORK_PREDICATE = (
    "    SELECT to_char(date_trunc('day', d.created), 'YYYYMMDD') AS pd,"
    "           d.field AS field"
    "      FROM DiffImages d"
    "      JOIN Attempts a ON a.attempt_id = d.attempt_id"
    "     WHERE d.ppid = %s"
    "       AND d.vbest = 1"
    "       AND a.rapid_outcome = 'success'"
    "       AND d.field IS NOT NULL"
)

#: A (date, field) pair is ACCEPTED when it has a crossmatch attempt that is
#: pending or succeeded — the same blocking predicate
#: `get_fields_with_blocking_crossmatch_attempt_for_processing_date` uses for
#: the resubmission gate, so "in flight or done" means one thing in this
#: codebase. A pair whose crossmatch attempts all FAILED is owed, which is
#: what makes a failed-and-retryable unit block its successors rather than
#: being stepped over.
_ACCEPTED_WORK_PREDICATE = (
    "    SELECT to_char(la.processing_date, 'YYYYMMDD') AS pd,"
    "           la.field AS field"
    "      FROM Attempts la"
    "      JOIN logical_jobs lj ON lj.logical_job_id = la.logical_job_id"
    "     WHERE lj.job_type = %s"
    "       AND la.field IS NOT NULL"
    "       AND (la.lifecycle_state IN ('submitted','started')"
    "            OR la.rapid_outcome = 'success')"
)

_EARLIEST_OWED_SQL = (
    "WITH science AS ("
    + _SCIENCE_WORK_PREDICATE +
    "), accepted AS ("
    + _ACCEPTED_WORK_PREDICATE +
    "), wm AS ("
    "    SELECT w.watermark_proc_date AS wd"
    "      FROM association_watermarks w"
    "     WHERE w.association_set ="
    "           COALESCE(%s, derived.live_association_set())"
    "       AND w.lane = 0"
    ") "
    "SELECT min(s.pd) FROM science s"
    "  LEFT JOIN accepted a ON a.pd = s.pd AND a.field = s.field"
    "  CROSS JOIN wm"
    " WHERE a.field IS NULL"
    "   AND (wm.wd IS NULL OR s.pd >= wm.wd)"
)

#: The work inventory itself, for the agreement test. Same two CTEs, no
#: aggregation — so the test compares the inventory this repository derives
#: against the sibling gate's own answer, pair by pair, rather than comparing
#: two summaries that could agree by luck.
_WORK_INVENTORY_SQL = (
    "WITH science AS ("
    + _SCIENCE_WORK_PREDICATE +
    ") "
    "SELECT s.pd, s.field FROM science s"
    " WHERE (%s IS NULL OR s.pd = %s)"
    " ORDER BY s.pd, s.field"
)


class Watermark(typing.NamedTuple):
    """One lane's claim frontier.

    `(None, None)` is the ORIGIN — the row exists and nothing has been
    accepted in this set yet — which is a normal day-one state and must not be
    confused with "no row", which the repository reports as `None` and means
    the ordering schema is not deployed.
    """

    proc_date: typing.Optional[str]
    field: typing.Optional[int]


class AssociationRepository:
    """The claim path's reads, over a connection the caller owns."""

    def __init__(self, conn):
        self._conn = conn

    def _execute(self, method, statement, params):
        try:
            with self._conn.cursor() as cur:
                cur.execute(statement, params)
                return cur.fetchall()
        except Exception as exc:  # noqa: BLE001 - re-raised as a typed error
            raise RepositoryQueryFailed(method, str(exc)) from exc

    def ordering_schema_present(self):
        """Is DRAFT 049 applied on this database?

        Separate from the reads so a caller can tell "not deployed" from
        "deployed and empty" without interpreting a `None`.
        """
        rows = self._execute("ordering_schema_present", _SCHEMA_PROBE, None)
        return bool(rows) and rows[0][0] is not None

    def claim_position(self, association_set=None, lane=None):
        """The live lane's watermark, or `None` when 049 is not applied.

        `None` is the schema-absent answer and only that. A deployed-but-
        unaccepted lane answers `Watermark(None, None)`, and the claim path
        treats the two differently: the first degrades to unordered gathering,
        the second is the origin of the canonical order with everything ahead
        of it.
        """
        if not self.ordering_schema_present():
            return None

        rows = self._execute("claim_position", _WATERMARK_SQL,
                             (association_set, lane))
        if not rows:
            return None

        proc_date, field = rows[0]
        return Watermark(None if proc_date is None else str(proc_date),
                         None if field is None else int(field))

    def earliest_unaccepted_date(self, association_set=None):
        """The earliest processing date still owing crossmatch work.

        The CROSS-DATE half of the ordering gate. Gathering is invoked once
        per processing date, so the watermark alone cannot tell a pass for
        date d2 that d1 is still in retry; this answers exactly that, and a
        later date is not claimable while this names an earlier one.

        Returns `None` when the ordering schema is absent OR when nothing is
        owed. Both readings make the same decision in the caller — do not
        block this date on an earlier one — so collapsing them costs nothing;
        the watermark read is what distinguishes an ordered deployment from an
        unordered one.
        """
        if not self.ordering_schema_present():
            return None

        from submission.routes import (JOB_TYPE_CROSSMATCH, JOB_TYPE_SCIENCE,
                                       ppid_for)

        rows = self._execute(
            "earliest_unaccepted_date", _EARLIEST_OWED_SQL,
            (ppid_for(JOB_TYPE_SCIENCE), JOB_TYPE_CROSSMATCH,
             association_set))
        if not rows or rows[0][0] is None:
            return None
        return str(rows[0][0])

    def science_work_inventory(self, proc_date=None):
        """Every (date, field) pair that is science work, optionally one date.

        Exists for the agreement test: it exposes the SAME `science` CTE the
        cross-date gate aggregates over, so the test can compare this
        inventory against `RAPIDDB.get_fields_with_science_jobs_for_
        processing_date`'s own answer pair by pair. Comparing two summaries
        could agree by luck; comparing the inventories cannot.
        """
        from submission.routes import JOB_TYPE_SCIENCE, ppid_for

        rows = self._execute(
            "science_work_inventory", _WORK_INVENTORY_SQL,
            (ppid_for(JOB_TYPE_SCIENCE), proc_date, proc_date))
        return [(str(pd), int(field)) for pd, field in rows]
