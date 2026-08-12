"""
File:    test_publisher_classification.py

The PRODUCTION sender's error classification: which broker failures terminalize
a packet and which resend it.

**THIS DRIVES THE REAL SENDER, NOT A RE-IMPLEMENTATION.** The tests construct
`pipeline.publisher.service.ProducerSender` — the object `main()` builds — and
hand it a transport that raises transport-shaped exceptions. That matters
because the defect this file exists to prevent was precisely a gap between the
cycle's correct REFUSED branch and a production sender that could never reach
it: `DefiniteRefusal` was raised nowhere outside the tests, so a message the
broker would never accept retried forever at the head of the queue. A test that
exercised `classify()` alone would have passed against exactly that publisher.

**THE STUB EXCEPTIONS ARE NAME-SHAPED, NOT IMPORTED.** kafka-python is not
importable in every environment this tier runs in, and
`pipeline/publisher/classification.py` therefore matches on class NAME rather
than by `isinstance` — a deliberate design decision, restated in that module.
So the doubles here are classes carrying the real names, which is what the
production classifier actually sees. Their names are the contract.

**THE NAMES WERE VERIFIED, NOT RECALLED.** Probed against the installed client
on rapid-admin (kafka-python 3.0.10, 2026-08-12). Two names an earlier draft of
the classifier would have used — `RecordTooLargeError` and
`AuthenticationFailedError` — DO NOT EXIST in that version, and
`UnknownTopicOrPartitionError` is marked `retriable=True` by the client itself,
which is why it is on the ambiguous side despite reading like "the topic does
not exist". `test_the_classified_names_exist_in_the_installed_client` below
pins that check to the suite rather than leaving it as a claim in a comment.
"""

import unittest

import pytest

from pipeline.publisher.classification import (AMBIGUOUS_DESPITE_APPEARANCES,
                                               DEFINITE_REFUSAL_ERRORS,
                                               classify, is_definite_refusal)
from pipeline.publisher.cycle import DefiniteRefusal
from pipeline.publisher.service import ProducerSender


def _error_class(name, base=Exception):
    """An exception class carrying a real kafka-python error's NAME.

    The classifier matches on names, so this is not an approximation of the
    real error — for the purpose under test it is exactly equivalent to it,
    and it keeps this suite runnable with no broker client installed.
    """
    return type(name, (base,), {})


class _SynchronousTransport:
    """A transport whose `send` raises immediately, before any future exists.

    THE SYNCHRONOUS PATH IS REAL AND IS EASY TO MISS: kafka-python raises
    `MessageSizeTooLargeError` from `send()` itself when a record exceeds the
    configured `max_request_size`, without ever contacting a broker. A sender
    that only classified future outcomes would let the single most likely
    definite refusal through as an ambiguous `RuntimeError`.
    """

    def __init__(self, error):
        self.error = error
        self.sent = []

    def send(self, topic, value, key=None):
        self.sent.append((topic, value, key))
        raise self.error

    def flush(self):
        pass


class _FailedFuture:
    """A future that resolved to a broker error, as kafka-python's does."""

    def __init__(self, error):
        self.exception = error

    def succeeded(self):
        return False


class _AsynchronousTransport:
    """A transport that accepts the send and fails it on the future."""

    def __init__(self, error):
        self.error = error
        self.sent = []

    def send(self, topic, value, key=None):
        self.sent.append((topic, value, key))
        return _FailedFuture(self.error)

    def flush(self):
        pass


class _Producer:
    """The `GlueFramingProducer` surface `ProducerSender` reaches through."""

    def __init__(self, transport):
        self.transport = transport


def _sender(transport):
    return ProducerSender(_Producer(transport))


# ---------------------------------------------------------------------------
# The classifier itself
# ---------------------------------------------------------------------------

class ClassifierTests(unittest.TestCase):
    """`is_definite_refusal` over the enumerated taxonomy."""

    def test_every_enumerated_definite_error_classifies_as_definite(self):
        for name in DEFINITE_REFUSAL_ERRORS:
            with self.subTest(name=name):
                self.assertTrue(is_definite_refusal(_error_class(name)()))

    def test_every_documented_exclusion_classifies_as_ambiguous(self):
        # The exclusions are the interesting half: each is an error that READS
        # terminal and is deliberately not, with its reason recorded in the
        # classifier. Terminalizing any of them would drop packets during
        # ordinary operations — a topic being created, a broker restarting.
        for name in AMBIGUOUS_DESPITE_APPEARANCES:
            with self.subTest(name=name):
                self.assertFalse(is_definite_refusal(_error_class(name)()))

    def test_an_unknown_error_is_ambiguous(self):
        # THE DEFAULT, and the at-least-once bias stated as a test. An error
        # this module has never seen resends; it does not drop a packet on the
        # strength of not recognizing something.
        self.assertFalse(is_definite_refusal(_error_class("SomeNewError")()))
        self.assertFalse(is_definite_refusal(RuntimeError("boom")))
        self.assertFalse(is_definite_refusal(TimeoutError("no ack")))

    def test_a_subclass_of_a_definite_error_is_definite(self):
        # A client raising a more specific subclass must not escape the
        # classification: the MRO is searched, not just the leaf name.
        base = _error_class("MessageSizeTooLargeError")
        self.assertTrue(is_definite_refusal(_error_class("Narrower", base)()))

    def test_none_is_ambiguous(self):
        # A future that reports failure without an exception object still has
        # to resolve to something, and the something is a resend.
        self.assertFalse(is_definite_refusal(None))

    def test_the_reason_names_the_class_and_the_consequence(self):
        verdict, reason = classify(_error_class("InvalidTopicError")())
        self.assertEqual(verdict, "definite")
        self.assertIn("InvalidTopicError", reason)
        self.assertIn("retry", reason)

        verdict, reason = classify(TimeoutError("no acknowledgement"))
        self.assertEqual(verdict, "ambiguous")
        self.assertIn("identical", reason)


