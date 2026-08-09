"""
File:    alert_production.py

The alert-production job type — the trigger for the complete-but-unwired
alerts path.

What was already here: `alerts/` builds, serializes, publishes and archives
alerts, and has since it was written. What was missing was anything that
DECIDED to run it. `batch_produce(provider, pid, producer=...)` was the
injection seam with nothing on the injecting side; this module is that side.

**A DATABASE-EFFECT JOB TYPE, NOT A PRODUCT-PRODUCING ONE** (step-4 co-design
gate 3, operations design § Alert production). It declares an empty product
set: the alerts go to the stream, not to the product bucket, and the attempt's
terminal record is a pure disposition record. What the attempt records instead
is the alert-specific effect counts the design names by name — candidates
considered, alerts published, candidates dropped by reason, emissions
suppressed — plus per-candidate drop dispositions.

**CANDIDATE FAILURES NEVER FAIL THE ATTEMPT** (gate 3). A cutout that will not
load, a source whose object lookup returns nothing: these drop that candidate,
recorded with its reason, and the other candidates from the same chip publish
normally. The attempt fails only on CHIP-level failure — the provider cannot
open the difference image at all — and on delivery failure, which raises
loudly because a run that reports published alerts while publishing nothing is
worse than one that crashes (the 2026-08-04 Q7 finding, which is why
`GlueFramingProducer.flush` raises).

**EMISSION IS ONCE PER LOGICAL UNIT PER RELEASE** (gate 4). The watermark is
claimed BEFORE publishing, not after: a claim that loses the race means
another emitter has this unit, and this attempt then publishes nothing and
records the suppression. Claiming after publishing would leave a window in
which two attempts both published and only one recorded.

That ordering costs the at-least-once posture nothing, because at-least-once
is the contract (gate 6): a claim that succeeds and a publish that then dies
mid-flight leaves the unit marked emitted with fewer alerts than intended,
which is a duplicate-suppression question the CONSUMER answers by deduplicating
on alert identity. Publishing twice is permitted by the contract; publishing
twice while claiming to have published once is not.
"""

import logging

from pipeline.runtime.errors import InputError

logger = logging.getLogger(__name__)

#: The internal-phase topic. The mission/public stream is not reachable from
#: this job type: the internal namespace is what the publication policy grants
#: (`rapid.internal.alerts.*` in the msk-internal-client policy) and what the
#: step-4 co-design's internal phase measures against.
INTERNAL_ALERT_TOPIC = "rapid.internal.alerts.v1"

#: The PLACEHOLDER selection, labelled as one.
#:
#: The co-design records the selection rule as a placeholder — top N candidates
#: per SCA by signal-to-noise — and it is implemented as exactly that, with the
#: name saying so at every reference. Real selection is a science decision
#: (deterministic cuts, real-bogus classification) that the failure-path design
#: already anticipates as a distinct stage; nothing here should be mistaken for
#: it, and a reader who greps for "placeholder" must find this.
PLACEHOLDER_TOP_N_BY_SNR = 500


def _unit_field(context, name):
    """One declared field of the unit, or a named failure.

    Same contract as the post-DB chain's `_unit_field`: the unit is what the
    manifest says, and a stage that cannot find its declared input fails
    naming it rather than rediscovering it from the catalog.
    """
    fields = getattr(context.unit, "fields", None) or {}
    value = fields.get(name)
    if value is None:
        raise InputError(
            f"the alert-production unit does not declare {name!r}; it is a "
            f"submission fact and this stage has no way to derive one")
    return value


def select_candidates(sources, top_n=PLACEHOLDER_TOP_N_BY_SNR):
    """The PLACEHOLDER selection: the top `top_n` candidates by S/N.

    Named `PLACEHOLDER` in its constant, its docstring and the effect counts
    it produces, because that is what the co-design ruled it to be. It is a
    volume bound with a defensible ordering, not a scientific selection: it
    makes the internal phase's packet-count measurement bounded and
    reproducible while the real cuts are designed.

    Sources whose S/N is unknown sort LAST rather than being dropped. A missing
    measurement is not a low one, and dropping on absence would silently make
    the placeholder a filter on data completeness as well as on brightness.
    """
    ordered = sorted(
        sources,
        key=lambda source: (getattr(source, "snr", None) is None,
                            -(getattr(source, "snr", None) or 0.0)))
    return ordered[:top_n]


