"""The publisher: the only component that talks to the broker.

Rule 14 splits alert delivery in two. The alert-production job's obligation
ends when a packet is committed to `alert_outbox` in the same transaction as
the database effect that produced it; THIS process's obligation begins there
and ends at a broker acknowledgement. Nothing else in the tree constructs a
producer or sends after brief E — a contract test asserts exactly that.

What the split buys, stated as the three things that were true before it and
are not now:

  * A broker outage failed science attempts. Delivery happened inside the
    Batch job's lifetime, so an unreachable broker raised through
    `GlueFramingProducer.flush` and the attempt died — for a reason that had
    nothing to do with the science it had already completed. Now the packets
    sit in the outbox and the job closes successfully; the outbox absorbs the
    downtime by design (§2.3).

  * A resend was whatever the next attempt happened to serialize. "Identical
    bytes on resend" could not be true when the bytes were rebuilt from
    scratch by a different process against a registry that may have moved.
    Now the bytes and their pinned schema version are stored, and the
    publisher frames strictly from them.

  * There was no representable state between "produced" and "delivered".
    A packet was either sent or it had never existed, so nothing could say how
    many were waiting, how long they had waited, or which had been refused.
    The two clocks §2.8 names — acceptance→outbox and outbox→acknowledgement —
    had nothing to read.

The three modules, smallest surface first:

  `outbox.py`  the four short transactions: claim, policy read, finalization,
               orphan recovery. Takes an executor; no broker, no session.
  `cycle.py`   one pass: claim, check policy, send outside every transaction,
               finalize conditionally on the claim token.
  `service.py` the process: the parameter tree, the credential, the preflight,
               the signal handler and the loop.

The layering is what makes the protocol testable against a real database and a
stub broker without a service, which is what the contract tier does.
"""
