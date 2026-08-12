"""
File:    classification.py

Which broker failures are DEFINITE refusals, and which are ambiguous.

The publisher's state machine has two failure exits and they are not
interchangeable. An ambiguous outcome returns the row to `PENDING` and resends
identical bytes under an identical key — at-least-once, consumers deduplicate.
A definite refusal marks the row `REFUSED`, terminally and operator-visibly,
and never sends it again.

**GETTING THIS WRONG IS COSTLY IN BOTH DIRECTIONS**, which is why the mapping
is explicit and enumerated rather than inferred:

  * A definite refusal treated as ambiguous is an infinite loop against a fixed
    answer. A packet the broker will never accept — because it exceeds the
    topic's maximum message size, or because this principal is not authorized
    to write there — is retried every cycle, forever, at the head of the
    queue, delaying every packet behind it. This was the state of the
    publisher as first written: `DefiniteRefusal` existed and the REFUSED
    branch was correct, but the production sender raised a bare `RuntimeError`
    for everything, so the branch was unreachable outside the tests.
  * An ambiguous outcome treated as definite DROPS A PACKET the broker may
    well have accepted, or may accept on the next attempt. That is silent
    alert loss, which is worse.

**SO THE DEFAULT IS AMBIGUOUS AND THE DEFINITE LIST IS CLOSED.** Anything not
named below — including any exception class this module has never heard of —
resends. The bias is at-least-once by construction, and adding to the definite
list is a deliberate act with a reason attached.

**THE TAXONOMY IS THE INSTALLED CLIENT'S, VERIFIED, NOT RECALLED.** Probed
against kafka-python **3.0.10** on rapid-admin (2026-08-12), which is the
version `requirements.txt`/`pyproject.toml` resolve there. Two classes this
module would otherwise have named DO NOT EXIST in it — `RecordTooLargeError`
and `AuthenticationFailedError` — so referring to them by import would have
been an `ImportError` at publisher startup, and referring to them by name in a
tuple would have silently classified nothing.

**`retriable` IS CONSULTED BUT NOT OBEYED.** kafka-python annotates its error
classes with a `retriable` flag, and it is a good first filter — but it answers
a different question than this module does. It means "can the CLIENT retry this
request", which is about protocol-level recovery within one send; this module
asks "will the BROKER ever accept this packet", which is about the packet. The
probe found two places the two answers diverge, and both are handled
explicitly below rather than by trusting the flag.
"""

import logging

logger = logging.getLogger("rapid.publisher.classification")

#: Broker errors that are DEFINITE refusals: the broker did not take this
#: packet and will not take it on a retry. Named as STRINGS rather than
#: imported classes, deliberately — `kafka.errors` is not importable in the
#: contract tier or in any environment without the client installed, and a
#: publisher module that could not be imported without kafka-python would make
#: the classification untestable exactly where it most needs testing. Matched
#: against the exception's own class name and its bases, below.
#:
#: Each entry, and why terminalizing it is safe:
#:
#:   MessageSizeTooLargeError (errno 10, retriable=False)
#:       The broker's `message.max.bytes` refused this record. The packet's
#:       bytes are write-once in the outbox, so the identical packet will be
#:       refused identically forever. The alert-production stage already
#:       drops oversize packets before they reach the outbox
#:       (`MAX_PACKET_BYTES`); a row that still hits this means the broker's
#:       limit is below the producer's, which is a deployment fact an operator
#:       must see rather than a backlog they must diagnose.
#:
#:   TopicAuthorizationFailedError (errno 29, retriable=False)
#:   ClusterAuthorizationFailedError (errno 31, retriable=False)
#:       This principal may not write to this topic (or to the cluster). A
#:       retry re-presents the same identity to the same ACL. Recorded as
#:       REFUSED so the health view shows an authorization problem rather than
#:       a stalled queue — and the 2026-08-04 Q7 finding is exactly this class
#:       of error being invisible.
#:
#:   InvalidTopicError (errno 17, retriable=False)
#:       The topic NAME is malformed — not "absent", which is different (see
#:       the note on UnknownTopicOrPartitionError below). A name the broker
#:       rejects as invalid is frozen in the row's write-once envelope, so no
#:       later cycle can present a different one.
#:
#:   UnsupportedVersionError (errno 35, retriable=False)
#:       The broker does not support the protocol version this request needs.
#:       Retrying the same request against the same broker cannot change that;
#:       it is a client/broker compatibility fault for an operator.
DEFINITE_REFUSAL_ERRORS = (
    "MessageSizeTooLargeError",
    "TopicAuthorizationFailedError",
    "ClusterAuthorizationFailedError",
    "InvalidTopicError",
    "UnsupportedVersionError",
)

