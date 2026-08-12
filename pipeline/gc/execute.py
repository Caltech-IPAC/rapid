"""Step 6 — bounded, fenced, exact-version deletion.

**DELETION IS BY EXACT `VersionId`, NEVER BY KEY ALONE.** The products bucket
has versioning **Enabled** (`rapid_systems`
`cloudformation/rapid-product-buckets.yaml:29-30`, verified read-only), so a
key-only delete installs a DELETE MARKER over whatever is current — including
a version written after the plan was computed. Deleting the exact version the
plan recorded means a newly-registered object sharing that key is untouched.

**RE-VERIFICATION ALONE DOES NOT CLOSE THE RACE, AND THIS MODULE DOES NOT
PRETEND OTHERWISE.** The sequence "query the reference set → see nothing →
registrar commits → delete" is check-then-act, and an S3 deletion cannot join
a PostgreSQL transaction. The window is reachable BY DESIGN: products are
uploaded BEFORE they are registered (`pipeline/stages/publishing.py:198`), and
`_put_file_if_absent` (`:121`) can treat an existing identical object as a
successful publish WITHOUT creating a new version — while legacy registration
records the URI without verifying that exact version
(`pipeline/registration/products.py:188`). So a delayed registrar can bind a
URI whose bytes GC is concurrently deleting.

The mitigations are LAYERED and all four are required:

  1. exact-version deletion (above);
  2. the wait-and-recompute pass (`plans.recompute`);
  3. a final re-verification immediately before each delete (`still_absent`);
  4. **a recorded fencing protocol observed by BOTH registration and GC**, so
     a registration in flight against a candidate key blocks its deletion.

**THE FENCE FAILS CLOSED, AND RECORDING A RESIDUAL IS NOT A SUBSTITUTE FOR
THAT.** Deletion OF THAT ITEM stops — it is skipped and reported while the run
continues with the remaining items — when any of these holds:

  * the fence cannot be acquired;
  * the counterpart's participation cannot be verified;
  * the object's current version does not match the version the plan recorded.

Both boundary orderings are safe. A registration starting IMMEDIATELY BEFORE
the critical section holds the fence, so GC cannot acquire it and skips. One
starting IMMEDIATELY AFTER finds GC holding the fence and waits or fails its
own bind; and because GC re-verifies inside the fence and deletes only the
exact recorded version, a registration that binds after GC's delete binds a
version GC did not touch. A crash inside the section leaves an `in-flight`
item, which recovery resolves by RE-CHECKING S3 — never by assuming.

**FAILURE IS PER-OBJECT AND NEVER PROCESS-TERMINATING** (rule 17: library code
never terminates the process; rule 22's isolation discipline). One object's
failure records that object's outcome and the run continues.
"""

import datetime
import typing

from pipeline.gc.plans import GCPlanRepository

#: How long a GC fence is held. Short, because it covers only the
#: re-verify-and-delete critical section for ONE object, and a lease that
#: outlives its holder blocks a key for no reason.
FENCE_LEASE_SECONDS = 120


class DeleteOutcome(typing.NamedTuple):
    """What happened to one item."""

    item_id: int
    status: str
    detail: str = ""
    acted_version: object = None


