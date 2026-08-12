"""
File:    kafka_producer.py

The Kafka producer the publication path uses: kafka-python, Glue
schema-registry framing, MSK IAM auth.

Two decisions shape this module.

The client is kafka-python (decisions.md § Pipeline Kafka client): it is
the client `aws-glue-schema-registry` documents an adapter for, and the
one `environment-rapid.yml` ships. The confluent_kafka producer the
alerts CLI used to construct is retired with it.

The wire format is Glue-framed (decisions.md § Publication dispatch
contract): one registry (`roman-rapid-alerts`), schema name = topic name,
auto-registration off. Framing prefixes the schemaless Avro bytes
produce.py already emits with the Glue header, so serialization stays
where it is and this module only frames and publishes.

The seam
--------
produce.py's publish path takes `producer` as an injected object and only
ever calls ``produce(topic, value, callback=...)`` and ``flush()``. That
is the confluent surface, and it is the seam the decision register refers
to. Rather than rewrite every call site, `GlueFramingProducer` presents
that same two-method surface over kafka-python's ``send()``/``flush()``,
so the injected object changes and the pipeline code does not.

Everything the module needs from the outside — the broker list, the
registry name, the schema version — is injected too, so the unit tests
run the full framing path against a fake transport with no broker, no
registry, and no AWS credentials::

    producer = GlueFramingProducer(transport=FakeTransport(),
                                   schema_version="00.01",
                                   registry="roman-rapid-alerts")
    publish_alert(alert_bytes, producer, topic="rapid.test.alerts")
"""

import logging
import struct
import uuid
from pathlib import Path
from typing import Any, Callable, Protocol

logger = logging.getLogger(__name__)

# Registry of record, per the publication dispatch contract. Schema name
# equals topic name, so the registry is the only registry-side name this
# module carries.
DEFAULT_REGISTRY = "roman-rapid-alerts"

# Glue's wire header: one version byte, one compression byte, then the
# 16-byte schema-version UUID the registry assigned. AWS's own encoder
# writes exactly this; we frame it here so the producer stays a thin
# wrapper rather than pulling the whole AWS serializer into the hot path.
GLUE_HEADER_VERSION_BYTE = 0x03
GLUE_COMPRESSION_NONE = 0x00
GLUE_COMPRESSION_ZLIB = 0x05
GLUE_HEADER_STRUCT = struct.Struct(">BB16s")
GLUE_HEADER_LEN = GLUE_HEADER_STRUCT.size

# Schema tree, shared with produce.py. Resolved through latest.txt so the
# producer frames against the CURRENT schema version: a version bump is a
# file change, never a code change here.
SCHEMA_ROOT = Path(__file__).resolve().parent / "schema"


def current_schema_version(schema_root: str | Path = SCHEMA_ROOT) -> str:
    """Return the schema version the producer frames against.

    Reads ``latest.txt`` — the schema tree's own statement of what is
    current. Deliberately not a constant in this module: the alert schema
    and the producer version independently, and a hardcoded version here
    would publish stale framing after a schema bump.

    Parameters
    ----------
    schema_root : str or pathlib.Path, optional
        Directory holding ``<major>/<minor>/`` and ``latest.txt``.

    Returns
    -------
    str
        Schema version, e.g. ``"00.01"``.

    Raises
    ------
    FileNotFoundError
        If ``latest.txt`` is missing: framing against a guessed version
        is worse than not publishing.
    """
    latest = Path(schema_root) / "latest.txt"
    if not latest.exists():
        raise FileNotFoundError(
            f"{latest} is missing; the producer cannot determine which "
            "schema version to frame against")
    return latest.read_text().strip()


def schema_name_for_topic(topic: str) -> str:
    """Return the Glue schema name for a topic.

    Schema name equals topic name (decisions.md § Publication dispatch
    contract). A function rather than an inline expression so the one
    place the rule lives is greppable when the public topic scheme lands.
    """
    return topic


