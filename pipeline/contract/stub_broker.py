"""
File:    stub_broker.py

Broker doubles for the publisher's contract tests — doubles that can REFUSE.

**A DOUBLE THAT CANNOT FAIL PROVES NOTHING** (brief E, and the tier's own
standing discipline in `pipeline/contract/fixture.py`: "doubles must be able to
refuse"). The publisher's whole contract is about what happens when a send goes
wrong, so a stub that only ever succeeds would let every one of these tests
pass against a publisher with no error handling at all. Each stub here models
one thing that really happens at a broker:

  `RecordingBroker`   accepts, and records exactly what went on the wire —
                      topic, key and bytes — so the ORDER, the KEY and the
                      BYTE-IDENTITY assertions have something to read.
  `AmbiguousBroker`   the send does not complete and its outcome is UNKNOWN: a
                      timeout, a dropped connection. The packet may or may not
                      have been delivered. This is the case the at-least-once
                      contract exists for.
  `RefusingBroker`    a DEFINITE refusal: the broker said no in a way a retry
                      will not change. Terminal.
  `FlakyBroker`       ambiguous once, then accepting — the resend path end to
                      end, which is where "identical bytes on resend" is
                      actually observable.

**WHY A MODULE AND NOT INLINE CLASSES.** Two contract test files need these,
and a double copied into each would drift — and a drifting double is worse than
a shared one, because the two copies would silently disagree about what a
refusal is.

**THE AMBIGUOUS/REFUSED DISTINCTION IS MODELLED, NOT INFERRED.** Real
kafka-python does not cleanly separate "the broker rejected this" from "the
send did not complete", which is exactly why the publisher treats an unknown
error as ambiguous and requires an explicit `DefiniteRefusal` to mark a row
REFUSED. These stubs raise the two cases distinguishably so the test can assert
the publisher's classification rather than assume it.
"""

from pipeline.publisher.cycle import DefiniteRefusal


class RecordingBroker:
    """Accepts every send and records the wire form.

    `sent` is a list of `(topic, wire_bytes, key)` in the order the publisher
    sent them, which is what the ordering, keying and byte-identity assertions
    read. The metadata returned is the shape a real transport's record metadata
    has, so the row's `broker_metadata` column is exercised with something
    realistic rather than None.
    """

    def __init__(self):
        self.sent = []

    def send(self, topic, wire_bytes, key=None):
        self.sent.append((topic, wire_bytes, key))
        return {"topic": topic, "partition": 0, "offset": len(self.sent) - 1}

    @property
    def keys(self):
        return [key for _topic, _bytes, key in self.sent]

    @property
    def payloads(self):
        return [wire for _topic, wire, _key in self.sent]


class AmbiguousBroker:
    """Every send fails with an UNKNOWN outcome — the at-least-once case.

    Raises a plain exception rather than `DefiniteRefusal`, which is precisely
    the point: the publisher classifies anything that is not an explicit
    refusal as ambiguous, resends, and lets consumers deduplicate. A stub that
    raised `DefiniteRefusal` here would be testing the other branch.

    Attempts are still RECORDED. A timeout does not mean nothing was sent — the
    broker may well have taken the message and lost the acknowledgement — and
    the tests need to see how many times the publisher tried.
    """

    def __init__(self, error=None):
        self.sent = []
        self.error = error or TimeoutError(
            "no acknowledgement within the request timeout; the broker may or "
            "may not have taken this message")

    def send(self, topic, wire_bytes, key=None):
        self.sent.append((topic, wire_bytes, key))
        raise self.error


class RefusingBroker:
    """Every send is DEFINITELY refused — terminal, never retried.

    `DefiniteRefusal` is the publisher's explicit signal for "this will not
    succeed on a retry": the message exceeds the topic's configured maximum,
    the topic does not exist, authorization was denied.
    """

    def __init__(self, reason="message exceeds the topic's maximum size"):
        self.sent = []
        self.reason = reason

    def send(self, topic, wire_bytes, key=None):
        self.sent.append((topic, wire_bytes, key))
        raise DefiniteRefusal(self.reason)


class FlakyBroker:
    """Ambiguous for the first `failures` sends, then accepting.

    THE RESEND PATH END TO END, and the only stub that can demonstrate
    byte-identity across a resend: the first attempt's bytes and the second
    attempt's bytes are both in `sent`, so the test compares them directly
    rather than trusting that the publisher meant to reproduce them.
    """

    def __init__(self, failures=1):
        self.sent = []
        self.remaining_failures = failures

    def send(self, topic, wire_bytes, key=None):
        self.sent.append((topic, wire_bytes, key))
        if self.remaining_failures > 0:
            self.remaining_failures -= 1
            raise TimeoutError(
                "no acknowledgement within the request timeout (attempt "
                f"{len(self.sent)})")
        return {"topic": topic, "partition": 0, "offset": len(self.sent) - 1}


class CrashingBroker:
    """Sends normally, then raises a BaseException to model a process kill.

    Used for the crash-window tests. `KeyboardInterrupt` derives from
    `BaseException`, not `Exception`, so it passes straight through the
    publisher's `except Exception` ambiguity handler and unwinds the cycle
    without finalizing — which is exactly what a `SIGKILL` looks like from the
    database's point of view: a claim with no finalization and no explanation.

    `crash_after_send` chooses the window: False kills BEFORE the bytes leave
    (nothing was sent), True kills AFTER (the broker has them, and may or may
    not have acknowledged). The tests assert the SAME recovery for both,
    because the database cannot tell them apart — that indistinguishability is
    the contract, not an accident.
    """

    def __init__(self, crash_after_send=True):
        self.sent = []
        self.crash_after_send = crash_after_send

    def send(self, topic, wire_bytes, key=None):
        if not self.crash_after_send:
            raise KeyboardInterrupt("killed before the send")
        self.sent.append((topic, wire_bytes, key))
        raise KeyboardInterrupt("killed after the send, before finalization")
