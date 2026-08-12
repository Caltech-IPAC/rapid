"""
File:    outbox.py

The outbox repository: the four short transactions the publisher runs, and
nothing else.

Separated from `service.py` so the PROTOCOL — claim, policy check,
finalization, orphan recovery — is testable against a real database without a
broker, a session, or a running service. Every method here takes an executor
(`execute(sql, params)`, the contract `observability.attempts.Executor`
documents) rather than opening its own connection, which is what lets the
contract tier run these exact statements the service runs.

**EVERY TRANSACTION HERE IS SHORT, AND THE SEND IS OUTSIDE ALL OF THEM.** The
publisher is transaction-mode pooled (minimal viable target §2.2), so a
transaction held across a network send would pin a pooled server connection for
the duration of a broker round trip — and a broker that stopped answering would
hold it indefinitely. The cycle is therefore: claim (transaction), send (no
transaction), finalize (transaction). This is also why the claim must be atomic
rather than a read followed by an update: between the two there is no
transaction to serialize them.

**THE CLAIM IS A CAS, NOT A SELECT-THEN-UPDATE.** `UPDATE ... WHERE state =
'PENDING'` with the row set chosen by a subquery, returning what it actually
claimed. Two overlapping cycles cannot both claim one row because the second
one's `WHERE state = 'PENDING'` no longer matches — the same shape migration
037's emission claim uses, for the same reason. `FOR UPDATE SKIP LOCKED` on the
subquery so a second publisher takes the NEXT rows rather than blocking on the
first's, which is what makes §2.3's "active-active safe even though one
instance runs" true rather than aspirational.

**FINALIZATION IS CONDITIONAL ON THE CLAIM TOKEN.** A cycle that was slow
enough for its lease to expire may find its rows reclaimed by a recovery pass;
its finalization must then affect zero rows rather than overwrite the new
claimant's work. Every finalizing statement carries `AND claim_token = %s`, and
the caller is told how many rows it actually changed.
"""

import logging

logger = logging.getLogger("rapid.publisher.outbox")

#: How long a claim is honoured before an orphan-recovery pass may take it.
#:
#: Restated here as the CODE's copy of the same interval DRAFT 050's
#: `alert_outbox_stale_claims` view carries as a SQL literal — the view is what
#: an operator reads, this is what the reclaim statement enforces, and the two
#: are kept in sync by inspection exactly as `CLAIM_STALENESS` in
#: `pipeline/stages/alert_production.py` is against migration 037's view.
#:
#: Five minutes is a deliberate compromise: long enough that a cycle held up by
#: a slow broker is not reclaimed underneath itself (which would produce a
#: duplicate send that the at-least-once contract permits but nobody wants
#: routinely), short enough that a crashed publisher's packets are not stranded
#: for an operator-visible interval.
CLAIM_LEASE = "interval '5 minutes'"

#: The most rows one cycle claims. A bound rather than a tuning parameter: the
#: cycle holds these in memory as framed wire bytes, and alert packets carry
#: cutouts at roughly 200 KB each.
DEFAULT_BATCH = 20