def frame_alert(alert_bytes: bytes, schema_version_id: str | uuid.UUID) -> bytes:
    """Prefix serialized alert bytes with the Glue wire header.

    Parameters
    ----------
    alert_bytes : bytes
        Schemaless Avro from ``produce.serialize_alert()``.
    schema_version_id : str or uuid.UUID
        The registry's schema-version UUID for this topic's schema.

    Returns
    -------
    bytes
        Header + payload, the bytes that go on the wire.
    """
    if isinstance(schema_version_id, str):
        schema_version_id = uuid.UUID(schema_version_id)
    header = GLUE_HEADER_STRUCT.pack(GLUE_HEADER_VERSION_BYTE,
                                     GLUE_COMPRESSION_NONE,
                                     schema_version_id.bytes)
    return header + alert_bytes


def unframe_alert(wire_bytes: bytes) -> tuple[uuid.UUID, bytes]:
    """Split Glue-framed bytes back into schema-version UUID and payload.

    The readback side of `frame_alert`, used by the tests and by anything
    verifying published bytes against an archive copy.

    Raises
    ------
    ValueError
        If the header is absent or carries an unknown version byte —
        an unframed or foreign message, not a RAPID alert.
    """
    if len(wire_bytes) < GLUE_HEADER_LEN:
        raise ValueError(
            f"message too short to carry a Glue header "
            f"({len(wire_bytes)} < {GLUE_HEADER_LEN} bytes)")
    version_byte, _compression, raw_uuid = GLUE_HEADER_STRUCT.unpack(
        wire_bytes[:GLUE_HEADER_LEN])
    if version_byte != GLUE_HEADER_VERSION_BYTE:
        raise ValueError(
            f"unexpected Glue header version byte 0x{version_byte:02x} "
            f"(expected 0x{GLUE_HEADER_VERSION_BYTE:02x})")
    return uuid.UUID(bytes=raw_uuid), wire_bytes[GLUE_HEADER_LEN:]


class Transport(Protocol):
    """The kafka-python surface this module actually uses.

    Narrow by design: it is the whole contract a test fake has to satisfy,
    and it keeps `GlueFramingProducer` honest about what it depends on.

    `key` was added by brief E. The publisher sets every message's key to its
    `alert_id`, because at-least-once delivery is only deduplicable if each
    copy of a packet arrives under the same key — and this Protocol is where a
    test fake learns it has to accept one. It is OPTIONAL so that the
    pre-existing value-only call sites, and the fakes written against them,
    keep working unchanged: kafka-python's own `send` has always taken
    `key=None` by default, so this widens the declared contract to match what
    the real transport already does rather than asking anything to change.
    """

    def send(self, topic: str, value: bytes, key: bytes | None = None) -> Any:
        ...

    def flush(self) -> None:
        ...

    def close(self) -> None:
        ...


class SchemaRegistry(Protocol):
    """Resolves a schema name to its registered schema-version UUID."""

    def schema_version_id(self, schema_name: str) -> str:
        ...


class GlueSchemaRegistry:
    """Glue-backed schema-version lookup, with a per-process cache.

    Registration is deliberately absent: the publication dispatch
    contract requires auto-registration OFF, so an unregistered schema
    is an error to surface, never a schema to create. Lookups are cached
    because a schema version is immutable — a breaking change takes a new
    topic, and therefore a new schema name and a new lookup.
    """

    def __init__(self, registry: str = DEFAULT_REGISTRY,
                 client: Any = None, region: str | None = None):
        self.registry = registry
        self.region = region
        self._client = client
        self._cache: dict[str, str] = {}

    @property
    def client(self) -> Any:
        """The boto3 Glue client, created on first use.

        Lazy so that constructing a producer — which the tests do — never
        requires AWS credentials or a network round trip.
        """
        if self._client is None:
            import boto3
            kwargs = {"region_name": self.region} if self.region else {}
            self._client = boto3.client("glue", **kwargs)
        return self._client

    def schema_version_id(self, schema_name: str) -> str:
        """Return the registry's current schema-version UUID for a schema.

        Raises
        ------
        KeyError
            If the schema is not registered. Auto-registration is off by
            contract, so this is a deployment fault (the schema was never
            published to the registry), not something to paper over.
        """
        if schema_name in self._cache:
            return self._cache[schema_name]

        try:
            response = self.client.get_schema_version(
                SchemaId={"RegistryName": self.registry,
                          "SchemaName": schema_name},
                SchemaVersionNumber={"LatestVersion": True})
        except Exception as exc:                      # noqa: BLE001
            # EntityNotFoundException is the expected shape, but the
            # botocore error classes are only importable with a live
            # client; match on the name so the tests can fake it.
            if type(exc).__name__ == "EntityNotFoundException":
                raise KeyError(
                    f"schema {schema_name!r} is not registered in Glue "
                    f"registry {self.registry!r}; auto-registration is off "
                    "by contract, so register it before publishing") from exc
            raise

        version_id = response["SchemaVersionId"]
        self._cache[schema_name] = version_id
        return version_id