def produce_alerts(context) -> None:
    """Publish this unit's alerts, once, and record what happened.

    The whole job type in one stage, because it is one indivisible decision:
    claim the emission, publish under the claim, record the effect. Splitting
    it would create a window between claiming and publishing in which a
    retry could see a claimed-but-unpublished unit and have no way to tell
    that from a published one.
    """
    from alerts.cli import make_provider
    from alerts.produce import (assemble_alert_for_source, load_schema,
                                publish_alert, serialize_alert)

    pid = int(_unit_field(context, "difference_image_pid"))
    attempt_id = int(_unit_field(context, "attempt_id"))
    release_identity = str(_unit_field(context, "release_identity"))
    exposure = int(context.unit.exposure)
    sca = int(context.unit.sca)

    provider = make_provider()
    handle = provider.db

    # THE CLAIM, FIRST. `record_alert_emission` is an INSERT ... ON CONFLICT
    # DO NOTHING against the watermark's primary key, so exactly one caller
    # can win per (unit, release) no matter how many race.
    claimed = handle.record_alert_emission(
        exposure, sca, release_identity, attempt_id, pid=pid,
        alerts_published=0)
    if getattr(handle, "exit_code", 0) >= 64:
        raise RuntimeError(
            f"could not claim the alert emission watermark for unit "
            f"{exposure}/{sca} under release {release_identity}: rapid_db "
            f"exit_code {handle.exit_code}. Refusing to publish unclaimed: "
            f"an emission that is not recorded can be emitted again")

    if not claimed:
        # Already emitted under this release. The ruled behaviour is silence:
        # replays, re-executions and serial-later registrations of the same
        # unit do not re-emit. Recorded as a suppression, which is one of the
        # four effect counts the design names, and closes successfully.
        context.record_effect(
            candidates_considered=0, alerts_published=0,
            emissions_suppressed=1,
            suppression_reason="already emitted under this release",
            alert_release_identity=release_identity,
            alert_difference_image_pid=pid)
        context.logger.info(
            "unit %s/%s already emitted under release %s; publishing nothing",
            exposure, sca, release_identity)
        return

    producer = _make_internal_producer(context)
    schema = load_schema()

    considered = 0
    published = 0
    dropped_by_reason: dict = {}
    drop_dispositions: list = []

    try:
        sources = list(provider.iter_sources(pid))
    except Exception as exc:  # noqa: BLE001
        # CHIP-LEVEL failure: the provider could not read this difference
        # image at all. This is the one case that fails the attempt, per the
        # failure-path design's SCA-level scope.
        raise RuntimeError(
            f"could not read candidates for difference image pid {pid}: "
            f"{exc}") from exc

    selected = select_candidates(sources)
    considered = len(selected)

    for source in selected:
        sid = getattr(source, "sid", None)
        try:
            alert = assemble_alert_for_source(provider, source)
            payload = serialize_alert(alert, schema=schema)
            publish_alert(payload, producer, topic=INTERNAL_ALERT_TOPIC,
                          flush=False)
            published += 1
        except Exception as exc:  # noqa: BLE001 - a candidate, not the chip
            # PER-CANDIDATE DROP (gate 3). The reason is the exception's type
            # rather than its message: the counts are grouped by reason and a
            # message carrying a source id would make every drop its own
            # category.
            reason = type(exc).__name__
            dropped_by_reason[reason] = dropped_by_reason.get(reason, 0) + 1
            drop_dispositions.append({"sid": sid, "reason": reason,
                                      "detail": str(exc)[:200]})
            context.logger.warning("candidate sid=%s dropped (%s): %s",
                                   sid, reason, exc)

    # DELIVERY failure raises loudly — flush() reports what send() deferred.
    producer.flush()

    # The watermark carries the count now that it is known. The claim above
    # wrote zero deliberately: it is what makes the claim safe to take before
    # the work, and this is where it becomes the truth.
    _update_emission_count(handle, exposure, sca, release_identity, published)

    context.record_effect(
        candidates_considered=considered,
        alerts_published=published,
        candidates_dropped=sum(dropped_by_reason.values()),
        dropped_by_reason=dropped_by_reason,
        drop_dispositions=drop_dispositions,
        emissions_suppressed=0,
        alert_topic=INTERNAL_ALERT_TOPIC,
        alert_release_identity=release_identity,
        alert_difference_image_pid=pid,
        selection_rule=f"PLACEHOLDER top-{PLACEHOLDER_TOP_N_BY_SNR}-by-snr",
        sources_available=len(sources))
    context.logger.info(
        "unit %s/%s: %d candidate(s) considered, %d published, %d dropped "
        "(release %s, topic %s)",
        exposure, sca, considered, published,
        sum(dropped_by_reason.values()), release_identity,
        INTERNAL_ALERT_TOPIC)


def _update_emission_count(handle, exposure, sca, release_identity, published):
    """Write the published count onto the claimed watermark row.

    Separate from the claim because `ON CONFLICT DO NOTHING` deliberately does
    not update — that is what makes it a claim rather than a last-writer-wins
    upsert. The count is written here, against the row this attempt owns.
    """
    try:
        handle.cur.execute(
            "update Alert_Emissions set alerts_published = %s "
            "where exposure_id = %s and sca = %s and release_identity = %s;",
            (int(published), int(exposure), int(sca), str(release_identity)))
        handle.conn.commit()
    except Exception as exc:  # noqa: BLE001
        # The alerts are published; failing the attempt now would misreport a
        # successful emission as a failure and invite a retry that the
        # watermark would correctly refuse. Logged loudly instead — the
        # attempt's own effect counts still carry the number.
        logger.warning("could not update the published count on the emission "
                       "watermark for %s/%s: %s", exposure, sca, exc)


def _make_internal_producer(context):
    """The real Kafka producer, on the internal topic.

    THE INJECTION SEAM MADE REAL. `batch_produce`'s `producer=` argument has
    always accepted one; nothing constructed it outside the CLI. The broker is
    a PARAMETER, not an environment variable read at import: the environment
    policy puts nothing that selects a destination in the environment, and a
    misread broker would publish to the wrong cluster silently.
    """
    from alerts.kafka_producer import make_producer

    broker = context.parameter("kafka/bootstrap-brokers")
    if not broker:
        raise InputError(
            "the parameter tree does not carry kafka/bootstrap-brokers; the "
            "alert-production job type publishes to the internal topic and "
            "has no default broker")
    return make_producer(broker)


#: The job type's sequence — one stage, for the reason `produce_alerts` states.
ALERT_PRODUCTION_SEQUENCE = (
    ("produce_alerts", produce_alerts),
)
