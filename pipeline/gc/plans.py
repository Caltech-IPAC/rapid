"""Steps 4 and 5 — the checksummed plan, and the wait-and-recompute.

A carved repository over DRAFT 052's tables, following this package's
conventions exactly: connection owned by the caller, never commits, named
records, typed errors, catalog probes for draft-gated objects.

**THE CHECKSUM IS COMPUTED ONCE AND NEVER RECOMPUTED.** §4.11 step 4 asks for
a checksummed plan; the value of the checksum is that it pins WHAT PASS ONE
COMPUTED. A checksum recomputed to match a changed list is evidence of
nothing, so `recompute` records its verdicts as item STATUSES and leaves both
the list and the checksum alone. DRAFT 052's trigger is the backstop.

**A PLAN EXCEEDING ITS BOUND IS REFUSED AT COMPUTATION, NOT TRUNCATED AT
EXECUTION.** Silent truncation reads as "covered everything" when it did not
— the same "no false cleans" failure this arc has hit before. The refusal
happens before any row is written.
"""

import datetime
import hashlib
import json
import typing

from pipeline.gc.references import PlanRefused
from pipeline.repositories.errors import RepositoryQueryFailed

#: SQLSTATE 052 raises for its own invariants — the append-only trigger, the
#: illegal-transition refusal, the frozen terminal item. Classified by code,
#: never by message text.
SQLSTATE_GC_INVARIANT = "RA011"
SQLSTATE_RAISE_EXCEPTION = "P0001"

_SCHEMA_PROBE = (
    "SELECT to_regclass('public.gc_plans') IS NOT NULL"
    "   AND to_regclass('public.gc_plan_items') IS NOT NULL"
    "   AND to_regclass('public.gc_fences') IS NOT NULL")


class GCSchemaAbsent(Exception):
    """DRAFT 052 is not applied, so no plan can be recorded.

    A REFUSAL. Rule 21 requires deletion against a RECORDED plan; with no
    table to record it in there is no conforming way to delete, so the run
    stops rather than deleting unrecorded.
    """

    error_category = "gc_schema_absent"


class PlanBoundExceeded(PlanRefused):
    """More candidates than the plan's configured maximum.

    Refused at computation. Truncating to the bound would delete an arbitrary
    subset while reporting a complete plan.
    """

    error_category = "gc_plan_bound_exceeded"


class Plan(typing.NamedTuple):
    plan_id: int
    state: str
    candidate_checksum: str
    candidate_count: int
    horizon_seconds: object
    approved_by: object


class PlanItem(typing.NamedTuple):
    item_id: int
    bucket: str
    object_key: str
    version_id: str
    object_class: str
    status: str
    #: The attempt the key was canonically attributed to, carried so the
    #: executor can RE-VERIFY that attempt's discharge inside the fence — the
    #: planning-time watermark check is a snapshot, and the terminal-record
    #: sequence can advance after it.
    attributed_attempt_id: object = None


