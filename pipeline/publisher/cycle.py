"""
File:    cycle.py

One publisher cycle: claim, check policy, send, finalize.

Separated from `service.py` so the cycle runs in the contract tier against a
real database and a stub broker, with no session, no parameter tree and no
signal handling. `service.py` is the process; this is what the process does.

**STATELESS BETWEEN CYCLES.** Nothing is carried over — not a claim, not a
partial batch, not a retry counter. Everything a resumption needs is in the
outbox row, which is what lets the publisher be killed at any moment and lets
the outbox absorb its downtime by design (§2.3). A cycle that dies mid-flight
leaves rows IN_FLIGHT; the next cycle's recovery pass reclaims them after the
lease.

**THE SEND HAPPENS OUTSIDE EVERY DATABASE TRANSACTION**, between the claim
transaction and the finalization transaction. See `outbox.py` for why (the
publisher is transaction-mode pooled and a broker round trip must not pin a
pooled server connection).

**THE WIRE BYTES COME STRICTLY FROM STORED FIELDS.** `frame_alert(payload,
pinned_schema_version_id)` — no registry lookup on the send path, ever. The
production producer resolves the registry's LATEST schema version at publish
time (`alerts/kafka_producer.py`, `LatestVersion: True`), so framing that way
here would make a resend's bytes differ from the first send's after any
registry bump. The pinned UUID in the row is what makes "identical bytes on
resend" true across a registry change, which is exactly what acceptance 4
tests.

**THE THREE SEND OUTCOMES ARE DISTINCT, AND THE DISTINCTION IS THE POINT.**

  * acknowledged     -> SENT
  * AMBIGUOUS        -> PENDING, resend counter incremented. A timeout, a
                        dropped connection, any error that leaves it unknown
                        whether the broker took the message. Resent with
                        identical bytes and an identical key; consumers
                        deduplicate on `alert_id`.
  * DEFINITE REFUSAL -> REFUSED, terminal, operator-visible. The broker said
                        no in a way that will not change on a retry.

A refusal silently retried as if ambiguous is an infinite loop against a fixed
answer; an ambiguity treated as a refusal drops a packet that may never have
been rejected. Neither is acceptable, so the transport's outcome is classified
explicitly rather than by catching everything.
"""

import logging

from alerts.kafka_producer import frame_alert

logger = logging.getLogger("rapid.publisher.cycle")


class DefiniteRefusal(Exception):
    """The broker refused this message in a way a retry will not change.

    Raised by a transport (or classified from one's error) to mean: stop. The
    message is too large for the topic's configuration, the topic does not
    exist, authorization was denied. Distinct from every other exception, which
    is treated as AMBIGUOUS — the conservative reading, because a packet resent
    unnecessarily is deduplicated by consumers while a packet wrongly marked
    refused is simply lost.
    """


class PublisherCycle:
    """Claim, check, send, finalize — one pass over the outbox."""

    def __init__(self, repository, sender, claim_token, batch=None,
                 topic_guard=None):
        self.repository = repository
        self.sender = sender
        self.claim_token = claim_token
        self.batch = batch
        # The internal-topic guard, MOVED HERE from the alert-production job
        # (brief E2): after E the publisher is the only component that talks to
        # a broker, so it is the only place a guard on the destination can
        # still be enforced. A row whose topic fails the guard is REFUSED
        # rather than held: the topic is in the write-once envelope, so no
        # later cycle could reach a different verdict, and holding it would be
        # a backlog that never drains.
        self.topic_guard = topic_guard

    def run_once(self):
        """One cycle. Returns a dict of what happened, for the log and tests."""
        counts = {"reclaimed": 0, "claimed": 0, "sent": 0, "resend": 0,
                  "refused": 0, "held": 0}

        # RECOVERY FIRST, so a crashed predecessor's rows re-enter the ordinary
        # PENDING flow before this cycle claims — otherwise a busy outbox would
        # starve the orphans behind a continuous stream of newer packets.
        counts["reclaimed"] = self.repository.reclaim_stale()

        claimed = self.repository.claim_batch(self.claim_token,
                                              **({"limit": self.batch}
                                                 if self.batch else {}))
        counts["claimed"] = len(claimed)

        for row in claimed:
            (alert_id, _basis, payload, _checksum, schema_version_id, topic,
             release_identity, _resends, _created_at) = row
            self._dispatch(counts, alert_id, payload, schema_version_id,
                           topic, release_identity)
        return counts

    def _dispatch(self, counts, alert_id, payload, schema_version_id, topic,
                  release_identity):
        """One claimed row: policy, guard, send, finalize."""
        # THE POLICY CHECK, IMMEDIATELY BEFORE THE SEND and on every pass —
        # including a resend. A revocation between an ambiguous first send and
        # its resend must prevent the resend (acceptance 6), which is only true
        # if the check is here rather than at claim time.
        if not self.repository.release_authorized(release_identity):
            self.repository.release_to_pending(alert_id, self.claim_token)
            counts["held"] += 1
            logger.info(
                "packet %s held: release %r is not authorized for delivery "
                "(default-DENY; authorize it in delivery_policies to release "
                "the backlog)", alert_id, release_identity)
            return

        if self.topic_guard is not None and not self.topic_guard(topic):
            self.repository.mark_refused(
                alert_id, self.claim_token,
                f"topic {topic!r} is not an authorized destination")
            counts["refused"] += 1
            logger.error(
                "packet %s REFUSED: topic %r is not an authorized "
                "destination; the packet's topic is write-once, so no later "
                "cycle can reach a different verdict", alert_id, topic)
            return

        # FRAMED STRICTLY FROM STORED FIELDS. No registry lookup: that is what
        # makes this byte-identical to the first send after a registry bump.
        wire_bytes = frame_alert(bytes(payload), schema_version_id)

        try:
            # THE KEY IS THE ALERT ID. Consumers get the deterministic identity
            # on every record, which is what makes at-least-once delivery
            # deduplicable at all.
            metadata = self.sender.send(topic, wire_bytes,
                                        key=alert_id.encode("utf-8"))
        except DefiniteRefusal as exc:
            self.repository.mark_refused(alert_id, self.claim_token, str(exc))
            counts["refused"] += 1
            logger.error("packet %s REFUSED by the broker: %s (terminal; not "
                         "resent)", alert_id, exc)
            return
        except Exception as exc:                            # noqa: BLE001
            # AMBIGUOUS BY DEFAULT — the conservative reading. A packet resent
            # unnecessarily is deduplicated by consumers; a packet wrongly
            # marked refused is lost.
            self.repository.return_for_resend(alert_id, self.claim_token)
            counts["resend"] += 1
            logger.warning(
                "packet %s: acknowledgement ambiguous (%s: %s); returned to "
                "PENDING for an identical-bytes, identical-key resend",
                alert_id, type(exc).__name__, exc)
            return

        # THE FINALIZATION IS CONDITIONAL ON THE TOKEN. Zero rows means this
        # cycle's lease expired and a recovery pass reclaimed the row while the
        # send was in flight: the new claimant owns it now, and overwriting its
        # state would be this cycle asserting an outcome for work it no longer
        # holds. The packet is sent again by that claimant — at-least-once,
        # which is the contract.
        changed = self.repository.mark_sent(alert_id, self.claim_token,
                                            broker_metadata=metadata)
        if changed:
            counts["sent"] += 1
        else:
            logger.warning(
                "packet %s was sent but its claim had already been reclaimed "
                "(lease expired mid-send); the reclaiming cycle will send it "
                "again — at-least-once, and consumers deduplicate on the key",
                alert_id)