class OutboxRepository:
    """The outbox's four short transactions, over an injected executor.

    `only_release` restricts every claim to ONE release identity. Production
    passes nothing and drains the whole outbox, which is the point of a single
    publisher; it exists because the CONTRACT TIER shares one long-lived
    database with every other test in the run, and a publisher that correctly
    claims every PENDING row will happily claim the rows another test is in the
    middle of asserting about. Found on this branch's third acceptance run,
    where four publisher tests failed with counts three and four rows too high
    and nothing wrong with the publisher.

    It is a FILTER, never a fallback: it narrows what a cycle considers and
    changes nothing about the claim's atomicity, its ordering, or its
    finalization, so the properties the tests assert are the production ones.
    The alternative — truncating the table between tests — is exactly what this
    tier's fixture-honesty discipline forbids.
    """

    def __init__(self, execute, lease=CLAIM_LEASE, only_release=None):
        self.execute = execute
        self.lease = lease
        self.only_release = only_release

    def _release_filter(self, params):
        """`(sql_fragment, params)` narrowing a statement to one release."""
        if self.only_release is None:
            return "", params
        return " AND release_identity = %s", params + [self.only_release]

    # -- the claim ---------------------------------------------------------

    def claim_batch(self, claim_token, limit=DEFAULT_BATCH):
        """Atomically claim up to `limit` PENDING rows. Returns them.

        THE ORDER IS `(created_at, alert_id)`, the total order DRAFT 050's
        partial index carries. `created_at` alone is NOT a total order: a
        confirmation transaction writes every one of a chip's packets with one
        `now()`, so the rows of one emission share a timestamp to the
        microsecond and `ORDER BY created_at` would leave their relative order
        to the plan. The tie-break makes the sequence reproducible, which is
        what acceptance 4 asserts.

        `SKIP LOCKED` rather than plain `FOR UPDATE`: a concurrent claim should
        take different rows, not wait for these. Waiting would serialize two
        publishers into one throughput and, worse, the waiter would then
        re-evaluate `state = 'PENDING'` and find nothing — a wasted cycle that
        looks like an empty outbox.
        """
        scope, params = self._release_filter([claim_token])
        rows = self.execute(
            "UPDATE alert_outbox SET"
            "   state = 'IN_FLIGHT', claim_token = %s, claimed_at = now()"
            " WHERE outbox_id IN ("
            "   SELECT outbox_id FROM alert_outbox"
            "    WHERE state = 'PENDING'" + scope +
            "    ORDER BY created_at, alert_id"
            "    LIMIT %s FOR UPDATE SKIP LOCKED)"
            " RETURNING alert_id, identity_basis, payload, payload_checksum,"
            "           schema_version_id, topic, release_identity,"
            "           resend_count, created_at",
            params + [int(limit)])
        # RE-SORTED, because RETURNING's order is the UPDATE's execution
        # order and is NOT promised to be the subquery's ORDER BY. The send
        # order is part of the contract (acceptance 4), so it is established
        # here rather than left to the plan.
        #
        # `created_at` is deliberately in the RETURNING list for this sort
        # alone: sorting on `alert_id` by itself would be a different order
        # from the one the claim selected, because the tie-break only applies
        # WITHIN one timestamp.
        return sorted(rows or [], key=lambda row: (row[8], row[0]))

    def reclaim_stale(self, limit=DEFAULT_BATCH):
        """Return expired IN_FLIGHT claims to PENDING. Returns the count.

        ORPHAN RECOVERY, and the whole of it. A crash anywhere between the
        claim and the finalization leaves a row IN_FLIGHT with a token nobody
        holds; after the lease it is reclaimable, and reclaiming means going
        back to PENDING so the next cycle claims it in the ordinary way.

        THE THREE CRASH WINDOWS ARE NOT DISTINGUISHED, deliberately: before the
        send, after the send but before the acknowledgement, and after the
        acknowledgement but before the `SENT` write all land here identically,
        because there is no durable evidence written between the claim and the
        finalization that could tell them apart. All three therefore produce
        one more send of the identical bytes under the identical key. That is
        the at-least-once contract, and trying to narrow it would mean a
        durable write per message on the hot path to buy an exactly-once
        guarantee the target explicitly does not make.

        The resend counter is NOT incremented here. It counts sends, and a
        reclaimed row may never have been sent at all — the pre-send crash
        window. It is incremented by `return_for_resend`, which is reached only
        when a send actually happened and its outcome was ambiguous.
        """
        scope, params = self._release_filter([])
        rows = self.execute(
            "UPDATE alert_outbox SET"
            "   state = 'PENDING', claim_token = NULL, claimed_at = NULL"
            " WHERE outbox_id IN ("
            "   SELECT outbox_id FROM alert_outbox"
            f"   WHERE state = 'IN_FLIGHT' AND claimed_at < now() - {self.lease}"
            + scope +
            "    ORDER BY claimed_at"
            "    LIMIT %s FOR UPDATE SKIP LOCKED)"
            " RETURNING alert_id",
            params + [int(limit)])
        reclaimed = list(rows or [])
        if reclaimed:
            logger.warning(
                "reclaimed %d outbox row(s) whose claim outlived the lease; "
                "each will be resent with identical bytes under its identical "
                "key (at-least-once: the crash windows are indistinguishable)",
                len(reclaimed))
        return len(reclaimed)

    # -- the policy check --------------------------------------------------

    def release_authorized(self, release_identity):
        """Is this release authorized for delivery RIGHT NOW?

        Default-DENY: a release with no policy row is unauthorized, so a new
        release cannot start delivering because nobody wrote a policy.

        Called immediately before EVERY send, including resends. Not at claim
        time and not once per cycle: a revocation between an ambiguous first
        send and its resend must prevent the resend, which is only true if the
        check is on the send path itself.
        """
        rows = self.execute(
            "SELECT authorized FROM delivery_policies"
            " WHERE release_identity = %s", [release_identity])
        if not rows:
            return False
        return bool(rows[0][0])

    def release_to_pending(self, alert_id, claim_token):
        """Un-claim a held row, leaving it PENDING for a later cycle.

        A packet whose release is unauthorized was claimed before the check
        (the claim is a batch operation; the check is per row) and must not
        stay IN_FLIGHT: it is not in flight, nothing was sent, and leaving it
        claimed would make it wait out the lease before anything could look at
        it again. The counter is untouched — nothing was sent.
        """
        return self._rowcount(self.execute(
            "UPDATE alert_outbox SET"
            "   state = 'PENDING', claim_token = NULL, claimed_at = NULL"
            " WHERE alert_id = %s AND claim_token = %s AND state = 'IN_FLIGHT'",
            [alert_id, claim_token]))

    # -- finalization ------------------------------------------------------

    def mark_sent(self, alert_id, claim_token, broker_metadata=None):
        """`SENT`, with the acknowledgement's metadata. Conditional on the token."""
        import json

        return self._rowcount(self.execute(
            "UPDATE alert_outbox SET"
            "   state = 'SENT', sent_at = now(), claim_token = NULL,"
            "   claimed_at = NULL, broker_metadata = %s::jsonb"
            " WHERE alert_id = %s AND claim_token = %s AND state = 'IN_FLIGHT'",
            [json.dumps(broker_metadata) if broker_metadata is not None
             else None, alert_id, claim_token]))

    def return_for_resend(self, alert_id, claim_token):
        """Back to `PENDING`, resend counter incremented — the AMBIGUOUS ack.

        An ambiguous acknowledgement is one where the publisher cannot tell
        whether the broker took the message: a timeout, a dropped connection, a
        transport error that is not a refusal. The packet may or may not have
        been delivered, so it is sent again — identical bytes, identical key —
        and consumers deduplicate on `alert_id`. That is the at-least-once
        contract's accepted cost.

        DISTINCT FROM A REFUSAL, which is terminal. Conflating the two is the
        failure this method's existence prevents in both directions: retrying a
        definite refusal forever, or dropping a packet the broker never
        actually rejected.
        """
        return self._rowcount(self.execute(
            "UPDATE alert_outbox SET"
            "   state = 'PENDING', claim_token = NULL, claimed_at = NULL,"
            "   resend_count = resend_count + 1"
            " WHERE alert_id = %s AND claim_token = %s AND state = 'IN_FLIGHT'",
            [alert_id, claim_token]))

    def mark_refused(self, alert_id, claim_token, reason):
        """`REFUSED`, terminal and operator-visible — the DEFINITE refusal.

        The broker said no in a way that will not change by trying again: the
        message is too large for the topic's configuration, the topic does not
        exist, authorization was denied. Retrying would be an infinite loop
        against a fixed answer, so the row stops here carrying the reason, and
        the health view shows it as a state rather than as a backlog.
        """
        return self._rowcount(self.execute(
            "UPDATE alert_outbox SET"
            "   state = 'REFUSED', claim_token = NULL, claimed_at = NULL,"
            "   refusal_reason = %s"
            " WHERE alert_id = %s AND claim_token = %s AND state = 'IN_FLIGHT'",
            [str(reason)[:500], alert_id, claim_token]))

    @staticmethod
    def _rowcount(result):
        """The executor returns rows for a result set, rowcount otherwise."""
        if isinstance(result, int):
            return result
        return len(result or [])