def candidate_checksum(candidates):
    """sha256 over the canonical serialization of the candidate list.

    Ordered by `(bucket, key, version_id)` HERE rather than relying on the
    caller's iteration order: the checksum must be a function of the SET, so
    two computations over the same candidates must agree whatever order they
    were discovered in.
    """
    rows = sorted((c.obj.bucket, c.obj.key, c.obj.version_id, c.object_class)
                  for c in candidates)
    canonical = json.dumps([list(row) for row in rows],
                           sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class GCPlanRepository:
    """Plan writes over a connection the caller owns."""

    def __init__(self, conn):
        self._conn = conn

    def schema_present(self):
        rows = self._query("schema_present", _SCHEMA_PROBE, ())
        return bool(rows and rows[0][0])

    def _require_schema(self):
        if not self.schema_present():
            raise GCSchemaAbsent(
                "DRAFT 052's GC plan schema is not applied, so a deletion "
                "plan cannot be recorded. Rule 21 requires deletion against "
                "a RECORDED plan, so there is no conforming way to proceed: "
                "this run deletes nothing. Apply 052 first.")

    # -- step 4: record the plan ----------------------------------------

    def record_plan(self, *, candidates, retained_counts, inventory,
                    declared_buckets, declared_prefixes, horizon_seconds,
                    horizon_provenance, max_deletions, allowlist, reason,
                    idempotency_key, computed_by):
        """Write one COMPUTED plan and its candidate items.

        The bound is checked BEFORE anything is written, so a refused plan
        leaves no partial row behind.
        """
        self._require_schema()
        if max_deletions is None or int(max_deletions) <= 0:
            raise PlanRefused(
                "a GC plan needs an explicit maximum-deletions bound; there "
                "is no default, because an unbounded plan is one nobody "
                "sized")
        if len(candidates) > int(max_deletions):
            raise PlanBoundExceeded(
                "this plan computed %d candidates, exceeding its configured "
                "maximum of %d. REFUSED AT COMPUTATION rather than truncated "
                "at execution: a truncated plan would delete an arbitrary "
                "subset while reporting itself complete."
                % (len(candidates), int(max_deletions)))

        checksum = candidate_checksum(candidates)
        rows = self._query(
            "record_plan",
            "INSERT INTO gc_plans"
            " (declared_buckets, declared_prefixes, horizon_seconds,"
            "  horizon_provenance, inventory_id, inventory_taken_at,"
            "  inventory_object_count, inventory_complete,"
            "  candidate_checksum, candidate_count, max_deletions,"
            "  retained_counts, allowlist, computed_by, reason,"
            "  idempotency_key)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
            "         %s, %s, %s)"
            " RETURNING plan_id, state, candidate_checksum, candidate_count,"
            "           horizon_seconds, approved_by",
            (list(declared_buckets), list(declared_prefixes),
             horizon_seconds, horizon_provenance, inventory.inventory_id,
             inventory.taken_at, len(inventory.objects),
             bool(inventory.complete), checksum, len(candidates),
             int(max_deletions), json.dumps(retained_counts),
             list(allowlist), computed_by, reason, idempotency_key))
        row = rows[0]
        plan_id = row[0]

        for candidate in candidates:
            self._query(
                "record_plan_item",
                "INSERT INTO gc_plan_items"
                " (plan_id, bucket, object_key, version_id, object_size,"
                "  object_modified, object_class, attributed_attempt_id,"
                "  attributed_prefix, status)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending')"
                " ON CONFLICT (plan_id, bucket, object_key, version_id)"
                "   DO NOTHING",
                (plan_id, candidate.obj.bucket, candidate.obj.key,
                 candidate.obj.version_id, candidate.obj.size,
                 candidate.obj.last_modified, candidate.object_class,
                 candidate.attempt_id, candidate.canonical_prefix))

        return Plan(row[0], row[1], row[2], row[3], row[4], row[5])

    # -- step 5: wait and recompute -------------------------------------

    def recompute(self, plan_id, *, surviving_keys, inventory,
                  recomputed_by):
        """Record the second pass's verdict.

        `surviving_keys` is the set of `(bucket, key, version_id)` triples
        that were absent in BOTH passes with the horizon elapsed between them.
        Every other pending item becomes `excluded-on-recompute`.

        **NOTHING IS DELETED AND THE CHECKSUM IS UNTOUCHED.** A candidate that
        reappeared stays in the plan as an excluded row, which is what makes
        the plan a record of what pass one computed rather than a moving
        summary of what is currently true.
        """
        self._require_schema()
        items = self._query(
            "recompute",
            "SELECT item_id, bucket, object_key, version_id, object_class,"
            "       status FROM gc_plan_items"
            " WHERE plan_id = %s AND status = 'pending'", (plan_id,))
        excluded = 0
        for item in items:
            key = (item[1], item[2], item[3])
            if key in surviving_keys:
                continue
            self._query(
                "recompute",
                "UPDATE gc_plan_items"
                "   SET status = 'excluded-on-recompute', status_reason = %s"
                " WHERE item_id = %s AND status = 'pending'",
                ("absent in the plan pass but not in the recomputation; "
                 "only candidates absent in BOTH passes survive (§4.11 "
                 "step 5)", item[0]))
            excluded += 1

        self._query(
            "recompute",
            "UPDATE gc_plans"
            "   SET state = 'RECOMPUTED', recomputed_at = now(),"
            "       recompute_inventory_id = %s,"
            "       recompute_inventory_taken_at = %s"
            " WHERE plan_id = %s AND state = 'COMPUTED'",
            (inventory.inventory_id, inventory.taken_at, plan_id))
        return excluded

    def approve(self, plan_id, *, approved_by, reason):
        """Record approval — a DISTINCT act with its own actor.

        Self-approval by the computing actor is permitted and RECORDED AS
        SUCH (P-H4): RAPID is a single-operator system today, and a
        second-actor requirement would make the mechanism unusable rather
        than safer. The record is what keeps it reviewable — `computed_by`
        and `approved_by` are both stored and their equality is visible.
        """
        self._require_schema()
        rows = self._query(
            "approve",
            "UPDATE gc_plans SET state = 'APPROVED', approved_by = %s,"
            "                    approved_at = now()"
            " WHERE plan_id = %s AND state = 'RECOMPUTED'"
            " RETURNING plan_id, computed_by, approved_by", (approved_by,
                                                             plan_id))
        if not rows:
            raise PlanRefused(
                "plan %s could not be approved: it must be in RECOMPUTED "
                "state, which means the second pass has run. Approval before "
                "recomputation would defeat the two-pass requirement."
                % (plan_id,))
        return {"plan_id": rows[0][0], "computed_by": rows[0][1],
                "approved_by": rows[0][2],
                "self_approved": rows[0][1] == rows[0][2]}

    def begin_execution(self, plan_id):
        self._require_schema()
        rows = self._query(
            "begin_execution",
            "UPDATE gc_plans SET state = 'EXECUTING'"
            " WHERE plan_id = %s AND state IN ('APPROVED', 'EXECUTING')"
            " RETURNING plan_id", (plan_id,))
        if not rows:
            raise PlanRefused(
                "plan %s is not APPROVED, so it cannot execute. Compute, "
                "recompute, approve and execute are distinct recorded steps: "
                "no code path deletes an object it computed candidacy for in "
                "the same breath." % (plan_id,))
        return plan_id

    def complete(self, plan_id):
        self._require_schema()
        self._query(
            "complete",
            "UPDATE gc_plans SET state = 'COMPLETE', executed_at = now()"
            " WHERE plan_id = %s AND state = 'EXECUTING'", (plan_id,))

    def abandon(self, plan_id, reason):
        self._require_schema()
        self._query(
            "abandon",
            "UPDATE gc_plans SET state = 'ABANDONED', abandoned_reason = %s"
            " WHERE plan_id = %s AND state <> 'COMPLETE'", (reason, plan_id))

    # -- reads -----------------------------------------------------------

    def plan(self, plan_id):
        self._require_schema()
        rows = self._query(
            "plan",
            "SELECT plan_id, state, candidate_checksum, candidate_count,"
            "       horizon_seconds, approved_by FROM gc_plans"
            " WHERE plan_id = %s", (plan_id,))
        if not rows:
            return None
        row = rows[0]
        return Plan(row[0], row[1], row[2], row[3], row[4], row[5])

    def unresolved_items(self, plan_id):
        """Items a (possibly resumed) execution must still act on.

        `pending` and `in-flight` only. A resumed execution acts ONLY on
        unresolved items — `deleted` and `already-absent` are terminal, and
        DRAFT 052's trigger refuses to re-open them.
        """
        self._require_schema()
        rows = self._query(
            "unresolved_items",
            "SELECT item_id, bucket, object_key, version_id, object_class,"
            "       status, attributed_attempt_id FROM gc_plan_items"
            " WHERE plan_id = %s AND status IN ('pending', 'in-flight')"
            " ORDER BY item_id", (plan_id,))
        return [PlanItem(*row) for row in rows]

    def verify_checksum(self, plan_id):
        """Re-derive the checksum from the recorded items and compare.

        Verified BEFORE execution (§4.11 step 4's whole purpose). This reads
        the stored items rather than recomputing candidacy: the question is
        "is this plan still the plan that was computed", not "would the same
        plan be computed now".
        """
        self._require_schema()
        rows = self._query(
            "verify_checksum",
            "SELECT candidate_checksum FROM gc_plans WHERE plan_id = %s",
            (plan_id,))
        if not rows:
            raise PlanRefused("no plan %s" % (plan_id,))
        recorded = rows[0][0]

        items = self._query(
            "verify_checksum",
            "SELECT bucket, object_key, version_id, object_class"
            "  FROM gc_plan_items WHERE plan_id = %s", (plan_id,))
        canonical = json.dumps(
            [list(row) for row in sorted(tuple(item) for item in items)],
            sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        derived = "sha256:" + hashlib.sha256(
            canonical.encode("utf-8")).hexdigest()
        if derived != recorded:
            raise PlanRefused(
                "plan %s's candidate list no longer matches its recorded "
                "checksum (recorded %s, derived %s). The plan is REFUSED: a "
                "plan whose list changed after computation is not the plan "
                "that was reviewed." % (plan_id, recorded, derived))
        return recorded

    # -- plumbing --------------------------------------------------------

    def _query(self, method, sql, params):
        """Execute and fetch, re-typing failures — except a raised invariant.

        NOT ROLLED BACK HERE, for this package's usual reason: the caller owns
        the transaction boundary.
        """
        try:
            with self._conn.cursor() as cur:
                cur.execute(sql, params)
                if cur.description is None:
                    return []
                return cur.fetchall()
        except Exception as exc:                      # noqa: BLE001
            if _is_invariant_violation(exc):
                raise
            raise RepositoryQueryFailed(method, str(exc)) from exc


def _is_invariant_violation(exc):
    code = getattr(exc, "pgcode", None) or getattr(exc, "sqlstate", None)
    return code in (SQLSTATE_GC_INVARIANT, SQLSTATE_RAISE_EXCEPTION)