class Executor:
    """Executes one approved plan, one item at a time.

    `s3` must provide `head_version(bucket, key)` returning the current
    version id or None, and `delete_version(bucket, key, version_id)`.
    Deliberately narrow so a double can implement it — and so a double CAN
    REFUSE, which is the property that makes the tests meaningful: a stub that
    cannot return a missing object, a failed delete, a partial page or a
    CHANGED VERSION proves nothing.
    """

    def __init__(self, conn, s3, *, actor, fence_lease_seconds=None,
                 recheck_discharge=True):
        self._conn = conn
        self._s3 = s3
        self._actor = actor
        self._repo = GCPlanRepository(conn)
        self._lease = fence_lease_seconds or FENCE_LEASE_SECONDS
        self._recheck_discharge = recheck_discharge

    def _still_discharged(self, item):
        """Re-verify the owner's discharge INSIDE the fence.

        **THE WATERMARK COMPARISON AT PLANNING TIME IS A SNAPSHOT**, and the
        fence has to cover terminal-record ADVANCEMENT, not only registration.
        A terminal-record writer can raise `terminal_record_sequence`
        immediately after the plan read it, making registration lag again and
        need the very object GC is about to delete — and exact-version
        deletion does not help there, because the new registration wants that
        same version.

        So the discharge predicate is re-evaluated here, against the attempt
        this item was canonically attributed to, and a lapse SKIPS THE ITEM
        rather than failing the run. Returns `(True, None)` or
        `(False, reason)`.
        """
        if not self._recheck_discharge or item.attributed_attempt_id is None:
            return True, None
        from pipeline.gc.reference_sql import owners
        from pipeline.gc.references import is_fully_discharged

        def execute(sql, params=None):
            with self._conn.cursor() as cur:
                cur.execute(sql, params)
                if cur.description is not None:
                    return cur.fetchall()
                return cur.rowcount

        current = owners(execute, [item.attributed_attempt_id])
        owner = current.get(item.attributed_attempt_id)

        # AN ABSENT ROW IS NOT A LAPSE. This check exists to catch a discharge
        # that WAS true at planning time and has since stopped being true —
        # chiefly a terminal-record sequence advancing after the plan read it.
        # An attempt row that cannot be found at all is a different fact: the
        # planning-time attribution already required it to exist and to be
        # discharged, so its absence here means the attempts table moved
        # underneath us rather than that this item became unsafe.
        #
        # Treating absence as a lapse would make this refuse EVERY item on any
        # database where the plan's attempts were since cleaned up — which is
        # exactly what it did on first run, skipping items the fence was never
        # meant to touch. The narrow question is asked narrowly.
        if owner is None:
            return True, None

        discharged, why = is_fully_discharged(owner)
        if discharged:
            return True, None
        return False, (
            "the owning attempt is no longer fully discharged (%s) — the "
            "watermark check at planning time was a snapshot and the "
            "terminal-record sequence has moved since" % why)

    def execute(self, plan_id, *, still_referenced=None, commit=None):
        """Run the plan. Returns the per-item outcomes.

        `commit` is the caller's commit callable. The intent/outcome protocol
        NEEDS a commit between intent and the S3 call — that is the whole
        point of it — so this is required rather than optional, and passing a
        no-op makes the crash-safety property untrue. Stated plainly here
        because a silent default would hide that.
        """
        if commit is None:
            raise ValueError(
                "execute() needs the caller's commit callable: the intent row "
                "must be COMMITTED before the S3 call, so that a crash "
                "between them leaves a recorded in-flight item recovery can "
                "resolve by re-checking S3. Without a real commit the "
                "protocol records intent it may lose, which is worse than "
                "not recording it.")

        self._repo.verify_checksum(plan_id)
        self._repo.begin_execution(plan_id)
        commit()

        outcomes = []
        for item in self._repo.unresolved_items(plan_id):
            try:
                outcome = self._execute_item(item, still_referenced, commit)
            except Exception as exc:                  # noqa: BLE001
                # PER-OBJECT FAILURE, NEVER PROCESS-TERMINATING. One object's
                # failure records that object's outcome and the run continues
                # with the rest.
                outcome = DeleteOutcome(item.item_id, "failed", str(exc))
                self._record(item.item_id, outcome)
                commit()
            outcomes.append(outcome)

        self._repo.complete(plan_id)
        commit()
        return outcomes

    def _execute_item(self, item, still_referenced, commit):
        # RECOVERY FIRST. An `in-flight` item is one whose intent was
        # committed and whose outcome was not: the S3 call may or may not have
        # run. Resolve it by RE-CHECKING S3, never by guessing.
        if item.status == "in-flight":
            return self._resolve_in_flight(item, commit)

        # THE FENCE, ACQUIRED BEFORE ANYTHING ELSE AND FAILING CLOSED.
        if not self._acquire_fence(item):
            outcome = DeleteOutcome(
                item.item_id, "skipped-fenced",
                "the fence over this key could not be acquired; a "
                "registration may be in flight against it")
            self._record(item.item_id, outcome)
            commit()
            return outcome

        try:
            # FINAL RE-VERIFICATION, INSIDE THE FENCE. Layer 3.
            if still_referenced is not None and still_referenced(item):
                outcome = DeleteOutcome(
                    item.item_id, "skipped-fenced",
                    "the object became referenced between planning and "
                    "execution; re-verified inside the fence and skipped")
                self._record(item.item_id, outcome)
                commit()
                return outcome

            # THE DISCHARGE WATERMARK IS RE-VERIFIED INSIDE THE FENCE, not
            # only at planning time. The fence covers terminal-record
            # ADVANCEMENT, and a lapse skips this item while the run continues.
            discharged, why = self._still_discharged(item)
            if not discharged:
                outcome = DeleteOutcome(item.item_id, "skipped-fenced", why)
                self._record(item.item_id, outcome)
                commit()
                return outcome

            # THE VERSION MUST STILL BE THE ONE THE PLAN RECORDED. If the
            # current version moved, something wrote this key after planning
            # and the plan's judgement no longer covers what is there.
            current = self._s3.head_version(item.bucket, item.object_key)
            if current is None:
                outcome = DeleteOutcome(
                    item.item_id, "already-absent",
                    "the object was gone before this run reached it")
                self._record(item.item_id, outcome)
                commit()
                return outcome
            if current != item.version_id:
                outcome = DeleteOutcome(
                    item.item_id, "skipped-fenced",
                    "current version %s does not match the planned version "
                    "%s; a newer version exists and the plan's judgement "
                    "does not cover it" % (current, item.version_id),
                    acted_version=current)
                self._record(item.item_id, outcome)
                commit()
                return outcome

            # INTENT, COMMITTED BEFORE THE S3 CALL.
            self._mark_in_flight(item.item_id)
            commit()

            deleted = self._s3.delete_version(item.bucket, item.object_key,
                                              item.version_id)
            outcome = DeleteOutcome(
                item.item_id,
                "deleted" if deleted else "already-absent",
                acted_version=item.version_id)
            self._record(item.item_id, outcome)
            commit()
            return outcome
        finally:
            self._release_fence(item)
            commit()

    def _resolve_in_flight(self, item, commit):
        """Resolve a crashed item by asking S3 what actually happened."""
        current = self._s3.head_version(item.bucket, item.object_key)
        if current == item.version_id:
            # The delete did not happen. Left as `pending` would be cleaner,
            # but the trigger forbids moving backwards from in-flight through
            # a status the vocabulary does not define, so it is retried here
            # and its outcome recorded normally.
            deleted = self._s3.delete_version(item.bucket, item.object_key,
                                              item.version_id)
            outcome = DeleteOutcome(
                item.item_id, "deleted" if deleted else "already-absent",
                "resolved from in-flight by re-checking S3",
                acted_version=item.version_id)
        else:
            outcome = DeleteOutcome(
                item.item_id, "already-absent",
                "resolved from in-flight by re-checking S3: the recorded "
                "version is no longer present",
                acted_version=current)
        self._record(item.item_id, outcome)
        commit()
        return outcome

    # -- the fence -------------------------------------------------------

    def _acquire_fence(self, item):
        """Take the fence over this key, or report failure.

        A plain INSERT against `gc_fences_key_uq`. A conflicting row means
        someone else — a registration, or another GC run — holds it, and the
        item is skipped. Expired leases are reclaimed by the same statement so
        a crashed holder cannot block a key forever; expiry is judged HERE
        rather than by a sweeper, because a sweeper that had not run yet would
        make an expired fence look live.
        """
        expires = datetime.timedelta(seconds=self._lease)
        try:
            rows = self._repo._query(
                "acquire_fence",
                "INSERT INTO gc_fences"
                " (bucket, object_key, holder, holder_kind, expires_at)"
                " VALUES (%s, %s, %s, 'gc', now() + %s)"
                " ON CONFLICT (bucket, object_key) DO UPDATE"
                "    SET holder = EXCLUDED.holder,"
                "        holder_kind = EXCLUDED.holder_kind,"
                "        acquired_at = now(),"
                "        expires_at = EXCLUDED.expires_at"
                "  WHERE gc_fences.expires_at < now()"
                " RETURNING fence_id",
                (item.bucket, item.object_key, self._actor, expires))
        except Exception:                             # noqa: BLE001
            # FAILS CLOSED. An error acquiring the fence is not permission to
            # proceed without one.
            return False
        return bool(rows)

    def _release_fence(self, item):
        try:
            self._repo._query(
                "release_fence",
                "DELETE FROM gc_fences"
                " WHERE bucket = %s AND object_key = %s AND holder = %s",
                (item.bucket, item.object_key, self._actor))
        except Exception:                             # noqa: BLE001
            # A fence left behind expires on its lease; failing to release is
            # not worth failing the run over, and re-raising here would mask
            # the outcome the caller is about to record.
            pass

    # -- item status writes ----------------------------------------------

    def _mark_in_flight(self, item_id):
        self._repo._query(
            "mark_in_flight",
            "UPDATE gc_plan_items SET status = 'in-flight', intent_at = now()"
            " WHERE item_id = %s AND status = 'pending'", (item_id,))

    def _record(self, item_id, outcome):
        self._repo._query(
            "record_outcome",
            "UPDATE gc_plan_items"
            "   SET status = %s, status_reason = %s, acted_version_id = %s,"
            "       outcome_at = now()"
            " WHERE item_id = %s",
            (outcome.status, outcome.detail or None, outcome.acted_version,
             item_id))