def _default_region() -> str:
    """The region the MSK IAM signer signs for.

    AWS_REGION is set in the Batch container environment; the boto3
    session is the fallback for anything run outside it.
    """
    import os
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if region:
        return region
    import boto3
    session_region = boto3.session.Session().region_name
    if not session_region:
        raise RuntimeError(
            "no AWS region for the MSK IAM token provider; set AWS_REGION")
    return session_region


def make_transport(bootstrap_servers: str, region: str | None = None,
                   max_request_size: int = 15728640, **kwargs: Any) -> Any:
    """Build the kafka-python producer that talks to MSK under IAM auth.

    Imported lazily so that this module — and the tests that exercise its
    framing — load without kafka-python or the IAM auth plugin present.

    Parameters
    ----------
    bootstrap_servers : str
        Comma-separated MSK IAM bootstrap brokers (port 9098).
    region : str, optional
        AWS region for the IAM token provider; defaults to the session's.
    max_request_size : int, optional
        Alerts carry cutouts, so the default 1 MB request cap is far too
        small; this matches the 15 MiB the confluent path used to set.

    Returns
    -------
    kafka.KafkaProducer
        Configured for SASL_SSL / AWS_MSK_IAM.
    """
    from kafka import KafkaProducer
    from aws_msk_iam_sasl_signer import MSKAuthTokenProvider
    # The provider MUST subclass AbstractTokenProvider. kafka-python
    # type-checks the object it is handed: a duck-typed class with a
    # token() method is silently ignored, the OAUTHBEARER handshake never
    # completes, and the client fails with KafkaTimeoutError "Unable to
    # bootstrap" — an error that names the brokers and says nothing about
    # authentication, so it reads as a network problem.
    #
    # Found live 2026-08-04 by the Q7 publication readback: this module's
    # provider was a bare class, so every produce attempt against MSK
    # timed out at bootstrap while the schema lookup beside it succeeded.
    # The class moved in kafka-python 3.x (kafka.sasl.oauth ->
    # kafka.net.sasl.oauth), so both paths are tried — the same shape
    # cloudformation/msk_alert_test.py and t2_glue_roundtrip.py in the
    # infrastructure repo have always used, and whose comments recorded
    # this exact trap.
    try:
        from kafka.net.sasl.oauth import AbstractTokenProvider   # 3.x
    except ImportError:                                  # pragma: no cover
        from kafka.sasl.oauth import AbstractTokenProvider       # older

    resolved_region = region or _default_region()

    class _TokenProvider(AbstractTokenProvider):
        """kafka-python's OAuth token hook, backed by the MSK signer.

        kafka-python calls ``token()`` on every re-authentication, so the
        signer is invoked per token rather than cached: MSK IAM tokens
        expire, and a cached one fails the next handshake.
        """

        def token(self) -> str:
            token, _expiry_ms = MSKAuthTokenProvider.generate_auth_token(
                resolved_region)
            return token

    return KafkaProducer(
        bootstrap_servers=bootstrap_servers.split(","),
        security_protocol="SASL_SSL",
        sasl_mechanism="OAUTHBEARER",
        sasl_oauth_token_provider=_TokenProvider(),
        max_request_size=max_request_size,
        **kwargs)