#: Errors that LOOK definite and are deliberately NOT, each with the reason.
#: Kept as a named constant rather than as prose so the exclusions are as
#: greppable as the inclusions — and so a future edit that "tidies" one of
#: these into the definite list has to delete an explanation first.
#:
#:   UnknownTopicOrPartitionError (errno 3, retriable=TRUE)
#:       Reads as "the topic does not exist", which sounds terminal — and
#:       kafka-python marks it retriable with `invalid_metadata=True`, because
#:       it is ROUTINELY TRANSIENT: it is what a client sees against a topic
#:       that is being created, or whose metadata this client has not yet
#:       refreshed, or during a partition reassignment. Terminalizing it would
#:       REFUSE every packet produced in the seconds after a topic is created.
#:       The probe's own answer (retriable=True) is what settled this.
#:
#:   BrokerNotAvailableError (errno 8, retriable=False)
#:       The one place the client's own flag is NOT followed. It is marked
#:       non-retriable at the protocol level, but as a statement about the
#:       WORLD it means "this broker is down right now" — a rolling restart, a
#:       failover, a scaling event. That is the most transient condition there
#:       is, and the outbox exists precisely to absorb it (§2.3: "the outbox
#:       absorbs downtime by design"). Refusing packets because a broker was
#:       briefly unavailable would be alert loss during ordinary operations.
AMBIGUOUS_DESPITE_APPEARANCES = (
    "UnknownTopicOrPartitionError",
    "BrokerNotAvailableError",
)


def is_definite_refusal(error):
    """Is this exception a definite, terminal broker refusal?

    Matched on the exception's class name AND on every name in its method
    resolution order, so a client that raises a more specific subclass of a
    listed error is still classified by the listed one. Matching by NAME
    rather than by `isinstance` keeps this module importable — and therefore
    testable — with no kafka-python present, which is the state of the
    contract tier and of any environment that has not installed the client.

    Returns False for anything unrecognized. That is the at-least-once bias
    stated as code: an error this module has never seen resends, and the
    packet's identical bytes go out again under its identical key.
    """
    if error is None:
        return False

    names = {type(error).__name__}
    names.update(base.__name__ for base in type(error).__mro__)

    # THE EXCLUSIONS ARE CHECKED FIRST, so an error that appears in both — a
    # subclass relationship, say, or a future edit that lists one in each —
    # resolves to ambiguous. The safe direction wins ties by construction
    # rather than by ordering luck.
    if names & set(AMBIGUOUS_DESPITE_APPEARANCES):
        return False
    return bool(names & set(DEFINITE_REFUSAL_ERRORS))


def classify(error):
    """`("definite", reason)` or `("ambiguous", reason)` for one send failure.

    The reason is what lands in `alert_outbox.refusal_reason` for a definite
    refusal, and in the publisher's log for an ambiguous one, so it names the
    class and says what the classification means operationally.
    """
    name = type(error).__name__ if error is not None else "UnknownError"
    if is_definite_refusal(error):
        return "definite", (
            f"{name}: the broker refused this packet and a retry cannot "
            f"change that ({error})")
    return "ambiguous", (
        f"{name}: the outcome is indeterminate, so the packet is resent with "
        f"identical bytes under its identical key ({error})")