# ---------------------------------------------------------------------------
# The production sender, driven for real
# ---------------------------------------------------------------------------

class ProductionSenderTests(unittest.TestCase):
    """`ProducerSender` raises what the cycle's two branches read."""

    def test_a_synchronous_definite_error_raises_definite_refusal(self):
        sender = _sender(_SynchronousTransport(
            _error_class("MessageSizeTooLargeError")("record too large")))

        with self.assertRaises(DefiniteRefusal):
            sender.send("rapid.internal.alerts.v1", b"wire", key=b"k")

    def test_an_asynchronous_definite_error_raises_definite_refusal(self):
        sender = _sender(_AsynchronousTransport(
            _error_class("TopicAuthorizationFailedError")("not authorized")))

        with self.assertRaises(DefiniteRefusal):
            sender.send("rapid.internal.alerts.v1", b"wire", key=b"k")

    def test_a_synchronous_ambiguous_error_does_not_raise_definite_refusal(self):
        # It still RAISES — the send failed — but as something the cycle reads
        # as ambiguous. Asserted as "not DefiniteRefusal" rather than as a
        # specific type, because the cycle's own rule is "anything that is not
        # a DefiniteRefusal".
        sender = _sender(_SynchronousTransport(TimeoutError("no ack")))

        with self.assertRaises(Exception) as caught:
            sender.send("rapid.internal.alerts.v1", b"wire", key=b"k")
        self.assertNotIsInstance(caught.exception, DefiniteRefusal)

    def test_an_asynchronous_ambiguous_error_does_not_raise_definite_refusal(self):
        sender = _sender(_AsynchronousTransport(
            _error_class("UnknownTopicOrPartitionError")("no such topic yet")))

        with self.assertRaises(Exception) as caught:
            sender.send("rapid.internal.alerts.v1", b"wire", key=b"k")
        self.assertNotIsInstance(caught.exception, DefiniteRefusal)

    def test_a_broker_briefly_unavailable_is_ambiguous(self):
        # The one place the client's own `retriable=False` flag is deliberately
        # NOT followed: BrokerNotAvailableError means "this broker is down
        # right now", which is a rolling restart or a failover — the most
        # transient condition there is, and the one the outbox exists to
        # absorb. Terminalizing it would be alert loss during ordinary ops.
        sender = _sender(_AsynchronousTransport(
            _error_class("BrokerNotAvailableError")("restarting")))

        with self.assertRaises(Exception) as caught:
            sender.send("rapid.internal.alerts.v1", b"wire", key=b"k")
        self.assertNotIsInstance(caught.exception, DefiniteRefusal)

    def test_the_key_and_bytes_reach_the_transport_before_any_failure(self):
        # The classification must not change WHAT was attempted. A sender that
        # classified correctly but sent the wrong bytes, or dropped the key,
        # would break deduplication while looking healthy.
        transport = _SynchronousTransport(TimeoutError("no ack"))
        sender = _sender(transport)

        with self.assertRaises(Exception):
            sender.send("rapid.internal.alerts.v1", b"wire-bytes", key=b"kk")

        self.assertEqual(transport.sent,
                         [("rapid.internal.alerts.v1", b"wire-bytes", b"kk")])


class InstalledClientTests(unittest.TestCase):
    """The enumerated names are real, checked against the installed client.

    A classifier that matches on names is only as good as the names, and a
    typo would silently classify nothing — the failure mode being that every
    definite refusal quietly becomes an infinite resend, which is the exact
    defect this whole fix addresses. So where kafka-python IS importable, the
    names are checked against it; where it is not, this skips rather than
    failing, because its absence is not a defect in this code.
    """

    def test_the_classified_names_exist_in_the_installed_client(self):
        errors = pytest.importorskip(
            "kafka.errors",
            reason="kafka-python is not installed in this environment; the "
                   "classifier matches on names precisely so it does not need "
                   "the client to be importable")

        missing = [name for name in DEFINITE_REFUSAL_ERRORS
                   if not hasattr(errors, name)]
        self.assertEqual(
            missing, [],
            f"these names are classified as definite refusals but do not "
            f"exist in the installed kafka-python: {missing}. A name that "
            f"does not exist matches nothing, so every error it was meant to "
            f"terminalize would resend forever instead")

        missing = [name for name in AMBIGUOUS_DESPITE_APPEARANCES
                   if not hasattr(errors, name)]
        self.assertEqual(
            missing, [],
            f"these names are documented exclusions but do not exist in the "
            f"installed kafka-python: {missing}")

    def test_the_definite_errors_are_the_clients_non_retriable_ones(self):
        """Cross-check the classification against the client's own flag.

        NOT an equality assertion, deliberately: the two answer different
        questions (see the classifier's module docstring), and the exclusions
        exist precisely where they diverge. What IS asserted is the direction
        that would be a defect — a class the client marks RETRIABLE must not be
        on the definite list, because the client is telling us a retry can
        succeed.
        """
        errors = pytest.importorskip("kafka.errors")

        wrong = []
        for name in DEFINITE_REFUSAL_ERRORS:
            cls = getattr(errors, name, None)
            if cls is not None and getattr(cls, "retriable", False):
                wrong.append(name)
        self.assertEqual(
            wrong, [],
            f"these are classified as definite refusals but the installed "
            f"client marks them retriable: {wrong}. Terminalizing a retriable "
            f"error drops packets the broker would have accepted")


if __name__ == "__main__":
    unittest.main()