class GlueFramingProducer:
    """Glue-framing Kafka producer with the injected-producer surface.

    Presents ``produce(topic, value, callback=...)`` and ``flush()`` —
    what produce.py's publish path calls — over kafka-python's
    ``send()``/``flush()``, framing each payload for the Glue registry on
    the way through.

    Both collaborators are injected. In production they are a kafka-python
    producer and a `GlueSchemaRegistry`; in the tests they are fakes, which
    is what lets the whole framing and dispatch path run with no broker.

    Parameters
    ----------
    transport : Transport
        Object with ``send(topic, value)``, ``flush()``, ``close()``.
    registry : SchemaRegistry, optional
        Schema-version resolver. Defaults to a `GlueSchemaRegistry` on
        `DEFAULT_REGISTRY` (constructed lazily; no AWS call until publish).
    schema_version : str, optional
        The alert schema version being produced, recorded for the caller's
        benefit. Defaults to `current_schema_version()`.
    """

    def __init__(self, transport: Transport,
                 registry: SchemaRegistry | None = None,
                 schema_version: str | None = None):
        self.transport = transport
        self.registry = registry if registry is not None else GlueSchemaRegistry()
        self.schema_version = (schema_version if schema_version is not None
                               else current_schema_version())
        self._pending: list[Any] = []

    def produce(self, topic: str, value: bytes,
                callback: Callable[[Any, Any], None] | None = None,
                key: bytes | None = None) -> None:
        """Frame and send one alert.

        Signature matches the injected-producer surface produce.py calls,
        `callback` included: kafka-python has no per-message callback
        argument, so it is attached to the returned future instead, which
        preserves the caller's error-reporting contract.

        `key` (brief E) is passed straight through to the transport. NOTE that
        the publisher does NOT come through this method: it must frame from the
        outbox row's PINNED schema version, and this method resolves the
        registry's latest instead — which is the whole difference "identical
        bytes on resend" turns on. The parameter is here so the surface is
        honest and so an archive or replay tool can key its messages the same
        way, not because the publisher uses it.
        """
        schema_name = schema_name_for_topic(topic)
        version_id = self.registry.schema_version_id(schema_name)
        wire_bytes = frame_alert(value, version_id)

        future = self.transport.send(topic, wire_bytes, key=key)
        self._pending.append(future)

        if callback is not None and hasattr(future, "add_callback"):
            future.add_callback(lambda meta: callback(None, meta))
            future.add_errback(lambda exc: callback(exc, None))

    def flush(self) -> None:
        """Block until every produced message has resolved, and RAISE if
        any of them failed.

        The raise is the point. ``transport.flush()`` waits for the send
        futures to resolve but does not report their outcome, so a flush
        that returns tells you the producer is drained — not that anything
        was delivered. This class collected the futures and then discarded
        them unexamined, which meant a total delivery failure looked
        exactly like a clean publish.

        Found live 2026-08-04 by the Q7 publication readback: every send
        failed ClusterAuthorizationFailedError on the idempotent
        producer's InitProducerId handshake, the run reported publishing
        its alerts, and the topic's end offset was still 0. A pipeline
        that reports published alerts while publishing nothing is worse
        than one that crashes, so the failure is now loud.

        Raises
        ------
        RuntimeError
            If any message produced since the last flush failed. The
            first underlying error is chained as the cause; the message
            counts how many of how many failed.
        """
        self.transport.flush()

        failures = []
        for future in self._pending:
            # A test fake's "future" need not implement this surface; only
            # inspect what can actually report an outcome.
            if not hasattr(future, "succeeded"):
                continue
            try:
                if not future.succeeded():
                    failures.append(future.exception)
            except Exception as exc:               # noqa: BLE001
                failures.append(exc)

        produced = len(self._pending)
        self._pending.clear()

        if failures:
            raise RuntimeError(
                f"{len(failures)} of {produced} alert(s) failed to publish; "
                f"first error: {failures[0]!r}") from (
                    failures[0] if isinstance(failures[0], BaseException)
                    else None)

    def close(self) -> None:
        """Flush and release the transport."""
        self.transport.close()

    def __enter__(self) -> "GlueFramingProducer":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


def make_producer(bootstrap_servers: str, registry: str = DEFAULT_REGISTRY,
                  region: str | None = None,
                  schema_version: str | None = None) -> GlueFramingProducer:
    """Build the production producer: kafka-python + MSK IAM + Glue framing.

    The one call site that assembles the real thing; everything else takes
    the producer by injection.
    """
    transport = make_transport(bootstrap_servers, region=region)
    return GlueFramingProducer(
        transport=transport,
        registry=GlueSchemaRegistry(registry=registry, region=region),
        schema_version=schema_version)
